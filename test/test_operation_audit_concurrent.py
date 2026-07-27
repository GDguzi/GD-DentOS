"""并发回归:两个非自审计的改数据请求并发时,中间件兜底审计必须各记一条,
不能因为共享全局 max(audit_id) 被对方的 audit 写污染而漏掉。

旧实现(全局 max(audit_id) 增量去重)在此场景下会漏掉后完成的那条;新实现给每个请求一个独立
cell(db._audit_trace 写回),不再读全局计数器,故并发安全。本测试用 asyncio 精确编排出
「A 全程跑完(含兜底写)后 B 才读判定」的交错,确定性复现旧 bug、锁住新修复。"""
import asyncio
import tempfile
import unittest
from pathlib import Path

from local_app.auth import OperationAuditMiddleware
from local_app.db import connect, init_db


def _make_inner(db, gates):
    """极简 ASGI 内层:写一行业务数据(非 audit),等本请求的闸门放行后再返回 200。
    业务写 → db._audit_trace 给本请求 cell 标 changed=True、audited=False。"""
    async def app(scope, receive, send):
        name = scope["path"].rsplit("/", 1)[-1]
        with connect(db) as c:
            c.execute("insert into _probe(tag) values (?)", (name,))
            c.commit()
        await gates[name].wait()
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b"ok"})
    return app


class ConcurrentFallbackAuditTest(unittest.TestCase):
    def test_two_concurrent_mutations_each_get_fallback_audit(self):
        async def run():
            with tempfile.TemporaryDirectory() as tmp:
                db = Path(tmp) / "clinic.sqlite3"
                init_db(db)
                with connect(db) as c:
                    c.execute("create table _probe(id integer primary key, tag text)")
                    c.commit()

                gates = {"a": asyncio.Event(), "b": asyncio.Event()}
                mw = OperationAuditMiddleware(_make_inner(db, gates), db)

                async def call(p):
                    scope = {"type": "http", "method": "POST", "path": p, "headers": []}
                    await mw(scope, _noop_receive, _noop_send)

                ta = asyncio.create_task(call("/api/probe/a"))
                tb = asyncio.create_task(call("/api/probe/b"))
                await asyncio.sleep(0.05)        # 两请求都进到各自闸门前(业务已写,均未返回)
                gates["a"].set(); await ta       # A 走完:含中间件兜底写 audit
                gates["b"].set(); await tb       # B 再走完:旧实现会因 A 的 audit 误判已自审计而漏

                with connect(db) as c:
                    acts = sorted(
                        r[0] for r in c.execute(
                            "select entity_id from audit_logs where action='post_probe'"))
                self.assertEqual(acts, ["a", "b"], "并发两请求都应各留一条兜底审计")

        asyncio.run(run())


async def _noop_receive():
    return {"type": "http.request", "body": b"", "more_body": False}


async def _noop_send(message):
    return None


if __name__ == "__main__":
    unittest.main()
