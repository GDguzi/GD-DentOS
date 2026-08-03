// 回归：挂号表示患者已经到店，应进入今日候诊队列；月历必须完整显示七列。
// 跑：node --test test/web/test_walkin_register_calendar.mjs
import { test } from "node:test";
import assert from "node:assert";
import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import { dirname, join } from "node:path";
import vm from "node:vm";

const here = dirname(fileURLToPath(import.meta.url));
const staticDir = join(here, "..", "..", "local_app", "static");
const workspaceSrc = readFileSync(join(staticDir, "patient_workspace.js"), "utf8");
const patientSrc = readFileSync(join(staticDir, "patient_module.js"), "utf8");
const editorSrc = readFileSync(join(staticDir, "appointment_editor.js"), "utf8");
const todayWorkSrc = readFileSync(join(staticDir, "today_work.js"), "utf8");
const indexSrc = readFileSync(join(staticDir, "index.html"), "utf8");
const stylesSrc = readFileSync(join(staticDir, "styles.css"), "utf8");

function extractFunction(src, header) {
  const start = src.indexOf(header);
  assert.ok(start >= 0, `应能定位 ${header}`);
  let brace = src.indexOf("{", start);
  assert.ok(brace >= 0, `${header} 应有函数体`);
  let depth = 0;
  let quote = "";
  let escaped = false;
  let lineComment = false;
  let blockComment = false;
  for (let i = brace; i < src.length; i++) {
    const ch = src[i], next = src[i + 1];
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
    if (ch === "}" && --depth === 0) return src.slice(start, i + 1);
  }
  assert.fail(`${header} 函数体不完整`);
}

test("患者档案的挂号入口直接快速挂号，不打开预约弹窗", async () => {
  const code = extractFunction(workspaceSrc, "async function railAction");
  const calls = {checkIn: [], appointments: 0, picks: 0};
  const sandbox = {
    checkInPatient: async options => { calls.checkIn.push(options); return {appointment: {appointment_id: "a1"}}; },
    openNewAppointment: () => { calls.appointments += 1; },
    pickApptPatient: () => { calls.picks += 1; },
  };
  vm.createContext(sandbox);
  vm.runInContext(`${code}\nthis.__railAction = railAction;`, sandbox);
  await sandbox.__railAction("reg", "synthetic-patient", "演示患者");
  assert.deepStrictEqual(
    JSON.parse(JSON.stringify(calls.checkIn)),
    [{
      patientIdentity: "synthetic-patient",
      displayName: "演示患者",
      openTriageAfter: false,
      switchToToday: true,
      showSuccess: true,
    }],
  );
  assert.strictEqual(calls.appointments, 0);
  assert.strictEqual(calls.picks, 0);
});

test("患者档案的预约入口不应被改成到店", async () => {
  const code = extractFunction(workspaceSrc, "async function railAction");
  const calls = [];
  const sandbox = {openNewAppointment: prefill => calls.push(prefill), pickApptPatient: () => {}};
  vm.createContext(sandbox);
  vm.runInContext(`${code}\nthis.__railAction = railAction;`, sandbox);
  await sandbox.__railAction("appt", "synthetic-patient", "演示患者");
  assert.deepStrictEqual(JSON.parse(JSON.stringify(calls)), [{}]);
});

test("患者列表的挂号入口直接快速挂号，不打开预约弹窗", async () => {
  const code = extractFunction(patientSrc, "function bindTableBody");
  let clickHandler = null;
  const button = {
    dataset: {pid: "synthetic-list-patient", pname: "列表患者"},
    addEventListener(type, handler) { if (type === "click") clickHandler = handler; },
  };
  const body = {
    querySelectorAll(selector) { return selector === "[data-pc-reg]" ? [button] : []; },
    querySelector() { return null; },
  };
  const calls = {checkIn: [], appointments: 0, picks: 0};
  const sandbox = {
    bindPatientCenter() {},
    checkInPatient: async options => { calls.checkIn.push(options); return {appointment: {appointment_id: "a2"}}; },
    openNewAppointment: () => { calls.appointments += 1; },
    pickApptPatient: () => { calls.picks += 1; },
  };
  vm.createContext(sandbox);
  vm.runInContext(`${code}\nthis.__bindTableBody = bindTableBody;`, sandbox);
  sandbox.__bindTableBody(body);
  assert.ok(clickHandler, "应绑定列表挂号点击");
  await clickHandler({stopPropagation() {}});
  assert.deepStrictEqual(JSON.parse(JSON.stringify(calls.checkIn)), [{
    patientIdentity: "synthetic-list-patient",
    displayName: "列表患者",
    openTriageAfter: false,
    switchToToday: true,
    showSuccess: true,
  }]);
  assert.strictEqual(calls.appointments, 0);
  assert.strictEqual(calls.picks, 0);
});

test("新增患者展示三个明确提交动作，不再保存后追问", () => {
  const openFn = extractFunction(todayWorkSrc, "function openNewPatient");
  const submitFn = extractFunction(todayWorkSrc, "async function submitNewPatient");
  assert.match(openFn, /submitNewPatient\(['"]save['"]\)/);
  assert.match(openFn, /submitNewPatient\(['"]register['"]\)/);
  assert.match(openFn, /submitNewPatient\(['"]register-triage['"]\)/);
  assert.doesNotMatch(submitFn, /window\.confirm/);
  assert.doesNotMatch(submitFn, /openNewAppointment/);
});

test("到店模式显示挂号文案，普通模式保留预约文案", () => {
  const fn = extractFunction(editorSrc, "function openNewAppointment");
  assert.match(fn, /prefill\.registerType\s*===\s*["']到店["']/);
  assert.match(fn, /新增挂号/);
  assert.match(fn, /确认挂号/);
  assert.match(fn, /新增预约/);
  assert.match(fn, /保存预约/);
});

test("预约/挂号弹窗的无障碍名称跟随动态标题", () => {
  assert.match(indexSrc, /role="dialog"[^>]+aria-labelledby="apptModalTitle"/);
  assert.doesNotMatch(indexSrc, /role="dialog"[^>]+aria-label="新增预约"/);
});

test("月历日期按钮清除全局按钮尺寸，七列可在网格内收缩", () => {
  const rule = stylesSrc.match(/\.appt-cal-cell\s*\{([^}]*)\}/)?.[1] || "";
  assert.match(rule, /padding\s*:\s*0\b/);
  assert.match(rule, /min-width\s*:\s*0\b/);
  assert.match(rule, /min-height\s*:\s*0\b/);
  assert.match(rule, /width\s*:\s*100%\s*;/);
});
