import io, json, sqlite3, tempfile, unittest, zipfile
from pathlib import Path
from fastapi.testclient import TestClient
from local_app import auth
from local_app.api import create_app
from local_app.db import connect, init_db
from local_app.patient_import import parse_table
from local_app.restore_pending import FULL_BACKUP_MESSAGE, apply_pending_restore, pending_restore


def _app(root, name):
    db = Path(root) / name / "clinic.sqlite3"
    db.parent.mkdir(parents=True)
    init_db(db)
    images = db.parent / "images"
    images.mkdir()
    with connect(db) as conn:
        auth.create_user(conn, "admin", "管理员", "admin123", role="admin")
        conn.commit()
    client = TestClient(create_app(db, images_dir=images))
    client.post("/api/auth/login", json={"username": "admin", "password": "admin123"})
    return db, images, client


def _seed_record(db, pid):
    with connect(db) as conn:
        conn.execute("insert into patients(patient_identity, display_name, updated_at, current_hash) values (?,?,?,?)",
                     (pid, "合成患者", "2026-01-01 00:00:00", "h"))
        cols = {r[1]: r for r in conn.execute("pragma table_info(medical_records)")}
        need = [c for c, r in cols.items() if r[3] and r[4] is None and not r[5]]
        vals = {c: f"{pid}-{c}" for c in need}
        vals["patient_identity"] = pid
        conn.execute(f"insert into medical_records({','.join(vals)}) values ({','.join('?' * len(vals))})", tuple(vals.values()))
        conn.commit()


def _count(db, table):
    c = sqlite3.connect(db)
    try:
        return c.execute(f"select count(*) from {table}").fetchone()[0]
    finally:
        c.close()


class RestoreTest(unittest.TestCase):
    def _backup(self, client, db, images=False):
        name = client.post("/api/backup", json={"include_images": images}).json()["name"]
        return name, (db.parent / "backups" / name).read_bytes()

    def test_old_backup_restores_records_after_restart_with_safety_copy(self):
        with tempfile.TemporaryDirectory() as tmp:
            old_db, _, old = _app(tmp, "old")
            _seed_record(old_db, "old-p")
            name, data = self._backup(old, old_db)
            new_db, new_images, new = _app(tmp, "new")
            _seed_record(new_db, "new-p")
            r = new.post("/api/backup/restore", files={"file": (name, data)})
            self.assertEqual(r.status_code, 200, r.text)
            self.assertEqual(r.json()["pending"]["counts"]["medical_records"], 1)
            c = sqlite3.connect(new_db)
            self.assertEqual(c.execute("select patient_identity from patients").fetchall(), [("new-p",)])
            c.close()
            done = apply_pending_restore(new_db, new_images)
            c = sqlite3.connect(new_db)
            self.assertEqual(c.execute("select patient_identity from patients").fetchall(), [("old-p",)])
            self.assertEqual(c.execute("select count(*) from medical_records").fetchone()[0], 1)
            c.close()
            safety = sqlite3.connect(new_db.parent / "backups" / done["safety"])
            self.assertEqual(safety.execute("select patient_identity from patients").fetchall(), [("new-p",)])
            safety.close()
            self.assertIsNone(pending_restore(new_db))
            self.assertIsNone(apply_pending_restore(new_db, new_images))

    def test_zip_restores_images(self):
        with tempfile.TemporaryDirectory() as tmp:
            old_db, old_images, old = _app(tmp, "old")
            (old_images / "sub").mkdir()
            (old_images / "sub" / "a.jpg").write_bytes(b"img")
            name, data = self._backup(old, old_db, images=True)
            new_db, new_images, new = _app(tmp, "new")
            r = new.post("/api/backup/restore", files={"file": (name, data)})
            self.assertEqual(r.json()["pending"]["image_count"], 1)
            apply_pending_restore(new_db, new_images)
            self.assertEqual((new_images / "sub" / "a.jpg").read_bytes(), b"img")

    def test_rejects_bad_files_and_leaves_nothing_pending(self):
        with tempfile.TemporaryDirectory() as tmp:
            db, _, client = _app(tmp, "a")
            other = Path(tmp) / "other.db"
            c = sqlite3.connect(other); c.execute("create table t(a)"); c.commit(); c.close()
            buf = io.BytesIO()
            with zipfile.ZipFile(buf, "w") as z:
                z.writestr("backup_20260101_000000.db", other.read_bytes())
                z.writestr("../evil.txt", "x")
            for fn, body in (("a.csv", b"name,phone\n"), ("o.db", other.read_bytes()), ("e.zip", buf.getvalue()),
                             ("t.db", b"SQLite format 3\x00garbage")):
                r = client.post("/api/backup/restore", files={"file": (fn, body)})
                self.assertEqual(r.status_code, 400, fn)
            self.assertIsNone(pending_restore(db))
            self.assertFalse(list(db.parent.glob("restore_incoming.*")))
            self.assertFalse((Path(tmp) / "evil.txt").exists())

    def test_cancel(self):
        with tempfile.TemporaryDirectory() as tmp:
            db, images, client = _app(tmp, "a")
            _seed_record(db, "keep")
            name, data = self._backup(client, db)
            client.post("/api/backup/restore", files={"file": (name, data)})
            self.assertIsNotNone(client.get("/api/backup/restore").json()["pending"])
            self.assertTrue(client.post("/api/backup/restore/cancel").json()["cancelled"])
            self.assertIsNone(apply_pending_restore(db, images))

    def test_interrupted_apply_finishes_on_next_start(self):
        with tempfile.TemporaryDirectory() as tmp:
            db, images, client = _app(tmp, "a")
            name, data = self._backup(client, db)
            client.post("/api/backup/restore", files={"file": (name, data)})
            staged = db.parent / "restore_pending" / "clinic.sqlite3"
            staged.unlink()   # 模拟库已换入、暂存未清时断电
            before = _count(db, "patients")
            apply_pending_restore(db, images)
            self.assertEqual(_count(db, "patients"), before)
            self.assertIsNone(pending_restore(db))

    def test_non_admin_forbidden(self):
        with tempfile.TemporaryDirectory() as tmp:
            db, _, client = _app(tmp, "a")
            client.post("/api/auth/logout")
            self.assertIn(client.post("/api/backup/restore", files={"file": ("a.db", b"x")}).status_code, (401, 403))

    def test_patient_import_refuses_full_backup(self):
        with tempfile.TemporaryDirectory() as tmp:
            db, _, client = _app(tmp, "a")
            _name, data = self._backup(client, db)
            with self.assertRaises(ValueError) as cm:
                parse_table("backup.db", data)
            self.assertEqual(str(cm.exception), FULL_BACKUP_MESSAGE)
            zname, zdata = self._backup(client, db, images=True)
            with self.assertRaises(ValueError) as cm:
                parse_table(zname, zdata)
            self.assertEqual(str(cm.exception), FULL_BACKUP_MESSAGE)


if __name__ == "__main__":
    unittest.main()
