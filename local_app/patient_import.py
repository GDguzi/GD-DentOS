"""患者档案批量导入:解析 CSV/XLSX/SQLite → 表头自动匹配建档字段。
文件来源常见为旧系统/Excel 花名册导出;不引第三方库——CSV 走标准库,
XLSX 用 zipfile+ElementTree 读简单表,SQLite(.db/.sqlite3)自动挑含姓名+电话列的表。"""
import csv
import io
import os
import re
import sqlite3
import tempfile
import zipfile
from datetime import date, timedelta
from xml.etree import ElementTree as ET

# 建档字段 → 表头常见叫法(全部先做去空格/去冒号/小写归一后精确匹配)
FIELD_ALIASES = {
    "display_name": ("姓名", "名字", "患者姓名", "患者", "客户姓名", "name", "patient_name", "display_name"),
    "phone": ("电话", "手机", "手机号", "手机号码", "联系电话", "电话号码", "联系方式", "phone", "mobile", "tel"),
    "sex": ("性别", "sex", "gender"),
    "birthday": ("生日", "出生日期", "出生年月", "出生年月日", "birthday", "birth_date", "birthdate"),
    "id_card": ("身份证", "身份证号", "身份证号码", "证件号", "证件号码", "id_card", "idcard"),
    "address": ("地址", "住址", "家庭住址", "联系地址", "家庭地址", "address"),
    "wechat": ("微信", "微信号"),
    "email": ("邮箱", "电子邮箱", "email"),
    "occupation": ("职业",),
    "work_unit": ("工作单位", "单位"),
    "allergy_history": ("过敏史", "过敏"),
    "medication_history": ("用药史", "用药"),
    "patient_type": ("患者类型",),
    "responsible_doctor": ("责任医生", "主治医生", "医生"),
    "consultant": ("咨询师",),
    "referral_source": ("患者来源", "来源", "来源渠道", "信息来源"),
    "introducer_name": ("介绍人",),
    "remark": ("备注", "说明", "备注说明"),
}

_ALIAS_LOOKUP = {}
for _field, _names in FIELD_ALIASES.items():
    for _n in _names:
        _ALIAS_LOOKUP[_n.lower()] = _field


def _norm_header(text):
    """表头归一:去首尾空白/全半角冒号星号/内部空格,小写。"""
    return re.sub(r"[\s:：*＊]", "", str(text or "")).lower()


def auto_map_header(header):
    """表头行 → {列下标: 建档字段}。同一字段命中多列只认第一列。"""
    mapping, used = {}, set()
    for idx, cell in enumerate(header):
        field = _ALIAS_LOOKUP.get(_norm_header(cell))
        if field and field not in used:
            mapping[idx] = field
            used.add(field)
    return mapping


# ---------- 文件解析 ----------

def parse_table(filename, data):
    """CSV/XLSX/SQLite → 行列表(list[list[str]],首行为表头)。按文件魔数判别格式。"""
    if not data:
        raise ValueError("文件是空的")
    if data[:2] == b"PK":
        return _xlsx_rows(data)
    if data[:16] == b"SQLite format 3\x00":
        return _sqlite_rows(data)
    if str(filename or "").lower().endswith((".xls", ".xlsx")):
        # 旧 .xls(OLE2 二进制)读不了;xlsx 但 zip 魔数不对=文件损坏
        raise ValueError("只支持 .xlsx(新版 Excel)、CSV 或 SQLite(.db);旧版 .xls 请先用 Excel/WPS 另存为 .xlsx 或 CSV")
    if str(filename or "").lower().endswith((".db", ".sqlite", ".sqlite3")):
        raise ValueError("这不是有效的 SQLite 数据库文件")
    return _csv_rows(data)


def _csv_rows(data):
    text = None
    for enc in ("utf-8-sig", "gb18030"):
        try:
            text = data.decode(enc)
            break
        except UnicodeDecodeError:
            continue
    if text is None:
        raise ValueError("文件编码识别不了,请另存为 UTF-8 或 GBK 的 CSV")
    sample = text[:4096]
    delimiter = "\t" if sample.count("\t") > sample.count(",") else ","
    return [list(row) for row in csv.reader(io.StringIO(text), delimiter=delimiter)]


_XLSX_NS = "{http://schemas.openxmlformats.org/spreadsheetml/2006/main}"


def _col_index(ref):
    """单元格引用列字母 → 0 起下标,如 'C7'→2。"""
    n = 0
    for ch in ref:
        if ch.isalpha():
            n = n * 26 + (ord(ch.upper()) - 64)
    return n - 1


def _xlsx_rows(data):
    try:
        z = zipfile.ZipFile(io.BytesIO(data))
    except zipfile.BadZipFile:
        raise ValueError("xlsx 文件损坏,打不开")
    with z:
        shared = []
        if "xl/sharedStrings.xml" in z.namelist():
            for si in ET.fromstring(z.read("xl/sharedStrings.xml")).iter(f"{_XLSX_NS}si"):
                shared.append("".join(t.text or "" for t in si.iter(f"{_XLSX_NS}t")))
        sheets = sorted(n for n in z.namelist()
                        if n.startswith("xl/worksheets/sheet") and n.endswith(".xml"))
        if not sheets:
            raise ValueError("xlsx 里没有工作表")
        rows = []
        for row in ET.fromstring(z.read(sheets[0])).iter(f"{_XLSX_NS}row"):   # ponytail: 只读第一张表
            cells = {}
            for c in row.iter(f"{_XLSX_NS}c"):
                col = _col_index(c.get("r") or "")
                if col < 0:
                    continue
                kind = c.get("t") or ""
                v = c.find(f"{_XLSX_NS}v")
                if kind == "s" and v is not None and (v.text or "").isdigit():
                    val = shared[int(v.text)] if int(v.text) < len(shared) else ""
                elif kind == "inlineStr":
                    node = c.find(f"{_XLSX_NS}is")
                    val = "".join(t.text or "" for t in node.iter(f"{_XLSX_NS}t")) if node is not None else ""
                else:
                    val = v.text if v is not None else ""
                cells[col] = str(val or "")
            if cells:
                rows.append([cells.get(i, "") for i in range(max(cells) + 1)])
        return rows


def _sqlite_rows(data):
    """SQLite 库文件 → 挑"最像患者表"的一张(列名经别名匹配须含姓名+电话,且有数据行,
    匹配字段最多者胜),以列名作表头输出。只读打开,读完即删临时文件。"""
    fd, path = tempfile.mkstemp(suffix=".sqlite3")
    os.close(fd)
    try:
        with open(path, "wb") as fh:
            fh.write(data)
        try:
            conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
        except sqlite3.Error:
            raise ValueError("SQLite 文件打不开")
        try:
            try:
                tables = [r[0] for r in conn.execute(
                    "select name from sqlite_master where type = 'table' "
                    "and name not like 'sqlite_%'")]
            except sqlite3.DatabaseError:
                raise ValueError("SQLite 文件损坏或加了密")
            best = None   # (匹配字段数, 行数, 表名, 列名列表)
            for table in tables:
                quoted = '"' + table.replace('"', '""') + '"'
                cols = [r[1] for r in conn.execute(f"pragma table_info({quoted})")]
                fields = set(auto_map_header(cols).values())
                if "display_name" not in fields or "phone" not in fields:
                    continue
                count = conn.execute(f"select count(*) from {quoted}").fetchone()[0]
                if not count:
                    continue
                key = (len(fields), count)
                if best is None or key > best[0]:
                    best = (key, table, cols)
            if best is None:
                raise ValueError("数据库里找不到同时有姓名列和电话列的表")
            _, table, cols = best
            quoted = '"' + table.replace('"', '""') + '"'
            rows = [list(cols)]
            for row in conn.execute(f"select * from {quoted} limit 100000"):
                rows.append(["" if v is None else str(v) for v in row])
            return rows
        finally:
            conn.close()
    finally:
        os.unlink(path)


# ---------- 值清洗 ----------

def normalize_value(field, raw):
    text = str(raw or "").strip()
    if not text:
        return ""
    if field == "sex":
        low = text.lower()
        if text in ("男",) or low in ("m", "male", "1"):
            return "男"
        if text in ("女",) or low in ("f", "female", "2"):
            return "女"
        return text
    if field == "phone":
        cleaned = re.sub(r"[\s\-]", "", text)
        # Excel 把手机号存成数值会带 .0
        if re.fullmatch(r"\d+\.0", cleaned):
            cleaned = cleaned[:-2]
        return cleaned
    if field == "birthday":
        return _normalize_birthday(text)
    return text


def _normalize_birthday(text):
    # 先认四位出生年份(1900~2100 直接保留,建档"年龄直填"同款只存年份)——
    # 否则 1990 这类年份会被当 Excel 序列号折成 1905 年具体日期
    if re.fullmatch(r"(19|20)\d{2}", text):
        return text
    # Excel 日期序列号(1899-12-30 起算)
    if re.fullmatch(r"\d{4,5}(\.0)?", text):
        serial = int(float(text))
        if 366 < serial < 80000:
            return (date(1899, 12, 30) + timedelta(days=serial)).isoformat()
    m = re.fullmatch(r"(\d{4})[./年\-](\d{1,2})[./月\-](\d{1,2})日?", text)
    if m:
        return f"{m.group(1)}-{int(m.group(2)):02d}-{int(m.group(3)):02d}"
    if re.fullmatch(r"\d{8}", text):
        return f"{text[:4]}-{text[4:6]}-{text[6:]}"
    return text   # 纯年份等其它写法原样保留(建档年龄直填也只存年份)
