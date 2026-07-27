// 回归:病历编辑层「未保存拦截」守卫。无 DOM 测试栈——抽出 markMedicalEditorDirty +
// closeMedicalEditorOverlay 源码,用 stub document/window 在沙箱 eval,真测脏守卫行为。
// 跑：node --test test/web/test_medical_editor_dirty_guard.mjs
import { test } from "node:test";
import assert from "node:assert";
import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import { dirname, join } from "node:path";
import vm from "node:vm";

const here = dirname(fileURLToPath(import.meta.url));
const src = readFileSync(join(here, "..", "..", "local_app", "static", "workspace_tabs.js"), "utf8");

// 抽出两个守卫函数源码(从 markMedicalEditorDirty 到 medicalVisitDate 之前)
const start = src.indexOf("function markMedicalEditorDirty");
const end = src.indexOf("function medicalVisitDate");
assert.ok(start > 0 && end > start, "应能定位 markMedicalEditorDirty / closeMedicalEditorOverlay 源码");
const code = src.slice(start, end);

// 构造一个最小 overlay + 沙箱
function makeSandbox(initialDirty, confirmReturns) {
  const overlay = {
    hidden: false,
    dataset: { dirty: initialDirty },
    querySelector: () => ({ innerHTML: "x" }),
  };
  const sandbox = {
    document: {
      getElementById: id => (id === "medicalEditorOverlay" ? overlay : null),
      querySelector: () => null,   // 非 force 关闭后会查病历面板重渲,返回 null 即跳过
      body: { classList: { add() {}, remove() {} } },
    },
    window: { confirm: () => confirmReturns },
    closeToothSelector: () => {},
  };
  vm.createContext(sandbox);
  vm.runInContext(code + "\nthis.__close = closeMedicalEditorOverlay; this.__mark = markMedicalEditorDirty;", sandbox);
  return { sandbox, overlay };
}

test("脏 + 用户取消确认 → 不关闭(留住未保存病历)", () => {
  const { sandbox, overlay } = makeSandbox("1", false);
  sandbox.__close();
  assert.strictEqual(overlay.hidden, false, "确认取消时编辑层应保持打开");
});

test("脏 + 用户确认放弃 → 关闭", () => {
  const { sandbox, overlay } = makeSandbox("1", true);
  sandbox.__close();
  assert.strictEqual(overlay.hidden, true, "确认放弃后应关闭");
});

test("脏 + force=true(保存成功路径) → 直接关闭,不弹确认", () => {
  let confirmCalled = false;
  const overlay = { hidden: false, dataset: { dirty: "1" }, querySelector: () => ({ innerHTML: "x" }) };
  const sandbox = {
    document: { getElementById: () => overlay, querySelector: () => null, body: { classList: { add() {}, remove() {} } } },
    window: { confirm: () => { confirmCalled = true; return false; } },
    closeToothSelector: () => {},
  };
  vm.createContext(sandbox);
  vm.runInContext(code + "\nthis.__close = closeMedicalEditorOverlay;", sandbox);
  sandbox.__close(true);
  assert.strictEqual(overlay.hidden, true, "force 应跳过确认直接关闭");
  assert.strictEqual(confirmCalled, false, "force 不应调用 confirm");
});

test("未脏 → 直接关闭,不弹确认", () => {
  let confirmCalled = false;
  const overlay = { hidden: false, dataset: { dirty: "" }, querySelector: () => ({ innerHTML: "x" }) };
  const sandbox = {
    document: { getElementById: () => overlay, querySelector: () => null, body: { classList: { add() {}, remove() {} } } },
    window: { confirm: () => { confirmCalled = true; return false; } },
    closeToothSelector: () => {},
  };
  vm.createContext(sandbox);
  vm.runInContext(code + "\nthis.__close = closeMedicalEditorOverlay;", sandbox);
  sandbox.__close();
  assert.strictEqual(overlay.hidden, true, "未脏应直接关闭");
  assert.strictEqual(confirmCalled, false, "未脏不应弹确认");
});

test("markMedicalEditorDirty 把可见编辑层标脏", () => {
  const { sandbox, overlay } = makeSandbox("", false);
  sandbox.__mark();
  assert.strictEqual(overlay.dataset.dirty, "1");
});
