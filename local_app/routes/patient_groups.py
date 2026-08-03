"""患者分类：按本地列 patient_group 统计患者分组（手动分组）。

只读端点，只产出「分组名 + 患者计数」，绝不输出任何患者明细。分组是
诊所自定义的标签（种植牙/正畸患者/年卡办理…），非患者隐私字段；多分组
用逗号拼在一个字段里（如「最近患者,种植牙」），按逗号拆开逐组计数。
开源壳阶段2：真相源=patient_group 列(同步建档/回填已落列)，不再读 source_json。
"""
from fastapi import APIRouter

from local_app.auth import require_perm
from local_app.db import connect

# 原系统把"最近患者"当成系统分组混在 groupname 里，单独拎出来当"最近患者"统计，
# 不混进"手动分组"列表。
RECENT_GROUP = "最近患者"


def create_patient_groups_router(db_path):
    router = APIRouter()

    @router.get("/api/patient-groups")
    def patient_groups():
        require_perm("patient.view")   # #482：分组名虽非明细,仍属患者域,按 patient.view 守卫
        counts = {}
        recent = 0
        with connect(db_path) as conn:
            total = conn.execute("select count(*) from patients where coalesce(is_deleted,0)=0").fetchone()[0]
            cur = conn.execute(
                "select patient_group from patients "
                "where coalesce(patient_group, '') != '' and coalesce(is_deleted,0)=0"
            )
            for (groupname,) in cur:
                if not groupname:
                    continue
                names = [n.strip() for n in str(groupname).split(",") if n.strip()]
                if RECENT_GROUP in names:
                    recent += 1
                for name in names:
                    if name == RECENT_GROUP:
                        continue
                    counts[name] = counts.get(name, 0) + 1
        groups = sorted(
            ({"name": k, "count": v} for k, v in counts.items()),
            key=lambda g: (-g["count"], g["name"]),
        )
        # 公海患者：原系统里指"无归属业务员"的患者，是 CRM 归属概念，本地 source_json
        # 没有对应字段，暂返回 0（与诊所现状一致）；待确认数据口径后再补，留 future。
        return {
            "total": total,
            "recent": recent,
            "public_sea": 0,
            "groups": groups,
        }

    return router
