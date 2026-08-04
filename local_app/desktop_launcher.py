"""桌面一键启动器（Windows 一键 exe 与开发态共用入口）。

做什么：
1. 选数据目录——PyInstaller 冻结时 = exe 同级 `data/`（便携，整个目录拷走就是备份）；
   开发态 = `local_app/data/`。也可用 DENTAL_DB/DENTAL_IMAGES 环境变量自己指定。
2. 选端口——DENTAL_PORT 显式指定就用它；未指定时 8765 空闲用 8765，
   被占则让系统分一个空闲端口并在控制台说清（前台电脑常已有一个实例在跑）。
3. 起服务（完整复用 run_local 启动序列：建库→v3 首启引导→迁移→附件恢复→建管理员）
   ，健康检查 200 后自动打开浏览器。首启管理员 admin/admin（出厂默认，
   登录页会提醒改密），凭据也存在数据目录的 .admin_initial_password。

冻结打包见 packaging/windows/；本模块不含任何平台专属 API，macOS/Windows 通吃。
"""
from __future__ import annotations

import os
import socket
import sys
import threading
import time
import urllib.request
import webbrowser
from pathlib import Path

_DEFAULT_PORT = 8765


def resolve_data_dir(frozen_exe=None):
    """冻结时给 exe 同级 data/；开发态给 local_app/data/。"""
    if frozen_exe is not None:
        return Path(frozen_exe).parent / "data"
    return Path(__file__).resolve().parent / "data"


def pick_port(preferred=_DEFAULT_PORT):
    """DENTAL_PORT 显式指定就用它（非法值响亮报错）；否则 preferred 空闲用它,
    被占则让系统分一个空闲端口。返回端口号。"""
    env = os.environ.get("DENTAL_PORT", "").strip()
    if env:
        return int(env)  # int() 对非法值抛 ValueError,不静默兜底
    probe = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        probe.bind(("127.0.0.1", preferred))
        return preferred
    except OSError:
        probe2 = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        try:
            probe2.bind(("127.0.0.1", 0))
            return probe2.getsockname()[1]
        finally:
            probe2.close()
    finally:
        probe.close()


def open_browser_when_ready(url, timeout=30.0, interval=0.5):
    """轮询到服务 200 才开浏览器；超时返回 False 且不开。"""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            with urllib.request.urlopen(url, timeout=2) as resp:
                if resp.status == 200:
                    webbrowser.open(url)
                    return True
        except (OSError, ValueError):
            pass
        time.sleep(interval)
    return False


def _frozen_exe():
    if getattr(sys, "frozen", False):
        return Path(sys.executable)
    return None


def apply_desktop_defaults(env):
    """桌面一键版的默认值（环境变量显式设置过的一律不动）：
    - 只听本机：一键版一人一机用,默认不对局域网暴露;要让 iPad/手机连,自己设
      DENTAL_LOCAL_ONLY=0 并配合局域网访问口令。
    - 首启 admin 密码交回核心默认(admin,用户 2026-08-03 拍板:开箱即用优先,
      登录页对 admin 密码弹改密提醒)。默认只听本机,弱口令不直接暴露局域网;
      设 DENTAL_LOCAL_ONLY=0 上局域网前请先改密,并配合局域网访问口令。
      要预设其他密码显式给 DENTAL_ADMIN_PASSWORD。
    """
    env.setdefault("DENTAL_LOCAL_ONLY", "1")


def main():
    # 控制台/重定向管道编码可能是 cp1252 等不认中文的代码页,打印中文横幅会直接崩;
    # 强制 UTF-8 + replace,任何 Windows 语言环境都不因输出编码启动失败。
    for stream in (sys.stdout, sys.stderr):
        if stream is not None and hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")
    apply_desktop_defaults(os.environ)
    frozen = _frozen_exe()
    data_dir = resolve_data_dir(frozen)
    data_dir.mkdir(parents=True, exist_ok=True)

    # 环境变量必须在 import local_app 任何模块之前设好(db.py 在 import 期定库路径)
    os.environ.setdefault("DENTAL_DB", str(data_dir / "clinic.sqlite3"))
    os.environ.setdefault("DENTAL_IMAGES", str(data_dir / "images"))
    port_preset = bool(os.environ.get("DENTAL_PORT", "").strip())
    port = pick_port()
    os.environ["DENTAL_PORT"] = str(port)

    url = f"http://127.0.0.1:{port}/"
    print("=" * 48, flush=True)
    print("  开源诊所系统（桌面一键版）", flush=True)
    print(f"  数据目录: {data_dir}", flush=True)
    print(f"  本机网址: {url}（浏览器会自动打开）", flush=True)
    if port != _DEFAULT_PORT and not port_preset:
        print(f"  提示: 默认端口 {_DEFAULT_PORT} 被占用,本次改用 {port}", flush=True)
    print("  关闭本窗口即停止系统。首启管理员密码见下方。", flush=True)
    print("=" * 48, flush=True)

    threading.Thread(
        target=open_browser_when_ready, args=(url,), daemon=True
    ).start()

    from local_app.run_local import main as run_main

    run_main()


if __name__ == "__main__":
    main()
