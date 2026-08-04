let queueRooms = [];   // 诊室列表(候诊队列分诊室用)

async function loadTodayWork() {
  if (!todayWorkPanel) return false;
  todayWorkPanel.textContent = "今日工作载入中...";
  try { queueRooms = (await (await fetch("/api/settings/rooms")).json()).list || []; } catch { queueRooms = []; }
  // 工作日期可选：跟随共享 workDate（前/后箭头、点日期选日历切换），后端 /api/today-work 吃 date 参数
  const res = await fetch(`/api/today-work?date=${encodeURIComponent(workDate.value)}`);
  if (!res.ok) {
    todayWorkPanel.textContent = "今日工作载入失败";
    return false;
  }
  const data = await res.json();
  latestTodayWorkData = data;
  renderTodayWork(data);
  return true;
}

function renderTodayWork(data) {
  const summary = data.summary || {};
  todayWorkPanel.innerHTML = `
    <div class="today-dashboard">
      <section class="today-overview-grid" aria-label="今日空间总览">
        <div class="today-date-banner">
          <div class="today-date-copy">
            <span>DAILY OVERVIEW</span>
            <strong>${workDate.value === localDateValue() ? "今天，一切就绪。" : "当日工作总览"}</strong>
          </div>
          <div class="today-date-nav">
            <button type="button" class="today-date-arrow" onclick="changeWorkDate(-1)" title="前一天" aria-label="前一天">‹</button>
            <button type="button" class="today-date-display" onclick="openWorkDatePicker()" title="点击选择日期">
              <strong>${escapeHtml(todayDateLabel())}</strong>
            </button>
            <input type="date" id="workDatePicker" class="today-date-input" value="${escapeAttr(workDate.value)}" onchange="pickWorkDate(this.value)" aria-label="选择工作日期">
            <button type="button" class="today-date-arrow" onclick="changeWorkDate(1)" title="后一天" aria-label="后一天">›</button>
            <button type="button" class="today-date-jump" onclick="changeWorkDate(0)">今天</button>
          </div>
          <img class="today-tooth-motif" src="/tooth-motif.svg?v=2026-07-15-pearl-a1" alt="">
        </div>
        ${todaySaasKpiStrip(summary)}
      </section>
      ${renderUnlinkedVisitsBanner(data.unlinked_visits)}
      <section class="today-status-strip" aria-label="今日工作状态">
        ${todayStatusItem("待回访", summary.pending_return_visits)}
        ${todayStatusItem("今日待收", null, todayMoneyOrMask(summary.today_unpaid_amount))}
        <span class="today-status-spacer"></span>
        <button type="button" class="plain-button today-add-patient" onclick="openNewPatient()">+ 新增患者</button>
        <button type="button" class="plain-button today-refresh" onclick="refreshTodayWorkbench()">刷新今日</button>
        <span id="todayRefreshStatus" class="today-refresh-status"></span>
      </section>
      <div class="today-saas-layout">
        ${todaySaasEntryList(summary)}
        <div class="today-saas-main" id="todaySaasMain">
          ${renderTodayQueue(data.appointments || [])}
        </div>
      </div>
    </div>
  `;
  bindPatientDetailRows(todayWorkPanel);
  bindQueueOps(todayWorkPanel);
}

// 过去的日子，预约表可能已改期/取消(号变了),但人已经来看过病/交过钱——
// 这份是预约表没体现、但病历/账单证明真到店的人，提醒别漏看。今天不算(队列按预约走，见 api.py)。
function renderUnlinkedVisitsBanner(unlinkedVisits) {
  if (!unlinkedVisits || !unlinkedVisits.length) return "";
  const names = unlinkedVisits.map(p =>
    `<span class="today-unlinked-visit-name" data-patient-id="${escapeAttr(p.patient_identity)}">${escapeHtml(p.display_name || "")}</span>`
  ).join("、");
  return `
    <div class="today-unlinked-visits-banner">
      预约表没体现、但当天有病历/账单的到店（${unlinkedVisits.length}人）：${names}
    </div>
  `;
}

function todayDateLabel() {
  // 跟随选定的工作日期，友好展示：2026年6月24日 周三（当天加"· 今天"）
  const d = dateFromWorkValue(workDate.value);
  const wk = ["周日", "周一", "周二", "周三", "周四", "周五", "周六"][d.getDay()];
  const tag = workDate.value === localDateValue() ? " · 今天" : "";
  return `${d.getFullYear()}年${d.getMonth() + 1}月${d.getDate()}日 ${wk}${tag}`;
}

// 点击日期：优先原生 showPicker() 弹日历，不支持则回退 focus+click
function openWorkDatePicker() {
  const inp = document.getElementById("workDatePicker");
  if (!inp) return;
  if (typeof inp.showPicker === "function") {
    try { inp.showPicker(); return; } catch (e) { /* 回退 */ }
  }
  inp.focus();
  inp.click();
}

// 日历选中某天：切到该工作日期并刷新工作台
function pickWorkDate(value) {
  if (!value) return;
  workDate.value = value;
  refreshWorkDateViews();
}

function todaySaasEntryList(summary = {}) {
  // #321/#362：data-today-entry 是入口身份(决定右侧出哪张简表),data-today-view 才是
  // 「管理页查看全部」要跳的模块。两者分开——避免同 view 的两个入口(今日就诊/明日预约)
  // 一起高亮(旧 bug:active 按 view 匹配)。
  const entries = [
    {key: "today-appts", label: "今日就诊", view: "appointments", count: summary.appointments_today, offset: 0},
    {key: "tomorrow-appts", label: "明日预约", view: "appointments", count: summary.tomorrow_appointments || 0, offset: 1},
    {key: "settled", label: "今日结算", view: "billing", count: summary.today_settlements || 0, offset: 0, billing: "settled_today", display: todayCountOrMask(summary.today_settlements)},
    {key: "unpaid", label: "今日待缴费", view: "billing", count: summary.unpaid_bills, offset: 0, billing: "unpaid", display: todayMoneyOrMask(summary.today_unpaid_amount)},
    {key: "return-due", label: "今日待回访", view: "return-visits", count: summary.pending_return_visits, offset: 0, ret: "pending_due"},
    {key: "birthday", label: "今日生日", view: "patients", count: summary.birthdays_today || 0, offset: 0, patient: "birthday_today"},
  ];
  return `
    <aside class="today-saas-entry-list" aria-label="今日事项入口">
      ${entries.map((e, index) => `
        <button type="button" class="today-saas-entry${index === 0 ? " active" : ""}" data-today-entry="${escapeAttr(e.key)}" data-today-view="${escapeAttr(e.view)}" data-today-date-offset="${escapeAttr(e.offset)}" data-today-patient-filter="${escapeAttr(e.patient || "")}" data-today-return-filter="${escapeAttr(e.ret || "")}" data-today-billing-filter="${escapeAttr(e.billing || "")}" onclick="openTodaySaasEntry(this)">
          <span>${escapeHtml(e.label)}</span>
          <strong>${escapeHtml(e.display != null ? e.display : formatCount(e.count))}</strong>
        </button>
      `).join("")}
      <button type="button" class="today-saas-entry today-saas-entry-followup" onclick="openTodayFollowUps(this)">
        <span>今日待跟进</span>
        <strong></strong>
      </button>
    </aside>
  `;
}

// #321/#362：点左侧入口 → 在右侧壳(#todaySaasMain)内出该入口的简表,不再跳管理页。
// 高亮只亮当前一个(与 openTodayFollowUps 同款:先清后加)。
function openTodaySaasEntry(button) {
  document.querySelectorAll(".today-saas-entry").forEach(e => e.classList.remove("active"));
  button.classList.add("active");
  const main = document.getElementById("todaySaasMain");
  if (!main) return;
  const entry = button.dataset.todayEntry || "today-appts";
  const data = latestTodayWorkData || {};
  // 今日就诊 = 候诊队列本身(带处置/收费操作),复用原渲染。
  if (entry === "today-appts") {
    main.innerHTML = renderTodayQueue(data.appointments || []);
    bindPatientDetailRows(main);
    bindQueueOps(main);
    return;
  }
  const label = (button.querySelector("span") || {}).textContent || "";
  let rows = [];
  let renderer = renderTodayApptLite;
  if (entry === "tomorrow-appts") { rows = data.tomorrow_appointments || []; renderer = renderTodayApptLite; }
  else if (entry === "settled") { rows = data.today_settlements || []; renderer = renderTodaySettleLite; }
  else if (entry === "unpaid") { rows = data.unpaid_bills || []; renderer = renderTodayBillLite; }
  else if (entry === "return-due") { rows = data.return_visits || []; renderer = renderTodayReturnVisit; }
  else if (entry === "birthday") { rows = data.birthdays || []; renderer = renderTodayBirthdayLite; }
  // #487:后端简表列表有 limit(生日50/结算30),入口角标却是 summary 总数(如生日68),
  // 右侧简表角标改用总数并注明「显示前 N 条」,不再无解释地对不上。
  const summary = data.summary || {};
  const totals = {
    "tomorrow-appts": summary.tomorrow_appointments,
    settled: summary.today_settlements,
    unpaid: summary.unpaid_bills,
    "return-due": summary.pending_return_visits,
    birthday: summary.birthdays_today,
  };
  // 结算简表=表格布局:表头+合计行与数据行同网格列(对齐SaaS今日结算)。
  // 总额=今日现金流入(与流水同过滤口径恒等于逐行合计);支付方式列脚给出各方式小计。
  const methodNote = (data.today_pay_methods || [])
    .map((m) => `${escapeHtml(m.pay_type)} ${todayMoneyOrMask(m.amount)}`).join("<br>");
  const isSettled = entry === "settled";
  const footer = isSettled && summary.today_cash_in != null
    ? `<div class="settle-row settle-foot">
        <span>总合计</span><span></span><span></span><span></span>
        <span class="settle-methods">${methodNote}</span>
        <span class="num">${todayMoneyOrMask(summary.today_cash_in)}</span>
      </div>` : "";
  main.innerHTML = renderTodaySimpleShell(button, label, rows, renderer, totals[entry], footer, isSettled ? SETTLE_HEAD : "");
  bindPatientDetailRows(main);
}

// 简表外壳:标题+计数(#487:有总数用总数)+「管理页查看全部 →」(保留跳转能力)+「← 返回候诊」。
function renderTodaySimpleShell(button, label, rows, renderer, total, footer = "", head = "") {
  const view = button.dataset.todayView || "";
  const fullBtn = view ? `
    <button type="button" class="plain-button today-entry-full" onclick="openTodaySaasEntryFull(this)"
      data-today-view="${escapeAttr(view)}"
      data-today-date-offset="${escapeAttr(button.dataset.todayDateOffset || 0)}"
      data-today-patient-filter="${escapeAttr(button.dataset.todayPatientFilter || "")}"
      data-today-return-filter="${escapeAttr(button.dataset.todayReturnFilter || "")}"
      data-today-billing-filter="${escapeAttr(button.dataset.todayBillingFilter || "")}">管理页查看全部 →</button>` : "";
  // #532:total===null 是后端按权限降级的信号(同左侧入口 todayCountOrMask 口径)，
  // 不能落到 rows.length/"暂无数据"——那会把"没权限看"误呈现成"今天真的是0条"。
  // 注意只认 null,不认 undefined——undefined 是#487"调用方压根没传总数"的既有用法,
  // 那种场景下就该照旧退回 rows.length,不是权限降级信号。
  const masked = total === null;
  const truncated = !masked && Number(total) > rows.length;
  const limitNote = truncated
    ? `<span class="today-limit-note">显示前 ${rows.length} 条 / 共 ${escapeHtml(formatCount(total))}</span>` : "";
  const badgeText = masked ? "******" : formatCount(truncated ? total : rows.length);
  const emptyText = masked ? "无权限查看" : "暂无数据";
  return `
    <section class="today-queue-panel today-simple-panel" aria-label="${escapeAttr(label)}简表">
      <div class="today-card-head">
        <span>${escapeHtml(label)}</span>
        <strong class="today-card-badge">${escapeHtml(badgeText)}</strong>
        ${limitNote}
        <span class="today-status-spacer"></span>
        ${fullBtn}
        <button type="button" class="plain-button" onclick="loadTodayWork()">← 返回候诊</button>
      </div>
      <div class="today-simple-list">
        ${head}
        ${rows.length ? rows.map(renderer).join("") : `<div class="today-queue-empty">${escapeHtml(emptyText)}</div>`}
        ${footer}
      </div>
    </section>
  `;
}

// 「管理页查看全部」:沿用原跳转语义,显式进对应管理模块。
function openTodaySaasEntryFull(button) {
  const view = button.dataset.todayView || "today";
  const dateOffset = Number(button.dataset.todayDateOffset || 0);
  const context = {};
  if (button.dataset.todayPatientFilter) context.patientFilter = button.dataset.todayPatientFilter;
  if (button.dataset.todayReturnFilter) context.returnFilter = button.dataset.todayReturnFilter;
  if (button.dataset.todayBillingFilter) context.billingFilter = button.dataset.todayBillingFilter;
  switchWorkspaceView(view, "", {dateOffset, context});
}

// 简表行渲染：复用 todayPatientRow(点行开患者详情)。
function renderTodayApptLite(row) {
  return todayPatientRow(row.patient_identity, row.display_name, [
    (row.start_time || "").slice(11, 16),
    row.doctor_name,
    row.item_name,
    row.room,
  ]);
}

// 今日结算=收款流水(对齐SaaS今日结算·营业收入):每笔收款一行,含收旧账/0元;未挂单流水无单号。
// 表格布局同SaaS:各列对齐,金额右对齐单列;支付方式来自 paiddetail(多方式合并为"多种")。
const SETTLE_HEAD = `
  <div class="settle-row settle-head">
    <span>患者姓名</span><span>单号</span><span>医生</span><span>收费时间</span><span>支付方式</span><span class="num">本次实收</span>
  </div>`;

function renderTodaySettleLite(row) {
  return `
    <button type="button" class="today-row settle-row" data-patient-id="${escapeAttr(row.patient_identity || "")}">
      <span>${escapeHtml(row.display_name || "(无名)")}</span>
      <span>${escapeHtml(row.bill_no || "—")}</span>
      <span>${escapeHtml(row.doctor || "")}</span>
      <span>${escapeHtml((row.pay_time || "").slice(11, 16))}</span>
      <span>${escapeHtml(row.pay_type || "")}</span>
      <span class="num">${todayMoneyOrMask(row.amount)}</span>
    </button>`;
}

function renderTodayBillLite(row) {
  const unpaid = row.unpaid_fee != null ? row.unpaid_fee : null;
  return todayPatientRow(row.patient_identity, row.display_name, [
    row.bill_no ? `单号${row.bill_no}` : "",
    row.total_fee != null ? `应收${todayMoneyOrMask(row.total_fee)}` : "",
    row.paid_fee != null ? `已收${todayMoneyOrMask(row.paid_fee)}` : "",
    unpaid != null && Number(unpaid) > 0.005 ? `欠${todayMoneyOrMask(unpaid)}` : "",
  ]);
}

function renderTodayBirthdayLite(row) {
  return todayPatientRow(row.patient_identity, row.display_name, [
    row.birthday ? `生日 ${String(row.birthday).slice(0, 10)}` : "",
    row.sex,
  ]);
}

function todaySaasKpiStrip(summary = {}) {
  return `
    <section class="today-kpi-strip" aria-label="SaaS 首页今日指标">
      ${todaySaasKpiItem("今日就诊人次(初/复/新诊)", todayVisitBreakdown(summary))}
      ${todaySaasKpiItem("今日已预约", summary.appointments_today)}
      ${todaySaasKpiItem("今日已回访", summary.completed_return_visits || summary.return_visits_done || 0)}
      ${todaySaasKpiItem("今日应收", todayMoneyOrMask(summary.today_receivable))}
      ${todaySaasKpiItem("今日实收", todayMoneyOrMask(summary.today_paid))}
      ${todaySaasKpiItem("今日现金流入", todayMoneyOrMask(summary.today_cash_in))}
      <button type="button" class="plain-button today-money-toggle" onclick="toggleTodayMoneyHidden()" title="${todayMoneyHidden() ? "金额已隐藏，点击显示" : "一键隐藏主页所有金额"}">${todayMoneyHidden() ? "🙈" : "👁"}</button>
    </section>
  `;
}

function todaySaasKpiItem(label, value) {
  return `
    <div class="today-kpi-item">
      <span>${escapeHtml(label)}</span>
      <strong>${escapeHtml(String(value ?? 0))}</strong>
    </div>
  `;
}

function todayVisitBreakdown(summary = {}) {
  const total = summary.visits_today ?? summary.appointments_today ?? 0;
  const first = summary.first_visits_today ?? 0;
  const revisit = summary.revisits_today ?? 0;
  const newVisit = summary.new_visits_today ?? 0;
  return `${formatCount(total)}(${formatCount(first)}/${formatCount(revisit)}/${formatCount(newVisit)})`;
}

// 主页金额一键隐藏(屏幕转给患者看时防泄露)。状态记本浏览器,刷新不丢
function todayMoneyHidden() {
  try { return localStorage.getItem("today_money_hidden") === "1"; } catch { return false; }
}

function toggleTodayMoneyHidden() {
  try { localStorage.setItem("today_money_hidden", todayMoneyHidden() ? "0" : "1"); } catch { /* 隐私模式无存储,仅本次生效 */ }
  if (latestTodayWorkData) renderTodayWork(latestTodayWorkData);
}

function todayMoneyOrMask(value) {
  if (todayMoneyHidden()) return "****";
  return value === null || value === undefined || value === "" ? "******" : formatMoney(value);
}

// #528:无 billing.view 时后端结算计数降级为 null——入口显掩码,别把"无权限"误显成"今天 0 笔"
function todayCountOrMask(value) {
  return value === null || value === undefined ? "******" : formatCount(value);
}

function renderTodayQueue(rows) {
  const filteredRows = filteredTodayQueueRows(rows);
  // #11：搜索框/筛选条放在 list 容器外，搜索时只重渲 #todayQueueListWrap，输入框不被销毁→不失焦
  return `
    <section class="today-queue-panel" aria-label="今日候诊队列">
      <div class="today-card-head">
        <span>今日候诊队列</span>
        <strong class="today-card-badge">${escapeHtml(formatCount(filteredRows.length))}</strong>
      </div>
      ${todayQueueFilterBar(rows)}
      ${todayQueueToolbar()}
      <div id="todayQueueListWrap">${queueListInnerHtml(filteredRows, rows.length)}</div>
    </section>
  `;
}

function queueListInnerHtml(filteredRows, totalCount) {
  return `
    <div class="today-queue-result">${escapeHtml(todayQueueResultText(filteredRows.length, totalCount))}</div>
    ${todayQueueActiveChips()}
    <div class="today-queue-row today-queue-head">
      <span>操作</span>
      <span>患者</span>
      <span>时间 / 诊室</span>
      <span>医生 / 项目</span>
      <span>状态</span>
    </div>
    ${filteredRows.length ? filteredRows.map(renderTodayQueueRow).join("") : '<div class="today-queue-empty">暂无今日预约</div>'}
  `;
}

// 仅重渲队列列表区(不动搜索框)：保住输入框焦点，解决逐字符失焦(#11)
function rerenderTodayQueueList() {
  if (!latestTodayWorkData) return;
  const wrap = document.getElementById("todayQueueListWrap");
  if (!wrap) { renderTodayWork(latestTodayWorkData); return; }
  const rows = latestTodayWorkData.appointments || [];
  const filtered = filteredTodayQueueRows(rows);
  wrap.innerHTML = queueListInnerHtml(filtered, rows.length);
  const badge = todayWorkPanel.querySelector(".today-card-badge");
  if (badge) badge.textContent = formatCount(filtered.length);
  bindPatientDetailRows(wrap);
  bindQueueOps(wrap);
}

function todayQueueResultText(visibleCount, totalCount) {
  return `当前显示 ${formatCount(visibleCount)} / 全部 ${formatCount(totalCount)}`;
}

function todayQueueActiveChips() {
  const chips = [];
  if (todayQueueFilter !== "all") chips.push(`状态：${todayQueueFilterLabel(todayQueueFilter)}`);
  if (todayQueueSearch.trim()) chips.push(`搜索：${todayQueueSearch.trim()}`);
  if (!chips.length) return "";
  return `
    <div class="today-queue-chipbar" aria-label="当前队列过滤条件">
      ${chips.map(label => `<span class="today-queue-chip">${escapeHtml(label)}</span>`).join("")}
      <button type="button" class="plain-button" onclick="clearTodayQueueFilters()">清空条件</button>
    </div>
  `;
}

function todayQueueFilterLabel(filter) {
  return {
    "0": "待到诊",
    "1": "已到诊",
    "2": "已完成",
    "3": "已取消",
  }[filter] || "全部";
}

function todayQueueFilterBar(rows = []) {
  const counts = todayQueueFilterCounts(rows);
  const filters = [
    ["all", "全部"],
    ["0", "待到诊"],
    ["1", "已到诊"],
    ["2", "已完成"],
    ["3", "已取消"],
  ];
  return `
    <div class="today-queue-filter-bar" aria-label="候诊队列状态筛选">
      ${filters.map(([value, label]) => `
        <button type="button" class="plain-button today-queue-filter${todayQueueFilter === value ? " active" : ""}" onclick="setTodayQueueFilter('${value}')">
          ${label}
          <span class="today-queue-filter-count">${escapeHtml(formatCount(todayQueueFilterCount(counts, value)))}</span>
        </button>
      `).join("")}
    </div>
  `;
}

// #114：筛选条按就诊状态流阶段(tqStage)分桶，兼容新中文状态(已确认/已到诊/已分诊/已完成/已离开)
// 桶值沿用旧 0/1/2/3：0=待到诊(阶段0-1) 1=已到诊(阶段2-3) 2=已完成(阶段4-5) 3=已取消
function queueFilterBucket(row) {
  const stage = (typeof tqStage === "function") ? tqStage(row) : 0;
  if (stage < 0) return "3";
  if (stage <= 1) return "0";
  if (stage <= 3) return "1";
  return "2";
}

function todayQueueFilterCounts(rows) {
  const counts = {all: rows.length};
  rows.forEach(row => {
    const b = queueFilterBucket(row);
    counts[b] = (counts[b] || 0) + 1;
  });
  return counts;
}

function todayQueueFilterCount(counts, value) {
  return counts[value] || 0;
}

function filteredTodayQueueRows(rows) {
  return rows.filter(row => {
    const statusMatched = todayQueueFilter === "all" || queueFilterBucket(row) === todayQueueFilter;
    const doctorMatched = !todayQueueDoctor || String(row.doctor_name || "") === todayQueueDoctor;
    return statusMatched && doctorMatched && matchesTodayQueueSearch(row);
  });
}

function todayQueueDoctors() {
  const rows = (latestTodayWorkData && latestTodayWorkData.appointments) || [];
  return [...new Set(rows.map(r => String(r.doctor_name || "").trim()).filter(Boolean))].sort();
}

function todayQueueToolbar() {
  const docs = todayQueueDoctors();
  return `
    <div class="today-queue-toolbar">
      <select class="today-queue-doctor ord-input" onchange="setTodayQueueDoctor(this.value)" aria-label="分诊医生筛选">
        <option value="">全部医生</option>
        ${docs.map(d => `<option value="${escapeAttr(d)}"${d === todayQueueDoctor ? " selected" : ""}>${escapeHtml(d)}</option>`).join("")}
      </select>
      <input
        class="today-queue-search"
        value="${escapeAttr(todayQueueSearch)}"
        placeholder="搜索今日预约"
        oninput="setTodayQueueSearch(this.value)"
      >
      <button type="button" class="plain-button" onclick="clearTodayQueueSearch()">清除</button>
    </div>
  `;
}

function setTodayQueueDoctor(v) {
  todayQueueDoctor = v || "";
  if (latestTodayWorkData) renderTodayWork(latestTodayWorkData);
}
window.setTodayQueueDoctor = setTodayQueueDoctor;

function matchesTodayQueueSearch(row) {
  const keyword = todayQueueSearch.trim().toLowerCase();
  if (!keyword) return true;
  return [
    row.display_name,
    row.doctor_name,
    row.item_name,
    row.start_time,
    row.patient_identity,
  ].filter(Boolean).some(value => String(value).toLowerCase().includes(keyword));
}

function setTodayQueueSearch(value) {
  todayQueueSearch = value || "";
  todayFlowFilter = "";
  rerenderTodayQueueList();   // #11 只重渲列表区,搜索框保焦
}

function clearTodayQueueSearch() {
  todayQueueSearch = "";
  const input = todayWorkPanel.querySelector(".today-queue-search");
  if (input) input.value = "";
  rerenderTodayQueueList();
}

function clearTodayQueueFilters() {
  todayQueueFilter = "all";
  todayFlowFilter = "all";
  todayQueueSearch = "";
  todayQueueDoctor = "";
  if (latestTodayWorkData) renderTodayWork(latestTodayWorkData);
}

function setTodayQueueFlowFilter(filter, flow = filter) {
  todayQueueFilter = filter || "all";
  todayFlowFilter = flow || todayQueueFilter;
  todayQueueSearch = "";
  if (latestTodayWorkData) renderTodayWork(latestTodayWorkData);
}

function setTodayQueueFilter(filter) {
  todayQueueFilter = filter || "all";
  todayFlowFilter = "";
  if (latestTodayWorkData) renderTodayWork(latestTodayWorkData);
}

// 就诊状态流：预约确认→到达→分诊→完成治疗→患者离开，做完即深色，下一步高亮，
// 点已完成阶段=回退到该阶段(后端清后续时间戳)。处置/收费/病历等放进"⋯"次级菜单。
// #101 用 data-* + 事件委托，不把 ID 拼进 inline onclick。
// 小图标(线性SVG,跟随 currentColor,沿用 .tq-step 绿色风格)；文字进 title 悬浮提示
const TQ_STAGES = [
  {label: "确认", st: "已确认", stage: 1, icon: '<svg viewBox="0 0 24 24" class="tq-ic"><path d="M5 13l4 4L19 7"/></svg>'},
  {label: "到达", st: "已到诊", stage: 2, icon: '<svg viewBox="0 0 24 24" class="tq-ic"><path d="M12 22s7-6 7-12a7 7 0 1 0-14 0c0 6 7 12 7 12z"/><circle cx="12" cy="10" r="2.3"/></svg>'},
  {label: "分诊", st: "已分诊", stage: 3, icon: '<svg viewBox="0 0 24 24" class="tq-ic"><circle cx="12" cy="8" r="3.2"/><path d="M5.5 21c0-3.6 3-6 6.5-6s6.5 2.4 6.5 6"/></svg>'},
  {label: "完成", st: "已完成", stage: 4, icon: '<svg viewBox="0 0 24 24" class="tq-ic"><path d="M5 21V4h11l-2 4 2 4H5"/></svg>'},
  {label: "离开", st: "已离开", stage: 5, icon: '<svg viewBox="0 0 24 24" class="tq-ic"><path d="M14 21H5V3h9"/><path d="M10 12h10m0 0l-3-3m3 3l-3 3"/></svg>'},
];

function tqStage(row) {
  const s = String(row.status || "").trim();
  if (s === "3" || s === "已取消" || s === "已爽约" || s === "爽约") return -1;  // 爽约同取消排除漏斗(P1-2)
  if (s === "已离开" || s === "患者离开") return 5;
  if (["2", "完成", "已完成", "完成治疗"].includes(s)) return 4;
  if (s === "已分诊") return 3;
  if (["1", "已到诊", "已到达", "预约到达"].includes(s)) return 2;
  if (s === "已确认" || s === "预约确认") return 1;
  return 0;
}

// 次级操作也图标化全展开(不再藏进 ⋯)：处置/收费/病历/取消/再约
const TQ_MORE = [
  {key: "treat", label: "处置", icon: '<svg class="tq-ic" viewBox="0 0 24 24"><rect x="4" y="4" width="16" height="16" rx="2"/><path d="M12 9v6M9 12h6"/></svg>'},
  {key: "bill", label: "收费", icon: '<svg class="tq-ic" viewBox="0 0 24 24"><path d="M7 5l5 6 5-6M12 11v8M8 14h8M8 17.5h8"/></svg>'},
  {key: "medical", label: "病历", icon: '<svg class="tq-ic" viewBox="0 0 24 24"><rect x="5" y="3" width="14" height="18" rx="2"/><path d="M9 8h6M9 12h6M9 16h4"/></svg>'},
  {key: "visit", label: "回访", icon: '<svg class="tq-ic" viewBox="0 0 24 24"><path d="M5 4h3.5l1.7 4.3-2.1 1.3a11 11 0 0 0 5 5l1.3-2.1L18.7 15V18.5a2 2 0 0 1-2 2A14 14 0 0 1 3 6a2 2 0 0 1 2-2"/></svg>'},
  {key: "cancel", label: "取消", danger: true, icon: '<svg class="tq-ic" viewBox="0 0 24 24"><circle cx="12" cy="12" r="9"/><path d="M8.5 8.5l7 7M15.5 8.5l-7 7"/></svg>'},
  {key: "rebook", label: "预约", icon: '<svg class="tq-ic" viewBox="0 0 24 24"><rect x="4" y="5" width="16" height="16" rx="2"/><path d="M4 9h16M8 3v4M16 3v4M12 13v4M10 15h4"/></svg>'},
  {key: "delete", label: "删除", danger: true, icon: '<svg class="tq-ic" viewBox="0 0 24 24"><path d="M5 7h14M10 7V5h4v2M6 7l1 13h10l1-13"/></svg>'},
];

function _tqBtn(cls, tqop, id, pid, label, icon, extra, titleText) {
  // titleText 仅改 hover/无障碍说明,可见文字始终用短 label(禁用态不让长说明撑爆整行)
  const tip = titleText || label;
  return `<button type="button" class="tq-step${cls ? " " + cls : ""}" data-tqop="${tqop}"${extra || ""} data-aid="${id}" data-pid="${pid}" title="${escapeAttr(tip)}" aria-label="${escapeAttr(tip)}">${icon}<span class="tq-lbl">${escapeHtml(label)}</span></button>`;
}

function tqOps(row) {
  const pid = escapeAttr(row.patient_identity || "");
  const id = escapeAttr(row.appointment_id || "");
  const stage = tqStage(row);
  // 各操作按钮高亮口径=「今天这次就诊真做了这件事」(用户校准):
  //  处置=今天有处置单 / 收费=今天收了款(paid_today,历史欠费不算) / 病历=今天写了病历 /
  //  回访=今天本地新增回访(return_visit_today=created_at今天) / 预约=今天本地新增预约(rebooked_today=created_at今天)。
  const done = {treat: row.treated_today, bill: !!row.paid_today, medical: row.record_today,
    visit: row.return_visit_today, rebook: row.rebooked_today};
  // #330:已收费/已处置不可删,已就诊/收费/完成/离开不可取消——置灰且【真 disabled】(原来只样式没禁)
  const lockDel = !!(row.treated_today || row.paid_today);
  const lockCancel = !!row.paid_today;   // #330修正:仅【已收费】不可取消(完成/处置仍可取消,用户口径)
  const more = keys => TQ_MORE.filter(a => keys.includes(a.key))
    .map(a => {
      let cls = "tq-extra" + (a.danger ? " danger" : "") + (done[a.key] ? " tq-done" : "");
      // 锁定态只置灰+禁用,可见文字仍是短标签(取消/删除);原因移到 hover 提示,不撑爆整行
      let label = a.label, extra = "", tip = "";
      if (a.key === "delete" && lockDel) { cls += " tq-locked"; tip = "已收费/已处置，不可删除"; extra = " disabled"; }
      if (a.key === "cancel" && lockCancel) { cls += " tq-locked"; tip = "已就诊/收费/完成，不可取消"; extra = " disabled"; }
      return _tqBtn(cls, a.key, id, pid, label, a.icon, extra, tip);
    }).join("");
  // 次级操作每行固定一致(不随状态增减)，保证各行按钮对齐
  const moreKeys = ["treat", "bill", "medical", "visit", "cancel", "rebook", "delete"];
  let steps;
  if (stage === -1) {
    const st = String(row.status || "").trim();
    const label = (st === "已爽约" || st === "爽约") ? "已爽约" : "已取消";   // P1-2 区分爽约/取消
    steps = `<span class="tq-cancelled">${label}</span>`;
  } else {
    const closed = stage === 5;   // #330:已离开=当天闭环,禁止再点回前面步骤(防误回退)
    steps = TQ_STAGES.map(s => {
      const cls = stage >= s.stage ? "done" : (stage === s.stage - 1 ? "next" : "");
      return _tqBtn(cls, "stage", id, pid, s.label, s.icon, ` data-st="${escapeAttr(s.st)}"${closed ? " disabled" : ""}`);
    }).join("");
  }
  // 单排小按钮:状态+操作一字排开(按钮做小,人多也好选)
  return `<span class="tq-ops">${steps}${more(moreKeys)}</span>`;
}

// 队列操作组点击委托：读 dataset(纯字符串,无注入)
function bindQueueOps(container) {
  if (!container) return;
  container.querySelectorAll("[data-tqop]").forEach(btn => btn.addEventListener("click", ev => {
    ev.stopPropagation();
    const d = btn.dataset;
    if (d.tqop === "stage") {
      const row = ((latestTodayWorkData && latestTodayWorkData.appointments) || []).find(a => a.appointment_id === d.aid) || {};
      const cur = tqStage(row);
      if (cur >= 5) return;                       // 已离开:锁定,不可撤销(离开不可逆)
      if (d.st === "已完成" && cur >= 4) advanceQueue(d.aid, "已分诊");  // 已完成再点完成=撤销→回退已分诊
      else if (d.st === "已分诊") openTriage(d.aid);   // 分诊走弹框(顺带分医生/诊室)
      else advanceQueue(d.aid, d.st);
    } else if (d.tqop === "treat") {
      openPatientWorkspace(d.pid, "treatments");
    } else if (d.tqop === "bill") {
      window._queueWantsPay = d.pid;   // 存患者id:进收费tab后须同患者才弹(防A点击落到B,#267)
      openPatientWorkspace(d.pid, "billing");
    } else if (d.tqop === "medical") {
      openPatientWorkspace(d.pid, "medical");
    } else if (d.tqop === "visit") {
      openPatientWorkspace(d.pid, "return-visits");
    } else if (d.tqop === "cancel") {
      cancelAppointment(d.aid);
    } else if (d.tqop === "delete") {
      if (btn.classList.contains("tq-locked")) { window.alert("该患者当天已有处置或收费，不能删除预约（请先撤销相关记录）"); return; }
      deleteAppointment(d.aid);
    } else if (d.tqop === "rebook") {
      if (typeof openNewAppointment === "function") {
        const row = ((latestTodayWorkData && latestTodayWorkData.appointments) || []).find(a => a.appointment_id === d.aid) || {};
        // #332:给已就诊患者约下次,带入当前患者+就诊医生,免重新搜
        openNewAppointment({
          patient: {patient_identity: d.pid, display_name: row.display_name || ""},
          doctor: row.doctor_name || "",
        });
      }
    }
  }));
}

// 删除预约：二次确认后硬删(后端留审计old快照)，刷新队列
async function deleteAppointment(appointmentId) {
  if (!appointmentId) return;
  if (!window.confirm("确定删除该预约吗？删除后将从队列移除（已留审计可追溯）。")) return;
  let res;
  try { res = await fetch(`/api/appointments/${encodeURIComponent(appointmentId)}`, {method: "DELETE"}); }
  catch { window.alert("删除失败（网络异常）"); return; }
  if (!res.ok) { const m = await res.json().catch(() => ({})); window.alert("删除失败：" + (m.detail || res.status)); return; }
  loadTodayWork();
}
window.deleteAppointment = deleteAppointment;

// 次级操作菜单：处置记录/收费信息/病历信息/患者流失/取消预约/再次预约
let _qMoreEl = null;
function closeQueueMore() { if (_qMoreEl) { _qMoreEl.remove(); _qMoreEl = null; } }
function openQueueMore(anchor, aid, pid) {
  if (_qMoreEl) { closeQueueMore(); return; }
  const row = ((latestTodayWorkData && latestTodayWorkData.appointments) || []).find(a => a.appointment_id === aid) || {};
  const items = [
    {label: "处置记录", act: () => openPatientWorkspace(pid, "treatments")},
    {label: "收费信息", act: () => openPatientWorkspace(pid, "billing")},
    {label: "病历信息", act: () => openPatientWorkspace(pid, "medical")},
    ...((typeof tqStage === "function" && tqStage(row) >= 0 && tqStage(row) <= 1)
      ? [{label: "标记爽约", act: () => markNoShow(aid)}] : []),   // #134 仅未到诊阶段可标爽约
    {label: "取消预约", danger: true, act: () => cancelAppointment(aid)},
    {label: "再次预约", act: () => { if (typeof openNewAppointment === "function") { openNewAppointment({}); if (typeof pickApptPatient === "function") { const b = document.createElement("button"); b.dataset.pid = pid; b.dataset.pname = row.display_name || ""; pickApptPatient(b); } } }},
  ];
  const menu = document.createElement("div");
  menu.className = "tq-menu";
  menu.innerHTML = items.map((it, i) => `<button type="button" class="tq-menu-item${it.danger ? " danger" : ""}" data-i="${i}">${escapeHtml(it.label)}</button>`).join("");
  document.body.appendChild(menu);
  const r = anchor.getBoundingClientRect();
  menu.style.left = Math.min(r.left, window.innerWidth - 160) + "px";
  menu.style.top = (r.bottom + 4) + "px";
  menu.querySelectorAll("[data-i]").forEach(el => el.addEventListener("click", ev => {
    ev.stopPropagation();
    const it = items[Number(el.dataset.i)];
    closeQueueMore();
    if (it && it.act) it.act();
  }));
  _qMoreEl = menu;
  setTimeout(() => document.addEventListener("click", closeQueueMore, {once: true}), 0);
}

// 胶囊单选组：hidden input 承接原 select 的 id/value，saveTriage 等读值代码零改动。
// options: [[value, label], ...]；当前值对应的胶囊标 active。
function _chipField(id, current, options) {
  const cur = current || "";
  const chips = options.map(([v, label]) => {
    const on = v === cur;
    return `<button type="button" class="chip-option${on ? " active" : ""}" data-value="${escapeAttr(v)}" aria-pressed="${on}">${escapeHtml(label)}</button>`;
  }).join("");
  return `<input type="hidden" id="${id}" value="${escapeAttr(cur)}"><div class="chip-select" data-target="${id}" role="group">${chips}</div>`;
}
// 点击胶囊 → 同组去active、自己加active、写对应 hidden input 的值。弹窗渲染后对容器调一次。
function bindChipSelects(container) {
  (container || document).querySelectorAll(".chip-select").forEach(group => {
    group.querySelectorAll(".chip-option").forEach(btn => {
      btn.addEventListener("click", () => {
        group.querySelectorAll(".chip-option").forEach(b => { b.classList.remove("active"); b.setAttribute("aria-pressed", "false"); });
        btn.classList.add("active"); btn.setAttribute("aria-pressed", "true");
        const hidden = document.getElementById(group.dataset.target);
        if (hidden) hidden.value = btn.dataset.value;
      });
    });
  });
}

// 分诊弹框：给患者分 医生 / 就诊类型(初/复/新诊) / 诊室。一个动作搞定(转诊=再开换医生)。
// #813④:预约上下文跟弹窗 DOM(dataset)走,不放全局变量——快速连点 A、B 时旧全局值会被覆盖,
// 造成"显示 A 保存到 B"。令牌照 module_views.js moduleViewToken 范式丢弃过期渲染。
let _triageToken = 0;
async function openTriage(appointmentId, prefillRow) {
  const token = ++_triageToken;
  const appts = (latestTodayWorkData && latestTodayWorkData.appointments) || [];
  // prefillRow：从患者档案分诊时今日工作台数据没这个患者，外部传入当前预约值(医生/诊室/类型/status)
  const row = prefillRow || appts.find(a => a.appointment_id === appointmentId) || {};
  if (!(queueRooms && queueRooms.length)) {  // 直接从档案分诊、今日工作台没载过时，补诊室列表
    try { queueRooms = (await (await fetch("/api/settings/rooms")).json()).list || []; } catch { queueRooms = []; }
  }
  // 医生列表：人员库医生 ∪ 排班里实际出现的医生(人员库空时也能从排班拿到真实医生)
  let names = [];
  try { names = ((await (await fetch("/api/staff-members?role=" + encodeURIComponent("医生"))).json()).members || []).map(d => d.name); }
  catch { names = []; }
  if (token !== _triageToken) return;   // 期间又开了别的分诊弹窗:本次过期,不渲染不覆盖
  appts.map(a => a.doctor_name).filter(Boolean).forEach(n => { if (!names.includes(n)) names.push(n); });
  if (row.doctor_name && !names.includes(row.doctor_name)) names.push(row.doctor_name);
  let m = document.getElementById("triageModal");
  if (!m) { m = document.createElement("div"); m.id = "triageModal"; m.className = "modal-backdrop"; document.body.appendChild(m); }
  m.dataset.apptId = appointmentId;
  // #175:推进判定用打开时捕获的预约状态(含 status),别重查 latestTodayWorkData
  m.dataset.stage = String(tqStage(row));
  const docChips = _chipField("triageDoctor", row.doctor_name, [["", "未指定"]].concat(names.map(n => [n, n])));
  const typeChips = _chipField("triageType", row.visit_type, [["", "未分"], ["初诊", "初诊"], ["复诊", "复诊"], ["新诊", "新诊"]]);
  const roomChips = _chipField("triageRoom", row.room, [["", "不指定"]].concat((queueRooms || []).map(r => [r, r])));
  m.hidden = false;
  m.innerHTML = `
    <section class="appt-modal" role="dialog" aria-modal="true" aria-label="分诊">
      <div class="modal-head"><strong>分诊 · ${escapeHtml(row.display_name || "")}</strong>
        <button type="button" class="plain-button" onclick="closeTriage()">×</button></div>
      <div class="appt-body">
        <div class="appt-field">分诊医生（转诊=改这里）${docChips}</div>
        <div class="appt-field">就诊类型 ${typeChips}</div>
        <div class="appt-field">诊室 ${roomChips}</div>
      </div>
      <div class="modal-actions">
        <button type="button" class="tooth-confirm-btn" onclick="saveTriage()">保存分诊</button>
        <button type="button" class="plain-button" onclick="closeTriage()">取消</button>
      </div>
    </section>`;
  bindChipSelects(m);
}
function closeTriage() { const m = document.getElementById("triageModal"); if (m) m.hidden = true; }
async function saveTriage() {
  // #813④:预约 ID/推进阶段从弹窗 dataset 读——用户看到谁就保存到谁
  const m = document.getElementById("triageModal");
  if (!m || m.hidden || !m.dataset.apptId) return;
  const v = id => (document.getElementById(id) || {}).value || "";
  const payload = {doctor_name: v("triageDoctor"), visit_type: v("triageType"), room: v("triageRoom")};
  // 分诊保存顺带推进状态到"已分诊"(仅当还没到分诊阶段,不回退已完成的单)。
  if (Number(m.dataset.stage) < 3) payload.status = "已分诊";
  let res;
  try {
    res = await fetch(`/api/appointments/${encodeURIComponent(m.dataset.apptId)}`,
      {method: "PUT", headers: {"Content-Type": "application/json"}, body: JSON.stringify(payload)});
  } catch { window.alert("保存分诊失败（网络异常）"); return; }
  if (!res.ok) { const err = await res.json().catch(() => ({})); window.alert("保存分诊失败：" + (err.detail || res.status)); return; }
  closeTriage();
  loadTodayWork();
}
window.openTriage = openTriage; window.closeTriage = closeTriage; window.saveTriage = saveTriage;

// 今日工作台「+新增患者」：完整建档表单(对齐病历页基本资料字段) → POST /api/patients → 可一键挂号
// 字段 id → 后端字段名。性别走单选、生日走日期(自动算岁数)、备注走多行，其余在此声明即可。
const NEW_PATIENT_FIELD_MAP = {
  npName: "display_name", npPhone: "phone", npBirthday: "birthday", npIdCard: "id_card",
  npAddress: "address", npWechat: "wechat", npEmail: "email", npOccupation: "occupation",
  npWorkUnit: "work_unit", npAllergy: "allergy_history", npMedication: "medication_history",
  npPatientType: "patient_type", npDoctor: "responsible_doctor", npConsultant: "consultant",
  npSource: "referral_source", npSource2: "referral_source2", npSource3: "referral_source3",
  npSource4: "referral_source4", npIntroducerType: "introducer_type", npIntroducerName: "introducer_name",
  npPhoneVestee: "phone_vestee", npRemark: "remark",
};
let _newPatientAvatarFile = null;
let _npSexTouched = false;      // 前台是否真的点过性别（默认勾"男"，光看 :checked 分不出）
let _npManualFields = new Set();  // 人工碰过(手输/清空/点原始行)的字段 id：重拍绝不覆盖，哪怕现在是空的
let _newPatientSubmitting = false;
let _newPatientRequestId = "";
let _newPatientSaved = null;

// LAN HTTP（iPad/手机）不是安全上下文，没有 crypto.randomUUID。
// 统一用 getRandomValues 生成 RFC 4122 v4 UUID，本机与 LAN 只走这一条路径。
function _newPatientRequestUuid() {
  const bytes = new Uint8Array(16);
  crypto.getRandomValues(bytes);
  bytes[6] = (bytes[6] & 0x0f) | 0x40;
  bytes[8] = (bytes[8] & 0x3f) | 0x80;
  const hex = Array.from(bytes, value => value.toString(16).padStart(2, "0")).join("");
  return `${hex.slice(0, 8)}-${hex.slice(8, 12)}-${hex.slice(12, 16)}-${hex.slice(16, 20)}-${hex.slice(20)}`;
}

// 建档幂等号存 localStorage：建档响应丢在路上时用户会关页面重开重填，复用同一个号才能让
// 后端重放命中(返回首次那位患者)，否则同一个人被建两次。只存一次性 UUID，不含任何患者信息。
const NP_REQUEST_KEY = "np_pending_request_id";

// 本浏览器是否禁用了本地存储(如 iPad Safari 隐私模式)。禁用时防重复建档的保护失效，
// 必须显式告知操作员——不静默降级顶上去(禁止兜底)。
let _npStorageBlocked = false;

function _takeNewPatientRequestId() {
  try {
    let id = localStorage.getItem(NP_REQUEST_KEY) || "";
    if (!id) {
      id = _newPatientRequestUuid();
      localStorage.setItem(NP_REQUEST_KEY, id);
    }
    _npStorageBlocked = false;
    return id;
  } catch {
    _npStorageBlocked = true;
    return _newPatientRequestUuid();
  }
}

function _rotateNewPatientRequestId() {
  const id = _newPatientRequestUuid();
  try { localStorage.setItem(NP_REQUEST_KEY, id); } catch { /* 同上 */ }
  return id;
}

function _clearNewPatientRequestId() {
  try { localStorage.removeItem(NP_REQUEST_KEY); } catch { /* 同上 */ }
}

// 后端三种 409 只有这一种代表「同号换了个人」，其余都意味着患者行已存在，不能换号重发。
async function _isPayloadConflict(res) {
  const body = await res.clone().json().catch(() => ({}));
  return (body && body.detail && body.detail.code) === "request_id_payload_conflict";
}

function _errorText(body) {
  const detail = body && body.detail;
  if (!detail) return "";
  return typeof detail === "string" ? detail : (detail.message || detail.code || "");
}

function _syncNewPatientActionState() {
  const disabled = _newPatientSubmitting || _ocrBusyGen !== 0;
  ["npSavePatientBtn", "npRegisterPatientBtn", "npRegisterTriagePatientBtn",
    "npClosePatientBtn", "npCancelPatientBtn"].forEach(id => {
    const button = document.getElementById(id);
    if (button) button.disabled = disabled;
  });
  const ocrInput = document.getElementById("npOcrInput");
  if (ocrInput) ocrInput.disabled = _newPatientSubmitting || _newPatientSaved !== null;
}

function _lockNewPatientFields() {
  document.querySelectorAll(
    "#newPatientModal .np-body input, #newPatientModal .np-body select, "
      + "#newPatientModal .np-body textarea, #newPatientModal .np-body button",
  ).forEach(field => { field.disabled = true; });
}

function _npInput(id, label, opts = {}) {
  const dict = opts.dict ? ` data-dict="${opts.dict}"` : "";
  const ph = opts.placeholder ? ` placeholder="${escapeAttr(opts.placeholder)}"` : "";
  const cls = opts.wide ? "appt-field np-wide" : "appt-field";
  const tag = opts.type === "date"
    ? `<input id="${id}" type="date" class="ord-input"${opts.extra || ""}>`
    : (opts.type === "textarea"
      ? `<textarea id="${id}" class="ord-input np-textarea" rows="2"${ph}></textarea>`
      : `<input id="${id}" class="ord-input"${opts.type === "tel" ? ' inputmode="tel"' : ""} autocomplete="off"${dict}${ph}>`);
  return `<label class="${cls}">${escapeHtml(label)} ${tag}</label>`;
}

function openNewPatient() {
  let m = document.getElementById("newPatientModal");
  if (!m) { m = document.createElement("div"); m.id = "newPatientModal"; m.className = "modal-backdrop"; document.body.appendChild(m); }
  _newPatientAvatarFile = null;
  _npSexTouched = false;
  _npManualFields = new Set();
  _newPatientSubmitting = false;
  _newPatientRequestId = _takeNewPatientRequestId();
  _newPatientSaved = null;
  cancelNewPatientOcr();          // 作废并终止所有在途识别
  m.hidden = false;
  m.innerHTML = `
    <section class="appt-modal np-modal" role="dialog" aria-modal="true" aria-label="新增患者">
      <div class="modal-head"><strong>新增患者</strong><button type="button" class="plain-button" id="npClosePatientBtn" data-np-close onclick="closeNewPatient()">×</button></div>
      <div class="appt-body np-body">
        <div class="np-avatar-row">
          <div class="np-avatar-preview" id="npAvatarPreview" aria-hidden="true">头像</div>
          <label class="np-avatar-btn">上传头像
            <input type="file" id="npAvatarInput" accept="image/*" onchange="onNewPatientAvatarPick(this)" hidden>
          </label>
        </div>
        <div class="np-ocr-row" id="npOcrRow" hidden>
          <label class="np-ocr-btn">📷 拍照识别
            <input type="file" id="npOcrInput" accept="image/*" capture="environment"
                   onchange="onNewPatientOcrPick(this)" hidden>
          </label>
          <span class="np-ocr-hint">拍身份证 / 登记表 / 微信截图，自动填表。照片不保存、不上传外网。</span>
        </div>
        <div class="np-ocr-panel" id="npOcrPanel" hidden></div>
        <div class="np-section-title">基本信息</div>
        <div class="np-grid">
          ${_npInput("npName", "姓名*", {placeholder: "必填"})}
          <label class="appt-field">电话*
            <span class="np-phone-wrap">
              <input id="npPhone" class="ord-input" inputmode="tel" autocomplete="off" placeholder="必填">
              <select id="npPhoneVestee" class="ord-input np-vestee" title="机主关系">
                ${["本人", "爸爸", "妈妈", "儿子", "女儿", "老公", "老婆", "爷爷", "奶奶", "其他"].map(v => `<option value="${v}">${v}</option>`).join("")}
              </select>
            </span>
          </label>
          <div class="appt-field">性别
            <span class="appt-radios">
              <label><input type="radio" name="npSex" value="男" onclick="_npSexTouched = true" checked>男</label>
              <label><input type="radio" name="npSex" value="女" onclick="_npSexTouched = true">女</label>
            </span>
          </div>
          <label class="appt-field">生日
            <span class="np-birthday-wrap">
              <input id="npBirthday" type="date" class="ord-input" oninput="updateNewPatientAge()">
              <span id="npAgeBadge" class="np-age-badge"></span>
            </span>
          </label>
          <label class="appt-field">年龄
            <span class="np-birthday-wrap">
              <input id="npAge" type="number" min="0" max="149" inputmode="numeric" class="ord-input" placeholder="不知生日填这里" oninput="updateNewPatientBirthdayFromAge()">
              <span id="npAgeEstBadge" class="np-age-badge"></span>
            </span>
          </label>
          ${_npInput("npIdCard", "身份证号")}
          ${_npInput("npAddress", "地址", {wide: true})}
        </div>
        <div class="np-section-title">联系与社会</div>
        <div class="np-grid">
          ${_npInput("npWechat", "微信")}
          ${_npInput("npEmail", "邮箱")}
          ${_npInput("npOccupation", "职业", {dict: "Occupation"})}
          ${_npInput("npWorkUnit", "工作单位")}
        </div>
        <div class="np-section-title">医疗信息</div>
        <div class="np-grid">
          ${_npInput("npAllergy", "过敏史", {dict: "AllergyHistory", wide: true, placeholder: "无 / 青霉素 等"})}
          ${_npInput("npMedication", "用药史", {wide: true})}
          ${_npInput("npPatientType", "就诊项目/主诉", {dict: "PatientKind", placeholder: "如 补牙/拔牙/正畸/洗牙"})}
        </div>
        <div class="np-section-title">接诊与来源
          <button type="button" class="np-section-gear" title="患者来源设置" onclick="openReferralSettings()">⚙</button>
        </div>
        <div class="np-grid">
          ${_npInput("npDoctor", "责任医生")}
          ${_npInput("npConsultant", "咨询师")}
          ${referralSlotHtml("npSource", "患者来源")}
          ${referralSlotHtml("npSource2", "患者来源2")}
          ${referralSlotHtml("npSource3", "患者来源3")}
          ${referralSlotHtml("npSource4", "患者来源4")}
          <label class="appt-field np-wide">介绍人
            <span class="np-introducer">
              <select id="npIntroducerType" class="ord-input np-intro-type">
                <option value="患者介绍">患者介绍</option>
                <option value="员工介绍">员工介绍</option>
              </select>
              <input id="npIntroducerName" class="ord-input" list="npIntroducerList" placeholder="输入搜索介绍人" oninput="searchIntroducer(this.value)" autocomplete="off">
              <datalist id="npIntroducerList"></datalist>
            </span>
          </label>
        </div>
        <div class="np-section-title">备注</div>
        <div class="np-grid">
          ${_npInput("npRemark", "备注", {type: "textarea", wide: true, placeholder: "本次主诉/需要提醒前台的事项等"})}
        </div>
      </div>
      <div class="modal-actions">
        <span id="npStatus" class="today-refresh-status"></span>
        <button type="button" class="plain-button" id="npSavePatientBtn" data-np-submit onclick="submitNewPatient('save')">仅保存</button>
        <button type="button" class="plain-button" id="npRegisterPatientBtn" data-np-submit onclick="submitNewPatient('register')">保存并挂号</button>
        <button type="button" class="tooth-confirm-btn" id="npRegisterTriagePatientBtn" data-np-submit onclick="submitNewPatient('register-triage')">保存、挂号并分诊</button>
        <button type="button" class="plain-button" id="npCancelPatientBtn" data-np-close onclick="closeNewPatient()">取消</button>
      </div>
    </section>`;
  // #219：来源/职业/过敏史/患者类型接已同步字典，挂 datalist 建议
  if (typeof bindDictInputs === "function") bindDictInputs(m);
  refreshOcrEntry();
  // 前台一旦在建档字段里手动动过(改字/清空)，这个字段就归人工所有：记进 _npManualFields，
  // 重拍绝不覆盖(哪怕人把它清成空的)，并去掉机器高亮标记。
  // 用 oninput 属性(覆盖式)不用 addEventListener，避免弹窗复用时监听器越挂越多。
  m.oninput = e => {
    const t = e.target;
    if (!t || !t.id || !(t.id in NEW_PATIENT_FIELD_MAP)) return;
    _npManualFields.add(t.id);
    if (t.classList) t.classList.remove("np-ocr-filled", "np-ocr-uncertain");
  };
  _syncNewPatientActionState();
  if (_npStorageBlocked) {
    const warn = document.getElementById("npStatus");
    if (warn) {
      warn.textContent = "注意：本浏览器禁用了本地存储，防重复建档保护不可用；"
        + "若保存后没有反应，请先去患者列表确认，不要直接重复提交";
    }
  }
  const nm = document.getElementById("npName"); if (nm) nm.focus();
}
function closeNewPatient() {
  cancelNewPatientOcr();
  const m = document.getElementById("newPatientModal");
  if (m) m.hidden = true;
  _newPatientAvatarFile = null;
}

// 介绍人搜索：患者介绍→/api/patients?q；员工介绍→/api/staff-members(本地过滤)。接口位置不同
let _introSearchTimer = null;
function searchIntroducer(v) {
  clearTimeout(_introSearchTimer);
  const kw = (v || "").trim();
  if (!kw) return;
  const type = (document.getElementById("npIntroducerType") || {}).value || "患者介绍";
  _introSearchTimer = setTimeout(async () => {
    let names = [];
    try {
      if (type === "员工介绍") {
        const members = (await (await fetch("/api/staff-members")).json()).members || [];
        names = members.map(s => s.name).filter(n => n && n.includes(kw)).slice(0, 10);
      } else {
        const list = (await (await fetch(`/api/patients?q=${encodeURIComponent(kw)}&pagesize=10`)).json()).list || [];
        names = list.map(p => p.display_name).filter(Boolean);
      }
    } catch { names = []; }
    const dl = document.getElementById("npIntroducerList");
    if (dl) dl.innerHTML = names.map(n => `<option value="${escapeAttr(n)}"></option>`).join("");
  }, 250);
}
window.searchIntroducer = searchIntroducer;

// 生日 → 实岁(按当前日期算,未到生日减一岁)。生日有值时清掉年龄直填,两者互斥后填生效。
function updateNewPatientAge() {
  const badge = document.getElementById("npAgeBadge");
  if (!badge) return;
  const v = (document.getElementById("npBirthday") || {}).value || "";
  const d = v ? new Date(v) : null;
  if (!d || isNaN(d.getTime())) { badge.textContent = ""; return; }
  const ageEl = document.getElementById("npAge");
  const estBadge = document.getElementById("npAgeEstBadge");
  if (ageEl && ageEl.value) { ageEl.value = ""; if (estBadge) estBadge.textContent = ""; }
  const now = bjToday();
  let age = now.getFullYear() - d.getFullYear();
  const m = now.getMonth() - d.getMonth();
  if (m < 0 || (m === 0 && now.getDate() < d.getDate())) age -= 1;
  badge.textContent = age >= 0 && age < 150 ? `${age}岁` : "";
}

// 年龄直填(不知生日时)：换算"推算出生年"，只存年份 → 各处岁数随时间自动增长
function updateNewPatientBirthdayFromAge() {
  const ageEl = document.getElementById("npAge");
  const badge = document.getElementById("npAgeEstBadge");
  if (!ageEl || !badge) return;
  const age = parseInt(ageEl.value, 10);
  if (!Number.isFinite(age) || age < 0 || age >= 150) { badge.textContent = ""; return; }
  const b = document.getElementById("npBirthday");
  if (b && b.value) { b.value = ""; updateNewPatientAge(); }
  badge.textContent = `按 ${bjToday().getFullYear() - age} 年生推算`;
}

// ---- 拍照建档：证件图 → 预填表单。识别在本机跑，照片不落盘、不出本机 ----
let _ocrAvailable = null;   // null=未探测
let _ocrLines = [];
let _ocrGen = 0;   // 每开一次弹窗/每拍一张递增；过期响应一律丢弃，避免把上一个人的证件填进新表单
let _ocrBusyGen = 0;
let _ocrAbortController = null;

function setNewPatientOcrBusy(gen, busy) {
  if (busy) _ocrBusyGen = gen;
  else if (_ocrBusyGen === gen) _ocrBusyGen = 0;
  else return;                         // 旧请求 finally 不得解锁新请求
  _syncNewPatientActionState();
}

function cancelNewPatientOcr() {
  _ocrGen++;                           // 先作废世代，再 abort；catch/finally 只能看见已作废状态
  const controller = _ocrAbortController;
  _ocrAbortController = null;
  _ocrBusyGen = 0;
  if (controller) controller.abort();
  _syncNewPatientActionState();
}

async function refreshOcrEntry() {
  const row = document.getElementById("npOcrRow");
  if (!row) return;
  if (_ocrAvailable === null) {
    try {
      const r = await fetch("/api/ocr/status");
      _ocrAvailable = r.ok ? !!(await r.json()).available : false;
    } catch { _ocrAvailable = false; }
  }
  row.hidden = !_ocrAvailable;   // 没有识别引擎的机器：入口根本不出现
}

async function onNewPatientOcrPick(input) {
  if (_newPatientSubmitting) {
    if (input) input.value = "";
    return;
  }
  const f = input && input.files && input.files[0];
  input.value = "";                       // 允许连拍同一张
  if (!f) return;
  const panel = document.getElementById("npOcrPanel");
  if (!panel) return;
  const gen = ++_ocrGen;              // 本次识别的世代号
  if (_ocrAbortController) _ocrAbortController.abort();
  const controller = new AbortController();
  _ocrAbortController = controller;
  setNewPatientOcrBusy(gen, true);
  panel.hidden = false;
  panel.innerHTML = `<div class="np-ocr-busy">识别中…</div>`;

  let res;
  try {
    const r = await fetch("/api/ocr/patient-card", {
      method: "POST",
      headers: {"Content-Type": f.type || "image/jpeg"},
      body: f,                  // 原始 File，不走 multipart —— 服务端才不会把证件照 spool 到磁盘
      signal: controller.signal,
    });
    if (gen !== _ocrGen) return;      // 期间换了患者/又拍了一张 → 这次结果作废，绝不落进当前表单
    if (!r.ok) {
      const detail = (await r.json().catch(() => ({}))).detail || "识别失败";
      panel.innerHTML = `<div class="np-ocr-warn">${escapeHtml(detail)}</div>`;
      if (r.status === 503) {
        // 503 可能是引擎真熔断，也可能只是这张图 Vision 没认出来（后端连续多次失败才熔断）。
        // 别一刀切藏入口——作废缓存后回头问一次 status（真相源）：真不可用才收起，
        // 只是烂图就留着入口，让前台照错误提示换张清晰的重拍。
        _ocrAvailable = null;
        refreshOcrEntry();
      }
      return;
    }
    res = await r.json();
  } catch (err) {
    if (gen !== _ocrGen) return;
    if (err && err.name === "AbortError") return;
    panel.innerHTML = `<div class="np-ocr-warn">识别失败，请重试</div>`;
    return;
  } finally {
    if (gen === _ocrGen) {
      if (_ocrAbortController === controller) _ocrAbortController = null;
      setNewPatientOcrBusy(gen, false);
    }
  }
  if (gen !== _ocrGen) return;        // await r.json() 期间也可能已作废
  const filled = applyOcrFields(res);
  renderOcrPanel(res, filled);
}

// 只填空字段——绝不覆盖前台已经手输的内容
function applyOcrFields(res) {
  const fields = (res && res.fields) || {};
  const confident = new Set((res && res.confident) || []);
  const backToId = {};
  Object.entries(NEW_PATIENT_FIELD_MAP).forEach(([id, name]) => { backToId[name] = id; });
  const filled = [];

  Object.entries(fields).forEach(([name, value]) => {
    if (name === "sex") {
      // 性别默认勾"男"，所以不能用":checked"判断"人有没有填过"——否则女患者会被建成男的。
      // 只认前台是否真的点过性别，没点过就按证件填（性别是从身份证号算出来的，不会错）。
      if (_npSexTouched) return;
      const radio = document.querySelector(`input[name="npSex"][value="${value}"]`);
      if (radio) { radio.checked = true; filled.push("性别"); }
      return;
    }
    const id = backToId[name];
    const el = id && document.getElementById(id);
    if (!el) return;
    // 人工碰过的字段绝不覆盖(哪怕现在空的——是人主动清空的)；没碰过的(空的或上次OCR填的)才填。
    // 用独立的 _npManualFields 记所有权，不靠"当前值/class"推断，免得清空动作没痕迹被重拍撤销。
    if (_npManualFields.has(id)) return;
    el.value = value;
    el.classList.add("np-ocr-filled");
    el.classList.toggle("np-ocr-uncertain", !confident.has(name));    // 标黄：请核对
    filled.push(id);
  });
  if (filled.includes("npBirthday")) updateNewPatientAge();
  return filled;
}

// 兜底：识别出的每一行原始文字都能点进字段。版式没见过、手写潦草，也不会白拍一张
function renderOcrPanel(res, filled) {
  const panel = document.getElementById("npOcrPanel");
  if (!panel) return;
  _ocrLines = res.lines || [];
  const warns = (res.warnings || [])
    .map(w => `<div class="np-ocr-warn">${escapeHtml(w)}</div>`).join("");
  const targets = [["npName", "姓名"], ["npPhone", "电话"], ["npIdCard", "身份证"], ["npAddress", "地址"]];
  const lines = _ocrLines.map((t, i) => `
    <li class="np-ocr-line">
      <span class="np-ocr-text" title="${escapeAttr(t)}">${escapeHtml(t)}</span>
      <span class="np-ocr-targets">
        ${targets.map(([id, label]) =>
          `<button type="button" class="np-ocr-chip"
             onclick="fillFieldFromLine('${id}', ${i})">${label}</button>`).join("")}
      </span>
    </li>`).join("");

  panel.innerHTML = `
    ${warns}
    <div class="np-ocr-summary">
      已自动填入 ${filled.length} 项（${res.elapsed_ms || 0} 毫秒）。<b>请核对高亮字段</b>，照片未保存。
    </div>
    <details class="np-ocr-raw" ${filled.length ? "" : "open"}>
      <summary>识别到的原文（${_ocrLines.length} 行）——点右侧按钮可填进对应字段</summary>
      <ul class="np-ocr-lines">${lines}</ul>
    </details>`;
}

function fillFieldFromLine(fieldId, lineIndex) {
  const el = document.getElementById(fieldId);
  const text = _ocrLines[lineIndex];
  if (!el || text == null) return;
  el.value = String(text).trim();
  // 人主动点原始行填的 = 人工确认，记进所有权、去掉机器标记，重拍不许再覆盖它
  _npManualFields.add(fieldId);
  el.classList.remove("np-ocr-filled", "np-ocr-uncertain");
  if (fieldId === "npBirthday") updateNewPatientAge();
}

window.onNewPatientOcrPick = onNewPatientOcrPick;
window.fillFieldFromLine = fillFieldFromLine;

// 头像：先选先存，建档拿到 patient_identity 后再上传(此处只做入口)
function onNewPatientAvatarPick(input) {
  const f = input && input.files && input.files[0];
  _newPatientAvatarFile = f || null;
  const prev = document.getElementById("npAvatarPreview");
  if (!prev) return;
  if (f) { prev.style.backgroundImage = `url(${URL.createObjectURL(f)})`; prev.classList.add("has-img"); prev.textContent = ""; }
  else { prev.style.backgroundImage = ""; prev.classList.remove("has-img"); prev.textContent = "头像"; }
}

async function submitNewPatient(mode = "save") {
  const val = id => (document.getElementById(id) || {}).value || "";
  const status = document.getElementById("npStatus");
  const action = String(mode || "save");
  if (!["save", "register", "register-triage"].includes(action)) {
    if (status) status.textContent = "未知的保存动作";
    return;
  }
  if (_newPatientSubmitting) return;
  if (_ocrBusyGen !== 0) {
    if (status) status.textContent = "识别中，请稍候再保存";
    return;
  }
  const name = _newPatientSaved ? _newPatientSaved.displayName : val("npName").trim();
  if (!_newPatientSaved) {
    const phone = val("npPhone").trim();
    if (!name) { if (status) status.textContent = "请填姓名"; return; }
    if (!phone) { if (status) status.textContent = "请填电话"; return; }
  }

  _newPatientSubmitting = true;
  _syncNewPatientActionState();
  try {
    if (!_newPatientSaved) {
      const sexEl = document.querySelector('input[name="npSex"]:checked');
      const payload = {request_id: _newPatientRequestId, sex: sexEl ? sexEl.value : ""};
      Object.entries(NEW_PATIENT_FIELD_MAP).forEach(([id, key]) => { payload[key] = val(id).trim(); });
      if (!payload.birthday) {
        // 年龄直填 → 只存推算出生年(如 "1990")：new Date("1990") 合法,岁数随年份自动长
        const directAge = parseInt(val("npAge"), 10);
        if (Number.isFinite(directAge) && directAge >= 0 && directAge < 150) {
          payload.birthday = String(bjToday().getFullYear() - directAge);
        }
      }
      if (status) status.textContent = "保存中...";
      const post = () => fetch("/api/patients", {
        method: "POST",
        headers: {"Content-Type": "application/json"},
        body: JSON.stringify(payload),
      });
      let res = await post();
      if (res.status === 409 && await _isPayloadConflict(res)) {
        // 只有「同号不同内容」这一种 409 才换号重来：这一单确实是另一个人。
        // 其余 409(首次快照损坏 / 病历号冲突)时患者行已经在库里了，换号重发会建出第二份档案，
        // 必须停下报错让人去核对——fail closed，不猜。
        _newPatientRequestId = _rotateNewPatientRequestId();
        payload.request_id = _newPatientRequestId;
        res = await post();
      }
      if (!res.ok) {
        const errorBody = await res.json().catch(() => ({}));
        if (status) status.textContent = "保存失败：" + (_errorText(errorBody) || res.status);
        return;
      }
      const body = await res.json();
      if (!body.patient_identity) {
        if (status) status.textContent = "保存失败：未返回患者标识";
        return;
      }
      _newPatientSaved = {patientIdentity: body.patient_identity, displayName: name};
      _clearNewPatientRequestId();          // 这一单已落库，号作废，下次建档取新号
      _lockNewPatientFields();
      // 头像入口：选了图就在建档后上传(失败不挡建档主流程)
      if (_newPatientAvatarFile) {
        try {
          const fd = new FormData(); fd.append("file", _newPatientAvatarFile);
          await fetch(`/api/patients/${encodeURIComponent(body.patient_identity)}/avatar`, {method: "POST", body: fd});
        } catch { /* 头像非必须 */ }
      }
    }

    if (action === "save") {
      closeNewPatient();
      loadTodayWork();
      return;
    }

    if (status) status.textContent = "患者已保存，正在挂号...";
    const result = await checkInPatient({
      patientIdentity: _newPatientSaved.patientIdentity,
      displayName: _newPatientSaved.displayName,
      openTriageAfter: action === "register-triage",
      switchToToday: true,
      showSuccess: true,
    });
    if (!result) {
      if (status) status.textContent = "患者已保存，后续操作未完成，请按提示重试";
      return;
    }
    closeNewPatient();
  } catch {
    if (status) {
      status.textContent = _newPatientSaved
        ? "患者已保存，后续操作未完成，请按提示重试"
        : "保存失败（网络异常）";
    }
  } finally {
    _newPatientSubmitting = false;
    _syncNewPatientActionState();
  }
}
window.openNewPatient = openNewPatient; window.closeNewPatient = closeNewPatient; window.submitNewPatient = submitNewPatient;
window.updateNewPatientAge = updateNewPatientAge; window.onNewPatientAvatarPick = onNewPatientAvatarPick;

// 登记方式标识：预约(提前约) vs 到店(walk-in 临时来登记)
function tqRegChip(row) {
  return String(row.register_type || "") === "到店"
    ? '<span class="tq-reg tq-reg-walkin">到店</span>'
    : '<span class="tq-reg">预约</span>';
}

// 时间列：预约时刻 + (已到诊则)到达时刻
function tqTimeCell(row) {
  // 扫荡#397:约/到/离三个时间都带标签做成独立小块,不再裸拼成「18:00 到07:59 离08:16」难辨。
  const appt = (row.start_time || "").slice(11, 16);
  const arr = (row.arrived_at || "").slice(11, 16);
  const fin = (row.finished_at || "").slice(11, 16);
  const parts = [];
  if (appt) parts.push(`<span class="tq-t tq-t-appt"><b>约</b>${escapeHtml(appt)}</span>`);
  if (arr) parts.push(`<span class="tq-t tq-t-arr"><b>到</b>${escapeHtml(arr)}</span>`);
  if (fin) parts.push(`<span class="tq-t tq-t-fin"><b>离</b>${escapeHtml(fin)}</span>`);
  return parts.join("");
}

async function advanceQueue(appointmentId, status) {
  if (!appointmentId) return;
  try {
    await fetch(`/api/appointments/${encodeURIComponent(appointmentId)}`,
      {method: "PUT", headers: {"Content-Type": "application/json"}, body: JSON.stringify({status})});
  } catch { return; }
  loadTodayWork();  // 重载: 反映新状态 + 到达/离店时刻 + 筛选 tab 计数
}

// P1-2 标记爽约：患者未到 → 状态置「已爽约」(排除出候诊漏斗)，复用 advanceQueue 的 PUT+重载
function markNoShow(appointmentId) {
  if (!appointmentId) return;
  if (typeof closeQueueMore === "function") closeQueueMore();
  if (!window.confirm("确认将该预约标记为爽约（患者未到）？")) return;
  advanceQueue(appointmentId, "已爽约");
}

// 生日 → 实岁(未到生日减一岁)；无效返回 null
function ageFromBirthday(b) {
  const d = b ? new Date(b) : null;
  if (!d || isNaN(d.getTime())) return null;
  const now = bjToday();
  let a = now.getFullYear() - d.getFullYear();
  const m = now.getMonth() - d.getMonth();
  if (m < 0 || (m === 0 && now.getDate() < d.getDate())) a -= 1;
  return (a >= 0 && a < 150) ? a : null;
}

// 性别小头像(男蓝/女粉,默认灰)：一个人形图标，颜色区分男女
function tqAvatar(row) {
  const sex = String(row.sex || "").trim();
  const cls = sex === "男" ? "male" : (sex === "女" ? "female" : "");
  return `<span class="tq-avatar ${cls}" title="${escapeAttr(sex || "未知")}"><svg viewBox="0 0 24 24"><circle cx="12" cy="8" r="3.6"/><path d="M5 20c0-3.9 3.1-6.6 7-6.6s7 2.7 7 6.6"/></svg></span>`;
}

// 性别+年龄小标(跟在姓名后)
function tqGenderAge(row) {
  const sex = String(row.sex || "").trim();
  const age = ageFromBirthday(row.birthday);
  const txt = [sex, age != null ? age + "岁" : ""].filter(Boolean).join(" · ");
  return txt ? `<span class="tq-ga">${escapeHtml(txt)}</span>` : "";
}

function renderTodayQueueRow(row) {
  const patientId = row.patient_identity || "";
  const rowClass = patientId === activePatientId ? "today-queue-row selected" : "today-queue-row";
  const owe = Number(row.unpaid_amount || 0) > 0;
  return `
    <div class="${rowClass}" role="button" tabindex="0" data-patient-id="${escapeAttr(patientId)}" title="点击进入患者主页">
      <span>${tqOps(row)}</span>
      <span class="tq-who">
        ${tqAvatar(row)}
        <span class="tq-who-text">
          <span class="tq-name-line"><span class="tq-name tq-link">${escapeHtml(row.display_name || "(无名)")}</span>${tqGenderAge(row)}</span>
          <span class="tq-sub"><span class="tq-phone">${escapeHtml(row.phone || "")}</span>${owe ? `<span class="tq-owe-amt">欠 ${escapeHtml(todayMoneyOrMask(row.unpaid_amount))}</span>` : ""}</span>
        </span>
      </span>
      <span class="tq-dim">${tqTimeCell(row)}${row.room ? ` <span class="tq-room-chip">${escapeHtml(row.room)}</span>` : ""} ${tqRegChip(row)}</span>
      <span class="tq-dim">
        ${row.doctor_name ? `<span class="tq-doc">${escapeHtml(row.doctor_name)}</span>` : ""}
        ${row.item_name ? `<span class="tq-proj"><b>预约</b>${escapeHtml(row.item_name)}</span>` : ""}
        ${row.today_items ? `<span class="tq-proj tq-proj-today"><b>今日</b>${escapeHtml(row.today_items)}</span>` : ""}
        ${row.visit_type ? `<span class="tq-vtype">${escapeHtml(row.visit_type)}</span>` : ""}
      </span>
      <span>${appointmentStatusBadge(row.status)}</span>
    </div>
  `;
}

function todayQueueActions(patientId) {
  const id = escapeAttr(patientId || "");
  return `
    <span class="today-queue-actions">
      <button type="button" class="plain-button queue-shortcut" data-queue-view="patients" data-patient-id="${id}">患者</button>
      <button type="button" class="plain-button queue-shortcut queue-shortcut-medical" data-queue-view="patients" data-queue-tab-target="medical" data-patient-id="${id}">病历</button>
      <button type="button" class="plain-button queue-shortcut queue-shortcut-note" data-queue-view="patients" data-queue-tab-target="notes" data-patient-id="${id}">备注</button>
      <button type="button" class="plain-button queue-shortcut" data-queue-view="appointments" data-patient-id="${id}">预约</button>
      <button type="button" class="plain-button queue-shortcut queue-shortcut-return-visit" data-queue-view="return-visits" data-patient-id="${id}">回访</button>
      <button type="button" class="plain-button queue-shortcut" data-queue-view="billing" data-patient-id="${id}">收费</button>
    </span>
  `;
}

function openQueueShortcut(view, patientId, tab = "") {
  switchWorkspaceView(view, patientId || "");
  if (view === "patients" && patientId) openPatientWorkspace(patientId, tab);
}

function appointmentStatusBadge(status) {
  const stageCls = ["status-waiting", "status-waiting", "status-arrived", "status-arrived", "status-finished", "status-finished"];
  const st = tqStage({status});
  const cls = st === -1 ? "status-cancelled" : (stageCls[st] || "status-unknown");
  return `<span class="appointment-status-badge ${cls}">${escapeHtml(formatAppointmentStatus(status))}</span>`;
}

function formatAppointmentStatus(status) {
  const value = String(status ?? "").trim();
  const labels = {
    "0": "待确认", "": "待确认", "已预约": "待确认",
    "已确认": "已确认", "预约确认": "已确认",
    "1": "已到达", "已到诊": "已到达", "已到达": "已到达", "预约到达": "已到达",
    "已分诊": "已分诊",
    "2": "完成治疗", "已完成": "完成治疗", "完成": "完成治疗", "完成治疗": "完成治疗",
    "已离开": "已离开", "患者离开": "已离开",
    "3": "已取消", "已取消": "已取消",
  };
  return labels[value] || value || "待确认";
}

function todayStatusItem(label, value, display) {
  return `
    <div class="today-status-item">
      <span>${label}</span>
      <strong>${escapeHtml(display != null ? display : formatCount(value))}</strong>
    </div>
  `;
}

function todayFlowNode(label, value, hint, filter = "all", flow = filter) {
  return `
    <div class="${todayFlowNodeClass(flow)}">
      <button type="button" onclick="setTodayQueueFlowFilter('${escapeAttr(filter)}', '${escapeAttr(flow)}')">
        <span>${escapeHtml(label)}</span>
        <strong>${escapeHtml(formatCount(value))}</strong>
        <small>${escapeHtml(hint || "")}</small>
      </button>
    </div>
  `;
}

function todayFlowNodeClass(flow) {
  return todayFlowFilter === flow ? "today-flow-node active" : "today-flow-node";
}

function todayActionButton(label, view, count = "", display) {
  const shown = display != null ? display : (count === "" || count === null || count === undefined ? "" : formatCount(count));
  const countText = shown === "" ? "" : `
    <span class="today-action-count">${escapeHtml(shown)}</span>`;
  return `
    <button type="button" class="plain-button today-action" data-today-action-view="${escapeAttr(view)}" onclick="switchWorkspaceView('${view}')">
      <span>${label}</span>
      ${countText}
    </button>
  `;
}

async function refreshTodayWorkbench() {
  const status = document.getElementById("todayRefreshStatus");
  if (status) status.textContent = "刷新中...";
  try {
    await loadTodayWork();
    const nextStatus = document.getElementById("todayRefreshStatus");
    if (nextStatus) nextStatus.textContent = "已刷新";
  } catch {
    const nextStatus = document.getElementById("todayRefreshStatus");
    if (nextStatus) nextStatus.textContent = "刷新失败";
  }
}

function syncTodayActions(view) {
  document.querySelectorAll("[data-today-action-view]").forEach(button => {
    button.classList.toggle("active", button.dataset.todayActionView === view);
  });
}

function todayWorkCard(title, count, rows, renderer) {
  return `
    <section class="today-card">
      <div class="today-card-head">
        <span>${title}</span>
        <strong class="today-card-badge">${escapeHtml(formatCount(count))}</strong>
      </div>
      <div class="today-card-list">
        ${rows.length ? rows.slice(0, 4).map(renderer).join("") : '<span class="empty">暂无</span>'}
      </div>
    </section>
  `;
}

function renderTodayAppointment(row) {
  return todayPatientRow(row.patient_identity, row.display_name, [
    row.start_time,
    row.doctor_name,
    row.item_name,
  ]);
}

function renderTodayReturnVisit(row) {
  return todayPatientRow(row.patient_identity, row.display_name, [
    row.due_time,
    row.item_name,
    returnVisitStatusText(row.status),   // 扫荡#400:不再显示裸状态码「0」
  ]);
}

function renderTodayBill(row) {
  return todayPatientRow(row.patient_identity, row.display_name, [
    row.bill_no,
    `欠 ${todayMoneyOrMask(row.unpaid_fee)}`,
  ]);
}

function renderTodayAudit(row) {
  return `
    <button type="button" class="today-row">
      <span>${escapeHtml(row.action || "")}</span>
      <small>${escapeHtml([row.entity_type, row.created_at].filter(Boolean).join(" | "))}</small>
    </button>
  `;
}

function todayPatientRow(patientId, name, parts) {
  return `
    <button type="button" class="today-row" data-patient-id="${escapeAttr(patientId || "")}">
      <span>${escapeHtml(name || "(无名)")}</span>
      <small>${escapeHtml(parts.filter(Boolean).join(" | "))}</small>
    </button>
  `;
}

Object.assign(window, {
  loadTodayWork,
  renderTodayWork,
  setTodayQueueSearch,
  clearTodayQueueSearch,
  clearTodayQueueFilters,
  setTodayQueueFlowFilter,
  setTodayQueueFilter,
  openQueueShortcut,
  appointmentStatusBadge,
  todaySaasEntryList,
  openTodaySaasEntry,
  openTodaySaasEntryFull,
  todaySaasKpiStrip,
  todaySaasKpiItem,
  refreshTodayWorkbench,
  syncTodayActions,
});
