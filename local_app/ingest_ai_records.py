"""摄入 AI 语音病历草稿（CLI）。

扫描 {records-dir}/{date}/*.md，解析后按脱敏映射还原真名，在 patients 表
唯一命中才落 medical_records（draft_status=ai_draft, origin=ai_voice）；
未命中/重名/无映射 -> ai_unclaimed_drafts。
幂等键 source_file（含日期目录的相对路径，如 2026-06-09/record_001.md），
两表语义一致：预读集合覆盖 medical_records(ai_meta_json.source_file) +
ai_unclaimed_drafts(source_file) 作为主去重判断；unclaimed 表的 on conflict
仅作并发兜底。

隐私：报告只含计数，不打印姓名/病历正文。
"""
import argparse
import hashlib
import os
import logging
import json
import sqlite3
from datetime import datetime
from pathlib import Path

from local_app.db import DEFAULT_DB_PATH, init_db, new_id
from local_app.ai_record_parser import parse_ai_record_markdown
from local_app.batches import ensure_batch as _ensure_batch, finish_batch as _finish_batch
from local_app.versioning import stable_hash


def _load_mapping(mapping_path):
    if not mapping_path:
        return {}
    path = Path(mapping_path)
    if not path.exists():
        # 映射文件缺失：友好提示后视为无映射（全部归 unclaimed），不整批崩
        print(f"[warn] 映射文件不存在，按无映射处理: {path}")
        return {}
    return json.loads(path.read_text(encoding="utf-8"))


def _resolve_patient(conn, real_name):
    if not isinstance(real_name, str):
        # 非字符串映射值（dict/list/int 等）视为无映射 -> 进 unclaimed
        return None
    if not real_name:
        return None
    rows = conn.execute(
        "select patient_identity from patients where display_name = ?",
        (real_name,),
    ).fetchall()
    if len(rows) == 1:
        return rows[0][0]
    return None


def _existing_source_files(conn):
    files = set()
    for (meta,) in conn.execute("select ai_meta_json from medical_records"):
        try:
            sf = json.loads(meta or "{}").get("source_file")
        except (ValueError, TypeError):
            sf = None
        if sf:
            files.add(sf)
    for (sf,) in conn.execute(
        "select source_file from ai_unclaimed_drafts where source_file is not null"
    ):
        if sf:
            files.add(sf)
    return files


def _write_medical_record(conn, patient_identity, parsed, source_file, batch_id):
    header = parsed["header"]
    content = parsed["content_json"]
    tooth = parsed["tooth_json"]
    visit_time = header.get("visit_time") or ""
    doctor_name = header.get("doctor") or ""
    ai_meta = {
        "source_file": source_file,
        "room": header.get("room") or "",
        "code_name": header.get("name_code") or "",
        "extras": parsed["extras"],
    }
    record_id = new_id("local-ai")
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    tooth_json = json.dumps(tooth, ensure_ascii=False, sort_keys=True)
    content_json = json.dumps(content, ensure_ascii=False, sort_keys=True)
    # 哈希字段口径必须跟 snapshots.medical_record_snapshot()(api.py 更新病历时用它算
    # old_hash/new_hash)完全一致——否则首次本地编辑会拿两套不同公式的哈希互相比较,恒判"有变化"，
    # 写出跟 stable_hash(旧快照) 对不上的不可校验版本行。
    current_hash = stable_hash(
        {
            "record_id": record_id,
            "patient_identity": patient_identity,
            "study_identity": None,
            "record_type": "",
            "visit_time": visit_time,
            "doctor_name": doctor_name,
            "tooth_json": tooth_json,
            "content_json": content_json,
            "updated_at": now,
        }
    )
    conn.execute(
        """
        insert into medical_records(
            record_id, patient_identity, record_type, visit_time, doctor_name,
            tooth_json, content_json, draft_status, origin, ai_meta_json,
            created_at, updated_at, current_hash, last_batch_id
        )
        values (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            record_id,
            patient_identity,
            "",
            visit_time,
            doctor_name,
            tooth_json,
            content_json,
            "ai_draft",
            "ai_voice",
            json.dumps(ai_meta, ensure_ascii=False, sort_keys=True),
            now,
            now,
            current_hash,
            batch_id,
        ),
    )


def _write_unclaimed(conn, parsed, source_file):
    header = parsed["header"]
    ai_meta = {"extras": parsed["extras"]}
    conn.execute(
        """
        insert into ai_unclaimed_drafts(
            unclaimed_id, code_name, visit_time, room, doctor_name,
            content_json, tooth_json, ai_meta_json, source_file, status
        )
        values (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        on conflict(source_file) do nothing
        """,
        (
            new_id("ai-unclaimed"),
            header.get("name_code") or "",
            header.get("visit_time") or "",
            header.get("room") or "",
            header.get("doctor") or "",
            json.dumps(parsed["content_json"], ensure_ascii=False, sort_keys=True),
            json.dumps(parsed["tooth_json"], ensure_ascii=False, sort_keys=True),
            json.dumps(ai_meta, ensure_ascii=False, sort_keys=True),
            source_file,
            "pending",
        ),
    )


def ingest_ai_records(db_path, records_dir, date, mapping_path=None, batch_id=None):
    init_db(db_path)
    if batch_id is None:
        batch_id = "ai-record-ingest-" + datetime.now().strftime("%Y%m%d%H%M%S")

    mapping = _load_mapping(mapping_path)
    day_dir = Path(records_dir) / date
    md_files = sorted(day_dir.glob("*.md")) if day_dir.is_dir() else []

    report = {
        "files_scanned": 0,
        "parsed": 0,
        "matched": 0,
        "unclaimed": 0,
        "skipped_dup": 0,
        "errors": 0,
        "error_files": [],
    }

    with sqlite3.connect(db_path) as conn:
        conn.execute("pragma foreign_keys = on")
        _ensure_batch(conn, batch_id, "ai_record_ingest")
        seen_source_files = _existing_source_files(conn)

        for md_file in md_files:
            report["files_scanned"] += 1
            # 去重键含日期目录的相对路径，保证跨日同名文件唯一
            source_file = f"{date}/{md_file.name}"
            if source_file in seen_source_files:
                report["skipped_dup"] += 1
                continue

            try:
                parsed = parse_ai_record_markdown(
                    md_file.read_text(encoding="utf-8")
                )
                report["parsed"] += 1

                # name_code 是 .md「姓名」字段。两种数据契约都要兼容：
                # ①真名直填（当前语音管线输出）→ 直接匹配 display_name；
                # ②脱敏代号 + 映射(代号->真名) → 经映射还原再匹配。
                # 先直配真名，无果再走映射，互不破坏。
                name_code = parsed["header"].get("name_code") or ""
                patient_identity = _resolve_patient(conn, name_code)
                if not patient_identity:
                    patient_identity = _resolve_patient(conn, mapping.get(name_code))

                if patient_identity:
                    _write_medical_record(
                        conn, patient_identity, parsed, source_file, batch_id
                    )
                    report["matched"] += 1
                else:
                    _write_unclaimed(conn, parsed, source_file)
                    report["unclaimed"] += 1
                seen_source_files.add(source_file)
            except Exception as exc:
                # 单文件任意环节出错（解析/映射/写库）跳过不中断整批、不回滚其他已写入文件；
                # 异常必须留痕(架构铁律#禁止兜底)——但文件名含患者真名、异常message可能带病历上下文,
                # 一律不进report/CLI/日志(隐私铁律)：对外只给 序号+短hash+异常类型,
                # 明文映射只写 db 同目录(gitignored data区)的 ingest_errors.log 供人工定位。
                report["errors"] += 1
                fhash = hashlib.sha1(str(source_file).encode("utf-8")).hexdigest()[:8]
                report["error_files"].append(f"#{report['errors']} hash={fhash} {type(exc).__name__}")
                try:
                    with open(Path(db_path).parent / "ingest_errors.log", "a", encoding="utf-8") as fh:
                        fh.write(f"{datetime.now():%Y-%m-%d %H:%M:%S} hash={fhash} "
                                 f"{source_file}: {type(exc).__name__}: {exc}\n")
                except OSError:
                    logging.getLogger("local_app").error("摄入诊断文件写入失败(不挡主流程)")
                logging.getLogger("local_app").error(
                    "AI病历摄入失败 hash=%s type=%s(明细见data区ingest_errors.log)", fhash, type(exc).__name__)
                continue

        _finish_batch(conn, batch_id, report["matched"], report["skipped_dup"])
        conn.commit()

    report["batch_id"] = batch_id
    return report


def main():
    parser = argparse.ArgumentParser(
        description="摄入 AI 语音病历草稿到本地库（报告仅计数，不打印患者数据）。"
    )
    parser.add_argument(
        "--records-dir",
        # 默认中性(数据区 ai_records/,可用环境变量或参数指向自己的生成器输出);
        # 上游调用方已显式传本参数,不吃默认值。
        default=os.environ.get("DENTAL_AI_RECORDS_DIR")
        or str(Path(DEFAULT_DB_PATH).parent / "ai_records"),
        help="AI 病历草稿目录(按日期分子目录,每份 *.md);默认 data/ai_records 或 $DENTAL_AI_RECORDS_DIR",
    )
    parser.add_argument("--date", help="YYYY-MM-DD，处理某天子目录")
    parser.add_argument("--all", action="store_true", help="处理所有日期子目录")
    parser.add_argument("--mapping", help="脱敏映射 JSON 路径（代号->真名）")
    parser.add_argument("--db", default=str(DEFAULT_DB_PATH))
    args = parser.parse_args()

    records_dir = Path(args.records_dir)
    if args.all:
        # 干净安装(可选 AI 草稿箱)时目录不存在是正常空态,给清晰提示按 0 文件退出,不抛栈
        if not records_dir.is_dir():
            print(f"草稿目录不存在（AI 草稿箱为可选功能，未使用则无需处理）: {records_dir}")
            return 0
        dates = sorted(p.name for p in records_dir.iterdir() if p.is_dir())
    elif args.date:
        dates = [args.date]
    else:
        parser.error("必须指定 --date 或 --all")

    totals = {
        "files_scanned": 0,
        "parsed": 0,
        "matched": 0,
        "unclaimed": 0,
        "skipped_dup": 0,
        "errors": 0,
    }
    error_files = []
    for date in dates:
        report = ingest_ai_records(
            db_path=args.db,
            records_dir=records_dir,
            date=date,
            mapping_path=args.mapping,
        )
        for key in totals:
            totals[key] += report[key]
        error_files.extend(report.get("error_files", []))

    for key in ["files_scanned", "parsed", "matched", "unclaimed", "skipped_dup", "errors"]:
        print(f"{key}={totals[key]}")
    for line in error_files:   # 出错文件+异常类型逐条可见，不再只有一个数字
        print(f"error_file={line}")


if __name__ == "__main__":
    main()
