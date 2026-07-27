"""Phase3 迁移机制归一:schema_migrations 有序记账。

三种剧本:全新库(全跑+记账)、二次启动(全跳过)、模拟生产存量库(旧机制已应用过,
土法标记在,归一后首次记账不得改变任何角色/权限数据)。"""
import tempfile
import unittest
from pathlib import Path

from local_app import auth
from local_app.db import connect, init_db
from local_app.migrations import MIGRATIONS, apply_migrations


def _snapshot_roles_perms(db):
    with connect(db) as conn:
        roles = sorted(tuple(r) for r in conn.execute(
            "select role_key, name, is_system, sort from roles"))
        perms = sorted(tuple(r) for r in conn.execute(
            "select role_key, perm_key from role_permissions"))
    return roles, perms


class MigrationRegistryTest(unittest.TestCase):
    def test_ids_unique_and_ordered(self):
        ids = [m[0] for m in MIGRATIONS]
        self.assertEqual(ids, sorted(ids), "登记册 id 必须单调递增")
        self.assertEqual(len(ids), len(set(ids)), "登记册 id 不得重复")


class FreshDbTest(unittest.TestCase):
    def test_fresh_db_applies_all_then_skips(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "clinic.sqlite3"
            init_db(db)
            applied = apply_migrations(db)
            self.assertEqual(applied, [m[0] for m in MIGRATIONS], "全新库应按序全跑")
            with connect(db) as conn:
                rows = {r["id"]: r["applied_at"] for r in conn.execute(
                    "select id, applied_at from schema_migrations")}
                n_roles = conn.execute("select count(*) from roles").fetchone()[0]
                n_src = conn.execute("select count(*) from referral_sources").fetchone()[0]
            self.assertEqual(set(rows), {m[0] for m in MIGRATIONS})
            self.assertGreater(n_roles, 0, "角色种子应已种")
            self.assertGreater(n_src, 0, "患者来源树种子应已种")
            # 二次启动:全部跳过,记账行原样
            self.assertEqual(apply_migrations(db), [], "已记账的迁移不得重跑")
            with connect(db) as conn:
                rows2 = {r["id"]: r["applied_at"] for r in conn.execute(
                    "select id, applied_at from schema_migrations")}
            self.assertEqual(rows, rows2, "二次启动不得改动记账")

    def test_wrappers_still_work(self):
        # auth 转发壳兼容旧调用点(几十个测试按 auth.ensure_xxx 引用)
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "clinic.sqlite3"
            init_db(db)
            self.assertTrue(auth.ensure_seed_roles(db))
            self.assertFalse(auth.ensure_seed_roles(db))   # 幂等
            self.assertEqual(auth.ensure_preset_roles_present(db), 0)


class LegacyProductionDbTest(unittest.TestCase):
    def test_first_bookkeeping_run_changes_nothing(self):
        """模拟生产存量库:旧机制(逐个调 ensure_*)已全部应用、土法标记在。
        归一后首次 apply_migrations 只是记账,角色/权限数据必须一字不变。"""
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "clinic.sqlite3"
            init_db(db)
            # 旧机制走一遍(等价于生产库现状)
            auth.ensure_seed_roles(db)
            auth.ensure_preset_roles_present(db)
            auth.migrate_technician_role(db)
            auth.ensure_absorbed_lab_perms(db)
            auth.ensure_assistant_patient_perms(db)
            auth.ensure_report_perm_admin_only(db)
            auth.ensure_comm_call_perms(db)
            from local_app.seed_referral_sources import seed_referral_sources
            seed_referral_sources(db)
            # 用户在矩阵里自定义过权限(归一记账绝不能覆盖用户改动)
            with connect(db) as conn:
                conn.execute("delete from role_permissions where role_key='reception' "
                             "and perm_key='lab_order.manage'")
                conn.commit()
            before = _snapshot_roles_perms(db)
            applied = apply_migrations(db)
            self.assertEqual(applied, [m[0] for m in MIGRATIONS], "首次记账应把全部迁移登记")
            self.assertEqual(_snapshot_roles_perms(db), before,
                             "存量库首次记账不得改变任何角色/权限数据(含用户自定义)")
            self.assertEqual(apply_migrations(db), [])




class SchemaDiffPropertyTest(unittest.TestCase):
    """Phase3 第二刀(schema.sql 差分补列)属性测试:随机删掉一批历史补列模拟旧库,
    跑 init_db 后结构必须与全新库逐表逐列(名/类型/NOT NULL/默认值)完全一致。"""

    # 覆盖多表、多类型(text/real/integer)、含 NOT NULL 与各种默认值的代表性历史补列
    DROPS = [
        ("treatment_items", "refunded_fee"),
        ("treatment_items", "tooth_codes"),
        ("return_visits", "created_at"),
        ("return_visits", "intent_level"),
        ("return_visits", "channel"),
        ("return_visits", "revision"),
        ("communications", "direction"),
        ("communications", "reason"),
        ("communications", "revision"),
        ("calls", "revision"),
        ("medical_records", "draft_status"),
        ("patient_images", "tooth_codes"),
        ("treatment_plans", "bill_id"),
        ("treatment_orders", "visit_type"),
    ]

    @staticmethod
    def _full_schema(db):
        out = {}
        with connect(db) as conn:
            for (t,) in conn.execute("select name from sqlite_master where type='table' "
                                     "and name not like 'sqlite_%' order by name"):
                out[t] = sorted(
                    (r[1], (r[2] or "").lower(), r[3], r[4]) for r in
                    conn.execute(f'pragma table_info("{t}")'))
        return out

    def test_old_db_converges_to_fresh_schema(self):
        with tempfile.TemporaryDirectory() as tmp:
            fresh = Path(tmp) / "fresh.sqlite3"
            old = Path(tmp) / "old.sqlite3"
            init_db(fresh)
            init_db(old)
            with connect(old) as conn:   # 模拟旧库:删掉历史补列(sqlite>=3.35 支持 drop column)
                for table, col in self.DROPS:
                    conn.execute(f'alter table "{table}" drop column "{col}"')
                conn.commit()
            init_db(old)                 # 启动=差分补列
            self.assertEqual(self._full_schema(old), self._full_schema(fresh),
                             "旧库启动后结构必须与全新库逐列一致(名/类型/NOT NULL/默认值)")

    def test_added_column_ddl_faithful(self):
        # 抽查:补回的列类型/约束/默认值忠实还原(refunded_fee real not null default 0)
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "x.sqlite3"
            init_db(db)
            with connect(db) as conn:
                conn.execute('alter table "treatment_items" drop column "refunded_fee"')
                conn.commit()
            init_db(db)
            with connect(db) as conn:
                r = next(r for r in conn.execute("pragma table_info(treatment_items)")
                         if r[1] == "refunded_fee")
            self.assertEqual((r[2].lower(), r[3], r[4]), ("real", 1, "0"))
    def test_nonconstant_default_red_line_fails_loud(self):
        """红线兜底:非常量默认(current_timestamp)的列若从存量库缺失,差分补列必须
        响亮报错(SQLite 的 ADD COLUMN 拒绝非常量默认)——绝不静默跳过留下缺列库。
        用真 schema 的 audit_logs.created_at(default current_timestamp)实测。"""
        import sqlite3 as _sq
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "x.sqlite3"
            init_db(db)
            with connect(db) as conn:
                # 表里得有数据:SQLite 对空表放行非常量默认的 ADD COLUMN,有数据才拒——
                # 真实存量库的流水表必然非空,红线在真实场景必然生效
                from local_app.db import audit_write
                audit_write(conn, 'probe', 'x', 'act', operator='tester')
                conn.commit()
                conn.execute('drop index if exists idx_audit_logs_created')   # 索引挡删列,先摘
                conn.execute('alter table "audit_logs" drop column "created_at"')
                conn.commit()
            with self.assertRaises(_sq.OperationalError):
                init_db(db)   # 响亮失败=红线生效;需要此类列请配常量默认+应用层赋值或走有序迁移

    def test_legacy_import_conflicts_upgrade_preserves_rows_and_empty_keys(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "legacy.sqlite3"
            with connect(db) as conn:
                conn.executescript(
                    """
                    create table sync_batches (
                        batch_id text primary key,
                        source text not null,
                        status text not null,
                        started_at text not null,
                        finished_at text,
                        summary_json text not null default '{}'
                    );
                    create table import_conflicts (
                        conflict_id integer primary key autoincrement,
                        entity_type text not null,
                        entity_id text not null,
                        field_name text not null,
                        local_value text,
                        incoming_value text,
                        batch_id text references sync_batches(batch_id),
                        status text not null default 'open',
                        created_at text not null default current_timestamp
                    );
                    insert into import_conflicts(
                        entity_type, entity_id, field_name, local_value, incoming_value
                    ) values
                        ('synthetic', 'one', 'field_a', 'local-a', 'incoming-a'),
                        ('synthetic', 'two', 'field_b', 'local-b', 'incoming-b');
                    """
                )
                conn.commit()

            init_db(db)
            with connect(db) as conn:
                first = [
                    tuple(row)
                    for row in conn.execute(
                        "select conflict_id, entity_type, entity_id, field_name, "
                        "local_value, incoming_value, conflict_key, reason_code "
                        "from import_conflicts order by conflict_id"
                    )
                ]

            init_db(db)
            with connect(db) as conn:
                second = [
                    tuple(row)
                    for row in conn.execute(
                        "select conflict_id, entity_type, entity_id, field_name, "
                        "local_value, incoming_value, conflict_key, reason_code "
                        "from import_conflicts where conflict_id <= 2 order by conflict_id"
                    )
                ]
                index = conn.execute(
                    "select sql from sqlite_master where type='index' and name=?",
                    ("idx_import_conflicts_conflict_key",),
                ).fetchone()
                conn.execute(
                    "insert into import_conflicts(entity_type,entity_id,field_name) "
                    "values ('synthetic','three','field_c')"
                )
                empty_keys = conn.execute(
                    "select count(*) from import_conflicts where conflict_key = ''"
                ).fetchone()[0]
                conn.commit()

            self.assertEqual(first, second)
            self.assertEqual(
                [(row[6], row[7]) for row in first],
                [("", "value_conflict"), ("", "value_conflict")],
            )
            self.assertIsNotNone(index)
            self.assertEqual(empty_keys, 3)
            self.assertIn(
                "where conflict_key <> ''",
                " ".join(index[0].lower().split()),
            )


if __name__ == "__main__":
    unittest.main()
