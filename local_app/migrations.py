"""有序迁移（Phase 3 迁移机制归一）：schema_migrations 版本表记账，启动时按序补跑未执行项。

登记规矩（铁律）：
- MIGRATIONS 列表只增不改不删；id 前缀单调递增，永不复用。
- 每条迁移必须幂等：存量库可能在旧机制（每次启动全量重跑 + app_settings 'migration.*'
  土法标记）下已应用过同等变更，首次记账时会原样再跑一遍，靠各函数自身的幂等检查兜底。
  旧土法标记保留在各函数体内当双保险，不迁移不清理。
- 结构变更（建表/加列）不进这里：建表走 schema.sql（executescript 幂等），
  存量补列走 db._COLUMN_MIGRATIONS。这里只放 数据/种子/权限 类一次性升级。
- 函数签名统一 fn(db_path)，自管连接与提交（与旧机制一致，纯搬家零改动）。

历史：这些函数原散在 auth.py（8 个 ensure_*/migrate_*，run_local 每次启动全量重跑）；
auth.py 留一行转发壳（几十个测试引用它们），本体归一到此。ensure_seed_admin 不属迁移
（首启账号供给，含打印初始凭据），留在 auth.py。
"""
from local_app.db import DEFAULT_DB_PATH, connect
from local_app.timeutil import now_str


# ---------- 迁移函数本体（自 auth.py 原样搬入，零逻辑改动） ----------

def ensure_seed_roles(db_path=DEFAULT_DB_PATH):
    """首启 roles 表空则写预置 5 角色 + 默认权限勾选(admin 不入表)。幂等；已种子化返回 False。"""
    from local_app.auth import DEFAULT_ROLE_PERMS, PRESET_ROLES
    with connect(db_path) as conn:
        if conn.execute("select count(*) from roles").fetchone()[0]:
            return False
        now = now_str()
        for key, name, sort in PRESET_ROLES:
            conn.execute(
                "insert into roles(role_key, name, is_system, sort, created_at, updated_at) "
                "values (?, ?, 1, ?, ?, ?)", (key, name, sort, now, now))
        for role, perms in DEFAULT_ROLE_PERMS.items():
            for pk in perms:
                conn.execute(
                    "insert into role_permissions(role_key, perm_key) values (?, ?)", (role, pk))
        conn.commit()
    return True


def ensure_preset_roles_present(db_path=DEFAULT_DB_PATH):
    """升级用:幂等补齐 PRESET_ROLES 里缺失的角色。已 seed 过的旧库新增的
    主任/收银员/客服/助理2 不会被 ensure_seed_roles 补(它只在空表时整体种)。
    只插缺失角色 + 其默认权限,**不动已存在角色**(用户可能已在矩阵里改过权限)。返回新增条数。"""
    from local_app.auth import DEFAULT_ROLE_PERMS, PRESET_ROLES
    with connect(db_path) as conn:
        existing = {r[0] for r in conn.execute("select role_key from roles").fetchall()}
        if not existing:
            return 0   # 空表交给 ensure_seed_roles 整体种
        now = now_str()
        added = 0
        for key, name, sort in PRESET_ROLES:
            if key in existing:
                continue
            conn.execute(
                "insert into roles(role_key, name, is_system, sort, created_at, updated_at) "
                "values (?, ?, 1, ?, ?, ?)", (key, name, sort, now, now))
            for pk in DEFAULT_ROLE_PERMS.get(key, set()):
                conn.execute(
                    "insert into role_permissions(role_key, perm_key) values (?, ?)", (key, pk))
            added += 1
        if added:
            conn.commit()
    return added


def migrate_technician_role(db_path=DEFAULT_DB_PATH):
    """一次性升级:产品决策去掉「技师」角色,技工功能并入 助理/前台。
    仅当旧库 roles 表还有 technician 行时才执行(天然幂等,跑过一次后不再动,
    也不会覆盖用户之后在权限矩阵里的自定义):
    ①technician 账号迁到 assistant;②员工岗位 技师→助理(role + 多岗位 roles,去重);
    ③删 technician 角色行及其权限勾选;④给已存在的 assistant/assistant2 补
    lab_order.manage/warehouse.view、reception 补 lab_order.manage(只插缺失)。
    返回是否执行了迁移。"""
    with connect(db_path) as conn:
        if not conn.execute("select 1 from roles where role_key='technician'").fetchone():
            return False
        conn.execute("update users set role='assistant' where role='technician'")
        for sid, roles in conn.execute(
                "select staff_id, roles from staff_members where roles like '%,技师,%'").fetchall():
            items, seen = [], set()
            for x in roles.strip(",").split(","):
                x = "助理" if x == "技师" else x
                if x and x not in seen:
                    seen.add(x)
                    items.append(x)
            conn.execute("update staff_members set roles=? where staff_id=?",
                         (("," + ",".join(items) + ",") if items else "", sid))
        conn.execute("update staff_members set role='助理' where role='技师'")
        conn.execute("delete from role_permissions where role_key='technician'")
        conn.execute("delete from roles where role_key='technician'")
        _grant_absorbed_lab_perms(conn)
        conn.commit()
    return True


# 技工功能并入后,存量角色要补的权限(只插缺失)
_ABSORBED_LAB_PERMS = (("assistant", "lab_order.manage"), ("assistant", "warehouse.view"),
                       ("assistant2", "lab_order.manage"), ("assistant2", "warehouse.view"),
                       ("reception", "lab_order.manage"))
_LAB_PERMS_MARK = "migration.502_lab_perms"

# 尾巴:产品决策助理功能加大——补 患者建档/编辑
_ASSISTANT_PATIENT_PERMS = (("assistant", "patient.create"), ("assistant", "patient.edit"),
                            ("assistant2", "patient.create"), ("assistant2", "patient.edit"))
_ASSISTANT_PATIENT_MARK = "migration.502_assistant_patient_perms"


def _grant_perms_if_missing(conn, grants):
    for role, pk in grants:
        if conn.execute("select 1 from roles where role_key=?", (role,)).fetchone() and \
           not conn.execute("select 1 from role_permissions where role_key=? and perm_key=?",
                            (role, pk)).fetchone():
            conn.execute("insert into role_permissions(role_key, perm_key) values (?, ?)",
                         (role, pk))


def _grant_absorbed_lab_perms(conn):
    _grant_perms_if_missing(conn, _ABSORBED_LAB_PERMS)


def _one_shot_grant(db_path, mark, grants):
    """一次性权限升级:app_settings 无标记则给现存角色补缺失权限并落标记;有标记跳过。
    只跑一次,不覆盖用户之后在权限矩阵里的删改。(旧土法标记,保留当双保险)"""
    with connect(db_path) as conn:
        if conn.execute("select 1 from app_settings where key=?", (mark,)).fetchone():
            return False
        _grant_perms_if_missing(conn, grants)
        conn.execute("insert into app_settings(key, value, updated_at) values (?, '{}', ?)",
                     (mark, now_str()))
        conn.commit()
    return True


def ensure_absorbed_lab_perms(db_path=DEFAULT_DB_PATH):
    """的存量权限补齐不能只挂在 migrate_technician_role 上——停在 前的
    旧 6 角色库(从没建过 technician)直升当前版本时,那条路径不会跑,助理/前台仍缺技工权限。
    改为独立一次性升级(见 _one_shot_grant)。"""
    return _one_shot_grant(db_path, _LAB_PERMS_MARK, _ABSORBED_LAB_PERMS)


def ensure_assistant_patient_perms(db_path=DEFAULT_DB_PATH):
    """尾巴:产品决策助理(含助理2)补 患者建档/编辑,存量库一次性升级。"""
    return _one_shot_grant(db_path, _ASSISTANT_PATIENT_MARK, _ASSISTANT_PATIENT_PERMS)


# 客户沟通(通话录音+沟通记录)并入后,存量库已存在的电话沟通主力角色不会被 ensure_seed_roles
# 补新权限(它只在空表整体种)。给目标角色一次性补 call/communication 权限,否则旧库升级后这些角色
# 看到「客户沟通」入口但接口 403。
_COMM_CALL_ROLES = ("doctor", "reception", "consultant", "director", "support")
_COMM_CALL_PERMS = tuple(
    (role, perm)
    for role in _COMM_CALL_ROLES
    for perm in ("call.view", "call.manage", "communication.view", "communication.manage")
)
_COMM_CALL_MARK = "migration.718_comm_call_perms"


def ensure_comm_call_perms(db_path=DEFAULT_DB_PATH):
    """客户沟通(通话录音+沟通记录)权限一次性补给存量库目标角色。"""
    return _one_shot_grant(db_path, _COMM_CALL_MARK, _COMM_CALL_PERMS)


_REPORT_ADMIN_ONLY_MARK = "migration.512_report_admin_only"


def ensure_report_perm_admin_only(db_path=DEFAULT_DB_PATH):
    """产品决策业绩/门诊报表只有院长(admin)可查——存量库一次性
    回收所有角色的 report.view 勾选(admin 本就代码层全通过,不受矩阵影响)。
    带标记只跑一次:用户之后想在权限矩阵给某角色单独授回,不会被再次回收。"""
    with connect(db_path) as conn:
        if conn.execute("select 1 from app_settings where key=?", (_REPORT_ADMIN_ONLY_MARK,)).fetchone():
            return False
        conn.execute("delete from role_permissions where perm_key='report.view'")
        conn.execute("insert into app_settings(key, value, updated_at) values (?, '{}', ?)",
                     (_REPORT_ADMIN_ONLY_MARK, now_str()))
        conn.commit()
    return True


def _seed_referral_sources(db_path=DEFAULT_DB_PATH):
    """患者来源三级树起始种子(空表才种,本地可在设置页继续维护)。本体在独立模块。"""
    from local_app.seed_referral_sources import seed_referral_sources
    return seed_referral_sources(db_path)


_REFUND_RESTORE_MARK = "migration.813_refund_restore_bill_amounts"


def restore_refunded_bill_amounts(db_path=DEFAULT_DB_PATH):
    """①:退款改为不冲减 bills.total_fee/paid_fee 后,把存量本地退款(payment_record_type='refund',
    仅本地退费写入)已冲减的金额一次性加回：total_fee += Σ退款、paid_fee += Σ退款——两边同加,
    欠款(total−优惠−paid)差值为 0,报表欠款不跳变；历史应收/实收恢复史实,新可退公式
    (paid_fee − Σ退款流水)对老单成立。

    ⚠️ 本迁移天然不幂等(重跑会翻倍加)：schema_migrations 记账 + app_settings 标记双保险锁死。

    ⚠️ 只回填"老口径"退款(源自旧代码,已冲减过账单)。新口径退款(①后)不冲减 total/paid,
    绝不能再加回。判据=退款流水 source_json 有无 refund_items 键:老口径退款没有这个键(它是新
    口径才写的),新口径退款才有。不靠"迁移先于服务"的调用顺序保证安全(改钱迁移不能依赖时序)。"""
    old_style = ("payment_record_type = 'refund' and "
                 "json_extract(iif(json_valid(source_json), source_json, '{}'), '$.refund_items') is null")
    with connect(db_path) as conn:
        if conn.execute("select 1 from app_settings where key=?", (_REFUND_RESTORE_MARK,)).fetchone():
            return False
        conn.execute(
            f"""
            update bills set
                total_fee = round(coalesce(total_fee, 0) + (
                    select coalesce(sum(-p.amount), 0) from payments p
                    where p.bill_id = bills.bill_id and {old_style.replace('source_json', 'p.source_json')}), 2),
                paid_fee = round(coalesce(paid_fee, 0) + (
                    select coalesce(sum(-p.amount), 0) from payments p
                    where p.bill_id = bills.bill_id and {old_style.replace('source_json', 'p.source_json')}), 2)
            where bill_id in (select distinct bill_id from payments p where {old_style.replace('source_json', 'p.source_json')}
                              and p.bill_id is not null)
            """
        )
        # 老口径退款(无 refund_items)把项目归属回填到 refunded_fee,补齐
        # Σ项目已退 = Σ退款流水(实退净额)的不变式。否则历史整单退(未落项目)之后再按项退,
        # 新退款按"项目未退"算封顶,报表里老退款按比例、新退款按精确,叠加后某类别净额为负。
        # 迁移时所有退款流水都是老口径,按各项剩余额比例分摊 Σ实退额(整数分,余数归占比最大项,
        # 不超各项剩余)——分摊的是实退金额(折价已含在流水里),折扣单也不虚增。
        for (bid,) in conn.execute(
            f"select distinct bill_id from payments where {old_style} and bill_id is not null").fetchall():
            target_c = round((conn.execute(
                f"select coalesce(sum(-amount),0) from payments where bill_id=? and {old_style}",
                (bid,)).fetchone()[0] or 0) * 100)
            items = conn.execute(
                "select treatment_item_id as iid, round(coalesce(total_fee,0),2) as line, "
                "round(coalesce(refunded_fee,0),2) as done from treatment_items "
                "where bill_id=? and is_deleted=0 order by treatment_item_id", (bid,)).fetchall()
            gap_c = target_c - round(sum(x["done"] for x in items) * 100)
            rem_c = [max(round((x["line"] - x["done"]) * 100), 0) for x in items]
            total_rem = sum(rem_c)
            if gap_c > 0 and total_rem > 0:   # 已按项归属(per-item 老退款)的单 gap<=0,跳过不动
                base = [gap_c * rc // total_rem for rc in rem_c]
                order = sorted(range(len(items)),
                               key=lambda i: (gap_c * rem_c[i]) % total_rem, reverse=True)
                left = gap_c - sum(base)
                for i in order:
                    if left <= 0:
                        break
                    if base[i] < rem_c[i]:
                        base[i] += 1
                        left -= 1
                for it, a in zip(items, base):
                    if a > 0:
                        conn.execute(
                            "update treatment_items set refunded_fee = round(coalesce(refunded_fee,0)+?,2) "
                            "where treatment_item_id=?", (a / 100, it["iid"]))
        # 已退清(state=refunded)的历史单:项目标 is_refund=1(闭合不变式;折扣单退满净额<行价,
        # threshold 到不了,靠这步标)。只标 is_refund,不改 refunded_fee(上面已按实退额回填)。
        conn.execute(
            """
            update treatment_items set is_refund = 1
            where is_deleted = 0 and coalesce(is_refund,0) = 0 and bill_id in (
                select bill_id from bills where state = 'refunded' and bill_id in (
                    select distinct bill_id from payments
                    where payment_record_type = 'refund' and bill_id is not null))
            """
        )
        conn.execute("insert into app_settings(key, value, updated_at) values (?, '{}', ?)",
                     (_REFUND_RESTORE_MARK, now_str()))
        conn.commit()
    return True


def ensure_v3_role_permission_matrix(db_path=DEFAULT_DB_PATH):
    """v3 空库首启：把「仍是旧 35 键默认集」的预置角色权限改写为 旧→新映射后的
    99 键集（含依赖闭包，复用生产迁移同款 oneoff/legacy_permission_mapping）。

    根因：v3 矩阵接口只收 99 新键并强校验依赖闭包,
    而 0001 种子写的是旧 35 键——lab_order.manage 无配套 lab_order.view,
    人员权限页 normalize 整页崩。生产库经一次性迁移显式赋新键免疫,空库全中招。

    红线：已是新键/被用户在矩阵里自定义过的角色(当前勾选 ≠ 旧默认集)一律不动;
    非 v3 库(users 无 is_system_admin,旧栈测试库)不动。幂等。返回改写角色数。"""
    from local_app.auth import DEFAULT_ROLE_PERMS
    from local_app.oneoff.legacy_permission_mapping import compute_new_permissions
    with connect(db_path) as conn:
        user_cols = {row[1] for row in conn.execute("pragma table_info(users)")}
        if "is_system_admin" not in user_cols:
            return 0
        changed = 0
        for role, legacy_defaults in DEFAULT_ROLE_PERMS.items():
            current = {
                row[0]
                for row in conn.execute(
                    "select perm_key from role_permissions where role_key = ?",
                    (role,),
                )
            }
            if not current or current != set(legacy_defaults):
                continue  # 空集/已自定义/已迁新键:不动
            new_keys = sorted(
                compute_new_permissions(role, frozenset(legacy_defaults))
            )
            conn.execute(
                "delete from role_permissions where role_key = ?", (role,)
            )
            conn.executemany(
                "insert into role_permissions(role_key, perm_key) values (?, ?)",
                [(role, key) for key in new_keys],
            )
            changed += 1
        if changed:
            conn.commit()
    return changed


# ---------- 登记册（只增不改不删；id 单调递增） ----------
MIGRATIONS = [
    ("0001_seed_roles", ensure_seed_roles),                       # 首启预置角色+默认权限
    ("0002_preset_roles_present", ensure_preset_roles_present),   # 补齐蓝本新增角色
    ("0003_technician_role_merge", migrate_technician_role),      # 去技师并入助理
    ("0004_absorbed_lab_perms", ensure_absorbed_lab_perms),       # 技工权限补齐
    ("0005_assistant_patient_perms", ensure_assistant_patient_perms),  # 尾巴
    ("0006_report_admin_only", ensure_report_perm_admin_only),    # 报表只院长
    ("0007_comm_call_perms", ensure_comm_call_perms),             # 沟通权限补齐
    ("0008_seed_referral_sources", _seed_referral_sources),       # 患者来源树种子
    ("0009_refund_restore_bill_amounts", restore_refunded_bill_amounts),  # ① 退款冲减史实回填
    ("0010_v3_role_perm_matrix", ensure_v3_role_permission_matrix),  # v3 空库角色矩阵新键化
]


def apply_migrations(db_path=DEFAULT_DB_PATH):
    """按序补跑未记账的迁移。返回本次执行的 id 列表。

    记账与执行分离：函数自管事务提交后才落 schema_migrations 行——迁移中途崩溃则
    不记账,下次启动重跑(各函数幂等,重跑安全)。"""
    with connect(db_path) as conn:
        conn.execute("create table if not exists schema_migrations("
                     "id text primary key, applied_at text not null)")
        conn.commit()
        done = {r[0] for r in conn.execute("select id from schema_migrations")}
    applied = []
    for mid, fn in MIGRATIONS:
        if mid in done:
            continue
        fn(db_path)
        with connect(db_path) as conn:
            conn.execute("insert into schema_migrations(id, applied_at) values (?, ?)",
                         (mid, now_str()))
            conn.commit()
        applied.append(mid)
    return applied
