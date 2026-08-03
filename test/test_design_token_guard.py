"""设计 token 守卫(UI重构Phase0):styles.css 的硬编码色值只减不增。
1) :root 之外的十六进制色值(键+出现次数)必须 == WHITELIST(长尾冻结清单,Phase3 逐模块燃尽):
   新增色值→红(应改用 var(--token));白名单有已清理项→也红(须同步删,防清单腐烂);
   次数多于记录→也红(堵"复用白名单已有颜色写新硬编码"的绕过道)。
2) styles.css 里引用的 var(--x) 必须在 :root 有定义(抓 var(--accented,#d8e0db) 这类
   引用不存在 token、一直渲染备胎值的真 bug)。
注意:先剥 /* */ 注释再扫描——注释里的工单号(#392 等)不是颜色。
token 合法定义区 = :root 块 + body.theme-pearl 主题定义块(玉瓷主题覆盖值,UI重构Phase1 起)。
规格:docs/superpowers/specs/2026-07-14-ui-design-refactor-design.md Phase 0。
"""
import re
import unittest
from collections import Counter
from pathlib import Path

CSS_PATH = Path(__file__).resolve().parent.parent / "local_app" / "static" / "styles.css"

# 长尾冻结清单 {hex: 允许出现次数}:键和次数都只减不增。由 migrate_css_tokens.py --report 生成。
WHITELIST = {
    # 分类调色板(p-badge)豁免:初/复/植/修徽章各自要不同色区分,不收编到语义token
    "#2ea36b": 1,
    "#5a8f3c": 1,
    "#e0a230": 1,
    "#c77d2e": 1,
    # 深色底浅色文字,Phase1 反色token收编
    "#e7ebf1": 4,
    "#cfd6df": 2,
    # 死CSS hover区分保留
    "#a93226": 1,
    "#566": 1,
    "#6b76c9": 1,
    "#6cb284": 1,
    "#6fb58a": 1,
    "#7a44c4": 1,
    "#7a8794": 1,
    "#7fb5d5": 2,
    "#8a7a45": 1,
    "#8a93a8": 1,
    "#8b9aa8": 1,
    "#8fd0ca": 1,
    "#9a85c9": 1,
    "#9aa5a0": 1,
    "#9aa6a1": 1,
    "#9aa6b2": 1,
    "#9aa8a3": 2,
    "#9b59b6": 1,
    "#9fb0c0": 1,
    "#aaa": 2,
    "#aab3c0": 1,
    "#b3bcc4": 1,
    "#b6c0ca": 1,
    "#b6ddcf": 2,
    "#b9c6c0": 1,
    "#b9c6cf": 2,
    "#bbb": 2,
    "#bbf7d0": 1,
    "#bfdbfe": 1,
    "#bfe0d4": 2,
    "#bfe2df": 1,
    "#c0408a": 1,
    "#c0573a": 1,
    "#c3c9d2": 1,
    "#c4cedb": 1,
    "#c8e9e6": 1,
    "#ccc": 1,
    "#ccd6e0": 1,
    "#cfe0d8": 1,
    "#cfe0ff": 1,
    "#cfe3d9": 1,
    "#cfe8dd": 1,
    "#cfe9df": 2,
    "#cfeee9": 1,
    "#d2876c": 1,
    "#d5ead7": 1,
    "#d6e4ff": 1,
    "#d6e6e0": 1,
    "#d7f0dd": 1,
    "#d8b4fe": 1,
    "#d98b8b": 1,
    "#e0cfa0": 1,
    "#e3e8ec": 1,
    "#e3e8ee": 1,
    "#e4b3b3": 1,
    "#e6b3b1": 1,
    "#e6d3a8": 1,
    "#e6ddc7": 1,
    "#e6f2ed": 1,
    "#e6f5ea": 1,
    "#e7ecf2": 1,
    "#e7f1ed": 1,
    "#e7f6ee": 1,
    "#e8743b": 1,
    "#e87fa6": 1,
    "#e8f0fd": 1,
    "#e8f1fb": 1,
    "#ead6d6": 1,
    "#eaf4ef": 1,
    "#eceeed": 1,
    "#eef1ef": 7,
    "#eef1f4": 7,
    "#eef2f5": 2,
    "#eef2f6": 1,
    "#eef3f6": 3,
    "#eef4f2": 1,
    "#eef6f2": 2,
    "#eef7f3": 1,
    "#eefaf9": 1,
    "#f0c89a": 1,
    "#f0d9a8": 1,
    "#f0e0c8": 1,
    "#f0f3f1": 1,
    "#f0fdf4": 2,
    "#f1ebfb": 1,
    "#f2f7f4": 3,
    "#f2faf7": 1,
    "#f2fbf3": 1,
    "#f2fbfa": 1,
    "#f3d6d2": 1,
    "#f3d9d9": 1,
    "#f3e4b8": 2,
    "#f3f4f5": 2,
    "#f3f4f6": 1,
    "#f3f5f7": 4,
    "#f3f8f5": 1,
    "#f3faf7": 5,
    "#f4f6f7": 3,
    "#f4f6f9": 1,
    "#f4f7f6": 1,
    "#f4faf7": 2,
    "#f5f7f6": 1,
    "#f5f7f9": 2,
    "#f5f8fa": 1,
    "#f6f8fa": 1,
    "#f6f8fb": 1,
    "#f6faf8": 6,
    "#f6fffd": 1,
    "#f7f8fa": 1,
    "#f7f9fb": 4,
    "#f7faf8": 1,
    "#f7faf9": 6,
    "#f8fbfa": 1,
    "#faf5ff": 1,
    "#fafbfb": 2,
    "#fafbfc": 4,
    "#fafbfd": 1,
    "#fafcfe": 1,
    "#fafdfc": 2,
    "#fbf6e9": 1,
    "#fbfcfe": 1,
    "#fbfdff": 1,
    "#fce9f1": 1,
    "#fde68a": 1,
    "#fdf0e3": 1,
    "#fdf3e3": 1,
    "#fdf3e7": 1,
    "#fdf6ec": 1,
    "#fdf7e9": 1,
    "#fecaca": 1,
    "#fee4e2": 1,
    "#fef3c7": 1,
    "#ffd9a8": 2,
    "#fff3e0": 1,
    "#fff6ec": 1,
    "#fff7e6": 1,
    "#fff7ed": 1,
    "#fff8f0": 1,
    "#fff9ec": 1,
    "#fffdf6": 1,
}


def _strip_comments(css: str) -> str:
    return re.sub(r"/\*.*?\*/", "", css, flags=re.S)


def _split_root(css: str):
    """返回 (定义区内容, 定义区之外的全文)。
    定义区 = :root 块 + 首个 body.theme-pearl 主题定义块(若存在;玉瓷主题覆盖值,UI重构Phase1 起)。
    选择器锚定为 `body.theme-pearl `+`{`,不匹配 `body.theme-pearl .side-nav {` 这类后代选择器规则。
    """
    m = re.search(r":root \{.*?\n\}", css, flags=re.S)
    assert m, ":root 块未找到"
    root = m.group(0)
    outside = css[: m.start()] + css[m.end():]
    pearl_m = re.search(r"\nbody\.theme-pearl \{.*?\n\}", outside, flags=re.S)
    if pearl_m:
        root += pearl_m.group(0)
        outside = outside[: pearl_m.start()] + outside[pearl_m.end():]
    return root, outside


HEX_RE = re.compile(r"#[0-9a-fA-F]{8}\b|#[0-9a-fA-F]{6}\b|#[0-9a-fA-F]{4}\b|#[0-9a-fA-F]{3}\b")


class DesignTokenGuardTest(unittest.TestCase):
    def setUp(self):
        self.css = _strip_comments(CSS_PATH.read_text(encoding="utf-8"))
        self.root, self.outside = _split_root(self.css)

    def test_hex_outside_root_matches_whitelist(self):
        counts = Counter(m.lower() for m in HEX_RE.findall(self.outside))
        unexpected = set(counts) - set(WHITELIST)
        stale = set(WHITELIST) - set(counts)
        self.assertFalse(
            unexpected,
            "styles.css 新增硬编码色值,应改用 var(--token):\n" + "\n".join(sorted(unexpected)),
        )
        self.assertFalse(
            stale,
            "白名单存在已清理的残留项,请从 WHITELIST 删除:\n" + "\n".join(sorted(stale)),
        )
        # 次数棘轮:每个色值出现次数只减不增,堵"复用白名单已有颜色写新硬编码"的绕过道
        drift = {h: (counts[h], WHITELIST[h]) for h in counts if counts[h] != WHITELIST[h]}
        self.assertFalse(
            drift,
            "色值出现次数与白名单不符 hex: (实际, 白名单)——新增用法改 var(--token),清理后同步减记:\n"
            + "\n".join(f"{h}: {v}" for h, v in sorted(drift.items())),
        )

    def test_var_references_all_defined(self):
        defined = set(re.findall(r"(--[\w-]+)\s*:", self.root))
        referenced = set(re.findall(r"var\(\s*(--[\w-]+)", self.css))
        missing = referenced - defined
        self.assertFalse(
            missing,
            ":root 未定义却被 var() 引用的 token(备胎值在裸奔):\n" + "\n".join(sorted(missing)),
        )
