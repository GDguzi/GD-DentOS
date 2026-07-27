"""回归：v3 下自勾遗留 admin 角色不得越过 require_admin 硬闸。

已复现的提权路径：空库 v3 会种出零权限的遗留 admin 角色，
持有 staff.edit 的账号可给自己勾上它，从而通过患者合并/储值退现等仅管理员接口。
修法：require_admin 在 v3 目标模式下只认 is_system_admin。
"""
import tempfile
import unittest
from pathlib import Path

from fastapi.testclient import TestClient

from local_app.api import create_app
from local_app.db import connect, init_db
from local_app.migrations import apply_migrations
from local_app.passwords import hash_password
from local_app.v3_bootstrap import ensure_v3_personnel_schema


class AdminRoleEscalationTest(unittest.TestCase):
    def _fresh(self, tmp):
        db = Path(tmp) / "clinic.sqlite3"
        init_db(db)
        ensure_v3_personnel_schema(db)
        apply_migrations(db)
        with connect(db) as conn:
            conn.execute(
                "insert into staff_members(staff_id, name, employment_status) "
                "values ('s1', '合成员工', 'employed')"
            )
            conn.execute(
                "insert into staff_role_assignments(staff_id, role_key, is_primary) "
                "values ('s1', 'director', 1)"
            )
            for perm in ("staff.view", "staff.edit", "membership.view", "membership.refund"):
                conn.execute(
                    "insert or ignore into role_permissions(role_key, perm_key) "
                    "values ('director', ?)", (perm,))
            conn.execute(
                "insert into users(id, username, display_name, password_hash, staff_id, "
                "is_active, is_system_admin, account_kind) "
                "values ('u1', 'synth', '合成', ?, 's1', 1, 0, 'staff')",
                (hash_password("pass123"),))
            conn.commit()
        client = TestClient(create_app(db, access_v3=True))
        self.assertEqual(
            client.post("/api/auth/login",
                        json={"username": "synth", "password": "pass123"}).status_code, 200)
        return client

    def test_self_assigned_admin_role_cannot_pass_require_admin(self):
        with tempfile.TemporaryDirectory() as tmp:
            client = self._fresh(tmp)
            refund = {"request_id": "probe", "amount": 1}
            self.assertEqual(
                client.post("/api/patients/missing/member-account/refund",
                            json=refund).status_code, 403)

            granted = client.put("/api/staff-members/s1", json={
                "name": "合成", "primary_role": "director",
                "role_keys": ["director", "admin"]})
            self.assertEqual(granted.status_code, 200, granted.text)
            self.assertIn("admin", granted.json()["role_keys"])

            # 勾上遗留 admin 角色后仍必须 403——只有 is_system_admin 才是管理员
            after = client.post("/api/patients/missing/member-account/refund",
                                json={"request_id": "probe2", "amount": 1})
            self.assertEqual(after.status_code, 403, after.text)


if __name__ == "__main__":
    unittest.main()
