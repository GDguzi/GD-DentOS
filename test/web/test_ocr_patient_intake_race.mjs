import { test } from "node:test";
import assert from "node:assert";
import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import { dirname, join } from "node:path";
import vm from "node:vm";

const here = dirname(fileURLToPath(import.meta.url));
const src = readFileSync(join(here, "..", "..", "local_app", "static", "today_work.js"), "utf8");

function deferred() {
  let resolve;
  const promise = new Promise(r => { resolve = r; });
  return {promise, resolve};
}

function ocrResponse(displayName) {
  return {
    ok: true,
    status: 200,
    json: async () => ({
      fields: {display_name: displayName},
      confident: ["display_name"],
      lines: [],
      warnings: [],
      elapsed_ms: 1,
    }),
  };
}

function makeHarness() {
  const nodes = new Map();
  const node = id => {
    if (!nodes.has(id)) {
      nodes.set(id, {
        id,
        value: "",
        textContent: "",
        innerHTML: "",
        hidden: false,
        disabled: false,
        style: {},
        classList: {add() {}, remove() {}, toggle() {}},
      });
    }
    return nodes.get(id);
  };
  node("npName").value = "手工姓名";
  node("npPhone").value = "13800000000";
  node("newPatientModal").hidden = false;
  const ocrGates = [];
  const ocrSignals = [];
  const patientPosts = [];
  const sandbox = {
    console,
    AbortController,
    document: {
      getElementById: id => node(id),
      querySelector: selector => selector.includes("npSex") ? {value: "男"} : null,
      querySelectorAll: () => [],
      createElement: () => node("created"),
      body: {appendChild() {}},
    },
    fetch: (url, opts = {}) => {
      if (url === "/api/ocr/patient-card") {
        const gate = deferred();
        ocrGates.push(gate);
        ocrSignals.push(opts.signal);
        return gate.promise;
      }
      if (url === "/api/patients") {
        patientPosts.push(JSON.parse(opts.body));
        return Promise.resolve({ok: true, json: async () => ({patient_identity: "p1"})});
      }
      return Promise.resolve({ok: true, json: async () => ({available: true})});
    },
    escapeHtml: s => String(s ?? ""),
    escapeAttr: s => String(s ?? ""),
    bjToday: () => new Date("2026-07-22T00:00:00Z"),
    bindDictInputs() {},
    loadTodayWork() {},
    encodeURIComponent,
    FormData: class { append() {} },
    URL: {createObjectURL: () => "blob:test"},
    window: {confirm: () => true},
    setTimeout,
    clearTimeout,
  };
  vm.createContext(sandbox);
  vm.runInContext(src, sandbox);
  return {sandbox, node, ocrGates, ocrSignals, patientPosts};
}

function pickInput(name) {
  return {value: name, files: [{type: "image/jpeg", name}]};
}

test("OCR 在途时保存 fail-closed，不得快照旧字段 POST 患者", async () => {
  const h = makeHarness();
  const pending = h.sandbox.window.onNewPatientOcrPick(pickInput("a.jpg"));
  await Promise.resolve();
  await h.sandbox.window.submitNewPatient();
  const postCountWhileBusy = h.patientPosts.length;
  const disabledWhileBusy = h.node("npSavePatientBtn").disabled;
  h.ocrGates[0].resolve(ocrResponse("OCR姓名"));
  await pending;
  assert.strictEqual(postCountWhileBusy, 0, "识别完成前不得创建患者");
  assert.strictEqual(disabledWhileBusy, true, "识别中保存按钮必须禁用");
});

test("关闭新增患者会作废并 abort OCR，迟到响应不得填入表单", async () => {
  const h = makeHarness();
  const pending = h.sandbox.window.onNewPatientOcrPick(pickInput("close.jpg"));
  await Promise.resolve();
  h.sandbox.window.closeNewPatient();
  const signal = h.ocrSignals[0];
  h.ocrGates[0].resolve(ocrResponse("迟到姓名"));
  await pending;
  assert.ok(signal, "OCR fetch 必须携带 AbortSignal");
  assert.strictEqual(signal.aborted, true, "关闭弹窗必须 abort 在途 OCR");
  assert.strictEqual(h.node("npName").value, "手工姓名", "关闭后的迟到响应不得改表单");
});

test("A 迟到的 finally 不得解除 B 的 busy", async () => {
  const h = makeHarness();
  const pendingA = h.sandbox.window.onNewPatientOcrPick(pickInput("a.jpg"));
  await Promise.resolve();
  const pendingB = h.sandbox.window.onNewPatientOcrPick(pickInput("b.jpg"));
  await Promise.resolve();
  const signalA = h.ocrSignals[0];
  h.ocrGates[0].resolve(ocrResponse("A姓名"));
  await pendingA;
  const disabledAfterA = h.node("npSavePatientBtn").disabled;
  h.ocrGates[1].resolve(ocrResponse("B姓名"));
  await pendingB;
  assert.ok(signalA, "OCR fetch 必须携带 AbortSignal");
  assert.strictEqual(signalA.aborted, true, "开始 B 时必须 abort A");
  assert.strictEqual(disabledAfterA, true, "A 的 finally 不能解锁仍在途的 B");
  assert.strictEqual(h.node("npSavePatientBtn").disabled, false, "B 完成后才能恢复保存");
  assert.strictEqual(h.node("npName").value, "B姓名");
});
