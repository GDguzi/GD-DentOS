"""UI重构Phase0 一次性迁移:styles.css 硬编码色值收编到设计 token。
用法(仓库根执行):
  python3 -m local_app.oneoff.migrate_css_tokens --strip-fallbacks  # 剥 var(--x,#hex) 备胎
  python3 -m local_app.oneoff.migrate_css_tokens --apply-mapping    # 按 MAPPING 换族
  python3 -m local_app.oneoff.migrate_css_tokens --report           # 打印剩余长尾(贴守卫白名单)
三种模式均幂等;只改 :root 与注释之外的文本,url(...) 内容不碰。
"""
import re
import sys
from pathlib import Path

CSS = Path(__file__).resolve().parent.parent / "static" / "styles.css"

# 语义色族映射表(2026-07-14 拍板:老绿→青绿品牌色,杂红→danger,蓝→info,橙黄→warn,
# 灰→ink/muted,面/线→surface-2/line,白黑→white/black,淡底→*-soft)。键一律小写。
MAPPING = {
    # 品牌绿 → accent / accent-dark
    "#2c8c6f": "var(--accent)", "#1f9c92": "var(--accent)", "#1f9b86": "var(--accent)",
    "#1f6b53": "var(--accent-dark)", "#1f6b52": "var(--accent-dark)",
    "#1f6e57": "var(--accent-dark)", "#2c7a5a": "var(--accent-dark)",
    "#2c6c58": "var(--accent-dark)", "#247a60": "var(--accent-dark)",
    # 成功绿 → ok
    # 分类调色板(p-badge)豁免,勿收编: ea36b(b-first)、a8f3c(b-implant)
    "#1f7a3d": "var(--ok)", "#16a34a": "var(--ok)", "#166534": "var(--ok)",
    "#1a9e63": "var(--ok)", "#1f8a52": "var(--ok)",
    "#1d7a4d": "var(--ok)", "#237a35": "var(--ok)",
    "#1f9b6e": "var(--ok)",
    # 红 → danger
    # 死CSS hover区分豁免,勿收编: #a93226(.danger-button:hover 比底色深一档)
    "#c0392b": "var(--danger)", "#b03030": "var(--danger)", "#b3261e": "var(--danger)",
    "#d9534f": "var(--danger)", "#dc2626": "var(--danger)", "#b42318": "var(--danger)",
    "#991b1b": "var(--danger)", "#c9302c": "var(--danger)",
    "#d23b3b": "var(--danger)", "#d8514c": "var(--danger)", "#c0413c": "var(--danger)",
    "#c0504d": "var(--danger)", "#d92d20": "var(--danger)", "#9b3a3a": "var(--danger)",
    # 蓝 → info
    "#2f6fd6": "var(--info)", "#3b6fd6": "var(--info)", "#3f86d6": "var(--info)",
    "#2c7bd6": "var(--info)", "#2a63c2": "var(--info)", "#1d5cc8": "var(--info)",
    "#1f6bbd": "var(--info)", "#5b8def": "var(--info)", "#6c9bd2": "var(--info)",
    # 橙黄 → warn
    # 分类调色板(p-badge)豁免,勿收编: #e0a230(b-revisit)、#c77d2e(b-restore)
    "#f59e0b": "var(--warn)", "#d97706": "var(--warn)", "#b45309": "var(--warn)",
    "#92600a": "var(--warn)", "#92400e": "var(--warn)", "#8a5a00": "var(--warn)",
    "#b25e00": "var(--warn)", "#b25a00": "var(--warn)", "#b06a00": "var(--warn)",
    "#a06a00": "var(--warn)", "#c47f17": "var(--warn)", "#c4730f": "var(--warn)",
    "#e0a91b": "var(--warn)",
    "#d79a2b": "var(--warn)", "#b8860b": "var(--warn)", "#8a6d3b": "var(--warn)",
    "#c08a2d": "var(--warn)", "#e6a23c": "var(--warn)", "#b54708": "var(--warn)",
    # 深灰墨 → ink(含老绿调深灰)
    "#333": "var(--ink)", "#344054": "var(--ink)", "#374151": "var(--ink)",
    "#111827": "var(--ink)", "#4b5563": "var(--ink)", "#4a5568": "var(--ink)",
    "#14171d": "var(--ink)", "#1f2933": "var(--ink)", "#243029": "var(--ink)",
    "#1f2d2a": "var(--ink)", "#33413a": "var(--ink)", "#3a4a46": "var(--ink)",
    "#3a4a42": "var(--ink)", "#1f3a33": "var(--ink)", "#0b1f1a": "var(--ink)",
    "#465049": "var(--ink)", "#46524b": "var(--ink)", "#425953": "var(--ink)",
    # 中灰 → muted
    "#999": "var(--muted)", "#888": "var(--muted)", "#777": "var(--muted)",
    "#666": "var(--muted)", "#555": "var(--muted)", "#444": "var(--muted)",
    "#6b7280": "var(--muted)", "#667085": "var(--muted)", "#6b766f": "var(--muted)",
    "#6b7686": "var(--muted)", "#6b7a85": "var(--muted)", "#6b7785": "var(--muted)",
    "#8d99a5": "var(--muted)", "#8a98a3": "var(--muted)", "#8a98a5": "var(--muted)",
    "#94a09a": "var(--muted)", "#9aa3ad": "var(--muted)", "#8b93a3": "var(--muted)",
    "#7a8a85": "var(--muted)", "#5b6b64": "var(--muted)", "#5b6b7b": "var(--muted)",
    "#56625b": "var(--muted)", "#5a6b78": "var(--muted)", "#51606e": "var(--muted)",
    "#6c7a72": "var(--muted)", "#4a5750": "var(--muted)", "#4a564f": "var(--muted)",
    # 浅灰面 → surface-2
    "#eee": "var(--surface-2)", "#f0f0f0": "var(--surface-2)", "#e6e6e6": "var(--surface-2)",
    "#ececec": "var(--surface-2)", "#f2f2f2": "var(--surface-2)", "#f4f4f4": "var(--surface-2)",
    "#fafafa": "var(--surface-2)", "#f9fafb": "var(--surface-2)", "#f1f4f7": "var(--surface-2)",
    "#f2f4f7": "var(--surface-2)",
    # 边线(含老绿调边框) → line
    # 深色底浅色文字豁免,勿收编: #e7ebf1/#cfd6df(灯箱深底文字,Phase1 反色token收编)
    "#edf0f4": "var(--line)", "#e5e7eb": "var(--line)",
    "#ddd": "var(--line)", "#d6dde3": "var(--line)", "#dfe5ee": "var(--line)",
    "#d8e0e8": "var(--line)", "#e3e8ef": "var(--line)",
    "#cdd6d2": "var(--line)", "#cfe0da": "var(--line)", "#e3e8e6": "var(--line)",
    "#e2e8e5": "var(--line)", "#e6ece9": "var(--line)", "#e7edea": "var(--line)",
    "#d8e0db": "var(--line)", "#dfe7e3": "var(--line)",
    # 白黑
    "#fff": "var(--white)", "#ffffff": "var(--white)", "#000": "var(--black)",
    # 淡底 → *-soft
    "#fef2f2": "var(--danger-soft)", "#fdecec": "var(--danger-soft)",
    "#fdeaea": "var(--danger-soft)", "#fbeaea": "var(--danger-soft)",
    "#fdf1f0": "var(--danger-soft)", "#fff3f3": "var(--danger-soft)",
    "#fdeceb": "var(--danger-soft)",
    "#e8f3ee": "var(--ok-soft)", "#e8f5ee": "var(--ok-soft)", "#eef4f1": "var(--ok-soft)",
    "#e2eeea": "var(--ok-soft)", "#eaf6f1": "var(--ok-soft)", "#eef7f0": "var(--ok-soft)",
    "#e6f4ee": "var(--ok-soft)", "#dff0e8": "var(--ok-soft)",
    "#fff8e6": "var(--warn-soft)", "#fffbeb": "var(--warn-soft)", "#fbf0d8": "var(--warn-soft)",
    "#eef4fd": "var(--info-soft)", "#f0f7ff": "var(--info-soft)",
    "#f0f5ff": "var(--info-soft)", "#eff6ff": "var(--info-soft)",
}

HEX_TAIL = r"(?![0-9a-fA-F])"   # 防止 #fff 命中 #fff8e6 的前缀


def _spans_to_skip(css):
    """注释、:root 块、url(...) 的区间——这些范围内不做替换。"""
    spans = [m.span() for m in re.finditer(r"/\*.*?\*/", css, flags=re.S)]
    m = re.search(r":root \{.*?\n\}", css, flags=re.S)
    if m:
        spans.append(m.span())
    else:
        sys.exit(":root 未找到,拒绝执行")
    spans += [m.span() for m in re.finditer(r"url\([^)]*\)", css)]
    return spans


def _replace_outside(css, pattern, repl_fn):
    skip = _spans_to_skip(css)
    out, last = [], 0
    for m in re.finditer(pattern, css):
        if any(s <= m.start() < e for s, e in skip):
            continue
        out.append(css[last:m.start()])
        out.append(repl_fn(m))
        last = m.end()
    out.append(css[last:])
    return "".join(out)


def strip_fallbacks(css):
    defined = set(re.findall(r"(--[\w-]+)\s*:", re.search(r":root \{.*?\n\}", css, flags=re.S).group(0)))
    def repl(m):
        tok = m.group(1)
        return f"var({tok})" if tok in defined else m.group(0)
    # [^()]+ 而非 [^)]+:遇到 var(--a, var(--b)) 嵌套备胎时整体不匹配(留着不动),避免截断出残缺括号
    return _replace_outside(css, r"var\(\s*(--[\w-]+),\s*[^()]+\)", repl)


def apply_mapping(css):
    def repl(m):
        return MAPPING.get(m.group(0).lower(), m.group(0))
    return _replace_outside(css, r"#[0-9a-fA-F]{6}" + HEX_TAIL + r"|#[0-9a-fA-F]{3}" + HEX_TAIL, repl)


def report(css):
    from collections import Counter
    stripped = re.sub(r"/\*.*?\*/", "", css, flags=re.S)
    m = re.search(r":root \{.*?\n\}", stripped, flags=re.S)
    outside = stripped[: m.start()] + stripped[m.end():]
    # 计数口径与守卫测试一致:剥注释后、:root 外、8/6/4/3 四分支
    counts = Counter(x.lower() for x in re.findall(
        r"#[0-9a-fA-F]{8}\b|#[0-9a-fA-F]{6}\b|#[0-9a-fA-F]{4}\b|#[0-9a-fA-F]{3}\b", outside))
    print("WHITELIST = {")
    for h in sorted(counts):
        print(f'    "{h}": {counts[h]},')
    print("}")


def main():
    mode = sys.argv[1] if len(sys.argv) > 1 else "--report"
    css = CSS.read_text(encoding="utf-8")
    if mode == "--strip-fallbacks":
        CSS.write_text(strip_fallbacks(css), encoding="utf-8")
    elif mode == "--apply-mapping":
        CSS.write_text(apply_mapping(css), encoding="utf-8")
    elif mode == "--report":
        report(css)
    else:
        sys.exit(f"未知模式: {mode}")


if __name__ == "__main__":
    main()
