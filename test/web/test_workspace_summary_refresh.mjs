// GD-09:收款/退款后 refreshWorkspaceDetail 必须把新 summary 写回 workspaceData
// 并重渲染左栏(累计消费即时更新);切患者竞态不串;summary 失败保留旧值。
import { test } from "node:test";
import assert from "node:assert";
import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import { dirname, join } from "node:path";
import vm from "node:vm";

const here = dirname(fileURLToPath(import.meta.url));
const src = readFileSync(join(here, "..", "..", "local_app", "static", "workspace_tabs.js"), "utf8");

function section(text, startMarker, endMarker) {
  const start = text.indexOf(startMarker);
  assert.ok(start >= 0, `missing ${startMarker}`);
  const end = text.indexOf(endMarker, start + startMarker.length);
  assert.ok(end > start, `missing ${endMarker}`);
  return text.slice(start, end);
}

const core = section(src, "async function refreshWorkspaceDetail", "// ---------- 病历 tab ----------");

function deferred() {
  let resolve, reject;
  const promise = new Promise((res, rej) => { resolve = res; reject = rej; });
  return { promise, resolve, reject };
}

function makeSandbox(pid) {
  const fetches = [];
  const railRenders = [];
  const sandbox = {
    workspacePatientId: pid,
    workspaceData: { patient: { patient_identity: pid }, summary: { total_paid: 0 }, detail: {} },
    renderWorkspaceTabCounts: () => {},
    renderWorkspaceRail: (patient, summary) => railRenders.push({ patient, summary }),
    encodeURIComponent,
    fetch: url => {
      const d = deferred();
      fetches.push({ url, ...d });
      return d.promise;
    },
  };
  vm.createContext(sandbox);
  vm.runInContext(core + "\nthis.__refresh = refreshWorkspaceDetail;", sandbox);
  return { sandbox, fetches, railRenders };
}

const ok = body => ({ ok: true, json: async () => body });

test("GD-09:收款后 summary 写回 workspaceData 并立即重渲染左栏", async () => {
  const { sandbox, fetches, railRenders } = makeSandbox("p1");
  const p = sandbox.__refresh();
  fetches[0].resolve(ok({ patient: { patient_identity: "p1", display_name: "甲" } }));
  await new Promise(r => setTimeout(r, 0));
  fetches[1].resolve(ok({ total_paid: 280, counts: {} }));
  await p;
  assert.strictEqual(sandbox.workspaceData.summary.total_paid, 280, "summary 必须写回共享状态");
  assert.strictEqual(railRenders.length, 1, "必须重渲染左栏");
  assert.strictEqual(railRenders[0].summary.total_paid, 280);
  assert.strictEqual(railRenders[0].patient.display_name, "甲");
});

test("GD-09:summary 返回前切换患者,旧数据不得渲染到新患者", async () => {
  const { sandbox, fetches, railRenders } = makeSandbox("p1");
  const p = sandbox.__refresh();
  fetches[0].resolve(ok({ patient: { patient_identity: "p1" } }));
  await new Promise(r => setTimeout(r, 0));
  sandbox.workspacePatientId = "p2";   // 用户切走了
  fetches[1].resolve(ok({ total_paid: 999, counts: {} }));
  await p;
  assert.notStrictEqual(sandbox.workspaceData.summary.total_paid, 999, "旧患者 summary 不得写入");
  assert.strictEqual(railRenders.length, 0, "不得把旧患者数据渲染到新患者 rail");
});

test("GD-09:summary 请求失败保留旧累计消费,不清零、无未处理异常", async () => {
  const { sandbox, fetches, railRenders } = makeSandbox("p1");
  sandbox.workspaceData.summary = { total_paid: 500 };
  const p = sandbox.__refresh();
  fetches[0].resolve(ok({ patient: { patient_identity: "p1" } }));
  await new Promise(r => setTimeout(r, 0));
  fetches[1].reject(new Error("network"));
  const data = await p;   // 不得抛出
  assert.ok(data, "detail 已成功时业务刷新继续");
  assert.strictEqual(sandbox.workspaceData.summary.total_paid, 500, "失败不得用空 summary 覆盖");
  assert.strictEqual(railRenders.length, 0);
});
