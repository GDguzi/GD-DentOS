"""paiddetail 串解析（'扫码付:1233||现金:800' 这种多方式收款原文）。

多方式收款原文存在核心库里，收银查询/营收报表都要解析它——解析逻辑的真相源放核心，
其他模块需要时向核心 import（单向依赖：外围→核心）。
"""
import os
import re


ALIAS_ENV = "DENTAL_PAY_METHOD_ALIASES"


def load_pay_method_aliases():
    """方式名归一表：上游系统导出的写法 → 本地收款选项的写法（两个函数共用的唯一真相源）。

    诊所专属值不写死在源码里（开源剥离铁律）：走环境变量
    `DENTAL_PAY_METHOD_ALIASES`，格式 `上游写法=本地写法`，多条用 `,` 隔开，例如
        DENTAL_PAY_METHOD_ALIASES="某某E付=某某e付,某某Pay=某某支付"
    没配（变量缺席或为空）就是不做归一——按原文落。

    配了但写错格式一律抛错，不跳过、不降级（禁止兜底）：静默忽略一条写坏的映射，
    表现是服务照常起、报表里同一种付款方式悄悄裂成两组，钱对不上还查不出原因。
    """
    raw = os.environ.get(ALIAS_ENV, "").strip()
    if not raw:
        return {}
    aliases = {}
    for pair in raw.split(","):
        # 必须恰好一个 '='：partition 只切第一个，`A=B=C` 会被当成合法并把 A 映射成 `B=C`，
        # 又是一次静默的错映射。
        src, sep, dst = pair.partition("=")
        if pair.count("=") != 1 or not src.strip() or not dst.strip():
            raise ValueError(
                f"{ALIAS_ENV} 格式错误：条目 {pair!r} 不是「上游写法=本地写法」。"
                f"整串为 {raw!r}；多条用英文逗号隔开，每条恰好一个等号，两侧都不能为空。"
            )
        aliases[src.strip()] = dst.strip()
    return aliases


def paytype_from_detail(paiddetail):
    """从付款明细 paiddetail('扫码付:1233||现金:800') 解析付款方式：单一→该方式,多个→'多种'。
    给收银查询/营收报表的付款方式透视用(否则同步流水 pay_type 为空→落'其他')。"""
    norm = load_pay_method_aliases()
    segs = [seg.split(":", 1)[0].strip() for seg in re.split(r"\|\||[;,、\n]", str(paiddetail or "")) if ":" in seg]
    methods = list(dict.fromkeys(norm.get(m, m) for m in segs if m))
    if not methods:
        return ""
    return methods[0] if len(methods) == 1 else "多种"


def split_paid_detail(detail):
    """paiddetail('扫码付:1233||现金:800') → [('扫码付',1233.0),('现金',800.0)]。
    任一段解析不出(缺名/金额非数)返回 [],调用方回退按 pay_type 整笔计(#525)。
    方式名归一口径与 paytype_from_detail 一致。只按 SaaS 实际分隔符'||'切段——
    金额可能带千分位逗号,不能像 paytype_from_detail 那样把逗号也当分隔符。"""
    norm = load_pay_method_aliases()
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
        out.append((norm.get(name, name), amt))
    return out
