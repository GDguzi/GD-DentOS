// 预约·月视图 + 列表视图(对标 SaaS)。复用 appointment_grid.js 的 localISO/apptBlockClass/apptStatusLabel。
// 月视图：7列月历格,格内堆当天预约条,点格→天视图。列表视图：筛选条+表格+操作。

let _apptMonthToken = 0;
let _apptListToken = 0;

// ====== 月视图 ======
async function loadAppointmentMonthView() {
  if (!modulePanel) return;
  const token = ++_apptMonthToken;
  const base = apptBaseDate();
  const d = new Date(base + "T00:00:00");
  const year = d.getFullYear(), month = d.getMonth();
  const first = new Date(year, month, 1);
  const startDow = (first.getDay() + 6) % 7;          // 周一为0
  const gridStart = new Date(year, month, 1 - startDow);
  const last = new Date(year, month + 1, 0);
  const endDow = (last.getDay() + 6) % 7;
  const gridEnd = new Date(year, month, last.getDate() + (6 - endDow));

  modulePanel.innerHTML = `<div class="module-loading">月历载入中...</div>`;
  let appts, statusFilter = appointmentStatusFilter;
  try {
    const sQ = statusFilter ? `status=${encodeURIComponent(statusFilter)}&` : "";
    const res = await fetch(`/api/appointments?date_from=${localISO(gridStart)}&date_to=${localISO(gridEnd)}&${sQ}pagesize=1000`);
    if (token !== _apptMonthToken) return;
    appts = res.ok ? (await res.json()).list || [] : [];
    if (token !== _apptMonthToken) return;
  } catch {
    if (token !== _apptMonthToken) return;
    modulePanel.innerHTML = `<div class="module-loading">月历载入失败</div>`;
    return;
  }
  // 统计 chip 同源
  const byStatus = {};
  appts.forEach(a => { const k = String(a.status || ""); byStatus[k] = (byStatus[k] || 0) + 1; });
  const summary = {by_status: Object.entries(byStatus).map(([key, count]) => ({key, count}))};
  // 按天分组
  const byDay = {};
  appts.forEach(a => { const day = String(a.start_time || "").slice(0, 10); if (day) (byDay[day] = byDay[day] || []).push(a); });

  modulePanel.innerHTML = `
    <div class="module-head"><strong>预约排班</strong><span>${year}年${month + 1}月 · ${escapeHtml(formatCount(appts.length))} 条</span></div>
    ${scheduleViewTabs()}
    ${appointmentMonthNav(year, month)}
    ${scheduleStatusFilters(summary)}
    ${renderMonthGrid(year, month, gridStart, gridEnd, byDay)}
  `;
  if (typeof bindAppointmentInteractions === "function") bindAppointmentInteractions(modulePanel);
  bindMonthView(modulePanel);
}

function appointmentMonthNav(year, month) {
  return `
    <div class="appt-date-nav">
      <button type="button" class="plain-button" data-appt-month-shift="-1">◀ 上月</button>
      <strong>${year}年${month + 1}月</strong>
      <button type="button" class="plain-button" data-appt-month-shift="1">下月 ▶</button>
      <button type="button" class="plain-button" data-appt-date-today="1">本月</button>
    </div>`;
}

function renderMonthGrid(year, month, gridStart, gridEnd, byDay) {
  let head = ["一", "二", "三", "四", "五", "六", "日"].map(w => `<div class="appt-mhead">${w}</div>`).join("");
  let cells = "";
  const cur = new Date(gridStart);
  while (cur <= gridEnd) {
    const ds = localISO(cur);
    const inMonth = cur.getMonth() === month;
    const list = byDay[ds] || [];
    const shown = list.slice(0, 4);
    const more = list.length - shown.length;
    cells += `
      <div class="appt-mcell${inMonth ? "" : " appt-mcell-out"}" data-month-day="${escapeAttr(ds)}">
        <div class="appt-mcell-date">${cur.getDate()}</div>
        ${shown.map(a => `<div class="appt-mentry ${apptBlockClass(a.status)}">${escapeHtml(String(a.start_time || "").slice(11, 16))} ${escapeHtml(a.display_name || "")}</div>`).join("")}
        ${more > 0 ? `<div class="appt-mmore">+${more} 更多</div>` : ""}
      </div>`;
    cur.setDate(cur.getDate() + 1);
  }
  return `<div class="appt-month"><div class="appt-mhead-row">${head}</div><div class="appt-mgrid">${cells}</div></div>`;
}

function bindMonthView(container) {
  if (!container) return;
  container.querySelectorAll("[data-appt-month-shift]").forEach(b =>
    b.addEventListener("click", () => {
      const d = new Date(apptBaseDate() + "T00:00:00");
      d.setDate(1);
      d.setMonth(d.getMonth() + parseInt(b.dataset.apptMonthShift, 10));
      setWorkDateTo(localISO(d));
    }));
  // 点某天 → 切到天视图看当天
  container.querySelectorAll("[data-month-day]").forEach(cell =>
    cell.addEventListener("click", () => {
      if (typeof workDate !== "undefined" && workDate) workDate.value = cell.dataset.monthDay;
      if (typeof setAppointmentView === "function") setAppointmentView("day");
    }));
}

// ====== 列表视图(对标 SaaS 表格) ======
async function loadAppointmentListView() {
  if (!modulePanel) return;
  const token = ++_apptListToken;
  modulePanel.innerHTML = `<div class="module-loading">列表载入中...</div>`;
  // 列表用筛选条的日期区间(默认当天)
  const from = _apptListFrom || apptBaseDate();
  const to = _apptListTo || apptBaseDate();
  const statusQ = appointmentStatusFilter ? `status=${encodeURIComponent(appointmentStatusFilter)}&` : "";
  const docQ = _apptListDoctor ? `doctor_name=${encodeURIComponent(_apptListDoctor)}&` : "";
  const kw = moduleSearchValue();
  const kwQ = kw ? `q=${encodeURIComponent(kw)}&` : "";
  let data, doctors;
  try {
    const [rA, rD] = await Promise.all([
      fetch(`/api/appointments?date_from=${encodeURIComponent(from)}&date_to=${encodeURIComponent(to)}&${statusQ}${docQ}${kwQ}pageno=${modulePage}`),
      fetch("/api/staff-members?role=医生"),
    ]);
    if (token !== _apptListToken) return;
    data = rA.ok ? await rA.json() : {list: [], totalcount: 0};
    doctors = rD.ok ? (await rD.json()).list || [] : [];
    if (token !== _apptListToken) return;
  } catch {
    if (token !== _apptListToken) return;
    modulePanel.innerHTML = `<div class="module-loading">列表载入失败</div>`;
    return;
  }
  const rows = data.list || [];
  modulePanel.innerHTML = `
    <div class="module-head"><strong>预约列表</strong><span>${escapeHtml(formatCount(data.totalcount))} 条</span></div>
    ${scheduleViewTabs()}
    ${appointmentListFilters(from, to, doctors)}
    <div class="module-table appt-list-table">
      <div class="module-row module-row-head">
        <span>操作</span><span>状态</span><span>患者</span><span>电话</span><span>预约时间</span><span>预约医生</span><span>项目</span>
      </div>
      ${rows.map(appointmentListRow).join("") || '<div class="module-empty">暂无预约</div>'}
    </div>
    ${modulePager(data, "loadAppointmentModule")}
  `;
  if (typeof bindAppointmentInteractions === "function") bindAppointmentInteractions(modulePanel);
  bindListView(modulePanel);
}

let _apptListFrom = "", _apptListTo = "", _apptListDoctor = "";

function appointmentListFilters(from, to, doctors) {
  const docOpts = [`<option value="">全部医生</option>`].concat(
    doctors.map(d => `<option value="${escapeAttr(d.name)}"${d.name === _apptListDoctor ? " selected" : ""}>${escapeHtml(d.name)}</option>`)).join("");
  return `
    <div class="appt-list-filters">
      <label>预约时间 <input type="date" data-list-from value="${escapeAttr(from)}"> - <input type="date" data-list-to value="${escapeAttr(to)}"></label>
      <label>预约医生 <select data-list-doctor>${docOpts}</select></label>
      <button type="button" class="plain-button" data-list-query="1">查询</button>
      ${moduleToolbar("搜索患者/医生/项目", "loadAppointmentModule", moduleSearchValue())}
    </div>`;
}

function appointmentListRow(row) {
  const apptId = row.appointment_id || "";
  // #615:原来只挡"已取消/3"，"完成治疗"等终态行仍渲染可点的取消按钮，点了会清到达/完成时间戳。
  // 复用今日队列同一套 tqStage() 终态判定(stage 4=完成/完成治疗，5=已离开，-1=已取消/爽约)，
  // 不再各写一套口径。列表页没有每行收费状态数据，这里比今日队列稍严——完成即锁，不像队列
  // 那样"完成但未收费"仍可取消(#330)，偏保守但避免误撤已完成就诊。
  const stage = typeof tqStage === "function" ? tqStage(row) : 0;
  const cancelable = apptId && stage >= 0 && stage < 4;
  return `
    <div class="module-row">
      <span class="appt-row-actions">
        <button type="button" class="module-row-action" data-patient-id="${escapeAttr(row.patient_identity || "")}">查看</button>
        ${cancelable ? `<button type="button" class="module-row-action appt-cancel" data-appt-cancel="${escapeAttr(apptId)}">取消</button>` : ""}
      </span>
      <span>${typeof appointmentStatusBadge === "function" ? appointmentStatusBadge(row.status) : escapeHtml(apptStatusLabel(row.status))}</span>
      <span>${escapeHtml(row.display_name || "(无名)")}</span>
      <span>${escapeHtml(row.phone || "")}</span>
      <span>${escapeHtml(row.start_time || "")}</span>
      <span>${escapeHtml(row.doctor_name || "")}</span>
      <span>${escapeHtml(row.item_name || "")}</span>
    </div>`;
}

function bindListView(container) {
  if (!container) return;
  const q = container.querySelector("[data-list-query]");
  if (q) q.addEventListener("click", () => {
    _apptListFrom = (container.querySelector("[data-list-from]") || {}).value || "";
    _apptListTo = (container.querySelector("[data-list-to]") || {}).value || "";
    _apptListDoctor = (container.querySelector("[data-list-doctor]") || {}).value || "";
    modulePage = 1;
    loadAppointmentModule();
  });
}

// ====== 周视图(7天 × 时间轴网格) ======
let _apptWeekToken = 0;

async function loadAppointmentWeekView() {
  if (!modulePanel) return;
  const token = ++_apptWeekToken;
  const base = apptBaseDate();
  const d = new Date(base + "T00:00:00");
  const dow = (d.getDay() + 6) % 7;                 // 周一为0
  const monday = new Date(d); monday.setDate(d.getDate() - dow);
  const days = [];
  for (let i = 0; i < 7; i++) { const x = new Date(monday); x.setDate(monday.getDate() + i); days.push(localISO(x)); }

  modulePanel.innerHTML = `<div class="module-loading">周视图载入中...</div>`;
  let settings, appts;
  try {
    const sQ = appointmentStatusFilter ? `status=${encodeURIComponent(appointmentStatusFilter)}&` : "";
    const [rS, rA] = await Promise.all([
      fetch("/api/settings/appointment"),
      fetch(`/api/appointments?date_from=${days[0]}&date_to=${days[6]}&${sQ}pagesize=1000`),
    ]);
    if (token !== _apptWeekToken) return;
    settings = rS.ok ? await rS.json() : {};
    appts = rA.ok ? (await rA.json()).list || [] : [];
    if (token !== _apptWeekToken) return;
  } catch {
    if (token !== _apptWeekToken) return;
    modulePanel.innerHTML = `<div class="module-loading">周视图载入失败</div>`;
    return;
  }
  const byStatus = {};
  appts.forEach(a => { const k = String(a.status || ""); byStatus[k] = (byStatus[k] || 0) + 1; });
  const summary = {by_status: Object.entries(byStatus).map(([key, count]) => ({key, count}))};

  modulePanel.innerHTML = `
    <div class="module-head"><strong>预约排班</strong><span>${days[0]} ~ ${days[6]} · ${escapeHtml(formatCount(appts.length))} 条</span></div>
    ${scheduleViewTabs()}
    <div class="appt-date-nav">
      <button type="button" class="plain-button" data-appt-week-shift="-1">◀ 上周</button>
      <strong>${days[0]} ~ ${days[6]}</strong>
      <button type="button" class="plain-button" data-appt-week-shift="1">下周 ▶</button>
      <button type="button" class="plain-button" data-appt-date-today="1">本周</button>
    </div>
    ${scheduleStatusFilters(summary)}
    <div class="appt-grid-wrap">${renderWeekGrid(settings, days, appts)}</div>
  `;
  if (typeof bindAppointmentInteractions === "function") bindAppointmentInteractions(modulePanel);
  bindWeekView(modulePanel, appts);
}

function renderWeekGrid(settings, days, appts) {
  const startMin = hm2min(settings.business_start) ?? 480;
  const endMin = hm2min(settings.business_end) ?? 1080;
  const slot = Number(settings.min_slot_minutes) || 30;
  const nSlots = Math.max(1, Math.ceil((endMin - startMin) / slot));
  const SLOT_H = 22;
  const WEEK_DOW = ["周一", "周二", "周三", "周四", "周五", "周六", "周日"];
  const cols = `64px repeat(7, minmax(90px, 1fr))`;
  const rows = `28px repeat(${nSlots}, ${SLOT_H}px)`;

  let cells = `<div class="appt-grid-head appt-grid-corner" style="grid-row:1;grid-column:1">时间</div>`;
  days.forEach((ds, i) => {
    cells += `<div class="appt-grid-head" style="grid-row:1;grid-column:${i + 2}">${WEEK_DOW[i]}<br>${escapeHtml(ds.slice(5))}</div>`;
  });
  for (let s = 0; s < nSlots; s++) {
    const min = startMin + s * slot;
    const hh = String(Math.floor(min / 60)).padStart(2, "0");
    const mm = String(min % 60).padStart(2, "0");
    const isHour = (min % 60) === 0;
    cells += `<div class="appt-grid-time${isHour ? " appt-grid-hour" : ""}" style="grid-row:${s + 2};grid-column:1">${isHour ? hh + ":00" : mm}</div>`;
    days.forEach((ds, i) => {
      cells += `<div class="appt-grid-cell" data-cell-date="${escapeAttr(ds)}" data-cell-time="${escapeAttr(`${hh}:${mm}`)}" style="grid-row:${s + 2};grid-column:${i + 2}"></div>`;
    });
  }
  const dayIndex = {}; days.forEach((ds, i) => { dayIndex[ds] = i; });
  appts.forEach(a => {
    const sMin = apptStartMin(a);
    if (sMin == null) return;
    const day = String(a.start_time || "").slice(0, 10);
    const col = dayIndex[day];
    if (col == null) return;
    const eMin = apptEndMin(a, slot);
    let rowStart = Math.max(0, Math.min(Math.floor((sMin - startMin) / slot), nSlots - 1));
    let rowEnd = Math.max(rowStart + 1, Math.min(Math.ceil((eMin - startMin) / slot), nSlots));
    cells += `
      <button type="button" class="appt-block ${apptBlockClass(a.status)}"
        data-appt-block="${escapeAttr(a.appointment_id || "")}"
        style="grid-row:${rowStart + 2}/${rowEnd + 2};grid-column:${col + 2}">
        <span class="appt-block-name">${escapeHtml(a.display_name || "(无名)")}</span>
        <span class="appt-block-item">${escapeHtml(a.item_name || "")}</span>
        <span class="appt-block-time">${escapeHtml(String(a.start_time || "").slice(11, 16))}</span>
      </button>`;
  });
  return `<div class="appt-grid" style="grid-template-columns:${cols};grid-template-rows:${rows}">${cells}</div>`;
}

function bindWeekView(container, appts) {
  if (!container) return;
  container.querySelectorAll("[data-appt-week-shift]").forEach(b =>
    b.addEventListener("click", () => {
      const d = new Date(apptBaseDate() + "T00:00:00");
      d.setDate(d.getDate() + 7 * parseInt(b.dataset.apptWeekShift, 10));
      setWorkDateTo(localISO(d));
    }));
  container.querySelectorAll("[data-appt-date-today]").forEach(b =>
    b.addEventListener("click", () => setWorkDateTo(localDateValue())));
  // 点空格新增(带入那天那时段)
  container.querySelectorAll("[data-cell-date]").forEach(cell =>
    cell.addEventListener("click", () => {
      if (typeof openNewAppointment === "function")
        openNewAppointment({start: `${cell.dataset.cellDate} ${cell.dataset.cellTime}`});
    }));
  // 点块弹患者卡片
  const byId = {};
  (appts || []).forEach(a => { byId[a.appointment_id] = a; });
  container.querySelectorAll("[data-appt-block]").forEach(blk =>
    blk.addEventListener("click", event => {
      event.stopPropagation();
      if (typeof showApptCard === "function") showApptCard(byId[blk.dataset.apptBlock], blk);
    }));
}

Object.assign(window, {
  loadAppointmentMonthView, renderMonthGrid, bindMonthView,
  loadAppointmentListView, appointmentListRow, bindListView,
  loadAppointmentWeekView, renderWeekGrid, bindWeekView,
});
