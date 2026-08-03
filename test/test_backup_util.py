"""同步前备份 + 回滚：make_backup 整库热备 / prune 轮转 / restore 回退(含安全备份)。"""
import tempfile
import unittest
from pathlib import Path

from local_app.db import connect, init_db
from local_app import backup_util


class BackupUtilTest(unittest.TestCase):
    def test_make_backup_is_valid_copy(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "clinic.sqlite3"; init_db(db)
            with connect(db) as c:
                c.execute("insert into patients(patient_identity,display_name,updated_at,current_hash) values ('pX','甲','x','h')")
                c.commit()
            bk = backup_util.make_backup(db, reason="test")
            self.assertTrue(bk.exists() and bk.name.startswith("backup_") and bk.name.endswith(".db"))
            with connect(bk) as c:                       # 备份可独立打开,含数据
                n = c.execute("select count(*) from patients where patient_identity='pX'").fetchone()[0]
            self.assertEqual(n, 1)

    def test_prune_keeps_recent(self):
        # 改为按年龄轮转 + min_keep 兜底:4 份均已超期,min_keep=2 → 只留最近 2 份
        import os
        import time
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "clinic.sqlite3"; init_db(db)
            bdir = db.parent / "backups"; bdir.mkdir(parents=True, exist_ok=True)
            old_t = time.time() - 30 * 86400   # 都设成 30 天前(超过 keep_days)
            for stamp in ["20260601_010101", "20260602_010101", "20260603_010101", "20260604_010101"]:
                p = bdir / f"backup_{stamp}.db"; p.write_text("x"); os.utime(p, (old_t, old_t))
            removed = backup_util.prune_backups(db, keep_days=7, min_keep=2)
            left = sorted(f.name for f in bdir.glob("backup_*.db"))
            self.assertEqual(removed, 2)
            self.assertEqual(left, ["backup_20260603_010101.db", "backup_20260604_010101.db"])  # 留最近2份

    def test_restore_reverts_and_keeps_safety(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "clinic.sqlite3"; init_db(db)
            c = connect(db)
            c.execute("insert into patients(patient_identity,display_name,updated_at,current_hash) values ('pX','甲','x','h')")
            c.commit(); c.close()
            bk = backup_util.make_backup(db, reason="before")
            # 模拟同步把库改坏：删掉 pX(回滚前写入方须停,这里显式关连接模拟)
            c = connect(db)
            c.execute("delete from patients where patient_identity='pX'"); c.commit(); c.close()
            # #569:本测试验证的是回滚机制本身,不是服务探测门禁(那个有专门测试)——
            # force=True 跳过探测,避免真机上若恰好开着 8765 服务导致本测试假失败。
            res = backup_util.restore_backup(db, bk.name, force=True)
            c = connect(db)
            n = c.execute("select count(*) from patients where patient_identity='pX'").fetchone()[0]
            c.close()
            self.assertEqual(n, 1)                        # pX 回来了
            self.assertTrue(res["safety"].startswith("backup_"))   # 回滚前留了安全备份
            self.assertTrue((db.parent / "backups" / res["safety"]).exists())

    def test_restore_copy_failure_leaves_db_intact(self):
        # Y22(代码审计_2026-07-17):回滚拷贝中途失败(磁盘满)不得损毁生产库——
        # 旧实现 copyfile 直写生产库路径,写一半抛错=库文件被截断,只能手工救。
        import errno
        from unittest import mock
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "clinic.sqlite3"; init_db(db)
            c = connect(db)
            c.execute("insert into patients(patient_identity,display_name,updated_at,current_hash) values ('pX','甲','x','h')")
            c.commit(); c.close()
            bk = backup_util.make_backup(db, reason="before")

            def broken_copy(fin, fout, *a):
                fout.write(b"broken-half-copy")   # 往临时文件写了一半就断,模拟磁盘满
                raise OSError(errno.ENOSPC, "No space left on device")

            with mock.patch("local_app.backup_util.shutil.copyfileobj", side_effect=broken_copy):
                with self.assertRaises(OSError):
                    backup_util.restore_backup(db, bk.name, force=True)
            # 生产库必须完好:能打开、数据还在
            c = connect(db)
            n = c.execute("select count(*) from patients where patient_identity='pX'").fetchone()[0]
            c.close()
            self.assertEqual(n, 1)
            # 不留半截临时文件
            self.assertEqual([p.name for p in Path(tmp).glob("*.restore_tmp")], [])

    def test_make_backup_failure_leaves_no_halfbaked_file(self):
        # Y24 根因同款:VACUUM INTO 中途失败(磁盘满)会留下半截文件;
        # 半截文件若挂正式 backup_*.db 名,会被备份列表当"正常备份"展示,真恢复时才发现是空炮。
        import errno
        from unittest import mock
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "clinic.sqlite3"; init_db(db)
            bdir = db.parent / "backups"
            real_connect = backup_util.connect

            class BoomConn:
                def __init__(self, inner): self._inner = inner
                def execute(self, sql, params=()):
                    if sql.startswith("vacuum into"):
                        Path(params[0]).write_bytes(b"partial")   # 模拟 vacuum 写了一半炸掉
                        raise OSError(errno.ENOSPC, "No space left on device")
                    return self._inner.execute(sql, params)
                def commit(self): self._inner.commit()
                def close(self): self._inner.close()

            with mock.patch("local_app.backup_util.connect", side_effect=lambda p: BoomConn(real_connect(p))):
                with self.assertRaises(OSError):
                    backup_util.make_backup(db, reason="test")
            self.assertEqual(list(bdir.glob("backup_*.db")), [], "失败不得留下正式命名的半截备份")
            self.assertEqual(list(bdir.glob("*.part")), [], "半截临时文件必须清掉")

    def test_restore_preserves_db_file_mode(self):
        # P1:回滚经临时文件+os.replace 换入的是新 inode——若不显式拷权限,
        # 收紧过的生产库权限(0600)会被放宽成 umask 默认(通常 0644),患者库变本机他人可读。
        import os
        import stat
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "clinic.sqlite3"; init_db(db)
            bk = backup_util.make_backup(db, reason="before")
            os.chmod(db, 0o600)
            backup_util.restore_backup(db, bk.name, force=True)
            mode = stat.S_IMODE(db.stat().st_mode)
            self.assertEqual(mode, 0o600, f"回滚不得放宽生产库权限,实际 {oct(mode)}")

    def test_make_backup_same_second_never_overwrites(self):
        # P2:同一秒两次备份(每日自动+同步前重叠)不得静默互覆盖——
        # 后者必须另挂 _n 后缀,两份都完整可开。
        from unittest import mock
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "clinic.sqlite3"; init_db(db)
            fixed = backup_util.bj_now().replace(microsecond=0)
            with mock.patch("local_app.backup_util.bj_now", return_value=fixed):
                a = backup_util.make_backup(db, reason="t1")
                b = backup_util.make_backup(db, reason="t2")
            self.assertNotEqual(a.name, b.name, "同秒第二份必须另起名,不得覆盖第一份")
            for p in (a, b):
                self.assertTrue(p.exists())
                with connect(p) as c:   # 两份都必须是完整可开的库
                    c.execute("select count(*) from patients").fetchone()

    def test_claim_backup_name_refuses_overwrite(self):
        # P2 机制测试:认领正式名必须原子且绝不覆盖已存在的备份(竞态窗口的兜底)。
        with tempfile.TemporaryDirectory() as tmp:
            d = Path(tmp)
            final = d / "backup_20260717_120000.db"
            final.write_bytes(b"earlier-finished-backup")   # 竞争者已完成的备份
            t = d / "backup_20260717_120000.mine.part"
            t.write_bytes(b"my-backup")
            got = backup_util.claim_backup_name(t, final)
            self.assertEqual(final.read_bytes(), b"earlier-finished-backup", "已有备份不得被覆盖")
            self.assertEqual(got.name, "backup_20260717_120000_1.db")
            self.assertEqual(got.read_bytes(), b"my-backup")
            self.assertFalse(t.exists(), "认领后不留临时文件")

    def test_restore_tmp_is_tight_mode_during_copy(self):
        # 四轮 P1:复制"进行中"临时文件就必须是 0600——不得复制完才收紧,
        # 大库拷十几秒,窗口期整库患者数据对本机其他账号可读。
        import os
        import shutil as _shutil
        import stat as stat_mod
        from unittest import mock
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "clinic.sqlite3"; init_db(db)
            bk = backup_util.make_backup(db, reason="before")
            seen = {}
            real = _shutil.copyfileobj

            def spy(fin, fout, *a):
                seen["mode"] = stat_mod.S_IMODE(os.fstat(fout.fileno()).st_mode)
                return real(fin, fout, *a)

            with mock.patch("local_app.backup_util.shutil.copyfileobj", side_effect=spy):
                backup_util.restore_backup(db, bk.name, force=True)
            self.assertEqual(seen.get("mode"), 0o600, f"复制进行中必须 0600,实际 {seen}")

    def test_stale_restore_tmp_swept(self):
        # 四轮 P2:恢复被强杀/断电遗留的 *.restore_tmp(整库患者数据)必须有人收走——
        # 超过 1 小时的清掉,新鲜的(可能是并发恢复正在写)不动。
        import os
        import time
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "clinic.sqlite3"; init_db(db)
            stale = Path(str(db) + ".deadbeef.restore_tmp"); stale.write_bytes(b"x")
            bdir = db.parent / "backups"; bdir.mkdir(parents=True, exist_ok=True)
            # 六轮 P1:过期 .part 大文件残留可占满磁盘让备份永远失败,必须在入口"动手前"清
            stale_part = bdir / "backup_20260101_000000.dead.part"; stale_part.write_bytes(b"x")
            old_t = time.time() - 2 * 3600
            os.utime(stale, (old_t, old_t)); os.utime(stale_part, (old_t, old_t))
            fresh = Path(str(db) + ".cafebabe.restore_tmp"); fresh.write_bytes(b"y")
            backup_util.make_backup(db, reason="daily_auto")   # 每日备份入口动手前清扫
            self.assertFalse(stale.exists(), "过期恢复残留必须被收走")
            self.assertFalse(stale_part.exists(), "过期 .part 残留必须被收走")
            self.assertTrue(fresh.exists(), "新鲜临时文件(并发恢复在写)不得误删")

    def test_claim_backup_name_works_without_hardlink(self):
        # 五轮 P1:exFAT/FAT 等文件系统 os.link 抛 ENOTSUP——备份不得因此全线失败,
        # 且退化路径必须保住"绝不覆盖已完成备份"。
        import errno
        import os
        from unittest import mock
        with tempfile.TemporaryDirectory() as tmp:
            d = Path(tmp)
            final = d / "backup_20260717_120000.db"
            final.write_bytes(b"earlier-finished-backup")
            t = d / "backup_20260717_120000.mine.part"
            t.write_bytes(b"my-backup")
            with mock.patch("local_app.backup_util.os.link",
                            side_effect=OSError(errno.ENOTSUP, "not supported")):
                got = backup_util.claim_backup_name(t, final)
            self.assertEqual(final.read_bytes(), b"earlier-finished-backup", "已有备份不得被覆盖")
            self.assertEqual(got.name, "backup_20260717_120000_1.db")
            self.assertEqual(got.read_bytes(), b"my-backup")
            self.assertFalse(t.exists())

    def test_claim_fallback_replace_failure_removes_placeholder(self):
        # 终审 P2:无硬链接回退路径里,占位后 os.replace 失败必须收回 0 字节占位——
        # 0 字节文件挂正式名会被备份列表当"最新正常备份"。
        import errno
        import os
        from unittest import mock
        with tempfile.TemporaryDirectory() as tmp:
            d = Path(tmp)
            final = d / "backup_20260717_120000.db"
            t = d / "backup_20260717_120000.mine.part"
            t.write_bytes(b"my-backup")
            with mock.patch("local_app.backup_util.os.link",
                            side_effect=OSError(errno.ENOTSUP, "not supported")), \
                 mock.patch("local_app.backup_util.os.replace",
                            side_effect=OSError(errno.EIO, "I/O error")):
                with self.assertRaises(OSError):
                    backup_util.claim_backup_name(t, final)
            self.assertFalse(final.exists(), "换入失败必须收回 0 字节占位文件")

    def test_restore_survives_missing_chown(self):
        # 五轮 P1:Windows 无 os.chown——恢复不得因 AttributeError 中断。
        from unittest import mock
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "clinic.sqlite3"; init_db(db)
            bk = backup_util.make_backup(db, reason="before")
            import local_app.backup_util as bu
            real_getattr = getattr

            def fake_getattr(obj, name, default=None):
                if obj is bu.os and name == "chown":
                    return None   # 模拟 Windows:os 没有 chown
                return real_getattr(obj, name, default)

            with mock.patch("local_app.backup_util.getattr", create=True, side_effect=fake_getattr):
                res = backup_util.restore_backup(db, bk.name, force=True)
            self.assertEqual(res["restored"], bk.name)

    def test_restore_unknown_name_raises(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "clinic.sqlite3"; init_db(db)
            with self.assertRaises(FileNotFoundError):
                backup_util.restore_backup(db, "backup_19990101_000000.db")

    def test_service_running_detects_listening_port(self):
        # #569:探测端口是否被监听——用一个真实临时监听端口验证探测函数本身准确。
        import socket
        srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        srv.bind(("127.0.0.1", 0))
        srv.listen(1)
        port = srv.getsockname()[1]
        try:
            self.assertTrue(backup_util._service_running(port=port))
        finally:
            srv.close()
        self.assertFalse(backup_util._service_running(port=port))  # 关了就探测不到

    def test_restore_refuses_when_service_running_force_bypasses(self):
        # #569:回滚前必须程序性拒绝服务仍在监听的场景，不能只靠 docstring 提醒操作者手动停服务。
        import os
        import socket
        srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        srv.bind(("127.0.0.1", 0))
        srv.listen(1)
        port = srv.getsockname()[1]
        orig_port = os.environ.get("DENTAL_PORT")
        os.environ["DENTAL_PORT"] = str(port)
        try:
            with tempfile.TemporaryDirectory() as tmp:
                db = Path(tmp) / "clinic.sqlite3"; init_db(db)
                bk = backup_util.make_backup(db, reason="before")
                with self.assertRaises(RuntimeError):
                    backup_util.restore_backup(db, bk.name)   # 服务(端口)还在监听,拒绝
                # force=True 跳过探测,照常回滚成功
                res = backup_util.restore_backup(db, bk.name, force=True)
                self.assertTrue(res["restored"])
        finally:
            srv.close()
            if orig_port is None:
                os.environ.pop("DENTAL_PORT", None)
            else:
                os.environ["DENTAL_PORT"] = orig_port


if __name__ == "__main__":
    unittest.main()
