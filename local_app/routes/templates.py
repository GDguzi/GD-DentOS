import json
from pathlib import Path

from fastapi import APIRouter, HTTPException
from local_app.timeutil import now_str
from local_app.auth import require_perm

from local_app.db import connect, init_db
from local_app.snapshots import safe_json_object
from local_app.versioning import stable_hash


UNCATEGORIZED = "未分类"


def _display_category(category):
    """#11：库里有些分类名是裸数字ID（外部导入的目录节点缺名/被软删时，兜底存了原始数字标识）。
    展示层把"空 或 纯数字"的分类归到「未分类」桶，不露乱码数字；
    库里原始值保留不动，将来拿到目录名仍可还原。"""
    text = str(category or "").strip()
    if not text or text.isdigit():
        return UNCATEGORIZED
    return text


def _clean_content(content):
    """只保留非空科目（字符串），键排序后 dump，与导入模板同口径。"""
    if not isinstance(content, dict):
        return {}
    cleaned = {}
    for key, value in content.items():
        text = str(value or "").strip()
        if text:
            cleaned[str(key)] = text
    return cleaned


def create_templates_router(db_path):
    router = APIRouter()
    db_path = Path(db_path)

    @router.get("/api/medical-templates")
    def medical_templates():
        require_perm("medical_record.view")   # 越权:读患者/病历数据
        with connect(db_path) as conn:
            rows = conn.execute(
                """
                select template_id, template_name, category, content_json
                from medical_templates
                where active = 1
                order by category, template_name
                """
            ).fetchall()

        categories = []
        index_by_category = {}
        for row in rows:
            category = _display_category(row["category"])
            group = index_by_category.get(category)
            if group is None:
                group = {"category": category, "templates": []}
                index_by_category[category] = group
                categories.append(group)
            group["templates"].append(
                {
                    "template_id": row["template_id"],
                    "template_name": row["template_name"],
                    "content_json": safe_json_object(row["content_json"]),
                }
            )

        return {"categories": categories}

    @router.post("/api/medical-templates")
    def save_as_template(payload: dict):
        """另存为模板：把当前病历科目存成新模板。

        同 名+分类 用确定性 template_id 幂等 upsert，重复另存只更新不堆重复。
        本地新建模板 id 带 local-tmpl- 前缀、source_ids='local'，与同步来的官方模板区分。
        """
        require_perm("medical_record.edit")   # 越权:写患者/病历数据
        template_name = str(payload.get("template_name") or "").strip()
        category = str(payload.get("category") or "").strip()
        content = _clean_content(payload.get("content_json"))

        if not template_name:
            raise HTTPException(status_code=400, detail="模板名称不能为空")
        if not content:
            raise HTTPException(status_code=400, detail="病历内容为空，无可保存的科目")

        template_id = "local-tmpl-" + stable_hash(template_name + "\x00" + category)
        content_json = json.dumps(content, ensure_ascii=False, sort_keys=True)
        now = now_str()

        init_db(db_path)
        with connect(db_path) as conn:
            existing = conn.execute(
                "select 1 from medical_templates where template_id = ?",
                (template_id,),
            ).fetchone()
            conn.execute(
                """
                insert into medical_templates(
                    template_id, template_name, category, content_json,
                    source_ids, active, created_at, updated_at
                )
                values (?, ?, ?, ?, 'local', 1, ?, ?)
                on conflict(template_id) do update set
                    template_name = excluded.template_name,
                    category = excluded.category,
                    content_json = excluded.content_json,
                    active = 1,
                    updated_at = excluded.updated_at
                """,
                (template_id, template_name, category, content_json, now, now),
            )
            conn.commit()

        return {
            "template_id": template_id,
            "template_name": template_name,
            "category": category,
            "created": existing is None,
        }

    return router
