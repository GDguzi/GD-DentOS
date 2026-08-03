// 回归：预约块盖住底层时间格时，拖放仍应命中该时间格并进入重叠确认。
// 跑：node --test test/web/test_appt_drag_overlap_prompt.mjs
import { test } from "node:test";
import assert from "node:assert";
import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import { dirname, join } from "node:path";
import vm from "node:vm";

const here = dirname(fileURLToPath(import.meta.url));
const src = readFileSync(join(here, "..", "..", "local_app", "static", "appointment_grid.js"), "utf8");

function extractFunction(source, header) {
  const start = source.indexOf(header);
  if (start < 0) return "";
  const brace = source.indexOf("{", start);
  if (brace < 0) return "";
  let depth = 0, quote = "", escaped = false, lineComment = false, blockComment = false;
  for (let i = brace; i < source.length; i++) {
    const ch = source[i], next = source[i + 1];
    if (lineComment) { if (ch === "\n") lineComment = false; continue; }
    if (blockComment) { if (ch === "*" && next === "/") { blockComment = false; i++; } continue; }
    if (quote) {
      if (escaped) escaped = false;
      else if (ch === "\\") escaped = true;
      else if (ch === quote) quote = "";
      continue;
    }
    if (ch === "/" && next === "/") { lineComment = true; i++; continue; }
    if (ch === "/" && next === "*") { blockComment = true; i++; continue; }
    if (ch === '"' || ch === "'" || ch === "`") { quote = ch; continue; }
    if (ch === "{") depth++;
    if (ch === "}" && --depth === 0) return source.slice(start, i + 1);
  }
  return "";
}

const hitTestFn = extractFunction(src, "function apptDropCellAtPoint");
const dragFn = extractFunction(src, "function startApptBlockDrag");

function runHitTest(stack, grid = null) {
  assert.ok(hitTestFn, "appointment_grid.js 应定义 apptDropCellAtPoint");
  const sandbox = {document: {elementsFromPoint: () => stack}};
  vm.createContext(sandbox);
  vm.runInContext(`${hitTestFn}\nthis.__hit = apptDropCellAtPoint;`, sandbox);
  return sandbox.__hit(320, 240, grid);
}

test("预约块遮住时间格时返回命中栈中的底层时间格", () => {
  const grid = {};
  const cell = {dataset: {cellTime: "11:00", cellDoctor: "演示医生"}};
  const block = {closest: selector => selector === ".appt-grid" ? grid : null};
  const stack = [
    {closest: selector => selector === "[data-appt-block]" ? block : null},
    {closest: selector => selector === "[data-appt-block]" ? block : null},
    {closest: selector => {
      if (selector === "[data-cell-time]") return cell;
      if (selector === ".appt-grid") return grid;
      return null;
    }},
  ];
  assert.strictEqual(runHitTest(stack, grid), cell);
});

test("sticky 表头遮住时间格时不穿透到隐藏格", () => {
  const grid = {};
  const cell = {dataset: {cellTime: "08:00", cellDoctor: "演示医生"}};
  const stack = [
    {closest: selector => selector === ".appt-grid" ? grid : null},
    {closest: selector => {
      if (selector === "[data-cell-time]") return cell;
      if (selector === ".appt-grid") return grid;
      return null;
    }},
  ];
  assert.strictEqual(runHitTest(stack, grid), null);
});

test("网格外没有时间格时返回 null", () => {
  assert.strictEqual(runHitTest([{closest: () => null}]), null);
});

test("拖动过程使用穿透命中函数更新落点", () => {
  assert.ok(dragFn, "应能定位 startApptBlockDrag");
  assert.match(dragFn, /apptDropCellAtPoint\(ev\.clientX,\s*ev\.clientY,\s*grid\)/);
});

function runDrag({confirmResult = true, overlap = true} = {}) {
  assert.ok(dragFn, "应能定位 startApptBlockDrag");
  const listeners = new Map();
  const saves = [];
  let confirmCalls = 0;
  let reloads = 0;
  const cellClasses = new Set();
  const targetCell = {
    dataset: {cellTime: "15:00", cellDoctor: "王医生"},
    classList: {
      add: name => cellClasses.add(name),
      remove: name => cellClasses.delete(name),
    },
  };
  const blockClasses = new Set();
  const grid = {};
  const block = {
    dataset: {apptBlock: "drag-1", apptDur: "60"},
    style: {transform: "translateX(0%)", pointerEvents: ""},
    classList: {
      add: name => blockClasses.add(name),
      remove: name => blockClasses.delete(name),
    },
    closest: selector => selector === ".appt-grid" ? grid : null,
  };
  const appointments = [
    {appointment_id: "drag-1", doctor_name: "王医生", start_time: "2026-08-02 14:00", end_time: "2026-08-02 15:00"},
    ...(overlap ? [{appointment_id: "other-1", doctor_name: "王医生", start_time: "2026-08-02 15:00", end_time: "2026-08-02 16:00"}] : []),
  ];
  const sandbox = {
    document: {
      addEventListener: (name, handler) => listeners.set(name, handler),
      removeEventListener: (name, handler) => {
        if (listeners.get(name) === handler) listeners.delete(name);
      },
    },
    window: {confirm: () => { confirmCalls++; return confirmResult; }},
    Math,
    apptDropCellAtPoint: () => targetCell,
    closeApptCard: () => {},
    showApptCard: () => {},
    loadAppointmentModule: () => { reloads++; },
    rescheduleApptByDrag: (...args) => saves.push(args),
    hm2min: value => {
      const match = String(value || "").match(/^(\d{2}):(\d{2})$/);
      return match ? Number(match[1]) * 60 + Number(match[2]) : null;
    },
  };
  vm.createContext(sandbox);
  vm.runInContext(`${extractFunction(src, "function _apptMinRange")}\n${dragFn}\nthis.__drag = startApptBlockDrag;`, sandbox);
  sandbox.__drag({button: 0, clientX: 100, clientY: 100}, block, {"drag-1": appointments[0]}, appointments);
  listeners.get("pointermove")({clientX: 110, clientY: 110});
  listeners.get("pointerup")();
  return {block, blockClasses, cellClasses, confirmCalls, reloads, saves};
}

test("重叠改约取消后不保存并恢复预约块", () => {
  const result = runDrag({confirmResult: false, overlap: true});
  assert.equal(result.confirmCalls, 1);
  assert.equal(result.saves.length, 0);
  assert.equal(result.reloads, 1);
  assert.equal(result.block.style.transform, "translateX(0%)");
  assert.equal(result.block.style.pointerEvents, "");
  assert.equal(result.blockClasses.has("appt-block-dragging"), false);
  assert.equal(result.cellClasses.has("appt-cell-drophover"), false);
});

test("重叠改约确认后只保存一次目标医生和时段", () => {
  const result = runDrag({confirmResult: true, overlap: true});
  assert.equal(result.confirmCalls, 1);
  assert.deepEqual(result.saves, [[{id: "drag-1", dur: 60}, "15:00", "王医生"]]);
});

test("非重叠改约不弹确认并正常保存", () => {
  const result = runDrag({overlap: false});
  assert.equal(result.confirmCalls, 0);
  assert.deepEqual(result.saves, [[{id: "drag-1", dur: 60}, "15:00", "王医生"]]);
});
