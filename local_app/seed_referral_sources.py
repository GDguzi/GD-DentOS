"""患者来源三级树的起始种子(对齐官方"患者来源设置"页)。
幂等：表非空则跳过。本地可在设置页继续增删改。"""
import uuid

from local_app.db import connect

# 一级 -> { 二级 -> [三级...] }。通用中性示例：各店在设置页按自己情况增删改。
SEED_TREE = {
    "网络咨询": {},
    "朋友介绍": {
        "老患者介绍": [],
        "员工介绍": [],
        "门诊": [],
    },
    "家住附近": {},
    "诊所网站": {},
    "线上平台": {
        "美团": [],
        "大众点评": [],
    },
    "路过": {},
    "地图": {},
    "其他": {},
}


def _sid():
    return "rs-" + uuid.uuid4().hex[:10]


def seed_referral_sources(db_path):
    """空表才种。返回新增条数。"""
    with connect(db_path) as conn:
        if conn.execute("select count(*) from referral_sources").fetchone()[0]:
            return 0
        added = 0

        def ins(parent, level, name, order):
            sid = _sid()
            conn.execute(
                "insert into referral_sources(source_id, parent_id, level, name, display_order) "
                "values (?, ?, ?, ?, ?)",
                (sid, parent, level, name, order),
            )
            return sid

        for i, (l1, children) in enumerate(SEED_TREE.items()):
            id1 = ins("", 1, l1, i)
            added += 1
            for j, (l2, grand) in enumerate(children.items()):
                id2 = ins(id1, 2, l2, j)
                added += 1
                for k, l3 in enumerate(grand):
                    ins(id2, 3, l3, k)
                    added += 1
        conn.commit()
        return added
