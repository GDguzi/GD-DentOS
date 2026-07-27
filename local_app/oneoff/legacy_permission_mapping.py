"""旧 35 权限键 → 新 99 权限键迁移映射：数据常量 + 纯函数，不触碰任何数据库。

映射规则：
- 3.1 同名直留 11 键（SAME_NAME_KEEP）。
- 3.2 一对多展开 21 键：19 键无条件展开（EXPANSION_MAP）+ report.view/data.export
  按 D3+D11 报表门禁（REPORT_GATED_LEGACY_KEYS + ROLE_BLANKET_GRANTS）。
- 3.3 移除 3 键（REMOVED_LEGACY_KEYS，D6 确认：迁往系统管理员专属能力，不可分配给角色）。
- D2 删除类 6 键 + D4 审核/停用/删除 4 键 blanket 授予 director；
  D9 medical_record.ai.review blanket 授予 doctor/director（ROLE_BLANKET_GRANTS）。
- D1/3.4 派生：medical_record.view→+treatment_plan.view、
  medical_record.edit→+medical_record.template.manage（DERIVED_BY_LEGACY_KEY）。
- D7/D8 派生：迁移后新集含 patient.profile.view 的角色补
  patient.group.manage + master_data.view（RESULT_DERIVED_GRANTS）。

铁律：fail-closed，目录外脏键直接报错，不静默丢弃；禁止 import local_app.migration_layer。
"""

from __future__ import annotations

from local_app.access_policy import (
    ALL_PERMISSIONS,
    NON_ASSIGNABLE_PERMISSIONS,
    PERMISSION_DEPENDENCIES,
)


class LegacyPermissionMappingError(ValueError):
    """映射输入不合法（含既不在旧 35 键也不在新 99 目录的脏键）；fail-closed。"""


# 旧 35 键目录（与 local_app/auth.py PERMISSION_DEFS 一致；自存一份快照，
# 不 import 旧运行时，测试锁死两边同步）。
LEGACY_PERMISSIONS = frozenset({
    "patient.view",
    "patient.create",
    "patient.edit",
    "patient.delete",
    "patient.export",
    "medical_record.view",
    "medical_record.edit",
    "treatment.manage",
    "lab_order.manage",
    "surgery.manage",
    "oral_exam.manage",
    "consult.manage",
    "consent.manage",
    "call.view",
    "call.manage",
    "communication.view",
    "communication.manage",
    "billing.view",
    "billing.pay",
    "billing.refund",
    "membership.manage",
    "report.view",
    "data.export",
    "warehouse.view",
    "warehouse.manage",
    "sterilize.manage",
    "master_data.manage",
    "staff.manage",
    "settings.manage",
    "user.manage",
    "role.manage",
    "audit.view",
    "backup.manage",
    "sync.manage",
    "recycle.manage",
})

# 3.1 同名直留（11 键，无决策）。
SAME_NAME_KEEP = frozenset({
    "audit.view",
    "billing.view",
    "communication.view",
    "communication.manage",
    "consent.manage",
    "lab_order.manage",
    "master_data.manage",
    "medical_record.view",
    "medical_record.edit",
    "oral_exam.manage",
    "surgery.manage",
})

# 3.2 一对多展开（19 键无条件部分；report.view/data.export 见 REPORT_GATED_LEGACY_KEYS）。
EXPANSION_MAP = {
    "patient.view": frozenset({
        "patient.profile.view",
        "patient.image.view",
        "appointment.view",
        "return_visit.view",
    }),
    "patient.create": frozenset({"patient.profile.create"}),
    # D2：patient.image.delete/appointment.delete 不随本键派发，仅主任授予。
    "patient.edit": frozenset({
        "patient.profile.edit",
        "patient.tag.manage",
        "patient.image.manage",
        "appointment.create",
        "appointment.edit",
        "appointment.status",
        "appointment.cancel",
        "return_visit.manage",
    }),
    "patient.delete": frozenset({
        "patient.profile.merge",
        "recycle.view",
        "recycle.restore",
    }),
    "patient.export": frozenset({"patient.profile.export"}),
    # D1：治疗计划写路由现网全部由 treatment.manage 把门。
    "treatment.manage": frozenset({
        "treatment_order.view",
        "treatment_order.manage",
        "treatment_order.price",
        "treatment_order.void",
        "treatment_plan.view",
        "treatment_plan.manage",
        "treatment_plan.status",
        "treatment_plan.bill",
        "treatment_plan.delete",
    }),
    "consult.manage": frozenset({"consultation.view", "consultation.manage"}),
    "call.view": frozenset({"call_record.view"}),
    # D2：call_record.delete 不随本键派发，仅主任授予。
    "call.manage": frozenset({"call_record.manage"}),
    "billing.pay": frozenset({"payment.collect", "payment_method.manage"}),
    "billing.refund": frozenset({"payment.refund"}),
    "membership.manage": frozenset({
        "membership.view",
        "membership.topup",
        "membership.consume",
        "membership.refund",
    }),
    "warehouse.view": frozenset({
        "warehouse.inventory.view",
        "warehouse.item.view",
        "warehouse.supplier.view",
        "warehouse.stock_in.view",
        "warehouse.stock_out.view",
        "warehouse.stocktake.view",
    }),
    "warehouse.manage": frozenset({
        "warehouse.item.manage",
        "warehouse.supplier.manage",
        "warehouse.stock_in.manage",
        "warehouse.stock_in.confirm",
        "warehouse.stock_out.manage",
        "warehouse.stock_out.confirm",
        "warehouse.stocktake.manage",
        "warehouse.stocktake.confirm",
    }),
    # D4：instrument.disable/delete、sterilization_order.audit/cancel_audit 仅主任授予。
    "sterilize.manage": frozenset({
        "instrument.view",
        "instrument.manage",
        "sterilization_order.view",
        "sterilization_order.manage",
        "sterilization_order.submit",
    }),
    # D5：账号部分归 user.manage→account.*，不并入。
    "staff.manage": frozenset({"staff.view", "staff.edit", "staff.employment"}),
    "settings.manage": frozenset({"clinic_settings.manage"}),
    "user.manage": frozenset({"account.view", "account.open", "account.security"}),
    "recycle.manage": frozenset({"recycle.view", "recycle.restore"}),
}

# D3+D11 拍板：报表键仅主任/收银员/前台经 ROLE_BLANKET_GRANTS 获得（与旧键持有无关）；
# 其余角色即使持有 report.view/data.export 也按 收紧丢弃，不派生。
REPORT_GATED_LEGACY_KEYS = frozenset({"report.view", "data.export"})

# 3.3 移除（D6 确认）：三键迁往系统管理员专属能力，不可分配给角色，迁移时从各角色键集删除。
REMOVED_LEGACY_KEYS = frozenset({"role.manage", "backup.manage", "sync.manage"})

# D2/D4/D9 blanket 授予：与角色持有哪些旧键无关，按角色键直接补齐。
ROLE_BLANKET_GRANTS = {
    "director": frozenset({
        # D2 删除类 6 键
        "patient.image.delete",
        "appointment.delete",
        "call_record.delete",
        "communication.delete",
        "lab_order.delete",
        "return_visit.delete",
        # D4 审核/停用/删除 4 键
        "instrument.disable",
        "instrument.delete",
        "sterilization_order.audit",
        "sterilization_order.cancel_audit",
        # D9
        "medical_record.ai.review",
        # D3+D11 报表包：财务/绩效/运营
        "report.finance.view",
        "report.finance.export",
        "report.performance.view",
        "report.operations.view",
        "report.operations.export",
    }),
    # D9：仅医生、主任角色。
    "doctor": frozenset({"medical_record.ai.review"}),
    # D3+D11 报表包：欠款/收银
    "cashier": frozenset({
        "report.arrears.view",
        "report.cashier.view",
        "report.cashier.export",
    }),
    "reception": frozenset({
        "report.arrears.view",
        "report.cashier.view",
        "report.cashier.export",
    }),
}

# D1/3.4 派生：旧键触发（对纯旧键集与新旧混合键集同样生效）。
DERIVED_BY_LEGACY_KEY = {
    # 治疗计划读路由现网由 medical_record.view 把门。
    "medical_record.view": frozenset({"treatment_plan.view"}),
    # 病历模板管理现网由 medical_record.edit 把门。
    "medical_record.edit": frozenset({"medical_record.template.manage"}),
    # D10：lab_order.manage/delete 依赖 lab_order.view，补派生维持依赖闭包。
    "lab_order.manage": frozenset({"lab_order.view"}),
}

# D7/D8 派生：迁移后新集含触发键即补授予。
RESULT_DERIVED_GRANTS = {
    "patient.profile.view": frozenset({"patient.group.manage", "master_data.view"}),
}


def dependency_closure(keys):
    """PERMISSION_DEPENDENCIES 传递闭包（排除不可分配键）。

    拍板：映射/拍板/blanket/派生生成集合后，必须统一补齐依赖闭包，
    使任何输入键集的结果都能通过新 GUI 保存校验（_validate_dependencies）。
    """
    result = set(keys)
    queue = list(result)
    while queue:
        key = queue.pop()
        for dep in PERMISSION_DEPENDENCIES.get(key, ()):
            if (
                dep not in result
                and dep in ALL_PERMISSIONS
                and dep not in NON_ASSIGNABLE_PERMISSIONS
            ):
                result.add(dep)
                queue.append(dep)
    return result


def compute_new_permissions(role_key, old_keys, *, with_closure=True):
    """把某角色的旧键集映射为迁移后新键集（frozenset[str]，⊂ ALL_PERMISSIONS）。

    - 已是 99 目录内的新键（新旧混合键集）原样保留。
    - 旧 35 键按映射表展开/直留/删除，再叠加角色 blanket 与派生规则。
    - with_closure=True（默认）时统一补齐 PERMISSION_DEPENDENCIES 传递闭包
      （排除不可分配键），保证结果可被新 GUI 保存；False 仅用于报告拆分展示。
    - 既不在旧 35 键也不在新 99 目录的脏键：fail-closed 抛 LegacyPermissionMappingError。
    """
    if type(role_key) is not str or not role_key.strip():
        raise LegacyPermissionMappingError("role_key 必须是非空白字符串")
    try:
        old_set = frozenset(old_keys)
    except TypeError:
        raise LegacyPermissionMappingError("old_keys 必须是可迭代的权限键集合") from None
    for key in old_set:
        if type(key) is not str:
            raise LegacyPermissionMappingError(f"权限键必须是字符串: {key!r}")

    unknown = old_set - LEGACY_PERMISSIONS - ALL_PERMISSIONS
    if unknown:
        raise LegacyPermissionMappingError(
            f"角色 {role_key} 存在目录外权限键: {', '.join(sorted(unknown))}"
        )

    new_set = set()
    for key in old_set:
        if key in ALL_PERMISSIONS:
            new_set.add(key)  # 同名直留 11 键与混合键集中的新键，都走这里。
        elif key in EXPANSION_MAP:
            new_set.update(EXPANSION_MAP[key])
        elif key in REPORT_GATED_LEGACY_KEYS:
            continue  # D3+D11：报表键只经 ROLE_BLANKET_GRANTS 发放，其余角色收紧丢弃。
        elif key in REMOVED_LEGACY_KEYS:
            continue  # D6 移除，不派生。
        else:  # pragma: no cover —— 旧 35 键完备覆盖由测试锁死，此处不可达。
            raise LegacyPermissionMappingError(f"旧键未被映射表覆盖: {key}")

    new_set.update(ROLE_BLANKET_GRANTS.get(role_key, frozenset()))
    for trigger, grants in DERIVED_BY_LEGACY_KEY.items():
        if trigger in old_set:
            new_set.update(grants)
    for trigger, grants in RESULT_DERIVED_GRANTS.items():
        if trigger in new_set:
            new_set.update(grants)
    if with_closure:
        new_set = dependency_closure(new_set)

    result = frozenset(new_set)
    if not result <= ALL_PERMISSIONS:  # pragma: no cover —— 映射目标 ∈ 目录由测试锁死。
        raise LegacyPermissionMappingError("映射结果越出 99 键目录")
    return result


def find_unknown_keys(all_role_keys):
    """逐角色找出既不在旧 35 键也不在新 99 目录的脏键。

    输入 {role_key: 键集}，返回 {role_key: set(脏键)}（只含脏键角色）；
    调用方据此 fail-closed（apply 整单拒绝），绝不静默丢弃。
    """
    unknown_by_role = {}
    for role_key, keys in all_role_keys.items():
        unknown = frozenset(keys) - LEGACY_PERMISSIONS - ALL_PERMISSIONS
        if unknown:
            unknown_by_role[role_key] = set(unknown)
    return unknown_by_role
