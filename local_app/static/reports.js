// 报表中心：把已验证的后端 /api/reports/arrears(欠费清单) 与 /api/lab-orders/in-progress(在制义齿)
// 做成可点界面。纯对接，数据走 fetch + 事件委托(不拼内联JS)。
// #617:已从顶层导航搬进配置管理"报表"子tab，渲染进 config_center.js 的 cfgBody；
// 收费管理(原独立顶层入口，前台基本用不上)并入本tab，点了直接跳转回 billing 全屏视图(今日工作台深链复用同一视图)。
let _rptTab = "docstat";

// 左侧竖菜单分组(对标 SaaS「本店统计」左栏)。第一组 9 项与 SaaS 完全一致；第二组是本地扩展报表。
const RPT_GROUPS = [
  ["本店统计", [
    ["docstat", "🩺", "医生统计"], ["nursestat", "💉", "护士统计"], ["asststat", "🧰", "助理统计"],
    ["consultstat", "💬", "咨询师统计"], ["cashier", "💴", "收银查询"], ["dailysettle", "📄", "日结报表"],
    ["incomestat", "📊", "收入统计"], ["cliniclog", "📋", "门诊日志"], ["reconcilecal", "📅", "对账日历"],
  ]],
  ["其他报表", [
    ["arrears", "🧾", "欠费清单"], ["lab", "🦷", "在制义齿"], ["income", "💵", "营收日报"],
    ["monthly", "🗓️", "月度汇总"], ["daily", "🧮", "日结单"], ["staffperf", "🏅", "员工业绩"],
    ["handlecat", "📦", "处置大类"], ["reconcile", "🔁", "双跑对账"], ["billing", "💳", "收费管理"],
  ]],
];

function loadReportsModule() {
  const modulePanel = document.getElementById("cfgBody");
  if (!modulePanel) return;
  // #691:billing tab 是"跳转回收费管理全屏"的外链(会 switchWorkspaceView)。若上次停在它,
  // 再进报表会立刻跳走、回不来。进模块时把停在跳转 tab 的状态重置为默认(医生统计)。
  if (_rptTab === "billing") _rptTab = "docstat";
  modulePanel.innerHTML = `
    <div class="rpt-shell">
      <nav class="rpt-sidenav" data-rpt-nav aria-label="店内统计"></nav>
      <div class="rpt-content"><div id="rptBody" class="module-body"></div></div>
    </div>
  `;
  renderRptTabs(modulePanel);
}

function renderRptTabs(root) {
  const nav = root.querySelector("[data-rpt-nav]");
  if (!nav) return;
  nav.innerHTML = RPT_GROUPS.map(([group, items]) => `
    <div class="rpt-nav-group">
      <div class="rpt-nav-title">${group}</div>
      ${items.map(([k, icon, label]) =>
        `<button type="button" class="rpt-nav-item${k === _rptTab ? " active" : ""}" data-rpt-tab="${k}">
           <span class="rpt-nav-icon">${icon}</span><span>${label}</span>
         </button>`).join("")}
    </div>`).join("");
  nav.querySelectorAll("[data-rpt-tab]").forEach(btn =>
    btn.addEventListener("click", () => { _rptTab = btn.dataset.rptTab; renderRptTabs(root); }));
  loadRptTab();
}

const RPT_LOADERS = {
  docstat: b => loadRolePerfTab(b, "doctor"), nursestat: b => loadRolePerfTab(b, "nurse"),
  asststat: b => loadRolePerfTab(b, "assistant"), consultstat: b => loadRolePerfTab(b, "consultant"),
  incomestat: b => loadIncomeStatTab(b), dailysettle: b => loadDailySettlementTab(b),
  cliniclog: b => loadClinicLogTab(b), reconcilecal: b => loadReconcileCalendarTab(b),
  arrears: b => loadArrearsTab(b), income: b => loadIncomeTab(b), monthly: b => loadMonthlyTab(b),
  daily: b => loadDailyTab(b), staffperf: b => loadStaffPerfTab(b), cashier: b => loadCashierTab(b),
  handlecat: b => loadHandleCatTab(b), reconcile: b => loadReconcileTab(b), lab: b => loadLabLedgerTab(b),
};

function loadRptTab() {
  // 收费管理不是"报表"标签页里的内容,是跳转回独立的 billing 全屏视图(与今日工作台深链共用)。
  if (_rptTab === "billing") { switchWorkspaceView("billing"); return; }
  const body = document.getElementById("rptBody");
  if (!body) return;
  body.innerHTML = `<div class="module-loading">载入中...</div>`;
  const loader = RPT_LOADERS[_rptTab] || RPT_LOADERS.lab;
  // 兜住加载器同步抛错(如某函数未定义/依赖缺失),把真实错误显示出来而不是干卡"载入中"
  try {
    loader(body);
  } catch (e) {
    body.innerHTML = `<div class="module-loading txt-danger" style="white-space:pre-wrap">`
      + `报表载入出错：${escapeHtml(String((e && e.message) || e))}\n（把这条截图发给开发即可定位）</div>`;
    if (window.console) console.error("[报表载入]", _rptTab, e);
  }
  return;
}

let _arrearsQ = "";       // 欠费清单筛选：患者姓名/电话/病历号
let _arrearsMin = "";     // 最低欠费额

function loadArrearsTab(body) {
  // 筛选条常驻 + 结果区单独刷新，避免每次查询丢输入焦点
  body.innerHTML = `
    <div class="rpt-filter">
      <input class="ord-input" data-arrears-q placeholder="患者姓名 / 电话 / 病历号" value="${escapeAttr(_arrearsQ)}">
      <input class="ord-input" data-arrears-min placeholder="最低欠费额" inputmode="decimal" value="${escapeAttr(_arrearsMin)}">
      <button type="button" class="plain-button" data-arrears-go>查询</button>
      <button type="button" class="plain-button" data-arrears-clear>清空</button>
    </div>
    <div data-arrears-results><div class="module-loading">载入中...</div></div>`;
  const qEl = body.querySelector("[data-arrears-q]");
  const minEl = body.querySelector("[data-arrears-min]");
  const target = body.querySelector("[data-arrears-results]");
  const run = () => { _arrearsQ = qEl.value.trim(); _arrearsMin = minEl.value.trim(); fetchArrears(target); };
  body.querySelector("[data-arrears-go]").addEventListener("click", run);
  body.querySelector("[data-arrears-clear]").addEventListener("click", () => {
    _arrearsQ = ""; _arrearsMin = ""; qEl.value = ""; minEl.value = ""; fetchArrears(target);
  });
  [qEl, minEl].forEach(el => el.addEventListener("keydown", e => { if (e.key === "Enter") run(); }));
  fetchArrears(target);
}

async function fetchArrears(target) {
  const params = new URLSearchParams();
  if (_arrearsQ) params.set("q", _arrearsQ);
  // #162：严格十进制有限正数才发，挡 '123abc'/'1,000'/'Infinity' 这类 parseFloat 宽松值
  if (/^\d+(\.\d+)?$/.test(_arrearsMin) && parseFloat(_arrearsMin) > 0) {
    params.set("min_arrears", _arrearsMin);
  }
  target.innerHTML = `<div class="module-loading">载入中...</div>`;
  let d;
  try {
    const res = await fetch(`/api/reports/arrears?${params.toString()}`);
    if (!res.ok) { target.innerHTML = `<div class="module-loading">载入失败，请稍后重试</div>`; return; }
    d = await res.json();
  } catch { target.innerHTML = `<div class="module-loading">载入失败（网络异常）</div>`; return; }
  const rows = d.rows || [];
  const head = `<div class="rpt-summary">未结欠费 <b>${rows.length}</b> 笔，合计欠费 <b>${d.total_arrears != null ? d.total_arrears : 0}</b> 元</div>`;
  if (!rows.length) { target.innerHTML = head + `<div class="empty">暂无欠费单</div>`; return; }
  const trs = rows.map(r => `
    <tr>
      <td>${escapeHtml(r.bill_no || r.bill_id || "")}</td>
      <td>${escapeHtml(r.display_name || "")}</td>
      <td>${escapeHtml(r.phone || "")}</td>
      <td>${escapeHtml(String(r.bill_time || "").slice(0, 16))}</td>
      <td style="text-align:right">${r.total_fee != null ? r.total_fee : ""}</td>
      <td style="text-align:right">${r.paid_fee != null ? r.paid_fee : ""}</td>
      <td style="text-align:right"><b>${r.arrears != null ? r.arrears : ""}</b></td>
    </tr>`).join("");
  target.innerHTML = head + `
    <table class="wh-table">
      <thead><tr><th>账单号</th><th>患者</th><th>电话</th><th>账单时间</th><th>应收</th><th>已收</th><th>欠费</th></tr></thead>
      <tbody>${trs}</tbody>
    </table>`;
}

async function loadLabLedgerTab(body) {
  let d;
  try {
    const res = await fetch("/api/lab-orders/in-progress");
    if (!res.ok) { body.innerHTML = `<div class="module-loading">载入失败，请稍后重试</div>`; return; }
    d = await res.json();
  } catch { body.innerHTML = `<div class="module-loading">载入失败（网络异常）</div>`; return; }
  const rows = d.rows || [];
  const head = `<div class="rpt-summary">在制 <b>${rows.length}</b> 件，超 ${d.overdue_days != null ? d.overdue_days : 7} 天逾期 <b class="rpt-overdue">${d.overdue_count != null ? d.overdue_count : 0}</b> 件</div>`;
  if (!rows.length) { body.innerHTML = head + `<div class="empty">暂无在制技工单</div>`; return; }
  const trs = rows.map(r => `
    <tr${r.overdue ? ' class="rpt-row-overdue"' : ""}>
      <td>${escapeHtml(r.patient_name || "")}</td>
      <td>${escapeHtml(r.project_type || "")}</td>
      <td>${escapeHtml(r.lab_name || "")}</td>
      <td>${escapeHtml(r.tooth_codes || "")}</td>
      <td>${escapeHtml(r.color || "")}</td>
      <td>${escapeHtml(r.sent_date || "")}</td>
      <td style="text-align:right">${r.days_sent != null ? r.days_sent : ""}${r.overdue ? " ⚠" : ""}</td>
    </tr>`).join("");
  body.innerHTML = head + `
    <table class="wh-table">
      <thead><tr><th>患者</th><th>项目</th><th>加工厂</th><th>牙位</th><th>颜色</th><th>送出日</th><th>已送天数</th></tr></thead>
      <tbody>${trs}</tbody>
    </table>`;
}

let _incFrom = "";   // 营收日报区间(空=后端默认今天)
let _incTo = "";

function loadIncomeTab(body) {
  body.innerHTML = `
    <div class="rpt-filter">
      <label>起止日期 <span class="rpt-range" data-inc-range></span></label>
      <button type="button" class="plain-button" data-inc-go>查询</button>
    </div>
    <div data-inc-results><div class="module-loading">载入中...</div></div>`;
  const target = body.querySelector("[data-inc-results]");
  mountRangePicker(body.querySelector("[data-inc-range]"), {from: _incFrom, to: _incTo,
    onApply: (f, t) => { _incFrom = f; _incTo = t; fetchIncome(target); }});
  body.querySelector("[data-inc-go]").addEventListener("click", () => fetchIncome(target));
  fetchIncome(target);
}

async function fetchIncome(target) {
  const params = new URLSearchParams();
  if (_incFrom) params.set("date_from", _incFrom);
  if (_incTo) params.set("date_to", _incTo);
  target.innerHTML = `<div class="module-loading">载入中...</div>`;
  let d;
  try {
    const res = await fetch(`/api/reports/income?${params.toString()}`);
    if (!res.ok) {
      const m = await res.json().catch(() => ({}));
      target.innerHTML = `<div class="module-loading">${escapeHtml(m.detail || "载入失败，请稍后重试")}</div>`;
      return;
    }
    d = await res.json();
  } catch { target.innerHTML = `<div class="module-loading">载入失败（网络异常）</div>`; return; }
  const days = d.days || [];
  const methods = d.methods || [];
  const num = v => v != null ? escapeHtml(String(v)) : ""; // #171：数字也统一转义,口径一致
  const money = v => (v != null && v !== 0) ? escapeHtml(String(v)) : "";
  const head = `<div class="rpt-summary">${escapeHtml(d.date_from || "")} ~ ${escapeHtml(d.date_to || "")}　实收合计 <b>${num(d.total_amount) || 0}</b> 元，应收 <b>${num(d.total_receivable) || 0}</b>，<b>${num(d.total_count) || 0}</b> 笔</div>`;
  if (!days.length) { target.innerHTML = head + `<div class="empty">该区间无收款记录</div>`; return; }
  const mhead = methods.map(m => `<th style="text-align:right">${escapeHtml(m)}</th>`).join("");
  const trs = days.map(r => `
    <tr>
      <td>${escapeHtml(r.date || "")}</td>
      ${methods.map(m => `<td style="text-align:right">${money((r.methods || {})[m])}</td>`).join("")}
      <td style="text-align:right">${money(r.receivable)}</td>
      <td style="text-align:right"><b>${num(r.amount)}</b></td>
      <td style="text-align:right">${num(r.count)}</td>
    </tr>`).join("");
  target.innerHTML = head + `
    <table class="wh-table">
      <thead><tr><th>日期</th>${mhead}<th style="text-align:right">应收</th><th style="text-align:right">实收(元)</th><th style="text-align:right">笔数</th></tr></thead>
      <tbody>${trs}</tbody>
    </table>`;
}

// 收银查询：逐笔收款 + 付款方式 + 处置类别 + 按日期/医生/来源/方式筛 + 导出CSV
let _cashFrom = "", _cashTo = "", _cashDoctor = "", _cashSource = "", _cashMethod = "", _cashOperator = "";

function loadCashierTab(body) {
  body.innerHTML = `
    <div class="rpt-filter">
      <label>起止日期 <span class="rpt-range" data-cash-range></span></label>
      <label>医生 <input class="ord-input" data-cash-doctor data-staff-role="医生" placeholder="选填" value="${escapeAttr(_cashDoctor)}"></label>
      <label>来源 <input class="ord-input" data-cash-source placeholder="选填" value="${escapeAttr(_cashSource)}"></label>
      <label>方式 <input class="ord-input" data-cash-method placeholder="选填" value="${escapeAttr(_cashMethod)}"></label>
      <label>收银员 <input class="ord-input" data-cash-operator data-staff-role="*" placeholder="选填" value="${escapeAttr(_cashOperator)}"></label>
      <button type="button" class="plain-button" data-cash-go>查询</button>
      <button type="button" class="plain-button" data-cash-csv>导出CSV</button>
    </div>
    <div data-cash-results><div class="module-loading">载入中...</div></div>`;
  if (typeof bindStaffInputs === "function") bindStaffInputs(body);   // #420二批:医生/收银员筛选选人
  const get = sel => body.querySelector(sel).value.trim();
  const sync = () => {
    _cashDoctor = get("[data-cash-doctor]"); _cashSource = get("[data-cash-source]");
    _cashMethod = get("[data-cash-method]"); _cashOperator = get("[data-cash-operator]");
  };
  const target = body.querySelector("[data-cash-results]");
  mountRangePicker(body.querySelector("[data-cash-range]"), {from: _cashFrom, to: _cashTo,
    onApply: (f, t) => { _cashFrom = f; _cashTo = t; sync(); fetchCashier(target); }});
  body.querySelector("[data-cash-go]").addEventListener("click", () => { sync(); fetchCashier(target); });
  body.querySelector("[data-cash-csv]").addEventListener("click", () => { sync(); window.open(`/api/reports/cashier-query.csv?${_cashParams()}`, "_blank"); });
  body.querySelectorAll(".ord-input").forEach(el => el.addEventListener("keydown", e => { if (e.key === "Enter") { sync(); fetchCashier(target); } }));
  fetchCashier(target);
}

function _cashParams() {
  const p = new URLSearchParams();
  if (_cashFrom) p.set("date_from", _cashFrom);
  if (_cashTo) p.set("date_to", _cashTo);
  if (_cashDoctor) p.set("doctor", _cashDoctor);
  if (_cashSource) p.set("source", _cashSource);
  if (_cashMethod) p.set("method", _cashMethod);
  if (_cashOperator) p.set("operator", _cashOperator);
  return p.toString();
}

async function fetchCashier(target) {
  target.innerHTML = `<div class="module-loading">载入中...</div>`;
  let d;
  try {
    const res = await fetch(`/api/reports/cashier-query?${_cashParams()}`);
    if (!res.ok) { const m = await res.json().catch(() => ({})); target.innerHTML = `<div class="module-loading">${escapeHtml(m.detail || "载入失败")}</div>`; return; }
    d = await res.json();
  } catch { target.innerHTML = `<div class="module-loading">载入失败（网络异常）</div>`; return; }
  const num = v => v != null ? escapeHtml(String(v)) : "";
  const byMethod = Object.entries(d.by_method || {}).map(([k, v]) => `${escapeHtml(k)} ${num(v)}`).join(" · ");
  const head = `<div class="rpt-summary">${escapeHtml(d.date_from || "")} ~ ${escapeHtml(d.date_to || "")}　实收合计 <b>${num(d.total_amount) || 0}</b> 元，<b>${num(d.count) || 0}</b> 笔　[${byMethod}]</div>`;
  const rows = d.rows || [];
  if (!rows.length) { target.innerHTML = head + `<div class="empty">该区间无收款记录</div>`; return; }
  const trs = rows.map(r => {
    const cats = Object.entries(r.categories || {}).map(([k, v]) => `${escapeHtml(k)}${num(v)}`).join("、");
    return `<tr>
      <td>${escapeHtml(String(r.pay_time || "").slice(0, 16))}</td>
      <td>${escapeHtml(r.chart_no || "")}</td>
      <td>${escapeHtml(r.display_name || "")}</td>
      <td>${escapeHtml(r.phone || "")}</td>
      <td>${escapeHtml(r.referral_source || "")}</td>
      <td>${escapeHtml(r.referral_source2 || "")}</td>
      <td>${escapeHtml(r.referral_source3 || "")}</td>
      <td>${escapeHtml(r.doctor || "")}</td>
      <td>${escapeHtml(r.operator || "")}</td>
      <td>${escapeHtml(r.pay_type || "")}</td>
      <td style="text-align:right"><b>${num(r.amount)}</b></td>
      <td>${cats}</td>
    </tr>`;
  }).join("");
  target.innerHTML = head + `
    <table class="wh-table">
      <thead><tr><th>收款时间</th><th>病历号</th><th>患者</th><th>联系电话</th><th>患者来源</th><th>二级来源</th><th>三级来源</th><th>医生</th><th>收款员</th><th>方式</th><th>总收入</th><th>处置类别</th></tr></thead>
      <tbody>${trs}</tbody>
    </table>`;
}

// 处置大类：收银查询同源数据按处置类别(fee_type)分组
let _hcFrom = "", _hcTo = "";
function loadHandleCatTab(body) {
  body.innerHTML = `
    <div class="rpt-filter">
      <label>起止日期 <span class="rpt-range" data-hc-range></span></label>
      <button type="button" class="plain-button" data-hc-go>查询</button>
      <button type="button" class="plain-button" data-hc-csv>导出CSV</button>
    </div>
    <div data-hc-results><div class="module-loading">载入中...</div></div>`;
  const target = body.querySelector("[data-hc-results]");
  const params = () => { const p = new URLSearchParams(); if (_hcFrom) p.set("date_from", _hcFrom); if (_hcTo) p.set("date_to", _hcTo); return p.toString(); };
  mountRangePicker(body.querySelector("[data-hc-range]"), {from: _hcFrom, to: _hcTo,
    onApply: (f, t) => { _hcFrom = f; _hcTo = t; fetchHandleCat(target, params); }});
  body.querySelector("[data-hc-go]").addEventListener("click", () => fetchHandleCat(target, params));
  body.querySelector("[data-hc-csv]").addEventListener("click", () => window.open(`/api/reports/handle-category.csv?${params()}`, "_blank"));
  fetchHandleCat(target, params);
}
async function fetchHandleCat(target, params) {
  target.innerHTML = `<div class="module-loading">载入中...</div>`;
  let d;
  try {
    const res = await fetch(`/api/reports/handle-category?${params()}`);
    if (!res.ok) { const m = await res.json().catch(() => ({})); target.innerHTML = `<div class="module-loading">${escapeHtml(m.detail || "载入失败")}</div>`; return; }
    d = await res.json();
  } catch { target.innerHTML = `<div class="module-loading">载入失败（网络异常）</div>`; return; }
  const num = v => v != null ? escapeHtml(String(v)) : "";
  const items = d.categories || [];
  const head = `<div class="rpt-summary">${escapeHtml(d.date_from || "")} ~ ${escapeHtml(d.date_to || "")}　实收合计 <b>${num(d.total_amount) || 0}</b> 元</div>`;
  if (!items.length) { target.innerHTML = head + `<div class="empty">该区间无收款记录</div>`; return; }
  const trs = items.map(c => `<tr><td>${escapeHtml(c.fee_type || "")}</td><td style="text-align:right"><b>${num(c.amount)}</b></td><td style="text-align:right">${num(c.count)}</td></tr>`).join("");
  target.innerHTML = head + `
    <table class="wh-table">
      <thead><tr><th>处置类别</th><th>实收(元)</th><th>笔数</th></tr></thead>
      <tbody>${trs}</tbody>
    </table>`;
}

// 双跑对账：本地 vs SaaS 每日 预约/实收/患者 差异(差异≠0 标红)。数据由 8点同步写 reconcile_log
let _recFrom = "", _recTo = "";
function loadReconcileTab(body) {
  body.innerHTML = `
    <div class="rpt-filter">
      <label>起止日期 <span class="rpt-range" data-rec-range></span></label>
      <button type="button" class="plain-button" data-rec-go>查询</button>
      <span class="rpt-note">对账数据由每晚 8 点同步自动写入；差异≠0 标红</span>
    </div>
    <div data-rec-results><div class="module-loading">载入中...</div></div>`;
  const target = body.querySelector("[data-rec-results]");
  const params = () => { const p = new URLSearchParams(); if (_recFrom) p.set("date_from", _recFrom); if (_recTo) p.set("date_to", _recTo); return p.toString(); };
  mountRangePicker(body.querySelector("[data-rec-range]"), {from: _recFrom, to: _recTo,
    onApply: (f, t) => { _recFrom = f; _recTo = t; fetchReconcile(target, params); }});
  body.querySelector("[data-rec-go]").addEventListener("click", () => fetchReconcile(target, params));
  fetchReconcile(target, params);
}
async function fetchReconcile(target, params) {
  target.innerHTML = `<div class="module-loading">载入中...</div>`;
  let d;
  try {
    const res = await fetch(`/api/reports/reconcile?${params()}`);
    if (!res.ok) { const m = await res.json().catch(() => ({})); target.innerHTML = `<div class="module-loading">${escapeHtml(m.detail || "载入失败")}</div>`; return; }
    d = await res.json();
  } catch { target.innerHTML = `<div class="module-loading">载入失败（网络异常）</div>`; return; }
  const rows = d.rows || [];
  const num = v => v != null ? escapeHtml(String(v)) : "";
  const head = `<div class="rpt-summary">${escapeHtml(d.date_from || "")} ~ ${escapeHtml(d.date_to || "")}　差异天数 <b>${num(d.diff_days) || 0}</b></div>`;
  if (!rows.length) { target.innerHTML = head + `<div class="empty">该区间无对账记录（等 8 点同步或手动跑）</div>`; return; }
  const cell = (m) => { const bad = m.diff !== 0; return `<td${bad ? ' class="txt-danger"' : ''} style="text-align:right${bad ? ';font-weight:600' : ''}">${num(m.local)}/${num(m.saas)}${bad ? `(${m.diff > 0 ? "+" : ""}${num(m.diff)})` : ""}</td>`; };
  const trs = rows.map(r => `<tr>
      <td>${escapeHtml(r.date || "")}</td>
      ${cell(r.appts)}${cell(r.income)}${cell(r.patients)}
      <td>${r.ok ? "✅" : "⚠️"}</td>
    </tr>`).join("");
  target.innerHTML = head + `
    <table class="wh-table">
      <thead><tr><th>日期</th><th>预约(本地/SaaS)</th><th>实收(本地/SaaS)</th><th>患者(本地/SaaS)</th><th>对账</th></tr></thead>
      <tbody>${trs}</tbody>
    </table>`;
}

// ===== #657 月度汇总 =====
let _moFrom = "", _moTo = "";   // 空=后端默认近12个月

function loadMonthlyTab(body) {
  body.innerHTML = `
    <div class="rpt-filter">
      <label>起止日期 <span class="rpt-range" data-mo-range></span></label>
      <button type="button" class="plain-button" data-mo-go>查询</button>
    </div>
    <div data-mo-results><div class="module-loading">载入中...</div></div>`;
  const target = body.querySelector("[data-mo-results]");
  mountRangePicker(body.querySelector("[data-mo-range]"), {from: _moFrom, to: _moTo,
    onApply: (f, t) => { _moFrom = f; _moTo = t; fetchMonthly(target); }});
  body.querySelector("[data-mo-go]").addEventListener("click", () => fetchMonthly(target));
  fetchMonthly(target);
}

async function fetchMonthly(target) {
  const params = new URLSearchParams();
  if (_moFrom) params.set("date_from", _moFrom);
  if (_moTo) params.set("date_to", _moTo);
  target.innerHTML = `<div class="module-loading">载入中...</div>`;
  let d;
  try {
    const res = await fetch(`/api/reports/income-monthly?${params.toString()}`);
    if (!res.ok) {
      const m = await res.json().catch(() => ({}));
      target.innerHTML = `<div class="module-loading">${escapeHtml(m.detail || "载入失败，请稍后重试")}</div>`;
      return;
    }
    d = await res.json();
  } catch { target.innerHTML = `<div class="module-loading">载入失败（网络异常）</div>`; return; }
  const months = d.months || [];
  const num = v => v != null ? escapeHtml(String(v)) : "";
  const head = `<div class="rpt-summary">${escapeHtml(d.date_from || "")} ~ ${escapeHtml(d.date_to || "")}　实收合计 <b>${num(d.total_amount) || 0}</b> 元，应收 <b>${num(d.total_receivable) || 0}</b>，<b>${num(d.total_count) || 0}</b> 笔</div>`;
  if (!months.length) { target.innerHTML = head + `<div class="empty">该区间无收款记录</div>`; return; }
  const trs = months.map(r => `
    <tr>
      <td>${escapeHtml(r.month || "")}</td>
      <td style="text-align:right">${num(r.count)}</td>
      <td style="text-align:right"><b>${num(r.amount)}</b></td>
      <td style="text-align:right">${num(r.receivable)}</td>
    </tr>`).join("");
  target.innerHTML = head + `
    <table class="data-table">
      <thead><tr><th>月份</th><th style="text-align:right">笔数</th><th style="text-align:right">实收(元)</th><th style="text-align:right">应收(元)</th></tr></thead>
      <tbody>${trs}</tbody>
    </table>`;
}

// ===== #657 日结单(任意日期营收日报) =====
let _dailyDate = "";   // 空=今天

function loadDailyTab(body) {
  body.innerHTML = `
    <div class="rpt-filter">
      <label>日期 <input type="date" data-daily-date value="${escapeAttr(_dailyDate || localDateValue())}"></label>
      <button type="button" class="plain-button" data-daily-go>查询</button>
    </div>
    <div data-daily-results><div class="module-loading">载入中...</div></div>`;
  const target = body.querySelector("[data-daily-results]");
  const input = body.querySelector("[data-daily-date]");
  const go = () => { _dailyDate = input.value; fetchDaily(target); };
  body.querySelector("[data-daily-go]").addEventListener("click", go);
  input.addEventListener("change", go);
  fetchDaily(target);
}

async function fetchDaily(target) {
  const params = new URLSearchParams();
  if (_dailyDate) params.set("date", _dailyDate);
  target.innerHTML = `<div class="module-loading">载入中...</div>`;
  let d;
  try {
    const res = await fetch(`/api/reports/today-collection?${params.toString()}`);
    if (!res.ok) {
      const m = await res.json().catch(() => ({}));
      target.innerHTML = `<div class="module-loading">${escapeHtml(m.detail || "载入失败，请稍后重试")}</div>`;
      return;
    }
    d = await res.json();
  } catch { target.innerHTML = `<div class="module-loading">载入失败（网络异常）</div>`; return; }
  const num = v => v != null ? escapeHtml(String(v)) : "";
  const head = `<div class="rpt-summary">${escapeHtml(d.date || "")}　实收 <b>${num(d.collected) || 0}</b> 元(${num(d.count) || 0} 笔)　当日开单待收 <b>${num(d.pending_total) || 0}</b> 元(${num(d.pending_count) || 0} 单)</div>`;
  const rows = d.by_method || [];
  if (!rows.length) { target.innerHTML = head + `<div class="empty">当日无收款</div>`; return; }
  const trs = rows.map(r => `
    <tr><td>${escapeHtml(r.method || "")}</td><td style="text-align:right"><b>${num(r.amount)}</b></td></tr>`).join("");
  target.innerHTML = head + `
    <table class="data-table" style="max-width:420px">
      <thead><tr><th>收费方式</th><th style="text-align:right">金额(元)</th></tr></thead>
      <tbody>${trs}</tbody>
    </table>`;
}

// ===== #657 员工业绩(配台统计,只算已划价单) =====
let _spFrom = "", _spTo = "";

function loadStaffPerfTab(body) {
  body.innerHTML = `
    <div class="rpt-filter">
      <label>起止日期 <span class="rpt-range" data-sp-range></span></label>
      <button type="button" class="plain-button" data-sp-go>查询</button>
    </div>
    <div class="rpt-note">口径：只统计<b>已划价</b>处置单（未划价金额未定、已撤销不计）；同一单的配台各角色都记入各自名下。</div>
    <div data-sp-results><div class="module-loading">载入中...</div></div>`;
  const target = body.querySelector("[data-sp-results]");
  mountRangePicker(body.querySelector("[data-sp-range]"), {from: _spFrom, to: _spTo,
    onApply: (f, t) => { _spFrom = f; _spTo = t; fetchStaffPerf(target); }});
  body.querySelector("[data-sp-go]").addEventListener("click", () => fetchStaffPerf(target));
  fetchStaffPerf(target);
}

async function fetchStaffPerf(target) {
  const params = new URLSearchParams();
  if (_spFrom) params.set("date_from", _spFrom);
  if (_spTo) params.set("date_to", _spTo);
  target.innerHTML = `<div class="module-loading">载入中...</div>`;
  let d;
  try {
    const res = await fetch(`/api/reports/staff-workload?${params.toString()}`);
    if (!res.ok) {
      const m = await res.json().catch(() => ({}));
      target.innerHTML = `<div class="module-loading">${escapeHtml(m.detail || "载入失败，请稍后重试")}</div>`;
      return;
    }
    d = await res.json();
  } catch { target.innerHTML = `<div class="module-loading">载入失败（网络异常）</div>`; return; }
  const staff = d.staff || [];
  const num = v => v != null ? escapeHtml(String(v)) : "";
  const head = `<div class="rpt-summary">${escapeHtml(d.date_from || "")} ~ ${escapeHtml(d.date_to || "")}　共 <b>${staff.length}</b> 人</div>`;
  if (!staff.length) { target.innerHTML = head + `<div class="empty">该区间无已划价处置单</div>`; return; }
  const trs = staff.map(r => `
    <tr>
      <td>${escapeHtml(r.role || "")}</td>
      <td>${escapeHtml(r.name || "")}</td>
      <td style="text-align:right">${num(r.order_count)}</td>
      <td style="text-align:right">${num(r.item_count)}</td>
      <td style="text-align:right"><b>${num(r.amount)}</b></td>
    </tr>`).join("");
  target.innerHTML = head + `
    <table class="data-table">
      <thead><tr><th>角色</th><th>姓名</th><th style="text-align:right">处置单数</th><th style="text-align:right">项目数</th><th style="text-align:right">金额(元)</th></tr></thead>
      <tbody>${trs}</tbody>
    </table>`;
}

Object.assign(window, { loadReportsModule });
