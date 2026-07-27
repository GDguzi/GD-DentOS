"""备份轮转(用户要求定期删过去的):按年龄删旧备份,但永远留最近 N 份 .db 防全删光。
合成空文件 + os.utime 造旧 mtime,不碰真实库/真实备份。"""
import os
import shutil
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from local_app import backup_util


def _mk(d, name, age_days):
    p = d / name
    p.write_bytes(b"x")
    t = time.time() - age_days * 86400
    os.utime(p, (t, t))
    return p


class BackupRetentionTest(unittest.TestCase):
    def _setup(self, tmp):
        db = Path(tmp) / "clinic.sqlite3"
        (Path(tmp) / "backups").mkdir(parents=True, exist_ok=True)
        return db, Path(tmp) / "backups"

    def test_old_pruned_recent_kept(self):
        with tempfile.TemporaryDirectory() as tmp:
            db, bk = self._setup(tmp)
            old = _mk(bk, "backup_20260601_120000.db", 20)
            recent = _mk(bk, "backup_20260628_120000.db", 1)
            removed = backup_util.prune_backups(db, keep_days=7, min_keep=0)
            self.assertEqual(removed, 1)
            self.assertFalse(old.exists())
            self.assertTrue(recent.exists())

    def test_min_keep_floor_protects_newest(self):
        # 全是 30 天旧的 5 份,min_keep=3 → 最新 3 份(按名)受保护,最旧 2 份删
        with tempfile.TemporaryDirectory() as tmp:
            db, bk = self._setup(tmp)
            names = [f"backup_2026060{i}_120000.db" for i in range(1, 6)]
            for n in names:
                _mk(bk, n, 30)
            removed = backup_util.prune_backups(db, keep_days=7, min_keep=3)
            self.assertEqual(removed, 2)
            survivors = sorted(f.name for f in bk.glob("backup_*.db"))
            self.assertEqual(survivors, names[2:])   # 603/604/605 留

    def test_old_zip_pruned(self):
        with tempfile.TemporaryDirectory() as tmp:
            db, bk = self._setup(tmp)
            z = _mk(bk, "backup_20260601_120000.zip", 20)
            backup_util.prune_backups(db, keep_days=7, min_keep=0)
            self.assertFalse(z.exists(), "含影像 zip 备份也按年龄轮转")

    def test_non_backup_files_untouched(self):
        with tempfile.TemporaryDirectory() as tmp:
            db, bk = self._setup(tmp)
            other = _mk(bk, "notes.txt", 99)
            _mk(bk, "backup_20260601_120000.db", 99)
            backup_util.prune_backups(db, keep_days=7, min_keep=0)
            self.assertTrue(other.exists(), "非备份命名的文件不动")

    def test_backup_route_name_re_accepts_suffix(self):
        # 同步前备份同秒会生成 _1.db,备份接口的 _NAME_RE 必须认它,否则列不出/下不了
        from local_app.routes.backup import _NAME_RE
        self.assertTrue(_NAME_RE.match("backup_20260629_120000.db"))
        self.assertTrue(_NAME_RE.match("backup_20260629_120000_1.db"), "同秒 _1 后缀也要认")
        self.assertTrue(_NAME_RE.match("backup_20260629_120000.zip"))
        self.assertFalse(_NAME_RE.match("../etc/passwd"))
        self.assertFalse(_NAME_RE.match("backup_x.db"))

    def test_suffix_named_backup_matched(self):
        # 同秒重复备份的 _1 后缀也要能被轮转识别
        with tempfile.TemporaryDirectory() as tmp:
            db, bk = self._setup(tmp)
            p = _mk(bk, "backup_20260601_120000_1.db", 20)
            backup_util.prune_backups(db, keep_days=7, min_keep=0)
            self.assertFalse(p.exists())


class ManualSnapshotPruneTest(unittest.TestCase):
    """手工快照(改动前"先存一份")清理:只留最近 keep 份,更旧的拷 NAS 再删本地。
    -对抗复审确认的三个必修 + 一个建议,均已补探针锁住："""

    def _setup(self, tmp):
        db = Path(tmp) / "clinic.sqlite3"
        db.write_bytes(b"live")
        (Path(tmp) / "backups").mkdir(parents=True, exist_ok=True)
        nas = Path(tmp) / "nas_root"
        nas.mkdir(parents=True, exist_ok=True)
        return db, Path(tmp) / "backups", nas / "backup"

    def _mounted(self):
        # 单测沙箱里的临时目录不是真挂载点,os.path.ismount 天然为 False;
        # "已挂载" 场景必须显式打桩模拟,不能靠目录存在冒充(的病根)。
        return mock.patch("local_app.backup_util.os.path.ismount", return_value=True)

    def test_keeps_newest_moves_rest_to_nas(self):
        with tempfile.TemporaryDirectory() as tmp:
            db, bk, nas_dir = self._setup(tmp)
            files = []
            for i in range(10):
                f = _mk(db.parent, f"clinic.backup_before_x{i}.sqlite3", age_days=10 - i)
                files.append(f)
            with self._mounted():
                result = backup_util.prune_manual_snapshots(db, nas_dir, keep=7)
            self.assertEqual(result, {"moved": 3, "skipped_no_nas": 0, "skipped_conflict": 0, "failed": 0})
            survivors = sorted(f.name for f in db.parent.glob("clinic.backup_before_*.sqlite3"))
            self.assertEqual(len(survivors), 7)
            nas_files = sorted(f.name for f in (nas_dir / "手工快照").glob("*.sqlite3"))
            self.assertEqual(len(nas_files), 3)
            for f in files[:3]:                  # 最旧的3份该搬走了
                self.assertFalse(f.exists())
                self.assertIn(f.name, nas_files)

    def test_live_db_never_touched(self):
        with tempfile.TemporaryDirectory() as tmp:
            db, bk, nas_dir = self._setup(tmp)
            _mk(db.parent, "clinic.backup_before_old.sqlite3", age_days=99)
            with self._mounted():
                backup_util.prune_manual_snapshots(db, nas_dir, keep=0)
            self.assertTrue(db.exists(), "生产库本体绝不能被当成快照清掉")

    def test_backups_dir_stray_snapshot_included(self):
        with tempfile.TemporaryDirectory() as tmp:
            db, bk, nas_dir = self._setup(tmp)
            old = _mk(bk, "clinic_before_repair_old.sqlite3", age_days=30)
            _mk(bk, "clinic_before_repair_new.sqlite3", age_days=1)
            with self._mounted():
                backup_util.prune_manual_snapshots(db, nas_dir, keep=1)
            self.assertFalse(old.exists())
            self.assertTrue((bk / "clinic_before_repair_new.sqlite3").exists())

    def test_no_nas_mounted_skips_deletion(self):
        # 目录"存在"不等于"已挂载"——不打桩,tmp 目录天然不是真挂载点。
        with tempfile.TemporaryDirectory() as tmp:
            db, bk, nas_dir = self._setup(tmp)
            f = _mk(db.parent, "clinic.backup_before_old.sqlite3", age_days=99)
            result = backup_util.prune_manual_snapshots(db, nas_dir, keep=0)
            self.assertEqual(result, {"moved": 0, "skipped_no_nas": 1, "skipped_conflict": 0, "failed": 0})
            self.assertTrue(f.exists(), "NAS 没真挂载就不能删本地唯一副本")

    def test_nas_parent_missing_skips_deletion(self):
        with tempfile.TemporaryDirectory() as tmp:
            db, bk, _ = self._setup(tmp)
            f = _mk(db.parent, "clinic.backup_before_old.sqlite3", age_days=99)
            unmounted_nas = Path(tmp) / "not_mounted_volume" / "backup"
            result = backup_util.prune_manual_snapshots(db, unmounted_nas, keep=0)
            self.assertEqual(result["skipped_no_nas"], 1)
            self.assertTrue(f.exists())

    def test_nothing_to_prune(self):
        with tempfile.TemporaryDirectory() as tmp:
            db, bk, nas_dir = self._setup(tmp)
            _mk(db.parent, "clinic.backup_before_a.sqlite3", age_days=1)
            with self._mounted():
                result = backup_util.prune_manual_snapshots(db, nas_dir, keep=7)
            self.assertEqual(result, {"moved": 0, "skipped_no_nas": 0, "skipped_conflict": 0, "failed": 0})

    def test_symlink_candidate_excluded(self):
        # 候选是软链接——绝不跟随复制链接指向的任意内容,也不当成待清理项处理。
        with tempfile.TemporaryDirectory() as tmp:
            db, bk, nas_dir = self._setup(tmp)
            target = Path(tmp) / "secret_elsewhere.bin"
            target.write_bytes(b"not a snapshot")
            link = db.parent / "clinic.backup_before_link.sqlite3"
            link.symlink_to(target)
            os.utime(link, (time.time() - 99 * 86400,) * 2, follow_symlinks=False)
            with self._mounted():
                result = backup_util.prune_manual_snapshots(db, nas_dir, keep=0)
            self.assertEqual(result, {"moved": 0, "skipped_no_nas": 0, "skipped_conflict": 0, "failed": 0})
            self.assertTrue(link.exists(), "软链接候选不该被当成快照删掉")
            self.assertFalse((nas_dir / "手工快照" / target.name).exists())

    def test_nas_side_symlink_conflict_not_overwritten(self):
        # NAS 侧目标已被占成软链接(如上次异常中断留下)——不写穿,不误删本地。
        with tempfile.TemporaryDirectory() as tmp:
            db, bk, nas_dir = self._setup(tmp)
            f = _mk(db.parent, "clinic.backup_before_old.sqlite3", age_days=99)
            dest_dir = nas_dir / "手工快照"
            dest_dir.mkdir(parents=True)
            other = Path(tmp) / "unrelated_nas_file.bin"
            other.write_bytes(b"do not touch")
            (dest_dir / f.name).symlink_to(other)
            with self._mounted():
                result = backup_util.prune_manual_snapshots(db, nas_dir, keep=0)
            self.assertEqual(result["skipped_conflict"], 1)
            self.assertTrue(f.exists(), "撞上 NAS 侧软链接时本地原件不能删")
            self.assertEqual(other.read_bytes(), b"do not touch", "不能写穿软链接覆盖无关文件")

    def test_nas_same_name_different_content_kept_as_conflict(self):
        # data/ 和 data/backups/ 各有一份同 basename、不同内容的快照,不能互相覆盖丢数据。
        with tempfile.TemporaryDirectory() as tmp:
            db, bk, nas_dir = self._setup(tmp)
            f1 = _mk(db.parent, "clinic_before_dup.sqlite3", age_days=10)
            f1.write_bytes(b"content-A")
            os.utime(f1, (time.time() - 10 * 86400,) * 2)
            f2 = _mk(bk, "clinic_before_dup.sqlite3", age_days=9)
            f2.write_bytes(b"content-B-longer")
            os.utime(f2, (time.time() - 9 * 86400,) * 2)
            with self._mounted():
                result = backup_util.prune_manual_snapshots(db, nas_dir, keep=0)
            self.assertEqual(result["moved"], 1)
            self.assertEqual(result["skipped_conflict"], 1)
            # 两份不能都消失——冲突的那份必须留在本地
            self.assertEqual(f1.exists() + f2.exists(), 1)

    def test_nas_same_name_same_size_different_content_kept(self):
        # 同名同大小但内容不同,st_size 骗过旧守卫会误删本地;必须逐字节比对判冲突
        with tempfile.TemporaryDirectory() as tmp:
            db, bk, nas_dir = self._setup(tmp)
            local = _mk(db.parent, "clinic_before_twin.sqlite3", age_days=10)
            local.write_bytes(b"AAAA")
            os.utime(local, (time.time() - 10 * 86400,) * 2)
            dest_dir = nas_dir / "手工快照"
            dest_dir.mkdir(parents=True, exist_ok=True)
            (dest_dir / local.name).write_bytes(b"BBBB")   # 同名同大小(4字节)异内容
            with self._mounted():
                result = backup_util.prune_manual_snapshots(db, nas_dir, keep=0)
            self.assertEqual(result["skipped_conflict"], 1)
            self.assertEqual(result["moved"], 0)
            self.assertTrue(local.exists(), "同大小异内容不得删本地原件")
            self.assertEqual((dest_dir / local.name).read_bytes(), b"BBBB", "NAS 侧不得被覆盖")

    def test_nas_same_name_identical_content_moved(self):
        # 对照:同名且逐字节相同,才视为已归档,允许删本地
        with tempfile.TemporaryDirectory() as tmp:
            db, bk, nas_dir = self._setup(tmp)
            local = _mk(db.parent, "clinic_before_same.sqlite3", age_days=10)
            local.write_bytes(b"IDENTICAL")
            os.utime(local, (time.time() - 10 * 86400,) * 2)
            dest_dir = nas_dir / "手工快照"
            dest_dir.mkdir(parents=True, exist_ok=True)
            (dest_dir / local.name).write_bytes(b"IDENTICAL")
            with self._mounted():
                result = backup_util.prune_manual_snapshots(db, nas_dir, keep=0)
            self.assertEqual(result["moved"], 1)
            self.assertFalse(local.exists())

    def test_nas_same_name_directory_kept_as_conflict(self):
        # 同名目标是目录(st_size 可能碰巧和文件相等),绝不能删本地
        with tempfile.TemporaryDirectory() as tmp:
            db, bk, nas_dir = self._setup(tmp)
            dest_dir = nas_dir / "手工快照"
            dest_dir.mkdir(parents=True, exist_ok=True)
            trap = dest_dir / "clinic_before_trap.sqlite3"
            trap.mkdir()                                    # NAS 侧同名目录
            local = _mk(db.parent, "clinic_before_trap.sqlite3", age_days=10)
            local.write_bytes(b"x" * trap.stat().st_size)   # 刻意凑成同 st_size 触发旧 bug
            os.utime(local, (time.time() - 10 * 86400,) * 2)
            with self._mounted():
                result = backup_util.prune_manual_snapshots(db, nas_dir, keep=0)
            self.assertEqual(result["skipped_conflict"], 1)
            self.assertTrue(local.exists(), "同名目录必须判冲突保留本地")

    def test_copy_failure_keeps_local_and_does_not_abort_batch(self):
        # 单份拷贝失败不牵连其它候选,也不留半成品 .part。
        with tempfile.TemporaryDirectory() as tmp:
            db, bk, nas_dir = self._setup(tmp)
            bad = _mk(db.parent, "clinic.backup_before_bad.sqlite3", age_days=10)
            good = _mk(db.parent, "clinic.backup_before_good.sqlite3", age_days=9)
            real_copy2 = shutil.copy2

            def flaky_copy2(src, dst, *a, **kw):
                if Path(src).name == bad.name:
                    Path(dst).write_bytes(b"short")   # 模拟半写
                    raise OSError("simulated I/O failure")
                return real_copy2(src, dst, *a, **kw)

            with self._mounted(), mock.patch("local_app.backup_util.shutil.copy2", side_effect=flaky_copy2):
                result = backup_util.prune_manual_snapshots(db, nas_dir, keep=0)
            self.assertEqual(result["moved"], 1)
            self.assertEqual(result["failed"], 1)
            self.assertTrue(bad.exists(), "拷贝失败的那份本地原件必须保留")
            self.assertFalse(good.exists())
            self.assertFalse((nas_dir / "手工快照" / (bad.name + ".part")).exists(), "半成品不能残留")


if __name__ == "__main__":
    unittest.main()
