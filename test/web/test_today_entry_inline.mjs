// 回归:今日工作台左入口点击应在右侧壳内出简表,不跳管理页;高亮只亮当前一个。
// 无 DOM 测试栈——守源码不变量(跳转语义移到「管理页查看全部」二级入口)。
// 跑：node --test test/web/test_today_entry_inline.mjs
import { test } from "node:test";
import assert from "node:assert";
import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import { dirname, join } from "node:path";

const here = dirname(fileURLToPath(import.meta.url));
const src = readFileSync(join(here, "..", "..", "local_app", "static", "today_work.js"), "utf8");

function fnBody(name) {
  const i = src.indexOf("function " + name);
  assert.ok(i >= 0, `应能定位 ${name}`);
  const rest = src.slice(i);
  const next = rest.indexOf("\nfunction ", 1);
  return next > 0 ? rest.slice(0, next) : rest;
}

test("点入口不再跳管理页:openTodayWorkEntry 不调 switchWorkspaceView", () => {
  const body = fnBody("openTodayWorkEntry");
  assert.ok(!/switchWorkspaceView/.test(body),
    "入口点击应在右侧壳出简表,跳转语义只属于 openTodayWorkEntryFull");
});

test("高亮只亮当前一个:先清全部 .today-work-entry 的 active 再加到 button", () => {
  const body = fnBody("openTodayWorkEntry");
  assert.match(body, /querySelectorAll\(["']\.today-work-entry["']\)[\s\S]*remove\(["']active["']\)/,
    "应先清掉所有入口的 active");
  assert.match(body, /button\.classList\.add\(["']active["']\)/,
    "再把 active 加到被点的入口");
});

test("简表渲染进右侧壳 #todayWorkMain", () => {
  const body = fnBody("openTodayWorkEntry");
  assert.match(body, /getElementById\(["']todayWorkMain["']\)/);
});

test("管理页查看全部仍保留跳转能力:openTodayWorkEntryFull 调 switchWorkspaceView", () => {
  const body = fnBody("openTodayWorkEntryFull");
  assert.match(body, /switchWorkspaceView/);
});

test("入口身份用 data-today-entry,不再用 data-today-action-view(避免同 view 双高亮)", () => {
  const list = fnBody("todayWorkEntryList");
  assert.match(list, /data-today-entry=/, "入口应带身份键 data-today-entry");
  assert.ok(!/data-today-action-view=/.test(list),
    "入口不应再用 data-today-action-view(那会被 syncTodayActions 按 view 一起点亮)");
});
