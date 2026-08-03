"""#420:按权限树蓝本补齐角色/岗位 + 升级幂等补缺 + 新角色可建账号。
#502:用户拍板去「技师」——预置 10 类,旧库 technician 由 migrate_technician_role 一次性迁移。
合成库,只断言结构/计数,不碰真实库。"""
import tempfile
import unittest
from pathlib import Path

from fastapi.testclient import TestClient

from local_app import auth
from local_app.api import create_app
from local_app.db import connect, init_db
from local_app.routes import staff as staff_routes


# 升级前的旧 6 类(本次扩充前的基线),用于模拟"已 seed 过的旧库"
_OLD_ROLES = [("admin", "管理员"), ("doctor", "医生"), ("nurse", "护士"),
              ("reception", "前台"), ("consultant", "咨询师"), ("assistant", "助理")]
_NEW_KEYS = {"director", "cashier", "assistant2", "support"}


class BlueprintRolesTest(unittest.TestCase):
    def test_preset_has_10_blueprint_roles(self):
        keys = {k for k, _, _ in auth.PRESET_ROLES}
        self.assertEqual(len(auth.PRESET_ROLES), 10)
        self.assertTrue(_NEW_KEYS <= keys, "应含主任/收银员/客服/助理2 独立角色")
        self.assertNotIn("technician", keys, "#502:技师角色已去除")
        # 每个新角色都要有默认权限定义(否则建出来是空权限角色)
        for k in _NEW_KEYS:
            self.assertIn(k, auth.DEFAULT_ROLE_PERMS, f"{k} 缺默认权限")

    def test_staff_positions_has_10(self):
        self.assertIn("客服", staff_routes.ROLES)
        self.assertIn("助理2", staff_routes.ROLES)
        self.assertNotIn("技师", staff_routes.ROLES)
        self.assertEqual(len(staff_routes.ROLES), 10)

    def test_assistant_reception_absorb_lab_perms(self):
        """#502:技工功能并入——助理/助理2 带 lab_order.manage+warehouse.view,前台带 lab_order.manage。"""
        self.assertIn("lab_order.manage", auth.DEFAULT_ROLE_PERMS["assistant"])
        self.assertIn("warehouse.view", auth.DEFAULT_ROLE_PERMS["assistant"])
        self.assertIn("lab_order.manage", auth.DEFAULT_ROLE_PERMS["assistant2"])
        self.assertIn("lab_order.manage", auth.DEFAULT_ROLE_PERMS["reception"])

    def test_assistant_patient_create_edit(self):
        """#502尾巴(用户拍板 2026-07-02):助理/助理2 带患者建档/编辑。"""
        for role in ("assistant", "assistant2"):
            self.assertIn("patient.create", auth.DEFAULT_ROLE_PERMS[role])
            self.assertIn("patient.edit", auth.DEFAULT_ROLE_PERMS[role])

    def test_report_view_admin_only(self):
        """#512(用户拍板 2026-07-02):业绩/门诊报表只有院长(admin)可查——任何预置角色默认不带 report.view。"""
        for role, perms in auth.DEFAULT_ROLE_PERMS.items():
            self.assertNotIn("report.view", perms, f"{role} 不应默认带报表权限")


class UpgradeMigrationTest(unittest.TestCase):
    def _seed_old(self, conn):
        now = auth.now_str()
        for key, name in _OLD_ROLES:
            conn.execute("insert into roles(role_key, name, is_system, sort, created_at, updated_at) "
                         "values (?, ?, 1, 0, ?, ?)", (key, name, now, now))
        conn.commit()

    def test_adds_missing_roles_idempotently(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "clinic.sqlite3"
            init_db(db)
            with connect(db) as conn:
                self._seed_old(conn)
            added = auth.ensure_preset_roles_present(db)
            self.assertEqual(added, 4, "应补齐 4 个新角色")
            with connect(db) as conn:
                keys = {r[0] for r in conn.execute("select role_key from roles")}
                self.assertTrue(_NEW_KEYS <= keys)
                # 新角色带上了默认权限
                cashier_perms = {r[0] for r in conn.execute(
                    "select perm_key from role_permissions where role_key='cashier'")}
                self.assertIn("billing.pay", cashier_perms)
            # 幂等:再跑不重复加
            self.assertEqual(auth.ensure_preset_roles_present(db), 0)

    def test_empty_table_defers_to_seed(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "clinic.sqlite3"
            init_db(db)
            self.assertEqual(auth.ensure_preset_roles_present(db), 0, "空表交给 ensure_seed_roles")


class TechnicianMigrationTest(unittest.TestCase):
    """#502:模拟带 technician 角色的旧库,验证一次性迁移。"""

    def _seed_old_with_technician(self, db):
        now = auth.now_str()
        with connect(db) as conn:
            self._roles = _OLD_ROLES + [("technician", "技师")]
            for key, name in self._roles:
                conn.execute("insert into roles(role_key, name, is_system, sort, created_at, updated_at) "
                             "values (?, ?, 1, 0, ?, ?)", (key, name, now, now))
            # 旧 technician 角色的权限勾选
            for pk in ("patient.view", "lab_order.manage"):
                conn.execute("insert into role_permissions(role_key, perm_key) values ('technician', ?)", (pk,))
            # 技师账号 + 技师员工(单岗位 & 多岗位含重叠)
            auth.create_user(conn, "tech01", "老技师", "pw123456", role="technician")
            conn.execute("insert into staff_members(staff_id, name, role, roles) values "
                         "('s1', '技师甲', '技师', ',技师,')")
            conn.execute("insert into staff_members(staff_id, name, role, roles) values "
                         "('s2', '多岗乙', '护士', ',护士,技师,助理,')")
            conn.commit()

    def test_migrates_accounts_staff_and_perms(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "clinic.sqlite3"
            init_db(db)
            self._seed_old_with_technician(db)
            self.assertTrue(auth.migrate_technician_role(db))
            with connect(db) as conn:
                # 角色行已删,账号迁到 assistant
                self.assertIsNone(conn.execute(
                    "select 1 from roles where role_key='technician'").fetchone())
                self.assertEqual(conn.execute(
                    "select count(*) from role_permissions where role_key='technician'").fetchone()[0], 0)
                self.assertEqual(conn.execute(
                    "select role from users where username='tech01'").fetchone()[0], "assistant")
                # 员工岗位迁到助理;多岗位替换后去重(护士,助理,不出现两个助理)
                self.assertEqual(tuple(conn.execute(
                    "select role, roles from staff_members where staff_id='s1'").fetchone()),
                    ("助理", ",助理,"))
                self.assertEqual(conn.execute(
                    "select roles from staff_members where staff_id='s2'").fetchone()[0],
                    ",护士,助理,")
                # 已存在的 assistant/reception 补上技工/库房权限
                asst = {r[0] for r in conn.execute(
                    "select perm_key from role_permissions where role_key='assistant'")}
                self.assertTrue({"lab_order.manage", "warehouse.view"} <= asst)
                rec = {r[0] for r in conn.execute(
                    "select perm_key from role_permissions where role_key='reception'")}
                self.assertIn("lab_order.manage", rec)
            # 幂等:technician 已不在,再跑不动库(用户之后在矩阵删权限也不会被找补回来)
            self.assertFalse(auth.migrate_technician_role(db))
            with connect(db) as conn:
                conn.execute("delete from role_permissions where role_key='assistant' "
                             "and perm_key='warehouse.view'")
                conn.commit()
            self.assertFalse(auth.migrate_technician_role(db))
            with connect(db) as conn:
                self.assertIsNone(conn.execute(
                    "select 1 from role_permissions where role_key='assistant' "
                    "and perm_key='warehouse.view'").fetchone())

    def test_no_technician_is_noop(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "clinic.sqlite3"
            init_db(db)
            auth.ensure_seed_roles(db)  # 新库直接种 10 类,无 technician
            self.assertFalse(auth.migrate_technician_role(db))


class AbsorbedLabPermsUpgradeTest(unittest.TestCase):
    """#507:旧 6 角色库(从没建过 technician)直升当前 HEAD,助理/前台也必须补上技工权限。"""

    def _seed_old6(self, db):
        now = auth.now_str()
        with connect(db) as conn:
            for key, name in _OLD_ROLES:
                conn.execute("insert into roles(role_key, name, is_system, sort, created_at, updated_at) "
                             "values (?, ?, 1, 0, ?, ?)", (key, name, now, now))
            # #420 前的老权限勾选(最小化)
            for r in ("assistant", "reception"):
                conn.execute("insert into role_permissions(role_key, perm_key) values (?, 'patient.view')", (r,))
            conn.commit()

    def test_old6_direct_upgrade_grants_lab_perms(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "clinic.sqlite3"
            init_db(db)
            self._seed_old6(db)
            # 模拟 run_local 启动升级链
            self.assertEqual(auth.ensure_preset_roles_present(db), 4)
            self.assertFalse(auth.migrate_technician_role(db))   # 库里从没有过 technician
            self.assertTrue(auth.ensure_absorbed_lab_perms(db))
            with connect(db) as conn:
                asst = {r[0] for r in conn.execute(
                    "select perm_key from role_permissions where role_key='assistant'")}
                rec = {r[0] for r in conn.execute(
                    "select perm_key from role_permissions where role_key='reception'")}
            self.assertTrue({"lab_order.manage", "warehouse.view"} <= asst, "直升路径助理缺技工/库房权限")
            self.assertIn("lab_order.manage", rec, "直升路径前台缺技工权限")

    def test_assistant_patient_perms_one_shot_upgrade(self):
        """#502尾巴:存量库助理一次性补 patient.create/edit;有标记不找补。"""
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "clinic.sqlite3"
            init_db(db)
            self._seed_old6(db)
            self.assertTrue(auth.ensure_assistant_patient_perms(db))
            with connect(db) as conn:
                asst = {r[0] for r in conn.execute(
                    "select perm_key from role_permissions where role_key='assistant'")}
                self.assertTrue({"patient.create", "patient.edit"} <= asst)
                conn.execute("delete from role_permissions where role_key='assistant' "
                             "and perm_key='patient.edit'")
                conn.commit()
            self.assertFalse(auth.ensure_assistant_patient_perms(db))
            with connect(db) as conn:
                self.assertIsNone(conn.execute(
                    "select 1 from role_permissions where role_key='assistant' "
                    "and perm_key='patient.edit'").fetchone())

    def test_report_perm_revoke_one_shot(self):
        """#512:存量库一次性回收所有角色的 report.view;有标记后用户在矩阵单独授回不被再收。"""
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "clinic.sqlite3"
            init_db(db)
            self._seed_old6(db)
            with connect(db) as conn:
                conn.execute("insert into role_permissions(role_key, perm_key) values ('consultant', 'report.view')")
                conn.commit()
            self.assertTrue(auth.ensure_report_perm_admin_only(db))
            with connect(db) as conn:
                self.assertEqual(conn.execute(
                    "select count(*) from role_permissions where perm_key='report.view'").fetchone()[0], 0)
                # 用户之后在矩阵单独授回 → 再跑不被回收(有标记跳过)
                conn.execute("insert into role_permissions(role_key, perm_key) values ('doctor', 'report.view')")
                conn.commit()
            self.assertFalse(auth.ensure_report_perm_admin_only(db))
            with connect(db) as conn:
                self.assertEqual(conn.execute(
                    "select count(*) from role_permissions where perm_key='report.view'").fetchone()[0], 1)

    def test_one_shot_marker_respects_user_matrix_edits(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "clinic.sqlite3"
            init_db(db)
            self._seed_old6(db)
            auth.ensure_preset_roles_present(db)
            self.assertTrue(auth.ensure_absorbed_lab_perms(db))
            # 用户之后在矩阵里删掉 → 再跑不找补(有标记直接跳过)
            with connect(db) as conn:
                conn.execute("delete from role_permissions where role_key='assistant' "
                             "and perm_key='warehouse.view'")
                conn.commit()
            self.assertFalse(auth.ensure_absorbed_lab_perms(db))
            with connect(db) as conn:
                self.assertIsNone(conn.execute(
                    "select 1 from role_permissions where role_key='assistant' "
                    "and perm_key='warehouse.view'").fetchone())


class NewRoleAccountTest(unittest.TestCase):
    def test_open_account_with_new_role_and_position(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "clinic.sqlite3"
            init_db(db)
            auth.ensure_seed_roles(db)   # 空表整体种 10 类
            with connect(db) as conn:
                auth.create_user(conn, "admin", "管理员", "admin123", role="admin")
                conn.commit()
            client = TestClient(create_app(db))
            client.post("/api/auth/login", json={"username": "admin", "password": "admin123"})

            # 新增"客服"岗位员工(只建档案,不开账号)
            r = client.post("/api/staff-members", json={"name": "测试客服", "role": "客服"})
            self.assertEqual(r.status_code, 200, r.text)
            sid = r.json()["staff_id"]

            # 给他开通"收银员"独立角色账号
            r2 = client.post(f"/api/staff-members/{sid}/account",
                             json={"username": "cs01", "password": "secret9", "role": "cashier"})
            self.assertEqual(r2.status_code, 200, r2.text)
            self.assertEqual(r2.json()["account_role"], "cashier")


if __name__ == "__main__":
    unittest.main()
