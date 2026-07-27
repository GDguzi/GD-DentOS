// 今日工作台操作按钮高亮口径(用户校准):深色=今天这次就诊真做了这件事,历史/未来/同步触碰不算。
//   收费=paid_today(今天收款,历史欠费不点亮) / 回访=return_visit_today(今天本地新增回访) /
//   预约=rebooked_today(今天本地新增预约)。守 tqOps 源码不变量,防 UI 调整时再退化。
// 跑：node --test test/web/test_today_billing_highlight.mjs
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

test("收费高亮只认今天收款 paid_today(历史欠费不点亮)", () => {
  const body = fnBody("tqOps");
  assert.match(body, /bill:\s*!!row\.paid_today/, "done.bill 应只看今天 paid_today");
  assert.ok(!/bill:\s*hasBilling/.test(body), "不应再用含欠费的 hasBilling 口径");
});

test("回访=今天新增 return_visit_today,预约=今天新增 rebooked_today", () => {
  const body = fnBody("tqOps");
  assert.match(body, /visit:\s*row\.return_visit_today/, "回访按今天新增回访");
  assert.match(body, /rebook:\s*row\.rebooked_today/, "预约按今天新增预约(rebooked_today),不再用 has_future_appt");
  assert.ok(!/rebook:\s*row\.has_future_appt/.test(body), "预约不再用 has_future_appt(历史/未来旧约不点亮)");
});

test("删除/取消锁仍只认 已处置/已收费(不被其它口径放大)", () => {
  const body = fnBody("tqOps");
  assert.match(body, /lockDel\s*=\s*!!\(row\.treated_today\s*\|\|\s*row\.paid_today\)/);
  assert.match(body, /lockCancel\s*=\s*!!row\.paid_today/);
});
