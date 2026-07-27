"""解析 AI 病历 Markdown（纯函数）。

约定格式见 docs/开源发布_AI草稿接口.md。
头部行：姓名:/性别:/年龄:/就诊时间:/主诊医生:/诊室:
正文块：【主诉】【现病史】【检查所见】【诊断】【治疗明细】【收费明细】【医嘱】【复诊计划】
"""
import re


# 头部字段标签 -> header 键
_HEADER_FIELDS = {
    "姓名": "name_code",
    "性别": "sex",
    "年龄": "age",
    "就诊时间": "visit_time",
    "主诊医生": "doctor",
    "诊室": "room",
}

# 正文块标题 -> content_json 科目键（收费明细单独进 extras）
_BLOCK_SUBJECTS = {
    "主诉": "PC",
    "现病史": "HPI",
    "检查所见": "Exam",
    "诊断": "DG",
    "治疗明细": "TR",
    "医嘱": "DA",
    "复诊计划": "Plan",
}

_EXTRA_BLOCKS = {"收费明细"}

# 所有已知块标题（用于切分），含收费明细
_ALL_BLOCK_TITLES = list(_BLOCK_SUBJECTS.keys()) + list(_EXTRA_BLOCKS)

# Palmer 牙位：符号 ┘└┌┐ 与数字/字母组合，符号可在前可在后
_TOOTH_RE = re.compile(r"(?:[┘└┌┐]\s*[0-9A-Za-z]+|[0-9A-Za-z]+\s*[┘└┌┐])")


def _parse_header(text):
    header = {key: "" for key in _HEADER_FIELDS.values()}
    for label, key in _HEADER_FIELDS.items():
        m = re.search(rf"^{label}\s*[:：]\s*(.*)$", text, re.MULTILINE)
        if m:
            header[key] = m.group(1).strip()
    return header


def _split_blocks(text):
    """切出 【标题】 块 -> {标题: 块正文}。"""
    titles = "|".join(re.escape(t) for t in _ALL_BLOCK_TITLES)
    pattern = re.compile(rf"【({titles})】")
    blocks = {}
    matches = list(pattern.finditer(text))
    for i, m in enumerate(matches):
        title = m.group(1)
        start = m.end()
        end = matches[i + 1].start() if i + 1 < len(matches) else len(text)
        body = text[start:end].strip()
        if body:
            blocks[title] = body
    return blocks


def _extract_teeth(text):
    seen = []
    for m in _TOOTH_RE.finditer(text):
        token = re.sub(r"\s+", "", m.group(0))
        if token not in seen:
            seen.append(token)
    return seen


def parse_ai_record_markdown(text: str) -> dict:
    text = text or ""
    header = _parse_header(text)
    blocks = _split_blocks(text)

    content_json = {}
    extras = {}
    for title, body in blocks.items():
        if title in _EXTRA_BLOCKS:
            extras[title] = body
        else:
            content_json[_BLOCK_SUBJECTS[title]] = body

    teeth = _extract_teeth(text)
    tooth_json = {"positions": teeth} if teeth else {}

    return {
        "header": header,
        "content_json": content_json,
        "tooth_json": tooth_json,
        "extras": extras,
    }
