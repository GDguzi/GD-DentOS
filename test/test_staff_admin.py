"""统一员工管理：配台人员开通登录账号(users.staff_id 关联) + 员工/账号聚合列表。"""
import tempfile
from pathlib import Path
import unittest

from fastapi.testclient import TestClient

from local_app import auth
from local_app.api import create_app
from local_app.db import connect, init_db


def _setup(tmp):
    db = Path(tmp) / "clinic.sqlite3"
    init_db(db)
    auth.ensure_seed_roles(db)
    with connect(db) as conn:
        auth.create_user(conn, "admin", "管理员", "admin123", role="admin")
        auth.create_user(conn, "qt", "前台", "pw123456", role="reception")
        conn.commit()
    return db


def _login(c, u, p):
    assert c.post("/api/auth/login", json={"username": u, "password": p}).status_code == 200


def _add_staff(c, name="张医生", role="医生"):
    r = c.post("/api/staff-members", json={"name": name, "role": role})
    assert r.status_code == 200, r.text
    return r.json()["staff_id"]


class StaffAdminTest(unittest.TestCase):
    def test_open_account_links_and_logs_in(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = _setup(tmp)
            c = TestClient(create_app(db, require_login=True))
            _login(c, "admin", "admin123")
            sid = _add_staff(c)
            r = c.post(f"/api/staff-members/{sid}/account",
                       json={"username": "zhang", "password": "pw123456", "role": "doctor"})
            self.assertEqual(r.status_code, 200, r.text)
            self.assertEqual(r.json()["staff_id"], sid)
            # 关联落库
            with connect(db) as conn:
                row = conn.execute("select staff_id, role from users where username='zhang'").fetchone()
                self.assertEqual(row["staff_id"], sid)
                self.assertEqual(row["role"], "doctor")
            # 新账号能登录,权限=doctor(有 medical_record.edit)
            c2 = TestClient(create_app(db, require_login=True))
            _login(c2, "zhang", "pw123456")
            self.assertNotIn(c2.post("/api/medical-records", json={"patient_identity": "nope"}).status_code,
                             (401, 403))

    def test_open_account_duplicate_and_username_taken(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = _setup(tmp)
            c = TestClient(create_app(db, require_login=True))
            _login(c, "admin", "admin123")
            sid = _add_staff(c)
            c.post(f"/api/staff-members/{sid}/account",
                   json={"username": "zhang", "password": "pw123456", "role": "doctor"})
            # 同员工再开 → 409
            self.assertEqual(c.post(f"/api/staff-members/{sid}/account",
                             json={"username": "zhang2", "password": "pw123456"}).status_code, 409)
            # 用户名已占用 → 409
            sid2 = _add_staff(c, name="王护士", role="护士")
            self.assertEqual(c.post(f"/api/staff-members/{sid2}/account",
                             json={"username": "zhang", "password": "pw123456"}).status_code, 409)

    def test_open_account_validations(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = _setup(tmp)
            c = TestClient(create_app(db, require_login=True))
            _login(c, "admin", "admin123")
            sid = _add_staff(c)
            self.assertEqual(c.post(f"/api/staff-members/{sid}/account",
                             json={"username": "", "password": "pw123456"}).status_code, 400)
            self.assertEqual(c.post(f"/api/staff-members/{sid}/account",
                             json={"username": "x", "password": "   "}).status_code, 400)
            self.assertEqual(c.post("/api/staff-members/nope/account",
                             json={"username": "y", "password": "pw123456"}).status_code, 404)

    def test_open_account_requires_staff_manage(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = _setup(tmp)
            admin = TestClient(create_app(db, require_login=True))
            _login(admin, "admin", "admin123")
            sid = _add_staff(admin)
            c = TestClient(create_app(db, require_login=True))
            _login(c, "qt", "pw123456")            # 前台无 staff.manage
            self.assertEqual(c.post(f"/api/staff-members/{sid}/account",
                             json={"username": "y", "password": "pw123456"}).status_code, 403)

    def test_open_account_non_admin_cannot_create_admin(self):
        # #549: 非 admin(仅持 staff.manage)不能借开通员工账号建 admin 账号
        with tempfile.TemporaryDirectory() as tmp:
            db = _setup(tmp)
            with connect(db) as conn:
                auth.create_user(conn, "boss", "主任", "pw123456", role="director")
                conn.execute("insert or replace into role_permissions(role_key, perm_key) "
                             "values ('director','staff.manage')")
                conn.commit()
            admin = TestClient(create_app(db, require_login=True))
            _login(admin, "admin", "admin123")
            sid = _add_staff(admin)
            c = TestClient(create_app(db, require_login=True))
            _login(c, "boss", "pw123456")           # director 持 staff.manage 但非 admin
            r = c.post(f"/api/staff-members/{sid}/account",
                       json={"username": "evil", "password": "pw123456", "role": "admin"})
            self.assertEqual(r.status_code, 403, r.text)
            with connect(db) as conn:
                self.assertIsNone(conn.execute("select 1 from users where username='evil'").fetchone())
            # admin 本人仍可开 admin 账号(不误伤)
            r2 = admin.post(f"/api/staff-members/{sid}/account",
                            json={"username": "adm2", "password": "pw123456", "role": "admin"})
            self.assertEqual(r2.status_code, 200, r2.text)

    def test_multi_role_staff_matches_each_role_dropdown(self):
        # 多岗位：一个人 roles=[护士,助理] 应同时出现在 护士 和 助理 下拉
        with tempfile.TemporaryDirectory() as tmp:
            db = _setup(tmp)
            c = TestClient(create_app(db, require_login=True))
            _login(c, "admin", "admin123")
            r = c.post("/api/staff-members",
                       json={"name": "双岗", "role": "护士", "roles": ["护士", "助理"]})
            self.assertEqual(r.status_code, 200, r.text)
            self.assertEqual(set(r.json()["roles"]), {"护士", "助理"})
            c.post("/api/staff-members", json={"name": "纯医生", "role": "医生"})  # 单岗位
            nurses = {m["name"] for m in c.get("/api/staff-members?role=护士").json()["members"]}
            assist = {m["name"] for m in c.get("/api/staff-members?role=助理").json()["members"]}
            docs = {m["name"] for m in c.get("/api/staff-members?role=医生").json()["members"]}
            self.assertIn("双岗", nurses)
            self.assertIn("双岗", assist)      # 多岗位:助理下拉也能选到
            self.assertNotIn("双岗", docs)
            self.assertIn("纯医生", docs)
            self.assertNotIn("纯医生", assist)  # 单岗位:不串到助理

    def test_selection_endpoint_hides_sensitive_fields(self):
        # #291：选人下拉接口 /api/staff-members 不得返回身份证/手机/证号等隐私
        with tempfile.TemporaryDirectory() as tmp:
            db = _setup(tmp)
            admin = TestClient(create_app(db, require_login=True))
            _login(admin, "admin", "admin123")
            admin.post("/api/staff-members", json={"name": "李医生", "role": "医生",
                       "job_no": "x1", "phone": "13800000000", "id_card": "44011920000101",
                       "license_no": "LIC9", "title": "主治"})
            # 前台(无 staff.manage)调选人接口 → 只拿到 name/role/roles，无隐私字段
            c = TestClient(create_app(db, require_login=True))
            _login(c, "qt", "pw123456")
            m = [x for x in c.get("/api/staff-members?role=医生").json()["members"] if x["name"] == "李医生"][0]
            self.assertEqual(set(m.keys()), {"staff_id", "name", "role", "roles"})
            for leak in ("id_card", "phone", "license_no", "title"):
                self.assertNotIn(leak, m, f"选人接口泄露了 {leak}")
            # 受控 staff-admin/list(staff.manage) 才有档案明细
            full = [x for x in admin.get("/api/staff-admin/list").json()["members"] if x["name"] == "李医生"][0]
            self.assertEqual(full["id_card"], "44011920000101")

    def test_delete_staff_disables_linked_account(self):
        # 取消员工 → 级联停用其登录账号(即时收回登录)
        with tempfile.TemporaryDirectory() as tmp:
            db = _setup(tmp)
            c = TestClient(create_app(db, require_login=True))
            _login(c, "admin", "admin123")
            sid = _add_staff(c)
            c.post(f"/api/staff-members/{sid}/account",
                   json={"username": "zhang", "password": "pw123456", "role": "doctor"})
            # 删员工
            r = c.delete(f"/api/staff-members/{sid}")
            self.assertEqual(r.status_code, 200, r.text)
            self.assertTrue(r.json()["account_disabled"])
            # 该账号已停用：无法再登录
            c2 = TestClient(create_app(db, require_login=True))
            self.assertEqual(c2.post("/api/auth/login",
                             json={"username": "zhang", "password": "pw123456"}).status_code, 401)
            with connect(db) as conn:
                self.assertEqual(
                    conn.execute("select is_active from users where username='zhang'").fetchone()["is_active"], 0)

    def test_admin_list_shows_account_summary(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = _setup(tmp)
            c = TestClient(create_app(db, require_login=True))
            _login(c, "admin", "admin123")
            sid = _add_staff(c)
            _add_staff(c, name="李助理", role="助理")   # 未开通账号
            c.post(f"/api/staff-members/{sid}/account",
                   json={"username": "zhang", "password": "pw123456", "role": "doctor"})
            body = c.get("/api/staff-admin/list").json()
            by_id = {m["staff_id"]: m for m in body["members"]}
            self.assertEqual(by_id[sid]["account"]["username"], "zhang")
            self.assertEqual(by_id[sid]["account"]["account_role"], "doctor")
            # 未开通的 account=None
            other = [m for m in body["members"] if m["name"] == "李助理"][0]
            self.assertIsNone(other["account"])
            # 前台无权看
            c2 = TestClient(create_app(db, require_login=True))
            _login(c2, "qt", "pw123456")
            self.assertEqual(c2.get("/api/staff-admin/list").status_code, 403)


class StaffAccountPasswordRuleTest(unittest.TestCase):
    """2D.3：开员工账号只剩一条密码规则——原文至少含一个非空白字符,且不裁剪。"""

    def test_single_char_password_opens_and_logs_in(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = _setup(tmp)
            c = TestClient(create_app(db, require_login=True))
            _login(c, "admin", "admin123")
            sid = _add_staff(c)
            r = c.post(f"/api/staff-members/{sid}/account",
                       json={"username": "zhang", "password": "短", "role": "doctor"})
            self.assertEqual(r.status_code, 200, r.text)
            c2 = TestClient(create_app(db, require_login=True))
            _login(c2, "zhang", "短")

    def test_blank_password_rejected_and_no_account(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = _setup(tmp)
            c = TestClient(create_app(db, require_login=True))
            _login(c, "admin", "admin123")
            sid = _add_staff(c)
            for pw in ["", " ", "   ", "\t", "\n", " \t\n "]:
                r = c.post(f"/api/staff-members/{sid}/account",
                           json={"username": "zhang", "password": pw, "role": "doctor"})
                self.assertEqual(r.status_code, 400, f"{pw!r} -> {r.text}")
            with connect(db) as conn:
                self.assertEqual(conn.execute("select count(*) from users where username='zhang'").fetchone()[0], 0)

    def test_non_str_password_rejected_and_no_account(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = _setup(tmp)
            c = TestClient(create_app(db, require_login=True))
            _login(c, "admin", "admin123")
            sid = _add_staff(c)
            for pw in [1, 0, True, False, ["x"], {"k": "x"}]:
                r = c.post(f"/api/staff-members/{sid}/account",
                           json={"username": "zhang", "password": pw, "role": "doctor"})
                self.assertEqual(r.status_code, 400, f"{pw!r} -> {r.text}")
                self.assertEqual(r.json()["detail"], "密码不能为空")
            with connect(db) as conn:
                self.assertEqual(conn.execute("select count(*) from users where username='zhang'").fetchone()[0], 0)

    def test_surrogate_password_rejected_and_no_account(self):
        # 孤立代理项(原始 JSON \ud800)不可 UTF-8 编码 → 400,不得 500,不得开号
        with tempfile.TemporaryDirectory() as tmp:
            db = _setup(tmp)
            c = TestClient(create_app(db, require_login=True))
            _login(c, "admin", "admin123")
            sid = _add_staff(c)
            for esc in ['\\ud800', 'a\\udfffb', ' \\ud800 ']:
                r = c.post(f"/api/staff-members/{sid}/account",
                           content=('{"username": "zhang", "password": "%s", "role": "doctor"}' % esc).encode("utf-8"),
                           headers={"content-type": "application/json"})
                self.assertEqual(r.status_code, 400, f"{esc} -> {r.status_code} {r.text}")
                self.assertEqual(r.json()["detail"], "密码不能为空")
            with connect(db) as conn:
                self.assertEqual(conn.execute("select count(*) from users where username='zhang'").fetchone()[0], 0)

    def test_password_not_trimmed(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = _setup(tmp)
            c = TestClient(create_app(db, require_login=True))
            _login(c, "admin", "admin123")
            sid = _add_staff(c)
            r = c.post(f"/api/staff-members/{sid}/account",
                       json={"username": "zhang", "password": " x ", "role": "doctor"})
            self.assertEqual(r.status_code, 200, r.text)
            c2 = TestClient(create_app(db, require_login=True))
            self.assertEqual(c2.post("/api/auth/login", json={"username": "zhang", "password": "x"}).status_code, 401)
            self.assertEqual(c2.post("/api/auth/login", json={"username": "zhang", "password": " x "}).status_code, 200)


if __name__ == "__main__":
    unittest.main()
