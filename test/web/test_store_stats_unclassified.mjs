import { test } from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import vm from "node:vm";

const source = readFileSync(new URL("../../local_app/static/store_stats.js", import.meta.url), "utf8");
const start = source.indexOf("async function fetchRolePerf(");
const render = source.slice(start, source.indexOf("\n}", start) + 2);
const cell = values => ({ visit: 0, paid: 0, receivable: 0, deal: 0, ...values });

async function draw(unclassified) {
  const target = { innerHTML: "" };
  const sandbox = {
    _rpState: () => ({}), _ROLE_LABEL: { doctor: "医生" }, URLSearchParams,
    escapeHtml: text => String(text),
    fetch: async () => ({ ok: true, json: async () => ({ rows: [], unclassified }) }),
  };
  vm.createContext(sandbox);
  vm.runInContext(render, sandbox);
  await sandbox.fetchRolePerf(target, "doctor");
  return target.innerHTML;
}

test("empty staff rows still display unclassified payments and receivables", async () => {
  const html = await draw({ first: cell({ visit: 1, paid: 80, receivable: 100 }), revisit: cell() });
  assert.ok(html.includes("无法唯一归类"));
  assert.ok(html.includes("未归类金额"));
  assert.ok(html.includes("80.00"));
  assert.ok(html.includes("100.00"));
});

test("unclassified money remains visible even without a matched visit", async () => {
  const html = await draw({ first: cell({ paid: 75 }), revisit: cell({ receivable: 125 }) });
  assert.ok(html.includes("75.00"));
  assert.ok(html.includes("125.00"));
});

test("a truly empty report keeps its existing empty message", async () => {
  const html = await draw({ first: cell(), revisit: cell() });
  assert.ok(html.includes("该区间无就诊记录"));
  assert.ok(!html.includes("未归类金额"));
});
