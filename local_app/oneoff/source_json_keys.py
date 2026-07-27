"""统计 patients.source_json 的键名和出现频次，只输出键名绝不输出值。

用法（用户执行）：
    python3 -m local_app.oneoff.source_json_keys [--db 路径]

输出结果贴回对话后，用于完善基本资料 tab 的 SOURCE_FIELD_GROUPS 分组映射。
"""

import argparse
import json
import sqlite3
from collections import Counter

from .db import DEFAULT_DB_PATH


def collect_key_counts(db_path):
    counter = Counter()
    rows = 0
    conn = sqlite3.connect(db_path)
    try:
        for (raw,) in conn.execute("select source_json from patients"):
            rows += 1
            try:
                data = json.loads(raw or "{}")
            except (TypeError, ValueError):
                continue
            if isinstance(data, dict):
                counter.update(data.keys())
    finally:
        conn.close()
    return rows, counter


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", default=str(DEFAULT_DB_PATH))
    args = parser.parse_args()

    rows, counter = collect_key_counts(args.db)
    print(f"patients={rows}")
    print(f"distinct_keys={len(counter)}")
    for key, count in counter.most_common():
        print(f"{key}\t{count}")


if __name__ == "__main__":
    main()
