// 客户通已确认 V2 入口与分层守卫。
// 跑：node --test test/web/test_customer_hub_ui.mjs
import { test } from "node:test";
import assert from "node:assert";
import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import { dirname, join } from "node:path";

const here = dirname(fileURLToPath(import.meta.url));
const html = readFileSync(join(here, "..", "..", "local_app", "static", "index.html"), "utf8");
const mv = readFileSync(join(here, "..", "..", "local_app", "static", "module_views.js"), "utf8");
const hub = readFileSync(join(here, "..", "..", "local_app", "static", "customer_hub_v2.js"), "utf8");
const contract = readFileSync(join(here, "..", "..", "local_app", "static", "customer_hub_v2_contract.js"), "utf8");

test("左侧导航 回访管理 → 客户通(key return-visits 不变)", () => {
  assert.match(html, /data-workspace-view="return-visits"[^>]*>客户通</, "导航文案应为客户通");
  assert.ok(!/data-workspace-view="return-visits"[^>]*>回访管理</.test(html), "不应再有回访管理");
});

test("已确认 V2 默认今日总览且只有三个顶层入口", () => {
  assert.match(hub, /_chvView\s*=\s*"today"/, "默认视图应为今日总览");
  for (const label of ["今日总览", "回访列表", "沟通记录"]) {
    assert.ok(hub.includes(label), `缺少 ${label}`);
  }
  assert.doesNotMatch(hub, /customer-hub-v2__|data-chv2-tab/);
});

test("今日总览接聚合接口且权限缺块不伪造假零", () => {
  assert.ok(hub.includes("/api/customer-hub/today"), "呈现层应读取今日聚合接口");
  assert.match(hub, /if \(data\.return_visits\)/, "回访块按存在性渲染");
  assert.match(hub, /if \(data\.communications\)/, "沟通块按存在性渲染");
  assert.ok(!hub.includes("data.return_visits || {"), "不得用假零兜底");
});

test("今日总览显式带当前工作日期(与今日工作台同一真相源)", () => {
  assert.match(hub, /workDate && workDate\.value/, "应读共享 workDate.value 作为日期");
  assert.match(hub, /\/api\/customer-hub\/today\?date=\$\{_chvEncodeComponent\(day\)\}/,
    "fetch URL 应带 date=当前工作日期");
});

test("入口只委托呈现层，写合同只归合同层", () => {
  assert.match(mv, /loadReturnVisitModule[\s\S]*?loadCustomerHubV2\(ctx\)/);
  assert.match(mv, /requestLeaveReturnVisitModule[\s\S]*?requestLeaveCustomerHubV2/);
  assert.doesNotMatch(hub, /\bmoduleViewToken\b/,
    "呈现层不得反向读取 module_views 的生命周期状态");
  assert.match(hub, /let _chvLifecycleGeneration\s*=\s*0/,
    "客户通应独立持有自己的异步生命周期代数");
  assert.match(hub, /window\.CustomerHubV2Contract/);
  assert.doesNotMatch(hub, /fetch\s*\(/, "呈现层不得直接发请求");
  for (const token of [
    "runRetriableWrite",
    "handleReturnVisitWrite",
    "handleCommunicationWrite",
    "handleCallWrite",
    "handleDictionaryWrite",
  ]) {
    assert.ok(contract.includes(token), `合同层应拥有 ${token}`);
    assert.ok(!hub.includes(`function ${token}`), `呈现层不应复制 ${token}`);
    assert.ok(!mv.includes(`function ${token}`), `入口层不应复制 ${token}`);
  }
});

test("合同层缺失时明确失败，不静默回退旧业务", () => {
  assert.match(hub, /customer_hub_contract_unavailable/);
  assert.doesNotMatch(mv, /loadReturnVisitLegacyModule/);
});

test("旧客户通实现零调用，module_views 只保留 V2 适配入口", () => {
  assert.doesNotMatch(mv, /loadReturnVisitCalendar|renderReturnVisitStudyCard|renderCustomerHubToday/);
  assert.doesNotMatch(mv, /function\s+(build|handle|render)CustomerHubV2/);
});
