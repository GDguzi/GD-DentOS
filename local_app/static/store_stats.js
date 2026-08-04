// 店内统计前端。挂在「配置管理 → 报表」tab 体系内，由 reports.js 分发。
// 医生/护士/助理/咨询师 收费统计共用本文件一套渲染，role 参数区分。

const _rolePerfState = {};   // role -> {from, to, staff}
const _ROLE_LABEL = {doctor: "处置医生", nurse: "护士", consultant: "咨询师", assistant: "助理"};

// 默认查「当月到今天」(北京),避免默认只查今天看着空。#541:走 localDateValue()(把北京 bjToday Date 格式化成 YYYY-MM-DD 字符串);bjToday() 返回 Date,勿直接 .slice。
function _bjMonthStart() { return localDateValue().slice(0, 7) + "-01"; }

function _rpState(role) {
  if (!_rolePerfState[role]) _rolePerfState[role] = {from: _bjMonthStart(), to: localDateValue(), staff: ""};
  return _rolePerfState[role];
}

function loadRolePerfTab(body, role) {
  const st = _rpState(role);
  const label = _ROLE_LABEL[role] || "人员";
  body.innerHTML = `
    <div class="rpt-filter">
      <label>起止日期 <span class="rpt-range" data-rp-range></span></label>
      <label>${escapeHtml(label)} <input class="ord-input" data-rp-staff data-staff-role="${role === "doctor" ? "医生" : "*"}" placeholder="选填" value="${escapeAttr(st.staff)}"></label>
      <button type="button" class="plain-button" data-rp-go>查询</button>
      <span class="wh-subtabs"><b class="wh-subtab active">收费统计</b><span class="wh-subtab disabled" title="二期(依赖处置大类字典)">处置大类</span><span class="wh-subtab disabled" title="二期">大类汇总</span></span>
    </div>
    <div class="rpt-note">口径：初诊/复诊按${role === "doctor" ? "病历(每条就诊自带初/复诊)" : "处置单就诊类型"}计量；实收剔除作废、退费冲减；应收=账单净额。</div>
    <div data-rp-results><div class="module-loading">载入中...</div></div>`;
  if (typeof bindStaffInputs === "function") bindStaffInputs(body);
  const target = body.querySelector("[data-rp-results]");
  const sync = () => { st.staff = body.querySelector("[data-rp-staff]").value.trim(); };
  mountRangePicker(body.querySelector("[data-rp-range]"), {from: st.from, to: st.to,
    onApply: (f, t) => { st.from = f; st.to = t; sync(); fetchRolePerf(target, role); }});
  body.querySelector("[data-rp-go]").addEventListener("click", () => { sync(); fetchRolePerf(target, role); });
  body.querySelector("[data-rp-staff]").addEventListener("keydown", e => { if (e.key === "Enter") { sync(); fetchRolePerf(target, role); } });
  fetchRolePerf(target, role);
}

async function fetchRolePerf(target, role) {
  const st = _rpState(role);
  const p = new URLSearchParams({role});
  if (st.from) p.set("start", st.from);
  if (st.to) p.set("end", st.to);
  if (st.staff) p.set("staff", st.staff);
  target.innerHTML = `<div class="module-loading">载入中...</div>`;
  let d;
  try {
    const res = await fetch(`/api/store-stats/role-perf?${p.toString()}`);
    if (!res.ok) { const m = await res.json().catch(() => ({})); target.innerHTML = `<div class="module-loading">${escapeHtml(m.detail || "载入失败")}</div>`; return; }
    d = await res.json();
  } catch { target.innerHTML = `<div class="module-loading">载入失败（网络异常）</div>`; return; }
  const num = v => (v != null ? escapeHtml(String(v)) : "");
  const money = v => (v != null ? escapeHtml(Number(v).toFixed(2)) : "");
  const label = _ROLE_LABEL[role] || "人员";
  const rows = d.rows || [];
  const head = `<div class="rpt-summary">${escapeHtml(d.start || "")} ~ ${escapeHtml(d.end || "")}　共 <b>${rows.length}</b> 人</div>`;
  if (!rows.length) {
    // GD-07:空结果要区分"确实无就诊"和"有就诊但无法归类"(病历没填医生且线索不唯一)
    const u0 = d.unclassified;
    const unclCount = u0 ? (Number(u0.first.visit) || 0) + (Number(u0.revisit.visit) || 0) : 0;
    const emptyMsg = (role === "doctor" || role === "nurse")
      ? (unclCount > 0
        ? `该区间有 ${unclCount} 次就诊无法唯一归类到医生(病历未填医生,预约/处置线索也不唯一),未计入任何人`
        : "该区间无就诊记录")
      : "本地暂无该角色的配台数据";
    target.innerHTML = head + `<div class="empty">${emptyMsg}</div>`;
    return;
  }
  const cell = (r, side) => `
      <td style="text-align:right">${num(r[side].visit)}</td>
      <td style="text-align:right">${num(r[side].deal)}</td>
      <td style="text-align:right"><b>${money(r[side].paid)}</b></td>
      <td style="text-align:right">${money(r[side].receivable)}</td>`;
  const trs = rows.map(r => `<tr><td>${escapeHtml(r.name || "")}</td>${cell(r, "first")}${cell(r, "revisit")}</tr>`).join("");
  const t = d.total || {first: {}, revisit: {}};
  const totalRow = `<tr class="rpt-total"><td><b>总合计</b></td>${cell(t, "first")}${cell(t, "revisit")}</tr>`;
  // 未归类:关联不上就诊的收款 + 无法唯一归类到医生的就诊量(GD-07),有数才显示
  let uncl = "";
  const u = d.unclassified;
  if (u && (u.first.paid || u.first.receivable || u.first.visit || u.revisit.visit)) {
    uncl = `<tr class="rpt-uncl"><td>(未归类)</td>
      <td style="text-align:right">${num(u.first.visit) || "—"}</td><td style="text-align:right">—</td>
      <td style="text-align:right"><b>${money(u.first.paid)}</b></td><td style="text-align:right">${money(u.first.receivable)}</td>
      <td style="text-align:right">${num(u.revisit.visit) || "—"}</td>
      <td colspan="3" style="text-align:left;color:#888">病历缺医生或账单缺就诊关联，单列不并入具体医生</td></tr>`;
  }
  target.innerHTML = head + `<div class="rpt-scroll">
    <table class="data-table">
      <thead>
        <tr><th rowspan="2">${escapeHtml(label)}</th><th colspan="4">初诊</th><th colspan="4">复诊</th></tr>
        <tr><th style="text-align:right">量</th><th style="text-align:right">成交人数</th><th style="text-align:right">实收金额</th><th style="text-align:right">应收金额</th>
            <th style="text-align:right">量</th><th style="text-align:right">成交人数</th><th style="text-align:right">实收金额</th><th style="text-align:right">应收金额</th></tr>
      </thead>
      <tbody>${trs}${uncl}${totalRow}</tbody>
    </table></div>`;
}

// ===== 收入统计(日×收款方式 + 应收款项/实际收款/新增欠款/补交欠款 + 期初/期末欠款) =====
let _incStatFrom = "", _incStatTo = "";

function loadIncomeStatTab(body) {
  if (!_incStatFrom) { _incStatFrom = _bjMonthStart(); _incStatTo = localDateValue(); }
  body.innerHTML = `
    <div class="rpt-filter">
      <label>起止日期 <span class="rpt-range" data-inc-range></span></label>
      <button type="button" class="plain-button" data-inc-go>查询</button>
      <button type="button" class="plain-button" data-inc-clear>清空</button>
      <button type="button" class="plain-button" data-inc-csv>导出CSV</button>
    </div>
    <div data-inc-results><div class="module-loading">载入中...</div></div>`;
  const target = body.querySelector("[data-inc-results]");
  const params = () => { const p = new URLSearchParams(); if (_incStatFrom) p.set("date_from", _incStatFrom); if (_incStatTo) p.set("date_to", _incStatTo); return p.toString(); };
  mountRangePicker(body.querySelector("[data-inc-range]"), {from: _incStatFrom, to: _incStatTo,
    onApply: (f, t) => { _incStatFrom = f; _incStatTo = t; fetchIncomeStat(target); }});
  body.querySelector("[data-inc-go]").addEventListener("click", () => fetchIncomeStat(target));
  body.querySelector("[data-inc-clear]").addEventListener("click", () => { _incStatFrom = ""; _incStatTo = ""; loadIncomeStatTab(body); });
  body.querySelector("[data-inc-csv]").addEventListener("click", () => window.open(`/api/reports/income.csv?${params()}`, "_blank"));
  fetchIncomeStat(target);
}

async function fetchIncomeStat(target) {
  const p = new URLSearchParams();
  if (_incStatFrom) p.set("date_from", _incStatFrom);
  if (_incStatTo) p.set("date_to", _incStatTo);
  target.innerHTML = `<div class="module-loading">载入中...</div>`;
  let d;
  try {
    const res = await fetch(`/api/reports/income?${p.toString()}`);
    if (!res.ok) { const m = await res.json().catch(() => ({})); target.innerHTML = `<div class="module-loading">${escapeHtml(m.detail || "载入失败")}</div>`; return; }
    d = await res.json();
  } catch { target.innerHTML = `<div class="module-loading">载入失败（网络异常）</div>`; return; }
  const money = v => (v ? escapeHtml(Number(v).toFixed(2)) : "");
  const methods = d.methods || [];
  const days = d.days || [];
  const head = `<div class="rpt-summary">${escapeHtml(d.date_from || "")} ~ ${escapeHtml(d.date_to || "")}　`
    + `期初欠款 <b class="rpt-overdue">${money(d.opening_arrears) || "0.00"}</b>　`
    + `期末欠款 <b class="rpt-overdue">${money(d.closing_arrears) || "0.00"}</b></div>`;
  if (!days.length) { target.innerHTML = head + `<div class="empty">该区间无收入记录</div>`; return; }
  const mHead = methods.map(m => `<th style="text-align:right">${escapeHtml(m)}</th>`).join("");
  const rowCells = row => methods.map(m => `<td style="text-align:right">${money((row.methods || {})[m])}</td>`).join("")
    + `<td style="text-align:right">${money(row.receivable)}</td>`
    + `<td style="text-align:right"><b>${money(row.amount)}</b></td>`
    + `<td style="text-align:right">${money(row.new_debt)}</td>`
    + `<td style="text-align:right">${money(row.repay_debt)}</td>`;
  const trs = days.map(r => `<tr><td>${escapeHtml(r.date || "")}</td>${rowCells(r)}</tr>`).join("");
  const sum = key => days.reduce((a, r) => a + (Number(r[key]) || 0), 0);
  const mSum = m => days.reduce((a, r) => a + (Number((r.methods || {})[m]) || 0), 0);
  const totalCells = methods.map(m => `<td style="text-align:right">${money(mSum(m))}</td>`).join("")
    + `<td style="text-align:right">${money(d.total_receivable)}</td>`
    + `<td style="text-align:right"><b>${money(d.total_amount)}</b></td>`
    + `<td style="text-align:right">${money(d.total_new_debt)}</td>`
    + `<td style="text-align:right">${money(d.total_repay_debt)}</td>`;
  target.innerHTML = head + `<div class="rpt-scroll">
    <table class="data-table">
      <thead><tr><th>日期</th>${mHead}<th style="text-align:right">应收款项</th><th style="text-align:right">实际收款</th><th style="text-align:right">新增欠款</th><th style="text-align:right">补交欠款</th></tr></thead>
      <tbody>${trs}<tr class="rpt-total"><td><b>总合计</b></td>${totalCells}</tr></tbody>
    </table></div>`;
}

// ===== 日结报表明细(每条就诊一行 + 患者信息 + 账单财务) =====
let _dsFrom = "", _dsTo = "";

function loadDailySettlementTab(body) {
  if (!_dsFrom) { _dsFrom = localDateValue(); _dsTo = localDateValue(); }   // 日结=按天，默认当天(可自选多天)
  body.innerHTML = `
    <div class="rpt-filter">
      <label>起止日期 <span class="rpt-range" data-ds-range></span></label>
      <button type="button" class="plain-button" data-ds-go>查询</button>
      <button type="button" class="plain-button" data-ds-csv>导出CSV</button>
    </div>
    <div class="rpt-note">每条就诊一行(病历为轴)。会员/储值卡等本地无对应数据的列不展示(禁止兜底)。</div>
    <div data-ds-results><div class="module-loading">载入中...</div></div>`;
  const target = body.querySelector("[data-ds-results]");
  const params = () => { const p = new URLSearchParams(); if (_dsFrom) p.set("start", _dsFrom); if (_dsTo) p.set("end", _dsTo); return p.toString(); };
  mountRangePicker(body.querySelector("[data-ds-range]"), {from: _dsFrom, to: _dsTo,
    onApply: (f, t) => { _dsFrom = f; _dsTo = t; fetchDailySettlement(target); }});
  body.querySelector("[data-ds-go]").addEventListener("click", () => fetchDailySettlement(target));
  body.querySelector("[data-ds-csv]").addEventListener("click", () => window.open(`/api/store-stats/daily-settlement.csv?${params()}`, "_blank"));
  fetchDailySettlement(target);
}

async function fetchDailySettlement(target) {
  const p = new URLSearchParams();
  if (_dsFrom) p.set("start", _dsFrom);
  if (_dsTo) p.set("end", _dsTo);
  target.innerHTML = `<div class="module-loading">载入中...</div>`;
  let d;
  try {
    const res = await fetch(`/api/store-stats/daily-settlement?${p.toString()}`);
    if (!res.ok) { const m = await res.json().catch(() => ({})); target.innerHTML = `<div class="module-loading">${escapeHtml(m.detail || "载入失败")}</div>`; return; }
    d = await res.json();
  } catch { target.innerHTML = `<div class="module-loading">载入失败（网络异常）</div>`; return; }
  const money = v => (v ? escapeHtml(Number(v).toFixed(2)) : "");
  const rows = d.rows || [];
  const head = `<div class="rpt-summary">${escapeHtml(d.start || "")} ~ ${escapeHtml(d.end || "")}　共 <b>${d.count || 0}</b> 条就诊</div>`;
  if (!rows.length) { target.innerHTML = head + `<div class="empty">该区间无就诊记录</div>`; return; }
  const cols = d.columns || [];
  const th = cols.map(c => `<th>${escapeHtml(c)}</th>`).join("");
  const trs = rows.map(r => `<tr>
      <td>${escapeHtml(r.visit_date || "")}</td><td>${escapeHtml(r.chart_no || "")}</td><td>${escapeHtml(r.name || "")}</td>
      <td>${escapeHtml(r.sex || "")}</td><td>${escapeHtml(r.age || "")}</td><td>${escapeHtml(String(r.created_at || "").slice(0, 10))}</td>
      <td>${escapeHtml(r.referral_source || "")}</td><td>${escapeHtml(r.phone || "")}</td><td>${escapeHtml(r.doctor || "")}</td>
      <td>${escapeHtml(r.visit_type || "")}</td><td>${escapeHtml(r.consultant || "")}</td><td>${escapeHtml(r.items || "")}</td>
      <td style="text-align:right">${money(r.total_fee)}</td><td style="text-align:right">${money(r.receivable)}</td>
      <td style="text-align:right"><b>${money(r.paid)}</b></td><td style="text-align:right">${money(r.discount)}</td>
      <td style="text-align:right">${money(r.arrears)}</td></tr>`).join("");
  const t = d.total || {};
  const totalRow = `<tr class="rpt-total"><td colspan="12"><b>总合计</b></td>
      <td style="text-align:right">${money(t.total_fee)}</td><td style="text-align:right">${money(t.receivable)}</td>
      <td style="text-align:right"><b>${money(t.paid)}</b></td><td style="text-align:right">${money(t.discount)}</td><td></td></tr>`;
  target.innerHTML = head + `<div style="overflow-x:auto"><table class="data-table"><thead><tr>${th}</tr></thead><tbody>${trs}${totalRow}</tbody></table></div>`;
}

// ===== 门诊日志(每条就诊一行，就诊管理性质) =====
let _clFrom = "", _clTo = "";

function loadClinicLogTab(body) {
  if (!_clFrom) { _clFrom = _bjMonthStart(); _clTo = localDateValue(); }
  body.innerHTML = `
    <div class="rpt-filter">
      <label>就诊日期 <span class="rpt-range" data-cl-range></span></label>
      <label>医生 <input class="ord-input" data-cl-doctor data-staff-role="医生" placeholder="选填"></label>
      <button type="button" class="plain-button" data-cl-go>查询</button>
      <button type="button" class="plain-button" data-cl-clear>清空</button>
      <button type="button" class="plain-button" data-cl-csv>导出CSV</button>
    </div>
    <div class="rpt-note">科室、病人去向本地无对应字段→留空(禁止兜底)；临床判断/措施取自病历正文。</div>
    <div data-cl-results><div class="module-loading">载入中...</div></div>`;
  if (typeof bindStaffInputs === "function") bindStaffInputs(body);
  const target = body.querySelector("[data-cl-results]");
  const doctor = () => body.querySelector("[data-cl-doctor]").value.trim();
  const params = () => { const p = new URLSearchParams(); if (_clFrom) p.set("start", _clFrom); if (_clTo) p.set("end", _clTo); if (doctor()) p.set("doctor", doctor()); return p.toString(); };
  mountRangePicker(body.querySelector("[data-cl-range]"), {from: _clFrom, to: _clTo,
    onApply: (f, t) => { _clFrom = f; _clTo = t; fetchClinicLog(target, params); }});
  body.querySelector("[data-cl-go]").addEventListener("click", () => fetchClinicLog(target, params));
  body.querySelector("[data-cl-clear]").addEventListener("click", () => { _clFrom = ""; _clTo = ""; loadClinicLogTab(body); });
  body.querySelector("[data-cl-csv]").addEventListener("click", () => window.open(`/api/store-stats/clinic-log.csv?${params()}`, "_blank"));
  fetchClinicLog(target, params);
}

async function fetchClinicLog(target, params) {
  target.innerHTML = `<div class="module-loading">载入中...</div>`;
  let d;
  try {
    const res = await fetch(`/api/store-stats/clinic-log?${params()}`);
    if (!res.ok) { const m = await res.json().catch(() => ({})); target.innerHTML = `<div class="module-loading">${escapeHtml(m.detail || "载入失败")}</div>`; return; }
    d = await res.json();
  } catch { target.innerHTML = `<div class="module-loading">载入失败（网络异常）</div>`; return; }
  const rows = d.rows || [];
  const head = `<div class="rpt-summary">${escapeHtml(d.start || "")} ~ ${escapeHtml(d.end || "")}　共 <b>${d.count || 0}</b> 条就诊</div>`;
  if (!rows.length) { target.innerHTML = head + `<div class="empty">该区间无就诊记录</div>`; return; }
  const th = (d.columns || []).map(c => `<th>${escapeHtml(c)}</th>`).join("");
  const trs = rows.map(r => `<tr>
      <td>${escapeHtml(r.visit_time || "")}</td><td>${escapeHtml(r.chart_no || "")}</td><td>${escapeHtml(r.name || "")}</td>
      <td>${escapeHtml(r.age || "")}</td><td>${escapeHtml(r.doctor || "")}</td><td>${escapeHtml(r.department || "")}</td>
      <td>${escapeHtml(r.visit_type || "")}</td><td>${escapeHtml(r.address || "")}</td>
      <td style="max-width:260px">${escapeHtml(r.diagnosis || "")}</td><td style="max-width:360px">${escapeHtml(r.treatment || "")}</td>
      <td>${escapeHtml(r.destination || "")}</td></tr>`).join("");
  target.innerHTML = head + `<div style="overflow-x:auto"><table class="data-table"><thead><tr>${th}</tr></thead><tbody>${trs}</tbody></table></div>`;
}

// ===== 对账日历(月历视图，每天 收/支/合) =====
let _rcMonth = "";   // YYYY-MM，空=北京当月

function loadReconcileCalendarTab(body) {
  body.innerHTML = `
    <div class="rpt-filter">
      <button type="button" class="plain-button" data-rc-prev>◀ 上月</button>
      <b data-rc-label style="min-width:120px;text-align:center;display:inline-block"></b>
      <button type="button" class="plain-button" data-rc-next>下月 ▶</button>
      <span data-rc-summary style="margin-left:16px"></span>
    </div>
    <div class="rpt-note">收=当日收款、支=当日退费、合=净额(用户拍板：本地无支出记账，支即退费)。</div>
    <div data-rc-grid><div class="module-loading">载入中...</div></div>`;
  const shift = (delta) => {
    const base = _rcMonth || _bjMonthStr();
    let [y, m] = base.split("-").map(Number);
    m += delta;
    if (m < 1) { m = 12; y -= 1; } else if (m > 12) { m = 1; y += 1; }
    _rcMonth = `${y}-${String(m).padStart(2, "0")}`;
    fetchReconcileCalendar(body);
  };
  body.querySelector("[data-rc-prev]").addEventListener("click", () => shift(-1));
  body.querySelector("[data-rc-next]").addEventListener("click", () => shift(1));
  fetchReconcileCalendar(body);
}

function _bjMonthStr() {
  // 北京当月(YYYY-MM)。用 localDateValue()(北京日期字符串);bjToday() 是 Date 不能 .slice。
  return localDateValue().slice(0, 7);
}

async function fetchReconcileCalendar(body) {
  const grid = body.querySelector("[data-rc-grid]");
  grid.innerHTML = `<div class="module-loading">载入中...</div>`;
  let d;
  try {
    const q = _rcMonth ? `?month=${encodeURIComponent(_rcMonth)}` : "";
    const res = await fetch(`/api/store-stats/reconcile-calendar${q}`);
    if (!res.ok) { const m = await res.json().catch(() => ({})); grid.innerHTML = `<div class="module-loading">${escapeHtml(m.detail || "载入失败")}</div>`; return; }
    d = await res.json();
  } catch { grid.innerHTML = `<div class="module-loading">载入失败（网络异常）</div>`; return; }
  _rcMonth = d.month;
  const money = v => (v ? Number(v).toFixed(2) : "0.00");
  body.querySelector("[data-rc-label]").textContent = `${d.year}年${String(d.month_num).padStart(2, "0")}月`;
  body.querySelector("[data-rc-summary]").innerHTML =
    `收 <b class="txt-ok">${money(d.total_income)}</b>　支 <b class="txt-danger">${money(d.total_expense)}</b>　余 <b>${money(d.total_net)}</b>`;
  const days = d.days || {};
  const firstDow = new Date(Date.UTC(d.year, d.month_num - 1, 1)).getUTCDay();  // 0=周日
  const daysInMonth = new Date(Date.UTC(d.year, d.month_num, 0)).getUTCDate();
  const weekHead = ["周日", "周一", "周二", "周三", "周四", "周五", "周六"]
    .map(w => `<th>${w}</th>`).join("");
  let cells = "";
  for (let i = 0; i < firstDow; i++) cells += `<td class="rc-empty"></td>`;
  for (let day = 1; day <= daysInMonth; day++) {
    const key = `${d.month}-${String(day).padStart(2, "0")}`;
    const info = days[key];
    const body_ = info
      ? `<div class="rc-in">收 ${money(info.income)}</div><div class="rc-out">支 ${money(info.expense)}</div><div class="rc-net">合 ${money(info.net)}</div>`
      : "";
    cells += `<td class="rc-day"><div class="rc-num">${day}</div>${body_}</td>`;
  }
  const total = firstDow + daysInMonth;
  for (let i = total; i % 7 !== 0; i++) cells += `<td class="rc-empty"></td>`;
  let rowsHtml = "", tds = cells.match(/<td[\s\S]*?<\/td>/g) || [];
  for (let i = 0; i < tds.length; i += 7) rowsHtml += `<tr>${tds.slice(i, i + 7).join("")}</tr>`;
  grid.innerHTML = `<table class="rc-calendar"><thead><tr>${weekHead}</tr></thead><tbody>${rowsHtml}</tbody></table>`;
}

Object.assign(window, { loadRolePerfTab, loadIncomeStatTab, loadDailySettlementTab, loadClinicLogTab, loadReconcileCalendarTab });
