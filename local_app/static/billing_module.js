// 顶层收费模块视图(呈现层,只读):账单列表+ECharts看板(收入趋势/员工工作量/今日收款/方式占比)。
// 独立模块(原 module_views.js 拆出);患者收费tab的收退款操作在 workspace_billing.js。

async function loadBillingModule() {
  if (!modulePanel) return;
  const token = ++moduleViewToken;
  const keyword = moduleSearchValue();
  const context = moduleContext || {};
  const includeBillingDate = context.billingFilter !== "unpaid";
  const dateQuery = includeBillingDate && workDate && workDate.value ? `date=${encodeURIComponent(workDate.value)}&` : "";
  const searchQuery = keyword ? `q=${encodeURIComponent(keyword)}&` : "";
  const billingModeQuery = context.billingFilter === "settled_today" ? "settlement_only=true" : "unpaid_only=true";
  const billingTitle = context.billingFilter === "settled_today" ? "今日结算列表" : "未结收费列表";
  const emptyText = context.billingFilter === "settled_today" ? "暂无今日结算" : "暂无未结收费";
  modulePanel.innerHTML = `<div class="module-loading">收费载入中...</div>`;
  let data;
  try {
    const res = await fetch(`/api/bills?${dateQuery}${searchQuery}${billingModeQuery}&pageno=${modulePage}`);
    if (token !== moduleViewToken) return;
    if (!res.ok) {
      modulePanel.innerHTML = `<div class="module-loading">收费载入失败</div>`;
      return;
    }
    data = await res.json();
    if (token !== moduleViewToken) return;
  } catch {
    if (token !== moduleViewToken) return;
    modulePanel.innerHTML = `<div class="module-loading">收费载入失败</div>`;
    return;
  }
  modulePanel.innerHTML = `
    <div class="module-head">
      <strong>${escapeHtml(billingTitle)}</strong>
      <span>${escapeHtml(formatCount(data.totalcount))} 条</span>
    </div>
    <div class="today-collection" data-today-collection>今日营收载入中...</div>
    <div class="report-charts">
      <div class="report-chart" id="chartIncomeTrend"></div>
      <div class="report-chart" id="chartPayMethods"></div>
    </div>
    <div class="staff-workload" data-staff-workload></div>
    ${moduleToolbar("搜索患者 / 单号 / 状态", "loadBillingModule", keyword)}
    ${billingContextChip(context, "loadBillingModule")}
    <div class="module-table billing-module">
      <div class="module-row module-row-head">
        <span>收费时间</span>
        <span>患者</span>
        <span>单号</span>
        <span>欠款</span>
        <span>状态</span>
        <span>操作</span>
      </div>
      ${(data.list || []).map(renderBillingModuleRow).join("") || `<div class="module-empty">${escapeHtml(emptyText)}</div>`}
    </div>
    ${modulePager(data, "loadBillingModule")}
  `;
  bindPatientDetailRows(modulePanel);
  loadTodayCollection();
  loadStaffWorkload();
  loadIncomeTrendChart();
}

// echarts 实例化（离线内置；库未加载则静默跳过，不影响表格）
function _echartsInit(id) {
  if (typeof echarts === "undefined") return null;
  const el = document.getElementById(id);
  if (!el) return null;
  return echarts.getInstanceByDom(el) || echarts.init(el);
}

// 近 14 天收入趋势折线图（数据源 /api/reports/income，按日补齐缺口）
async function loadIncomeTrendChart() {
  const chart = _echartsInit("chartIncomeTrend");
  if (!chart) return;
  const end = (typeof workDate !== "undefined" && workDate && workDate.value)
    ? workDate.value : localDateValue();
  const endD = dateFromWorkValue(end);
  const startD = new Date(endD); startD.setDate(startD.getDate() - 13);
  const start = localDateValue(startD);   // 不能用 toISOString(转UTC会差一天)
  let d;
  // 禁止兜底——接口非 2xx 不能当零数据画平线,明确显示载入失败
  try {
    const res = await fetch(`/api/reports/income?date_from=${start}&date_to=${end}`);
    if (!res.ok) { chart.setOption({title: {text: "收入趋势载入失败", left: "center", textStyle: {fontSize: 13, color: cssToken("--danger")}}, series: []}, true); return; }
    d = await res.json();
  } catch { chart.setOption({title: {text: "收入趋势载入失败", left: "center", textStyle: {fontSize: 13, color: cssToken("--danger")}}, series: []}, true); return; }
  const amountByDay = {};
  (d.days || []).forEach(x => { amountByDay[x.date] = x.amount || 0; });
  const dates = [];
  const values = [];
  for (let i = 0; i < 14; i++) {
    const day = new Date(startD); day.setDate(startD.getDate() + i);
    const key = localDateValue(day);   // 不能用 toISOString(转UTC会差一天)
    dates.push(key.slice(5));            // MM-DD
    values.push(amountByDay[key] || 0);
  }
  chart.setOption({
    title: {text: "近14天收入趋势", left: "center", textStyle: {fontSize: 13}},
    tooltip: {trigger: "axis", valueFormatter: v => "¥" + Number(v).toFixed(2)},
    grid: {left: 50, right: 16, top: 36, bottom: 28},
    xAxis: {type: "category", data: dates, axisLabel: {fontSize: 10}},
    yAxis: {type: "value"},
    series: [{type: "line", data: values, smooth: true, areaStyle: {},
              itemStyle: {color: cssToken("--accent")}, lineStyle: {color: cssToken("--accent")}}],
  });
}

// 配台统计：当天按医生/护士/咨询师/助理汇总配台处置单数/项数/金额(绩效)
async function loadStaffWorkload() {
  const box = modulePanel && modulePanel.querySelector("[data-staff-workload]");
  if (!box) return;
  const day = (typeof workDate !== "undefined" && workDate && workDate.value) ? workDate.value : "";
  const q = day ? `?date_from=${encodeURIComponent(day)}&date_to=${encodeURIComponent(day)}` : "";
  let d;
  // 禁止兜底——接口失败不能清空当"暂无配台",明确显示载入失败
  try {
    const res = await fetch(`/api/reports/staff-workload${q}`);
    if (!res.ok) { box.innerHTML = `<div class="sw-head">配台统计</div><div class="sw-empty">配台统计载入失败</div>`; return; }
    d = await res.json();
  } catch { box.innerHTML = `<div class="sw-head">配台统计</div><div class="sw-empty">配台统计载入失败</div>`; return; }
  const staff = d.staff || [];
  if (!staff.length) { box.innerHTML = `<div class="sw-head">配台统计</div><div class="sw-empty">当天暂无配台记录</div>`; return; }
  const ROLE_ORDER = ["医生", "护士", "咨询师", "助理"];
  const byRole = {};
  staff.forEach(s => { (byRole[s.role] = byRole[s.role] || []).push(s); });
  const groups = ROLE_ORDER.filter(r => byRole[r]).map(role => {
    const rows = byRole[role].map(s =>
      `<div class="sw-row"><span class="sw-name">${escapeHtml(s.name)}</span><span>${formatCount(s.order_count)}单</span><span>${formatCount(s.item_count)}项</span><span class="sw-amt">${formatMoney(s.amount)}</span></div>`
    ).join("");
    return `<div class="sw-group"><div class="sw-role">${role}</div>${rows}</div>`;
  }).join("");
  box.innerHTML = `<div class="sw-head">配台统计（当日）</div><div class="sw-body">${groups}</div>`;
}

// 今日营收小结条：当天实收(按方式)+待收
async function loadTodayCollection() {
  const box = modulePanel && modulePanel.querySelector("[data-today-collection]");
  if (!box) return;
  // 带上当前工作日期，与未结收费列表口径一致
  const day = (typeof workDate !== "undefined" && workDate && workDate.value) ? workDate.value : "";
  const q = day ? `?date=${encodeURIComponent(day)}` : "";
  let d;
  // 禁止兜底——接口失败不能把今日营收渲染成 0.00,明确显示载入失败;
  // 同时清掉收款方式饼图,否则切换工作日期失败时旧日期饼图残留冒充当前
  const fail = () => {
    box.innerHTML = `<span class="tc-collected">今日营收载入失败</span>`;
    const pmc = _echartsInit("chartPayMethods");
    if (pmc) pmc.setOption({title: {text: "今日收款方式载入失败", left: "center",
      textStyle: {fontSize: 13, color: cssToken("--danger")}}, series: []}, true);
  };
  try {
    const res = await fetch(`/api/reports/today-collection${q}`);
    if (!res.ok) { fail(); return; }
    d = await res.json();
  } catch { fail(); return; }
  const methods = (d.by_method || []).map(m => `${escapeHtml(m.method)} ${formatMoney(m.amount)}`).join("　");
  box.innerHTML = `
    <span class="tc-collected">今日实收 <strong>${formatMoney(d.collected)}</strong>（${formatCount(d.count)}笔）</span>
    ${methods ? `<span class="tc-methods">${methods}</span>` : ""}
    <span class="tc-pending">待收 <strong>${formatMoney(d.pending_total)}</strong>（${formatCount(d.pending_count)}单）</span>`;
  renderPayMethodsChart(d.by_method || []);
}

// 今日收款方式饼图（数据源同今日营收）
function renderPayMethodsChart(byMethod) {
  const chart = _echartsInit("chartPayMethods");
  if (!chart) return;
  const data = byMethod.map(m => ({name: m.method || "未填", value: m.amount || 0}));
  chart.setOption({
    title: {text: "今日收款方式", left: "center", textStyle: {fontSize: 13}},
    tooltip: {trigger: "item", valueFormatter: v => "¥" + Number(v).toFixed(2)},
    legend: {bottom: 0, type: "scroll", textStyle: {fontSize: 10}},
    series: [{
      type: "pie", radius: ["35%", "62%"], center: ["50%", "48%"],
      data: data.length ? data : [{name: "暂无收款", value: 0}],
      label: {fontSize: 10},
    }],
  });
}

function billingContextChip(context = {}, reloadFunctionName) {
  const labels = {
    settled_today: "今日结算",
    unpaid: "待缴费",
  };
  const label = labels[context.billingFilter];
  if (!label) return "";
  return `
    <span class="module-filter-chip">
      当前过滤：${escapeHtml(label)}
      <button type="button" class="plain-button" onclick="clearModuleFilter('${reloadFunctionName}')">清除过滤</button>
    </span>
  `;
}

function renderBillingModuleRow(row) {
  const patientId = row.patient_identity || "";
  return `
    <div class="module-row" role="button" tabindex="0" data-patient-id="${escapeAttr(patientId)}">
      <span>${escapeHtml(row.bill_time || "")}</span>
      <span>${escapeHtml(row.display_name || "(无名)")}</span>
      <span>${escapeHtml(row.bill_no || "")}</span>
      <span>${escapeHtml(formatMoney(row.unpaid_fee))}</span>
      <span>${escapeHtml(billStateLabel(row.state, row.total_fee, row.paid_fee))}</span>
      ${row.bill_id ? `<span class="module-bill-actions">
        ${(row.state === "pending" || row.state === "partial")
          ? `<button type="button" class="module-pay-btn" onclick="event.stopPropagation(); confirmBillPayment('${escapeAttr(row.bill_id)}', ${row.unpaid_fee || 0}, loadBillingModule)">收款</button>` : ""}
        ${(row.state === "paid" && String(row.bill_id).indexOf("local-bill-") === 0 && canRefund())
          ? `<button type="button" class="module-refund-btn" onclick="event.stopPropagation(); confirmBillRefund('${escapeAttr(row.bill_id)}', loadBillingModule)">退费</button>` : ""}
        <button type="button" class="module-print-btn"
          data-bill-id="${escapeAttr(row.bill_id)}" data-patient-id="${escapeAttr(patientId)}" data-patient-name="${escapeAttr(row.display_name || "")}"
          data-total="${row.total_fee != null ? row.total_fee : ""}" data-paid="${row.paid_fee != null ? row.paid_fee : ""}"
          data-state="${escapeAttr(row.state || "")}" data-bill-no="${escapeAttr(row.bill_no || "")}" data-bill-time="${escapeAttr(row.bill_time || "")}"
          onclick="event.stopPropagation(); printBillFromEl(this)">打印</button>
      </span>` : moduleRowAction(patientId)}
    </div>
  `;
}

// 患者管理中心：今日 / 全部 / 最近 三 tab + 分类栏 + 卡片(对标官方原系统)
// 全部 tab 为主从式：左分类树(可折叠展开该组患者) + 右选中患者主页信息

Object.assign(window, {
  loadBillingModule,
  renderBillingModuleRow,
});
