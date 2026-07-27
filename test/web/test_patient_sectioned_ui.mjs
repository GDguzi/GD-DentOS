import { test } from "node:test";
import assert from "node:assert";
import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import { dirname, join } from "node:path";
import vm from "node:vm";

const here = dirname(fileURLToPath(import.meta.url));
const patientSrc = readFileSync(
  join(here, "..", "..", "local_app", "static", "patient_workspace.js"),
  "utf8",
);
const tabsSrc = readFileSync(
  join(here, "..", "..", "local_app", "static", "workspace_tabs.js"),
  "utf8",
);

function sliceBetween(source, start, end) {
  const from = source.indexOf(start);
  const to = source.indexOf(end, from);
  assert.ok(from >= 0 && to > from, `missing source range: ${start}`);
  return source.slice(from, to);
}

const patientSandbox = {
  formatCount: value => String(value),
  cssEscape: value => String(value),
};
vm.createContext(patientSandbox);
vm.runInContext(
  sliceBetween(patientSrc, "function renderBizOverview", "function renderWorkspaceRail")
    + sliceBetween(patientSrc, "function renderWorkspaceTabCounts", "function workspaceSexAgeText")
    + "\nthis.renderBizOverview = renderBizOverview;"
    + "\nthis.renderWorkspaceTabCounts = renderWorkspaceTabCounts;",
  patientSandbox,
);

const tabsSandbox = {
  escapeHtml: value => String(value ?? ""),
  formatMoney: value => Number(value || 0).toFixed(2),
};
vm.createContext(tabsSandbox);
vm.runInContext(
  sliceBetween(tabsSrc, "function treatmentItemsHaveFinance", "// ---------- 备注 tab")
    + sliceBetween(tabsSrc, "function renderVisitDay", "// 就诊时间轴里的「预约」")
    + "\nthis.renderTreatmentVisit = renderTreatmentVisit;"
    + "\nthis.renderVisitDay = renderVisitDay;",
  tabsSandbox,
);

test("经营概览缺 total_spend 时不显示累计消费", () => {
  const html = patientSandbox.renderBizOverview({
    visit_count: 2, first_visit: "2026-01-01", last_visit: "2026-07-16",
  });
  assert.match(html, /到诊次数/);
  assert.match(html, /2 次/);
  assert.doesNotMatch(html, /累计消费|¥/);
});

test("经营概览有 total_spend 时保留原消费金额", () => {
  const html = patientSandbox.renderBizOverview({
    total_spend: 123.4, visit_count: 2,
    first_visit: "2026-01-01", last_visit: "2026-07-16",
  });
  assert.match(html, /累计消费/);
  assert.match(html, /¥123\.40/);
});

test("counts 缺 bills 时账单角标保持隐藏；字段存在时仍显示真实数量", () => {
  const badges = {
    medical: { hidden: true, textContent: "" },
    images: { hidden: true, textContent: "" },
    billing: { hidden: true, textContent: "" },
    notes: { hidden: true, textContent: "" },
  };
  patientSandbox.workspacePageEl = () => ({
    querySelector: selector => badges[/data-workspace-count="([^"]+)/.exec(selector)?.[1]] || null,
  });

  patientSandbox.renderWorkspaceTabCounts({medical_records: 1, images: 2, local_notes: 3});
  assert.strictEqual(badges.billing.hidden, true);
  assert.strictEqual(badges.billing.textContent, "");

  patientSandbox.renderWorkspaceTabCounts({medical_records: 1, images: 2, bills: 4, local_notes: 3});
  assert.strictEqual(badges.billing.hidden, false);
  assert.strictEqual(badges.billing.textContent, "4");
});

test("处置缺财务字段时保留项目/数量/分类，省略合计和单价金额列", () => {
  const html = tabsSandbox.renderTreatmentVisit({
    bill_time: "2026-07-16 09:00:00",
    doctor_names: ["医生A"],
    items: [{item_name: "洁牙", quantity: 2, fee_type: "治疗"}],
  }, true);
  assert.match(html, /洁牙/);
  assert.match(html, /<th>项目<\/th>/);
  assert.match(html, /<th>数量<\/th>/);
  assert.match(html, /<th>分类<\/th>/);
  assert.doesNotMatch(html, /合计|<th>单价<\/th>|<th>金额<\/th>|0\.00/);
});

test("处置有财务字段时总金额和单价金额列保持原样", () => {
  const html = tabsSandbox.renderTreatmentVisit({
    bill_time: "2026-07-16 09:00:00", bill_no: "B001", bill_total_fee: 200,
    doctor_names: ["医生A"],
    items: [{
      item_name: "洁牙", unit_price: 100, quantity: 2,
      total_fee: 200, fee_type: "治疗",
    }],
  }, true);
  assert.match(html, /合计 200\.00/);
  assert.match(html, /<th>单价<\/th>/);
  assert.match(html, /<th>金额<\/th>/);
  assert.match(html, />100\.00</);
  assert.match(html, />200\.00</);
});

test("就诊账单只有 bill_number 时只显示编号；有金额字段时保留原文案", () => {
  const numberOnly = tabsSandbox.renderVisitDay({
    date: "2026-07-16", bills: [{bill_number: "B001"}],
  }, true);
  assert.match(numberOnly, /B001/);
  assert.doesNotMatch(numberOnly, /合计|已付|0\.00/);

  const withMoney = tabsSandbox.renderVisitDay({
    date: "2026-07-16",
    bills: [{bill_no: "B002", total_fee: 200, paid_fee: 100}],
  }, true);
  assert.match(withMoney, /B002 合计200\.00 已付100\.00/);
});
