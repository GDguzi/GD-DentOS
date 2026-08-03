// #813④ 回归:分诊弹窗竞态——快速连开 A、B 两个分诊,A 的医生列表慢返回时,
// 旧实现全局 _triageApptId 被 B 覆盖但 A 的渲染后到,出现"显示 A 保存到 B"。
// 修后:预约 ID 跟弹窗 dataset 走 + 令牌丢弃过期渲染;保存失败(res.ok=false)不关弹窗不刷新。
// 跑：node --test test/web/test_triage_race_813.mjs
import { test } from "node:test";
import assert from "node:assert";
import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import { dirname, join } from "node:path";
import vm from "node:vm";

const here = dirname(fileURLToPath(import.meta.url));
const src = readFileSync(join(here, "..", "..", "local_app", "static", "today_work.js"), "utf8");

function extract(fnHeader) {
  const start = src.indexOf(fnHeader);
  assert.ok(start > 0, `应能定位 ${fnHeader}`);
  const end = src.indexOf("\n}", start) + 2;
  return src.slice(start, end);
}

const openCode = extract("async function openTriage");
const saveCode = extract("async function saveTriage");
assert.ok(src.includes("let _triageToken"), "openTriage 应有渲染令牌");
assert.ok(!src.includes("_triageApptId"), "全局 _triageApptId 必须已删除(竞态根源)");

function makeSandbox({staffFetch, putResponse}) {
  const calls = {loadTodayWork: 0, closeTriage: 0, alerts: [], putUrls: []};
  const modal = {id: "triageModal", hidden: true, dataset: {}, innerHTML: "", className: ""};
  const sandbox = {
    latestTodayWorkData: {appointments: [
      {appointment_id: "A", display_name: "患者甲", status: "已到达"},
      {appointment_id: "B", display_name: "患者乙", status: "已到达"},
    ]},
    queueRooms: ["1诊室"],           // 非空 → 跳过诊室补拉,只剩医生列表一个 await
    document: {
      getElementById: id => (id === "triageModal" ? modal : {value: ""}),
      createElement: () => modal,
      body: {appendChild: () => {}},
    },
    window: {alert: msg => calls.alerts.push(String(msg))},
    fetch: async (url, opts) => {
      if (String(url).includes("/api/staff-members")) return staffFetch();
      if (opts && opts.method === "PUT") { calls.putUrls.push(String(url)); return putResponse(); }
      return {ok: true, json: async () => ({})};
    },
    escapeHtml: s => String(s == null ? "" : s),
    _chipField: () => "",
    bindChipSelects: () => {},
    tqStage: () => 2,                 // 已到达 → 保存时应带 status=已分诊
    closeTriage: () => { calls.closeTriage++; modal.hidden = true; },
    loadTodayWork: () => { calls.loadTodayWork++; },
    encodeURIComponent,
    Number,
    String,
  };
  vm.createContext(sandbox);
  vm.runInContext(
    "let _triageToken = 0;\n" + openCode + "\n" + saveCode +
    "\nthis.__open = openTriage; this.__save = saveTriage;",
    sandbox,
  );
  return {sandbox, calls, modal};
}

test("#813④ 快速连开 A、B 且 A 响应后到 → 弹窗与保存都命中 B", async () => {
  let resolveA;
  const slowA = new Promise(r => { resolveA = r; });
  let first = true;
  const {sandbox, calls, modal} = makeSandbox({
    staffFetch: () => {
      if (first) { first = false; return slowA; }                 // A 的医生列表挂起
      return Promise.resolve({ok: true, json: async () => ({members: []})});
    },
    putResponse: () => ({ok: true, json: async () => ({})}),
  });
  const pA = sandbox.__open("A");
  const pB = sandbox.__open("B");
  await pB;                                                        // B 先渲染完成
  resolveA({ok: true, json: async () => ({members: []})});         // A 的响应后到
  await pA;
  assert.strictEqual(modal.dataset.apptId, "B", "A 的过期渲染必须被令牌丢弃,弹窗上下文保持 B");
  await sandbox.__save();
  assert.strictEqual(calls.putUrls.length, 1);
  assert.ok(calls.putUrls[0].includes("/api/appointments/B"), "保存必须命中弹窗显示的 B,不能串到别的预约");
});

test("#813④ 保存失败(res.ok=false) → 不关弹窗不刷新,提示错误", async () => {
  const {sandbox, calls} = makeSandbox({
    staffFetch: () => Promise.resolve({ok: true, json: async () => ({members: []})}),
    putResponse: () => ({ok: false, status: 500, json: async () => ({detail: "boom"})}),
  });
  await sandbox.__open("A");
  await sandbox.__save();
  assert.strictEqual(calls.closeTriage, 0, "保存失败不能关弹窗(用户要能重试)");
  assert.strictEqual(calls.loadTodayWork, 0, "保存失败不能假装成功刷新队列");
  assert.strictEqual(calls.alerts.length, 1);
  assert.ok(calls.alerts[0].includes("boom"));
});
