"""一次性/可重跑：给 patients.name_pinyin 为空的患者回填拼音（全拼+首字母）。

用法：
    python3 -m local_app.oneoff.backfill_pinyin            # 默认库
    python3 -m local_app.oneoff.backfill_pinyin /path/db   # 指定库

需要 pypinyin（pip install pypinyin）。大批同步后可再跑一次补新患者。
只填 name_pinyin 为空的行，幂等。
"""
import sys

from local_app.db import DEFAULT_DB_PATH, connect, init_db
from local_app.pinyin_util import name_pinyin


def backfill(db_path):
    init_db(db_path)  # 确保 name_pinyin 列已迁移
    with connect(db_path) as conn:
        rows = conn.execute(
            "select patient_identity, display_name from patients "
            "where coalesce(name_pinyin, '') = '' and coalesce(display_name, '') != ''"
        ).fetchall()
        filled = 0
        for r in rows:
            py = name_pinyin(r["display_name"])
            if not py:
                continue
            conn.execute(
                "update patients set name_pinyin = ? where patient_identity = ?",
                (py, r["patient_identity"]),
            )
            filled += 1
        conn.commit()
    return len(rows), filled


if __name__ == "__main__":
    path = sys.argv[1] if len(sys.argv) > 1 else DEFAULT_DB_PATH
    total, filled = backfill(path)
    print(f"待回填 {total} 人，成功回填 {filled} 人（拼音为空者已跳过）")
