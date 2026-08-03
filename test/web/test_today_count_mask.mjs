// #528:无 billing.view 时 summary.today_settlements=null——入口必须显掩码,
// 不得把"无权限查看"渲染成"今日结算 0"。沙箱 eval todayCountOrMask 锁住口径。
import { test } from "node:test";
import assert from "node:assert";
import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import { dirname, join } from "node:path";
import vm from "node:vm";

const here = dirname(fileURLToPath(import.meta.url));
const src = readFileSync(join(here, "..", "..", "local_app", "static", "today_work.js"), "utf8");

const start = src.indexOf("function todayCountOrMask");
const decl = src.slice(start, src.indexOf("}", start) + 1);

const sandbox = { formatCount: (v) => String(v) };
vm.createContext(sandbox);
vm.runInContext(decl + "\nthis.todayCountOrMask = todayCountOrMask;", sandbox);

test("null(无权限降级) → 掩码,不是 0", () => {
  assert.strictEqual(sandbox.todayCountOrMask(null), "******");
  assert.strictEqual(sandbox.todayCountOrMask(undefined), "******");
});

test("有权限的真实计数照常显示,0 也是真 0", () => {
  assert.strictEqual(sandbox.todayCountOrMask(0), "0");
  assert.strictEqual(sandbox.todayCountOrMask(7), "7");
});

// #532:点开右侧简表(renderTodaySimpleShell)同样要按 total===null 显掩码，
// 之前只有左侧入口显掩码，点进去的简表徽标/空态仍按 rows.length=0 显"0/暂无数据"。
const shellStart = src.indexOf("function renderTodaySimpleShell");
const shellCode = src.slice(shellStart, src.indexOf("\n}", shellStart) + 2);
function renderShell(total) {
  const shellSandbox = {
    escapeAttr: s => String(s), escapeHtml: s => String(s),
    formatCount: v => (v == null ? "0" : String(v)),
  };
  vm.createContext(shellSandbox);
  vm.runInContext(shellCode + "\nthis.__f = renderTodaySimpleShell;", shellSandbox);
  const btn = {dataset: {todayView: "billing"}};
  return shellSandbox.__f(btn, "今日结算", [], () => "", total, "", "");
}

test("#532 total=null(无权限) → 简表徽标显掩码、空态显无权限查看，不是 0/暂无数据", () => {
  const html = renderShell(null);
  assert.match(html, /today-card-badge">\*{6}</);
  assert.match(html, /今日结算简表/);
  assert.match(html, /无权限查看/);
  assert.doesNotMatch(html, /today-card-badge">0</);
});

test("#532 total=0(真的是0条,有权限) → 简表照常显0/暂无数据,不受掩码逻辑误伤", () => {
  const html = renderShell(0);
  assert.match(html, /today-card-badge">0</);
  assert.match(html, /暂无数据/);
  assert.doesNotMatch(html, /无权限查看/);
});
