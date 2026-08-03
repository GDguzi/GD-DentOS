import tempfile
import unittest
from pathlib import Path
from unittest import mock

from fastapi.testclient import TestClient

from local_app.api import create_app
from local_app.db import connect, init_db


def _client(tmpdir):
    db_path = Path(tmpdir) / "clinic.sqlite3"
    init_db(db_path)
    with connect(db_path) as conn:
        conn.execute(
            "insert into patients(patient_identity, display_name, updated_at, current_hash) "
            "values ('p1', '测试患者', '2026-06-07 00:00:00', 'h1')"
        )
        conn.commit()
    return db_path, TestClient(create_app(db_path))


class ReturnVisitOpsTest(unittest.TestCase):
    def test_create_return_visit(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path, client = _client(tmpdir)
            resp = client.post(
                "/api/patients/p1/return-visits",
                json={
                    "due_time": "2026-06-20",
                    "item_name": "术后复查",
                    "visitor": "护士小张",
                    "return_type": "电话回访",
                },
            )
            self.assertEqual(resp.status_code, 200)
            rv_id = resp.json()["return_visit_id"]
            self.assertTrue(rv_id.startswith("local-rv-"))
            with connect(db_path) as conn:
                row = conn.execute(
                    "select visitor, return_type, origin, is_deleted from return_visits "
                    "where return_visit_id = ?",
                    (rv_id,),
                ).fetchone()
            self.assertEqual(row["visitor"], "护士小张")
            self.assertEqual(row["origin"], "local")
            self.assertEqual(row["is_deleted"], 0)

    def test_edit_return_doctor_and_type_persist(self):
        # #37：编辑回访医生/类型能保存(前端卡片也展示这两字段)
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path, client = _client(tmpdir)
            rv_id = client.post("/api/patients/p1/return-visits", json={
                "due_time": "2026-06-20", "item_name": "复查"}).json()["return_visit_id"]
            resp = client.put(f"/api/return-visits/{rv_id}", json={
                "expected_revision": 1,
                "return_doctor": "王医生", "return_type": "到店回访"})
            self.assertEqual(resp.status_code, 200)
            with connect(db_path) as conn:
                row = conn.execute(
                    "select return_doctor, return_type from return_visits where return_visit_id = ?",
                    (rv_id,)).fetchone()
            self.assertEqual(row["return_doctor"], "王医生")
            self.assertEqual(row["return_type"], "到店回访")

    def test_client_actual_date_rejected(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path, client = _client(tmpdir)
            rv_id = client.post("/api/patients/p1/return-visits", json={
                "due_time": "2026-06-20", "item_name": "复查"}).json()["return_visit_id"]
            self.assertEqual(client.put(f"/api/return-visits/{rv_id}",
                                        json={"expected_revision": 1,
                                              "actual_date": "下周"}).status_code, 400)

    def test_today_completed_counts_by_actual_date(self):
        # #54：今天实际完成"未来计划"的回访 → 今日已回访数+1(按actual_date而非due_time)
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path, client = _client(tmpdir)
            rv_id = client.post("/api/patients/p1/return-visits", json={
                "due_time": "2026-06-20", "item_name": "种植复查"}).json()["return_visit_id"]
            with mock.patch("local_app.timeutil.today_str", return_value="2026-06-14"):
                client.put(f"/api/return-visits/{rv_id}",
                           json={"expected_revision": 1, "status": "已回访"})
            data = client.get("/api/today-work?date=2026-06-14").json()
            self.assertEqual(data["summary"]["completed_return_visits"], 1)

    def test_edit_workflow_fields_persist(self):
        # #13：回访工作流字段 回访人/回访结果/实际回访日期 能保存(标记已回访)
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path, client = _client(tmpdir)
            rv_id = client.post("/api/patients/p1/return-visits", json={
                "due_time": "2026-06-20", "item_name": "种植复查"}).json()["return_visit_id"]
            with mock.patch("local_app.timeutil.today_str", return_value="2026-06-14"):
                resp = client.put(f"/api/return-visits/{rv_id}", json={
                    "expected_revision": 1,
                    "status": "已回访", "visitor": "前台小李",
                    "return_result": "恢复良好", "note": "电话告知按时复诊",
                })
            self.assertEqual(resp.status_code, 200)
            with connect(db_path) as conn:
                row = conn.execute(
                    "select status, visitor, return_result, actual_date, note "
                    "from return_visits where return_visit_id = ?", (rv_id,)).fetchone()
            self.assertEqual(row["status"], "已回访")
            self.assertEqual(row["visitor"], "前台小李")
            self.assertEqual(row["return_result"], "恢复良好")
            self.assertEqual(row["actual_date"], "2026-06-14")
            self.assertEqual(row["note"], "电话告知按时复诊")

    def test_intent_and_satisfaction_persist(self):
        # P2-19：回访意向等级/满意度 可在新建时录、编辑时改，落独立列
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path, client = _client(tmpdir)
            rv_id = client.post("/api/patients/p1/return-visits", json={
                "due_time": "2026-06-20", "item_name": "种植复查",
                "intent_level": "高意向", "satisfaction": "满意"}).json()["return_visit_id"]
            with connect(db_path) as conn:
                row = conn.execute(
                    "select intent_level, satisfaction from return_visits "
                    "where return_visit_id = ?", (rv_id,)).fetchone()
            self.assertEqual(row["intent_level"], "高意向")
            self.assertEqual(row["satisfaction"], "满意")
            resp = client.put(f"/api/return-visits/{rv_id}", json={
                "expected_revision": 1,
                "intent_level": "中意向", "satisfaction": "非常满意"})
            self.assertEqual(resp.status_code, 200)
            with connect(db_path) as conn:
                row = conn.execute(
                    "select intent_level, satisfaction from return_visits "
                    "where return_visit_id = ?", (rv_id,)).fetchone()
            self.assertEqual(row["intent_level"], "中意向")
            self.assertEqual(row["satisfaction"], "非常满意")

    def test_noop_edit_does_not_insert_version(self):
        # #150：相同值连续 PUT 不应灌 return_visit_versions(no-op 守卫)
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path, client = _client(tmpdir)
            rv_id = client.post("/api/patients/p1/return-visits", json={
                "due_time": "2026-06-20", "item_name": "复查"}).json()["return_visit_id"]

            def vcount():
                with connect(db_path) as conn:
                    return conn.execute(
                        "select count(*) c from return_visit_versions where return_visit_id = ?",
                        (rv_id,)).fetchone()["c"]

            # 创建已落 revision=1 最终快照；真实变更再落 revision=2。
            self.assertEqual(client.put(f"/api/return-visits/{rv_id}",
                                        json={"expected_revision": 1,
                                              "intent_level": "高意向"}).status_code, 200)
            self.assertEqual(vcount(), 2)
            # 相同值再 PUT → no-op，版本数不变
            self.assertEqual(client.put(f"/api/return-visits/{rv_id}",
                                        json={"expected_revision": 2,
                                              "intent_level": "高意向"}).status_code, 200)
            self.assertEqual(vcount(), 2)

    def test_soft_delete_twice_404(self):
        # 审查#25：已软删的回访再删→404,不重复灌版本
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path, client = _client(tmpdir)
            rv_id = client.post("/api/patients/p1/return-visits", json={
                "due_time": "2026-06-20", "item_name": "复查"}).json()["return_visit_id"]
            self.assertEqual(client.request("DELETE", f"/api/return-visits/{rv_id}",
                                            json={"expected_revision": 1}).status_code, 200)
            self.assertEqual(client.request("DELETE", f"/api/return-visits/{rv_id}",
                                            json={"expected_revision": 1}).status_code, 404)
            with connect(db_path) as conn:
                n = conn.execute("select count(*) from return_visit_versions where return_visit_id=?", (rv_id,)).fetchone()[0]
            self.assertEqual(n, 2)   # 创建 + 删除各一个最终快照

    def test_create_rejects_empty_due_time(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            _, client = _client(tmpdir)
            resp = client.post("/api/patients/p1/return-visits", json={"item_name": "x"})
        self.assertEqual(resp.status_code, 400)

    def test_create_rejects_empty_item_name(self):
        # 复审#6：事项+到期时间一起录
        with tempfile.TemporaryDirectory() as tmpdir:
            _, client = _client(tmpdir)
            resp = client.post(
                "/api/patients/p1/return-visits", json={"due_time": "2026-06-20"}
            )
        self.assertEqual(resp.status_code, 400)

    def test_soft_delete_hides_from_list(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path, client = _client(tmpdir)
            rv_id = client.post(
                "/api/patients/p1/return-visits",
                json={"due_time": "2020-01-01", "item_name": "旧回访"},
            ).json()["return_visit_id"]
            # 软删
            self.assertEqual(
                client.request("DELETE", f"/api/return-visits/{rv_id}",
                               json={"expected_revision": 1}).status_code, 200
            )
            with connect(db_path) as conn:
                deleted = conn.execute(
                    "select is_deleted from return_visits where return_visit_id = ?",
                    (rv_id,),
                ).fetchone()["is_deleted"]
                # 复审#7：软删要留版本快照 + 审计
                ver = conn.execute(
                    "select count(*) from return_visit_versions where return_visit_id = ?",
                    (rv_id,),
                ).fetchone()[0]
                audit = conn.execute(
                    "select action from audit_logs where entity_type='return_visit' "
                    "and entity_id = ? order by audit_id desc",
                    (rv_id,),
                ).fetchone()
            self.assertEqual(deleted, 1)
            self.assertEqual(ver, 2)
            self.assertEqual(audit["action"], "soft_delete_return_visit")
            # 列表(date 取很晚以覆盖该到期日)不应再出现该回访
            listed = client.get("/api/return-visits?date=2026-12-31").json()
            ids = [r["return_visit_id"] for r in listed.get("list", [])]
            self.assertNotIn(rv_id, ids)

    def test_delete_unknown_404(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            _, client = _client(tmpdir)
            self.assertEqual(client.request(
                "DELETE", "/api/return-visits/nope",
                json={"expected_revision": 1},
            ).status_code, 404)

    def test_calendar_counts_by_day(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            _, client = _client(tmpdir)
            for due in ["2026-07-05", "2026-07-05", "2026-07-18"]:
                client.post("/api/patients/p1/return-visits",
                            json={"due_time": due, "item_name": "回访"})
            cal = client.get("/api/return-visits/calendar?month=2026-07").json()
            self.assertEqual(cal["total"], 3)
            by_day = {d["date"]: d["count"] for d in cal["days"]}
            self.assertEqual(by_day["2026-07-05"], 2)
            self.assertEqual(by_day["2026-07-18"], 1)

    def test_calendar_bad_month_400(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            _, client = _client(tmpdir)
            self.assertEqual(
                client.get("/api/return-visits/calendar?month=2026").status_code, 400
            )


if __name__ == "__main__":
    unittest.main()
