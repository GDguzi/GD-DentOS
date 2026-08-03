"""数据库备份/回滚共用工具：原子热备(VACUUM INTO) + 轮转 + 回滚。

与 routes/backup.py 同目录同命名(data/backups/backup_YYYYMMDD_HHMMSS.db)，
所以同步前自动备份的文件也会出现在「备份」界面里，可下载。

同步前调 make_backup() 留一份可回滚的整库副本；出问题用 restore_backup() 回到那一刻。
红线：只动库文件,不打印患者数据。
"""
import datetime as dt
import filecmp
import json
import os
import re
import shutil
import socket
import stat
import time
import uuid
from pathlib import Path

from local_app.db import audit_write, connect
from local_app.timeutil import bj_now

# 备份名:本接口/同步前备份统一 backup_YYYYMMDD_HHMMSS[_n].db|zip
_BACKUP_NAME_RE = re.compile(r"^backup_\d{8}_\d{6}(?:_\d+)?\.(?:db|zip)$")
# 轮转保留窗口(天)。诊所要求定期删过去的备份(默认约两周;可用环境变量调,设短到一周也行)。
DEFAULT_KEEP_DAYS = int(os.environ.get("DENTAL_BACKUP_KEEP_DAYS", "14") or "14")
# 无论多旧,至少保留最近这么多份 .db,确保任何时候都有近期可回滚整库副本(防一次全删光)。
MIN_KEEP_DB = int(os.environ.get("DENTAL_BACKUP_MIN_KEEP", "7") or "7")


def _backups_dir(db_path):
    d = Path(db_path).parent / "backups"
    d.mkdir(parents=True, exist_ok=True)
    return d


def sweep_stale_tmp(db_path):
    """清理强杀/断电遗留的两类整库临时文件:恢复的 *.restore_tmp(生产库旁,含全量患者数据)
    与备份的 backup_*.part(备份目录内,大文件残留可占满磁盘,让后续备份永远失败)。
    必须在每个备份/恢复入口"动手前"清——若等备份成功后再清,磁盘被残留占满时永远等不到成功。
    只清超过 1 小时的:正常备份/恢复几秒到几分钟就完,绝不误删并发流程正在写的文件。"""
    db_path = Path(db_path)
    stale = list(db_path.parent.glob(db_path.name + ".*.restore_tmp"))
    stale += (db_path.parent / "backups").glob("backup_*.part")
    for f in stale:
        try:
            if f.is_file() and time.time() - f.stat().st_mtime > 3600:
                f.unlink(missing_ok=True)
        except OSError:
            pass   # 单个残留清不掉不阻断备份/恢复主流程


def claim_backup_name(tmp, final):
    """把写好的临时文件原子挂上正式备份名。os.link 在目标已存在时抛错——
    绝不覆盖同秒竞争者(每日自动 vs 同步前手工)已完成的备份;撞名自动改挂 _n 后缀。
    返回实际挂上的正式路径,并删掉临时名。"""
    tmp, final = Path(tmp), Path(final)
    base, ext = final.name.rsplit(".", 1)
    i = 1
    while True:
        try:
            os.link(tmp, final)
            tmp.unlink(missing_ok=True)
            return final
        except FileExistsError:
            pass
        except OSError:
            # exFAT/FAT 等无硬链接的文件系统:退化为 O_EXCL 独占占位+换入,保住"绝不覆盖"。
            # ponytail: 占位与换入之间被强杀会留 0 字节正式名(窗口毫秒级);硬链接路径无此窗口
            try:
                os.close(os.open(final, os.O_CREAT | os.O_EXCL | os.O_WRONLY))
            except FileExistsError:
                pass
            else:
                try:
                    os.replace(tmp, final)
                except BaseException:
                    final.unlink(missing_ok=True)   # 占位是本次新建的:换入失败必须收回,
                    raise                           # 0 字节文件挂正式名会被当"最新正常备份"
                return final
        final = final.parent / f"{base}_{i}.{ext}"
        i += 1


def make_backup(db_path, reason="manual"):
    """原子热备整库 → data/backups/backup_YYYYMMDD_HHMMSS.db。返回备份文件 Path。写审计。"""
    db_path = Path(db_path)
    backups = _backups_dir(db_path)
    sweep_stale_tmp(db_path)              # 动手前先收走强杀/断电遗留的过期临时文件(防残留占满磁盘卡死备份链)
    stamp = bj_now().strftime("%Y%m%d_%H%M%S")
    # 先备到本次调用独有的临时名(.part 不匹配备份命名,不进列表/轮转/回滚),成功才认领正式名——
    # ①VACUUM INTO 中途失败(磁盘满)会留半截文件,半截文件挂正式名会被当"正常备份";
    # ②临时名带随机段:同秒并发的两次备份互不踩对方的临时文件。
    tmp = backups / f"backup_{stamp}.{uuid.uuid4().hex[:8]}.part"
    conn = connect(db_path)                # 显式管理连接生命周期(用完即关,不留句柄)
    try:
        conn.execute("vacuum into ?", (str(tmp),))
        out = claim_backup_name(tmp, backups / f"backup_{stamp}.db")   # 同秒撞名自动挂 _n,绝不互覆盖
        audit_write(conn, "backup", out.name, "pre_sync_backup",
                    new_json=json.dumps({"reason": reason, "size_bytes": out.stat().st_size},
                                        ensure_ascii=False),
                    operator="auto-sync")
        conn.commit()
    except BaseException:
        tmp.unlink(missing_ok=True)        # 失败不留半截临时文件
        raise
    finally:
        conn.close()
    return out


# 手工快照(改动前"先存一份"留的整库副本,如 clinic.backup_before_xxx.sqlite3、
# data/backups/ 下 clinic_before_xxx.sqlite3):命名不固定,不受 _BACKUP_NAME_RE 覆盖,
# prune_backups 管不到,会无限累积(07-11 排查发现攒了 40+ 份共约 11G)。
MANUAL_SNAPSHOT_KEEP = int(os.environ.get("DENTAL_MANUAL_SNAPSHOT_KEEP", "7") or "7")


def prune_manual_snapshots(db_path, nas_dir, keep=None):
    """清理手工整库快照:data/ 根目录下 clinic*.sqlite3(排除生产库本体)+ data/backups/
    下不符合 backup_YYYYMMDD_HHMMSS 命名的 clinic_before_*.sqlite3,按 mtime 只留最近 keep 份。
    更旧的份先拷到 nas_dir/手工快照/ 校验字节数一致再删本地;nas_dir.parent 没真挂载(os.path.ismount,
    不只是目录存在)则整批跳过删除,留到下次 NAS 在线再清——不在没有异地副本的情况下丢本地最后一份。
    候选只认普通文件(排除软链接,防止拷走链接指向的任意内容/写穿 NAS 侧同名链接)。
    NAS 侧撞名(如 data/ 和 data/backups/ 各有一份同名快照)时:逐字节比对确认同一副本才删本地;
    内容不同、目标是软链接/目录/非普通文件一律判冲突,保留本地原件不删,计入 skipped_conflict 交人工看。
    单份拷贝/改名失败不牵连其它候选,保留本地原件,计入 failed。
    返回 {"moved": n, "skipped_no_nas": n, "skipped_conflict": n, "failed": n}。"""
    db_path = Path(db_path)
    keep = MANUAL_SNAPSHOT_KEEP if keep is None else keep
    backups = _backups_dir(db_path)

    def _regular_files(it):
        return [f for f in it if f.is_file() and not f.is_symlink()]

    candidates = _regular_files(f for f in db_path.parent.glob("clinic*.sqlite3") if f.name != db_path.name)
    candidates += _regular_files(backups.glob("clinic_before_*.sqlite3"))
    candidates.sort(key=lambda f: f.stat().st_mtime, reverse=True)
    stale = candidates[keep:]
    result = {"moved": 0, "skipped_no_nas": 0, "skipped_conflict": 0, "failed": 0}
    if not stale:
        return result
    nas_dir = Path(nas_dir)
    if not os.path.ismount(nas_dir.parent):  # 只有真挂载的卷才算"有异地副本可用"
        result["skipped_no_nas"] = len(stale)
        return result
    dest_dir = nas_dir / "手工快照"
    dest_dir.mkdir(parents=True, exist_ok=True)
    for f in stale:
        dest = dest_dir / f.name
        tmp = dest_dir / (f.name + ".part")
        if dest.is_symlink() or tmp.is_symlink():   # NAS 侧被占成软链接,绝不写穿
            result["skipped_conflict"] += 1
            continue
        if dest.exists():
            # #737/#738 同名目标必须是普通文件且逐字节确认是同一副本才敢删本地;
            # 目录/设备等非普通文件、同大小异内容(st_size 相等不代表内容相等)一律判冲突
            try:
                same = dest.is_file() and filecmp.cmp(f, dest, shallow=False)
            except OSError:
                same = False                         # NAS 读不动 → 按冲突保留本地
            if same:
                f.unlink()                           # 确认已归档同一副本
                result["moved"] += 1
            else:
                result["skipped_conflict"] += 1
            continue
        try:
            shutil.copy2(f, tmp)
            if tmp.stat().st_size == f.stat().st_size:
                tmp.rename(dest)
                f.unlink()
                result["moved"] += 1
            else:
                result["failed"] += 1
        except OSError:
            result["failed"] += 1
        finally:
            if tmp.exists():
                tmp.unlink(missing_ok=True)          # 拷坏/拷断的半成品不留
    return result


def prune_backups(db_path, keep_days=None, min_keep=None):
    """按年龄轮转 data/backups 下的备份(.db + 含影像 .zip):删超过 keep_days 天的旧备份,
    但永远保留最近 min_keep 份 .db(确保任何时候都有近期可回滚整库副本,防一次删光)。
    每次新建备份后调用 → 等于"定期清理过去的"(备份创建本身就是定时节律)。返回删除数。
    备份都是单一生产库 clinic.sqlite3 的自包含整库副本(VACUUM INTO);含影像版是 zip 打包。"""
    keep_days = DEFAULT_KEEP_DAYS if keep_days is None else keep_days
    min_keep = MIN_KEEP_DB if min_keep is None else min_keep
    backups = _backups_dir(db_path)
    files = [f for f in backups.iterdir() if f.is_file() and _BACKUP_NAME_RE.match(f.name)]
    # 最近 min_keep 份 .db 受保护,无论多旧都不删
    dbs = sorted([f for f in files if f.name.endswith(".db")], key=lambda f: f.name, reverse=True)
    protected = set(dbs[:max(0, min_keep)])
    cutoff = time.time() - keep_days * 86400
    removed = 0
    for f in files:
        if f in protected:
            continue
        if f.stat().st_mtime < cutoff:
            f.unlink(missing_ok=True)
            removed += 1
    return removed


def list_backups(db_path):
    backups = _backups_dir(db_path)
    out = []
    for f in sorted(backups.glob("backup_*.db"), key=lambda f: f.name, reverse=True):
        st = f.stat()
        out.append({"name": f.name, "size_bytes": st.st_size,
                    "created_at": dt.datetime.fromtimestamp(st.st_mtime).strftime("%Y-%m-%d %H:%M:%S")})
    return out


def _service_running(port=None):
    """#569:探测本机 web 服务端口是否还在监听——restore_backup 是破坏性操作(删 WAL+覆盖库文件),
    不能只靠 docstring 提醒操作者"调用前务必停掉写入方"，服务真在跑时必须程序性拒绝。
    只探测本机 loopback，不联网。"""
    port = int(port or os.environ.get("DENTAL_PORT", "").strip() or 8765)
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.settimeout(0.5)
        return s.connect_ex(("127.0.0.1", port)) == 0


def restore_backup(db_path, name, force=False):
    """回滚：先把当前库另存一份安全备份(回滚也可逆),再用指定备份覆盖当前库。
    处理 WAL：删 -wal/-shm,确保副本生效。**调用前务必停掉写入方(web 服务/同步)**——
    #569:不再只是文档提醒,默认会探测本机服务端口，服务还在监听就拒绝执行(force=True 跳过探测)。
    返回 {restored, safety}。"""
    db_path = Path(db_path)
    backups = _backups_dir(db_path)
    src = backups / name
    if not src.is_file() or not name.startswith("backup_") or not name.endswith(".db"):
        raise FileNotFoundError(f"备份不存在: {name}")
    if not force and _service_running():
        raise RuntimeError(
            "检测到本机服务端口仍在监听，回滚前必须先停掉 web 服务/同步进程——"
            "服务存活时删 WAL 再覆盖库文件可能与在途写入冲突，静默损坏数据库。"
            "停止服务后重试；确认已手动处理可传 force=True 跳过这项探测。")
    sweep_stale_tmp(db_path)              # 收走上次恢复/备份断电/强杀遗留的过期临时文件
    # 回滚前先给当前库留安全备份(这样回滚本身也能撤销;此备份已含当前 WAL 里的改动)
    safety = make_backup(db_path, reason=f"pre_restore_of_{name}")
    # 先把备份拷到同目录、本次调用独有的临时文件——①拷一半失败(磁盘满)不得损毁生产库本体;
    # ②临时名带随机段:两个并发恢复(都 force=True)互不踩对方的临时文件。
    # 换入完成前的任何失败点都必须清掉临时副本:它是整库患者数据,不入轮转,残留即积累敏感副本。
    tmp = Path(f"{db_path}.{uuid.uuid4().hex[:8]}.restore_tmp")
    try:
        st = os.stat(db_path)
        # 以最紧权限(0600)独占新建临时文件再灌数据——复制进行中的整库副本
        # 不得有任何一瞬间按 umask 默认(0644)对本机其他账号可读
        fd = os.open(tmp, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        with os.fdopen(fd, "wb") as fout, open(src, "rb") as fin:
            shutil.copyfileobj(fin, fout, 1024 * 1024)
        os.chmod(tmp, stat.S_IMODE(st.st_mode))   # 复制完再对齐生产库现有权限
        chown = getattr(os, "chown", None)         # Windows 无 os.chown,不得因此中断恢复
        if chown:
            try:
                chown(tmp, st.st_uid, st.st_gid)   # sudo/跨账号恢复时保住原属主
            except PermissionError:
                pass   # 非特权同用户场景无需也无法改属主
        # ponytail: macOS ACL/扩展属性不随拷贝保留,本部署(单用户 Mac)不用 ACL;要用得换 copystat+xattr
        # 再清 WAL/SHM(否则旧 -wal 会盖回覆盖后的库,回滚失效),最后原子换入自包含副本
        for suffix in ("-wal", "-shm"):
            p = Path(str(db_path) + suffix)
            if p.exists():
                p.unlink()
        os.replace(tmp, db_path)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise
    return {"restored": name, "safety": safety.name}
