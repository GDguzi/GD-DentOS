"""多方式收款原文串解析（'银行卡:1233||现金:800' 这种一单多付）。

导入的收款流水可能把多种付款方式挤在一个字符串里；收银查询与营收报表都要拆开它，
解析口径统一放这里做唯一真相源。
"""
import re

# 方式名归一，与本地收款选项口径对齐（两个函数共用的唯一真相源）。
# 各家收单渠道的大小写/别名不一致时在此登记，如 {"XX支付": "xx支付"}。
_NORM = {}


def paytype_from_detail(paiddetail):
    """从收款原文('银行卡:1233||现金:800') 解析付款方式：单一→该方式,多个→'多种'。
    给收银查询/营收报表的付款方式透视用(否则导入流水 pay_type 为空→落'其他')。"""
    segs = [seg.split(":", 1)[0].strip() for seg in re.split(r"\|\||[;,、\n]", str(paiddetail or "")) if ":" in seg]
    methods = list(dict.fromkeys(_NORM.get(m, m) for m in segs if m))
    if not methods:
        return ""
    return methods[0] if len(methods) == 1 else "多种"


def split_paid_detail(detail):
    """收款原文('银行卡:1233||现金:800') → [('银行卡',1233.0),('现金',800.0)]。
    任一段解析不出(缺名/金额非数)返回 [],调用方回退按 pay_type 整笔计。
    方式名归一口径与 paytype_from_detail 一致。只按 '||' 切段——
    金额可能带千分位逗号,不能像 paytype_from_detail 那样把逗号也当分隔符。"""
    out = []
    for seg in str(detail or "").split("||"):
        if ":" not in seg:
            continue
        name, _, val = seg.partition(":")
        name = name.strip()
        try:
            amt = float(str(val).replace(",", "").strip())
        except ValueError:
            return []
        if not name:
            return []
        out.append((_NORM.get(name, name), amt))
    return out
