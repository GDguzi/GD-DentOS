"""汉字姓名 → 可搜索拼音串：全拼 + 首字母，小写、空格连接（如「罗小云」→「luoxiaoyun lxy」）。

pypinyin 为**软依赖**：未安装时返回空串，核心功能(建患者/同步/搜索)不受影响，
只是该患者搜不到拼音；装上 pypinyin 后(建患者/回填)即自动带拼音。
"""


def name_pinyin(name):
    name = (name or "").strip()
    if not name:
        return ""
    try:
        from pypinyin import lazy_pinyin, Style
    except ImportError:
        return ""
    try:
        full = "".join(lazy_pinyin(name)).lower()
        initials = "".join(lazy_pinyin(name, style=Style.FIRST_LETTER)).lower()
    except Exception:
        return ""
    return " ".join(p for p in (full, initials) if p)
