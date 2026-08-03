// #542 新合同：「挂号并继续」统一调用原子 checkInPatient，
// 不再 GET /today-visit 后 POST /api/appointments，也不伪造项目名「挂号」。
// 跑：node --test test/web/test_register_today_recheck_542.mjs
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

function makeCtx(checkInResult) {
  const calls = { checkIns: [], alerts: [], rendered: 0, synced: 0 };
  const ctx = {
    calls,
    workspacePatientId: "p1",
    orderModel: { doctor_name: "" },
    _orderTodayVisit: { has_today: false },
    orderEditorEl: () => ({ querySelector: () => ({ value: "王医生" }) }),
    orderSyncFromDom: () => { calls.synced += 1; },
    renderTreatmentOrderEditor: () => { calls.rendered += 1; },
    window: { alert: m => calls.alerts.push(String(m)) },
    checkInPatient: async options => {
      calls.checkIns.push(options);
      return checkInResult;
    },
    fetch: () => { throw new Error("挂号并继续不得直接 fetch"); },
    encodeURIComponent,
    JSON,
  };
  vm.createContext(ctx);
  vm.runInContext(fnCode, ctx);
  return ctx;
}

test("挂号并继续调用统一快速挂号，医生只带入处置", async () => {
  const ctx = makeCtx({appointment: {appointment_id: "a1", status: "已到诊", doctor_name: ""}});
  await vm.runInContext("registerTodayThenOrder()", ctx);
  assert.deepStrictEqual(JSON.parse(JSON.stringify(ctx.calls.checkIns)), [{
    patientIdentity: "p1",
    displayName: "",
    openTriageAfter: false,
    switchToToday: false,
    showSuccess: false,
  }]);
  assert.strictEqual(ctx._orderTodayVisit.has_today, true);
  assert.strictEqual(ctx._orderTodayVisit.appointment_id, "a1");
  assert.strictEqual(ctx.orderModel.doctor_name, "王医生", "界面选中医生只带入处置");
  assert.strictEqual(ctx.calls.synced, 1);
  assert.strictEqual(ctx.calls.rendered, 1, "应重渲去掉横幅");
});

test("用户取消多候选时保留原处置上下文", async () => {
  const ctx = makeCtx(null);
  await vm.runInContext("registerTodayThenOrder()", ctx);
  assert.strictEqual(ctx.calls.checkIns.length, 1);
  assert.strictEqual(ctx._orderTodayVisit.has_today, false);
  assert.strictEqual(ctx.orderModel.doctor_name, "");
  assert.strictEqual(ctx.calls.synced, 0);
  assert.strictEqual(ctx.calls.rendered, 0);
});

test("挂号并继续源码不再保留旧 GET→POST 竞态和伪项目", () => {
  assert.doesNotMatch(fnCode, /today-visit/);
  assert.doesNotMatch(fnCode, /\/api\/appointments/);
  assert.doesNotMatch(fnCode, /item_name\s*:\s*["']挂号["']/);
});
