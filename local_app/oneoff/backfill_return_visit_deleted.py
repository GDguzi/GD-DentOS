"""回填回访的 SaaS 删除标记（一次性）。

背景：`import_related` 组 base_fields 时漏抄 `IsDeleted`（已修，见 fix(迁移) 那笔提交），
导致外部库批量导入的行里，诊所在外部系统删掉的回访在本地全是 `is_deleted=0`，
被当成有效待办。本脚本按每行自己的 `source_json.IsDeleted` 把历史数据补回来。

判定只认行内证据，不猜：
- `source_json.IsDeleted == "1"` → 软删
- 值为其它 → 保持不动
- **键缺席** → 保持不动（该来源不提供删除信息，如 patvisit 接口）

安全边界：
- 默认 dry-run，只统计不写库；`--apply` 才写，且写前整库备份。
- 只改 `is_deleted` 一列，不碰任何业务字段，可逆（回滚见文末）。
- 跳过本地已软删的行（不重复处理），跳过本地改过的行（`--skip-locally-edited`，默认开）。

用法：
    python3 -m local_app.oneoff.backfill_return_visit_deleted            # dry-run
    python3 -m local_app.oneoff.backfill_return_visit_deleted --apply    # 真写

回滚：备份整库覆盖回去，或
    update return_visits set is_deleted = 0 where last_batch_id = '<本次批次>';
"""
import argparse
import shutil
import sqlite3
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from local_app.db import DEFAULT_DB_PATH


def _default_backup_dir(db_path):
    """备份跟着被改的库走,不写死仓库路径——否则拿合成库演练时,
    备份会落进真实备份目录污染现场(实测踩过)。"""
    return Path(db_path).resolve().parent / "backups"


def _bj_stamp():
    """时区铁律：一律北京时间，不用裸 datetime.now()。"""
    return datetime.now(ZoneInfo("Asia/Shanghai")).strftime("%Y%m%d_%H%M%S")


def survey(conn):
    """只读统计：按 source_json.IsDeleted 分桶，看各桶现在的本地删除标记。"""
    rows = conn.execute(
        """
        select coalesce(json_extract(source_json, '$.IsDeleted'), '(键缺席)') as flag,
               coalesce(is_deleted, 0) as local_deleted,
               count(*) as n
        from return_visits
        group by 1, 2
        """
    ).fetchall()
    return [(r["flag"], r["local_deleted"], r["n"]) for r in rows]


def targets(conn, skip_locally_edited=True):
    """待回填的 return_visit_id 列表：SaaS 已删、本地还没删的。

    skip_locally_edited：本地有过人工编辑/软删审计的行不动，交人工判定
    （远端删了但本地刚改过，属于真冲突，不该静默覆盖）。
    """
    sql = """
        select r.return_visit_id
        from return_visits r
        where json_extract(r.source_json, '$.IsDeleted') = '1'
          and coalesce(r.is_deleted, 0) = 0
    """
    if skip_locally_edited:
        sql += """
          and not exists (
              select 1 from audit_logs a
              where a.entity_type = 'return_visit'
                and a.entity_id = r.return_visit_id
                and a.action in ('update_return_visit', 'soft_delete_return_visit')
          )
        """
    return [r[0] for r in conn.execute(sql).fetchall()]


def pending_count(conn):
    """当前被判成「待办」的回访数（回填前后各测一次，看清掉多少幽灵）。"""
    return conn.execute(
        """
        select count(*) from return_visits r
        where coalesce(r.is_deleted, 0) = 0
          and not (
              coalesce(r.status, '') in ('done', '已回访', '完成', '4')
              or (coalesce(r.status, '') not in ('待回访', '待跟进', '做计划')
                  and (coalesce(r.item_name, '') like '已回访%'
                       or coalesce(r.note, '') like '已回访%'))
          )
        """
    ).fetchone()[0]


def run(db_path=None, apply=False, backup_dir=None, skip_locally_edited=True):
    db_path = Path(db_path or DEFAULT_DB_PATH)
    backup_dir = Path(backup_dir) if backup_dir else _default_backup_dir(db_path)

    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row

    print(">> 回填前分布（SaaS删除标记 × 本地删除标记）：")
    for flag, local_deleted, n in survey(conn):
        print(f"     IsDeleted={flag:<8} 本地is_deleted={local_deleted}  {n} 条")

    ids = targets(conn, skip_locally_edited)
    skipped_edited = 0
    if skip_locally_edited:
        all_ids = set(targets(conn, skip_locally_edited=False))
        skipped_edited = len(all_ids) - len(ids)

    before_pending = pending_count(conn)
    print(f">> 待回填 {len(ids)} 条；因本地改过而跳过 {skipped_edited} 条。")
    print(f">> 回填前「待办」回访数：{before_pending}")

    if not apply:
        print(">> dry-run，未写库。加 --apply 才真写。")
        conn.close()
        return {"targets": len(ids), "skipped_locally_edited": skipped_edited,
                "before_pending": before_pending, "applied": False}

    if not ids:
        print(">> 没有待回填的行，不写库。")
        conn.close()
        return {"targets": 0, "skipped_locally_edited": skipped_edited,
                "before_pending": before_pending, "applied": False}

    backup_dir.mkdir(parents=True, exist_ok=True)
    stamp = _bj_stamp()
    bak = backup_dir / f"clinic_before_rv_deleted_backfill_{stamp}.sqlite3"
    shutil.copy2(db_path, bak)
    print(f">> 已备份整库 → {bak.name} ({bak.stat().st_size / 1e6:.1f} MB)")

    batch_id = f"rv-deleted-backfill-{stamp}"
    conn.execute("begin immediate")
    conn.executemany(
        "update return_visits set is_deleted = 1, last_batch_id = ?, "
        "revision = revision + 1 where return_visit_id = ?",
        [(batch_id, rv_id) for rv_id in ids],
    )
    conn.commit()

    after_pending = pending_count(conn)
    print(f">> 已写入 {len(ids)} 条，批次 {batch_id}")
    print(f">> 回填后「待办」回访数：{after_pending}（少了 {before_pending - after_pending}）")
    print(f">> 回滚：update return_visits set is_deleted = 0 where last_batch_id = '{batch_id}';")
    conn.close()
    return {"targets": len(ids), "skipped_locally_edited": skipped_edited,
            "before_pending": before_pending, "after_pending": after_pending,
            "batch_id": batch_id, "backup": str(bak), "applied": True}


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true", help="真写库(默认dry-run只统计)")
    ap.add_argument("--db", default=None, help="库路径(默认走 DENTAL_DB/生产库)")
    ap.add_argument("--include-locally-edited", action="store_true",
                    help="连本地改过的行也回填(默认跳过,交人工判定)")
    args = ap.parse_args(argv)
    return run(db_path=args.db, apply=args.apply,
               skip_locally_edited=not args.include_locally_edited)


if __name__ == "__main__":
    main()
