// 审计R3 回归:新建病历保存必须用「打开编辑器那一刻快照的患者ID」,不读保存时的全局
// workspacePatientId——编辑期间浏览器后退等切了患者,病历仍要落在打开时的患者档案(快照范式)。
// 跑：node --test test/web/test_medical_editor_patient_snapshot.mjs
import { test } from "node:test";
import assert from "node:assert";
import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import { dirname, join } from "node:path";
import vm from "node:vm";

const here = dirname(fileURLToPath(import.meta.url));
const src = readFileSync(join(here, "..", "..", "local_app", "static", "medical_editor.js"), "utf8");

// 整文件跑进沙箱:stub 掉 DOM 与跨文件助手,拿真的 renderMedicalRecord / saveMedicalRecord 测行为
function makeSandbox() {
  const statusEl = { textContent: "" };
  const overlay = { hidden: false, dataset: {} };
  const sandbox = {
    window: {},
    fetchCalls: [],
    // 跨文件助手 stub(转义/牙位图/版本列表不影响患者ID路径)
    escapeAttr: s => String(s ?? ""),
    escapeHtml: s => String(s ?? ""),
    cssEscape: s => String(s ?? ""),
    toothCrossHtml: () => "",
    recordVersionList: () => "",
    parseImportedSubject: () => null,
    importedSubjectToText: () => null,
    closeMedicalEditorOverlay: () => {},
    refreshWorkspaceDetail: async () => null,
    loadAuditLogs: async () => {},
  };
  sandbox.fetch = async (url, opts) => {
    sandbox.fetchCalls.push({ url, opts: opts || {} });
    if (opts && opts.method === "POST") {
      return { ok: true, json: async () => ({ medical_record: { record_id: "rec-1" } }) };
    }
    return { ok: true, json: async () => ({}) };
  };
  sandbox.document = {
    getElementById: id => {
      if (id === "medicalEditorOverlay") return overlay;
      if (id === "recordStatus-new") return statusEl;
      return null;
    },
    querySelector: () => null,
    addEventListener: () => {},
    body: { classList: { add() {}, remove() {} } },
  };
  vm.createContext(sandbox);
  vm.runInContext(src, sandbox);
  return { sandbox };
}

// 编辑器容器 stub:携带(或缺失)患者快照,保存路径所需字段齐全
function makeContainer(patientSnapshot) {
  const fields = {
    tooth_json: { value: "{}" },
    content_json: { value: "{}" },
    record_type: { value: "初诊" },
    doctor_name: { value: "" },
    visit_time: { value: "2026-07-17 10:00" },
  };
  return {
    dataset: patientSnapshot === undefined ? {} : { patientSnapshot },
    querySelector: sel => {
      const m = String(sel).match(/data-record-field="(\w+)"/);
      return m ? fields[m[1]] || null : null;   // radio :checked 等其余选择器 → null
    },
    querySelectorAll: () => [],
  };
}

// 让 document.querySelector 能按 data-record-id="new" 找到容器,然后跑保存
async function saveWith(sandbox, container) {
  sandbox.document.querySelector = sel =>
    String(sel).includes('data-record-id="new"') ? container : null;
  await sandbox.window.saveMedicalRecord("new");
  return sandbox.fetchCalls.find(c => c.opts.method === "POST");
}

test("打开编辑器时把当前患者ID快照进编辑器 DOM", () => {
  const { sandbox } = makeSandbox();
  sandbox.workspacePatientId = "PATIENT_A";
  const html = sandbox.window.renderMedicalRecord({
    record_id: "new", record_type: "", doctor_name: "",
    visit_time: "2026-07-17 10:00", content_json: "{}", tooth_json: "{}",
  });
  const m = html.match(/data-patient-snapshot="([^"]*)"/);
  assert.ok(m, "编辑器根节点应携带打开那一刻的患者ID快照(data-patient-snapshot)");
  assert.strictEqual(m[1], "PATIENT_A");
});

test("打开编辑器给患者A→切全局患者为B→保存→POST 落在 A", async () => {
  const { sandbox } = makeSandbox();
  // 1. 打开编辑器时全局是患者A
  sandbox.workspacePatientId = "PATIENT_A";
  const html = sandbox.window.renderMedicalRecord({
    record_id: "new", record_type: "", doctor_name: "",
    visit_time: "2026-07-17 10:00", content_json: "{}", tooth_json: "{}",
  });
  const m = html.match(/data-patient-snapshot="([^"]*)"/);
  // 2. 编辑期间(浏览器后退等)全局切到患者B
  sandbox.workspacePatientId = "PATIENT_B";
  // 3. 保存:容器即打开时渲染出的编辑器 DOM(含快照)
  const post = await saveWith(sandbox, makeContainer(m ? m[1] : undefined));
  assert.ok(post, "应发出新建 POST");
  assert.strictEqual(post.url, "/api/medical-records");
  const body = JSON.parse(post.opts.body);
  assert.strictEqual(body.patient_identity, "PATIENT_A",
    "病历必须写进打开编辑器时的患者A,不能被切走后的全局患者B污染");
});

test("快照缺失不得兜底读全局患者(留空让后端 400 明确报错)", async () => {
  const { sandbox } = makeSandbox();
  sandbox.workspacePatientId = "PATIENT_B";
  const post = await saveWith(sandbox, makeContainer(undefined));
  assert.ok(post, "应发出新建 POST");
  const body = JSON.parse(post.opts.body);
  assert.strictEqual(body.patient_identity, "",
    "快照缺失时不得回退成当前全局患者,应留空由后端 400 拒绝");
});
