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

// ---------- GD-04:工作检查日期切换隔离 ----------

function deferred() {
  let resolve, reject;
  const promise = new Promise((res, rej) => { resolve = res; reject = rej; });
  return { promise, resolve, reject };
}

function makeDateSandbox() {
  const calls = { shift: [], changeWork: 0, loadTodayWork: 0 };
  const fetches = [];
  const sandbox = {
    todayWorkPanel: { innerHTML: "", textContent: "" },
    workDate: { value: "2026-07-16" },
    escapeHtml: v => String(v ?? ""),
    escapeAttr: v => String(v ?? ""),
    encodeURIComponent,
    setTimeout,
    shiftWorkDate: offset => {
      calls.shift.push(offset);
      sandbox.workDate.value = offset === 0 ? "2026-07-16" : "2026-07-15";
      return new Date(sandbox.workDate.value + "T12:00:00");
    },
    changeWorkDate: () => { calls.changeWork += 1; },
    loadTodayWork: () => { calls.loadTodayWork += 1; },
    fetch: url => {
      const d = deferred();
      fetches.push({ url, ...d });
      return d.promise;
    },
  };
  vm.createContext(sandbox);
  vm.runInContext(
    src +
      "\nthis.__changeInspectionDate = changeInspectionDate;" +
      "\nthis.__loadTodayInspection = loadTodayInspection;",
    sandbox,
  );
  return { sandbox, calls, fetches };
}

test("GD-04:切前一天只动共享日期并加载工作检查,不触发今日工作台", async () => {
  const { sandbox, calls, fetches } = makeDateSandbox();
  sandbox.__changeInspectionDate(-1);
  await new Promise(r => setTimeout(r, 0));

  assert.deepStrictEqual(calls.shift, [-1], "必须走纯日期函数 shiftWorkDate");
  assert.strictEqual(calls.changeWork, 0, "禁止调用会刷新今日工作台的 changeWorkDate");
  assert.strictEqual(calls.loadTodayWork, 0, "禁止调用 loadTodayWork");
  assert.strictEqual(fetches.length, 1);
  assert.ok(
    fetches[0].url.includes("/api/today-inspection?date=2026-07-15"),
    `只请求新日期的工作检查,实际:${fetches[0].url}`,
  );
});

test("GD-04:序号守卫——旧请求慢返回不得覆盖新日期结果", async () => {
  const { sandbox, fetches } = makeDateSandbox();
  const rendered = [];
  sandbox.renderInspection = data => { rendered.push(data.date); };

  sandbox.__loadTodayInspection();            // 旧请求
  sandbox.workDate.value = "2026-07-15";
  sandbox.__loadTodayInspection();            // 新请求
  assert.strictEqual(fetches.length, 2);

  fetches[1].resolve({ ok: true, json: async () => ({ date: "2026-07-15" }) });
  await new Promise(r => setTimeout(r, 0));
  fetches[0].resolve({ ok: true, json: async () => ({ date: "2026-07-16" }) });
  await new Promise(r => setTimeout(r, 0));

  assert.deepStrictEqual(rendered, ["2026-07-15"], "旧数据不得晚到覆盖");
});

test("GD-04:序号守卫——旧请求失败不得把新结果改成载入失败", async () => {
  const { sandbox, fetches } = makeDateSandbox();
  sandbox.renderInspection = () => { sandbox.todayWorkPanel.textContent = "OK"; };

  sandbox.__loadTodayInspection();
  sandbox.workDate.value = "2026-07-15";
  sandbox.__loadTodayInspection();

  fetches[1].resolve({ ok: true, json: async () => ({ date: "2026-07-15" }) });
  await new Promise(r => setTimeout(r, 0));
  fetches[0].reject(new Error("stale failure"));
  await new Promise(r => setTimeout(r, 0));

  assert.strictEqual(sandbox.todayWorkPanel.textContent, "OK", "旧请求失败不得覆盖新结果");
});

// ---------- GD-05:summary.total 唯一权威 + 处置收费派生状态 ----------

const auditPayload = {
  date: "2026-07-16",
  appointments: [],
  orders: [{
    order_id: "o1", order_no: "CZ1", patient_identity: "p1",
    display_name: "患者甲", doctor_name: "医生A", diagnosis: "龋齿",
    item_summary: "根管治疗", status: "priced", pay_state: "paid",
    bill_id: "b1", item_total: 100,
    overall: "ok", checks: [{ check: "账单", status: "ok" }],
  }],
  bills: [],
  return_visits: [{
    return_visit_id: "rv1", patient_identity: "p1", display_name: "患者甲",
    item_name: "术后回访", status: "已回访", note: "", overall: "ok", checks: [],
  }],
  walkins: [{ patient_identity: "p9", display_name: "到店患者", phone: "", overall: "warn" }],
  summary: {
    appointments: 0, orders: 1, bills: 0, return_visits_due: 1, walkins: 1,
    total: 3, ok: 2, warn: 1, missing: 0, error: 0,
  },
};

test("GD-05:总数用 summary.total,回访/无预约到店进分母,健康度同分母", () => {
  const html = render(auditPayload);
  assert.ok(html.includes('insp-stat-num">3</span><span class="insp-stat-label">总计'), "总计必须等于 summary.total=3");
  assert.match(html, /67%/, "健康度=ok/total=2/3");
  assert.match(html, /回访到期1/);
  assert.match(html, /无预约到店1/, "walkin 数量必须显示");
});

test("GD-05:处置收费标签用派生 pay_state,不信滞后的状态列", () => {
  const html = render(auditPayload);
  assert.match(html, /已收费/, "账单结清 → 已收费");
  assert.doesNotMatch(html, /待收费/, "不得再显示滞后的 priced 标签");
});

test("GD-05:旧 API 无 summary.total 时兼容计算也要含回访和 walkin", () => {
  const { total: _ignored, ...oldSummary } = auditPayload.summary;
  const html = render({ ...auditPayload, summary: oldSummary });
  assert.ok(html.includes('insp-stat-num">3</span><span class="insp-stat-label">总计'), "兼容分母=预约+处置+账单+回访+walkin");
});
