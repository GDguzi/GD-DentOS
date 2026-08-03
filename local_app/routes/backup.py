"""数据备份：把本地 SQLite 原子热备(VACUUM INTO)成带时间戳的备份文件，可列表、可下载。
可选连同影像目录打成 zip。备份落 data/backups/(不进 Git)。"""
import datetime as dt
import json
import os
import re
import uuid
import zipfile
from pathlib import Path

from fastapi import APIRouter, HTTPException
from fastapi.responses import FileResponse

from local_app.timeutil import bj_now
from local_app.auth import audit_write, require_admin
from local_app.backup_util import claim_backup_name, prune_backups, sweep_stale_tmp
from local_app.db import connect

_NAME_RE = re.compile(r"^backup_\d{8}_\d{6}(?:_\d+)?\.(db|zip)$")  # 只认本接口/同步前备份命名(含同秒 _n 后缀)，挡路径逃逸




def create_backup_router(db_path, images_dir):
    router = APIRouter()
    db_path = Path(db_path)
    images_dir = Path(images_dir)
    backups_dir = db_path.parent / "backups"

    @router.post("/api/backup")
    def create_backup(payload: dict = None):
        require_admin()   # #102 备份含全量隐私，仅管理员
        payload = payload or {}
        include_images = bool(payload.get("include_images"))
        backups_dir.mkdir(parents=True, exist_ok=True)
        sweep_stale_tmp(db_path)   # 动手前先收走强杀/断电遗留的过期临时文件(防残留占满磁盘卡死备份)
        stamp = bj_now().strftime("%Y%m%d_%H%M%S")
        db_backup = backups_dir / f"backup_{stamp}.db"
        # 同秒撞名不再 409 拒绝:认领逻辑会原子挂 _n 后缀,每日自动与手工备份同秒各得一份
        # 全程先写本次调用独有的 .part 临时名(不匹配 _NAME_RE,不进备份列表),成功才认领正式名——
        # ①中途磁盘满等故障留下的半截 zip/db 若挂正式名,会被当"正常备份"展示,真恢复时才发现是空炮;
        # ②临时名带随机段+认领用 os.link:同秒并发(手工 vs 每日自动)互不踩、绝不静默互覆盖
        rand = uuid.uuid4().hex[:8]
        db_tmp = backups_dir / f"backup_{stamp}.db.{rand}.part"
        zip_tmp = backups_dir / f"backup_{stamp}.zip.{rand}.part"
        try:
            # VACUUM INTO 原子热备：在线不锁库，生成自包含的整库副本
            with connect(db_path) as conn:
                conn.execute("vacuum into ?", (str(db_tmp),))

            if include_images:
                with zipfile.ZipFile(zip_tmp, "w", zipfile.ZIP_DEFLATED) as zf:
                    zf.write(db_tmp, arcname=db_backup.name)
                    if images_dir.is_dir():
                        for root, _dirs, files in os.walk(images_dir):
                            for fn in files:
                                full = Path(root) / fn
                                # #568：只跳过 os.walk 枚举后被并发删影像删掉的文件(FileNotFoundError);
                                # 磁盘满/权限/IO 等真故障(其它 OSError)照常抛出,不把失败备份伪装成成功
                                try:
                                    zf.write(full, arcname=str(Path("images") / full.relative_to(images_dir)))
                                except FileNotFoundError:
                                    continue
                db_tmp.unlink()  # 已并入 zip，删掉中间 .db
                result = claim_backup_name(zip_tmp, backups_dir / f"backup_{stamp}.zip")
            else:
                result = claim_backup_name(db_tmp, db_backup)
        except BaseException:
            db_tmp.unlink(missing_ok=True)   # 失败不留任何残渣,列表里看不到失败备份
            zip_tmp.unlink(missing_ok=True)
            raise

        with connect(db_path) as conn:
            audit_write(conn, "backup", result.name, "create_backup",
                        new_json=json.dumps({"include_images": include_images,
                                             "size_bytes": result.stat().st_size}, ensure_ascii=False))
            conn.commit()
        # 定期清理：每次新建备份后按年龄轮转,删过期旧备份(保留近期可回滚副本)
        pruned = prune_backups(db_path)
        return {"name": result.name, "size_bytes": result.stat().st_size,
                "include_images": include_images, "pruned": pruned}

    @router.get("/api/backup/list")
    def list_backups():
        require_admin()
        if not backups_dir.is_dir():
            return {"list": [], "totalcount": 0}
        items = []
        for f in backups_dir.iterdir():
            if f.is_file() and _NAME_RE.match(f.name):
                st = f.stat()
                items.append({
                    "name": f.name,
                    "size_bytes": st.st_size,
                    "created_at": dt.datetime.fromtimestamp(st.st_mtime).strftime("%Y-%m-%d %H:%M:%S"),
                    "kind": "zip" if f.name.endswith(".zip") else "db",
                })
        items.sort(key=lambda x: x["name"], reverse=True)
        return {"list": items, "totalcount": len(items)}

    @router.get("/api/backup/{name}/download")
    def download_backup(name: str):
        require_admin()
        if not _NAME_RE.match(name):
            raise HTTPException(status_code=404, detail="备份不存在")
        file_path = (backups_dir / name).resolve()
        if not file_path.is_relative_to(backups_dir.resolve()) or not file_path.is_file():
            raise HTTPException(status_code=404, detail="备份不存在")
        return FileResponse(file_path, filename=name, media_type="application/octet-stream")

    return router
