const list = document.getElementById("patientList");
const q = document.getElementById("q");
// 顶栏日期框已精简掉；workDate 改为内存中的"当前工作日期"对象(预约模块日期导航/明日预约用)。
// 保留 .value 接口，原有几十处 workDate.value 读写无需改动。
const workDate = { value: localDateValue() };
const statusText = document.getElementById("statusText");
const todayWorkPanel = document.getElementById("todayWorkPanel");
const modulePanel = document.getElementById("modulePanel");
const patientWorkspace = document.getElementById("patientWorkspace");
const auditPanel = document.getElementById("auditPanel");
const workspaceTitle = document.getElementById("workspaceTitle");
const workspaceSubtitle = document.getElementById("workspaceSubtitle");

let activePatientId = "";
let activeModuleView = "today";
let _authenticatedBusinessReady = false;
let _workspaceSwitchRevision = 0;
let _committedPatientWorkspaceHash = "";
let _committedWorkspaceHistoryState = history && history.state && typeof history.state === "object"
  ? {...history.state}
  : (history ? history.state : null);
let _approvedPatientHashExit = null;
let modulePage = 1;
let moduleSearchSeed = "";
let moduleContext = {};
let todayQueueFilter = "all";
let todayFlowFilter = "all";
let todayQueueSearch = "";
let todayQueueDoctor = "";   // 分诊医生筛选(空=全部)
let latestTodayWorkData = null;

const workspaceViews = {
  today: ["今日工作台", ""],
  history: ["历史搜索", "按姓名、电话、患者ID 或客户ID 检索全部本地患者"],
  patients: ["患者管理", "搜索患者，查看病历、预约、回访、收费和本地备注"],
  appointments: ["预约管理", "先查看患者预约，后续接入独立预约列表"],
  // #617:收费管理顶层导航按钮已撤下(前台基本用不上,实收走患者详情页)，
  // 但今日工作台"今日结算/今日待缴费"仍靠 billingFilter 深链进这个视图，view 本身留着。
  billing: ["收费管理", "先查看患者收费单和付款记录，保持本地只读金额链路"],
  "return-visits": ["客户通", "今日回访 / 沟通 / 录音总览，管理全店客户联系"],
  "ai-drafts": ["AI 草稿箱", "审核 AI 语音生成的病历草稿，确认/修改/废弃，并认领无主草稿"],
  sterilization: ["消毒管理", "器械库主数据：增删改查器械与分类，停用保留追溯（送消/消毒/发放为后续）"],
  warehouse: ["库存管理", "进销存：库存查询与低库存预警、物品档案、供应商、出入库/盘点单"],
  config: ["配置管理", "诊所人员、角色权限与系统设置等本地配置"],
  "data-reports": ["数据报表", "医生/护士统计·收银查询·收入统计·日结报表·门诊日志·对账日历等店内统计"],
  inspection: ["工作检查", "逐条核对今日所有预约/处置/账单/回访的完整性和一致性"],
};

// 本会话内是否通过 openPatientWorkspace 压入过 hash：是则上一条历史就是本应用页面，关闭时可安全 history.back()。
let workspaceHashPushed = false;

function openPatientWorkspace(identity, tab = "") {
  if (!identity) return Promise.resolve(false);
  const next = `#patient/${encodeURIComponent(identity)}${tab ? `/${encodeURIComponent(tab)}` : ""}`;
  return _runWorkspaceTransition(() => {
    if (location.hash !== next) {
      _pushWorkspaceHash(next, _committedPatientWorkspaceHash);
    }
    _rememberCommittedWorkspaceEntry(next);
    _showPatientWorkspaceRoute({identity, tab});
  });
}

function closePatientWorkspace() {
  if (_approvedPatientHashExit) return Promise.resolve(false);
  const route = parsePatientWorkspaceHash();
  if (!route) return Promise.resolve(false);
  const fromHash = location.hash;
  return _runWorkspaceTransition(revision => {
    if (workspaceHashPushed && history.length > 1) {
      const state = history && history.state && typeof history.state === "object"
        ? history.state
        : {};
      const targetHash = typeof state.__workspaceReturnHash === "string"
        ? state.__workspaceReturnHash
        : "";
      _approvedPatientHashExit = {revision, fromHash, targetHash};
      history.back();
      return;
    }
    // 直接深链没有可安全返回的应用内历史：替换当前 hash，避免清 hash 又压栈后 Back 重开患者页。
    const fallback = _patientWorkspaceFallbackEntry();
    workspaceHashPushed = false;
    _replaceWorkspaceHash(fallback.hash, fallback.state);
    const fallbackRoute = parsePatientWorkspaceHash(fallback.hash);
    _commitPatientWorkspaceHash(fallbackRoute, fallbackRoute ? fallback.hash : "");
  });
}

function parsePatientWorkspaceHash(hash = location.hash) {
  const match = /^#patient\/([^/]+)(?:\/([^/]+))?$/.exec(hash || "");
  if (!match) return null;
  try {
    return {
      identity: decodeURIComponent(match[1]),
      tab: match[2] ? decodeURIComponent(match[2]) : "",
    };
  } catch {
    // 非法编码（如 #patient/%zz）：decodeURIComponent 抛 URIError，按无效路由处理。
    return null;
  }
}

function markPatientWorkspaceHashCommitted(hash) {
  const page = document.getElementById("patientWorkspacePage");
  if (!page || page.hidden || !parsePatientWorkspaceHash(hash)) return false;
  _rememberCommittedWorkspaceEntry(hash);
  return true;
}

function _showPatientWorkspaceRoute(route) {
  if (todayWorkPanel) todayWorkPanel.hidden = true;
  if (modulePanel) modulePanel.hidden = true;
  if (patientWorkspace) patientWorkspace.hidden = true;
  showPatientWorkspacePage(route.identity, route.tab);
}

function _copyWorkspaceHistoryState(source) {
  if (!source || typeof source !== "object") return source || null;
  const copied = {...source};
  if (Array.isArray(source.__workspaceReturnHashes)) {
    copied.__workspaceReturnHashes = [...source.__workspaceReturnHashes];
  }
  return copied;
}

function _rememberCommittedWorkspaceEntry(hash) {
  _committedPatientWorkspaceHash = hash;
  _committedWorkspaceHistoryState = _copyWorkspaceHistoryState(history ? history.state : null);
}

function _historyStateWithoutPatientWorkspace(source = history ? history.state : null) {
  const current = source && typeof source === "object"
    ? {...source}
    : {};
  delete current.__patientWorkspacePushed;
  delete current.__workspaceReturnHash;
  delete current.__workspaceReturnHashes;
  return current;
}

function _pushWorkspaceHash(hash, returnHash) {
  try {
    if (history && typeof history.pushState === "function") {
      const currentState = _copyWorkspaceHistoryState(history.state);
      const returnHashes = currentState && Array.isArray(currentState.__workspaceReturnHashes)
        ? [...currentState.__workspaceReturnHashes]
        : [];
      returnHashes.push(returnHash);
      const state = {
        ..._historyStateWithoutPatientWorkspace(currentState),
        __patientWorkspacePushed: true,
        __workspaceReturnHash: returnHash,
        __workspaceReturnHashes: returnHashes,
      };
      history.pushState(state, "", hash);
      workspaceHashPushed = true;
      return;
    }
  } catch {
    // 旧浏览器再退回 hash 赋值；hashchange 会命中已提交 hash 并成为空操作。
  }
  workspaceHashPushed = true;
  location.hash = hash;
}

function _replaceWorkspaceHash(hash, state = undefined) {
  if (location.hash === hash && state === undefined) return;
  try {
    if (history && typeof history.replaceState === "function") {
      const base = `${location.pathname || ""}${location.search || ""}`;
      history.replaceState(state === undefined ? (history.state || null) : state, "", `${base}${hash}` || hash);
      return;
    }
  } catch {
    // replaceState 不可用时再退回 hash 赋值；已提交 hash 会让随后的 hashchange 成为空操作。
  }
  location.hash = hash;
}

function _patientWorkspaceFallbackEntry() {
  const committedState = _copyWorkspaceHistoryState(_committedWorkspaceHistoryState);
  const returnHashes = committedState && Array.isArray(committedState.__workspaceReturnHashes)
    ? [...committedState.__workspaceReturnHashes]
    : [];
  let hash = returnHashes.length
    ? returnHashes.pop()
    : (committedState && typeof committedState.__workspaceReturnHash === "string"
      ? committedState.__workspaceReturnHash
      : "");
  if (!parsePatientWorkspaceHash(hash)) hash = "";
  const state = _historyStateWithoutPatientWorkspace(committedState);
  if (hash) {
    state.__workspaceReturnHash = returnHashes.length ? returnHashes[returnHashes.length - 1] : "";
    state.__workspaceReturnHashes = returnHashes;
  }
  return {hash, state};
}

function _commitPatientWorkspaceHash(route, targetHash) {
  if (route) {
    _rememberCommittedWorkspaceEntry(targetHash);
    _showPatientWorkspaceRoute(route);
    return;
  }
  _committedPatientWorkspaceHash = "";
  _committedWorkspaceHistoryState = _copyWorkspaceHistoryState(history ? history.state : null);
  workspaceHashPushed = false;
  _commitWorkspaceView(activeModuleView);
  // 从患者页返回今日工作台:重拉数据,让处置/收费/病历/回访的"已做"高亮即时刷新
  if (activeModuleView === "today" && typeof loadTodayWork === "function") loadTodayWork();
}

function _hashFromUrl(url) {
  const value = String(url || "");
  const index = value.indexOf("#");
  return index >= 0 ? value.slice(index) : "";
}

function handlePatientWorkspaceHash(event = null) {
  const observedHash = event && typeof event.newURL === "string"
    ? _hashFromUrl(event.newURL)
    : location.hash;
  if (observedHash !== location.hash) return Promise.resolve(false);
  const route = parsePatientWorkspaceHash(observedHash);
  const targetHash = route ? observedHash : "";
  let previousHash = _committedPatientWorkspaceHash;
  const previousState = _copyWorkspaceHistoryState(_committedWorkspaceHistoryState);
  const observedState = _copyWorkspaceHistoryState(history ? history.state : null);
  // hashchange 发生时浏览器历史索引已经移动；后续 close 必须以当前历史项标记为准，不能沿用旧项的 true。
  workspaceHashPushed = Boolean(
    history && history.state && history.state.__patientWorkspacePushed === true,
  );
  if (targetHash === _committedPatientWorkspaceHash) {
    _approvedPatientHashExit = null;
    _rememberCommittedWorkspaceEntry(targetHash);
    return Promise.resolve(true);
  }

  const approvedExit = _approvedPatientHashExit;
  if (approvedExit) {
    _approvedPatientHashExit = null;
    if (approvedExit.revision !== _workspaceSwitchRevision
        || approvedExit.fromHash !== previousHash
        || approvedExit.targetHash !== targetHash) {
      workspaceHashPushed = false;
      _replaceWorkspaceHash(previousHash, previousState);
      return Promise.resolve(false);
    }
    _commitPatientWorkspaceHash(route, targetHash);
    return Promise.resolve(true);
  }

  if (activeModuleView === "config") {
    // 浏览器已先改地址栏；守卫等待期间恢复已显示 hash，业务 DOM 保持原状。
    _replaceWorkspaceHash(previousHash, previousState);
    // 当前历史索引已经被外部 Back/Forward 改动，取消后不能再假定上一项就是逻辑父页面。
    workspaceHashPushed = false;
  }
  return _runWorkspaceTransition(
    () => {
      _replaceWorkspaceHash(targetHash, observedState);
      workspaceHashPushed = Boolean(
        observedState && observedState.__patientWorkspacePushed === true,
      );
      _commitPatientWorkspaceHash(route, targetHash);
    },
    () => {
      _replaceWorkspaceHash(previousHash, previousState);
      workspaceHashPushed = false;
    },
  );
}

function switchWorkspaceView(view, searchText = "", options = {}) {
  return _runWorkspaceTransition(() => _commitWorkspaceView(view, searchText, options));
}

function _runWorkspaceTransition(commit, reject = null) {
  if (!_authenticatedBusinessReady) return Promise.resolve(false);
  const switchRevision = ++_workspaceSwitchRevision;
  const finish = allowed => {
    if (switchRevision !== _workspaceSwitchRevision || allowed !== true) {
      if (switchRevision === _workspaceSwitchRevision && typeof reject === "function") reject();
      return false;
    }
    commit(switchRevision);
    return true;
  };
  if (activeModuleView === "config") {
    return _requestLeaveConfigForWorkspaceSwitch().then(finish, () => {
      if (switchRevision === _workspaceSwitchRevision && typeof reject === "function") reject();
      return false;
    });
  }
  if (activeModuleView === "return-visits") {
    return _requestLeaveCustomerHubForWorkspaceSwitch().then(finish, () => {
      if (switchRevision === _workspaceSwitchRevision && typeof reject === "function") reject();
      return false;
    });
  }
  try {
    return Promise.resolve(finish(true));
  } catch {
    return Promise.resolve(false);
  }
}

async function _requestLeaveConfigForWorkspaceSwitch() {
  if (typeof requestLeaveConfigModule !== "function") return false;
  try {
    return (await requestLeaveConfigModule()) === true;
  } catch {
    return false;
  }
}

async function _requestLeaveCustomerHubForWorkspaceSwitch() {
  if (typeof requestLeaveReturnVisitModule !== "function") return false;
  try {
    return (await requestLeaveReturnVisitModule()) === true;
  } catch {
    return false;
  }
}

function _commitWorkspaceView(view, searchText = "", options = {}) {
  _committedPatientWorkspaceHash = "";
  workspaceHashPushed = false;
  hidePatientWorkspacePage();
  if (parsePatientWorkspaceHash()) {
    _replaceWorkspaceHash("", _historyStateWithoutPatientWorkspace());
  }
  _committedWorkspaceHistoryState = _copyWorkspaceHistoryState(history ? history.state : null);
  activeModuleView = view;
  modulePage = 1;
  moduleSearchSeed = searchText;
  moduleContext = options.context || {};
  if (q && searchText) q.value = searchText;
  if (Number.isFinite(options.dateOffset)) setWorkDateForOffset(options.dateOffset);
  const [title, subtitle] = workspaceViews[view] || workspaceViews.today;
  if (workspaceTitle) workspaceTitle.textContent = title;
  if (workspaceSubtitle) workspaceSubtitle.textContent = subtitle;
  document.querySelectorAll(".nav-item[data-workspace-view]").forEach(button => {
    button.classList.toggle("active", button.dataset.workspaceView === view);
  });
  syncTodayActions(view);
  const showModule = view === "history" || view === "patients" || view === "appointments" || view === "return-visits" || view === "billing" || view === "sterilization" || view === "warehouse" || view === "config" || view === "data-reports";
  if (modulePanel) modulePanel.hidden = !showModule;
  const aiDraftsPanel = document.getElementById("aiDraftsPanel");
  if (aiDraftsPanel) aiDraftsPanel.hidden = view !== "ai-drafts";
  if (patientWorkspace) patientWorkspace.hidden = true;
  if (todayWorkPanel) todayWorkPanel.hidden = view !== "today" && view !== "inspection";
  // 返回今日工作台必须重载:否则面板停在上次内容(如待跟进派生看板)回不到候诊队列
  if (view === "today" && typeof loadTodayWork === "function") loadTodayWork();
  if (view === "inspection" && typeof loadTodayInspection === "function") loadTodayInspection();
  if (view === "history" || view === "patients") loadPatientModule();
  if (view === "appointments") loadAppointmentModule();
  if (view === "return-visits") loadReturnVisitModule();
  if (view === "billing") loadBillingModule();
  if (view === "ai-drafts") loadAiDrafts();
  if (view === "sterilization" && typeof loadSterilizationModule === "function") loadSterilizationModule();
  if (view === "warehouse" && typeof loadWarehouseModule === "function") loadWarehouseModule();
  if (view === "config" && typeof loadConfigModule === "function") loadConfigModule();
  if (view === "data-reports") {
    // 数据报表=顶层入口,直接渲染店内统计报表模块(复用 loadReportsModule,它填充 #cfgBody)
    if (modulePanel) modulePanel.innerHTML = '<div id="cfgBody" class="module-card"></div>';
    if (typeof loadReportsModule === "function") loadReportsModule();
  }
}

function setWorkDateForOffset(offsetDays) {
  if (!workDate) return;
  const date = dateFromWorkValue(workDate.value);
  date.setDate(date.getDate() + Number(offsetDays || 0));
  workDate.value = localDateValue(date);
}

async function loadStats() {
  const statsPanel = document.getElementById("statsPanel");
  if (!statsPanel) return;
  statsPanel.textContent = "统计载入中...";
  try {
    const res = await fetch("/api/stats");
    if (!res.ok) {
      statsPanel.textContent = "统计载入失败";
      return;
    }
    const data = await res.json();
    const counts = data.counts || {};
    const money = data.money || {};
    const items = [
      ["患者", counts.patients],
      ["病历", counts.medical_records],
      ["预约", counts.appointments],
      ["回访", counts.return_visits],
    ];
    // #496：财务/审计段仅在后端按权限带出时才显示,无 billing.view/audit.view 的角色不显示占位垃圾行
    if (counts.bills !== undefined) items.push(["收费单", counts.bills], ["付款", counts.payments]);
    if (counts.audit_logs !== undefined) items.push(["审计", counts.audit_logs]);
    if (money.bill_total_fee !== undefined) {
      items.push(["收费总额", formatMoney(money.bill_total_fee)], ["付款总额", formatMoney(money.payment_amount)]);
    }
    statsPanel.innerHTML = items.map(([label, value]) => `
      <div class="stat-item">
        <span class="stat-label">${label}</span>
        <strong>${escapeHtml(formatCount(value))}</strong>
      </div>
    `).join("");
  } catch {
    // 网络异常：避免面板永卡"载入中"和 unhandled rejection
    statsPanel.textContent = "统计载入失败";
  }
}

async function loadAuditLogs() {
  if (!auditPanel) return;
  try {
    const res = await fetch("/api/audit-logs?pagesize=8");
    if (res.status === 403) {
      // #464：无 audit.view 权限的角色,审计概览面板整块隐藏(而非报"载入失败")
      auditPanel.hidden = true;
      return;
    }
    if (!res.ok) {
      auditPanel.textContent = "审计记录载入失败";
      return;
    }
    const data = await res.json();
    const rows = data.list || [];
    auditPanel.innerHTML = `
      <div class="audit-head">
        <strong>审计记录</strong>
        <span>${escapeHtml(formatCount(data.totalcount))} 条</span>
      </div>
      <div class="audit-list">
        ${rows.length ? rows.map(renderAuditRow).join("") : '<span class="empty">暂无本地编辑记录</span>'}
      </div>
    `;
  } catch {
    // 网络异常：避免面板永卡和 unhandled rejection
    auditPanel.textContent = "审计记录载入失败";
  }
}

function renderAuditRow(row) {
  return `
    <div class="audit-row">
      <span>${escapeHtml(row.action || "")}</span>
      <span>${escapeHtml(row.entity_type || "")}</span>
      <span>${escapeHtml(row.operator || "")}</span>
      <span>${escapeHtml(row.created_at || "")}</span>
    </div>
  `;
}

async function searchPatients() {
  statusText.textContent = "搜索中...";
  let data;
  try {
    const res = await fetch(`/api/patients?q=${encodeURIComponent(q.value)}&pagesize=80`);
    if (!res.ok) {
      statusText.textContent = "搜索失败";
      return;
    }
    data = await res.json();
  } catch {
    // 网络异常：避免状态栏永卡"搜索中"和 unhandled rejection
    statusText.textContent = "搜索失败";
    return;
  }
  statusText.textContent = `${data.totalcount} 条`;
  list.innerHTML = data.list.map(patientListItem).join("") || `<div class="patient-item empty">没有结果</div>`;
  document.querySelectorAll(".patient-item[data-id]").forEach(el => {
    el.addEventListener("click", () => openPatientWorkspace(el.dataset.id));
  });
}

function patientListItem(p) {
  const active = p.patient_identity === activePatientId ? " active" : "";
  const sub = [p.phone, p.source_customer_id, p.patient_identity].filter(Boolean).join(" | ");
  return `
    <div class="patient-item${active}" data-id="${escapeAttr(p.patient_identity)}">
      <div class="patient-name">${escapeHtml(p.display_name || "(无名)")}</div>
      <div class="patient-sub">${escapeHtml(sub)}</div>
    </div>
  `;
}

function refreshWorkDateViews() {
  modulePage = 1;
  loadTodayWork();
  const activeView = document.querySelector(".nav-item.active[data-workspace-view]")?.dataset.workspaceView;
  if (activeView === "appointments") loadAppointmentModule();
  if (activeView === "return-visits") loadReturnVisitModule();
  if (activeView === "billing") loadBillingModule();
}

// 纯日期层:只更新共享 workDate 并返回新日期,不渲染任何视图。
// 工作检查等自管刷新的视图直接用它,避免被 refreshWorkDateViews 的今日工作台覆盖(GD-04)。
function shiftWorkDate(offsetDays) {
  if (!workDate) return null;
  const date = offsetDays === 0 ? bjToday() : dateFromWorkValue(workDate.value);
  date.setDate(date.getDate() + Number(offsetDays || 0));
  workDate.value = localDateValue(date);
  return date;
}

function changeWorkDate(offsetDays) {
  if (shiftWorkDate(offsetDays) === null) return;
  refreshWorkDateViews();
}

// 顶栏搜索：已在患者/历史视图就原地重搜，否则切到「历史搜索」显示结果
// (结果渲染到可见的 modulePanel，且 /api/patients 已支持拼音/全拼/首字母)
function runTopSearch() {
  if (!_authenticatedBusinessReady) return;
  const query = q.value.trim();
  const activeView = document.querySelector(".nav-item.active[data-workspace-view]")?.dataset.workspaceView;
  // 顶栏的词权威：强制同步到模块搜索框+seed，避免被模块框旧值盖掉(拼音搜不到的根因)
  moduleSearchSeed = query;
  const mInput = document.getElementById("moduleSearchInput");
  if (mInput) mInput.value = query;
  if (activeView === "patients" || activeView === "history") {
    modulePage = 1;
    loadPatientModule();
  } else {
    switchWorkspaceView("patients", query);
  }
}
// 顶部搜索自动下拉：输入即列匹配患者(姓名/拼音/全拼/首字母/电话/ID)，点选直达主页
let _qDropdown = null;
let _qTimer = null;
function ensureQDropdown() {
  if (_qDropdown) return _qDropdown;
  _qDropdown = document.createElement("div");
  _qDropdown.className = "q-dropdown";
  _qDropdown.hidden = true;
  document.body.appendChild(_qDropdown);
  return _qDropdown;
}
function hideQDropdown() { if (_qDropdown) _qDropdown.hidden = true; }
function positionQDropdown() {
  const r = q.getBoundingClientRect();
  const d = ensureQDropdown();
  d.style.left = r.left + "px";
  d.style.top = (r.bottom + 4) + "px";
  d.style.width = Math.max(r.width, 280) + "px";
}
// 头像系统 v1：同步数据暂无真实照片 → 占位头像(姓名首字彩色圆,按性别上色)。
// 将来拿到真实头像(patients.avatar_url)时这里改成 <img src>。可全局复用。
function avatarHtml(p, extraClass) {
  const cls = `patient-avatar${extraClass ? " " + extraClass : ""}`;
  if (p && p.avatar_path && p.patient_identity) {
    const t = encodeURIComponent(p.local_updated_at || "");
    return `<span class="${cls}"><img src="/api/patients/${encodeURIComponent(p.patient_identity)}/avatar?t=${t}" alt=""></span>`;
  }
  const name = ((p && p.display_name) || "").trim();
  const ch = name ? name.charAt(0) : "?";
  const sexCls = p && p.sex === "女" ? " av-f" : (p && p.sex === "男" ? " av-m" : "");
  return `<span class="${cls}${sexCls}">${escapeHtml(ch)}</span>`;
}
window.avatarHtml = avatarHtml;

function _ageFromBirthday(b) {
  const s = String(b || "").trim();
  const m = s.match(/^(\d{4})-(\d{2})-(\d{2})/);
  const y = m ? m[1] : ((s.match(/^(\d{4})$/) || [])[1]);   // 年龄直填只存推算出生年
  if (!y || y === "0000") return "";
  const now = bjToday();
  let age = now.getFullYear() - (+y);
  if (m && (now.getMonth() + 1 < (+m[2]) || (now.getMonth() + 1 === (+m[2]) && now.getDate() < (+m[3])))) age--;
  return age >= 0 && age < 130 ? String(age) : "";
}
async function runQSuggest(val) {
  if (!_authenticatedBusinessReady) return;
  let data;
  try { data = await fetch(`/api/patients?q=${encodeURIComponent(val)}&pagesize=8`).then(r => r.json()); }
  catch { return; }
  if (q.value.trim() !== val) return;   // 输入已变，丢弃旧结果
  const d = ensureQDropdown();
  const list = data.list || [];
  if (!list.length) {
    d.innerHTML = `<div class="q-empty">无匹配患者</div>`;
  } else {
    d.innerHTML = list.map(p => {
      const age = _ageFromBirthday(p.birthday);
      const meta = [p.sex, age ? age + "岁" : ""].filter(Boolean).join(" · ");
      const last = (p.last_visit || "").slice(0, 10);
      const cid = p.chart_no ? `<span class="q-cid">病历号 ${escapeHtml(p.chart_no)}</span>`
        : (p.source_customer_id ? `<span class="q-cid">客户ID ${escapeHtml(p.source_customer_id)}</span>` : "");
      const remark = p.remark ? `<span class="q-remark">备注：${escapeHtml(p.remark)}</span>` : "";
      const line3 = (cid || remark) ? `<div class="q-line3">${cid}${remark}</div>` : "";
      return `<div class="q-item" data-id="${escapeAttr(p.patient_identity || "")}">` +
        `${avatarHtml(p, "q-av")}` +
        `<div class="q-info">` +
        `<div class="q-line1"><span class="q-nm">${escapeHtml(p.display_name || "(无名)")}</span>` +
        `${meta ? `<span class="q-meta">${escapeHtml(meta)}</span>` : ""}</div>` +
        `<div class="q-line2"><span class="q-ph">${escapeHtml(p.phone || "")}</span>` +
        `${last ? `<span class="q-last">最近就诊 ${escapeHtml(last)}</span>` : ""}</div>` +
        `${line3}</div></div>`;
    }).join("");
    d.querySelectorAll(".q-item").forEach(el => el.addEventListener("mousedown", event => {
      event.preventDefault();   // 先于 blur，确保点击生效
      openPatientWorkspace(el.dataset.id);
      q.value = ""; hideQDropdown();
    }));
  }
  positionQDropdown();
  d.hidden = false;
}
let _authenticatedBusinessEventsRegistered = false;
function registerAuthenticatedBusinessEvents() {
  if (_authenticatedBusinessEventsRegistered) return;
  _authenticatedBusinessEventsRegistered = true;
  const searchButton = document.getElementById("searchBtn");
  if (searchButton) searchButton.addEventListener("click", runTopSearch);
  q.addEventListener("keydown", event => {
    if (event.key === "Enter") runTopSearch();
    if (event.key === "Escape") hideQDropdown();
  });
  q.addEventListener("input", () => {
    clearTimeout(_qTimer);
    const val = q.value.trim();
    if (!val) { hideQDropdown(); return; }
    _qTimer = setTimeout(() => runQSuggest(val), 200);
  });
  q.addEventListener("blur", () => setTimeout(hideQDropdown, 120));
  window.addEventListener("resize", () => { if (_qDropdown && !_qDropdown.hidden) positionQDropdown(); });
  document.querySelectorAll(".nav-item[data-workspace-view]").forEach(button => {
    button.addEventListener("click", () => switchWorkspaceView(button.dataset.workspaceView));
  });
  window.addEventListener("hashchange", handlePatientWorkspaceHash);
}

// === 账号系统：会话失效拦截 + 当前用户显示 + 登出 ===
const _ROLE_LABELS = {admin: "管理员", reception: "前台", doctor: "医生", assistant: "助理",
                      nurse: "护士", consultant: "咨询师"};
let _authReloading = false;
const _origFetch = window.fetch.bind(window);

async function _accessResponseDetail(response) {
  try {
    const payload = await response.clone().json();
    return payload && typeof payload === "object" ? String(payload.detail || "") : "";
  } catch {
    return "";
  }
}

async function _confirmRecentPassword() {
  const password = await appPrompt(
    "这是退费、删除或恢复资料等重要操作，请输入当前登录密码确认：",
    "",
    {type: "password", autocomplete: "current-password"},
  );
  if (password === null) return false;
  let response;
  try {
    response = await _origFetch("/api/auth/reauth", {
      method: "POST",
      headers: {"Content-Type": "application/json"},
      body: JSON.stringify({password}),
    });
  } catch {
    window.alert("密码确认失败，请检查连接后重试。操作没有执行。");
    return false;
  }
  if (response.ok) return true;
  const detail = await _accessResponseDetail(response);
  if (response.status === 401 && detail !== "密码错误") {
    if (!_authReloading) {
      _authReloading = true;
      location.reload();
    }
    return false;
  }
  window.alert(response.status === 401 ? "密码不正确，操作没有执行。" : "密码确认失败，操作没有执行。");
  return false;
}

function _accessFetchAttempts(args) {
  if (
    window.__accessV3 !== true
    || typeof Request !== "function"
    || !(args[0] instanceof Request)
  ) return {first: args, retry: args};
  try {
    const first = new Request(...args);
    return {first: [first], retry: [first.clone()]};
  } catch {
    return {first: args, retry: args};
  }
}

window.fetch = async function (...args) {
  if (!_authenticatedBusinessReady) throw new Error("authentication not ready");
  // Request 的流式 body 只能消费一次；首次发送前留一份克隆，供确认成功后唯一一次重放。
  const attempts = _accessFetchAttempts(args);
  const res = await _origFetch(...attempts.first);
  // 会话失效(后端门禁返回401)→回登录页(门禁会对页面请求返回登录页)
  if (res.status === 401 && !_authReloading) {
    _authReloading = true;
    location.reload();
    return res;
  }
  if (
    window.__accessV3 === true
    && res.status === 403
    && await _accessResponseDetail(res) === "recent_reauthentication_required"
    && await _confirmRecentPassword()
  ) {
    // 原请求尚未进入业务处理函数；确认成功后只重放一次，避免重复副作用。
    return _origFetch(...attempts.retry);
  }
  return res;
};

// 按权限隐藏带 data-perm 的菜单项(仅 UX：减少无权角色看到进不去的入口；后端 require_perm 才是闸门)。
// 只 gate 后台/管理类菜单(报表/库存/消毒/收费项目/配置/同步)，日常临床菜单(今日/患者/预约/收费/回访)不 gate。
function applyPermVisibility() {
  document.querySelectorAll(".nav-item[data-perm], .workspace-tab[data-perm]").forEach((btn) => {
    btn.hidden = !hasPerm(btn.dataset.perm);
  });
  // data-perm-any="a,b,c"：有任一权限即显示(如配置管理含人员管理/角色权限/全店设置多种权限)
  document.querySelectorAll(".nav-item[data-perm-any], .workspace-tab[data-perm-any]").forEach((btn) => {
    const any = (btn.dataset.permAny || "").split(",").map(s => s.trim()).filter(Boolean);
    btn.hidden = !any.some(p => hasPerm(p));
  });
}

// #488:关闭的弹窗只隐藏外层 backdrop,内部 section[role=dialog] 仍留在 DOM 且自身不 hidden,
// 读屏/自动化全局扫描会读到旧弹窗内容(选择牙位/收款/划价…)。统一镜像:外层 hidden 变化时,
// 把 hidden/aria-hidden 同步到直接子 dialog;MutationObserver 覆盖动态建到 body 的弹窗,免改几十个 close 函数。
function syncDialogHidden(host) {
  host.querySelectorAll(":scope > [role=dialog]").forEach((d) => {
    d.hidden = !!host.hidden;
    d.setAttribute("aria-hidden", host.hidden ? "true" : "false");
  });
}
(function watchModalBackdrops() {
  document.querySelectorAll(".modal-backdrop").forEach(syncDialogHidden);
  new MutationObserver((muts) => {
    muts.forEach((m) => {
      if (m.target.nodeType === 1 && m.target.querySelector(":scope > [role=dialog]")) syncDialogHidden(m.target);
    });
  }).observe(document.body, { attributes: true, subtree: true, attributeFilter: ["hidden"] });
})();

async function initAuthUI() {
  let user = null;
  let accessContractInvalid = false;
  try {
    const response = await _origFetch("/api/auth/me");
    if (!response.ok) throw new Error("auth me failed");
    const payload = await response.json();
    const candidate = payload && typeof payload === "object" && !Array.isArray(payload)
      ? payload.user
      : null;
    if (candidate && typeof candidate === "object" && !Array.isArray(candidate)) {
      const candidateIsAccessV3 = Boolean(
        Object.prototype.hasOwnProperty.call(candidate, "primary_role")
          || Object.prototype.hasOwnProperty.call(candidate, "role_keys")
          || Object.prototype.hasOwnProperty.call(candidate, "is_system_admin")
      );
      if (!candidateIsAccessV3) {
        user = candidate;
      } else if (
        Array.isArray(candidate.permissions)
          && candidate.permissions.every(permission => (
            typeof permission === "string"
              && permission.length > 0
              && permission === permission.trim()
              && !/\s/.test(permission)
          ))
          && new Set(candidate.permissions).size === candidate.permissions.length
      ) {
        user = candidate;
      } else {
        accessContractInvalid = true;
      }
    }
  } catch { user = null; }
  const isAccessV3 = Boolean(user && (
    Object.prototype.hasOwnProperty.call(user, "primary_role")
      || Object.prototype.hasOwnProperty.call(user, "role_keys")
      || Object.prototype.hasOwnProperty.call(user, "is_system_admin")
  ));
  const primaryRole = user
    ? String(user.primary_role || user.role || "").trim()
    : "";
  const roleKeys = user && Array.isArray(user.role_keys)
    ? user.role_keys.filter(role => typeof role === "string" && role.trim())
    : (primaryRole ? [primaryRole] : []);
  const legacyAdmin = Boolean(user && !isAccessV3 && user.role === "admin");
  window.__accessV3 = isAccessV3;
  window.__userRole = primaryRole || null;
  window.__userRoleKeys = roleKeys;
  window.__isSystemAdmin = Boolean(user && (user.is_system_admin === true || legacyAdmin));
  // 始终保留后端返回的精确权限集合，供页面判断直接 entitlement；system admin 的全权限
  // 只由 __isSystemAdmin + hasPerm 首行保障。V3 permissions 合同损坏时不建立任何登录 UI。
  const _perms = user && user.permissions;
  window.__userPerms = Array.isArray(_perms)
    ? new Set(_perms)
    : (window.__isSystemAdmin ? new Set() : null);
  if (accessContractInvalid) {
    const authStatus = document.getElementById("statusText");
    if (authStatus) authStatus.textContent = "登录信息异常，请刷新页面或重新登录";
    document.querySelectorAll("[data-perm], [data-perm-any]").forEach(entry => {
      entry.hidden = true;
    });
  }
  if (Array.isArray(_perms) || window.__isSystemAdmin) applyPermVisibility();
  const chip = document.getElementById("userChip");
  const nameEl = document.getElementById("userChipName");
  if (user && chip && nameEl) {
    const displayName = user.display_name || user.username || "当前用户";
    const roleLabel = primaryRole ? (_ROLE_LABELS[primaryRole] || primaryRole) : "";
    nameEl.textContent = roleLabel ? `${displayName}（${roleLabel}）` : displayName;
    chip.hidden = false;
    const acct = document.getElementById("acctMgrBtn");
    if (acct) acct.hidden = !canAccessAccounts();
  }
  const logout = document.getElementById("logoutBtn");
  if (logout) logout.addEventListener("click", async () => {
    try { await _origFetch("/api/auth/logout", {method: "POST"}); } catch { /* 忽略 */ }
    location.reload();
  });
  return Boolean(user);
}
// 后端能力(开源壳阶段3):迁移层入口(同步中心等)按 /api/capabilities 显隐。
async function initCapabilities() {
  try { window.__capabilities = await (await _origFetch("/api/capabilities")).json(); }
  catch { window.__capabilities = null; }   // 拉不到按完整版显示,后端路由仍是闸门
}

// 诊所名：全站显示真相源(开源壳阶段1,不再硬编码)。启动拉一次 → window.CLINIC_NAME,
// 刷新标题/PWA名；管理员首启没填过诊所名时引导填一次(取消则下次登录再提示)。
async function initClinicIdentity() {
  let c = null;
  try { c = await (await _origFetch("/api/settings/clinic")).json(); } catch { return; }
  if (!c || !c.name) return;
  const apply = (name) => {
    window.CLINIC_NAME = name;
    document.title = `${name} · 本地系统`;
    const meta = document.querySelector('meta[name="apple-mobile-web-app-title"]');
    if (meta) meta.content = name;
  };
  apply(c.name);
  if (c.configured || typeof hasAnyPerm !== "function"
      || !hasAnyPerm("clinic_settings.manage", "settings.manage")) return;
  const input = await appPrompt("首次使用：请填写诊所名称（显示在登录页、回访卡、打印单头）", "");
  const name = (input || "").trim();
  if (!name) return;
  const r = await _origFetch("/api/settings/clinic", {
    method: "PUT", headers: {"Content-Type": "application/json"}, body: JSON.stringify({name}),
  });
  if (r.ok) apply(name);
  else window.alert("诊所名称保存失败：" + r.status);
}

initAuthUI().then(authenticated => {
  if (!authenticated) return;
  _authenticatedBusinessReady = true;
  registerAuthenticatedBusinessEvents();
  loadTodayWork();
  handlePatientWorkspaceHash();
  return Promise.all([initClinicIdentity(), initCapabilities()]);
});

// 侧栏底部显示当前前端资源版本：直接从已加载的 app.js ?v= 读取，自动同步、免手维护，
// 顺带当"缓存新鲜度"指示灯——真机看到的号≠最新即说明浏览器还在吃旧缓存。
function showAppVersion() {
  const el = document.getElementById("appVersion");
  if (!el) return;
  const tag = Array.from(document.scripts).find(s => /\/app\.js\?v=/.test(s.src || ""));
  const m = tag && tag.src.match(/[?&]v=([^&]+)/);
  el.textContent = m ? ("v" + decodeURIComponent(m[1])) : "";
}
showAppVersion();
