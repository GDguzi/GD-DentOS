import { test } from "node:test";
import assert from "node:assert";
import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import { dirname, join } from "node:path";
import vm from "node:vm";

const here = dirname(fileURLToPath(import.meta.url));
const src = readFileSync(
  join(here, "..", "..", "local_app", "static", "today_inspection.js"),
  "utf8",
);

function render(payload) {
  const sandbox = {
    todayWorkPanel: { innerHTML: "", textContent: "" },
    workDate: { value: "2026-07-16" },
    dateFromWorkValue: () => new Date("2026-07-16T12:00:00"),
    localDateValue: () => "2026-07-16",
    escapeHtml: value => String(value ?? ""),
    escapeAttr: value => String(value ?? ""),
    encodeURIComponent,
  };
  vm.createContext(sandbox);
  vm.runInContext(src + "\nthis.__renderInspection = renderInspection;", sandbox);
  assert.doesNotThrow(() => sandbox.__renderInspection(payload));
  return sandbox.todayWorkPanel.innerHTML;
}

const patientOnly = {
  date: "2026-07-16",
  appointments: [{
    appointment_id: "a1", patient_identity: "p1", display_name: "患者甲",
    start_time: "2026-07-16 09:00:00", doctor_name: "医生A",
    item_name: "复诊", visit_type: "复诊", room: "一诊室",
  }],
  orders: [{
    order_id: "o1", order_no: "CZ1", patient_identity: "p1",
    display_name: "患者甲", doctor_name: "医生A", diagnosis: "龋齿",
    item_summary: "根管治疗",
  }],
  return_visits: [{
    return_visit_id: "rv1", patient_identity: "p1", display_name: "患者甲",
    item_name: "术后回访", status: "待回访", note: "",
  }],
  walkins: [],
};

test("patient-only：缺 summary/核验字段仍渲染基础条目和总数，不伪造状态或财务", () => {
  const html = render(patientOnly);

  assert.match(html, /患者甲/);
  assert.match(html, /根管治疗/);
  assert.match(html, /总计/);
  assert.match(html, /共1条/);
  assert.doesNotMatch(html, /健康度|✅正常|⚠️警告|❌缺失|💥金额异常/);
  assert.doesNotMatch(html, /✅|⚠️|❌|💥/);
  assert.doesNotMatch(html, /账单检查|已收费|待收费|合计|应收|实收|元/);
  assert.doesNotMatch(html, /insp-item-status|insp-item-checks|insp-check/);
});

test("billing-only：显示账单金额与收费状态，但不渲染核验徽标", () => {
  const html = render({
    ...patientOnly,
    orders: [{
      ...patientOnly.orders[0], status: "paid", item_total: 100, bill_id: "b1",
      item_summary: "根管治疗(100元)",
    }],
    bills: [{
      bill_id: "b1", patient_identity: "p1", display_name: "患者甲",
      bill_no: "B001", items: "根管治疗", total_fee: 100,
      paid_fee: 100, unpaid_fee: 0,
    }],
    payments: [{ payment_id: "pay1", amount: 100 }],
  });

  assert.match(html, /账单/);
  assert.match(html, /已收费/);
  assert.match(html, /合计100元/);
  assert.match(html, /应收100 实收100/);
  assert.doesNotMatch(html, /健康度|✅正常|⚠️警告|❌缺失|💥金额异常/);
  assert.doesNotMatch(html, /✅|⚠️|❌|💥/);
  assert.doesNotMatch(html, /insp-item-status|insp-item-checks|insp-check/);
});
