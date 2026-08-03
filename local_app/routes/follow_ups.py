"""今日待跟进(派生)：本地无独立跟进任务表，纯只读派生——
"今天需要跟进的人" = 回访计划**今天到期**(未回访)的患者，按累计消费(payments)分
高/中/普通三档。只取当天到期，不含过去欠的回访(那是积压，不是今日跟进)；
待缴费另有官方"今日待缴费"菜单，不混进跟进。"""
from fastapi import APIRouter

from local_app.auth import require_perm
from local_app.db import connect
from local_app.validation import valid_date_param

VALUE_HIGH = 10000.0   # 累计消费 ≥1万 = 高价值
VALUE_MID = 3000.0     # ≥3千 = 中等价值；其余普通


def create_follow_ups_router(db_path):
    router = APIRouter()

    @router.get("/api/today-follow-ups")
    def today_follow_ups(date: str = "", assignee: str = "", q: str = ""):
        require_perm("patient.view")   # #494：含患者姓名/电话/消费分层,按 patient.view 守卫
        date = (date or "").strip()
        if not date:
            return {"groups": {"high": [], "mid": [], "normal": []},
                    "counts": {"high": 0, "mid": 0, "normal": 0}, "total": 0}
        valid_date_param(date, "date")
        assignee = (assignee or "").strip()
        q = (q or "").strip()

        with connect(db_path) as conn:
            # 回访计划"今天到期"(未回访)：due_time 当天、actual_date 空、未删除
            due = {}
            for r in conn.execute(
                "select patient_identity, coalesce(item_name,'') as item, "
                "coalesce(return_doctor,'') as rd from return_visits "
                "where coalesce(is_deleted,0)=0 and coalesce(actual_date,'')='' "
                "and substr(coalesce(due_time,''),1,10) = ?",
                (date,),
            ):
                pid = r["patient_identity"]
                d = due.setdefault(pid, {"count": 0, "doctor": "", "sample": ""})
                d["count"] += 1
                if not d["doctor"] and r["rd"]:
                    d["doctor"] = r["rd"]
                if not d["sample"]:
                    first = r["item"].split("\\n")[0].split("\n")[0].split("\\t")[0].strip()
                    d["sample"] = first[:16]

            pids = set(due)
            if not pids:
                return {"groups": {"high": [], "mid": [], "normal": []},
                        "counts": {"high": 0, "mid": 0, "normal": 0}, "total": 0}

            ph = ",".join("?" * len(pids))
            params = tuple(pids)
            # 累计消费口径与 today_cash_in/收银/经营概览一致(#276):排作废原单,保留退费负数冲减
            value = {}
            for r in conn.execute(
                f"select patient_identity, round(sum(amount), 2) as v from payments "
                f"where (coalesce(json_extract(iif(json_valid(source_json), source_json, '{{}}'), '$.CancelMark'), '') = '' "
                f"       or coalesce(amount, 0) < 0) and patient_identity in ({ph}) "
                f"group by patient_identity", params):
                value[r["patient_identity"]] = float(r["v"] or 0)
            info = {}
            for r in conn.execute(
                f"select patient_identity, coalesce(display_name,'') as name, coalesce(phone,'') as phone, "
                f"coalesce(responsible_doctor,'') as doctor from patients where patient_identity in ({ph})", params):
                info[r["patient_identity"]] = r

            rows = []
            for pid in pids:
                pinfo = info.get(pid)
                if not pinfo:
                    continue
                d = due[pid]
                detail = d["sample"] or "回访"
                if d["count"] > 1:
                    detail += f" 等{d['count']}条"
                # 计划跟进人：回访医生优先，否则主治医生
                who = d["doctor"] or pinfo["doctor"]
                if assignee and who != assignee:
                    continue
                if q and (q not in pinfo["name"] and q not in pinfo["phone"]):
                    continue
                v = value.get(pid, 0.0)
                tier = "high" if v >= VALUE_HIGH else ("mid" if v >= VALUE_MID else "normal")
                rows.append({"patient_identity": pid, "name": pinfo["name"], "phone": pinfo["phone"],
                             "value": round(v, 2), "tier": tier, "assignee": who,
                             "reasons": [{"type": "回访到期", "detail": detail}]})

            rows.sort(key=lambda x: (-x["value"], x["name"]))
            groups = {"high": [], "mid": [], "normal": []}
            for row in rows:
                groups[row["tier"]].append(row)
            return {"groups": groups,
                    "counts": {k: len(v) for k, v in groups.items()},
                    "total": len(rows)}

    return router
