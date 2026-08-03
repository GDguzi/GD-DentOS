import contextvars
import os
import re
import sqlite3
import uuid
from pathlib import Path

from local_app.timeutil import now_str


# 统一操作审计的「请求级共享 cell」(#437)。
# 中间件每个请求 set 一个 {"changed":False,"audited":False} dict;contextvar 复制到线程池端点时
# 只复制引用，改 dict 内容跨线程可见(故同步路由也能回传)。每个请求各自一个 cell=并发不互相污染,
# 替代原先全局 max(audit_id)/data_version 比较(并发下会被别的请求的 audit 写污染→漏审计)。
_audit_cell = contextvars.ContextVar("audit_cell", default=None)

# #438 只认「写入 audit_logs 表」这件结构化的事:insert/replace ... into audit_logs。
# 表名/列名总在任何用户值(VALUES 之后)之前出现,且 [^(]*? 不跨过列表 '(',
# 故业务字段里的 'audit_logs' 文本(在 VALUES 内)绝不会被误判。不能对整条展开后 SQL 做包含匹配。
_AUDIT_WRITE_RE = re.compile(
    r'^\s*(?:insert|replace)\b[^(]*?\binto\s+["\'`\[]?audit_logs\b', re.IGNORECASE)


def _audit_trace(sql):
    """每条 SQL 执行时回调:把「本请求是否改了库 / 是否写过 audit_logs」记到当前请求的 cell。
    只在请求上下文(cell 非空)内做事;后台/CLI/同步无 cell=零开销直接返回。"""
    cell = _audit_cell.get()
    if cell is None:
        return
    verb = sql.lstrip()[:7].lower()
    if verb.startswith(("insert", "update", "delete", "replac")):
        cell["changed"] = True
        if _AUDIT_WRITE_RE.match(sql):
            cell["audited"] = True


APP_DIR = Path(__file__).resolve().parent
SCHEMA_PATH = APP_DIR / "schema.sql"
# 默认连旧库 clinic.sqlite3；设环境变量 DENTAL_DB 可指向同步库 clinic_sync.sqlite3 等核对，旧库不动
DEFAULT_DB_PATH = Path(os.environ.get("DENTAL_DB") or (APP_DIR / "data" / "clinic.sqlite3"))

# 诊所名唯一真相源：存 app_settings key='clinic'；未配置时用中性默认(开源壳阶段1,不硬编码具体诊所)。
DEFAULT_CLINIC_NAME = "口腔门诊部"


def clinic_display_name(db_path=DEFAULT_DB_PATH):
    """读配置的诊所名(登录页/口令页等无会话场景用)。未配置或值坏 → 中性默认。"""
    import json as _json
    try:
        with connect(db_path) as conn:
            row = conn.execute("select value from app_settings where key = 'clinic'").fetchone()
        name = str(_json.loads(row["value"] or "{}").get("name") or "").strip() if row else ""
    except Exception:
        name = ""   # 库未初始化等极早期场景,登录页仍可渲染
    return name or DEFAULT_CLINIC_NAME


def new_id(prefix):
    """本地新建记录的统一 ID：'prefix-12位随机hex'。历史上同一写法散落各路由/各自定义
    new_id 三份，收敛到这里(格式一字不变)。[:10]/[:16] 等既有变体不归它管，保持原状。"""
    return f"{prefix}-{uuid.uuid4().hex[:12]}"


def audit_write(conn, entity_type, entity_id, action, *,
                old_json=None, new_json=None, operator, created_at=None,
                actor_user_id=None):
    """审计流水原始写入(数据层,仅 sqlite+timeutil,备份/恢复等独立工具可安全 import)。
    不 commit，事务边界归调用方。请求上下文里请用 auth.audit_write(自动填当前登录人)；
    operator 必填无默认(#746:漏传必须 TypeError,不许静默记成 local_user),
    后台工具显式给(如 'auto-sync')。"""
    created_at = created_at or now_str()
    if actor_user_id is None:
        conn.execute(
            "insert into audit_logs(entity_type, entity_id, action, old_json, "
            "new_json, operator, created_at) values (?, ?, ?, ?, ?, ?, ?)",
            (
                entity_type,
                entity_id,
                action,
                old_json,
                new_json,
                operator,
                created_at,
            ),
        )
    else:
        conn.execute(
            "insert into audit_logs(entity_type, entity_id, action, old_json, "
            "new_json, operator, created_at, actor_user_id) "
            "values (?, ?, ?, ?, ?, ?, ?, ?)",
            (
                entity_type,
                entity_id,
                action,
                old_json,
                new_json,
                operator,
                created_at,
                actor_user_id,
            ),
        )


# 撤单/作废账单状态：SaaS 撤单码(900收费方式错/500患者原因/400金额错/200改方案) + 本地 voided。
# 这些单 SaaS 自身藏起来、不算钱；本地同样不计入合计/欠费/应收。退费冲减(state 300 负数)、已结小单(0)仍有效。
VOIDED_BILL_STATES = ("900", "500", "400", "200", "voided")


def active_bill_clause(col="state"):
    """有效账单 WHERE 片段(排除撤单/作废)。col 可传带表别名的列名,如 'b.state'。无用户输入,安全拼接。"""
    vals = ", ".join("'%s'" % s for s in VOIDED_BILL_STATES)
    return "coalesce(%s, '') not in (%s)" % (col, vals)


def bill_discount_sql(prefix=""):
    """账单优惠额 SQL：SaaS 取 source_json.DiscountFee；本地自建单无此字段=0(折扣已并入 total_fee)。
    prefix 如 'b.' 用于带表别名查询。
    ⚠️ DiscountFee 是字符串,SaaS 大额优惠带千分位逗号(如 "1,400.00")。SQLite 把字符串当数值时
    读到逗号即停→"1,400.00" 被当成 1,导致优惠≥1000 的单凭空冒欠费。故先 replace 去逗号再参与运算。"""
    return ("coalesce(replace(json_extract(iif(json_valid(%(p)ssource_json), %(p)ssource_json, '{}'), "
            "'$.DiscountFee'), ',', ''), 0)") % {"p": prefix}


def bill_net_arrears_sql(prefix=""):
    """净欠费 = 总费用 - 优惠 - 已收(对齐 SaaS：应收=总价-优惠)。SaaS 打折单不扣优惠会冒幽灵欠费。"""
    return "round(%(p)stotal_fee - %(d)s - %(p)spaid_fee, 2)" % {
        "p": prefix, "d": bill_discount_sql(prefix)}


def connect(db_path=DEFAULT_DB_PATH):
    db_path = Path(db_path)
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    conn.execute("pragma foreign_keys = on")
    # 并发写防护(局域网多设备 iPad/手机签字 + 后台同步并发写同一库)：
    # busy_timeout 让撞锁的第二个写者排队等待而非立刻报 database is locked；
    # WAL 允许读写并发、显著降低锁冲突(库级持久属性,每连接幂等设置)。
    conn.execute("pragma busy_timeout = 5000")
    conn.execute("pragma journal_mode = wal")
    conn.set_trace_callback(_audit_trace)   # #437 请求级审计标记(无请求上下文时回调即时返回,零开销)
    return conn


def begin_immediate(conn):
    """改库存/改钱前抢写锁，把同一行的读-改-写串行化（杜绝双确认/丢更新/库存翻倍）。
    必须在守卫读之前调用：第二个写者会阻塞在此(busy_timeout 内排队)直到第一个 commit，
    醒来后重新读到已确认状态而触发 409，不会重复应用。范本见 routes/membership.py 的 _lock。
    isolation_level=None 切 autocommit 以手动控事务；连接出 with 块时异常会 rollback 释放锁。"""
    conn.isolation_level = None
    conn.execute("begin immediate")


def _apply_schema_diff(conn):
    """存量库自动补列(Phase3 第二刀):以 schema.sql 为唯一真相源——在内存库跑一遍
    schema.sql 得到目标结构,逐表对比活库 pragma,缺列即按目标列的 类型/NOT NULL/默认值
    重建 ALTER TABLE ADD COLUMN。取代原 _COLUMN_MIGRATIONS 手工字典(103条,与 schema.sql
    双写,谁忘了另一半就漂移;切换前已对账零漂移)。

    只加列,绝不删/改/重排(存量数据安全)。活库多出的历史列忽略。
    红线:给已有表新增列时,schema.sql 里该列的默认值必须是常量——SQLite 对**非空表**的
    ADD COLUMN 拒绝 current_timestamp 等非常量默认(空表放行,实测 3.53;存量业务表必然
    非空,违者启动即炸,test_migrations 红线测试兜底);需要非常量默认的新列请配常量默认
    + 应用层赋值,或走 migrations.py 有序迁移。"""
    ref = sqlite3.connect(":memory:")
    try:
        ref.executescript(SCHEMA_PATH.read_text(encoding="utf-8"))
        live_tables = {row[0] for row in conn.execute(
            "select name from sqlite_master where type='table'")}
        for (table,) in ref.execute(
                "select name from sqlite_master where type='table' "
                "and name not like 'sqlite_%'").fetchall():
            if table not in live_tables:
                continue  # 活库没这张表:executescript 的 create table 会整表新建(自带全列)
            qt = table.replace('"', '""')   # #789:标识符内双引号转义(当前schema无,防御性)
            live_cols = {row[1] for row in conn.execute(f'pragma table_info("{qt}")')}
            for _cid, name, ctype, notnull, dflt, _pk in ref.execute(
                    f'pragma table_info("{qt}")').fetchall():
                if name in live_cols:
                    continue
                qn = name.replace('"', '""')
                ddl = ctype or ""
                if notnull:
                    ddl += " not null"
                if dflt is not None:
                    ddl += f" default {dflt}"
                conn.execute(f'alter table "{qt}" add column "{qn}" {ddl}')
    finally:
        ref.close()


def init_db(db_path=DEFAULT_DB_PATH):
    db_path = Path(db_path)

    # 显式关连接：with connect() 只提交不关闭,留悬挂连接会卡住备份回滚的整库替换
    conn = connect(db_path)
    try:
        # 先给存量表补列：schema.sql 里依赖新列的索引(如 consent_documents.order_id)才不会建失败。
        # 单趟即可:差分以 schema.sql 为唯一真相源,executescript 新建的表天然带全列
        # (旧机制跑两趟是因为手工字典可能含 schema.sql 没有的列,双写消灭后不存在此况)。
        _apply_schema_diff(conn)
        conn.executescript(SCHEMA_PATH.read_text(encoding="utf-8"))
        conn.commit()
    finally:
        conn.close()

    return db_path
