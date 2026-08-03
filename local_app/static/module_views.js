// 模块面板竞态守卫：多个视图共用 modulePanel，快速切换时丢弃过期响应（同 ai_drafts.js 的 aiDraftsToken）。
let moduleViewToken = 0;

// 预约视图状态：day单日/week本周/month整月/list列表；statusFilter 按状态过滤（空=全部）。
let appointmentView = "day";
let appointmentStatusFilter = "";

// 视图 → 取数日期范围（基准日取工作日，没有则今天）。返回 query 片段。
function appointmentDateRangeQuery() {
  const base = (workDate && workDate.value) ? workDate.value : localDateValue();
  const d = dateFromWorkValue(base);
  const iso = x => localDateValue(x);   // 不能用 toISOString(转UTC会差一天,#541)
  if (appointmentView === "list") {
    // 列表：不限日期（全部），靠搜索/状态过滤收窄
    return "";
  }
  if (appointmentView === "week") {
    const dow = (d.getDay() + 6) % 7;   // 周一为 0
    const mon = new Date(d); mon.setDate(d.getDate() - dow);
    const sun = new Date(mon); sun.setDate(mon.getDate() + 6);
    return `date_from=${iso(mon)}&date_to=${iso(sun)}&`;
  }
  if (appointmentView === "month") {
    const first = new Date(d.getFullYear(), d.getMonth(), 1);
    const last = new Date(d.getFullYear(), d.getMonth() + 1, 0);
    return `date_from=${iso(first)}&date_to=${iso(last)}&`;
  }
  return `date=${encodeURIComponent(base)}&`;   // day
}

function setAppointmentView(view) {
  appointmentView = view;
  modulePage = 1;
  loadAppointmentModule();
}

function setAppointmentStatusFilter(value) {
  appointmentStatusFilter = (value === appointmentStatusFilter) ? "" : value;   // 再点同一个=取消过滤
  modulePage = 1;
  loadAppointmentModule();
}

async function loadAppointmentModule() {
  if (!modulePanel) return;
  // 四视图：天=泳道网格 / 周=7天网格 / 月=月历 / 列表=表格
  if (appointmentView === "day" && typeof loadAppointmentDayGrid === "function") return loadAppointmentDayGrid();
  if (appointmentView === "week" && typeof loadAppointmentWeekView === "function") return loadAppointmentWeekView();
  if (appointmentView === "month" && typeof loadAppointmentMonthView === "function") return loadAppointmentMonthView();
  if (appointmentView === "list" && typeof loadAppointmentListView === "function") return loadAppointmentListView();
  const token = ++moduleViewToken;
  const keyword = moduleSearchValue();
  const rangeQuery = appointmentDateRangeQuery();
  const searchQuery = keyword ? `q=${encodeURIComponent(keyword)}&` : "";
  const statusQuery = appointmentStatusFilter ? `status=${encodeURIComponent(appointmentStatusFilter)}&` : "";
  modulePanel.innerHTML = `<div class="module-loading">预约载入中...</div>`;
  let data, summary;
  try {
    const [resList, resSum] = await Promise.all([
      fetch(`/api/appointments?${rangeQuery}${searchQuery}${statusQuery}pageno=${modulePage}`),
      fetch(`/api/appointments/summary?${rangeQuery.replace(/&$/, "") || "date=" + encodeURIComponent((workDate && workDate.value) || localDateValue())}`),
    ]);
    if (token !== moduleViewToken) return;
    if (!resList.ok) { modulePanel.innerHTML = `<div class="module-loading">预约载入失败</div>`; return; }
    data = await resList.json();
    summary = resSum.ok ? await resSum.json() : {by_status: []};
    if (token !== moduleViewToken) return;
  } catch {
    if (token !== moduleViewToken) return;
    modulePanel.innerHTML = `<div class="module-loading">预约载入失败</div>`;
    return;
  }
  const rows = data.list || [];
  modulePanel.innerHTML = `
    <div class="module-head">
      <strong>预约排班</strong>
      <span>${escapeHtml(formatCount(data.totalcount))} 条</span>
    </div>
    ${scheduleViewTabs()}
    ${scheduleQuickBar()}
    ${moduleToolbar("搜索患者 / 医生 / 项目", "loadAppointmentModule", keyword)}
    ${scheduleStatusFilters(summary)}
    ${renderScheduleBoard(rows)}
    <div class="module-table appointment-module schedule-list-table">
      <div class="module-row module-row-head">
        <span>预约时间</span>
        <span>患者</span>
        <span>医生</span>
        <span>项目</span>
        <span>状态</span>
        <span>操作</span>
      </div>
      ${rows.map(renderAppointmentModuleRow).join("") || '<div class="module-empty">暂无预约</div>'}
    </div>
    ${modulePager(data, "loadAppointmentModule")}
  `;
  bindPatientDetailRows(modulePanel);
  bindAppointmentInteractions(modulePanel);
}

function scheduleViewTabs() {
  const views = [["day", "天"], ["week", "周"], ["month", "月"], ["list", "列表"]];
  return `
    <nav class="module-tabs schedule-view-tabs" aria-label="预约视图">
      ${views.map(([v, label]) => `
        <button type="button" class="plain-button${v === appointmentView ? " active" : ""}" data-appt-view="${v}">${label}</button>
      `).join("")}
      <span class="module-tabs-spacer"></span>
      <button type="button" class="plain-button" data-appt-new="1">新增预约</button>
    </nav>
  `;
}

// 绑定预约模块的交互（视图tab/状态chip/新增/取消），data-* + 事件委托防注入。
function bindAppointmentInteractions(container) {
  if (!container) return;
  container.querySelectorAll("[data-appt-view]").forEach(btn =>
    btn.addEventListener("click", () => setAppointmentView(btn.dataset.apptView)));
  container.querySelectorAll("[data-appt-new]").forEach(btn =>
    btn.addEventListener("click", () => { if (typeof openNewAppointment === "function") openNewAppointment(); }));
  container.querySelectorAll("[data-appt-status]").forEach(btn =>
    btn.addEventListener("click", () => setAppointmentStatusFilter(btn.dataset.apptStatus)));
  container.querySelectorAll("[data-appt-cancel]").forEach(btn =>
    btn.addEventListener("click", event => {
      event.stopPropagation();
      if (typeof cancelAppointment === "function") cancelAppointment(btn.dataset.apptCancel);
    }));
}

function scheduleQuickBar() {
  return `
    <div class="schedule-quick-bar" aria-label="预约快捷日期">
      ${["7天后", "1个月后", "3个月后", "半年后", "1年后"].map(label => `<span>${label}</span>`).join("")}
      <strong>${escapeHtml(todayDateLabel ? todayDateLabel() : "")}</strong>
    </div>
  `;
}

// 状态过滤条：从 summary.by_status 渲染真实状态+计数，可点击过滤（再点取消）。
function scheduleStatusFilters(summary) {
  const byStatus = (summary && summary.by_status) || [];
  const total = byStatus.reduce((s, c) => s + (c.count || 0), 0);
  const chips = [`<span class="schedule-status-filter${appointmentStatusFilter === "" ? " active" : ""}" role="button" tabindex="0" data-appt-status="">全部 <strong>${escapeHtml(formatCount(total))}</strong></span>`];
  byStatus.forEach(c => {
    const value = String(c.key || "");
    if (!value) return;   // 空状态不单列
    const label = (typeof formatAppointmentStatus === "function") ? formatAppointmentStatus(value) : value;
    chips.push(`<span class="schedule-status-filter${value === appointmentStatusFilter ? " active" : ""}" role="button" tabindex="0" data-appt-status="${escapeAttr(value)}">${escapeHtml(label)} <strong>${escapeHtml(formatCount(c.count || 0))}</strong></span>`);
  });
  return `<div class="schedule-status-filters" aria-label="预约状态">${chips.join("")}</div>`;
}

function renderScheduleBoard(rows) {
  const groups = groupAppointmentsByDoctor(rows);
  const doctors = Object.keys(groups);
  return `
    <section class="schedule-board" aria-label="医生预约排班">
      ${doctors.length ? doctors.map(doctor => `
        <div class="schedule-doctor-column">
          <div class="schedule-doctor-head">${escapeHtml(doctor)} <span>${escapeHtml(formatCount(groups[doctor].length))}</span></div>
          <div class="schedule-doctor-list">
            ${groups[doctor].map(renderScheduleCard).join("")}
          </div>
        </div>
      `).join("") : '<div class="module-empty">暂无预约排班</div>'}
    </section>
  `;
}

function groupAppointmentsByDoctor(rows) {
  return rows.reduce((groups, row) => {
    const doctor = row.doctor_name || "未指定医生";
    if (!groups[doctor]) groups[doctor] = [];
    groups[doctor].push(row);
    return groups;
  }, {});
}

function renderScheduleCard(row) {
  const patientId = row.patient_identity || "";
  return `
    <button type="button" class="schedule-card" data-patient-id="${escapeAttr(patientId)}">
      <strong>${escapeHtml(row.display_name || "(无名)")}</strong>
      <span>${escapeHtml(scheduleTimeRange(row))}</span>
      <small>${escapeHtml(row.item_name || "")}</small>
      ${appointmentStatusBadge(row.status)}
    </button>
  `;
}

function scheduleTimeRange(row) {
  const start = row.start_time || "";
  const end = row.end_time || "";
  if (!start && !end) return "";
  return [start.slice(11, 16) || start, end.slice(11, 16) || end].filter(Boolean).join(" - ");
}

function renderAppointmentModuleRow(row) {
  const patientId = row.patient_identity || "";
  const apptId = row.appointment_id || "";
  // 已取消的不再给取消按钮
  const cancelBtn = (apptId && String(row.status || "") !== "已取消")
    ? `<button type="button" class="module-row-action appt-cancel" data-appt-cancel="${escapeAttr(apptId)}">取消</button>`
    : "";
  return `
    <div class="module-row" role="button" tabindex="0" data-patient-id="${escapeAttr(patientId)}">
      <span>${escapeHtml(row.start_time || "")}</span>
      <span>${escapeHtml(row.display_name || "(无名)")}</span>
      <span>${escapeHtml(row.doctor_name || "")}</span>
      <span>${escapeHtml(row.item_name || "")}</span>
      <span>${appointmentStatusBadge(row.status)}</span>
      <span class="appt-row-actions">${moduleRowAction(patientId)}${cancelBtn}</span>
    </div>
  `;
}

// ===== 客户通 V2(#856):三视图(今日总览/回访列表/沟通记录)与回访处理弹窗
// 全部在独立模块 customer_hub_v2.js;此处仅保留视图入口委托(旧 today/list/study/calendar 视图已退役)。
async function loadReturnVisitModule() {
  const ctx = Object.create(null);
  const source = (typeof moduleContext !== "undefined" && moduleContext
    && typeof moduleContext === "object") ? moduleContext : null;
  try {
    if (source) {
      const descriptors = Object.getOwnPropertyDescriptors(source);
      for (const key of Object.keys(descriptors)) {
        const descriptor = descriptors[key];
        if (!descriptor.enumerable || !Object.prototype.hasOwnProperty.call(descriptor, "value")) continue;
        Object.defineProperty(ctx, key, {
          configurable: true,
          enumerable: true,
          value: descriptor.value,
          writable: true,
        });
      }
    }
    ctx.searchText = typeof moduleSearchSeed === "string" ? moduleSearchSeed.trim() : "";
    let selectedDate = "";
    if (typeof workDate !== "undefined" && workDate) {
      const workDateValue = workDate.value;
      if (workDateValue != null && typeof workDateValue !== "string") throw new Error("invalid_work_date");
      selectedDate = typeof workDateValue === "string" ? workDateValue.trim() : "";
    }
    if (!selectedDate) {
      const fallbackDate = localDateValue();
      selectedDate = typeof fallbackDate === "string" ? fallbackDate.trim() : "";
      if (!selectedDate) throw new Error("invalid_local_date");
    }
    ctx.selectedDate = selectedDate;
  } catch {
    if (modulePanel) modulePanel.innerHTML = '<div class="module-loading">客户通载入失败</div>';
    return false;
  }
  return loadCustomerHubV2(ctx);
}

async function requestLeaveReturnVisitModule() {
  if (typeof requestLeaveCustomerHubV2 !== "function") return false;
  return (await requestLeaveCustomerHubV2()) === true;
}

function moduleContextChip(context = {}, reloadFunctionName) {
  if (context.patientFilter !== "birthday_today") return "";
  return `
    <span class="module-filter-chip">
      当前过滤：今日生日
      <button type="button" class="plain-button" onclick="clearModuleFilter('${reloadFunctionName}')">清除过滤</button>
    </span>
  `;
}

function moduleToolbar(placeholder, reloadFunctionName, value = "") {
  return `
    <div class="module-toolbar">
      <input
        id="moduleSearchInput"
        value="${escapeAttr(value || "")}"
        placeholder="${escapeAttr(placeholder)}"
        onkeydown="if (event.key === 'Enter') reloadModuleFromFirstPage('${reloadFunctionName}')"
      >
      <button type="button" onclick="reloadModuleFromFirstPage('${reloadFunctionName}')">刷新</button>
      ${moduleFilterChip(value, reloadFunctionName)}
    </div>
  `;
}

function moduleFilterChip(value, reloadFunctionName) {
  const text = String(value || "").trim();
  if (!text) return "";
  return `
    <span class="module-filter-chip">
      当前过滤：${escapeHtml(text)}
      <button type="button" class="plain-button" onclick="clearModuleFilter('${reloadFunctionName}')">清除过滤</button>
    </span>
  `;
}

function modulePager(data, reloadFunctionName) {
  const page = Number(data.pageno || modulePage || 1);
  const pageSize = Number(data.pagesize || 50);
  const total = Number(data.totalcount || 0);
  const maxPage = Math.max(1, Math.ceil(total / pageSize));
  return `
    <div class="module-pager">
      <button type="button" ${page <= 1 ? "disabled" : ""} onclick="changeModulePage(${page - 1}, '${reloadFunctionName}')">上一页</button>
      <span>第 ${escapeHtml(page)} / ${escapeHtml(maxPage)} 页</span>
      <button type="button" ${page >= maxPage ? "disabled" : ""} onclick="changeModulePage(${page + 1}, '${reloadFunctionName}')">下一页</button>
    </div>
  `;
}

function changeModulePage(nextPage, reloadFunctionName) {
  modulePage = Math.max(1, Number(nextPage) || 1);
  window[reloadFunctionName]();
}

function reloadModuleFromFirstPage(reloadFunctionName) {
  modulePage = 1;
  window[reloadFunctionName]();
}

function clearModuleFilter(reloadFunctionName) {
  moduleSearchSeed = "";
  moduleContext = {};
  if (q) q.value = "";
  const input = document.getElementById("moduleSearchInput");
  if (input) input.value = "";
  reloadModuleFromFirstPage(reloadFunctionName);
}

function moduleSearchValue() {
  const input = document.getElementById("moduleSearchInput");
  return input ? input.value.trim() : moduleSearchSeed;
}

function bindPatientDetailRows(container) {
  if (!container) return;
  bindModuleRowInteractions(container);
}

function bindModuleRowInteractions(container) {
  container.querySelectorAll("[data-queue-view]").forEach(button => {
    button.addEventListener("click", event => {
      event.stopPropagation();
      openQueueShortcut(button.dataset.queueView, button.dataset.patientId, button.dataset.queueTabTarget);
    });
  });

  container.querySelectorAll("[data-patient-id]").forEach(el => {
    if (el.dataset.queueView) return;
    if (el.classList.contains("module-row-action")) {
      el.addEventListener("click", event => {
        event.stopPropagation();
        openPatientWorkspace(el.dataset.patientId);
      });
      return;
    }

    el.addEventListener("click", () => openPatientWorkspace(el.dataset.patientId));
    if (el.classList.contains("module-row") || el.classList.contains("today-queue-row")) {
      el.addEventListener("keydown", event => {
        if (!isKeyboardOpenKey(event)) return;
        event.preventDefault();
        openPatientWorkspace(el.dataset.patientId);
      });
    }
  });
}

function isKeyboardOpenKey(event) {
  return event.key === "Enter" || event.key === " ";
}

Object.assign(window, {
  loadAppointmentModule,
  setAppointmentView,
  setAppointmentStatusFilter,
  bindAppointmentInteractions,
  renderAppointmentModuleRow,
  loadReturnVisitModule,
  requestLeaveReturnVisitModule,
  moduleToolbar,
  moduleFilterChip,
  modulePager,
  changeModulePage,
  reloadModuleFromFirstPage,
  clearModuleFilter,
  moduleSearchValue,
  bindPatientDetailRows,
  bindModuleRowInteractions,
  isKeyboardOpenKey,
});
