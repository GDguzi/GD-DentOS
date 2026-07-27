// 回归:①refreshWorkspaceDetail 只拉 /api/patients/{id} 不拉 /summary，导致病历/回访等
// tab 角标(病历0/回访0)永远不重算；②标记已回访保存后卡片还停在编辑态，不重渲已回访状态。
// 抽出 refreshWorkspaceDetail / saveReturnVisit 源码，stub fetch/document，沙箱 eval 真测。
// 跑：node --test test/web/test_return_visit_save_refresh.mjs
import { test } from "node:test";
import assert from "node:assert";
import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import { dirname, join } from "node:path";
import vm from "node:vm";

const here = dirname(fileURLToPath(import.meta.url));
const src = readFileSync(join(here, "..", "..", "local_app", "static", "workspace_tabs.js"), "utf8");

function extract(fnHeader) {
  const start = src.indexOf(fnHeader);
  assert.ok(start > 0, `应能定位 ${fnHeader}`);
  const end = src.indexOf("\n}", start) + 2;
  return src.slice(start, end);
}

const refreshDetailCode = extract("async function refreshWorkspaceDetail");
const refreshReturnVisitCode = extract("async function refreshReturnVisitWorkspaceFromServer");
const saveReturnVisitCode = extract("async function saveReturnVisit");

test("refreshWorkspaceDetail 成功后应连带重算 tab 角标(拉 /summary)", async () => {
  const calls = {summaryFetched: 0, tabCounts: null};
  const sandbox = {
    workspacePatientId: "p1",
    workspaceData: {detail: {}, patient: {}},
    fetch: async url => {
      if (String(url).includes("/summary")) { calls.summaryFetched++; return {ok: true, json: async () => ({counts: {return_visit: 1}})}; }
      return {ok: true, json: async () => ({patient: {display_name: "张三"}})};
    },
    renderWorkspaceTabCounts: counts => { calls.tabCounts = counts; },
  };
  vm.createContext(sandbox);
  vm.runInContext(refreshDetailCode + "\nthis.__refresh = refreshWorkspaceDetail;", sandbox);
  await sandbox.__refresh();
  assert.strictEqual(calls.summaryFetched, 1, "应该拉一次 /summary，不能只拉 detail");
  assert.deepStrictEqual(calls.tabCounts, {return_visit: 1}, "应该用新 summary.counts 重渲染 tab 角标");
});

test("标记已回访保存成功 → 应切回只读态重渲(不留在编辑态)", async () => {
  const calls = {switchedTab: null, deletedFromCache: null};
  const loadedTabs = new Set(["return-visits"]);
  const sandbox = {
    cssEscape: s => s,
    document: {
      getElementById: id => (id.startsWith("returnVisitStatus-") ? {textContent: ""} : null),
      querySelector: () => ({
        dataset: {returnVisitRevision: "1"},
        querySelectorAll: () => ({forEach: () => {}}),
        querySelector: () => null,
      }),
    },
    fetch: async () => ({ok: true, json: async () => ({})}),
    evictVisitsCache: () => {},
    loadRecordVersions: async () => {},
    loadAuditLogs: async () => {},
    refreshWorkspaceDetail: async () => {},
    workspaceLoadedTabs: {delete: tab => { calls.deletedFromCache = tab; loadedTabs.delete(tab); }},
    switchWorkspaceTab: tab => { calls.switchedTab = tab; },
  };
  vm.createContext(sandbox);
  vm.runInContext(
    refreshReturnVisitCode + "\n" + saveReturnVisitCode
      + "\nthis.__save = saveReturnVisit;",
    sandbox,
  );
  await sandbox.__save("rv1");
  assert.strictEqual(calls.switchedTab, "return-visits", "保存成功后应重渲回访tab,让已回访状态/戳/按钮立即生效");
  assert.strictEqual(calls.deletedFromCache, "return-visits", "应先踢出加载缓存,强制重新拉取最新状态");
});
