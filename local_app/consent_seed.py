"""出厂内置知情同意书模板:库里一套模板都没有时(全新安装)自动种入 25 套行业通用文书。
用户删过/建过任何模板后不再自动补种——种子只落一次,不跟用户抢库。"""
import json
from pathlib import Path

from local_app.db import connect
from local_app.timeutil import now_str

_SEED_FILE = Path(__file__).parent / "consent_templates_seed.json"


def ensure_consent_seed(db_path):
    """返回种入数量;库里已有任何模板(含停用)则 0。"""
    with connect(Path(db_path)) as conn:
        if conn.execute("select count(*) as c from consent_templates").fetchone()["c"]:
            return 0
        items = json.loads(_SEED_FILE.read_text(encoding="utf-8"))
        now = now_str()
        for i, t in enumerate(items, start=1):
            conn.execute(
                "insert into consent_templates(template_id, name, category, body, "
                "source, active, created_at, updated_at) "
                "values (?, ?, ?, ?, 'builtin', 1, ?, ?)",
                (f"builtin-ct-{i:03d}", t["name"], t["category"], t["body"], now, now),
            )
        conn.commit()
        return len(items)
