// 今日事项简表被后端 limit 截断时(生日50/共68),角标要用总数并注明「显示前 N 条」,
// 入口数和右侧角标不能无解释地不一致。沙箱 eval renderTodaySimpleShell 验证。
import { test } from "node:test";
import assert from "node:assert";
import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import { dirname, join } from "node:path";
import vm from "node:vm";

const here = dirname(fileURLToPath(import.meta.url));
const src = readFileSync(join(here, "..", "..", "local_app", "static", "today_work.js"), "utf8");

const start = src.indexOf("function renderTodaySimpleShell");
const decl = src.slice(start, src.indexOf("\nfunction ", start + 1));

const sandbox = {
  escapeHtml: (s) => String(s),
  escapeAttr: (s) => String(s),
  formatCount: (n) => String(n),
};
vm.createContext(sandbox);
vm.runInContext(decl + "\nthis.renderTodaySimpleShell = renderTodaySimpleShell;", sandbox);

const btn = { dataset: {} };
const rows = [{}, {}];
const renderer = () => "<div>row</div>";

test("截断:总数68只给2条 → 角标68 + 显示前2条/共68", () => {
  const html = sandbox.renderTodaySimpleShell(btn, "今日生日", rows, renderer, 68);
  assert.ok(html.includes('class="today-card-badge">68<'));
  assert.ok(html.includes("显示前 2 条 / 共 68"));
});

test("无总数 → 角标=行数,无说明", () => {
  const html = sandbox.renderTodaySimpleShell(btn, "今日生日", rows, renderer, undefined);
  assert.ok(html.includes('class="today-card-badge">2<'));
  assert.ok(!html.includes("显示前"));
});

test("总数=行数(未截断) → 角标=行数,无说明", () => {
  const html = sandbox.renderTodaySimpleShell(btn, "今日生日", rows, renderer, 2);
  assert.ok(html.includes('class="today-card-badge">2<'));
  assert.ok(!html.includes("显示前"));
});
