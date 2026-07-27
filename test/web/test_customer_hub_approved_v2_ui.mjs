import { test } from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";

const here = dirname(fileURLToPath(import.meta.url));
const read = name => readFileSync(
  join(here, "..", "..", "local_app", "static", name),
  "utf8",
);
const html = read("index.html");
const hub = read("customer_hub_v2.js");
const contract = read("customer_hub_v2_contract.js");
const followupDate = read("customer_hub_v2_followup_date.js");
const moduleViews = read("module_views.js");

function section(source, start, end) {
  const from = source.indexOf(start);
  const to = source.indexOf(end, from + start.length);
  assert.ok(from >= 0, `缺少 ${start}`);
  assert.ok(to > from, `缺少 ${end}`);
  return source.slice(from, to);
}

test("静态资源按 合同层→回访日期模块→V2呈现层 加载，客户通样式独立", () => {
  assert.match(html, /customer_hub_v2\.css\?v=/);
  const contractAt = html.indexOf("/customer_hub_v2_contract.js?v=");
  const followupDateAt = html.indexOf("/customer_hub_v2_followup_date.js?v=");
  const viewAt = html.indexOf("/customer_hub_v2.js?v=");
  assert.ok(contractAt >= 0, "应加载客户通合同层");
  assert.ok(followupDateAt > contractAt, "日期模块必须在合同层之后加载");
  assert.ok(viewAt > followupDateAt, "呈现层必须在日期模块之后加载");
  assert.match(followupDate, /computeNextFollowupDate/);
});

test("module_views 只委托客户通独立模块", () => {
  assert.match(moduleViews, /loadReturnVisitModule[\s\S]*?loadCustomerHubV2/);
  for (const retired of [
    "loadReturnVisitCalendar",
    "renderReturnVisitStudyCard",
    "loadCustomerHubToday",
    "renderCustomerHubToday",
  ]) {
    assert.ok(!moduleViews.includes(retired), `入口层不应拥有 ${retired}`);
  }
});

test("顶层只恢复 今日总览、回访列表、沟通记录", () => {
  assert.match(hub, /_chvView\s*=\s*"today"/);
  for (const label of ["今日总览", "回访列表", "沟通记录"]) {
    assert.ok(hub.includes(label), `缺少 ${label}`);
  }
  assert.ok(!hub.includes("customer-hub-v2__views"), "不应保留 今日/日历/台账 通用壳");
  assert.ok(!hub.includes("customer-hub-v2__tabs"), "不应保留五页签通用壳");
  assert.ok(!hub.includes("data-chv2-tab"), "不应渲染总览/回访/沟通/通话/字典五页签");
  assert.ok(!hub.includes("<h2 tabindex=\"-1\" data-chv2-heading>客户通</h2>"), "不应重复客户通标题");
});

test("今日总览恢复三卡、两张表、日期弹历、录音弹卡和月度回顾", () => {
  for (const marker of [
    "chv-kpis",
    "今日回访",
    "今日沟通",
    "data-chv-cal",
    "data-chv-recs",
    "data-chv-mr",
  ]) {
    assert.ok(hub.includes(marker), `缺少 ${marker}`);
  }
  assert.match(hub, /\/api\/customer-hub\/today\?date=/);
  assert.match(hub, /\/api\/return-visits\/calendar\?month=/);
});

test("回访列表和处理弹窗保留确认版工作流", () => {
  for (const marker of [
    "高级查询",
    "/api/return-visits/export",
    "患者信息",
    "历史回访",
    "咨询沟通",
    "data-chv-okplan",
  ]) {
    assert.ok(hub.includes(marker), `缺少 ${marker}`);
  }
  assert.match(hub, /_chvWriteOnPage\(body,\s*button,\s*"returnVisit",\s*\{\s*action:\s*"batchDelete"/);
  assert.match(contract, /"\/api\/return-visits\/batch-delete"/);
  assert.match(hub, /data-chv-home/);
  assert.match(hub, /openPatientWorkspace/);
});

test("呈现层只调用合同层，批删逐条携带渲染 revision", () => {
  assert.match(hub, /window\.CustomerHubV2Contract/);
  assert.doesNotMatch(hub, /fetch\s*\(/);
  assert.match(hub, /class="chv-ck"[\s\S]{0,160}data-chv-revision=/);
  assert.match(hub, /revisions\s*=\s*Object\.fromEntries\(selected\.map/);
  assert.match(hub, /action:\s*"batchDelete"[\s\S]{0,80}ids,[\s\S]{0,80}revisions/);
});

test("截图和录音成功后推进父回访 revision，失败保留待传内容", () => {
  const save = section(hub, "const doSave = async focusTarget =>", "const okBtn = ovl.querySelector");
  assert.match(save, /while \(pendingFiles\.length\)/);
  assert.match(save, /expectedRevision,/);
  assert.match(save, /pendingFiles\.shift\(\)[\s\S]*?Number\.isSafeInteger\(uploadedRevision\)[\s\S]*?currentRevision\s*=\s*uploadedRevision/);
  assert.match(save, /revisionRecovery\.confirm\("screenshot",\s*expectedRevision\)/);
  assert.match(save, /if \(!uploadResult\.ok\)[\s\S]{0,320}return false/);
  const mic = section(hub, "function _chvMicButton", "// ============ 弹层基建");
  assert.match(mic, /linkedReturnVisitId:\s*rvId/);
  assert.match(mic, /expectedRevision:[\s\S]{0,100}getRevision\(\)/);
  assert.match(mic, /context_revision[\s\S]{0,160}setRevision\(nextRevision\)/);
});

test("服务端管理字段不进入写请求，字典按动态键合并缓存", () => {
  const addCommunication = section(hub, "function _chvOpenAddComm", "function _chvOpenNewRv");
  assert.doesNotMatch(addCommunication, /operator\s*:/);
  assert.doesNotMatch(addCommunication, /contacted_at\s*:/);
  const save = section(hub, "const doSave = async focusTarget =>", "const okBtn = ovl.querySelector");
  assert.doesNotMatch(save, /actual_date\s*:/);
  const dictionaries = section(hub, "async function _chvOpenDict", "// 区间选择器入口");
  assert.match(dictionaries, /fields:\s*\{\[name\]:\s*items\}/);
  assert.match(dictionaries, /_chvDicts\s*=\s*\{\.\.\.\(_chvDicts \|\| \{\}\),\s*\.\.\.saved\}/);
});

test("409 和离开守卫都不自动关闭或清空用户草稿", () => {
  assert.match(hub, /if \(!result\.ok\)\s*\{[\s\S]{0,160}_chvWriteError\(result,[\s\S]{0,120}return false/);
  assert.match(hub, /overlay\._chvGuarded && overlay\._chvDirty && !overlay\._chvClean/);
  assert.match(hub, /当前有未保存内容，确认放弃并离开吗/);
  assert.doesNotMatch(contract, /status\s*===\s*409[\s\S]{0,200}(retry|重试)\s*\(/i);
});

test("权限只控制显隐，关键写入口分别使用明确权限", () => {
  for (const marker of [
    'hasPerm("return_visit.view")',
    'hasPerm("return_visit.manage")',
    'hasPerm("return_visit.delete")',
    'hasPerm("communication.view")',
    'hasPerm("communication.manage")',
    'hasPerm("call_record.view")',
    'hasPerm("call_record.manage")',
    'hasPerm("appointment.create")',
  ]) {
    assert.ok(hub.includes(marker), `缺少权限显隐 ${marker}`);
  }
});
