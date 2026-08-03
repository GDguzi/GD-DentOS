from dataclasses import dataclass
from typing import Any, Dict, Optional


@dataclass(frozen=True)
class PatientSnapshot:
    patient_identity: str
    source_customer_id: Optional[str]
    display_name: Optional[str]
    phone: Optional[str]
    source: Dict[str, Any]
    updated_at: Optional[str] = None
    sex: Optional[str] = None
    birthday: Optional[str] = None
    address: Optional[str] = None


# 就诊状态流：待确认0 → 预约确认1 → 预约到达2 → 预约分诊3 → 完成治疗4 → 患者离开5；
# 已取消=-1。历史值归一(SaaS同步来的数字 0/1/2/3、中文 已预约/已到诊/已完成/已取消)。
def appt_stage(status):
    s = str(status or "").strip()
    if s in ("3", "已取消", "已爽约", "爽约"):   # 爽约与取消同样排除出候诊漏斗(P1-2)
        return -1
    if s in ("已离开", "患者离开"):
        return 5
    if s in ("2", "完成", "已完成", "完成治疗"):
        return 4
    if s in ("已分诊",):
        return 3
    if s in ("1", "已到诊", "已到达", "预约到达"):
        return 2
    if s in ("已确认", "预约确认"):
        return 1
    return 0  # 待确认 / 已预约 / 0 / 空
