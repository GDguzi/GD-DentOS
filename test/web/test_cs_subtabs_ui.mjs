// 客户沟通模块·步骤1 源码守卫:①患者页「回访」tab 改名「客户沟通」且顶层「面诊」tab 已撤;
// ②回访 tab(key 仍 return-visits)内部套三子tab 回访/沟通咨询/面诊;③面诊子tab 派发到并入的
// renderWorkspaceConsultTab。防后续 UI 调整把改名/并入面诊/子tab 结构冲掉。
// 跑：node --test test/web/test_cs_subtabs_ui.mjs
import { test } from "node:test";
import assert from "node:assert";
import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import { dirname, join } from "node:path";
import vm from "node:vm";

const here = dirname(fileURLToPath(import.meta.url));
const html = readFileSync(join(here, "..", "..", "local_app", "static", "index.html"), "utf8");
const wsTabs = readFileSync(join(here, "..", "..", "local_app", "static", "workspace_tabs.js"), "utf8");
const csJs = readFileSync(join(here, "..", "..", "local_app", "static", "consult_surgery.js"), "utf8");
const callJs = readFileSync(join(here, "..", "..", "local_app", "static", "call_recordings.js"), "utf8");

// 抽出某个顶格 function 源码(闭合 } 在行首;函数体内的 } 都有缩进)
function extractFn(src, header) {
  const start = src.indexOf(header);
  assert.ok(start >= 0, `应能定位 ${header}`);
  const end = src.indexOf("\n}", start) + 2;
  return src.slice(start, end);
}

test("患者页 return-visits tab 文案改成「客户沟通」(key 不动保 hash 兼容)", () => {
  assert.match(html, /data-workspace-tab="return-visits"[^>]*>客户沟通/,
    "return-visits 按钮文案应为「客户沟通」");
});

test("顶层「面诊」tab(consult)已撤(按钮+面板都删)", () => {
  assert.ok(!/data-workspace-tab="consult"/.test(html), "顶层面诊 tab 按钮应已删除");
  assert.ok(!/data-workspace-panel="consult"/.test(html), "顶层面诊面板应已删除");
});

test("客户沟通 tab 内部含三子tab:回访/沟通咨询/面诊", () => {
  for (const k of ["回访", "沟通咨询", "面诊"]) {
    assert.ok(wsTabs.includes(`data-cs-sub`) && wsTabs.includes(k), `子tab 应含 ${k}`);
  }
  assert.match(wsTabs, /CS_SUBTABS\s*=\s*\[\s*"回访"\s*,\s*"沟通咨询"\s*,\s*"面诊"\s*\]/,
    "CS_SUBTABS 应为 回访/沟通咨询/面诊");
});

test("面诊子tab 派发到并入的 renderWorkspaceConsultTab;回访子tab 保留录音容器", () => {
  assert.match(wsTabs, /_csSubTab === "面诊"[\s\S]*?renderWorkspaceConsultTab\(body\)/,
    "面诊子tab 应调用 renderWorkspaceConsultTab");
  assert.ok(wsTabs.includes("renderCsReturnVisitsSub"), "应有回访子tab 渲染函数");
  assert.ok(wsTabs.includes("data-consult-calls") && wsTabs.includes("loadCallsTab"),
    "回访子tab 应保留通话录音容器与挂载");
});

// ---- 行为回归(VM 真跑,非字符串守卫)----

test("(重)载客户沟通 tab 强制重置子tab 为「回访」,不跨患者残留", () => {
  const sandbox = {
    requireWorkspaceDetail: () => true,
    renderCsSubTabs: () => {},
    loadCsSubTab: () => {},
    __panel: { innerHTML: "", querySelectorAll: () => [] },
  };
  vm.createContext(sandbox);
  const code = 'let _csSubTab = "回访";\nconst CS_SUBTABS = ["回访","沟通咨询","面诊"];\n'
    + extractFn(wsTabs, "function renderWorkspaceReturnVisitsTab")
    + "\nthis.__get = () => _csSubTab; this.__set = v => { _csSubTab = v; };";
  vm.runInContext(code, sandbox);
  sandbox.__set("面诊");                       // 模拟上个患者停在面诊
  sandbox.renderWorkspaceReturnVisitsTab(sandbox.__panel);
  assert.strictEqual(sandbox.__get(), "回访", "重载后应回到默认回访子tab");
});

test("面诊保存成功走 onSaved 回调就地刷新,不 switchWorkspaceTab(顶层已无 consult)", async () => {
  let switched = null, onSavedCalled = false;
  const form = { querySelector: () => ({ textContent: "" }), querySelectorAll: () => [] };
  const sandbox = {
    workspacePageEl: () => ({ querySelector: () => form }),
    workspacePatientId: "p1",
    workspaceLoadedTabs: { delete: () => {} },
    switchWorkspaceTab: (t) => { switched = t; },
    fetch: async () => ({ ok: true, json: async () => ({}) }),
    console,
  };
  vm.createContext(sandbox);
  const code = extractFn(csJs, "async function saveSimpleForm")
    + "\nthis.__run = (cb) => saveSimpleForm('consult', 'consults', cb);";
  vm.runInContext(code, sandbox);
  await sandbox.__run(() => { onSavedCalled = true; });
  assert.strictEqual(onSavedCalled, true, "应调用 onSaved 就地刷新面诊子页");
  assert.strictEqual(switched, null, "面诊保存不得 switchWorkspaceTab(会被静默退回基本资料)");
});

test("源码含竞态守卫与回访意图入口", () => {
  assert.ok(/_csSubTab = "回访";/.test(wsTabs), "renderWorkspaceReturnVisitsTab 应重置 _csSubTab");
  assert.ok(wsTabs.includes("body.dataset.csCur = _csSubTab"), "loadCsSubTab 应盖章 csCur 供过期守卫");
  assert.match(wsTabs, /panel\.dataset\.csCur[\s\S]*?!== "回访"[\s\S]*?return/, "回访子tab 应有过期渲染守卫");
  assert.ok(wsTabs.includes("function showCustomerCommReturnVisit"), "应有强制回访子tab 入口");
  assert.ok(csJs.includes("const sub0 = panel.dataset") && csJs.includes("stale()"),
    "loadSimpleTab 应有子tab过期守卫(覆盖成功+失败态)");
});

test("loadSimpleTab 失败态:await 期间切走子tab → 不写「面诊记录载入失败」覆盖回访正文", async () => {
  const panel = { dataset: { csCur: "面诊" }, textContent: "" };
  const sandbox = {
    workspacePatientId: "p1",
    workspaceLoadedTabs: { delete: () => {} },
    // fetch 被 await 时先把子tab切到「回访」,再返回非 2xx —— 复现晚到失败态
    fetch: async () => { panel.dataset.csCur = "回访"; return { ok: false, status: 500, json: async () => ({}) }; },
    console,
  };
  vm.createContext(sandbox);
  vm.runInContext(extractFn(csJs, "async function loadSimpleTab")
    + "\nthis.__run = (p, ft) => loadSimpleTab(p, 'consults', () => { throw new Error('不该渲染'); }, ft);", sandbox);
  await sandbox.__run(panel, "面诊记录载入失败");
  assert.notStrictEqual(panel.textContent, "面诊记录载入失败", "已切走子tab,过期失败态不得覆盖共享 body");
});

test("沟通咨询子tab 接入 renderCommunicationsSub;communications.js 暴露关键函数", () => {
  const commJs = readFileSync(join(here, "..", "..", "local_app", "static", "communications.js"), "utf8");
  assert.match(wsTabs, /_csSubTab === "沟通咨询"[\s\S]*?renderCommunicationsSub\(body\)/,
    "沟通咨询子tab 应派发到 renderCommunicationsSub");
  for (const fn of ["renderCommunicationsSub", "saveCommunication", "deleteCommunication", "setCommDeal", "commChannelChanged"]) {
    assert.ok(commJs.includes(fn), `communications.js 应含 ${fn}`);
  }
  assert.ok(commJs.includes("/communications") && commJs.includes("/images"), "应对接沟通记录+截图接口");
  assert.ok(commJs.includes("linked_communication_id"), "电话录音应弱关联沟通记录");
  assert.ok(commJs.includes('panel.dataset.csCur') && commJs.includes('!== "沟通咨询"'), "应有子tab过期渲染守卫");
});

test("录音前端:成交状态失败不静默、删除不吞错、麦克风录音锁患者", () => {
  assert.ok(callJs.includes("_callMicPid = workspacePatientId"), "录音开始应锁定患者");
  assert.match(callJs, /_callMicPid !== workspacePatientId[\s\S]*?return/, "停止上传前应校验患者未切换");
  assert.match(callJs, /function setCallDeal[\s\S]*?res\.ok[\s\S]*?alert/, "setCallDeal 失败应告警");
  assert.match(callJs, /function deleteCall[\s\S]*?res\.ok[\s\S]*?alert/, "deleteCall 失败应告警");
});

test("沟通记录前端:成交状态/删除失败不静默", () => {
  const commJs = readFileSync(join(here, "..", "..", "local_app", "static", "communications.js"), "utf8");
  assert.match(commJs, /function setCommDeal[\s\S]*?res\.ok[\s\S]*?alert/, "setCommDeal 失败应告警");
  assert.match(commJs, /function deleteCommunication[\s\S]*?res\.ok[\s\S]*?alert/, "deleteCommunication 失败应告警");
});

test("沟通附件保存/删除失败不静默(逐个检查响应+解析 files_failed)", () => {
  const commJs = readFileSync(join(here, "..", "..", "local_app", "static", "communications.js"), "utf8");
  assert.ok(commJs.includes("attachFail"), "saveCommunication 应逐个检查附件上传响应");
  assert.match(commJs, /attachFail > 0[\s\S]*?alert/, "附件失败应告警,不按成功重载");
  assert.match(commJs, /files_failed > 0[\s\S]*?alert/, "deleteCommunication 应解析删盘失败并告警");
});

test("卡片补传入口存在:附件失败后有 补传截图/补传录音 可重试,不必重建记录", () => {
  const commJs = readFileSync(join(here, "..", "..", "local_app", "static", "communications.js"), "utf8");
  assert.ok(commJs.includes("reattachCommImages") && commJs.includes("reattachCommAudio"), "应有补传处理函数");
  assert.ok(commJs.includes("补传截图") && commJs.includes("补传录音"), "记录卡片应有补传入口");
  assert.ok(commJs.includes("data-reshot") && commJs.includes("data-rerec"), "补传应有独立文件输入");
  assert.match(commJs, /Object\.assign\(window,[\s\S]*?reattachCommImages[\s\S]*?reattachCommAudio/, "补传函数应暴露到 window");
});

test("行为回归:附件上传失败时 saveCommunication 明确告警(不静默假成功)", async () => {
  const commJs = readFileSync(join(here, "..", "..", "local_app", "static", "communications.js"), "utf8");
  let alerted = 0, reloaded = 0;
  const form = {
    querySelector(sel) {
      const map = {
        '[data-comm-status]': { textContent: "" },
        '[data-field="channel"]': { value: "wechat" },
        '[data-field="operator"]': { value: "" },
        '[data-field="contacted_at"]': { value: "" },
        '[data-field="deal_status"]': { value: "" },
        '[data-field="content"]': { value: "问价" },
        '[data-comm-images]': { files: [{ name: "a.png" }] },
        '[data-comm-audio]': { files: [] },
      };
      return map[sel] || null;
    },
  };
  const sandbox = {
    workspacePatientId: "p1",
    commFormEl: () => form,
    alert: () => { alerted++; },
    reloadCommunicationsSub: () => { reloaded++; },
    FormData: class { append() {} },
    fetch: async (url) => url.includes("/images")
      ? { ok: false, status: 400, json: async () => ({}) }          // 截图上传失败
      : { ok: true, json: async () => ({ communication_id: "c1" }) }, // 主记录成功
    console,
  };
  vm.createContext(sandbox);
  vm.runInContext(extractFn(commJs, "async function saveCommunication")
    + "\nthis.__run = () => saveCommunication();", sandbox);
  await sandbox.__run();
  assert.strictEqual(alerted, 1, "截图上传 400 时必须告警,不能静默按成功");
});

test("deleteCall:解析 file_removed=false 告警,且重载 return-visits(不是不存在的 calls)", async () => {
  let alerted = false, switched = null;
  const sandbox = {
    confirm: () => true,
    alert: () => { alerted = true; },
    fetch: async () => ({ ok: true, json: async () => ({ ok: true, file_removed: false }) }),
    workspaceLoadedTabs: { delete: () => {} },
    switchWorkspaceTab: (t) => { switched = t; },
    console,
  };
  vm.createContext(sandbox);
  vm.runInContext(extractFn(callJs, "async function deleteCall")
    + "\nthis.__run = (id, revision) => deleteCall(id, revision);", sandbox);
  await sandbox.__run("c1", 1);
  assert.strictEqual(alerted, true, "file_removed=false 应告警,不静默当成功");
  assert.strictEqual(switched, "return-visits", "删除后应回客户沟通(return-visits),不得切不存在的 calls");
});

test("沟通新建不提交服务端管理的操作人和时间", async () => {
  const commJs = readFileSync(join(here, "..", "..", "local_app", "static", "communications.js"), "utf8");
  const nodes = {
    '[data-comm-status]': { textContent: "" },
    '[data-field="channel"]': { value: "phone" },
    '[data-field="direction"]': { value: "inbound" },
    '[data-field="deal_status"]': { value: "" },
    '[data-field="content"]': { value: "问价" },
    '[data-comm-images]': { files: [] },
    '[data-comm-audio]': { files: [] },
  };
  const form = { querySelector: (sel) => nodes[sel] || null };
  let payload = null;
  const sandbox = {
    workspacePatientId: "p1",
    commFormEl: () => form,
    alert: () => {},
    reloadCommunicationsSub: () => {},
    fetch: async (url, opts) => {
      if (!payload) payload = JSON.parse(opts.body);
      return { ok: true, json: async () => ({ communication_id: "c1" }) };
    },
    console,
  };
  vm.createContext(sandbox);
  vm.runInContext(extractFn(commJs, "async function saveCommunication")
    + "\nthis.__run = () => saveCommunication();", sandbox);
  await sandbox.__run();
  assert.strictEqual(Object.hasOwn(payload, "operator"), false);
  assert.strictEqual(Object.hasOwn(payload, "contacted_at"), false);
  assert.strictEqual(payload.direction, "inbound");
  assert.strictEqual(payload.content, "问价", "内容照常提交");
  assert.doesNotMatch(commJs, /data-field="operator"/);
  assert.doesNotMatch(commJs, /data-field="contacted_at"/);
});
