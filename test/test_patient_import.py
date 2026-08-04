"""患者批量导入:CSV/XLSX 解析、表头自动匹配、预览/入库、防重复。"""
import io
import tempfile
import unittest
import zipfile
from pathlib import Path

from fastapi.testclient import TestClient

from local_app import auth
from local_app.api import create_app
from local_app.db import connect, init_db
from local_app.patient_import import auto_map_header, normalize_value, parse_table


def _client(tmp):
    db = Path(tmp) / "clinic.sqlite3"
    init_db(db)
    with connect(db) as conn:
        auth.create_user(conn, "boss", "院长", "admin123", role="admin")
        conn.commit()
    client = TestClient(create_app(db))
    client.post("/api/auth/login", json={"username": "boss", "password": "admin123"})
    return db, client


def _xlsx_bytes(rows):
    """拼一个最小可用 xlsx(inlineStr 单元格)。"""
    def cell(ref, val):
        return f'<c r="{ref}" t="inlineStr"><is><t>{val}</t></is></c>'

    def col_letter(i):
        s = ""
        i += 1
        while i:
            i, r = divmod(i - 1, 26)
            s = chr(65 + r) + s
        return s

    xml_rows = "".join(
        "<row r=\"%d\">%s</row>" % (
            ri + 1,
            "".join(cell(f"{col_letter(ci)}{ri + 1}", v) for ci, v in enumerate(row)),
        )
        for ri, row in enumerate(rows)
    )
    ns = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("xl/worksheets/sheet1.xml",
                   f'<worksheet xmlns="{ns}"><sheetData>{xml_rows}</sheetData></worksheet>')
    return buf.getvalue()


class ParseAndMapTest(unittest.TestCase):
    def test_csv_utf8_bom_and_gbk(self):
        rows = parse_table("a.csv", "﻿姓名,电话\n张三,13800001111\n".encode("utf-8"))
        self.assertEqual(rows, [["姓名", "电话"], ["张三", "13800001111"]])
        rows = parse_table("a.csv", "姓名,电话\n李四,13800002222\n".encode("gb18030"))
        self.assertEqual(rows[1][0], "李四")

    def test_xlsx_roundtrip(self):
        data = _xlsx_bytes([["姓名", "手机号"], ["王五", "13800003333"]])
        rows = parse_table("名单.xlsx", data)
        self.assertEqual(rows, [["姓名", "手机号"], ["王五", "13800003333"]])

    def test_old_xls_rejected_with_hint(self):
        with self.assertRaises(ValueError):
            parse_table("旧表.xls", b"\xd0\xcf\x11\xe0old-ole2")

    def test_header_auto_map_aliases(self):
        mapping = auto_map_header(["姓名", "联系电话", "出生日期", "身份证号", "备注", "认不出的列"])
        self.assertEqual(mapping, {0: "display_name", 1: "phone", 2: "birthday", 3: "id_card", 4: "remark"})

    def test_value_normalization(self):
        self.assertEqual(normalize_value("phone", "138 0000-1111.0".replace(" ", " ")), "1380000111" + "1")
        self.assertEqual(normalize_value("phone", "13800001111.0"), "13800001111")
        self.assertEqual(normalize_value("sex", "F"), "女")
        self.assertEqual(normalize_value("birthday", "1990/1/3"), "1990-01-03")
        self.assertEqual(normalize_value("birthday", "19900103"), "1990-01-03")
        # Excel 日期序列号:32874 = 1990-01-01
        self.assertEqual(normalize_value("birthday", "32874"), "1990-01-01")
        # 四位出生年份必须原样保留,不得折成 1905 年的序列号日期
        self.assertEqual(normalize_value("birthday", "1990"), "1990")
        self.assertEqual(normalize_value("birthday", "2005"), "2005")


class ImportApiTest(unittest.TestCase):
    def test_preview_reports_mapping_and_issues(self):
        with tempfile.TemporaryDirectory() as tmp:
            _, client = _client(tmp)
            csv_data = "姓名,手机,神秘列\n张三,13800001111,x\n,13800002222,y\n".encode("utf-8")
            resp = client.post("/api/patients/import",
                               files={"file": ("名单.csv", csv_data, "text/csv")},
                               data={"mode": "preview"})
            self.assertEqual(resp.status_code, 200, resp.text)
            body = resp.json()
            self.assertEqual(body["total_rows"], 2)
            self.assertEqual(body["importable"], 1)
            self.assertEqual(body["unmatched_headers"], ["神秘列"])
            fields = {m["field"] for m in body["matched_columns"]}
            self.assertEqual(fields, {"display_name", "phone"})
            self.assertTrue(any("缺姓名" in s for s in body["issues"]))

    def test_commit_creates_with_chart_no_and_skips_duplicates(self):
        with tempfile.TemporaryDirectory() as tmp:
            db, client = _client(tmp)
            csv_data = ("姓名,手机,性别,出生日期\n"
                        "张三,13800001111,男,1990/1/3\n"
                        "张三,13800001111,男,1990/1/3\n"      # 文件内重复
                        "李四,13800002222,女,32874\n").encode("utf-8")
            resp = client.post("/api/patients/import",
                               files={"file": ("名单.csv", csv_data, "text/csv")},
                               data={"mode": "commit"})
            self.assertEqual(resp.status_code, 200, resp.text)
            self.assertEqual(resp.json()["created"], 2)
            self.assertEqual(resp.json()["skipped_duplicates"], 1)
            with connect(db) as conn:
                rows = conn.execute(
                    "select display_name, phone, sex, birthday, chart_no, name_pinyin "
                    "from patients order by chart_no").fetchall()
            self.assertEqual(len(rows), 2)
            self.assertEqual(rows[0]["birthday"], "1990-01-03")
            self.assertEqual(rows[1]["birthday"], "1990-01-01")
            self.assertTrue(all(r["chart_no"] for r in rows), "导入也要有病历号")

            # 再导一遍:全部按重复跳过
            again = client.post("/api/patients/import",
                                files={"file": ("名单.csv", csv_data, "text/csv")},
                                data={"mode": "commit"})
            self.assertEqual(again.json()["created"], 0)
            self.assertEqual(again.json()["skipped_duplicates"], 3)

    def test_commit_with_manual_mapping_overrides_auto(self):
        with tempfile.TemporaryDirectory() as tmp:
            db, client = _client(tmp)
            csv_data = "A,B\n赵六,13800004444\n".encode("utf-8")
            resp = client.post("/api/patients/import",
                               files={"file": ("x.csv", csv_data, "text/csv")},
                               data={"mode": "commit",
                                     "mapping": '{"0": "display_name", "1": "phone"}'})
            self.assertEqual(resp.status_code, 200, resp.text)
            self.assertEqual(resp.json()["created"], 1)

    def test_commit_without_name_phone_columns_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            _, client = _client(tmp)
            csv_data = "备注\n随便\n".encode("utf-8")
            resp = client.post("/api/patients/import",
                               files={"file": ("x.csv", csv_data, "text/csv")},
                               data={"mode": "commit"})
            self.assertEqual(resp.status_code, 400)

    def test_import_route_registered_in_access_policy(self):
        # 生产的登录/权限门在策略层(access_policy)+登录网关;此处锁定路由已登记且口径=建档权限
        from local_app.access_policy import ROUTE_POLICY_BY_KEY
        policy = ROUTE_POLICY_BY_KEY.get(("POST", "/api/patients/import"))
        self.assertIsNotNone(policy, "导入路由必须在策略表登记")
        self.assertEqual(policy.policy_type, "SYSTEM_ADMIN", "批量数据进出与备份同口径:仅管理员")


if __name__ == "__main__":
    unittest.main()


def _sqlite_bytes(table_sql, insert_sql, rows):
    import sqlite3 as _sq
    import tempfile as _tf, os as _os
    fd, path = _tf.mkstemp(suffix=".sqlite3")
    _os.close(fd)
    try:
        conn = _sq.connect(path)
        conn.execute(table_sql)
        conn.executemany(insert_sql, rows)
        conn.commit()
        conn.close()
        return Path(path).read_bytes()
    finally:
        _os.unlink(path)


class SqliteImportTest(unittest.TestCase):
    """SQL(SQLite .db/.sqlite3)文件导入:自动挑出含姓名+电话列的表,列名同走别名匹配。"""

    def test_parse_sqlite_picks_patient_table(self):
        data = _sqlite_bytes(
            "create table 患者(姓名 text, 电话 text, 备注 text)",
            "insert into 患者 values(?,?,?)",
            [("张三", "13800001111", "老客")],
        )
        rows = parse_table("旧系统.db", data)
        self.assertEqual(rows[0], ["姓名", "电话", "备注"])
        self.assertEqual(rows[1], ["张三", "13800001111", "老客"])

    def test_parse_sqlite_english_columns_and_skips_unrelated_tables(self):
        import sqlite3 as _sq
        import tempfile as _tf, os as _os
        fd, path = _tf.mkstemp(suffix=".db")
        _os.close(fd)
        try:
            conn = _sq.connect(path)
            conn.execute("create table settings(k text, v text)")
            conn.execute("insert into settings values('a','b')")
            conn.execute("create table patients(name text, mobile text, birthday text)")
            conn.execute("insert into patients values('李四','13800002222','1990-01-03')")
            conn.commit()
            conn.close()
            data = Path(path).read_bytes()
        finally:
            _os.unlink(path)
        rows = parse_table("backup.sqlite3", data)
        self.assertEqual(rows[0], ["name", "mobile", "birthday"])
        self.assertEqual(rows[1][0], "李四")

    def test_parse_sqlite_without_patient_table_rejected(self):
        data = _sqlite_bytes(
            "create table logs(msg text)", "insert into logs values(?)", [("x",)],
        )
        with self.assertRaises(ValueError):
            parse_table("a.db", data)

    def test_import_api_accepts_sqlite_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            _, client = _client(tmp)
            data = _sqlite_bytes(
                "create table 患者(姓名 text, 手机号 text)",
                "insert into 患者 values(?,?)",
                [("王五", "13800003333")],
            )
            resp = client.post("/api/patients/import",
                               files={"file": ("旧库.db", data, "application/octet-stream")},
                               data={"mode": "commit"})
            self.assertEqual(resp.status_code, 200, resp.text)
            self.assertEqual(resp.json()["created"], 1)
