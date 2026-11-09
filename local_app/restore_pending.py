"""从备份恢复：上传的整库备份先校验、暂存，下次启动、建库之前才换入。
运行中的服务不碰主库文件；换入前给当前库留一份安全备份，恢复本身可撤销。"""
import io
import json
import os
import re
import shutil
import sqlite3
import tempfile
import zipfile
from pathlib import Path, PurePosixPath

from local_app.backup_util import make_backup
from local_app.timeutil import bj_now

REQUIRED_TABLES = ("patients", "medical_records", "appointments")
FULL_BACKUP_MESSAGE = ("这是本软件的整库备份，不能用「导入患者」——那样只会导入患者名单，"
                       "病历、预约、收费都会丢。请用「从备份恢复」。")
_ZIP_DB_RE = re.compile(r"^backup_\d{8}_\d{6}(?:_\d+)?\.db$")
_PENDING = "restore_pending"
_MANIFEST = "manifest.json"
_STAGED_DB = "clinic.sqlite3"


def is_full_backup_tables(tables):
    return set(REQUIRED_TABLES) <= set(tables)


def reject_full_backup_zip(data):
    """整库备份 zip 误进「导入患者」时明确拦下；其他文件交回原解析流程。"""
    if data[:2] != b"PK":
        return
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as zf:
            names = zf.namelist()
    except zipfile.BadZipFile:
        return
    if any(_ZIP_DB_RE.match(n) for n in names):
        raise ValueError(FULL_BACKUP_MESSAGE)


def _pending_dir(db_path):
    return Path(db_path).parent / _PENDING


def _inspect_db(path):
    """校验备份库并返回各表行数；不是完整可读的本软件备份就抛 ValueError。"""
    try:
        conn = sqlite3.connect(f"{Path(path).resolve().as_uri()}?mode=ro", uri=True)
    except sqlite3.Error:
        raise ValueError("备份文件打不开")
    try:
        try:
            tables = {r[0] for r in conn.execute("select name from sqlite_master where type = 'table'")}
            if not is_full_backup_tables(tables):
                raise ValueError("这个文件不是本软件的整库备份")
            if conn.execute("pragma quick_check(1)").fetchone()[0] != "ok":
                raise ValueError("备份文件已损坏")
            return {t: conn.execute(f"select count(*) from {t}").fetchone()[0] for t in REQUIRED_TABLES}
        except sqlite3.DatabaseError:
            raise ValueError("备份文件损坏或不是数据库文件")
    finally:
        conn.close()


def _extract_zip(src, stage):
    """取出 zip 里的备份库和 images/；路径逃逸的条目整包拒绝。返回影像张数。"""
    try:
        zf = zipfile.ZipFile(src)
    except zipfile.BadZipFile:
        raise ValueError("备份压缩包损坏")
    with zf:
        infos = [i for i in zf.infolist() if not i.is_dir()]
        dbs = [i for i in infos if _ZIP_DB_RE.match(i.filename)]
        if len(dbs) != 1:
            raise ValueError("压缩包里没有找到本软件的备份库")
        count = 0
        for info in infos:
            parts = PurePosixPath(info.filename.replace("\\", "/")).parts
            if info.filename.startswith(("/", "\\")) or ".." in parts or (parts and ":" in parts[0]):
                raise ValueError("压缩包路径不安全，已拒绝")
            if info is dbs[0]:
                target = stage / _STAGED_DB
            elif parts[0] == "images" and len(parts) > 1:
                target = stage.joinpath(*parts)
                count += 1
            else:
                continue
            target.parent.mkdir(parents=True, exist_ok=True)
            with zf.open(info) as fin, open(target, "wb") as fout:
                shutil.copyfileobj(fin, fout, 1024 * 1024)
    return count


def stage_restore(db_path, fileobj, filename):
    """校验上传的备份并暂存。整个目录就绪后才改名成正式暂存目录，半截上传不会被启动时换入。"""
    data_dir = Path(db_path).parent
    data_dir.mkdir(parents=True, exist_ok=True)
    stage = Path(tempfile.mkdtemp(prefix="restore_incoming.", dir=data_dir))
    try:
        upload = stage / "upload.bin"
        with open(upload, "wb") as fout:
            shutil.copyfileobj(fileobj, fout, 1024 * 1024)
        with open(upload, "rb") as fh:
            magic = fh.read(16)
        image_count = 0
        if magic[:2] == b"PK":
            image_count = _extract_zip(upload, stage)
            upload.unlink()
        elif magic == b"SQLite format 3\x00":
            upload.replace(stage / _STAGED_DB)
        else:
            raise ValueError("只能恢复本软件生成的备份文件（backup_….db 或 .zip）")
        summary = {"filename": os.path.basename(filename or ""), "image_count": image_count,
                   "counts": _inspect_db(stage / _STAGED_DB),
                   "staged_at": bj_now().strftime("%Y-%m-%d %H:%M:%S")}
        (stage / _MANIFEST).write_text(json.dumps(summary, ensure_ascii=False), encoding="utf-8")
        cancel_restore(db_path)
        stage.replace(_pending_dir(db_path))
        return summary
    except BaseException:
        shutil.rmtree(stage, ignore_errors=True)
        raise


def pending_restore(db_path):
    manifest = _pending_dir(db_path) / _MANIFEST
    if not manifest.is_file():
        return None
    return json.loads(manifest.read_text(encoding="utf-8"))


def cancel_restore(db_path):
    pending = _pending_dir(db_path)
    existed = pending.exists()
    if existed:
        shutil.rmtree(pending)
    return existed


def _move_images(src, dest):
    for root, _dirs, files in os.walk(src):
        for name in files:
            source = Path(root) / name
            target = Path(dest) / source.relative_to(src)
            target.parent.mkdir(parents=True, exist_ok=True)
            target.unlink(missing_ok=True)
            shutil.move(str(source), str(target))


def apply_pending_restore(db_path, images_dir):
    """启动时、任何连接打开之前调用。中途断电下次重来：影像可重复合并，库换入后才清暂存。"""
    db_path = Path(db_path)
    for stale in db_path.parent.glob("restore_incoming.*"):
        shutil.rmtree(stale, ignore_errors=True)
    pending = _pending_dir(db_path)
    summary = pending_restore(db_path)
    if summary is None:
        return None
    staged = pending / _STAGED_DB
    if staged.is_file():
        safety = make_backup(db_path, reason="pre_restore_upload").name if db_path.exists() else ""
        summary["safety"] = safety
        (pending / _MANIFEST).write_text(json.dumps(summary, ensure_ascii=False), encoding="utf-8")
        if (pending / "images").is_dir():
            _move_images(pending / "images", images_dir)
        for suffix in ("-wal", "-shm"):
            Path(f"{db_path}{suffix}").unlink(missing_ok=True)
        os.replace(staged, db_path)
    shutil.rmtree(pending)
    return summary
