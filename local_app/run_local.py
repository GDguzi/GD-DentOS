import os
import secrets
import socket
from pathlib import Path

import uvicorn

from local_app.api import create_app
from local_app.db import DEFAULT_DB_PATH, init_db


def _lan_ip():
    """本机局域网 IP：用 UDP socket 选默认出网卡再读本机地址。
    #69：目标用 RFC5737 TEST-NET 保留地址(192.0.2.1，永不路由到公网)，UDP connect 只选路由
    不发包，既拿到局域网 IP 又不触碰真实公网地址。失败回退 127.0.0.1。"""
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("192.0.2.1", 1))
        return s.getsockname()[0]
    except OSError:
        return "127.0.0.1"
    finally:
        s.close()


def _lan_password():
    """局域网访问口令：优先环境变量 DENTAL_ACCESS_PASSWORD；否则生成一次存到
    data/.lan_password 复用（重启不变），首启打印。"""
    env = os.environ.get("DENTAL_ACCESS_PASSWORD", "").strip()
    if env:
        return env
    path = DEFAULT_DB_PATH.parent / ".lan_password"
    if path.exists():
        saved = path.read_text(encoding="utf-8").strip()
        if saved:
            return saved
    pw = secrets.token_hex(4)  # #67：8 位十六进制(32bit)，自动生成不靠人记；可用 DENTAL_ACCESS_PASSWORD 自设
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(pw, encoding="utf-8")
    return pw


def main():
    # 配置校验必须排在所有副作用之前：放在 create_app() 里太晚——建库、迁移、附件收敛、
    # 种子管理员、PBX watcher 都已经跑完才抛错，一次配置写错就改了生产库。
    from local_app.paid_detail import load_pay_method_aliases
    load_pay_method_aliases()
    init_db(DEFAULT_DB_PATH)
    # Access V3 空库首启:人事权限四表建成 v3 目标结构并差分收敛(生产已迁移库纯 no-op);
    # 有数据的未迁移旧库在此响亮报错,不带旧结构病启动。
    from local_app.v3_bootstrap import ensure_v3_personnel_schema
    ensure_v3_personnel_schema(DEFAULT_DB_PATH)
    # Phase3 迁移机制归一:角色/权限/种子类一次性升级统一走 schema_migrations 有序记账,
    # 跑过即跳过(旧机制是每次启动全量重跑 8 个 ensure_*,各自幂等检查)。登记册见 migrations.py。
    from local_app.migrations import apply_migrations
    applied = apply_migrations(DEFAULT_DB_PATH)
    if applied:
        print(f"  已应用迁移: {', '.join(applied)}", flush=True)
    # SQLite 与文件系统无法共享一个事务：必须在构建应用、接受任何请求前
    # 收敛上次中断的客户通附件操作。不一致时直接阻止启动，不带病服务。
    images_dir = Path(
        os.environ.get("DENTAL_IMAGES", "").strip()
        or (DEFAULT_DB_PATH.parent / "images")
    )
    from local_app.attachment_file_ops import recover_attachment_file_ops
    recover_attachment_file_ops(DEFAULT_DB_PATH, images_dir)
    # 账号系统：所有人(含本机)都需登录，操作才能溯源到人。首启无用户则建 admin(账号供给,不属迁移)。
    from local_app.auth import ensure_seed_admin
    seeded = ensure_seed_admin(DEFAULT_DB_PATH)
    # #856 通话 AI 扩展在场才做(不在场则这两步整体缺席)
    from local_app.routes.calls import call_ai_available
    if call_ai_available():
        # 二期:启动扫描转写残留 running→failed + 清 >24h 临时目录(失败即退出,不带脏态就绪)
        from local_app.call_transcribe import _sweep_interrupted_transcripts
        _sweep_interrupted_transcripts(DEFAULT_DB_PATH)
        # 三期③:电话盒子自动监听(配置 DENTAL_PBX_WATCH_DIR 才启动,未配置静默缺席)
        from local_app.routes.pbx_import import _start_pbx_watcher
        _start_pbx_watcher(DEFAULT_DB_PATH, images_dir)
    # 默认局域网联机(iPad/手机签字)。强制只本机：DENTAL_LOCAL_ONLY=1
    local_only = os.environ.get("DENTAL_LOCAL_ONLY", "").strip().lower() in ("1", "true", "yes", "on")
    lan = not local_only
    host = "0.0.0.0" if lan else "127.0.0.1"
    # 端口默认 8765；设 DENTAL_PORT 可换端口，便于演示实例与主实例同机并行。
    try:
        port = int(os.environ.get("DENTAL_PORT", "").strip() or 8765)
    except ValueError:
        port = 8765

    print("=" * 48, flush=True)
    if lan:
        print("  局域网联机已开启（iPad/手机签字）", flush=True)
        print(f"  网址（同一WiFi打开）: http://{_lan_ip()}:{port}", flush=True)
    print("  请用账号登录（本机也需登录，以便操作溯源到人）。", flush=True)
    if seeded:
        print(f"  ★ 首次启动已建管理员：{seeded[0]} / {seeded[1]}  （请尽快登录后改密）", flush=True)
        print("    凭据也已存到 data/.admin_initial_password", flush=True)
    print("=" * 48, flush=True)

    # 影像目录默认 data/images；设 DENTAL_IMAGES 可换(演示实例用独立占位图目录,
    # 与 DENTAL_DB/DENTAL_PORT 一起支撑「演示实例与主实例同机并行」,互不污染)。
    # access_log=False: 患者搜索走 GET ?q=姓名/电话，开访问日志会把患者明文
    # 打进终端/重定向文件，违反"日志不含患者明文"的隐私铁律。
    uvicorn.run(
        # 2026-07-17 v3 开闸(审计Y19,用户拍板):人员权限中心 v3 接管全部认证/授权——
        # 路由策略表(243条)+员工岗位权限+is_system_admin;数据已由 personnel_access_migration 迁移换库
        create_app(DEFAULT_DB_PATH, images_dir=images_dir, access_v3=True),
        host=host,
        port=port,
        access_log=False,
    )


if __name__ == "__main__":
    main()
