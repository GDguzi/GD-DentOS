"""导入批次登记(sync_batches 表)公共工具。

核心(AI 病历摄入)与可选迁移扩展都要登记批次——真相源放核心，
迁移扩展向核心 import（单向依赖：扩展→核心）。
"""
import json

from local_app.timeutil import now_str


def ensure_batch(conn, batch_id, source):
    now = now_str()
    conn.execute(
        """
        insert into sync_batches(batch_id, source, status, started_at)
        values (?, ?, ?, ?)
        on conflict(batch_id) do update set
            source = excluded.source,
            status = excluded.status,
            started_at = excluded.started_at
        """,
        (batch_id, source, "running", now),
    )


def finish_batch(conn, batch_id, imported, skipped, summary=None):
    payload = summary if summary is not None else {"imported": imported, "skipped": skipped}
    conn.execute(
        """
        update sync_batches
        set status = ?, finished_at = ?, summary_json = ?
        where batch_id = ?
        """,
        (
            "done",
            now_str(),
            json.dumps(payload, ensure_ascii=False),
            batch_id,
        ),
    )
