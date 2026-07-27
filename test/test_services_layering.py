"""Phase4 分层铁律守卫:services/ 是纯聚合层——不许 import FastAPI/auth、不许抛 HTTPException、
不许写库。规矩见 services/finance_service.py 模块注释;新增服务模块自动纳管。"""
import ast
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SERVICES = ROOT / "local_app" / "services"

_BANNED_MODULES = ("fastapi", "local_app.auth")
_WRITE_VERBS = ("insert ", "update ", "delete ", "replace ", "create table", "drop ", "alter ")


class ServicesLayeringTest(unittest.TestCase):
    def _modules(self):
        return [p for p in SERVICES.glob("*.py") if p.name != "__init__.py"]

    def test_no_http_or_auth_imports(self):
        bad = []
        for p in self._modules():
            tree = ast.parse(p.read_text(encoding="utf-8"))
            for node in ast.walk(tree):
                mods = []
                if isinstance(node, ast.Import):
                    mods = [a.name for a in node.names]
                elif isinstance(node, ast.ImportFrom) and node.level == 0:
                    mods = [node.module or ""]
                for m in mods:
                    if any(m == b or m.startswith(b + ".") for b in _BANNED_MODULES):
                        bad.append(f"{p.name}:{node.lineno} import {m}")
        self.assertEqual(bad, [], "服务层不许碰 FastAPI/auth(HTTP与权限归路由层):\n" + "\n".join(bad))

    def test_no_http_exception(self):
        # AST 查真实使用(Name/Attribute),不搜字面——模块注释里写规矩提到这词不算违规
        bad = []
        for p in self._modules():
            tree = ast.parse(p.read_text(encoding="utf-8"))
            for node in ast.walk(tree):
                if (isinstance(node, ast.Name) and node.id == "HTTPException") or \
                   (isinstance(node, ast.Attribute) and node.attr == "HTTPException"):
                    bad.append(f"{p.name}:{node.lineno}")
        self.assertEqual(bad, [], "服务层不许抛 HTTPException(参数校验归路由层):\n" + "\n".join(bad))

    def test_read_only(self):
        # 只读聚合:SQL 里不许出现写动词(字符串级粗筛,误报再白名单)
        bad = []
        for p in self._modules():
            src = p.read_text(encoding="utf-8").lower()
            for verb in _WRITE_VERBS:
                if f'"{verb}' in src or f"'{verb}" in src or f'execute("{verb}' in src:
                    bad.append(f"{p.name}: {verb.strip()}")
        self.assertEqual(bad, [], "服务层只读:发现疑似写库动词")


if __name__ == "__main__":
    unittest.main()
