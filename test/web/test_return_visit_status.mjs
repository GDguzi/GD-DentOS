// 扫荡#400:回访状态码不能裸显示「0」。沙箱 eval returnVisitStatusText 验证映射。
import { test } from "node:test";
import assert from "node:assert";
import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import { dirname, join } from "node:path";
import vm from "node:vm";

const here = dirname(fileURLToPath(import.meta.url));
const src = readFileSync(join(here, "..", "..", "local_app", "static", "helpers.js"), "utf8");
const start = src.indexOf("function returnVisitStatusText");
const code = src.slice(start, src.indexOf("\n}", start) + 2);
const sandbox = {};
vm.createContext(sandbox);
vm.runInContext(code + "\nthis.f = returnVisitStatusText;", sandbox);

test("0/空 → 待回访(不再裸显示 0)", () => {
  assert.strictEqual(sandbox.f("0"), "待回访");
  assert.strictEqual(sandbox.f(""), "待回访");
  assert.strictEqual(sandbox.f(null), "待回访");
});

test("已回访各码归一", () => {
  for (const s of ["done", "已回访", "完成", "4"]) assert.strictEqual(sandbox.f(s), "已回访");
});

test("其他状态原样", () => {
  assert.strictEqual(sandbox.f("已取消"), "已取消");
});
