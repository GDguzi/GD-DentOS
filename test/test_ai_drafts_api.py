import datetime as dt
import json
import tempfile
import unittest
from pathlib import Path

from fastapi.testclient import TestClient

from local_app.api import create_app
from local_app.db import connect, init_db

# AI生成量 stats 是"近7天"滚动窗口，测试日期必须相对今天(否则随时间流逝掉出窗口)
_TODAY = dt.date.today().isoformat()
_YDAY = (dt.date.today() - dt.timedelta(days=1)).isoformat()


def _insert_patient(conn, identity="p1", name="张三"):
    conn.execute(
        """
        insert into patients(patient_identity, display_name, updated_at, current_hash)
        values (?, ?, '2026-06-07 00:00:00', 'h1')
        """,
        (identity, name),
    )


def _insert_draft(
    conn,
    record_id,
    patient_identity="p1",
    content=None,
    draft_status="ai_draft",
    origin="ai_voice",
    ai_meta=None,
    created_at=_TODAY + " 10:00:00",   # 默认今天(落在近7天 stats 窗口内)
    visit_time="2026-06-10 09:00:00",
    doctor_name="李医生",
):
    content = content if content is not None else {"主诉": "牙痛"}
    ai_meta = ai_meta if ai_meta is not None else {"room": "1诊室", "source_file": "2026-06-10/a.md"}
    conn.execute(
        """
        insert into medical_records(
            record_id, patient_identity, record_type, visit_time, doctor_name,
            tooth_json, content_json, draft_status, origin, ai_meta_json,
            created_at, updated_at, current_hash
        )
        values (?, ?, '', ?, ?, '{}', ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            record_id,
            patient_identity,
            visit_time,
            doctor_name,
            json.dumps(content, ensure_ascii=False, sort_keys=True),
            draft_status,
            origin,
            json.dumps(ai_meta, ensure_ascii=False, sort_keys=True),
            created_at,
            created_at,
            "hash-" + record_id,
        ),
    )


def _insert_unclaimed(
    conn,
    unclaimed_id,
    code_name="代号A",
    status="pending",
    content=None,
    created_at="2026-06-10 11:00:00",
):
    content = content if content is not None else {"主诉": "复诊"}
    conn.execute(
        """
        insert into ai_unclaimed_drafts(
            unclaimed_id, code_name, visit_time, room, doctor_name,
            content_json, tooth_json, ai_meta_json, source_file, status, created_at
        )
        values (?, ?, '2026-06-10 08:00:00', '2诊室', '王医生', ?, '{}', '{}', ?, ?, ?)
        """,
        (
            unclaimed_id,
            code_name,
            json.dumps(content, ensure_ascii=False, sort_keys=True),
            "2026-06-10/" + unclaimed_id + ".md",
            status,
            created_at,
        ),
    )


class AiDraftsListTest(unittest.TestCase):
    def test_list_returns_drafts_unclaimed_and_stats(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = Path(tmpdir) / "clinic.sqlite3"
            init_db(db_path)
            with connect(db_path) as conn:
                _insert_patient(conn)
                # 两条 pending 草稿，验证 created_at desc 排序
                _insert_draft(conn, "local-ai-1", created_at=_YDAY + " 10:00:00")
                _insert_draft(conn, "local-ai-2", created_at=_TODAY + " 12:00:00")
                # 已确认（转正式）
                _insert_draft(
                    conn,
                    "local-ai-3",
                    draft_status="",
                    ai_meta={"edit_tier": "none", "room": "1诊室"},
                )
                # 已废弃
                _insert_draft(conn, "local-ai-4", draft_status="discarded")
                _insert_unclaimed(conn, "ai-unclaimed-1")
                _insert_unclaimed(conn, "ai-unclaimed-claimed", status="claimed")
                conn.commit()

            client = TestClient(create_app(db_path))
            resp = client.get("/api/ai-drafts")

        self.assertEqual(resp.status_code, 200)
        payload = resp.json()
        drafts = payload["drafts"]
        self.assertEqual([d["record_id"] for d in drafts], ["local-ai-2", "local-ai-1"])
        self.assertEqual(drafts[0]["display_name"], "张三")
        self.assertEqual(drafts[0]["room"], "1诊室")
        self.assertEqual(drafts[0]["content_json"], {"主诉": "牙痛"})

        unclaimed = payload["unclaimed"]
        self.assertEqual([u["unclaimed_id"] for u in unclaimed], ["ai-unclaimed-1"])
        self.assertEqual(unclaimed[0]["code_name"], "代号A")

        stats = payload["stats"]
        self.assertEqual(stats["generated"], 4)
        self.assertEqual(stats["confirmed"], 1)
        self.assertEqual(stats["no_edit"], 1)
        self.assertEqual(stats["discarded"], 1)
        self.assertEqual(stats["pending"], 2)
        # stats 只返回计数，不含患者数据
        self.assertNotIn("张三", json.dumps(stats, ensure_ascii=False))

    def test_stats_window_excludes_old_records(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = Path(tmpdir) / "clinic.sqlite3"
            init_db(db_path)
            with connect(db_path) as conn:
                _insert_patient(conn)
                _insert_draft(conn, "old-1", created_at="2020-01-01 00:00:00")
                conn.commit()

            client = TestClient(create_app(db_path))
            payload = client.get("/api/ai-drafts").json()

        self.assertEqual(payload["stats"]["generated"], 0)
        self.assertEqual(payload["stats"]["pending"], 0)


class AiDraftsConfirmTest(unittest.TestCase):
    def _client_with_draft(self, tmpdir, content=None, ai_meta=None):
        db_path = Path(tmpdir) / "clinic.sqlite3"
        init_db(db_path)
        with connect(db_path) as conn:
            _insert_patient(conn)
            _insert_draft(conn, "local-ai-1", content=content, ai_meta=ai_meta)
            conn.commit()
        return db_path, TestClient(create_app(db_path))

    def test_confirm_no_edit_tier(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            base = {"主诉": "牙痛", "现病史": "三天前开始痛"}
            db_path, client = self._client_with_draft(tmpdir, content=base)
            resp = client.post(
                "/api/ai-drafts/local-ai-1/confirm",
                json={"content_json": base},
            )
            self.assertEqual(resp.status_code, 200)
            with connect(db_path) as conn:
                row = conn.execute(
                    "select draft_status, ai_meta_json from medical_records where record_id = 'local-ai-1'"
                ).fetchone()
        self.assertEqual(row["draft_status"], "")
        meta = json.loads(row["ai_meta_json"])
        self.assertEqual(meta["edit_tier"], "none")
        self.assertIn("confirmed_at", meta)

    def test_confirm_small_edit_on_long_record_is_not_none(self):
        # 回归：长病历上医生加十几个字的真实修改，旧逻辑(ratio>=0.98)误判"none"，
        # 高估无改率。修复后任何真实改动至少 minor，绝不算 none。
        with tempfile.TemporaryDirectory() as tmpdir:
            long_text = "患者主诉" + "牙齿冷热刺激敏感持续疼痛伴咬合不适反复发作" * 12
            base = {"主诉": long_text, "现病史": long_text, "诊断": long_text}
            db_path, client = self._client_with_draft(tmpdir, content=base)
            edited = dict(base)
            edited["诊断"] = base["诊断"] + "（医生复核：建议复诊）"
            resp = client.post(
                "/api/ai-drafts/local-ai-1/confirm",
                json={"content_json": edited},
            )
            self.assertEqual(resp.status_code, 200)
            with connect(db_path) as conn:
                meta = json.loads(
                    conn.execute(
                        "select ai_meta_json from medical_records where record_id = 'local-ai-1'"
                    ).fetchone()["ai_meta_json"]
                )
        self.assertNotEqual(meta["edit_tier"], "none")
        self.assertEqual(meta["edit_tier"], "minor")

    def test_confirm_minor_edit_tier(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            base = {"text": "患者主诉牙齿疼痛持续三天伴随冷热刺激敏感建议拍片检查后续治疗方案待定"}
            db_path, client = self._client_with_draft(tmpdir, content=base)
            edited = {"text": base["text"] + "并预约复诊"}
            resp = client.post(
                "/api/ai-drafts/local-ai-1/confirm",
                json={"content_json": edited},
            )
            self.assertEqual(resp.status_code, 200)
            with connect(db_path) as conn:
                meta = json.loads(
                    conn.execute(
                        "select ai_meta_json from medical_records where record_id = 'local-ai-1'"
                    ).fetchone()["ai_meta_json"]
                )
        self.assertEqual(meta["edit_tier"], "minor")

    def test_confirm_major_edit_tier(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            base = {"text": "牙痛"}
            db_path, client = self._client_with_draft(tmpdir, content=base)
            edited = {"text": "完全不同的内容，重写了整段病历记录，患者实际是来洗牙的"}
            resp = client.post(
                "/api/ai-drafts/local-ai-1/confirm",
                json={"content_json": edited},
            )
            self.assertEqual(resp.status_code, 200)
            with connect(db_path) as conn:
                meta = json.loads(
                    conn.execute(
                        "select ai_meta_json from medical_records where record_id = 'local-ai-1'"
                    ).fetchone()["ai_meta_json"]
                )
        self.assertEqual(meta["edit_tier"], "major")

    def test_confirm_writes_version_and_audit(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path, client = self._client_with_draft(tmpdir)
            resp = client.post(
                "/api/ai-drafts/local-ai-1/confirm",
                json={"content_json": {"主诉": "改过的"}, "doctor_name": "新医生"},
            )
            self.assertEqual(resp.status_code, 200)
            with connect(db_path) as conn:
                versions = conn.execute(
                    "select count(*) from medical_record_versions where record_id = 'local-ai-1'"
                ).fetchone()[0]
                audits = conn.execute(
                    "select count(*) from audit_logs where entity_id = 'local-ai-1'"
                ).fetchone()[0]
                rec = conn.execute(
                    "select doctor_name from medical_records where record_id = 'local-ai-1'"
                ).fetchone()
        self.assertEqual(versions, 1)
        self.assertEqual(audits, 1)
        self.assertEqual(rec["doctor_name"], "新医生")

    def test_confirm_uses_original_content_when_present(self):
        # ai_meta 带 original_content_json：基准应取它，而非当前库里的 content
        with tempfile.TemporaryDirectory() as tmpdir:
            current = {"text": "当前库里已被改过的内容完全不一样"}
            original = {"text": "确认时提交了和原始一致的内容"}
            db_path, client = self._client_with_draft(
                tmpdir,
                content=current,
                ai_meta={
                    "room": "1诊室",
                    "original_content_json": json.dumps(original, ensure_ascii=False, sort_keys=True),
                },
            )
            resp = client.post(
                "/api/ai-drafts/local-ai-1/confirm",
                json={"content_json": original},
            )
            self.assertEqual(resp.status_code, 200)
            with connect(db_path) as conn:
                meta = json.loads(
                    conn.execute(
                        "select ai_meta_json from medical_records where record_id = 'local-ai-1'"
                    ).fetchone()["ai_meta_json"]
                )
        self.assertEqual(meta["edit_tier"], "none")

    def test_confirm_404_when_not_ai_draft(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = Path(tmpdir) / "clinic.sqlite3"
            init_db(db_path)
            with connect(db_path) as conn:
                _insert_patient(conn)
                _insert_draft(conn, "local-ai-9", draft_status="")
                conn.commit()
            client = TestClient(create_app(db_path))
            resp = client.post(
                "/api/ai-drafts/local-ai-9/confirm", json={"content_json": {}}
            )
        self.assertEqual(resp.status_code, 404)

    def test_confirm_404_when_missing(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = Path(tmpdir) / "clinic.sqlite3"
            init_db(db_path)
            client = TestClient(create_app(db_path))
            resp = client.post(
                "/api/ai-drafts/nope/confirm", json={"content_json": {}}
            )
        self.assertEqual(resp.status_code, 404)


class AiDraftsDiscardTest(unittest.TestCase):
    def test_discard_sets_status_and_audit(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = Path(tmpdir) / "clinic.sqlite3"
            init_db(db_path)
            with connect(db_path) as conn:
                _insert_patient(conn)
                _insert_draft(conn, "local-ai-1")
                conn.commit()
            client = TestClient(create_app(db_path))
            resp = client.post("/api/ai-drafts/local-ai-1/discard")
            self.assertEqual(resp.status_code, 200)
            with connect(db_path) as conn:
                row = conn.execute(
                    "select draft_status from medical_records where record_id = 'local-ai-1'"
                ).fetchone()
                audits = conn.execute(
                    "select count(*) from audit_logs where entity_id = 'local-ai-1'"
                ).fetchone()[0]
        self.assertEqual(row["draft_status"], "discarded")
        self.assertEqual(audits, 1)

    def test_discard_404_when_not_ai_draft(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = Path(tmpdir) / "clinic.sqlite3"
            init_db(db_path)
            with connect(db_path) as conn:
                _insert_patient(conn)
                _insert_draft(conn, "local-ai-1", draft_status="")
                conn.commit()
            client = TestClient(create_app(db_path))
            resp = client.post("/api/ai-drafts/local-ai-1/discard")
        self.assertEqual(resp.status_code, 404)


class AiUnclaimedClaimTest(unittest.TestCase):
    def test_claim_moves_to_medical_records(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = Path(tmpdir) / "clinic.sqlite3"
            init_db(db_path)
            with connect(db_path) as conn:
                _insert_patient(conn, identity="p1")
                _insert_unclaimed(conn, "ai-unclaimed-1", content={"主诉": "复诊"})
                conn.commit()
            client = TestClient(create_app(db_path))
            resp = client.post(
                "/api/ai-unclaimed/ai-unclaimed-1/claim",
                json={"patient_identity": "p1"},
            )
            self.assertEqual(resp.status_code, 200)
            with connect(db_path) as conn:
                rec = conn.execute(
                    """
                    select record_id, patient_identity, draft_status, origin,
                           content_json, ai_meta_json
                    from medical_records where patient_identity = 'p1'
                    """
                ).fetchone()
                unclaimed_status = conn.execute(
                    "select status from ai_unclaimed_drafts where unclaimed_id = 'ai-unclaimed-1'"
                ).fetchone()[0]
                audits = conn.execute(
                    "select count(*) from audit_logs where action like '%claim%'"
                ).fetchone()[0]
        self.assertTrue(rec["record_id"].startswith("local-ai-"))
        self.assertEqual(rec["draft_status"], "ai_draft")
        self.assertEqual(rec["origin"], "ai_voice")
        self.assertEqual(json.loads(rec["content_json"]), {"主诉": "复诊"})
        meta = json.loads(rec["ai_meta_json"])
        self.assertEqual(meta["code_name"], "代号A")
        self.assertEqual(meta["room"], "2诊室")
        self.assertEqual(meta["claimed_from"], "ai-unclaimed-1")
        self.assertEqual(unclaimed_status, "claimed")
        self.assertEqual(audits, 1)

    def test_claim_404_when_patient_missing(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = Path(tmpdir) / "clinic.sqlite3"
            init_db(db_path)
            with connect(db_path) as conn:
                _insert_unclaimed(conn, "ai-unclaimed-1")
                conn.commit()
            client = TestClient(create_app(db_path))
            resp = client.post(
                "/api/ai-unclaimed/ai-unclaimed-1/claim",
                json={"patient_identity": "ghost"},
            )
        self.assertEqual(resp.status_code, 404)

    def test_claim_404_when_unclaimed_missing(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = Path(tmpdir) / "clinic.sqlite3"
            init_db(db_path)
            with connect(db_path) as conn:
                _insert_patient(conn)
                conn.commit()
            client = TestClient(create_app(db_path))
            resp = client.post(
                "/api/ai-unclaimed/nope/claim",
                json={"patient_identity": "p1"},
            )
        self.assertEqual(resp.status_code, 404)

    def test_claim_404_when_not_pending(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = Path(tmpdir) / "clinic.sqlite3"
            init_db(db_path)
            with connect(db_path) as conn:
                _insert_patient(conn)
                _insert_unclaimed(conn, "ai-unclaimed-1", status="claimed")
                conn.commit()
            client = TestClient(create_app(db_path))
            resp = client.post(
                "/api/ai-unclaimed/ai-unclaimed-1/claim",
                json={"patient_identity": "p1"},
            )
        self.assertEqual(resp.status_code, 404)


class AiUnclaimedDiscardTest(unittest.TestCase):
    def test_discard_sets_status_and_audit(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = Path(tmpdir) / "clinic.sqlite3"
            init_db(db_path)
            with connect(db_path) as conn:
                _insert_unclaimed(conn, "ai-unclaimed-1")
                conn.commit()
            client = TestClient(create_app(db_path))
            resp = client.post("/api/ai-unclaimed/ai-unclaimed-1/discard")
            self.assertEqual(resp.status_code, 200)
            with connect(db_path) as conn:
                status = conn.execute(
                    "select status from ai_unclaimed_drafts where unclaimed_id = 'ai-unclaimed-1'"
                ).fetchone()[0]
                audits = conn.execute(
                    "select count(*) from audit_logs where entity_id = 'ai-unclaimed-1'"
                ).fetchone()[0]
        self.assertEqual(status, "discarded")
        self.assertEqual(audits, 1)

    def test_discard_404_when_missing(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = Path(tmpdir) / "clinic.sqlite3"
            init_db(db_path)
            client = TestClient(create_app(db_path))
            resp = client.post("/api/ai-unclaimed/nope/discard")
        self.assertEqual(resp.status_code, 404)


if __name__ == "__main__":
    unittest.main()
