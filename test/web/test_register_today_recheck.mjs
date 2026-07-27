// 回归:「挂号并继续」不得盲建同日队列。
// 场景:横幅(该患者今天还没挂号)弹出后,前台/其他设备/日期口径修正可能已让患者有了今日挂号;
// 点「挂号并继续」必须先重查 /today-visit——已有今日挂号就直接并入当前就诊上下文(横幅消失、
// 医生带入),绝不再 POST /api/appointments 造第二条"挂号"队列行。
// 沙箱抽 registerTodayThenOrder 真跑,stub fetch 断言三条路径。
// 跑：node --test test/web/test_register_today_recheck.mjs
import { test } from "node:test";
import assert from "node:assert";
import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import { dirname, join } from "node:path";
import vm from "node:vm";

const here = dirname(fileURLToPath(import.meta.url));
const src = readFileSync(join(here, "..", "..", "local_app", "static", "treatment_order_editor.js"), "utf8");

const start = src.indexOf("async function registerTodayThenOrder");
assert.ok(start > 0, "应能定位 registerTodayThenOrder");
const fnCode = src.slice(start, src.indexOf("\n}", start) + 2);

function makeCtx(fetchImpl) {
  const calls = { posts: [], alerts: [], rendered: 0 };
  const ctx = {
    calls,
    workspacePatientId: "p1",
    orderModel: { doctor_name: "" },
    _orderTodayVisit: { has_today: false },
    orderEditorEl: () => ({ querySelector: () => ({ value: "陈医生" }) }),
    bjNowStr: () => "2026-07-08 10:30:00",
    orderSyncFromDom: () => {},
    renderTreatmentOrderEditor: () => { calls.rendered += 1; },
    evictVisitsCache: () => {},
    window: { alert: m => calls.alerts.push(String(m)) },
    fetch: fetchImpl,
    encodeURIComponent,
    JSON,
  };
  vm.createContext(ctx);
  vm.runInContext(fnCode, ctx);
  return ctx;
}

test("重查发现已有今日挂号 → 并入上下文,不再 POST 建号", async () => {
  const ctx = makeCtx(async (url, opts) => {
    if (opts && opts.method === "POST") { ctx.calls.posts.push(url); return { ok: true, json: async () => ({}) }; }
    return { ok: true, json: async () => ({ has_today: true, doctor_name: "前台已挂的医生" }) };
  });
  await vm.runInContext("registerTodayThenOrder()", ctx);
  assert.strictEqual(ctx.calls.posts.length, 0, "已有今日挂号时不得再建预约");
  assert.strictEqual(ctx._orderTodayVisit.has_today, true);
  assert.strictEqual(ctx.orderModel.doctor_name, "前台已挂的医生");
  assert.ok(ctx.calls.rendered >= 1, "应重渲去掉横幅");
});

test("重查确认没挂号 → 照常 POST 建今日挂号(北京时间戳)", async () => {
  let postBody = null;
  const ctx = makeCtx(async (url, opts) => {
    if (opts && opts.method === "POST") { postBody = JSON.parse(opts.body); return { ok: true, json: async () => ({}) }; }
    return { ok: true, json: async () => ({ has_today: false }) };
  });
  await vm.runInContext("registerTodayThenOrder()", ctx);
  assert.ok(postBody, "无今日挂号时应建号");
  assert.strictEqual(postBody.start_time, "2026-07-08 10:30");
  assert.strictEqual(postBody.doctor_name, "陈医生");
});

test("重查网络异常 → 提示并停下,不盲建", async () => {
  const ctx = makeCtx(async (url, opts) => {
    if (opts && opts.method === "POST") { ctx.calls.posts.push(url); return { ok: true, json: async () => ({}) }; }
    throw new Error("network down");
  });
  await vm.runInContext("registerTodayThenOrder()", ctx);
  assert.strictEqual(ctx.calls.posts.length, 0, "重查失败不得盲目建号");
  assert.ok(ctx.calls.alerts.length >= 1, "应提示用户");
});
