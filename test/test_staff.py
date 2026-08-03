import tempfile
import unittest
from pathlib import Path

from fastapi.testclient import TestClient

from local_app.api import create_app
from local_app.db import init_db


def _client(tmp):
    db = Path(tmp) / "clinic.sqlite3"
    init_db(db)
    return db, TestClient(create_app(db))


class StaffCrudTest(unittest.TestCase):
    def test_create_list_update_delete(self):
        with tempfile.TemporaryDirectory() as tmp:
            _, client = _client(tmp)
            # 建
            r = client.post("/api/staff-members", json={"name": "王医生", "role": "医生"})
            self.assertEqual(r.status_code, 200)
            sid = r.json()["staff_id"]
            client.post("/api/staff-members", json={"name": "李护士", "role": "护士"})
            # 列
            members = client.get("/api/staff-members").json()["members"]
            self.assertEqual({m["name"] for m in members}, {"王医生", "李护士"})
            # 按角色筛
            docs = client.get("/api/staff-members?role=医生").json()["members"]
            self.assertEqual([m["name"] for m in docs], ["王医生"])
            # 改
            r = client.put(f"/api/staff-members/{sid}", json={"name": "王主任", "role": "医生", "note": "正畸"})
            self.assertEqual(r.status_code, 200)
            self.assertEqual(client.get("/api/staff-members?role=医生").json()["members"][0]["name"], "王主任")
            # 删(软删)→ 不在列表
            self.assertEqual(client.delete(f"/api/staff-members/{sid}").status_code, 200)
            names = {m["name"] for m in client.get("/api/staff-members").json()["members"]}
            self.assertNotIn("王主任", names)
            self.assertIn("李护士", names)

    def test_empty_name_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            _, client = _client(tmp)
            self.assertEqual(client.post("/api/staff-members", json={"name": "  ", "role": "医生"}).status_code, 400)

    def test_bad_role_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            _, client = _client(tmp)
            self.assertEqual(client.post("/api/staff-members", json={"name": "张三", "role": "院长"}).status_code, 400)

    def test_update_missing_404(self):
        with tempfile.TemporaryDirectory() as tmp:
            _, client = _client(tmp)
            self.assertEqual(client.put("/api/staff-members/nope", json={"name": "x", "role": "医生"}).status_code, 404)

    def test_delete_missing_404(self):
        with tempfile.TemporaryDirectory() as tmp:
            _, client = _client(tmp)
            self.assertEqual(client.delete("/api/staff-members/nope").status_code, 404)

    def test_update_soft_deleted_returns_404(self):
        # #231：编辑后删除同一人,stale PUT 不能写进已软删行
        with tempfile.TemporaryDirectory() as tmp:
            _, client = _client(tmp)
            sid = client.post("/api/staff-members", json={"name": "王医生", "role": "医生"}).json()["staff_id"]
            self.assertEqual(client.delete(f"/api/staff-members/{sid}").status_code, 200)
            r = client.put(f"/api/staff-members/{sid}", json={"name": "李医生", "role": "医生"})
            self.assertEqual(r.status_code, 404)
            # 软删人员不出现在列表(确认没被 stale PUT 复活)
            names = {m["name"] for m in client.get("/api/staff-members").json()["members"]}
            self.assertNotIn("李医生", names)
            self.assertNotIn("王医生", names)

    def test_profile_fields_create_list_update(self):
        # P2-31 员工档案补全：工号/手机/性别/证件/职称/执业证号/科室 落库+回读+可改
        with tempfile.TemporaryDirectory() as tmp:
            _, client = _client(tmp)
            r = client.post("/api/staff-members", json={
                "name": "王医生", "role": "医生",
                "job_no": "D001", "phone": "13900000000", "sex": "男",
                "id_card": "990107199001010017", "title": "主治医师",
                "license_no": "1102000", "department": "口腔综合科"})
            self.assertEqual(r.status_code, 200)
            sid = r.json()["staff_id"]
            self.assertEqual(r.json()["job_no"], "D001")
            # 档案明细只从受控的 staff-admin/list 读(选人接口 /api/staff-members 不回隐私字段,见 #291)
            m = client.get("/api/staff-admin/list").json()["members"][0]
            self.assertEqual(m["title"], "主治医师")
            self.assertEqual(m["license_no"], "1102000")
            self.assertEqual(m["department"], "口腔综合科")
            # 选人接口不应泄露身份证/手机/证号
            light = client.get("/api/staff-members").json()["members"][0]
            self.assertEqual(set(light.keys()), {"staff_id", "name", "role", "roles"})
            # 改：更新职称/科室
            client.put(f"/api/staff-members/{sid}", json={
                "name": "王主任", "role": "医生", "title": "主任医师", "department": "正畸科"})
            m2 = client.get("/api/staff-admin/list").json()["members"][0]
            self.assertEqual(m2["title"], "主任医师")
            self.assertEqual(m2["department"], "正畸科")
            self.assertEqual(m2["job_no"], "")   # PUT 未传 → 清空(整档编辑语义)


if __name__ == "__main__":
    unittest.main()
