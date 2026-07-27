// 回归:预约新增/取消保存成功后，今日工作台没有即时刷新(需手点"刷新今日")。
// 抽出 submitNewAppointment / cancelAppointment / _refreshVisitsAfterApptChange 源码，
// stub document/fetch/window，沙箱 eval 真测保存成功后有没有调 loadTodayWork。
// 跑：node --test test/web/test_appointment_save_refresh.mjs
import { test } from "node:test";
import assert from "node:assert";
import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import { dirname, join } from "node:path";
import vm from "node:vm";

const here = dirname(fileURLToPath(import.meta.url));
const src = readFileSync(join(here, "..", "..", "local_app", "static", "appointment_editor.js"), "utf8");

function extract(fnHeader) {
  const start = src.indexOf(fnHeader);
  assert.ok(start > 0, `应能定位 ${fnHeader}`);
  const end = src.indexOf("\n}", start) + 2;
  return src.slice(start, end);
}

const submitCode = extract("async function submitNewAppointment");
const cancelCode = extract("async function cancelAppointment");
const refreshVisitsCode = extract("function _refreshVisitsAfterApptChange");

function makeSandbox() {
  const calls = {loadTodayWork: 0, closeNewAppointment: 0, reload: null};
  const stubDoc = {
    getElementById: id => {
      if (id === "apptStart") return {value: "2026-07-05 09:00"};
      if (id === "apptDoctor") return {value: "医生甲"};
      if (id === "apptItem") return {value: "洁牙"};
      return {value: "", textContent: ""};
    },
    querySelectorAll: () => [],
    querySelector: () => null,
  };
  const sandbox = {
    _apptPicked: {patient_identity: "p1"},
    _apptEditId: null,
    document: stubDoc,
    window: {confirm: () => true, alert: () => {}},
    appPrompt: async () => "补交欠款",
    fetch: async () => ({ok: true, json: async () => ({})}),
    closeNewAppointment: () => { calls.closeNewAppointment++; },
    reloadModuleFromFirstPage: name => { calls.reload = name; },
    evictVisitsCache: () => {},
    loadTodayWork: () => { calls.loadTodayWork++; },
  };
  vm.createContext(sandbox);
  vm.runInContext(
    refreshVisitsCode + "\n" + submitCode + "\n" + cancelCode +
    "\nthis.__submit = submitNewAppointment; this.__cancel = cancelAppointment;",
    sandbox,
  );
  return {sandbox, calls};
}

test("新增预约保存成功 → 应刷新今日工作台(loadTodayWork)", async () => {
  const {sandbox, calls} = makeSandbox();
  await sandbox.__submit();
  assert.strictEqual(calls.loadTodayWork, 1, "保存成功后应调用 loadTodayWork 刷新今日工作台，不能只等用户手点刷新");
});

test("取消预约成功 → 应刷新今日工作台(loadTodayWork)", async () => {
  const {sandbox, calls} = makeSandbox();
  await sandbox.__cancel("appt1");
  assert.strictEqual(calls.loadTodayWork, 1, "取消成功后应调用 loadTodayWork，不能让今日队列停在旧状态(如仍显示已到达)");
});
