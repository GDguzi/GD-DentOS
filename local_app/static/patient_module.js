// 顶层患者管理模块(呈现层):最近/今日/全部三tab+分类树+表格筛选导出+合并对话框+画像徽章。
// 独立模块(原 module_views.js 拆出);通用module工具(toolbar/pager/chip)仍在 module_views.js。

let patientCenterState = { tab: "all", group: "", expanded: "", selected: "", tableGroup: "", todayView: "list", todayDate: "", todayFold: {},
  tablePage: 1, tablePageSize: 100, tableQ: "", doctorFilter: "", firstDoctorFilter: "" };
let patientGroupsCache = null;
let _groupPatients = {};   // 分组key -> 患者行数组(展开时缓存)
let _todayAppts = [];      // 今日 tab 当前预约列表(供折叠时局部重渲,不重新拉)
let _todayKeyword = "";    // #121/#181 今日 tab 当前搜索词(折叠/切视图局部重渲也要保留过滤)

// 今日 tab 按搜索词过滤(姓名/电话)；空词返回全量。loadPatientTodayTab 和 refreshTodayLeft 共用
function todayFilteredAppts() {
  const kw = _todayKeyword;
  if (!kw) return _todayAppts;
  return _todayAppts.filter(a =>
    String(a.display_name || "").toLowerCase().includes(kw) || String(a.phone || "").includes(kw));
}
let _tableSelected = new Set();   // 全部表格勾选的 patient_identity
let _tableTotal = 0;              // 当前筛选下总条数
let _doctorOptions = null;        // 医生下拉缓存
let _patientRowIndex = {};        // patient_identity -> 患者行(供点击时取姓名记入"最近")
function indexPatients(rows) {
  (rows || []).forEach(r => { if (r && r.patient_identity) _patientRowIndex[r.patient_identity] = r; });
}

async function ensurePatientGroups(force) {
  if (patientGroupsCache && !force) return patientGroupsCache;
  try {
    const res = await fetch("/api/patient-groups");
    patientGroupsCache = res.ok ? await res.json() : null;
  } catch { patientGroupsCache = null; }
  return patientGroupsCache;
}

async function loadPatientModule() {
  if (!modulePanel) return;
  const token = ++moduleViewToken;
  const keyword = moduleSearchValue() || (q && q.value ? q.value.trim() : "");
  const tab = patientCenterState.tab;
  // 三 tab 均为主从式(左选择器 + 右档案/表格)；统一网络守护 + 竞态 token
  try {
    if (tab === "today") await loadPatientTodayTab(token, keyword);
    else if (tab === "recent") await loadPatientRecentTab(token, keyword);
    else await loadPatientAllTab(token, keyword);
  } catch {
    if (token !== moduleViewToken) return;   // 过期响应不覆盖新内容
    modulePanel.innerHTML = `<div class="module-loading">患者载入失败</div>`;
  }
}

// 右栏：未选患者时的占位(选中则由 renderInlineArchive 接管)
function renderCenterRight(emptyMsg) {
  const detail = document.getElementById("pcDetail");
  if (!detail) return;
  if (patientCenterState.selected) { renderInlineArchive(patientCenterState.selected); return; }
  detail.classList.remove("pc-detail-archive");
  detail.innerHTML = `<div class="pc-profile-empty">${escapeHtml(emptyMsg || "← 选择左侧患者查看完整档案")}</div>`;
}

// ===== 最近 tab：点击历史(localStorage) + 右档案 =====
async function loadPatientRecentTab(token, keyword) {
  if (token !== moduleViewToken) return;
  const recent = getRecentPatients();
  modulePanel.innerHTML = `
    <div class="pc-master pc-master-recent">
      <div class="pc-side">
        ${patientCenterTabs(patientCenterState)}
        <div id="pcRail" class="pc-rail">${renderRecentList(recent)}</div>
      </div>
      <div id="pcDetail" class="pc-detail"></div>
    </div>
  `;
  bindPatientCenter(modulePanel);
  renderCenterRight("← 选择左侧最近患者查看完整档案");
}

function renderRecentList(list) {
  list = (list || []).filter(p => p && (p.name || "").trim());   // 跳过早期 bug 留下的空名条目
  if (!list.length) return `<div class="pc-tree-empty">还没有最近点击的患者</div>`;
  // 与今日 tab 完全同款卡片(.pc-today-card)：姓名+主治医生+状态标+电话+日期
  return `<div class="pc-today-cards">` + list.map(p => {
    const date = String(p.visit_date || "").slice(0, 10);
    const t = String(p.visit_date || "").slice(11, 16);
    const badges = patientBadges({
      last_visit_type: p.visit_type, has_record: p.has_record,
      has_future_appt: p.has_future_appt, has_return_visit: p.has_return_visit,
      has_image: p.has_image, groupname: p.groupname,
    });
    return `
    <div class="pc-today-card${patientCenterState.selected === p.id ? " active" : ""}" role="button" tabindex="0" data-pc-pick="${escapeAttr(p.id)}">
      <div class="ptc-top"><span class="pc-name">${escapeHtml(p.name || "(无名)")}</span>
        ${p.doctor ? `<span class="ptc-doc">主 ${escapeHtml(p.doctor)}</span>` : (p.chart ? `<span class="ptc-doc">${escapeHtml(p.chart)}</span>` : "")}</div>
      <div class="ptc-badges">${badges}</div>
      <div class="ptc-meta">
        ${p.phone ? `<span class="pc-phone">${escapeHtml(p.phone)}</span>` : ""}
        ${date ? `<span class="pc-date">${escapeHtml(date)}${t ? " " + escapeHtml(t) : ""}</span>` : ""}
      </div>
    </div>`;
  }).join("") + `</div>`;
}

// ===== 今日 tab：日期导航 + 共N人 + 就诊列表/就诊类型 + 右档案 =====
async function loadPatientTodayTab(token, keyword) {
  const date = patientCenterState.todayDate || (typeof localDateValue === "function" ? localDateValue() : "");
  let tw = {appointments: [], summary: {}};
  try { tw = await (await fetch(`/api/today-work?date=${encodeURIComponent(date)}`)).json(); } catch { /* 用空 */ }
  if (token !== moduleViewToken) return;
  const allAppts = tw.appointments || [];
  _todayAppts = allAppts;
  indexPatients(allAppts);
  // #121/#181：今日 tab 搜索生效(姓名/电话);搜索词存模块态,折叠/切视图局部重渲也保留过滤
  _todayKeyword = String(keyword || "").trim().toLowerCase();
  const appts = todayFilteredAppts();
  modulePanel.innerHTML = `
    <div class="pc-master">
      <div class="pc-side">
        ${patientCenterTabs(patientCenterState)}
        <div id="pcRail" class="pc-rail pc-today-rail">${renderTodayLeft(appts, date)}</div>
      </div>
      <div id="pcDetail" class="pc-detail"></div>
    </div>
  `;
  bindPatientCenter(modulePanel);
  bindTodayLeft(modulePanel);
  renderCenterRight("← 选择左侧今日患者查看完整档案");
}

function renderTodayLeft(appts, date) {
  const view = patientCenterState.todayView || "list";
  const nav = `
    <div class="pc-today-nav">
      <button type="button" class="pc-today-arrow" data-today-nav="-1" aria-label="前一天">‹</button>
      <span class="pc-today-date">${escapeHtml(date)}</span>
      <button type="button" class="pc-today-arrow" data-today-nav="1" aria-label="后一天">›</button>
      <button type="button" class="pc-today-now" data-today-nav="0">今</button>
    </div>
    <div class="pc-today-bar"><span>共 ${appts.length} 人</span>
      <span class="pc-today-toggle">
        <button type="button" class="pc-tg${view === "list" ? " active" : ""}" data-today-view="list">就诊列表</button>
        <button type="button" class="pc-tg${view === "type" ? " active" : ""}" data-today-view="type">就诊类型</button>
      </span>
    </div>`;
  return nav + (view === "type" ? renderTodayByType(appts) : renderTodayByStatus(appts));
}

function todayApptStage(a) {
  return (typeof tqStage === "function") ? tqStage(a) : 0;
}

// 今日卡片状态标签：复用 patientBadges(映射字段) + 欠费
function todayApptBadges(a) {
  const base = patientBadges({
    last_visit_type: a.visit_type, has_record: a.record_today,
    has_future_appt: a.has_future_appt, has_return_visit: a.has_return_visit,
    has_image: a.has_image, groupname: a.groupname,
  });
  const owe = Number(a.unpaid_amount || 0) > 0 ? `<span class="p-badge b-owe">欠</span>` : "";
  return base + owe;
}

function renderTodayCard(a) {
  const pid = escapeAttr(a.patient_identity || "");
  const t = String(a.start_time || "").slice(11, 16);
  const date = String(a.start_time || "").slice(0, 10);
  return `
    <div class="pc-today-card${patientCenterState.selected === a.patient_identity ? " active" : ""}" role="button" tabindex="0" data-pc-pick="${pid}">
      <div class="ptc-top"><span class="pc-name">${escapeHtml(a.display_name || "(无名)")}</span>
        ${a.doctor_name ? `<span class="ptc-doc">主 ${escapeHtml(a.doctor_name)}</span>` : ""}</div>
      <div class="ptc-badges">${todayApptBadges(a)}</div>
      <div class="ptc-meta">
        ${a.phone ? `<span class="pc-phone">${escapeHtml(a.phone)}</span>` : ""}
        ${date ? `<span class="pc-date">${escapeHtml(date)}${t ? " " + escapeHtml(t) : ""}</span>` : ""}
      </div>
      <div class="ptc-actions">
        <button type="button" class="plain-button ptc-act" data-pc-appt="${pid}" data-pname="${escapeAttr(a.display_name || "")}">预约</button>
        ${a.appointment_id && !["已取消", "3", "已爽约", "爽约"].includes(String(a.status || "")) ? `<button type="button" class="plain-button ptc-act" data-pc-triage="${escapeAttr(a.appointment_id)}">分诊</button>` : ""}
      </div>
    </div>`;
}

// 可折叠分组：空组也显示(如 今日点诊 0)
function renderTodayGroup(label, list) {
  const folded = !!(patientCenterState.todayFold && patientCenterState.todayFold[label]);
  const header = `<button type="button" class="pc-today-grouphd" data-today-fold="${escapeAttr(label)}">
      <span class="pc-today-grouplabel">${escapeHtml(label)} ${list.length}</span>
      <span class="pc-caret">${folded ? "∨" : "∧"}</span></button>`;
  const body = folded ? "" :
    `<div class="pc-today-cards">${list.length ? list.map(renderTodayCard).join("") : '<div class="pc-tree-empty">无</div>'}</div>`;
  return `<div class="pc-today-group">${header}${body}</div>`;
}

function renderTodayByStatus(appts) {
  return renderTodayGroup("预约未到", appts.filter(a => todayApptStage(a) >= 0 && todayApptStage(a) <= 1))
    + renderTodayGroup("就诊中", appts.filter(a => todayApptStage(a) >= 2 && todayApptStage(a) <= 3))
    + renderTodayGroup("已完成", appts.filter(a => todayApptStage(a) >= 4));
}

function renderTodayByType(appts) {
  const dian = appts.filter(a => ["点诊", "新诊"].includes((a.visit_type || "").trim()));
  const other = appts.filter(a => !["初诊", "复诊", "点诊", "新诊"].includes((a.visit_type || "").trim()));
  let html = renderTodayGroup("今日初诊", appts.filter(a => (a.visit_type || "").trim() === "初诊"))
    + renderTodayGroup("今日复诊", appts.filter(a => (a.visit_type || "").trim() === "复诊"))
    + renderTodayGroup("今日点诊", dian);
  if (other.length) html += renderTodayGroup("未分类", other);
  return html;
}

function refreshTodayLeft() {   // 折叠时局部重渲左栏(不重新拉数据)
  const rail = document.getElementById("pcRail");
  if (!rail) return;
  const date = patientCenterState.todayDate || (typeof localDateValue === "function" ? localDateValue() : "");
  rail.innerHTML = renderTodayLeft(todayFilteredAppts(), date);   // #181 保留搜索过滤
  bindPatientCenter(modulePanel);
  bindTodayLeft(modulePanel);
}

function bindTodayLeft(container) {
  if (!container) return;
  container.querySelectorAll("[data-today-view]").forEach(btn => btn.addEventListener("click", () => {
    patientCenterState.todayView = btn.dataset.todayView;
    refreshTodayLeft();
  }));
  container.querySelectorAll("[data-today-fold]").forEach(btn => btn.addEventListener("click", () => {
    const k = btn.dataset.todayFold;
    patientCenterState.todayFold[k] = !patientCenterState.todayFold[k];
    refreshTodayLeft();
  }));
  container.querySelectorAll("[data-today-nav]").forEach(btn => btn.addEventListener("click", () => {
    const d = Number(btn.dataset.todayNav);
    const base = patientCenterState.todayDate || (typeof localDateValue === "function" ? localDateValue() : "");
    patientCenterState.todayDate = (d === 0) ? (typeof localDateValue === "function" ? localDateValue() : base) : shiftDateStr(base, d);
    patientCenterState.selected = "";
    loadPatientModule();
  }));
  // 卡片上「预约」：给该患者排未来预约(复用预约编辑器)
  container.querySelectorAll("[data-pc-appt]").forEach(btn => btn.addEventListener("click", ev => {
    ev.stopPropagation();
    if (typeof openNewAppointment !== "function") return;
    openNewAppointment({});
    if (typeof pickApptPatient === "function") {
      const b = document.createElement("button");
      b.dataset.pid = btn.dataset.pcAppt;
      b.dataset.pname = btn.dataset.pname || "";
      pickApptPatient(b);
    }
  }));
  // 卡片上「分诊」：把已到店患者分给医生/诊室(复用现有分诊弹框)
  container.querySelectorAll("[data-pc-triage]").forEach(btn => btn.addEventListener("click", ev => {
    ev.stopPropagation();
    const aid = btn.dataset.pcTriage;
    const row = (_todayAppts || []).find(a => a.appointment_id === aid) || {};
    if (typeof openTriage === "function") openTriage(aid, row);
  }));
}

function shiftDateStr(dateStr, delta) {
  const d = new Date((dateStr || "") + "T00:00:00");
  if (isNaN(d.getTime())) return dateStr;
  d.setDate(d.getDate() + delta);
  // 本地时区格式化(不能用 toISOString,会按UTC偏移导致跳错天)
  const y = d.getFullYear(), m = String(d.getMonth() + 1).padStart(2, "0"), day = String(d.getDate()).padStart(2, "0");
  return `${y}-${m}-${day}`;
}


// ===== 全部 tab：主从式(左分类树可折叠展开患者 + 右患者主页) =====
async function ensureGroupPatients(key, keyword) {
  if (_groupPatients[key] !== undefined) return _groupPatients[key];
  const params = new URLSearchParams({pagesize: "200"});
  if (key === "__search__") params.set("q", keyword || "");
  else if (key === "__birthday__") {   // 今日生日：今日工作台"今日生日"入口跳进来
    const d = (typeof workDate !== "undefined" && workDate && workDate.value) ? workDate.value
      : (typeof localDateValue === "function" ? localDateValue() : "");
    params.set("birthday_on", d);
  }
  else if (key === "最近患者") params.set("scope", "recent");
  else if (key) params.set("group", key);
  try { _groupPatients[key] = (await (await fetch(`/api/patients?${params.toString()}`)).json()).list || []; }
  catch { _groupPatients[key] = []; }
  indexPatients(_groupPatients[key]);
  return _groupPatients[key];
}

async function loadPatientAllTab(token, keyword) {
  const groups = await ensurePatientGroups();
  if (token !== moduleViewToken) return;
  // 今日工作台"今日生日"入口：context=birthday_today → 右表格按生日筛选
  if (patientModuleContext().patientFilter === "birthday_today" && !keyword) {
    patientCenterState.tableGroup = "__birthday__"; patientCenterState.selected = "";
  }
  // 顶部全局搜索带词进来 → 填进表格搜索 + 回第一页
  if (keyword) { patientCenterState.tableQ = keyword; patientCenterState.tablePage = 1; patientCenterState.selected = ""; }
  modulePanel.innerHTML = `
    <div class="pc-master">
      <div class="pc-side">
        ${patientCenterTabs(patientCenterState)}
        <div id="pcRail" class="pc-rail">${categoryTreeInner(groups)}</div>
      </div>
      <div id="pcDetail" class="pc-detail"></div>
    </div>
  `;
  bindPatientCenter(modulePanel);
  // 树里展开的手动分组,拉它的患者
  if (patientCenterState.expanded) {
    await ensureGroupPatients(patientCenterState.expanded);
    if (token !== moduleViewToken) return;
    refreshAllRail();
  }
  renderAllRight();
}

function categoryTreeInner(groups) {
  if (!groups) return `<div class="pc-tree-empty">分类载入失败</div>`;
  const st = patientCenterState;
  const cat = (key, label, count, opts = {}) => {
    const expandable = !opts.tableOnly && !opts.disabled;
    const on = expandable && st.expanded === key;        // 内联展开(仅手动分组/搜索)
    const activeTable = !st.selected && st.tableGroup === key && !opts.disabled;  // 当前表格分类高亮
    const caret = expandable ? `<span class="pc-caret">${on ? "▾" : "▸"}</span>` : `<span class="pc-caret"></span>`;
    let h = `<button type="button" class="pc-cat${on || activeTable ? " active" : ""}" data-pc-expand="${escapeAttr(key)}"${opts.disabled ? " disabled" : ""}>
      ${caret}<span class="pc-cat-name">${escapeHtml(label)}</span><span class="pc-cat-n">${escapeHtml(formatCount(count))}</span></button>`;
    if (on) h += renderGroupPatients(key);
    return h;
  };
  let html = "";
  if (st.expanded === "__search__") {   // 搜索结果置顶展开
    html += cat("__search__", "搜索结果", (_groupPatients["__search__"] || []).length);
  }
  html += cat("", "全部", groups.total, {tableOnly: true});          // 全部/最近患者：人多,只切表格不内联展开
  html += cat("最近患者", "最近患者", groups.recent, {tableOnly: true});
  html += cat("__sea__", "公海患者", groups.public_sea, {disabled: true});
  html += `<div class="pc-rail-title">手动分组</div>`;
  (groups.groups || []).forEach(g => { html += cat(g.name, g.name, g.count); });  // 手动分组可展开
  return html;
}

function renderGroupPatients(key) {
  const rows = _groupPatients[key];
  if (rows === undefined) return `<div class="pc-tree-loading">载入中…</div>`;
  if (!rows.length) return `<div class="pc-tree-empty">暂无患者</div>`;
  const items = rows.map(r => `
    <button type="button" class="pc-tree-item${patientCenterState.selected === r.patient_identity ? " active" : ""}" data-pc-pick="${escapeAttr(r.patient_identity || "")}">
      <span class="pc-tree-name">${escapeHtml(r.display_name || "(无名)")}</span>
      ${r.chart_no ? `<span class="pc-tree-chart">${escapeHtml(r.chart_no)}</span>` : ""}
    </button>`).join("");
  const more = rows.length >= 200 ? `<div class="pc-tree-more">仅显示前 200，请用搜索精确查找</div>` : "";
  return `<div class="pc-tree-list">${items}${more}</div>`;
}

async function toggleCatExpand(key) {
  // 点分类：右表格切到该组(取消已选患者+回第一页+清勾选)；手动分组在左树内联展开
  patientCenterState.tableGroup = key;
  patientCenterState.selected = "";
  patientCenterState.tablePage = 1;
  _tableSelected.clear();
  const expandable = key && key !== "最近患者";   // 全部("")/最近患者 仅表格,不内联展开
  if (expandable) patientCenterState.expanded = (patientCenterState.expanded === key) ? "" : key;
  else patientCenterState.expanded = "";
  refreshAllRail();
  renderAllRight();           // 右侧重拉表格
  await ensureGroupPatients(key);
  if (patientCenterState.tab !== "all") return;
  refreshAllRail();
}

function refreshAllRail() {
  const rail = document.getElementById("pcRail");
  if (rail) { rail.innerHTML = categoryTreeInner(patientGroupsCache); bindPatientCenter(modulePanel); }
}

// 右内容区：选中患者→内嵌完整档案(复用 workspace)；未选→官方风格患者表格(筛选条+分页+勾选)
function renderAllRight() {
  const detail = document.getElementById("pcDetail");
  if (!detail) return;
  if (patientCenterState.selected) { renderInlineArchive(patientCenterState.selected); return; }
  loadAllTable();
}

async function ensureDoctorOptions() {
  if (_doctorOptions) return _doctorOptions;
  try { _doctorOptions = ((await (await fetch("/api/staff-members?role=" + encodeURIComponent("医生"))).json()).members || []).map(d => d.name).filter(Boolean); }
  catch { _doctorOptions = []; }
  return _doctorOptions;
}

async function loadAllTable() {
  const detail = document.getElementById("pcDetail");
  if (!detail) return;
  detail.classList.remove("pc-detail-archive");
  await ensureDoctorOptions();
  detail.innerHTML = renderTableFilterBar() + `<div id="pcTableBody"><div class="pc-table-loading">载入中…</div></div>`;
  bindTableFilterBar(detail);
  const st = patientCenterState;
  const params = new URLSearchParams({pageno: String(st.tablePage), pagesize: String(st.tablePageSize)});
  if (st.tableQ) params.set("q", st.tableQ);
  const g = st.tableGroup || "";
  if (g === "最近患者") params.set("scope", "recent");
  else if (g === "__birthday__") params.set("birthday_on", (typeof localDateValue === "function" ? localDateValue() : ""));
  else if (g) params.set("group", g);
  if (st.doctorFilter) params.set("doctor", st.doctorFilter);
  if (st.firstDoctorFilter) params.set("first_doctor", st.firstDoctorFilter);
  let data;
  try { data = await (await fetch(`/api/patients?${params.toString()}`)).json(); } catch { data = null; }
  const body = document.getElementById("pcTableBody");
  if (!body) return;
  if (!data) { body.innerHTML = `<div class="module-empty">载入失败</div>`; return; }
  _tableTotal = data.totalcount || 0;
  indexPatients(data.list);
  body.innerHTML = renderPatientTable(data.list || []) + renderTableBottom(data);
  bindTableBody(body);
}

function renderTableFilterBar() {
  const st = patientCenterState;
  const sel = (id, cur) => `<select id="${id}" class="ptf-sel">${
    ['<option value="">全部</option>'].concat((_doctorOptions || []).map(n =>
      `<option value="${escapeAttr(n)}"${n === cur ? " selected" : ""}>${escapeHtml(n)}</option>`)).join("")}</select>`;
  return `
    <div class="ptf-bar">
      <span class="ptf-label">患者信息</span>
      <input id="pcTableSearch" class="ptf-input" value="${escapeAttr(st.tableQ || "")}" placeholder="姓名 / 首拼 / 病历号"
        onkeydown="if(event.key==='Enter') applyTableFilter()">
      <span class="ptf-label">主治医生</span>${sel("pcDoctorSel", st.doctorFilter)}
      <span class="ptf-label">初诊医生</span>${sel("pcFirstDoctorSel", st.firstDoctorFilter)}
      <button type="button" class="btn primary" onclick="applyTableFilter()">查询</button>
      <button type="button" class="btn" onclick="resetTableFilter()">重置</button>
      <span class="ptf-spacer"></span>
      ${canExport() ? '<button type="button" class="btn" onclick="exportPatients()">导出 Excel</button>' : ""}
      <button type="button" class="btn" disabled title="后期">批量操作 ▾</button>
    </div>`;
}

function renderPatientTable(rows) {
  const headChk = `<input type="checkbox" class="pt-ck" id="pcTableAllChk">`;
  const head = `<div class="pt-row pt-head">
    <span>${headChk}</span><span>患者姓名</span><span>病历号</span><span>手机号</span><span>年龄</span><span>性别</span>
    <span>患者标签</span><span>初诊医生</span><span class="pt-opcol">操作</span></div>`;
  if (!rows || !rows.length) return `<div class="pc-table">${head}<div class="module-empty">暂无患者</div></div>`;
  const body = rows.map(r => {
    const age = (typeof _ageFromBirthday === "function") ? _ageFromBirthday(r.birthday) : "";
    const pid = escapeAttr(r.patient_identity || "");
    const checked = _tableSelected.has(r.patient_identity) ? " checked" : "";
    return `<div class="pt-row${checked ? " sel" : ""}" role="button" tabindex="0" data-pc-pick="${pid}">
      <span><input type="checkbox" class="pt-ck pt-rowck" data-pid="${pid}"${checked}></span>
      <span class="pt-name">${escapeHtml(r.display_name || "(无名)")} <span class="pt-badges">${patientBadges(r)}</span></span>
      <span class="pt-mono">${escapeHtml(r.chart_no || "")}</span>
      <span class="pt-mono">${escapeHtml(r.phone || "")}</span>
      <span>${escapeHtml(age)}</span>
      <span>${escapeHtml(r.sex || "")}</span>
      <span></span>
      <span>${escapeHtml(r.first_doctor || r.last_doctor || "")}</span>
      <span class="pt-opcol pt-ops">
        <button type="button" class="pt-op" data-pc-appt data-pid="${pid}" data-pname="${escapeAttr(r.display_name || "")}">预约</button>
        <button type="button" class="pt-op" data-pc-reg data-pid="${pid}" data-pname="${escapeAttr(r.display_name || "")}">挂号</button>
        <button type="button" class="pt-op" data-pc-more data-pid="${pid}" data-pname="${escapeAttr(r.display_name || "")}" data-chart="${escapeAttr(r.chart_no || "")}" data-phone="${escapeAttr(r.phone || "")}">更多</button>
      </span>
    </div>`;
  }).join("");
  return `<div class="pc-table">${head}${body}</div>`;
}

function renderTableBottom(data) {
  const total = data.totalcount || 0;
  const page = Number(data.pageno || 1);
  const size = Number(data.pagesize || patientCenterState.tablePageSize);
  const maxPage = Math.max(1, Math.ceil(total / size));
  const sizeOpt = [50, 100, 200].map(s => `<option value="${s}"${s === size ? " selected" : ""}>${s}</option>`).join("");
  const pages = [];
  for (let p = 1; p <= maxPage; p++) {
    if (p <= 3 || p > maxPage - 1 || Math.abs(p - page) <= 1) pages.push(p);
    else if (pages[pages.length - 1] !== "…") pages.push("…");
  }
  const pgBtns = pages.map(p => p === "…" ? `<b class="pg-dot">…</b>`
    : `<b class="pg-num${p === page ? " on" : ""}" data-pg="${p}">${p}</b>`).join("");
  return `
    <div class="pt-foot">
      <label class="pt-foot-sel"><input type="checkbox" class="pt-ck" id="pcTableAllChk2"> 选中 全部 <span class="pt-red">${escapeHtml(formatCount(total))}</span> 位患者</label>
      <span>共 ${escapeHtml(formatCount(total))} 条</span>
      <span>每页显示 <select id="pcPageSize" class="ptf-sel">${sizeOpt}</select></span>
      <span class="pt-pg">
        <b class="pg-num" data-pg="${Math.max(1, page - 1)}">‹</b>${pgBtns}<b class="pg-num" data-pg="${Math.min(maxPage, page + 1)}">›</b>
      </span>
    </div>`;
}

function applyTableFilter() {
  const inp = document.getElementById("pcTableSearch");
  const doc = document.getElementById("pcDoctorSel");
  const fdoc = document.getElementById("pcFirstDoctorSel");
  patientCenterState.tableQ = inp ? inp.value.trim() : "";
  patientCenterState.doctorFilter = doc ? doc.value : "";
  patientCenterState.firstDoctorFilter = fdoc ? fdoc.value : "";
  patientCenterState.tablePage = 1;
  _tableSelected.clear();
  loadAllTable();
}

function resetTableFilter() {
  patientCenterState.tableQ = ""; patientCenterState.doctorFilter = ""; patientCenterState.firstDoctorFilter = "";
  patientCenterState.tablePage = 1; _tableSelected.clear();
  loadAllTable();
}

function exportPatients() {
  const st = patientCenterState;
  const params = new URLSearchParams();
  if (st.tableQ) params.set("q", st.tableQ);
  const g = st.tableGroup || "";
  if (g === "最近患者") params.set("scope", "recent");
  else if (g === "__birthday__") params.set("birthday_on", (typeof localDateValue === "function" ? localDateValue() : ""));  // #124 导出复用今日生日筛选
  else if (g) params.set("group", g);
  if (st.doctorFilter) params.set("doctor", st.doctorFilter);
  if (st.firstDoctorFilter) params.set("first_doctor", st.firstDoctorFilter);
  window.open(`/api/patients/export?${params.toString()}`, "_blank");
}

function bindTableFilterBar(container) {
  container.querySelectorAll("#pcDoctorSel, #pcFirstDoctorSel").forEach(s => s.addEventListener("change", applyTableFilter));
}

function bindTableBody(body) {
  bindPatientCenter(body);   // 行点击(data-pc-pick)/预约(data-pc-appt)
  // 勾选：全选(表头+底部) + 行选
  const allChks = body.querySelectorAll("#pcTableAllChk, #pcTableAllChk2");
  const rowChks = body.querySelectorAll(".pt-rowck");
  const syncAll = () => { const on = rowChks.length && [...rowChks].every(c => c.checked); allChks.forEach(a => a.checked = on); };
  rowChks.forEach(c => c.addEventListener("click", ev => {
    ev.stopPropagation();
    if (c.checked) _tableSelected.add(c.dataset.pid); else _tableSelected.delete(c.dataset.pid);
    c.closest(".pt-row").classList.toggle("sel", c.checked);
    syncAll();
  }));
  allChks.forEach(a => a.addEventListener("click", ev => {
    ev.stopPropagation();
    const on = a.checked;
    rowChks.forEach(c => { c.checked = on; c.closest(".pt-row").classList.toggle("sel", on); if (on) _tableSelected.add(c.dataset.pid); else _tableSelected.delete(c.dataset.pid); });
    allChks.forEach(x => x.checked = on);
  }));
  syncAll();
  // 挂号(患者已到店，进入今日队列) / 更多(进档案)
  body.querySelectorAll("[data-pc-reg]").forEach(b => b.addEventListener("click", async ev => {
    ev.stopPropagation();
    await checkInPatient({
      patientIdentity: b.dataset.pid || "",
      displayName: b.dataset.pname || "",
      openTriageAfter: false,
      switchToToday: true,
      showSuccess: true,
    });
  }));
  body.querySelectorAll("[data-pc-more]").forEach(b => b.addEventListener("click", ev => {
    ev.stopPropagation(); openMoreMenu(b);   // 更多→下拉菜单(打开档案/合并患者)
  }));
  // 分页 + 每页
  body.querySelectorAll(".pt-pg [data-pg]").forEach(b => b.addEventListener("click", () => {
    patientCenterState.tablePage = Number(b.dataset.pg) || 1; loadAllTable();
  }));
  const sizeSel = body.querySelector("#pcPageSize");
  if (sizeSel) sizeSel.addEventListener("change", () => { patientCenterState.tablePageSize = Number(sizeSel.value) || 100; patientCenterState.tablePage = 1; loadAllTable(); });
}

// ===== 更多菜单 + 合并患者 =====
let _pcMoreEl = null;
function closePcMore() { if (_pcMoreEl) { _pcMoreEl.remove(); _pcMoreEl = null; } }
function openMoreMenu(btn) {
  if (_pcMoreEl) { closePcMore(); return; }
  const d = btn.dataset;
  const items = [
    {label: "打开档案", act: () => selectPatientProfile(d.pid)},
    {label: "合并患者", act: () => openMergeDialog({id: d.pid, name: d.pname || "", chart: d.chart || "", phone: d.phone || ""})},
  ];
  const m = document.createElement("div");
  m.className = "tq-menu";
  m.innerHTML = items.map((it, i) => `<button type="button" class="tq-menu-item" data-i="${i}">${escapeHtml(it.label)}</button>`).join("");
  document.body.appendChild(m);
  const r = btn.getBoundingClientRect();
  m.style.left = Math.min(r.left, window.innerWidth - 150) + "px";
  m.style.top = (r.bottom + 4) + "px";
  m.querySelectorAll("[data-i]").forEach(el => el.addEventListener("click", ev => {
    ev.stopPropagation(); const it = items[Number(el.dataset.i)]; closePcMore(); if (it && it.act) it.act();
  }));
  _pcMoreEl = m;
  setTimeout(() => document.addEventListener("click", closePcMore, {once: true}), 0);
}

let _mergePrimary = null, _mergeSecondary = null;
function openMergeDialog(primary) {
  _mergePrimary = primary; _mergeSecondary = null;
  let m = document.getElementById("mergeModal");
  if (!m) { m = document.createElement("div"); m.id = "mergeModal"; m.className = "modal-backdrop"; document.body.appendChild(m); }
  m.hidden = false;
  const card = p => `<div class="mc-name">${escapeHtml(p.name || "(无名)")}</div><div class="mc-sub">病历号 ${escapeHtml(p.chart || "-")} · ${escapeHtml(p.phone || "无电话")}</div>`;
  m.innerHTML = `
    <section class="appt-modal merge-modal" role="dialog" aria-modal="true" aria-label="合并患者">
      <div class="modal-head"><strong>合并患者</strong><button type="button" class="plain-button" onclick="closeMergeDialog()">×</button></div>
      <div class="appt-body">
        <div class="merge-note">把「次患者」(重复建档)的所有数据合并到「主患者」，次患者将标记为已合并且<b>不可逆</b>。请先核对是同一个人。</div>
        <div class="merge-cols">
          <div class="merge-col"><div class="merge-col-h">主患者（保留）</div><div class="merge-card">${card(primary)}</div></div>
          <div class="merge-col"><div class="merge-col-h">次患者（合并掉）</div>
            <input id="mergeSearch" class="ord-input" placeholder="搜索重复患者 姓名/手机/病历号" autocomplete="off">
            <div id="mergeResults" class="appt-patient-results" hidden></div>
            <div id="mergePicked" class="merge-card" hidden></div></div>
        </div>
        <label class="appt-field">合并原因 <input id="mergeReason" class="ord-input" placeholder="如：重复建档"></label>
      </div>
      <div class="modal-actions"><span id="mergeStatus"></span>
        <button type="button" class="tooth-confirm-btn" onclick="submitMerge()">确定合并</button>
        <button type="button" class="plain-button" onclick="closeMergeDialog()">取消</button></div>
    </section>`;
  const s = document.getElementById("mergeSearch");
  if (s) { s.oninput = () => searchMergeSecondary(s.value); s.focus(); }
}
function closeMergeDialog() { const m = document.getElementById("mergeModal"); if (m) m.hidden = true; }
async function searchMergeSecondary(kw) {
  const res = document.getElementById("mergeResults"); if (!res) return;
  kw = String(kw || "").trim();
  if (!kw) { res.hidden = true; res.innerHTML = ""; return; }
  let data; try { data = await (await fetch(`/api/patients?q=${encodeURIComponent(kw)}&pagesize=8`)).json(); } catch { return; }
  const list = (data.list || []).filter(p => p.patient_identity !== _mergePrimary.id);
  res.hidden = false;
  res.innerHTML = list.length ? list.map(p =>
    `<button type="button" class="appt-patient-opt" data-pid="${escapeAttr(p.patient_identity)}" data-pname="${escapeAttr(p.display_name || "")}" data-chart="${escapeAttr(p.chart_no || "")}" data-phone="${escapeAttr(p.phone || "")}">
       ${escapeHtml(p.display_name || "(无名)")} <small>病历 ${escapeHtml(p.chart_no || "-")} · ${escapeHtml(p.phone || "")}</small></button>`).join("")
    : `<div class="appt-patient-empty">无匹配患者</div>`;
  res.querySelectorAll("[data-pid]").forEach(b => b.addEventListener("click", () => pickMergeSecondary(b)));
}
function pickMergeSecondary(el) {
  _mergeSecondary = {id: el.dataset.pid, name: el.dataset.pname, chart: el.dataset.chart, phone: el.dataset.phone};
  const res = document.getElementById("mergeResults"); if (res) { res.hidden = true; res.innerHTML = ""; }
  const s = document.getElementById("mergeSearch"); if (s) s.value = "";
  const pk = document.getElementById("mergePicked");
  if (pk) { pk.hidden = false; pk.innerHTML = `<div class="mc-name">${escapeHtml(_mergeSecondary.name || "(无名)")}</div><div class="mc-sub">病历号 ${escapeHtml(_mergeSecondary.chart || "-")} · ${escapeHtml(_mergeSecondary.phone || "无电话")}</div>`; }
}
async function submitMerge() {
  const st = document.getElementById("mergeStatus");
  if (!_mergeSecondary) { if (st) st.textContent = "请选要合并的次患者"; return; }
  if (_mergeSecondary.id === _mergePrimary.id) { if (st) st.textContent = "不能与自己合并"; return; }
  const reason = ((document.getElementById("mergeReason") || {}).value || "").trim();
  if (!reason) { if (st) st.textContent = "请填合并原因"; return; }
  if (!window.confirm(`确定把「${_mergeSecondary.name}」合并到「${_mergePrimary.name}」？此操作不可逆。`)) return;
  if (st) st.textContent = "合并中...";
  let res;
  try { res = await fetch(`/api/patients/${encodeURIComponent(_mergePrimary.id)}/merge`, {method: "POST", headers: {"Content-Type": "application/json"}, body: JSON.stringify({secondary: _mergeSecondary.id, reason})}); }
  catch { if (st) st.textContent = "合并失败（网络异常）"; return; }
  if (!res.ok) { const m = await res.json().catch(() => ({})); if (st) st.textContent = "合并失败：" + (m.detail || res.status); return; }
  closeMergeDialog();
  patientGroupsCache = null; _tableSelected.clear();   // 刷新分类计数 + 清勾选
  loadPatientModule();
}
window.closeMergeDialog = closeMergeDialog; window.submitMerge = submitMerge;

function selectPatientProfile(pid) {
  patientCenterState.selected = pid;
  const pool = _groupPatients[patientCenterState.expanded] || _groupPatients[patientCenterState.tableGroup || ""] || [];
  const row = _patientRowIndex[pid] || pool.find(x => x.patient_identity === pid) || {patient_identity: pid};
  pushRecentPatient(row);  // P5 最近点击历史(从索引取真实姓名,避免"(无名)")
  // 只高亮左栏选中项(不重建左栏——否则今日/最近的列表会被换成全部分类树,像"跳到全部")
  if (modulePanel) modulePanel.querySelectorAll("[data-pc-pick]").forEach(el => el.classList.toggle("active", el.dataset.pcPick === pid));
  renderInlineArchive(pid);  // 右栏渲染该患者完整档案
}

// P5：最近点击过的患者(localStorage,倒序,上限50)。存 id+姓名+病历号+电话 供"最近"tab 直接渲染。
const RECENT_PATIENTS_KEY = "dl_recent_patients";
function pushRecentPatient(row) {
  const pid = row && row.patient_identity;
  if (!pid) return;
  try {
    let list = JSON.parse(localStorage.getItem(RECENT_PATIENTS_KEY) || "[]");
    if (!Array.isArray(list)) list = [];
    const prev = list.find(x => x && x.id === pid) || {};
    list = list.filter(x => x && x.id !== pid);
    // row 可能是患者行(last_*)或今日预约(visit_type/doctor_name/start_time)，归一成今日卡片字段
    list.unshift({
      id: pid,
      name: row.display_name || prev.name || "",
      chart: row.chart_no || prev.chart || "",
      phone: row.phone || prev.phone || "",
      sex: row.sex || prev.sex || "",
      doctor: row.last_doctor || row.doctor_name || prev.doctor || "",
      visit_type: row.last_visit_type || row.visit_type || prev.visit_type || "",
      visit_date: row.last_visit || row.start_time || prev.visit_date || "",
      has_record: (row.has_record != null ? row.has_record : (row.record_today != null ? row.record_today : prev.has_record)) || 0,
      has_future_appt: (row.has_future_appt != null ? row.has_future_appt : prev.has_future_appt) || 0,
      has_return_visit: (row.has_return_visit != null ? row.has_return_visit : prev.has_return_visit) || 0,
      has_image: (row.has_image != null ? row.has_image : prev.has_image) || 0,
      groupname: row.groupname || prev.groupname || "",
      t: new Date().getTime(),
    });
    if (list.length > 50) list = list.slice(0, 50);
    localStorage.setItem(RECENT_PATIENTS_KEY, JSON.stringify(list));
  } catch { /* localStorage 不可用则忽略 */ }
}
function getRecentPatients() {
  try { const l = JSON.parse(localStorage.getItem(RECENT_PATIENTS_KEY) || "[]"); return Array.isArray(l) ? l : []; }
  catch { return []; }
}

// 右栏内嵌完整档案：克隆全屏页的 workspace-layout(剥 id 防冲突) → 设为 host → 复用 workspace 渲染器
function renderInlineArchive(pid) {
  const detail = document.getElementById("pcDetail");
  if (!detail) return;
  detail.classList.add("pc-detail-archive");
  let layout = detail.querySelector(".workspace-layout");
  if (!layout) {
    const src = document.querySelector("#patientWorkspacePage .workspace-layout");
    if (!src) return;
    detail.innerHTML = "";
    layout = src.cloneNode(true);
    layout.querySelectorAll("[id]").forEach(el => el.removeAttribute("id"));  // 防与全屏页 ID 冲突
    detail.appendChild(layout);
  }
  if (typeof showPatientWorkspacePage === "function") showPatientWorkspacePage(pid, "profile", detail);
}

// 卡片状态色块：初/复/史/约/访/影 + 分类(植/畸/修)，按后端 badge 字段
function patientBadges(row) {
  const out = [];
  const vt = (row.last_visit_type || "").trim();
  if (vt === "初诊") out.push(["初", "b-first"]);
  else if (vt === "复诊") out.push(["复", "b-revisit"]);
  else if (vt) out.push([vt.charAt(0), "b-visit"]);
  if (row.has_record) out.push(["史", "b-history"]);
  if (row.has_future_appt) out.push(["约", "b-appt"]);
  if (row.has_return_visit) out.push(["访", "b-return"]);
  if (row.has_image) out.push(["影", "b-image"]);
  const cat = { "种植牙": ["植", "b-implant"], "正畸患者": ["畸", "b-ortho"], "桩冠修复": ["修", "b-restore"] };
  String(row.groupname || "").split(",").map(s => s.trim()).forEach(g => {
    if (cat[g]) out.push(cat[g]);
  });
  return out.map(([t, c]) => `<span class="p-badge ${c}">${escapeHtml(t)}</span>`).join("");
}

function setPatientCenterTab(tab) {
  patientCenterState.tab = tab;
  patientCenterState.group = "";
  patientCenterState.expanded = "";
  patientCenterState.selected = "";
  patientCenterState.tableGroup = "";
  modulePage = 1;
  loadPatientModule();
}

// 事件委托(对 modulePanel/局部容器幂等)：data-bound 守卫防重复绑定
function bindPatientCenter(container) {
  if (!container) return;
  const once = (el, fn) => { if (el.dataset.pcBound) return; el.dataset.pcBound = "1"; el.addEventListener("click", fn); };
  container.querySelectorAll("[data-pc-tab]").forEach(btn => once(btn, () => setPatientCenterTab(btn.dataset.pcTab)));
  container.querySelectorAll("[data-pc-expand]").forEach(btn => { if (btn.disabled) return; once(btn, () => toggleCatExpand(btn.dataset.pcExpand || "")); });
  container.querySelectorAll("[data-pc-pick]").forEach(btn => {
    if (btn.dataset.pcBound) return; btn.dataset.pcBound = "1";
    btn.addEventListener("click", () => selectPatientProfile(btn.dataset.pcPick));
    btn.addEventListener("keydown", e => {   // #116 表格行是div,补键盘 Enter/Space 打开
      if (e.key === "Enter" || e.key === " ") { e.preventDefault(); selectPatientProfile(btn.dataset.pcPick); }
    });
  });
  container.querySelectorAll("[data-pc-open]").forEach(btn => once(btn, () => openPatientWorkspace(btn.dataset.pcOpen)));
  container.querySelectorAll("[data-pc-appt]").forEach(btn => once(btn, event => {   // #119 含表格 .pt-op,不再限 .pc-op
    event.stopPropagation();
    if (typeof openNewAppointment === "function") openNewAppointment({});
    if (typeof pickApptPatient === "function") pickApptPatient(btn);  // 预选该患者(读 data-pid/pname,防注入)
  }));
}

function patientCenterTabs(state) {
  const tab = (key, label) =>
    `<button type="button" class="pc-tab${state.tab === key ? " active" : ""}" data-pc-tab="${key}">
       ${label}
     </button>`;
  return `
    <nav class="patient-center-tabs" aria-label="患者分组">
      ${tab("today", "今日")}
      ${tab("all", "全部")}
      ${tab("recent", "最近")}
    </nav>
  `;
}

function patientModuleContext() {
  return moduleContext || {};
}


function renderPatientModuleRow(row) {
  const patientId = row.patient_identity || "";
  return `
    <div class="module-row" role="button" tabindex="0" data-patient-id="${escapeAttr(patientId)}">
      <span>${escapeHtml(row.display_name || "(无名)")}</span>
      <span>${escapeHtml(row.medical_record_no || row.source_customer_id || "")}</span>
      <span>${escapeHtml(row.phone || "")}</span>
      <span>${escapeHtml(row.age || "")}</span>
      <span>${escapeHtml(row.gender || "")}</span>
      <span>${escapeHtml(row.patient_tag || "")}</span>
      <span>${escapeHtml(row.first_doctor || "")}</span>
      <span>${escapeHtml(row.revisit_doctor || "")}</span>
      <span>${escapeHtml(row.first_visit_at || "")}</span>
      <span>${escapeHtml(row.revisit_at || "")}</span>
      <span>${escapeHtml(row.source || "")}</span>
      <span>${escapeHtml(row.visit_item || "")}</span>
      <span>${escapeHtml(formatMoney(row.total_spent || ""))}</span>
      <span>${escapeHtml(row.updated_at || "")}</span>
      ${moduleRowAction(patientId)}
    </div>
  `;
}

function renderCompactPatientModuleRow(row) {
  const patientId = row.patient_identity || "";
  return `
    <div class="module-row" role="button" tabindex="0" data-patient-id="${escapeAttr(patientId)}">
      <span>${escapeHtml(row.display_name || "(无名)")}</span>
      <span>${escapeHtml(row.phone || "")}</span>
      <span>${escapeHtml(row.source_customer_id || "")}</span>
      <span>${escapeHtml(row.updated_at || "")}</span>
      <span>${escapeHtml(patientId)}</span>
      ${moduleRowAction(patientId)}
    </div>
  `;
}

function moduleRowAction(patientId) {
  return `
    <button type="button" class="module-row-action" data-patient-id="${escapeAttr(patientId || "")}">
      查看详情
    </button>
  `;
}


Object.assign(window, {
  loadPatientModule,
  renderPatientModuleRow,
  moduleRowAction,
});
