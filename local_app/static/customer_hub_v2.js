// 客户通 V2(#856 任务3/4):今日总览(默认)/回访列表/沟通记录 三视图 + 回访处理弹窗。
// 形态基线:docs/superpowers/prototypes/2026-07-17-customer-hub-v2-final-demo.html(用户定稿)。
// 独立模块(防撞规矩):module_views.loadReturnVisitModule 仅委托 loadCustomerHubV2();
// 全局依赖(app.js 提供):modulePanel/workDate/localDateValue/
// escapeHtml/escapeAttr/openPatientWorkspace。权限即隐形:响应缺块=整块不渲染,禁假零兜底。

let _chvView = "today";          // today 今日总览(默认) / list 回访列表 / comm 沟通记录
let _chvDay = "";                // 总览当前日期(YYYY-MM-DD;空=工作日期)
let _chvCalMonth = "";           // 月历弹卡当前月 YYYY-MM
// 列表筛选状态(与后端参数一一对应)
function _chvEmptyFilter() {
  return {date_from: "", date_to: "", status: "", doctor: "", visitor: "",
          channel: "", rtype: "", att: "", item: "", patient_q: "", content_q: "", q: ""};
}
let _chvF = _chvEmptyFilter();
let _chvCommF = {date_from: "", date_to: "", q: ""};   // 沟通台账独立筛选(不与列表串味)
let _chvDicts = null;         // 常用语字典缓存(打开弹窗时取;失败不缓存,下次重试)
let _chvDictRequestGeneration = 0;
const _CHV_DICT_REQUEST_STALE = Symbol("customer_hub_dictionary_request_stale");
let _chvLifecycleGeneration = 0;
let _chvPendingChip = false;  // 待回访入口提示章
let _chvListPage = 1;         // 回访列表分页
let _chvCommPage = 1;         // 沟通台账分页
let _chvDialogSeq = 0;        // dialog 标题全局唯一编号
const _CHV_PAGESIZE = 50;
const _chvPageWriteState = {};

// 客户通所有迟到异步续作共用独立模块代数：进入、刷新或离开都会推进。
function _chvCaptureLifecycleToken() {
  return _chvLifecycleGeneration;
}

function _chvLifecycleIsCurrent(token) {
  return token === _chvLifecycleGeneration;
}

function _chvNodeConnected(node) {
  return Boolean(node && node.isConnected !== false && node.removed !== true);
}

function _chvEncodeComponent(value) {
  const source = String(value);
  let normalized = "";
  for (let index = 0; index < source.length; index += 1) {
    const code = source.charCodeAt(index);
    if (code >= 0xD800 && code <= 0xDBFF) {
      const next = source.charCodeAt(index + 1);
      if (next >= 0xDC00 && next <= 0xDFFF) {
        normalized += source[index] + source[index + 1];
        index += 1;
      } else {
        normalized += "\uFFFD";
      }
    } else {
      normalized += code >= 0xDC00 && code <= 0xDFFF ? "\uFFFD" : source[index];
    }
  }
  return encodeURIComponent(normalized);
}

function _chvHasAllPermissions(...permissions) {
  return permissions.every(permission => hasPerm(permission));
}

function _chvContractApi() {
  const api = window.CustomerHubV2Contract;
  if (!api || typeof api.fetchJson !== "function"
      || typeof api.sendCustomerHubRequest !== "function"
      || typeof api.readReturnVisitRevision !== "function"
      || typeof api.normalizeMedicalRecordTimeline !== "function"
      || typeof api.validateReturnVisitEditReceipt !== "function"
      || typeof api.validateReturnVisitEditConfirmation !== "function") {
    throw new Error("customer_hub_contract_unavailable");
  }
  return api;
}

function _chvFollowupDateApi() {
  const api = window.CustomerHubV2FollowupDate;
  if (!api || typeof api.computeNextFollowupDate !== "function") {
    throw new Error("customer_hub_followup_date_unavailable");
  }
  return api;
}

async function _chvWrite(kind, options) {
  const api = _chvContractApi();
  const handlers = {
    returnVisit: api.handleReturnVisitWrite,
    communication: api.handleCommunicationWrite,
    call: api.handleCallWrite,
    dictionary: api.handleDictionaryWrite,
  };
  const handler = handlers[kind];
  if (typeof handler !== "function") throw new Error("customer_hub_write_handler_unavailable");
  return handler({
    ...options,
    sendRequest: request => api.sendCustomerHubRequest(request, {fetch}),
  });
}

function _chvOverlayPending(ovl) {
  const state = ovl && ovl._chvWriteState;
  return Boolean(ovl && (ovl._chvMicBusy || ovl._chvMicPending
    || (state && (state.writeInFlight || state.operationInFlight))));
}

function _chvOverlayControls(ovl) {
  return ovl ? [...ovl.querySelectorAll("input, select, textarea, button")] : [];
}

async function _chvWriteInOverlay(ovl, focusTarget, kind, options) {
  if (!ovl || ovl.removed || ovl.isConnected === false || ovl._chvMicBusy || ovl._chvMicPending) {
    return {ok: false, status: 0, busy: true, message: "保存处理中，请勿重复提交"};
  }
  const state = ovl._chvWriteState || (ovl._chvWriteState = {});
  return _chvWrite(kind, {
    ...options,
    state,
    pendingControls: _chvOverlayControls(ovl),
    pendingFocusTarget: ovl.querySelector(".chv-pop") || ovl,
    focusTarget,
    isCurrent: () => !ovl.removed && ovl.isConnected !== false,
  });
}

async function _chvRunOverlayOperation(ovl, focusTarget, operation) {
  if (!ovl || _chvOverlayPending(ovl)) return false;
  const state = ovl._chvWriteState || (ovl._chvWriteState = {});
  if (state.operationInFlight) return false;
  const token = {};
  state.operationInFlight = token;
  const snapshots = _chvOverlayControls(ovl).map(control => ({control, disabled: control.disabled === true}));
  snapshots.forEach(({control}) => {
    control.disabled = true;
    control.setAttribute("aria-busy", "true");
    control.setAttribute("aria-disabled", "true");
  });
  const pendingTarget = ovl.querySelector(".chv-pop") || ovl;
  if (typeof pendingTarget.focus === "function") pendingTarget.focus();
  try {
    return await operation();
  } finally {
    snapshots.forEach(({control, disabled}) => {
      control.disabled = disabled;
      control.setAttribute("aria-busy", "false");
      control.setAttribute("aria-disabled", disabled ? "true" : "false");
    });
    if (state.operationInFlight === token) delete state.operationInFlight;
    if (!ovl.removed && ovl.isConnected !== false && focusTarget
        && focusTarget.disabled !== true && typeof focusTarget.focus === "function") focusTarget.focus();
  }
}

async function _chvWriteOnPage(root, focusTarget, kind, options) {
  if (_chvPageWriteState.writeInFlight) {
    return {ok: false, status: 0, busy: true, message: "保存处理中，请勿重复提交"};
  }
  const controlsRoot = modulePanel || root;
  return _chvWrite(kind, {
    ...options,
    state: _chvPageWriteState,
    pendingControls: controlsRoot.querySelectorAll("input, select, textarea, button"),
    pendingFocusTarget: root,
    focusTarget,
    isCurrent: () => root.isConnected !== false,
  });
}

function _chvWriteError(result, prefix) {
  const message = result && result.message ? result.message : `${prefix}失败`;
  window.alert(`${prefix}失败：${message.replace(/^保存失败：?/, "")}`);
}

// 分页条:totalcount/当前页/每页,onGo(n) 翻页
function _chvPagerHtml(total, page) {
  const pages = Math.max(1, Math.ceil((total || 0) / _CHV_PAGESIZE));
  if (pages <= 1) return "";
  return `<div class="chv-pager">
    <button type="button" class="chv-btn" data-chv-page="${page - 1}"${page <= 1 ? " disabled" : ""}>‹ 上一页</button>
    <span>第 ${page} / ${pages} 页 · 共 ${total} 条</span>
    <button type="button" class="chv-btn" data-chv-page="${page + 1}"${page >= pages ? " disabled" : ""}>下一页 ›</button>
  </div>`;
}

function _chvBindPager(root, total, page, onGo) {
  const pages = Math.max(1, Math.ceil((total || 0) / _CHV_PAGESIZE));
  root.querySelectorAll("[data-chv-page]").forEach(b => b.addEventListener("click", () => {
    const n = Number(b.dataset.chvPage);
    if (n >= 1 && n <= pages && n !== page) onGo(n);
  }));
}

function _chvToday() {
  // #729 日期契约:与今日工作台同一真相源
  return _chvDay || ((workDate && workDate.value) ? workDate.value : localDateValue());
}

function _chvWayText(channel, direction) {
  const way = channel === "phone" ? "电话" : channel === "wechat" ? "微信" : (channel || "—");
  if (channel === "phone" && direction) return `${way} ${direction === "inbound" ? "↘呼入" : "↗呼出"}`;
  return way;
}

// 完成态与后端 DONE_COND 同口径:status done集,或(status 非显式待办 且 item_name/note 前缀「已回访」)
// 显式待办状态(待回访/待跟进/做计划)优先于前缀
function _chvRowDone(r) {
  const st = (r && typeof r === "object") ? (r.status || "") : (r || "");
  if (["done", "已回访", "完成", "4"].includes(st)) return true;
  if (["待回访", "待跟进", "做计划"].includes(st)) return false;
  if (r && typeof r === "object")
    return (r.item_name || "").startsWith("已回访") || (r.note || "").startsWith("已回访");
  return false;
}
// 传行对象(推荐,含前缀判定)或裸 status 字符串(兼容旧调用)
function _chvStatusBadge(rowOrStatus) {
  const done = _chvRowDone(rowOrStatus);
  const raw = (rowOrStatus && typeof rowOrStatus === "object") ? (rowOrStatus.status || "") : (rowOrStatus || "");
  const cls = done ? "chv-b-done" : "chv-b-wait";
  return `<span class="chv-badge ${cls}">${escapeHtml(done ? "已回访" : (raw || "待回访"))}</span>`;
}

function _chvName(pid, name) {
  if (!hasPerm("patient.profile.view")) return escapeHtml(name || "(无名)");
  // 点名字才进个人主页(用户拍板);行点击只开弹窗
  return `<span class="chv-pname" data-chv-home="${escapeAttr(pid || "")}">${escapeHtml(name || "(无名)")}</span>`;
}

async function _chvFetchJson(url) {
  return _chvContractApi().fetchJson(url, {fetch});
}

async function _chvLoadStaffDirectory(mode) {
  const api = _chvContractApi();
  const request = typeof api.buildStaffDirectoryRequest === "function"
    ? api.buildStaffDirectoryRequest(mode) : null;
  if (!request || typeof api.normalizeStaffDirectory !== "function") {
    throw new Error("staff_directory_contract_unavailable");
  }
  const payload = await api.fetchJson(request.url, {fetch, method: request.method});
  return api.normalizeStaffDirectory(payload);
}

// ============ 入口与三视图骨架 ============

function _chvValidEntryDate(value) {
  const match = /^(\d{4})-(\d{2})-(\d{2})$/.exec(value);
  if (!match) return false;
  const year = Number(match[1]);
  const month = Number(match[2]);
  const day = Number(match[3]);
  const leap = year % 4 === 0 && (year % 100 !== 0 || year % 400 === 0);
  const monthDays = [31, leap ? 29 : 28, 31, 30, 31, 30, 31, 31, 30, 31, 30, 31];
  return year >= 1 && month >= 1 && month <= 12
    && day >= 1 && day <= monthDays[month - 1];
}

function _chvReadEntryContext(context) {
  const normalized = {
    status: "ok",
    returnFilter: "",
    searchText: "",
    selectedDate: "",
    selectedDateProvided: false,
  };
  if (context == null) return normalized;
  if (typeof context !== "object") return {status: "error"};
  const values = Object.create(null);
  try {
    for (const key of ["returnFilter", "searchText", "selectedDate"]) {
      const descriptor = Object.getOwnPropertyDescriptor(context, key);
      if (descriptor && Object.prototype.hasOwnProperty.call(descriptor, "value")) {
        values[key] = descriptor.value;
      }
    }
  } catch {
    return {status: "error"};
  }
  normalized.returnFilter = typeof values.returnFilter === "string" ? values.returnFilter.trim() : "";
  normalized.searchText = typeof values.searchText === "string" ? values.searchText.trim() : "";
  if (!Object.prototype.hasOwnProperty.call(values, "selectedDate")
      || values.selectedDate == null) return normalized;
  if (typeof values.selectedDate !== "string") return {status: "error"};
  const selectedDate = values.selectedDate.trim();
  if (!selectedDate) return normalized;
  if (!_chvValidEntryDate(selectedDate)) return {status: "error"};
  normalized.selectedDate = selectedDate;
  normalized.selectedDateProvided = true;
  return normalized;
}

function _chvFailEntryLoad() {
  _chvLifecycleGeneration += 1;
  modulePanel.innerHTML = '<div class="module-loading">客户通载入失败</div>';
  return false;
}

async function loadCustomerHubV2(context) {
  if (!modulePanel || !hasPerm("patient.profile.view")) return false;
  const entryContext = _chvReadEntryContext(context);
  if (entryContext.status !== "ok") return _chvFailEntryLoad();
  const {returnFilter, searchText, selectedDate} = entryContext;
  // 今日工作台「今日待回访」入口 → 直进列表视图(待回访工作台口径)
  // 前端复核轮P1:必须清全部旧筛选+页码,否则残留的医生/关键词/高级筛选会藏掉待跟进患者
  if (returnFilter === "pending_due") {
    _chvView = "list";
    _chvF = _chvEmptyFilter();
    _chvListPage = 1;
    _chvPendingChip = true;   // #221 今日工作台「今日待回访」入口:列表带提示章
  }
  if (searchText) {
    _chvView = "list";
    _chvF = _chvEmptyFilter();
    _chvF.q = searchText;
    _chvListPage = 1;
    if (returnFilter !== "pending_due") {
      _chvPendingChip = false;
    }
  }
  if (returnFilter === "pending_due" && entryContext.selectedDateProvided) _chvF.date_to = selectedDate;
  if (selectedDate) _chvDay = selectedDate;
  const token = ++_chvLifecycleGeneration;
  modulePanel.innerHTML = `
    ${_chvTabsHtml()}
    <div id="chvBody"><div class="module-loading">客户通载入中...</div></div>`;
  _chvBindTabs(modulePanel);
  if (_chvView === "today") return _chvRenderToday(token);
  if (_chvView === "list") return _chvRenderList(token);
  return _chvRenderComm(token);
}

async function requestLeaveCustomerHubV2() {
  const overlays = [...document.querySelectorAll(".chv-ovl")];
  if (_chvPageWriteState.writeInFlight || overlays.some(_chvOverlayPending)) return false;
  const dirty = overlays.some(overlay => (
    (overlay._chvGuarded && overlay._chvDirty && !overlay._chvClean)
    || overlay._chvMediaDirty
  ));
  if (dirty && !window.confirm("当前有未保存内容，确认放弃并离开吗？")) return false;
  overlays.forEach(overlay => {
    _chvDisposeOverlay(overlay);
  });
  _chvLifecycleGeneration += 1;
  return true;
}

function _chvTabsHtml() {
  // 权限即隐形:无回访查看权不显示回访列表,无沟通查看权不显示沟通记录
  const tabs = [["today", "今日总览", true]];
  if (hasPerm("return_visit.view")) tabs.push(["list", "回访列表", true]);
  if (hasPerm("patient.profile.view") && hasPerm("communication.view")) {
    tabs.push(["comm", "沟通记录", true]);
  }
  // 若当前视图已被权限收走,回落到今日总览
  if (!tabs.some(t => t[0] === _chvView)) _chvView = "today";
  return `<div class="chv-tabs">${tabs.map(([v, label]) =>
    `<button type="button" class="chv-tab${v === _chvView ? " on" : ""}" data-chv-view="${v}">${escapeHtml(label)}</button>`
  ).join("")}</div>`;
}

function _chvBindTabs(root) {
  root.querySelectorAll("[data-chv-view]").forEach(b => b.addEventListener("click", async () => {
    if (b.dataset.chvView === _chvView) return;
    if (!await requestLeaveCustomerHubV2()) return;
    _chvView = b.dataset.chvView;
    loadCustomerHubV2();
  }));
}

// ============ 今日总览 ============

async function _chvRenderToday(token) {
  if (!_chvHasAllPermissions("patient.profile.view")) return false;
  const body = document.getElementById("chvBody");
  const day = _chvToday();
  let data;
  try {
    // 单一来源:今日总览表格与 KPI 均取自 /api/customer-hub/today(避免二次按 due_time 查错账)
    data = await _chvFetchJson(`/api/customer-hub/today?date=${_chvEncodeComponent(day)}`);
    if (token !== _chvLifecycleGeneration) return;
  } catch {
    if (token === _chvLifecycleGeneration) body.innerHTML = `<div class="module-loading">今日总览载入失败</div>`;
    return;
  }

  const kpis = [];
  if (hasPerm("return_visit.manage")) {
    kpis.push(`<button type="button" class="chv-kpi chv-glass chv-kpi-add" data-chv-kpinew="1"><small>＋</small><b>新增回访</b><span>登记一次新回访</span></button>`);
  }
  if (data.return_visits) {
    kpis.push(`<div class="chv-kpi chv-glass hot"><small>待回访</small><b>${data.return_visits.pending_count}</b><span>还没打的回访电话</span></div>`);
    kpis.push(`<div class="chv-kpi chv-glass"><small>已回访</small><b>${data.return_visits.done_count}</b><span>已完成的回访</span></div>`);
  }

  let rvBlock = "";
  if (data.return_visits) {
    // 待回访 + 已回访(actual_date 归属当天)合并,与 KPI 计数同源
    const rows = [...(data.return_visits.pending || []), ...(data.return_visits.done || [])];
    const trs = rows.map(r => `
      <tr class="chv-row" data-chv-open="${escapeAttr(r.return_visit_id)}" data-chv-pid="${escapeAttr(r.patient_identity || "")}"
          data-visitor="${escapeAttr(r.visitor || "")}" data-doctor="${escapeAttr(r.responsible_doctor || "")}">
        <td>${_chvName(r.patient_identity, r.display_name)}</td>
        <td>${escapeHtml(r.phone || "")}</td>
        <td class="chv-clip">${escapeHtml(r.note || r.item_name || "")}</td>
        <td class="chv-clip">${escapeHtml(r.return_result || "—")}</td>
        <td>${escapeHtml(r.responsible_doctor || "—")} / ${escapeHtml(r.visitor || "—")}</td>
        <td>${_chvStatusBadge(r)}</td>
        <td>${hasPerm("call_record.view") ? `<button type="button" class="chv-mini" data-chv-play="${escapeAttr(r.return_visit_id)}" data-chv-pid2="${escapeAttr(r.patient_identity || "")}">▶</button>` : "—"}</td>
      </tr>`).join("");
    // 双筛选(真机验收#2改):回访人/医生,默认全部;选项取当天实际出现的值
    const uniq = k => [...new Set(rows.map(r => (r[k] || "").trim()).filter(Boolean))];
    const opts = vals => [`<option value="">全部</option>`,
      ...vals.map(v => `<option>${escapeHtml(v)}</option>`)].join("");
    rvBlock = `
      <section class="chv-panel chv-glass">
        <div class="chv-phead"><h3>今日回访</h3>
          <span class="chv-filters">
            <label>回访人 <select data-chv-fvisitor>${opts(uniq("visitor"))}</select></label>
            <label>医生 <select data-chv-fdoctor>${opts(uniq("responsible_doctor"))}</select></label>
          </span>
          <span class="chv-badge chv-b-teal">待回访 ${data.return_visits.pending_count} · 已回访 ${data.return_visits.done_count}</span></div>
        <table class="chv-table"><thead><tr><th>患者</th><th>电话</th><th>回访内容</th><th>回访结果</th><th>主治/回访人</th><th>状态</th><th>录音</th></tr></thead>
        <tbody>${trs || `<tr><td colspan="7" class="chv-empty">当天无回访任务</td></tr>`}</tbody></table>
        <div class="chv-note">点行弹窗处理这一次回访;点名字进他的个人主页;▶ 听该回访的录音</div>
      </section>`;
  }

  let commBlock = "";
  if (data.communications) {
    const trs = data.communications.map(c => `
      <tr>
        <td>${_chvName(c.patient_identity, c.display_name)}</td>
        <td>${escapeHtml(_chvWayText(c.channel, c.direction))}</td>
        <td>${c.reason ? `<span class="chv-badge chv-b-teal">${escapeHtml(c.reason)}</span>` : "—"}</td>
        <td>${escapeHtml(c.operator || "")}</td>
        <td class="chv-clip">${escapeHtml(c.content || "")}</td>
        <td>${escapeHtml(c.deal_status || "—")}</td>
      </tr>`).join("");
    const addCommBtn = hasPerm("patient.profile.view") && hasPerm("communication.manage")
      ? `<button type="button" class="chv-btn" data-chv-addcomm="1">＋ 记沟通</button>` : "";
    commBlock = `
      <section class="chv-panel chv-glass">
        <div class="chv-phead"><h3>今日沟通</h3>
          <span><span class="chv-badge chv-b-teal">${data.communications.length} 条</span>
          ${addCommBtn}</span></div>
        <table class="chv-table"><thead><tr><th>患者</th><th>方式</th><th>干什么</th><th>沟通人</th><th>聊了什么</th><th>成交</th></tr></thead>
        <tbody>${trs || `<tr><td colspan="6" class="chv-empty">当天无沟通记录</td></tr>`}</tbody></table>
      </section>`;
  }

  const recBtn = data.calls
    ? `<button type="button" class="chv-btn" data-chv-recs="1">🎙 录音 <span class="chv-badge chv-b-teal">${data.calls.length}</span></button>`
    : "";
  body.innerHTML = `
    <div class="chv-ovbar chv-glass">
      <div class="chv-dbar">
        <button type="button" class="chv-btn" data-chv-dprev="1" title="前一天">‹</button>
        <button type="button" class="chv-btn" data-chv-cal="1" title="点击选择日期">📅 ${escapeHtml(day)}</button>
        <button type="button" class="chv-btn" data-chv-dnext="1" title="后一天">›</button>
        <button type="button" class="chv-btn" data-chv-dtoday="1">今天</button>
        <h3>· 总览</h3>
      </div>
      <div class="chv-actions">
        ${recBtn}
        ${hasPerm("return_visit.view") ? '<button type="button" class="chv-btn" data-chv-mr="1">📊 月度回顾</button>' : ""}
      </div>
    </div>
    ${kpis.length ? `<div class="chv-kpis">${kpis.join("")}</div>` : ""}
    ${rvBlock}${commBlock}`;
  _chvBindCommon(body, data);
}

// 日期前后翻一天(今日工作同款);基准=当前选中日期或今天
function _chvShiftDay(delta) {
  const base = _chvDay || _chvToday();
  const d = new Date(`${base}T00:00:00`);
  d.setDate(d.getDate() + delta);
  _chvDay = `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, "0")}-${String(d.getDate()).padStart(2, "0")}`;
  loadCustomerHubV2();
}

// ============ 回访列表 ============

function _chvListQuery(extra) {
  const f = _chvF;
  const parts = [];
  for (const k of ["date_from", "date_to", "status", "doctor", "visitor",
                   "channel", "rtype", "att", "item", "patient_q", "content_q", "q"]) {
    if (f[k]) parts.push(`${k}=${_chvEncodeComponent(f[k])}`);
  }
  if (extra) parts.push(extra);
  return parts.join("&");
}

function _chvBindBatchDelete(body) {
  const button = body.querySelector("[data-chv-batchdel]");
  if (!button) return;
  button.addEventListener("click", async () => {
    if (!_chvHasAllPermissions("patient.profile.view", "return_visit.delete")) return false;
    if (_chvPageWriteState.writeInFlight) return;
    const selected = [...body.querySelectorAll(".chv-ck:checked")];
    const ids = selected.map(item => item.value);
    if (!ids.length) { window.alert("先勾选要删除的回访"); return; }
    if (!window.confirm(`确认批量删除 ${ids.length} 条回访？系统会保留版本和审计记录。`)) return;
    const revisions = Object.fromEntries(selected.map(item => [item.value, Number(item.dataset.chvRevision)]));
    let result;
    try {
      result = await _chvWriteOnPage(body, button, "returnVisit", {
        action: "batchDelete",
        ids,
        revisions,
      });
    } catch {
      window.alert("批量删除失败：网络异常，请显式重试");
      return;
    }
    if (result.busy) return;
    if (result.ok) { await loadCustomerHubV2(); return; }
    _chvWriteError(result, "批量删除");
    if (Number(result.status) !== 409) return;
    const refreshed = await loadCustomerHubV2();
    if (refreshed === false) {
      window.alert("批量删除冲突后列表刷新失败，请手动刷新并重新勾选；系统未重放删除请求");
    } else {
      window.alert("数据已更新，列表已刷新；请核对后重新勾选");
    }
  });
}

async function _chvRenderList(token) {
  if (!_chvHasAllPermissions("patient.profile.view", "return_visit.view")) return false;
  const body = document.getElementById("chvBody");
  let data;
  let doctorStaff = [], visitorStaff = [];
  let doctorStaffFailed = false, visitorStaffFailed = false;
  try {
    // status 空 = 后端「待回访(截至今日)」默认口径,不强制 all
    const listRequest = _chvFetchJson(
      `/api/return-visits?${_chvListQuery(`pageno=${_chvListPage}&pagesize=${_CHV_PAGESIZE}`)}`);
    const doctorRequest = _chvLoadStaffDirectory("doctor")
      .then(items => { doctorStaff = items; })
      .catch(() => { doctorStaffFailed = true; });
    const visitorRequest = _chvLoadStaffDirectory("visitor")
      .then(items => { visitorStaff = items; })
      .catch(() => { visitorStaffFailed = true; });
    [data] = await Promise.all([listRequest, doctorRequest, visitorRequest]);
    data = _chvContractApi().normalizeReturnVisitList(data);
    if (token !== _chvLifecycleGeneration) return;
  } catch {
    if (token === _chvLifecycleGeneration) body.innerHTML = `<div class="module-loading">回访列表载入失败</div>`;
    return false;
  }
  const opts = (staff, cur) => ['<option value="">全部</option>']
    .concat([...new Set(staff.map(s => s.name).filter(Boolean))].map(n =>
      `<option${n === cur ? " selected" : ""}>${escapeHtml(n)}</option>`)).join("");
  const rangeText = (_chvF.date_from || _chvF.date_to)
    ? `${_chvF.date_from || "…"} ~ ${_chvF.date_to || "…"}` : "全部日期";
  const pendingChipText = _chvF.date_to
    ? `待回访（截至 ${_chvF.date_to}）` : "待回访（截至今日）";
  const attIcons = r => {
    const a = [];
    if (r.has_recording) a.push('<span title="有录音">🎙</span>');
    if (r.has_screenshot) a.push('<span title="有截图">📷</span>');
    if (r.has_image) a.push('<span title="有影像">🩻</span>');
    return a.length ? a.join(" ") : '<span style="color:#b9c6ca">—</span>';
  };
  const trs = data.list.map(r => {
    return `
    <tr class="chv-row" data-chv-open="${escapeAttr(r.return_visit_id)}" data-chv-pid="${escapeAttr(r.patient_identity || "")}">
      <td data-chv-stop="1"><input type="checkbox" class="chv-ck" value="${escapeAttr(r.return_visit_id)}" data-chv-revision="${escapeAttr(r.revision)}"></td>
      <td>${escapeHtml(r.due_time || "")}</td>
      <td>${_chvName(r.patient_identity, r.display_name)}</td>
      <td>${escapeHtml(r.item_name || "")}</td>
      <td class="chv-clip">${escapeHtml(r.note || "—")}</td>
      <td class="chv-clip">${escapeHtml(r.return_result || "—")}</td>
      <td>主治:${escapeHtml(r.responsible_doctor || "—")} · 访:${escapeHtml(r.visitor || "—")}</td>
      <td>${escapeHtml(_chvWayText(r.channel, ""))}</td>
      <td style="font-size:14px">${attIcons(r)}</td>
      <td>${_chvStatusBadge(r)}</td>
    </tr>`;
  }).join("");
  body.innerHTML = `
    <section class="chv-panel chv-glass">
      <div class="chv-phead"><h3>回访列表</h3><span>${_chvPendingChip ? `<span class="chv-badge chv-b-teal">${escapeHtml(pendingChipText)}</span> ` : ""}${escapeHtml(String(data.totalcount))} 条</span></div>
      <div class="chv-fbar">
        <label>回访日期<button type="button" class="chv-btn" data-chv-range="1">📅 ${escapeHtml(rangeText)}</button></label>
        <label>状态<select data-chv-f="status">
          ${["", "all", "待回访", "已回访", "做计划", "待跟进"].map(s =>
            `<option value="${s}"${s === _chvF.status ? " selected" : ""}>${s === "" ? "待回访(默认)" : s === "all" ? "全部" : s}</option>`).join("")}
        </select></label>
        <label>主治医生${doctorStaffFailed
          ? `<input data-chv-f="doctor" value="${escapeAttr(_chvF.doctor)}" placeholder="员工名录载入失败,手填">`
          : `<select data-chv-f="doctor">${opts(doctorStaff, _chvF.doctor)}</select>`}</label>
        <label>回访人${visitorStaffFailed
          ? `<input data-chv-f="visitor" value="${escapeAttr(_chvF.visitor)}" placeholder="员工名录载入失败,手填">`
          : `<select data-chv-f="visitor">${opts(visitorStaff, _chvF.visitor)}</select>`}</label>
        <label>搜索<input data-chv-f="q" value="${escapeAttr(_chvF.q)}" placeholder="患者/事项/结果/首拼/病历号"></label>
        <button type="button" class="chv-btn pri" data-chv-search="1">查询</button>
        <button type="button" class="chv-btn" data-chv-adv="1">高级查询</button>
        ${hasPerm("return_visit.delete") ? `<button type="button" class="chv-btn danger" data-chv-batchdel="1">批量删除</button>` : ""}
        ${hasPerm("patient.profile.export") ? `<button type="button" class="chv-btn" data-chv-export="1">📥 导出 Excel</button>` : ""}
      </div>
      <table class="chv-table"><thead><tr><th></th><th>时间</th><th>患者</th><th>事项</th><th>回访内容</th><th>回访结果</th><th>人员</th><th>方式</th><th>附件</th><th>状态</th></tr></thead>
      <tbody>${trs || `<tr><td colspan="10" class="chv-empty">无符合条件的回访</td></tr>`}</tbody></table>
      ${_chvPagerHtml(data.totalcount, _chvListPage)}
    </section>`;
  _chvBindCommon(body, null);
  body.querySelectorAll("[data-chv-f]").forEach(el => el.addEventListener("change", () => {
    _chvF[el.dataset.chvF] = el.value.trim();
  }));
  const search = () => {
    _chvPendingChip = false; _chvListPage = 1;
    body.querySelectorAll("[data-chv-f]").forEach(el => { _chvF[el.dataset.chvF] = el.value.trim(); });
    loadCustomerHubV2();
  };
  body.querySelector("[data-chv-search]").addEventListener("click", search);
  body.querySelector("[data-chv-adv]").addEventListener("click", _chvOpenAdv);
  const exp = body.querySelector("[data-chv-export]");
  if (exp) exp.addEventListener("click", () => {
    window.open(`/api/return-visits/export?${_chvListQuery("")}`, "_blank");   // 与列表同口径(#124)
  });
  _chvBindBatchDelete(body);
  body.querySelectorAll("[data-chv-stop]").forEach(td => td.addEventListener("click", e => e.stopPropagation()));
  _chvBindPager(body, data.totalcount, _chvListPage, n => { _chvListPage = n; loadCustomerHubV2(); });
  return true;
}

// ============ 沟通记录台账 ============

async function _chvRenderComm(token) {
  if (!_chvHasAllPermissions("patient.profile.view", "communication.view")) return false;
  const body = document.getElementById("chvBody");
  let data;
  try {
    // 沟通台账用自己的筛选(不静默复用回访列表 _chvF,免看着像残缺台账)
    const parts = [`pageno=${_chvCommPage}`, `pagesize=${_CHV_PAGESIZE}`];
    if (_chvCommF.date_from) parts.push(`date_from=${_chvEncodeComponent(_chvCommF.date_from)}`);
    if (_chvCommF.date_to) parts.push(`date_to=${_chvEncodeComponent(_chvCommF.date_to)}`);
    if (_chvCommF.q) parts.push(`q=${_chvEncodeComponent(_chvCommF.q)}`);
    data = await _chvFetchJson(`/api/communications?${parts.join("&")}`);
    data = _chvContractApi().normalizeCommunicationList(data);
    if (token !== _chvLifecycleGeneration) return;
  } catch {
    if (token === _chvLifecycleGeneration) body.innerHTML = `<div class="module-loading">沟通记录载入失败</div>`;
    return false;
  }
  const trs = data.list.map(c => `
    <tr>
      <td>${escapeHtml(c.contacted_at || "")}</td>
      <td>${_chvName(c.patient_identity, c.display_name)}</td>
      <td>${escapeHtml(_chvWayText(c.channel, c.direction))}</td>
      <td>${c.reason ? `<span class="chv-badge chv-b-teal">${escapeHtml(c.reason)}</span>` : "—"}</td>
      <td>${escapeHtml(c.operator || "")}</td>
      <td class="chv-clip">${escapeHtml(c.content || "")}</td>
      <td>${escapeHtml(c.deal_status || "—")}</td>
    </tr>`).join("");
  const addCommBtn = hasPerm("patient.profile.view") && hasPerm("communication.manage")
    ? `<button type="button" class="chv-btn" data-chv-addcomm="1">＋ 记沟通</button>` : "";
  const cf = _chvCommF;
  body.innerHTML = `
    <section class="chv-panel chv-glass">
      <div class="chv-phead"><h3>沟通记录</h3>
        <span><span class="chv-badge chv-b-teal">${escapeHtml(String(data.totalcount))} 条</span>
        ${addCommBtn}</span></div>
      <div class="chv-fbar">
        <label>从<input type="date" data-cf="date_from" value="${escapeAttr(cf.date_from)}"></label>
        <label>到<input type="date" data-cf="date_to" value="${escapeAttr(cf.date_to)}"></label>
        <label>搜索<input data-cf="q" value="${escapeAttr(cf.q)}" placeholder="患者/内容/事由/沟通人"></label>
        <button type="button" class="chv-btn pri" data-chv-commsearch="1">查询</button>
        ${(cf.date_from || cf.date_to || cf.q) ? `<button type="button" class="chv-btn" data-chv-commclear="1">清除</button>` : ""}
      </div>
      <table class="chv-table"><thead><tr><th>时间</th><th>患者</th><th>方式</th><th>干什么</th><th>沟通人</th><th>聊了什么</th><th>成交</th></tr></thead>
      <tbody>${trs || `<tr><td colspan="7" class="chv-empty">无沟通记录</td></tr>`}</tbody></table>
      ${_chvPagerHtml(data.totalcount, _chvCommPage)}
    </section>`;
  _chvBindCommon(body, null);
  const commSearch = () => {
    body.querySelectorAll("[data-cf]").forEach(el => { _chvCommF[el.dataset.cf] = el.value.trim(); });
    _chvCommPage = 1; loadCustomerHubV2();
  };
  body.querySelector("[data-chv-commsearch]").addEventListener("click", commSearch);
  const cclr = body.querySelector("[data-chv-commclear]");
  if (cclr) cclr.addEventListener("click", () => { _chvCommF = {date_from: "", date_to: "", q: ""}; _chvCommPage = 1; loadCustomerHubV2(); });
  _chvBindPager(body, data.totalcount, _chvCommPage, n => { _chvCommPage = n; loadCustomerHubV2(); });
}

// ============ 公共交互绑定 ============

function _chvBindCommon(root, todayData) {
  // 点名字进个人主页;点行开弹窗;▶ 播录音
  root.addEventListener("click", async e => {
    if (_chvPageWriteState.writeInFlight) return;
    const home = e.target.closest("[data-chv-home]");
    if (home) {
      e.stopPropagation();
      if (home.dataset.chvHome) openPatientWorkspace(home.dataset.chvHome, "return-visits");
      return;
    }
    const play = e.target.closest("[data-chv-play]");
    if (play) {
      e.stopPropagation();
      _chvPlayRecording(play.dataset.chvPlay, play.dataset.chvPid2);
      return;
    }
    const row = e.target.closest("[data-chv-open]");
    if (row) _chvOpenRvModal(row.dataset.chvOpen, row.dataset.chvPid);
  });
  const cal = root.querySelector("[data-chv-cal]");
  // 有回访查看权 → 弹带任务计数的月历;否则(只看沟通/录音) → 弹不依赖回访权限的纯日期选择
  if (cal) cal.addEventListener("click", hasPerm("return_visit.view") ? _chvOpenCalendar : _chvOpenPlainDate);
  const dprev = root.querySelector("[data-chv-dprev]");
  if (dprev) dprev.addEventListener("click", () => _chvShiftDay(-1));
  const dnext = root.querySelector("[data-chv-dnext]");
  if (dnext) dnext.addEventListener("click", () => _chvShiftDay(1));
  const dtoday = root.querySelector("[data-chv-dtoday]");
  if (dtoday) dtoday.addEventListener("click", () => { _chvDay = ""; loadCustomerHubV2(); });
  // 今日回访双筛选(回访人/医生,默认全部)
  const fv = root.querySelector("[data-chv-fvisitor]");
  const fd = root.querySelector("[data-chv-fdoctor]");
  const applyRvFilter = () => {
    const v = fv ? fv.value : "";
    const doc = fd ? fd.value : "";
    root.querySelectorAll("tr[data-visitor]").forEach(tr => {
      const show = (!v || tr.dataset.visitor === v) && (!doc || tr.dataset.doctor === doc);
      tr.hidden = !show;
    });
  };
  if (fv) fv.addEventListener("change", applyRvFilter);
  if (fd) fd.addEventListener("change", applyRvFilter);
  const rec = root.querySelector("[data-chv-recs]");
  if (rec) rec.addEventListener("click", () => _chvOpenRecordings(todayData));
  const mr = root.querySelector("[data-chv-mr]");
  if (mr) mr.addEventListener("click", () => _chvOpenMonthlyReview());
  const kpiNew = root.querySelector("[data-chv-kpinew]");
  if (kpiNew) kpiNew.addEventListener("click", () => _chvOpenNewRv());
  root.querySelectorAll("[data-chv-addcomm]").forEach(b => b.addEventListener("click", () => _chvOpenAddComm("")));
}

// 行内播放:懒取该回访挂链的录音(calls.linked_return_visit_id)
async function _chvPlayRecording(rvId, pid) {
  if (!_chvHasAllPermissions("patient.profile.view", "call_record.view")) return false;
  if (!pid) { window.alert("该回访未关联患者,无录音"); return; }
  const lifecycleToken = _chvCaptureLifecycleToken();
  let calls;
  try {
    calls = (await _chvFetchJson(`/api/patients/${_chvEncodeComponent(pid)}/calls`)).calls || [];
  } catch {
    if (_chvLifecycleIsCurrent(lifecycleToken)) window.alert("读取录音失败(可能无录音查看权限)");
    return;
  }
  if (!_chvLifecycleIsCurrent(lifecycleToken)) return false;
  const linked = calls.filter(k => k.linked_return_visit_id === rvId);
  if (!linked.length) { window.alert("该回访暂无录音"); return; }
  const url = `/api/calls/${_chvEncodeComponent(linked[0].call_id)}/recording`;
  new Audio(url).play().catch(() => {
    if (_chvLifecycleIsCurrent(lifecycleToken)) window.alert("播放失败");
  });
}

// ============ #856二期改向 上传即转:回访弹窗自动填「回访内容」(规格§2) ============
// 转写文本全界面不展示;成功且未动过才填并标记待保存;失败/超时诚实提示;弹窗关闭即停轮询。

// 三期②:AI 辅助要点区(只读,绝不写入表单字段);pending 不出现,中/败两态诚实提示
function _chvRenderSummary(ovl, anchorEl, t) {
  let box = ovl.querySelector("[data-chv-summary]");
  if (!t || !t.summary_status || t.summary_status === "pending") {
    if (box) box.remove();
    return;
  }
  if (!box) {
    box = document.createElement("div");
    box.className = "chv-summary";
    box.dataset.chvSummary = "1";
    anchorEl.closest("label").after(box);
  }
  if (t.summary_status === "running") {
    box.innerHTML = `<div class="chv-sub">要点提炼中…</div>`;
    return;
  }
  if (t.summary_status === "failed") {
    box.innerHTML = `<div class="chv-sub">要点提炼失败</div>`;
    return;
  }
  const s = t.summary || {};
  box.innerHTML = `<div class="chv-sum-head">AI 辅助，须医生确认</div>
    <div class="chv-sum-line"><b>意向</b> ${escapeHtml(s.intent || "")}</div>
    <div class="chv-sum-line"><b>风险</b> ${escapeHtml(s.risk || "")}</div>
    <div class="chv-sum-line"><b>建议</b> ${escapeHtml(s.suggestion || "")}</div>`;
}

async function _chvMaybeFillTranscript(ovl, rvId, pid) {
  if (!_chvHasAllPermissions("patient.profile.view", "call_record.view")) return false;
  if (!pid) return;
  const noteEl = ovl.querySelector("#chvMC");
  if (!noteEl) return;
  let calls;
  try { calls = (await _chvFetchJson(`/api/patients/${_chvEncodeComponent(pid)}/calls`)).calls || []; }
  catch { return; }   // 录音列表读取失败:静默缺席(录音本就缺席语义),不阻断弹窗
  const linked = calls.filter(k => k.linked_return_visit_id === rvId);
  if (!linked.length) return;
  const transcriptUrl = `/api/calls/${_chvEncodeComponent(linked[0].call_id)}/transcript`;
  const hint = document.createElement("div");
  hint.className = "chv-note";
  const showHint = txt => {
    hint.textContent = txt;
    if (!hint.isConnected) noteEl.closest("label").after(hint);
  };
  let delay = 2000, waited = 0;          // 轮询参数(冻结):2s 起步,退避上限 10s,总上限 610s
  const MAX_WAIT = 610000;
  for (;;) {
    if (!document.body.contains(ovl)) return;          // 弹窗关闭(任何路径)即停轮询
    let t;
    try { t = await _chvFetchJson(transcriptUrl); }
    catch { return; }
    if (t && t.status === "succeeded") {
      // 未动过才填:空串 或 等于上次自动填入候选(字段级脏标记 ovl._chvNoteDirty 由 input 置位)
      if (!ovl._chvNoteDirty && (!noteEl.value.trim() || noteEl.value === ovl._chvNoteAuto)) {
        noteEl.value = t.text || "";
        ovl._chvNoteAuto = t.text || "";
        ovl._chvDirty = true;                          // 标记待保存(关闭守卫/保存按钮同口径)
      }
      _chvRenderSummary(ovl, noteEl, t);               // 三期②:要点区随状态刷新
      if (t.summary_status !== "running") return;      // 提炼终态即收;running 则按同参数续等
    } else if (t && t.status === "failed") { showHint("录音转写失败"); return; }
    else if (waited >= MAX_WAIT) { showHint("录音转写超时"); return; }
    else showHint("录音转写中…");
    await new Promise(r => setTimeout(r, delay));
    waited += delay;
    delay = Math.min(delay * 2, 10000);
  }
}

// ============ #856三期 下次回访制度预填:ReturnPlan 字典(诊所现行回访制度) ============
// 制度来源=现有字典接口 dict_type=ReturnPlan(name+天数),不新建路由;事项关键词命中排前,不自动选。
let _chvPlans = null;

async function _chvLoadReturnPlans(itemName) {
  if (!hasPerm("master_data.view")) throw new Error("无权限，回访制度不可用");
  if (_chvPlans === null) {
    const payload = await _chvFetchJson("/api/dictionaries?type=ReturnPlan");
    if (!payload || typeof payload !== "object" || Array.isArray(payload)
        || !Array.isArray(payload.items)) {
      throw new TypeError("invalid_return_plan_payload");
    }
    const normalized = payload.items.map(item => {
      if (!item || typeof item !== "object" || Array.isArray(item)
          || typeof item.name !== "string" || !item.name.trim()) {
        throw new TypeError("invalid_return_plan_item");
      }
      const rawDays = item.value;
      const days = typeof rawDays === "number"
        ? rawDays
        : typeof rawDays === "string" && /^[+-]?\d+$/.test(rawDays.trim())
          ? Number(rawDays.trim()) : Number.NaN;
      if (!Number.isSafeInteger(days)) throw new TypeError("invalid_return_plan_days");
      return {name: item.name.trim(), days};
    });
    _chvPlans = normalized;
  }
  const KW = ["洁牙", "拔牙", "根管", "种植", "矫正", "正畸", "戴牙", "补牙", "备牙", "助萌",
              "阻生", "骨增量", "支抗钉", "隐形", "即刻负重", "镶牙", "消炎", "治疗"];
  const hit = KW.find(k => (itemName || "").includes(k)) || "";
  return _chvPlans
    .map(p => ({...p}))
    .sort((a, b) => {
      const ah = hit && a.name.includes(hit) ? 0 : 1;
      const bh = hit && b.name.includes(hit) ? 0 : 1;
      return ah - bh || a.days - b.days;
    });
}

// ============ #856三期③A 桌面麦克风直录(MediaRecorder→既有上传→自动转写) ============
// 环境不支持/未授权都诚实提示;上传中防重;弹窗上下文自动挂 linked_return_visit_id。
function _chvMicButton(pid, rvId, getRevision, setRevision, ovl) {
  const btn = document.createElement("button");
  btn.type = "button";
  btn.className = "chv-btn";
  btn.dataset.chvMic = "1";
  btn.textContent = "🎙 录音";
  const supported = !!(rvId && navigator.mediaDevices && navigator.mediaDevices.getUserMedia && window.MediaRecorder);
  if (!supported) {
    btn.disabled = true;
    btn.title = rvId ? "此浏览器不支持录音" : "请从具体回访记录录音";
    return btn;
  }
  let activeSession = null;
  let pendingStart = null;
  let pendingBlob = null;
  let revisionRecovery = null;   // retry=409 后刷新再显式重传; confirm=200 待确认,绝不重传
  let timer = null;
  let starting = false;
  let busy = false;
  let disposed = false;
  let uploadQuarantined = false;
  const controlDisabledStates = new Map();

  const stopTracks = session => {
    if (!session || session.tracksStopped) return;
    session.tracksStopped = true;
    session.stream.getTracks().forEach(track => track.stop());
  };
  const revisionIsNew = (candidate, expected) => (
    Number.isSafeInteger(candidate) && candidate > expected
  );
  const setMicBusy = pending => {
    if (ovl) ovl._chvMicBusy = pending;
  };
  const setUploadPending = pending => {
    busy = pending;
    if (ovl) ovl._chvMicPending = pending;
    setMicBusy(pending);
    if (pending) {
      const controls = [btn];
      if (ovl) controls.push(...ovl.querySelectorAll("input, select, textarea, button"));
      [...new Set(controls)].forEach(control => {
        if (!controlDisabledStates.has(control)) controlDisabledStates.set(control, control.disabled);
        control.disabled = true;
      });
    } else {
      controlDisabledStates.forEach((disabled, control) => { control.disabled = disabled; });
      controlDisabledStates.clear();
    }
  };
  const quarantineUpload = () => {
    uploadQuarantined = true;
    if (!disposed) {
      window.alert("录音上传结果不确定，请关闭后重新打开该回访确认，请勿重复上传");
      btn.textContent = "上传结果不确定·请关闭确认";
    }
  };
  const finishConfirmedUpload = () => {
    pendingBlob = null;
    revisionRecovery = null;
    if (ovl) ovl._chvMediaDirty = false;
    if (!disposed) {
      btn.textContent = "已保存·转写中…";
      setTimeout(() => {
        if (!disposed && !pendingBlob && !busy) btn.textContent = "🎙 录音";
      }, 2500);
    }
  };
  const refreshRevision = async alreadyPending => {
    const recovery = revisionRecovery;
    if (!pendingBlob || !recovery || disposed || (!alreadyPending && busy)) return false;
    if (!alreadyPending) setUploadPending(true);
    if (!disposed) {
      btn.textContent = recovery.kind === "confirm" ? "确认上传结果中…" : "刷新版本中…";
    }
    try {
      const latest = await _chvFetchJson(
        `/api/return-visits/${_chvEncodeComponent(rvId)}`,
      );
      if (disposed) return false;
      const latestRevision = latest && latest.revision;
      if (!revisionIsNew(latestRevision, recovery.expectedRevision)
          || typeof setRevision !== "function") {
        throw new Error("invalid_return_visit_revision");
      }
      if (recovery.kind === "confirm") {
        setRevision(latestRevision);
        finishConfirmedUpload();
      } else {
        setRevision(latestRevision);
        revisionRecovery = null;
        btn.textContent = "↻ 重试上传";
      }
      return true;
    } catch {
      if (!disposed) {
        if (recovery.kind === "confirm") {
          window.alert("录音上传结果确认失败，请重试");
          btn.textContent = "↻ 确认上传结果";
        } else {
          window.alert("录音保存冲突，刷新回访版本失败，请重试");
          btn.textContent = "↻ 刷新版本";
        }
      }
      return false;
    } finally {
      if (!alreadyPending) setUploadPending(false);
    }
  };
  const uploadPendingBlob = async () => {
    if (!pendingBlob || busy || disposed || revisionRecovery || uploadQuarantined) return;
    const blob = pendingBlob;
    setUploadPending(true);
    btn.textContent = "保存中…";
    try {
      const expectedRevision = typeof getRevision === "function" ? getRevision() : 0;
      if (!Number.isSafeInteger(expectedRevision) || expectedRevision <= 0) {
        revisionRecovery = {kind: "retry", expectedRevision: 0};
        await refreshRevision(true);
        return;
      }
      const result = await _chvWrite("call", {
        action: "attachment",
        patientIdentity: pid,
        linkedReturnVisitId: rvId,
        expectedRevision: getRevision(),
        media: blob,
        fileName: "desktop-mic.webm",
        direction: "outbound",
        source: "desktop_mic",
      });
      if (disposed) return;
      if (!result.ok) {
        if (Number(result.status) === 0) {
          quarantineUpload();
          return;
        }
        _chvWriteError(result, "录音保存");
        if (Number(result.status) === 409) {
          revisionRecovery = {kind: "retry", expectedRevision};
          await refreshRevision(true);
          return;
        }
        if (!disposed) btn.textContent = "↻ 重试上传";
        return;
      }
      const nextRevision = result.response && result.response.data
        ? result.response.data.context_revision : undefined;
      if (revisionIsNew(nextRevision, expectedRevision) && typeof setRevision === "function") {
        setRevision(nextRevision);
        finishConfirmedUpload();
        return;
      }
      revisionRecovery = {kind: "confirm", expectedRevision};
      await refreshRevision(true);
    } catch {
      quarantineUpload();
    } finally {
      setUploadPending(false);
      if (uploadQuarantined && !disposed) btn.disabled = true;
    }
  };
  const abortMic = () => {
    if (disposed) return;
    disposed = true;
    if (pendingStart) pendingStart.cancelled = true;
    pendingBlob = null;
    revisionRecovery = null;
    if (ovl) {
      ovl._chvMediaDirty = false;
      ovl._chvMicPending = false;
    }
    setMicBusy(false);
    if (activeSession) {
      activeSession.cancelled = true;
      stopTracks(activeSession);
      if (activeSession.recorder.state === "recording") activeSession.recorder.stop();
    }
    clearInterval(timer);
    timer = null;
  };
  if (ovl) {
    const previousAbort = ovl._chvAbort;
    let abortsRun = false;
    ovl._chvAbort = () => {
      if (abortsRun) return;
      abortsRun = true;
      abortMic();
      if (typeof previousAbort === "function") previousAbort();
    };
  }

  btn.addEventListener("click", async () => {
    if (!_chvHasAllPermissions("patient.profile.view", "call_record.manage")) return false;
    if (uploadQuarantined) {
      quarantineUpload();
      return false;
    }
    if (busy || starting || disposed
        || (ovl && ovl._chvWriteState
          && (ovl._chvWriteState.writeInFlight || ovl._chvWriteState.operationInFlight))) return;
    if (pendingBlob) {
      if (revisionRecovery) return refreshRevision(false);
      return uploadPendingBlob();
    }
    if (activeSession && activeSession.recorder.state === "recording") {
      activeSession.recorder.stop();
      return;
    }
    const start = {cancelled: false};
    pendingStart = start;
    starting = true;
    setMicBusy(true);
    btn.disabled = true;
    btn.textContent = "正在开麦…";
    let stream;
    try { stream = await navigator.mediaDevices.getUserMedia({audio: true}); }
    catch {
      if (!start.cancelled && !disposed) window.alert("未授权麦克风");
      if (pendingStart === start) pendingStart = null;
      starting = false;
      setMicBusy(false);
      if (!disposed) { btn.disabled = false; btn.textContent = "🎙 录音"; }
      return;
    }
    if (start.cancelled || disposed || pendingStart !== start) {
      stream.getTracks().forEach(track => track.stop());
      starting = false;
      setMicBusy(false);
      if (!disposed) { btn.disabled = false; btn.textContent = "🎙 录音"; }
      return;
    }
    pendingStart = null;
    starting = false;
    btn.disabled = false;
    const session = {stream, recorder: null, chunks: [], cancelled: false, tracksStopped: false};
    let recorder;
    try {
      recorder = new MediaRecorder(stream);
      session.recorder = recorder;
      activeSession = session;
      recorder.ondataavailable = event => {
        if (event.data && event.data.size) session.chunks.push(event.data);
      };
      recorder.onstop = async () => {
        stopTracks(session);
        clearInterval(timer);
        timer = null;
        if (activeSession === session) activeSession = null;
        if (session.cancelled || disposed) {
          if (!disposed) setMicBusy(false);
          return;
        }
        btn.textContent = "🎙 录音";
        const blob = new Blob(session.chunks, {type: recorder.mimeType || "audio/webm"});
        if (!blob.size) { setMicBusy(false); return; }
        pendingBlob = blob;
        if (ovl) ovl._chvMediaDirty = true;
        await uploadPendingBlob();
      };
      recorder.start();
    } catch {
      session.cancelled = true;
      stopTracks(session);
      if (activeSession === session) activeSession = null;
      if (recorder && recorder.state === "recording") recorder.stop();
      clearInterval(timer);
      timer = null;
      setMicBusy(false);
      if (!disposed) {
        btn.disabled = false;
        btn.textContent = "🎙 录音";
        window.alert("录音启动失败，请重试");
      }
      return;
    }
    let secs = 0;
    btn.textContent = "■ 停止 0s";
    timer = setInterval(() => { secs += 1; btn.textContent = `■ 停止 ${secs}s`; }, 1000);
  });
  return btn;
}

// ============ 弹层基建 ============

function _chvHiddenThrough(element, boundary) {
  for (let current = element; current && current !== boundary; current = current.parentElement) {
    if (current.hidden || current.getAttribute("aria-hidden") === "true") return true;
    const inlineDisplay = String(current.style && current.style.display || "").toLowerCase();
    const inlineVisibility = String(current.style && current.style.visibility || "").toLowerCase();
    if (inlineDisplay === "none" || inlineVisibility === "hidden") return true;
    if (typeof window.getComputedStyle === "function") {
      try {
        const computed = window.getComputedStyle(current);
        if (String(computed.display || "").toLowerCase() === "none"
            || String(computed.visibility || "").toLowerCase() === "hidden") return true;
      } catch {}
    }
  }
  return false;
}

function _chvProgrammaticallyFocusableElement(element, boundary) {
  if (!element || element.isConnected === false || _chvHiddenThrough(element, boundary)) return false;
  let disabled = element.disabled === true || element.getAttribute("disabled") !== null;
  let nativeDisabledChecked = false;
  if (!disabled && typeof element.matches === "function") {
    try {
      disabled = element.matches(":disabled");
      nativeDisabledChecked = true;
    } catch {}
  }
  if (!disabled && !nativeDisabledChecked) {
    for (let current = element.parentElement; current && current !== boundary; current = current.parentElement) {
      if (String(current.tagName || "").toLowerCase() === "fieldset"
          && (current.disabled === true || current.getAttribute("disabled") !== null)) {
        disabled = true;
        break;
      }
    }
  }
  if (disabled) return false;
  const tag = String(element.tagName || "").toLowerCase();
  if (tag === "input" && element.type === "hidden") return false;
  return typeof element.focus === "function";
}

function _chvFocusableElement(element, boundary) {
  if (!_chvProgrammaticallyFocusableElement(element, boundary)) return false;
  const tag = String(element.tagName || "").toLowerCase();
  const tabindex = element.getAttribute("tabindex");
  if (tabindex !== null && Number(tabindex) < 0) return false;
  if (["button", "input", "select", "textarea"].includes(tag)) return true;
  if (tag === "a" && element.getAttribute("href") !== null) return true;
  return tabindex !== null;
}

function _chvDialogFocusables(ovl) {
  const dialog = ovl && ovl.querySelector(".chv-pop");
  return [...ovl.querySelectorAll("button, a[href], input, select, textarea, [tabindex]")]
    .filter(element => _chvFocusableElement(element, dialog));
}

function _chvDisposeOverlay(ovl) {
  if (!ovl || ovl._chvDisposed) return;
  ovl._chvDisposed = true;
  if (typeof ovl._chvAbort === "function") ovl._chvAbort();
  ovl.remove();
  const opener = ovl._chvOpener;
  if (_chvProgrammaticallyFocusableElement(opener)) opener.focus();
}

function _chvOvl(id, inner, wide) {
  let ovl = document.getElementById(id);
  if (ovl) _chvDisposeOverlay(ovl);
  const opener = document.activeElement;
  ovl = document.createElement("div");
  ovl.className = "chv-ovl";
  ovl.id = id;
  ovl.innerHTML = `<div class="chv-pop${wide ? " wide" : ""}">${inner}</div>`;
  ovl._chvWriteState = {};
  ovl._chvOpener = opener;
  const dialog = ovl.querySelector(".chv-pop");
  const title = dialog && dialog.querySelector("[data-chv-dialog-title], h1, h2, h3, h4, h5, h6");
  if (dialog) {
    dialog.setAttribute("role", "dialog");
    dialog.setAttribute("aria-modal", "true");
    dialog.setAttribute("tabindex", "-1");
    if (title) {
      const titleId = `chv-dialog-title-${++_chvDialogSeq}`;
      title.setAttribute("id", titleId);
      dialog.setAttribute("aria-labelledby", titleId);
    }
  }
  // 关闭前脏守卫:编辑过表单点背景/关闭要 confirm,防误丢未保存输入
  ovl._chvTryClose = () => {
    if (_chvOverlayPending(ovl)) return false;
    if (((ovl._chvGuarded && ovl._chvDirty && !ovl._chvClean) || ovl._chvMediaDirty)
        && !window.confirm("有未保存的修改,确定关闭并丢弃?")) return false;
    _chvDisposeOverlay(ovl);
    return true;
  };
  ovl.addEventListener("click", e => { if (e.target === ovl) ovl._chvTryClose(); });
  ovl.querySelectorAll("[data-chv-close]").forEach(b => b.addEventListener("click", ovl._chvTryClose));
  ovl.addEventListener("keydown", e => {
    if (e.key === "Escape") {
      e.preventDefault();
      e.stopPropagation();
      ovl._chvTryClose();
      return;
    }
    if (e.key !== "Tab") return;
    const focusables = _chvDialogFocusables(ovl);
    if (!focusables.length) {
      e.preventDefault();
      dialog.focus();
      return;
    }
    if (focusables.length === 1) {
      e.preventDefault();
      focusables[0].focus();
      return;
    }
    const active = document.activeElement;
    if ((!e.shiftKey && active === focusables[focusables.length - 1])
        || (e.shiftKey && active === focusables[0])) {
      e.preventDefault();
      focusables[e.shiftKey ? focusables.length - 1 : 0].focus();
    }
  });
  document.body.appendChild(ovl);
  const focusables = _chvDialogFocusables(ovl);
  (focusables[0] || dialog).focus();
  return ovl;
}

function _chvLeavePatientOverlay(ovl, patientId) {
  if (!ovl || _chvOverlayPending(ovl)) return false;
  if (((ovl._chvDirty && !ovl._chvClean) || ovl._chvMediaDirty)
      && !window.confirm("有未保存的修改,确定离开?")) return false;
  _chvDisposeOverlay(ovl);
  openPatientWorkspace(patientId, "return-visits");
  return true;
}

// 标记弹窗需脏守卫:任一 [data-chv-dirty] 或表单控件被改动即置脏;保存成功置 ovl._chvClean=true
function _chvGuardDirty(ovl) {
  ovl._chvGuarded = true;
  ovl._chvDirty = false;
  ovl.addEventListener("input", () => { ovl._chvDirty = true; });
  ovl.addEventListener("change", () => { ovl._chvDirty = true; });
}

// ============ 月历弹卡(选日,整页跟随) ============

function _chvCalendarLocalDate(year, monthIndex, day) {
  const date = new Date(2000, 0, 1, 12, 0, 0, 0);
  date.setFullYear(year, monthIndex, day);
  return date;
}

// 纯日期选择(无回访权限时用,不打受 return_visit.view 保护的月历计数接口)
function _chvOpenPlainDate() {
  const ovl = _chvOvl("chvPlainDateOvl", `
    <div class="chv-phead"><h3>选日期</h3><button type="button" class="chv-btn" data-chv-close="1">✕</button></div>
    <div class="chv-pbody chv-form2">
      <label>日期<input type="date" id="chvPd" value="${escapeAttr(_chvToday())}"></label>
      <div class="chv-actions" style="grid-column:1/-1;justify-content:flex-end">
        <button type="button" class="chv-btn" data-chv-today="1">今天</button>
        <button type="button" class="chv-btn pri" data-chv-ok="1">确定</button>
      </div>
    </div>`);
  ovl.querySelector("[data-chv-today]").addEventListener("click", () => {
    _chvDay = ""; _chvDisposeOverlay(ovl); loadCustomerHubV2();
  });
  ovl.querySelector("[data-chv-ok]").addEventListener("click", () => {
    const v = ovl.querySelector("#chvPd").value;
    if (v) _chvDay = v;
    _chvDisposeOverlay(ovl); loadCustomerHubV2();
  });
}

async function _chvOpenCalendar() {
  if (!_chvHasAllPermissions("patient.profile.view", "return_visit.view")) return false;
  const lifecycleToken = _chvCaptureLifecycleToken();
  const day = _chvToday();
  const month = _chvCalMonth || day.slice(0, 7);
  let days;
  try {
    const api = _chvContractApi();
    const payload = await api.fetchJson(
      `/api/return-visits/calendar?month=${_chvEncodeComponent(month)}`,
      {fetch},
    );
    if (typeof api.normalizeCalendarDays !== "function") {
      throw new Error("calendar_days_contract_unavailable");
    }
    days = api.normalizeCalendarDays(payload, month);
  } catch {
    if (_chvLifecycleIsCurrent(lifecycleToken)) window.alert("月历载入失败");
    return;
  }
  if (!_chvLifecycleIsCurrent(lifecycleToken)) return false;
  const byDay = {};
  days.forEach(item => { byDay[item.date] = item; });
  const [y, m] = month.split("-").map(Number);
  const firstDow = (_chvCalendarLocalDate(y, m - 1, 1).getDay() + 6) % 7;
  const dcount = _chvCalendarLocalDate(y, m, 0).getDate();
  let cells = "";
  for (let i = 0; i < firstDow; i++) cells += `<span class="chv-day empty"></span>`;
  for (let d = 1; d <= dcount; d++) {
    const iso = `${month}-${String(d).padStart(2, "0")}`;
    const info = byDay[iso];
    cells += `<button type="button" class="chv-day${iso === day ? " on" : ""}" data-chv-day="${iso}">
      <span>${d}</span>${info ? `<span class="chv-cnt" title="待${info.pending}·已${info.done}·逾${info.overdue}">${info.count}</span>` : ""}</button>`;
  }
  const ovl = _chvOvl("chvCalOvl", `
    <div class="chv-phead"><h3>选日期 · ${escapeHtml(month)}</h3>
      <span><button type="button" class="chv-btn" data-chv-mprev="1">‹</button>
      <button type="button" class="chv-btn" data-chv-mtoday="1">今天</button>
      <button type="button" class="chv-btn" data-chv-mnext="1">›</button>
      <button type="button" class="chv-btn" data-chv-close="1">✕</button></span></div>
    <div class="chv-pbody">
      <div class="chv-week"><span>一</span><span>二</span><span>三</span><span>四</span><span>五</span><span>六</span><span>日</span></div>
      <div class="chv-days">${cells}</div>
      <div class="chv-note">数字=当天回访任务数;点哪天,总览整页切到哪天</div>
    </div>`);
  const shift = delta => {
    const nd = _chvCalendarLocalDate(y, m - 1 + delta, 1);
    _chvCalMonth = `${String(nd.getFullYear()).padStart(4, "0")}-${String(nd.getMonth() + 1).padStart(2, "0")}`;
    _chvDisposeOverlay(ovl); _chvOpenCalendar();
  };
  ovl.querySelector("[data-chv-mprev]").addEventListener("click", () => shift(-1));
  ovl.querySelector("[data-chv-mnext]").addEventListener("click", () => shift(1));
  ovl.querySelector("[data-chv-mtoday]").addEventListener("click", () => {
    _chvDay = ""; _chvCalMonth = ""; _chvDisposeOverlay(ovl); loadCustomerHubV2();
  });
  ovl.querySelectorAll("[data-chv-day]").forEach(b => b.addEventListener("click", () => {
    _chvDay = b.dataset.chvDay; _chvDisposeOverlay(ovl); loadCustomerHubV2();
  }));
}

// ============ 录音弹卡(今日总览:录音不平铺,按钮点开) ============

function _chvOpenRecordings(todayData) {
  const calls = (todayData && todayData.calls) || [];
  const rows = calls.map(k => `
    <div class="chv-audio">
      <button type="button" class="chv-mini" data-chv-callplay="${escapeAttr(k.call_id)}">▶</button>
      <div>${k.patient_identity ? _chvName(k.patient_identity, k.display_name) : "—(未匹配患者)"}
        · ${escapeHtml(k.direction === "inbound" ? "呼入" : "呼出")} · ${escapeHtml(k.peer_norm || "")}
        · ${escapeHtml((k.started_at || "").slice(11, 16))}</div>
      <span>${escapeHtml(String(k.duration_sec || 0))}s</span>
    </div>`).join("");
  const ovl = _chvOvl("chvRecOvl", `
    <div class="chv-phead"><h3>当日录音 · ${calls.length} 条</h3><button type="button" class="chv-btn" data-chv-close="1">✕</button></div>
    <div class="chv-pbody">${rows || '<div class="chv-empty">当天无录音</div>'}
      <div class="chv-note">患者自己的录音也在他的个人沟通里;点名字进个人主页</div></div>`);
  ovl.querySelectorAll("[data-chv-callplay]").forEach(b => b.addEventListener("click", () => {
    new Audio(`/api/calls/${_chvEncodeComponent(b.dataset.chvCallplay)}/recording`).play()
      .catch(() => window.alert("播放失败"));
  }));
  ovl.querySelectorAll("[data-chv-home]").forEach(el => el.addEventListener("click", () => {
    _chvDisposeOverlay(ovl); openPatientWorkspace(el.dataset.chvHome, "return-visits");
  }));
}

// ============ 月度回顾(真机验收#2):服务端月统计 ============

const _CHV_MONTHLY_SUMMARY_KEYS = [
  "total", "completed", "pending", "overdue", "completion_rate",
  "channel_counts", "problem_counts", "top_results", "narrative",
];
const _CHV_MONTHLY_CHANNELS = ["phone", "wechat", "other", "unknown"];
const _CHV_MONTHLY_PROBLEMS = ["needs_attention", "overdue", "low_satisfaction"];

function _chvMonthlyOwn(record, key) {
  if (!record || typeof record !== "object" || Array.isArray(record)) return undefined;
  const descriptor = Object.getOwnPropertyDescriptor(record, key);
  return descriptor && Object.prototype.hasOwnProperty.call(descriptor, "value")
    ? descriptor.value : undefined;
}

function _chvMonthlyExactKeys(record, keys) {
  if (!record || typeof record !== "object" || Array.isArray(record)) return false;
  const actual = Object.keys(record);
  return actual.length === keys.length && keys.every(key => actual.includes(key));
}

function _chvMonthlyCount(value, positive) {
  return typeof value === "number" && Number.isSafeInteger(value)
    && (positive ? value > 0 : value >= 0);
}

function _chvMonthlySummaryValid(summary) {
  if (!_chvMonthlyExactKeys(summary, _CHV_MONTHLY_SUMMARY_KEYS)) return false;
  const total = _chvMonthlyOwn(summary, "total");
  const completed = _chvMonthlyOwn(summary, "completed");
  const pending = _chvMonthlyOwn(summary, "pending");
  const overdue = _chvMonthlyOwn(summary, "overdue");
  const completionRate = _chvMonthlyOwn(summary, "completion_rate");
  if (![total, completed, pending, overdue].every(value => _chvMonthlyCount(value, false))) return false;
  if (typeof completionRate !== "number" || !Number.isFinite(completionRate)
      || completionRate < 0 || completionRate > 100) return false;
  if (total !== completed + pending || overdue > pending) return false;

  const channels = _chvMonthlyOwn(summary, "channel_counts");
  if (!Array.isArray(channels) || channels.length !== _CHV_MONTHLY_CHANNELS.length) return false;
  const seenChannels = new Set();
  let channelTotal = 0;
  for (const item of channels) {
    if (!_chvMonthlyExactKeys(item, ["channel", "count"])) return false;
    const channel = _chvMonthlyOwn(item, "channel");
    const count = _chvMonthlyOwn(item, "count");
    if (!_CHV_MONTHLY_CHANNELS.includes(channel) || seenChannels.has(channel)
        || !_chvMonthlyCount(count, false)) return false;
    seenChannels.add(channel);
    channelTotal += count;
  }
  if (seenChannels.size !== _CHV_MONTHLY_CHANNELS.length || channelTotal !== total) return false;

  const problems = _chvMonthlyOwn(summary, "problem_counts");
  if (!_chvMonthlyExactKeys(problems, _CHV_MONTHLY_PROBLEMS)
      || !_CHV_MONTHLY_PROBLEMS.every(key => _chvMonthlyCount(_chvMonthlyOwn(problems, key), false))) {
    return false;
  }

  const results = _chvMonthlyOwn(summary, "top_results");
  if (!Array.isArray(results) || results.length > 5 || results.some(item => (
    !_chvMonthlyExactKeys(item, ["label", "count"])
    || typeof _chvMonthlyOwn(item, "label") !== "string"
    || !_chvMonthlyOwn(item, "label").trim()
    || !_chvMonthlyCount(_chvMonthlyOwn(item, "count"), true)
  ))) return false;

  const narrative = _chvMonthlyOwn(summary, "narrative");
  return _chvMonthlyExactKeys(narrative, ["status", "text"])
    && ["disabled", "ready", "failed"].includes(_chvMonthlyOwn(narrative, "status"))
    && typeof _chvMonthlyOwn(narrative, "text") === "string";
}

async function _chvOpenMonthlyReview(month) {
  if (!_chvHasAllPermissions("patient.profile.view", "return_visit.view")) {
    window.alert("无回访查看权限");
    return false;
  }
  const lifecycleToken = _chvCaptureLifecycleToken();
  month = month || _chvToday().slice(0, 7);
  const [y, m] = month.split("-").map(Number);
  const query = [`month=${_chvEncodeComponent(month)}`];
  for (const key of ["q", "status", "doctor", "visitor", "channel"]) {
    const value = _chvF[key];
    if (typeof value === "string" && value.trim()) {
      query.push(`${key}=${_chvEncodeComponent(value.trim())}`);
    }
  }
  let summary;
  try {
    const payload = await _chvFetchJson(`/api/return-visits/calendar?${query.join("&")}`);
    summary = _chvMonthlyOwn(payload, "summary");
    if (!_chvMonthlySummaryValid(summary)) throw new TypeError("invalid_monthly_summary");
  } catch {
    if (_chvLifecycleIsCurrent(lifecycleToken)) window.alert("客户通载入失败");
    return;
  }
  if (!_chvLifecycleIsCurrent(lifecycleToken)) return false;
  const channelLabels = {phone: "电话", wechat: "微信", other: "其他", unknown: "未设置"};
  const problemLabels = {needs_attention: "需关注", overdue: "逾期", low_satisfaction: "低满意度"};
  const channelRows = summary.channel_counts.map(item => {
    const rate = summary.total ? (item.count * 100 / summary.total).toFixed(1) : "0.0";
    return `<tr><td>方式占比</td><td>${channelLabels[item.channel]}</td><td>${item.count} / ${summary.total} · ${rate}%</td></tr>`;
  }).join("");
  const problemRows = _CHV_MONTHLY_PROBLEMS.map(key => (
    `<tr><td>问题</td><td>${problemLabels[key]}</td><td>${summary.problem_counts[key]}</td></tr>`
  )).join("");
  const resultRows = summary.top_results.length
    ? summary.top_results.map(item => (
      `<tr><td>高频结果</td><td>${escapeHtml(item.label)}</td><td>${item.count}</td></tr>`
    )).join("")
    : '<tr><td>高频结果</td><td colspan="2">暂无高频结果</td></tr>';
  const narrative = summary.narrative.status === "ready"
    ? `<div class="chv-note">${escapeHtml(summary.narrative.text)}</div>`
    : summary.narrative.status === "failed"
      ? '<div class="chv-note">月度摘要生成失败</div>' : "";
  const ovl = _chvOvl("chvMrOvl", `
    <div class="chv-phead"><h3>月度回顾 · ${escapeHtml(month)}</h3>
      <span><button type="button" class="chv-btn" data-chv-mrprev="1">‹</button>
      <button type="button" class="chv-btn" data-chv-mrnext="1">›</button>
      <button type="button" class="chv-btn" data-chv-close="1">✕</button></span></div>
    <div class="chv-pbody">
      <div class="chv-kpis">
        <div class="chv-kpi chv-glass"><small>本月回访</small><b>${summary.total}</b><span>全部任务</span></div>
        <div class="chv-kpi chv-glass"><small>已回访</small><b>${summary.completed}</b><span>已完成 ${summary.completed} / ${summary.total}</span></div>
        <div class="chv-kpi chv-glass hot"><small>完成率</small><b>${summary.completion_rate}%</b><span>待处理 ${summary.pending} · 逾期 ${summary.overdue}</span></div>
      </div>
      <table class="chv-table chv-mr-table"><thead><tr><th>统计</th><th>项目</th><th>数量 / 占比</th></tr></thead>
      <tbody>${channelRows}${problemRows}${resultRows}</tbody></table>
      ${narrative}
    </div>`, true);
  const shift = d => {
    const nd = new Date(y, m - 1 + d, 1);
    _chvDisposeOverlay(ovl);
    _chvOpenMonthlyReview(`${nd.getFullYear()}-${String(nd.getMonth() + 1).padStart(2, "0")}`);
  };
  ovl.querySelector("[data-chv-mrprev]").addEventListener("click", () => shift(-1));
  ovl.querySelector("[data-chv-mrnext]").addEventListener("click", () => shift(1));
}

// ============ 高级查询弹窗 ============

function _chvOpenAdv() {
  const f = _chvF;
  const ovl = _chvOvl("chvAdvOvl", `
    <div class="chv-phead"><h3>高级查询</h3><button type="button" class="chv-btn" data-chv-close="1">✕</button></div>
    <div class="chv-pbody chv-form2">
      <label>开始日期<input type="date" data-af="date_from" value="${escapeAttr(f.date_from)}"></label>
      <label>结束日期<input type="date" data-af="date_to" value="${escapeAttr(f.date_to)}"></label>
      <label>回访状态<select data-af="status">${["", "all", "待回访", "已回访", "做计划", "待跟进"].map(s =>
        `<option value="${s}"${s === f.status ? " selected" : ""}>${s === "" ? "待回访(默认)" : s === "all" ? "全部" : s}</option>`).join("")}</select></label>
      <label>回访方式<select data-af="channel">${["", "phone", "wechat"].map(s =>
        `<option value="${s}"${s === f.channel ? " selected" : ""}>${s === "" ? "全部" : s === "phone" ? "电话" : "微信"}</option>`).join("")}</select></label>
      <label>回访类型<input data-af="rtype" value="${escapeAttr(f.rtype)}" placeholder="如:术后随访"></label>
      <label>就诊项目/事项<input data-af="item" value="${escapeAttr(f.item)}" placeholder="事项关键词"></label>
      <label>患者信息<input data-af="patient_q" value="${escapeAttr(f.patient_q)}" placeholder="姓名/首拼/病历号/手机号"></label>
      <label>内容/结果关键词<input data-af="content_q" value="${escapeAttr(f.content_q)}" placeholder="搜回访内容与结果"></label>
      <label>附件<select data-af="att">${
        // 有录音/有影像仅在具对应查看权时可选(否则选了必 403 卡死列表)
        [["", "全部"], ...(hasPerm("call_record.view") ? [["recording", "有录音"]] : []),
         ["screenshot", "有截图"], ...(hasPerm("patient.image.view") ? [["image", "有影像"]] : [])]
          .map(([v, t]) => `<option value="${v}"${v === f.att ? " selected" : ""}>${t}</option>`).join("")}</select></label>
      <div class="chv-actions" style="grid-column:1/-1;justify-content:flex-end">
        <button type="button" class="chv-btn" data-chv-reset="1">重置</button>
        <button type="button" class="chv-btn pri" data-chv-ok="1">确定</button>
        <button type="button" class="chv-btn" data-chv-close="1">取消</button>
      </div>
    </div>`);
  ovl.querySelector("[data-chv-reset]").addEventListener("click", () => {
    ovl.querySelectorAll("[data-af]").forEach(el => { el.value = ""; });
  });
  ovl.querySelector("[data-chv-ok]").addEventListener("click", () => {
    ovl.querySelectorAll("[data-af]").forEach(el => { _chvF[el.dataset.af] = el.value.trim(); });
    _chvListPage = 1; // 前端复核轮P2:改筛选后回第一页,免停在旧页码看空表
    _chvDisposeOverlay(ovl);
    loadCustomerHubV2();
  });
}

// ============ 区间选择器(回访列表日期) ============

function _chvOpenRange() {
  const ovl = _chvOvl("chvRangeOvl", `
    <div class="chv-phead"><h3>选时间区间</h3><button type="button" class="chv-btn" data-chv-close="1">✕</button></div>
    <div class="chv-pbody chv-form2">
      <label>从<input type="date" id="chvRf" value="${escapeAttr(_chvF.date_from)}"></label>
      <label>到<input type="date" id="chvRt" value="${escapeAttr(_chvF.date_to)}"></label>
      <div class="chv-actions" style="grid-column:1/-1;flex-wrap:wrap">
        ${[["今天", 0, 0], ["近 7 天", -6, 0], ["近 30 天", -29, 0]].map(([t, a, b]) =>
          `<button type="button" class="chv-btn" data-chv-preset="${a},${b}">${t}</button>`).join("")}
        <span style="flex:1"></span>
        <button type="button" class="chv-btn pri" data-chv-ok="1">确定</button>
      </div>
    </div>`);
  ovl.querySelectorAll("[data-chv-preset]").forEach(b => b.addEventListener("click", () => {
    const [a, c] = b.dataset.chvPreset.split(",").map(Number);
    // 以北京时间「今天」为基准(bjToday),避免浏览器时区在跨日边界差一天
    const iso = off => { const d = bjToday(); d.setDate(d.getDate() + off); return localDateValue(d); };
    ovl.querySelector("#chvRf").value = iso(a);
    ovl.querySelector("#chvRt").value = iso(c);
  }));
  ovl.querySelector("[data-chv-ok]").addEventListener("click", () => {
    _chvF.date_from = ovl.querySelector("#chvRf").value;
    _chvF.date_to = ovl.querySelector("#chvRt").value;
    _chvListPage = 1;   // 改区间回第一页
    _chvDisposeOverlay(ovl);
    loadCustomerHubV2();
  });
}

// ============ 记沟通弹窗(direction/reason 全字段落库) ============

async function _chvDictsGet(permission) {
  if (!["communication.manage", "return_visit.manage", "return_visit.view"].includes(permission)
      || !_chvHasAllPermissions("patient.profile.view", permission)) {
    throw new Error("customer_hub_dictionary_forbidden");
  }
  // 失败不缓存空字典(否则整个会话看着像诊所没配常用语);抛错由调用方提示+可重试
  if (!_chvDicts) {
    const generation = ++_chvDictRequestGeneration;
    let loaded;
    try {
      loaded = await _chvFetchJson("/api/settings/customer-hub-dicts");
    } catch (error) {
      if (generation !== _chvDictRequestGeneration) throw _CHV_DICT_REQUEST_STALE;
      throw error;
    }
    if (generation !== _chvDictRequestGeneration) throw _CHV_DICT_REQUEST_STALE;
    _chvDicts = loaded;
  }
  return _chvDicts;
}

function _chvDictionaryRequestStale(error) {
  return error === _CHV_DICT_REQUEST_STALE;
}

// 患者搜索选择:多个匹配必须显式点选,禁止盲取 list[0] 写错档
function _chvBindPatientPick(ovl, inputId, hintId, state) {
  const input = ovl.querySelector(`#${inputId}`);
  const hint = ovl.querySelector(`#${hintId}`);
  let searchGen = 0; // 请求代数:丢弃迟到的旧响应,防它覆盖 state.pid
  input.addEventListener("change", async () => {
    const gen = ++searchGen;
    state.pid = "";
    hint.innerHTML = "";
    const q = input.value.trim();
    if (!q) return;
    let list;
    try { list = (await _chvFetchJson(`/api/patients?q=${_chvEncodeComponent(q)}&pagesize=8`)).list || []; }
    catch { if (gen === searchGen) hint.textContent = "患者搜索失败"; return; }
    if (gen !== searchGen) return;   // 已有更新的搜索,丢弃本次旧响应
    if (!list.length) { hint.textContent = "没找到该患者"; return; }
    if (list.length === 1) {
      state.pid = list[0].patient_identity;
      hint.textContent = `已选:${list[0].display_name}（${list[0].phone || "无电话"}）`;
      return;
    }
    // 多个同名/同号:列出候选,点选才定档
    hint.innerHTML = `匹配到 ${list.length} 位,请点选:<span class="chv-pick">${list.map(p =>
      `<button type="button" class="chv-btn" data-pick="${escapeAttr(p.patient_identity)}">${escapeHtml(p.display_name)}（${escapeHtml(p.phone || "无")}）</button>`
    ).join("")}</span>`;
    hint.querySelectorAll("[data-pick]").forEach(b => b.addEventListener("click", () => {
      state.pid = b.dataset.pick;
      hint.textContent = `已选:${b.textContent}`;
    }));
  });
}

async function _chvOpenAddComm(pid) {
  if (!hasPerm("patient.profile.view") || !hasPerm("communication.manage")) {
    window.alert("无沟通管理权限");
    return false;
  }
  const lifecycleToken = _chvCaptureLifecycleToken();
  let dicts;
  try { dicts = await _chvDictsGet("communication.manage"); }
  catch (error) {
    if (_chvDictionaryRequestStale(error)) return false;
    if (_chvLifecycleIsCurrent(lifecycleToken)) window.alert("常用语字典载入失败(可能无权限或网络异常),请重试");
    return;
  }
  if (!_chvLifecycleIsCurrent(lifecycleToken)) return false;
  const state = {pid: pid || ""};
  const ovl = _chvOvl("chvCommOvl", `
    <div class="chv-phead"><h3>记一条沟通</h3><button type="button" class="chv-btn" data-chv-close="1">✕</button></div>
    <div class="chv-pbody chv-form2">
      <label>患者<input id="chvNcP" placeholder="搜姓名/手机号" value="${escapeAttr(pid)}"${pid ? " readonly" : ""}><span id="chvNcHint" class="chv-note"></span></label>
      <label>方式<select id="chvNcWay"><option value="phone">电话</option><option value="wechat">微信</option></select></label>
      <label id="chvNcDirWrap">方向<select id="chvNcDir"><option value="inbound">↘呼入</option><option value="outbound">↗呼出</option></select></label>
      <label>干什么(事由)<select id="chvNcWhy">${(dicts.comm_reason || []).map(x =>
        `<option>${escapeHtml(x)}</option>`).join("")}</select></label>
      <label style="grid-column:1/-1">聊了什么<textarea id="chvNcText" placeholder="电话聊的、微信咨询的要点" data-chv-dirty></textarea></label>
      <label>成交状态<select id="chvNcDeal"><option value="">—</option><option>已成交</option><option>跟进中</option><option>未成交</option></select></label>
      <div class="chv-actions" style="grid-column:1/-1;justify-content:flex-end">
        <button type="button" class="chv-btn pri" data-chv-save="1">保存</button>
        <button type="button" class="chv-btn" data-chv-close="1">取消</button>
      </div>
    </div>`);
  _chvGuardDirty(ovl);
  // 预选患者:锁定输入框(readonly),state.pid 固定,防改名字把沟通写到别人档上
  if (pid) { state.pid = pid; ovl.querySelector("#chvNcHint").textContent = "已锁定当前患者(不可改)"; }
  else _chvBindPatientPick(ovl, "chvNcP", "chvNcHint", state);
  ovl.querySelector("#chvNcWay").addEventListener("change", e => {
    ovl.querySelector("#chvNcDirWrap").style.display = e.target.value === "phone" ? "" : "none";
  });
  const saveBtn = ovl.querySelector("[data-chv-save]");
  saveBtn.addEventListener("click", async () => {
    if (!_chvHasAllPermissions("patient.profile.view", "communication.manage")) return false;
    if (_chvOverlayPending(ovl)) return;
    const text = ovl.querySelector("#chvNcText").value.trim();
    if (!state.pid) { window.alert("先选定患者(多个同名时需点选)"); return; }
    if (!text) { window.alert("先写聊了什么"); return; }
    const way = ovl.querySelector("#chvNcWay").value;
    const payload = {
      channel: way,
      direction: way === "phone" ? ovl.querySelector("#chvNcDir").value : "",
      reason: ovl.querySelector("#chvNcWhy").value,
      content: text,
      deal_status: ovl.querySelector("#chvNcDeal").value,
    };
    try {
      const result = await _chvWriteInOverlay(ovl, saveBtn, "communication", {
        action: "create",
        patientIdentity: state.pid,
        fields: payload,
      });
      if (!result.ok) { _chvWriteError(result, "沟通保存"); return; }
    } catch { window.alert("保存失败(网络异常),请重试"); return; }
    ovl._chvClean = true;
    _chvDisposeOverlay(ovl);
    loadCustomerHubV2();
  });
}

// ============ 新增回访(最小可用:选患者+时间+事项) ============

async function _chvOpenNewRv(presetPid) {
  if (!_chvHasAllPermissions("patient.profile.view", "return_visit.manage")) return false;
  const lifecycleToken = _chvCaptureLifecycleToken();
  let dicts;
  try { dicts = await _chvDictsGet("return_visit.manage"); }
  catch (error) {
    if (_chvDictionaryRequestStale(error)) return false;
    if (_chvLifecycleIsCurrent(lifecycleToken)) window.alert("常用语字典载入失败(可能无权限或网络异常),请重试");
    return;
  }
  if (!_chvLifecycleIsCurrent(lifecycleToken)) return false;
  const ovl = _chvOvl("chvNewRvOvl", `
    <div class="chv-phead"><h3>新增回访</h3><button type="button" class="chv-btn" data-chv-close="1">✕</button></div>
    <div class="chv-pbody chv-form2">
      <label>患者<input id="chvNrP" placeholder="搜姓名/手机号" value="${escapeAttr(presetPid || "")}"${presetPid ? " readonly" : ""}><span id="chvNrHint" class="chv-note"></span></label>
      <label>回访时间<input type="datetime-local" id="chvNrT"></label>
      <label style="grid-column:1/-1">回访事项<select onchange="if(this.value){document.getElementById('chvNrI').value=this.value;this.selectedIndex=0}">
        <option value="">插入常用语…</option>${(dicts.visit_content || []).map(x => `<option>${escapeHtml(x)}</option>`).join("")}</select>
        <input id="chvNrI" placeholder="回访事项"></label>
      <div class="chv-actions" style="grid-column:1/-1;justify-content:flex-end">
        <button type="button" class="chv-btn pri" data-chv-save="1">保存</button>
        <button type="button" class="chv-btn" data-chv-close="1">取消</button>
      </div>
    </div>`);
  _chvGuardDirty(ovl);
  const state = {pid: presetPid || ""};
  if (presetPid) ovl.querySelector("#chvNrHint").textContent = "已锁定当前患者";
  else _chvBindPatientPick(ovl, "chvNrP", "chvNrHint", state);
  const saveBtn = ovl.querySelector("[data-chv-save]");
  saveBtn.addEventListener("click", async () => {
    if (!_chvHasAllPermissions("patient.profile.view", "return_visit.manage")) return false;
    if (_chvOverlayPending(ovl)) return;
    const t = ovl.querySelector("#chvNrT").value;
    const item = ovl.querySelector("#chvNrI").value.trim();
    if (!state.pid) { window.alert("先选定患者(多个同名时需点选)"); return; }
    if (!t || !item) { window.alert("回访时间与事项必填"); return; }
    try {
      const result = await _chvWriteInOverlay(ovl, saveBtn, "returnVisit", {
        action: "create",
        patientIdentity: state.pid,
        fields: {due_time: t.replace("T", " "), item_name: item},
      });
      if (!result.ok) { _chvWriteError(result, "回访保存"); return; }
    } catch { window.alert("保存失败(网络异常),请重试"); return; }
    ovl._chvClean = true;
    _chvDisposeOverlay(ovl);
    loadCustomerHubV2();
  });
}

// ============ 回访处理弹窗(任务4:左五页签患者卡 + 右回访表单) ============

let _chvModalGen = 0; // 弹窗打开代数:连点两行时,慢到的旧详情不得覆盖新弹窗
function _chvCreateRevisionRecovery(rvId, patientIdentity, getRevision, setRevision) {
  let pending = null;
  let confirmed = null;
  const readLatest = async recovery => {
    try {
      const latest = await _chvFetchJson(`/api/return-visits/${_chvEncodeComponent(rvId)}`);
      const api = _chvContractApi();
      const revision = api.readReturnVisitRevision(latest);
      if (!Number.isSafeInteger(revision) || revision <= recovery.expectedRevision) {
        throw new Error("invalid_return_visit_revision");
      }
      if (recovery.kind === "confirm" && recovery.action === "edit"
          && !Number.isSafeInteger(api.validateReturnVisitEditConfirmation(latest, {
            id: rvId,
            patientIdentity,
            expectedRevision: recovery.expectedRevision,
            fields: recovery.fields,
          }))) {
        setRevision(revision);
        if (pending === recovery) pending = null;
        recovery.requiresRewrite = true;
        return false;
      }
      setRevision(revision);
      if (pending === recovery) pending = null;
      return true;
    } catch {
      return false;
    }
  };
  return {
    async resume(draftSignature) {
      if (confirmed) {
        if (confirmed.action === "edit" && confirmed.draftSignature !== draftSignature) {
          confirmed = null;
          return {handled: false};
        }
        const recovery = confirmed;
        if (recovery.action !== "edit") confirmed = null;
        return {handled: true, ok: true, action: recovery.action, kind: "confirm"};
      }
      if (!pending) return {handled: false};
      const recovery = pending;
      const ok = await readLatest(recovery);
      if (ok && recovery.kind === "confirm" && recovery.action === "edit") {
        confirmed = {action: recovery.action, draftSignature: recovery.draftSignature};
      }
      if (!ok) {
        window.alert(recovery.requiresRewrite
          ? "写入结果未与当前草稿一致，版本已刷新；请核对后显式再次保存"
          : recovery.kind === "confirm"
          ? "上次写入结果确认失败，本次仅重试读取，未重放写入"
          : "回访版本刷新失败，本次仅重试读取，未发送写请求");
      } else {
        window.alert(recovery.kind === "confirm"
          ? "写入结果已确认，本次未重放写入"
          : "回访版本已刷新，请核对草稿后再次点击保存");
      }
      return {
        handled: true,
        ok,
        action: ok ? recovery.action : "",
        kind: recovery.kind,
        deferCompletion: ok && recovery.kind === "confirm",
      };
    },
    async conflict(action, expectedRevision) {
      const recovery = {kind: "retry", action, expectedRevision: getRevision() || expectedRevision};
      recovery.expectedRevision = expectedRevision;
      pending = recovery;
      const ok = await readLatest(recovery);
      window.alert(ok
        ? "数据已被更新，版本已刷新；请核对草稿后显式再点一次"
        : "保存冲突且版本刷新失败；下次点击只会重试读取");
      return false;
    },
    async confirm(action, expectedRevision, draftSignature, fields) {
      const recovery = {kind: "confirm", action, expectedRevision, draftSignature, fields};
      pending = recovery;
      const ok = await readLatest(recovery);
      if (ok && action === "edit") confirmed = {action, draftSignature};
      if (recovery.requiresRewrite) {
        window.alert("写入结果未与当前草稿一致，版本已刷新；请核对后显式再次保存");
      } else if (!ok) {
        window.alert("写入已接收，但结果确认失败；下次点击只重试读取，不会重放写入");
      }
      return ok;
    },
    clearConfirmed(action) {
      if (!action || (confirmed && confirmed.action === action)) confirmed = null;
    },
  };
}

async function _chvOpenRvModal(rvId, pid) {
  if (!_chvHasAllPermissions("patient.profile.view", "return_visit.view")) return false;
  const lifecycleToken = _chvCaptureLifecycleToken();
  const gen = ++_chvModalGen;
  let d;
  try { d = await _chvFetchJson(`/api/return-visits/${_chvEncodeComponent(rvId)}`); }
  catch {
    if (gen === _chvModalGen && _chvLifecycleIsCurrent(lifecycleToken)) window.alert("回访详情载入失败");
    return;
  }
  if (gen !== _chvModalGen || !_chvLifecycleIsCurrent(lifecycleToken)) return false;
  let dicts;
  try { dicts = await _chvDictsGet("return_visit.view"); }
  catch (error) {
    if (_chvDictionaryRequestStale(error)) return false;
    if (gen === _chvModalGen && _chvLifecycleIsCurrent(lifecycleToken)) {
      window.alert("常用语字典载入失败(可能无权限或网络异常),请重试");
    }
    return;
  }
  if (gen !== _chvModalGen || !_chvLifecycleIsCurrent(lifecycleToken)) return false;
  pid = pid || d.patient_identity || "";
  let currentRevision = d.revision;
  const revisionRecovery = _chvCreateRevisionRecovery(
    rvId,
    pid,
    () => currentRevision,
    revision => { currentRevision = revision; },
  );
  const sel = (list, cur) => (list || []).map(x =>
    `<option${x === cur ? " selected" : ""}>${escapeHtml(x)}</option>`).join("");
  const insertSel = (list, target, dictionaryName) => `
    <select data-chv-dict-insert="${escapeAttr(dictionaryName)}" onchange="if(this.value){document.getElementById('${target}').value=this.value;this.selectedIndex=0}">
      <option value="">插入常用语…</option>${(list || []).map(x => `<option>${escapeHtml(x)}</option>`).join("")}
    </select>`;
  const canEdit = hasPerm("return_visit.manage"); // 只有管理权才显示写控件(查看用户得只读弹窗)
  const ro = canEdit ? "" : " disabled";
  const dueLocal = (d.due_time || "").replace(" ", "T").slice(0, 16);
  // 状态保原值:导入态若不在标准四项内,补一个「原值」项并选中,防改其他字段时被浏览器落到「待回访」静默改写
  const STD_ST = ["已回访", "做计划", "待跟进", "待回访"];
  const curSt = String(d.status ?? "");
  const legacyCode3 = curSt === "3";
  // 完成态判定与后端 DONE_COND 同口径:显式待办状态优先,仅 status 空/其他时回退前缀(复核轮9-P1)
  const isDone = legacyCode3 ? false : _chvRowDone(d);
  const stdStatus = legacyCode3 ? null
    : isDone ? "已回访"
    : (curSt === "做计划" || curSt === "待跟进") ? curSt
    : (curSt === "待回访" || curSt === "") ? "待回访" : null;   // null=非标准原值
  const legacyLabel = legacyCode3
    ? "旧系统状态（代码 3，未映射）"
    : `${curSt}（原值）`;
  const titleStatusBadge = legacyCode3
    ? `<span class="chv-badge chv-b-wait">${escapeHtml(legacyLabel)}</span>`
    : _chvStatusBadge(d);
  const insertSelE = canEdit ? insertSel : (() => "");   // 只读时不给"插入常用语"
  const dictBtn = k => canEdit ? `<button type="button" class="chv-btn" data-chv-dict="${k}">⚙</button>` : "";
  const ovl = _chvOvl("chvRvOvl", `
    <div class="chv-phead">
      <span class="chv-mtitle" data-chv-dialog-title>${_chvName(pid, d.display_name)}
        <span class="chv-sub">${escapeHtml(d.due_time || "")} · ${escapeHtml(d.item_name || "")}</span>
        ${titleStatusBadge}${canEdit ? "" : '<span class="chv-badge chv-b-teal">只读</span>'}</span>
      <button type="button" class="chv-btn" data-chv-close="1">✕ 关闭</button>
    </div>
    <div class="chv-mbody">
      <div class="chv-mleft">
        <nav class="chv-ptabs">
          <button type="button" class="on" data-chv-ptab="info">患者信息</button>
          ${hasPerm("medical_record.view") ? `<button type="button" data-chv-ptab="emr">病历</button>` : ""}
          ${hasPerm("return_visit.view") ? `<button type="button" data-chv-ptab="hist">历史回访</button>` : ""}
          ${hasPerm("patient.image.view") ? `<button type="button" data-chv-ptab="img">影像</button>` : ""}
          ${hasPerm("patient.profile.view") && hasPerm("communication.view") ? `<button type="button" data-chv-ptab="comm">咨询沟通</button>` : ""}
        </nav>
        <div class="chv-mpane" id="chvPane"><div class="module-loading">载入中...</div></div>
      </div>
      <div class="chv-mform">
        <label>回访时间 *<input type="datetime-local" id="chvMT" value="${escapeAttr(dueLocal)}"${ro}></label>
        <label>回访事项 *<input id="chvMI" value="${escapeAttr(d.item_name || "")}"${ro}></label>
        <label>回访方式 *<span class="chv-actions">
          <button type="button" class="chv-btn${d.channel === "phone" ? " pri" : ""}" data-chv-way="phone"${ro}>📞 电话</button>
          <button type="button" class="chv-btn${d.channel === "wechat" ? " pri" : ""}" data-chv-way="wechat"${ro}>💬 微信</button>
          ${d.channel ? "" : '<span class="chv-req">请选择</span>'}
        </span><input type="hidden" id="chvMWay" value="${escapeAttr(d.channel || "")}"></label>
        <div id="chvWx" style="display:${d.channel === "wechat" ? "block" : "none"}" class="chv-wxbox">
          <b>聊天截图 <span class="chv-req">微信回访必须上传聊天截图(留证据)</span></b>
          <div class="chv-actions" id="chvScList">
            ${(d.images || []).map(im => `<span class="chv-badge chv-b-teal">📷 ${escapeHtml(im.file_name || "截图")}</span>`).join("")}
            ${canEdit ? `<label class="chv-btn">📷 上传截图<input type="file" id="chvScFile" accept="image/*" hidden></label>` : ""}
          </div>
        </div>
        <label>回访人 *<input id="chvMV" value="${escapeAttr(d.visitor || "")}" placeholder="回访人"${ro}></label>
        <label>回访状态 *<span class="chv-radios">
          ${stdStatus === null ? `<label><input type="radio" name="chvSt" value="${escapeAttr(curSt)}" checked${ro}>${escapeHtml(legacyLabel)}</label>` : ""}
          ${STD_ST.map(s =>
            `<label><input type="radio" name="chvSt" value="${s}"${s === stdStatus ? " checked" : ""}${ro}>${s}</label>`).join("")}
        </span></label>
        <label>回访类型<span class="chv-inline">${insertSelE(dicts.return_type, "chvMType", "return_type")}
          ${dictBtn("return_type")}</span>
          <input id="chvMType" value="${escapeAttr(d.return_type || "")}"${ro}></label>
        <label>回访内容 *<span class="chv-inline">${insertSelE(dicts.visit_content, "chvMC", "visit_content")}
          ${dictBtn("visit_content")}</span>
          <textarea id="chvMC" placeholder="这次回访说了什么"${ro}>${escapeHtml(d.note || "")}</textarea></label>
        <label>回访结果 *<span class="chv-inline">${insertSelE(dicts.visit_result, "chvMR", "visit_result")}
          ${dictBtn("visit_result")}</span>
          <textarea id="chvMR" placeholder="患者反馈"${ro}>${escapeHtml(d.return_result || "")}</textarea></label>
        ${canEdit ? `<label>下次回访（可选：填了保存时自动排一条计划回访）
          <span class="chv-inline">${hasPerm("master_data.view") ? '<select data-chv-plan="1"><option value="">按计划预填…</option></select>' : ""}<input type="date" id="chvMNext"></span>
          <span class="chv-pick" aria-label="下次回访快捷日期">
            <button type="button" class="chv-btn" data-chv-next="15d">半个月</button>
            <button type="button" class="chv-btn" data-chv-next="1m">1 个月</button>
            <button type="button" class="chv-btn" data-chv-next="6m">6 个月</button>
            <button type="button" class="chv-btn" data-chv-next="1y">1 年</button>
          </span>
        </label>` : ""}
      </div>
    </div>
    <div class="chv-mfoot">
      ${hasPerm("return_visit.delete") ? `<button type="button" class="chv-btn danger" data-chv-del="1">🗑 删除回访</button>` : ""}
      <span style="flex:1"></span>
      ${hasPerm("appointment.create") ? `<button type="button" class="chv-btn" data-chv-newappt="1">＋新增预约</button>` : ""}
      ${canEdit ? `<button type="button" class="chv-btn" data-chv-okplan="1">确定并计划</button>` : ""}
      ${canEdit ? `<button type="button" class="chv-btn pri" data-chv-ok="1">✓ 确定</button>` : ""}
      <button type="button" class="chv-btn" data-chv-close="1">${canEdit ? "取消" : "关闭"}</button>
    </div>`, true);
  _chvGuardDirty(ovl);
  // #856三期②:「回访内容」字段级脏标记(与表单级 ovl._chvDirty 独立) + 自动填入挂链录音转写
  const noteEl = ovl.querySelector("#chvMC");
  if (noteEl) noteEl.addEventListener("input", () => { ovl._chvNoteDirty = true; });
  _chvMaybeFillTranscript(ovl, rvId, pid);
  // #856三期③A:弹窗内桌面麦克风直录(自动挂本回访)
  const scList = ovl.querySelector("#chvScList");
  if (scList && hasPerm("call_record.manage")) {
    scList.appendChild(_chvMicButton(
      pid,
      rvId,
      () => currentRevision,
      revision => { currentRevision = revision; },
      ovl,
    ));
  }
  // #856三期:下次回访制度预填——选 ReturnPlan 计划名,日期=北京今天+天数;事项关键词命中排前,不自动选
  const planSel = ovl.querySelector("[data-chv-plan]");
  if (planSel) {
    _chvLoadReturnPlans(d.item_name).then(plans => {
      planSel.innerHTML = `<option value="">按计划预填…</option>` +
        plans.map(p => `<option value="${p.days}">${escapeHtml(p.name)}(+${p.days}天)</option>`).join("");
    }).catch(() => {
      planSel.innerHTML = '<option value="">制度载入失败，请关闭后重试</option>';
      planSel.disabled = true;
    });
    planSel.addEventListener("change", () => {
      if (planSel.value === "") return;
      const base = new Date(`${_chvToday()}T00:00:00`);
      base.setDate(base.getDate() + parseInt(planSel.value, 10));
      const next = ovl.querySelector("#chvMNext");
      if (next) {
        next.value = `${base.getFullYear()}-${String(base.getMonth() + 1).padStart(2, "0")}-${String(base.getDate()).padStart(2, "0")}`;
        ovl._chvDirty = true;   // 预填=待保存改动,走同一关闭守卫
      }
      planSel.value = "";
    });
  }
  ovl.querySelectorAll("[data-chv-next]").forEach(button => button.addEventListener("click", () => {
    const time = ovl.querySelector("#chvMT");
    const next = ovl.querySelector("#chvMNext");
    if (!time || !next) return;
    let api;
    try {
      api = _chvFollowupDateApi();
    } catch {
      window.alert("下次回访快捷日期不可用，请刷新后重试");
      return;
    }
    try {
      const value = api.computeNextFollowupDate(time.value, button.dataset.chvNext);
      next.value = value;
      ovl._chvDirty = true;
    } catch {
      window.alert("回访时间无效，请先填写正确的回访时间");
    }
  }));
  // 名字进主页(离开弹窗前脏守卫)
  ovl.querySelectorAll("[data-chv-home]").forEach(el => el.addEventListener("click", () => {
    _chvLeavePatientOverlay(ovl, el.dataset.chvHome);
  }));
  // 方式切换:电话/微信联动截图区
  ovl.querySelectorAll("[data-chv-way]").forEach(b => b.addEventListener("click", () => {
    ovl.querySelectorAll("[data-chv-way]").forEach(x => x.classList.toggle("pri", x === b));
    ovl.querySelector("#chvMWay").value = b.dataset.chvWay;
    ovl.querySelector("#chvWx").style.display = b.dataset.chvWay === "wechat" ? "block" : "none";
    ovl._chvDirty = true; // 隐藏 input 编程改值不触发事件,手动置脏
  }));
  // 截图暂存:选文件先入内存,保存时才真上传——取消/关闭就丢弃,
  // 不落库、无孤儿、无需删权/再认证;彻底绕开原「即传即存」的回滚困境。
  let existingImg = (d.images || []).length;
  const pendingFiles = [];
  let screenshotUploadQuarantined = false;
  const scCountOf = () => existingImg + pendingFiles.length;
  const scFile = ovl.querySelector("#chvScFile");
  if (scFile) scFile.addEventListener("change", () => {
    const file = scFile.files && scFile.files[0];
    if (!file) return;
    pendingFiles.push(file);
    scFile.closest("#chvScList").insertAdjacentHTML("afterbegin",
      `<span class="chv-badge chv-b-teal">📷 ${escapeHtml(file.name)}（待保存）</span>`);
    scFile.value = "";
  });
  // 字典设置
  ovl.querySelectorAll("[data-chv-dict]").forEach(b => b.addEventListener("click", async () => {
    if (_chvOverlayPending(ovl)) return;
    const lifecycleToken = _chvCaptureLifecycleToken();
    try {
      await _chvOpenDict(b.dataset.chvDict);
    } catch {
      if (_chvLifecycleIsCurrent(lifecycleToken)
          && _chvNodeConnected(ovl) && _chvNodeConnected(b)) {
        window.alert("常用语字典载入失败(可能无权限或网络异常),请重试");
      }
    }
  }));
  // 删除必须携带当前 revision；409 保留当前草稿，不自动重放。
  const delBtn = ovl.querySelector("[data-chv-del]");
  if (delBtn) delBtn.addEventListener("click", async () => {
    if (!_chvHasAllPermissions("patient.profile.view", "return_visit.delete")) return false;
    if (_chvOverlayPending(ovl)) return;
    if (!window.confirm("确认删除该回访？系统会保留版本和审计记录。")) return;
    await _chvRunOverlayOperation(ovl, delBtn, async () => {
      const resumed = await revisionRecovery.resume();
      if (resumed.handled) return false;
      const expectedRevision = currentRevision;
      try {
        const result = await _chvWriteInOverlay(ovl, delBtn, "returnVisit", {
          action: "delete",
          id: rvId,
          expectedRevision,
        });
        if (!result.ok) {
          _chvWriteError(result, "删除");
          if (Number(result.status) === 409) await revisionRecovery.conflict("delete", expectedRevision);
          return false;
        }
      } catch { window.alert("删除失败(网络异常),请重试"); return false; }
      ovl._chvClean = true;
      _chvDisposeOverlay(ovl);
      loadCustomerHubV2();
      return true;
    });
  });
  // 保存核心:返回 true=成功。供「确定」「确定并计划」复用。
  const doSave = async focusTarget => {
    if (!_chvHasAllPermissions("patient.profile.view", "return_visit.manage")) return false;
    const way = ovl.querySelector("#chvMWay").value;
    const time = (ovl.querySelector("#chvMT").value || "").trim();
    const item = ovl.querySelector("#chvMI").value.trim();
    const content = ovl.querySelector("#chvMC").value.trim();
    const result = ovl.querySelector("#chvMR").value.trim();
    const visitor = ovl.querySelector("#chvMV").value.trim();
    const st = ovl.querySelector("input[name=chvSt]:checked").value;
    const returnType = ovl.querySelector("#chvMType").value.trim();
    const draftSignature = JSON.stringify({time, item, content, result, visitor, st, returnType, way});
    const resumed = await revisionRecovery.resume(draftSignature);
    if (resumed.handled && (resumed.deferCompletion
        || !resumed.ok || resumed.kind !== "confirm" || resumed.action !== "edit")) return false;
    const editAlreadyConfirmed = resumed.handled && resumed.action === "edit";
    if (!way) { window.alert("请先选择回访方式(电话/微信)"); return false; } // channel 不默认
    if (way === "wechat" && scCountOf() === 0) {
      window.alert("微信回访必须上传聊天截图才能保存"); return false;
    }
    const miss = [];
    if (!time) miss.push("回访时间"); // 空时间会让记录从总览/月历消失,前端硬拦
    if (!item) miss.push("回访事项");
    if (!visitor) miss.push("回访人");
    if (!content) miss.push("回访内容");
    if (st === "已回访" && !result) miss.push("回访结果");
    if (miss.length) { window.alert("以下必填项未填:" + miss.join("、")); return false; }
    // 暂存截图逐个上传(微信留证的后端 PUT 校验要求截图已在库)。一张截图入库即成为该回访的
    // 合法附件(同患者,与其他字段是否保存无关),故上传成功即从待传队列移除、计数上调——
    // 保存链任一步失败时,已传截图不回滚(它是有效附件、非孤儿),重试只处理剩余,不会重复上传
    // 去掉需删权/再认证的补偿删除,改用「附件独立有效」的正确语义。
    while (pendingFiles.length) {
      if (screenshotUploadQuarantined) {
        window.alert("截图上传结果不确定，请关闭后重新打开该回访确认，请勿重复上传");
        return false;
      }
      const file = pendingFiles[0];
      let uploadResult;
      const expectedRevision = currentRevision;
      try {
        uploadResult = await _chvWriteInOverlay(ovl, focusTarget, "returnVisit", {
          action: "attachment",
          id: rvId,
          expectedRevision,
          media: file,
          fileName: file.name || "screenshot",
        });
      }
      catch {
        screenshotUploadQuarantined = true;
        window.alert("截图上传结果不确定，请关闭后重新打开该回访确认，请勿重复上传");
        return false;
      }
      if (!uploadResult.ok) {
        if (Number(uploadResult.status) === 0) {
          screenshotUploadQuarantined = true;
          window.alert("截图上传结果不确定，请关闭后重新打开该回访确认，请勿重复上传");
          return false;
        }
        _chvWriteError(uploadResult, "截图上传");
        if (Number(uploadResult.status) === 409) {
          await revisionRecovery.conflict("screenshot", expectedRevision);
        }
        return false;
      }
      const uploadedRevision = uploadResult.response && uploadResult.response.data
        ? uploadResult.response.data.revision : undefined;
      pendingFiles.shift();
      existingImg += 1;   // 入库即计入,重试不重复上传
      if (Number.isSafeInteger(uploadedRevision) && uploadedRevision > expectedRevision) {
        currentRevision = uploadedRevision;
      } else {
        await revisionRecovery.confirm("screenshot", expectedRevision);
        return false;
      }
    }
    const payload = {
      due_time: time.replace("T", " "),
      item_name: item,
      note: content,
      channel: way,
      visitor,
      status: st,
      return_type: returnType,
      return_result: result,
    };
    if (!editAlreadyConfirmed) try {
      const expectedRevision = currentRevision;
      const result = await _chvWriteInOverlay(ovl, focusTarget, "returnVisit", {
        action: "edit",
        id: rvId,
        expectedRevision,
        fields: payload,
      });
      if (!result.ok) {
        _chvWriteError(result, "回访保存");
        if (Number(result.status) === 409) await revisionRecovery.conflict("edit", expectedRevision);
        return false;
      }
      const savedRevision = _chvContractApi().validateReturnVisitEditReceipt(
        result.response,
        {id: rvId, patientIdentity: pid, expectedRevision, fields: payload},
      );
      if (Number.isSafeInteger(savedRevision)) {
        if (savedRevision > expectedRevision) currentRevision = savedRevision;
      } else {
        await revisionRecovery.confirm("edit", expectedRevision, draftSignature, payload);
        return false;
      }
    } catch { window.alert("保存失败(网络异常),请重试"); return false; }
    // 下次回访(可选):填了则自动排一条计划回访(同患者);失败明确报错、不关窗
    const next = ovl.querySelector("#chvMNext") && ovl.querySelector("#chvMNext").value;
    if (next && pid) {
      let pr;
      try {
        pr = await _chvWriteInOverlay(ovl, focusTarget, "returnVisit", {
          action: "create",
          patientIdentity: pid,
          fields: {due_time: next + " 09:00", item_name: item + "(计划回访)", status: "做计划"},
        });
      } catch { window.alert("本次回访已保存,但下次计划回访排程失败(网络异常),请在列表手动补一条"); return "saved_no_plan"; }
      if (!pr.ok) { _chvWriteError(pr, "下次计划回访排程"); return "saved_no_plan"; }
    }
    revisionRecovery.clearConfirmed("edit");
    ovl._chvClean = true;
    return next ? "saved_with_plan" : true;
  };
  const okBtn = ovl.querySelector("[data-chv-ok]");
  if (okBtn) okBtn.addEventListener("click", () => _chvRunOverlayOperation(ovl, okBtn, async () => {
    const rc = await doSave(okBtn);
    if (rc === true || rc === "saved_with_plan") { _chvDisposeOverlay(ovl); loadCustomerHubV2(); }
    else if (rc === "saved_no_plan") { loadCustomerHubV2(); }   // 主回访已存,刷新但留窗提示
    return rc;
  }));
  // 确定并计划:保存后开「新增回访」;若已在「下次回访」自动排过,则不再开第二个计划器(防重复)
  const okPlanBtn = ovl.querySelector("[data-chv-okplan]");
  if (okPlanBtn) okPlanBtn.addEventListener("click", () => _chvRunOverlayOperation(ovl, okPlanBtn, async () => {
    const rc = await doSave(okPlanBtn);
    if (rc === "saved_with_plan") { _chvDisposeOverlay(ovl); loadCustomerHubV2(); }   // 已自动排,不再开
    else if (rc === true) { _chvDisposeOverlay(ovl); loadCustomerHubV2(); _chvOpenNewRv(pid); }
    else if (rc === "saved_no_plan") { loadCustomerHubV2(); }
    return rc;
  }));
  // 新增预约:复用预约新增弹窗,预填该患者
  const newApptBtn = ovl.querySelector("[data-chv-newappt]");
  if (newApptBtn) newApptBtn.addEventListener("click", () => {
    if (_chvOverlayPending(ovl)) return;
    // openNewAppointment 从 prefill.patient.patient_identity 读预选患者
    if (typeof openNewAppointment === "function") {
      openNewAppointment({patient: {patient_identity: pid, display_name: d.display_name}});
      // 真机验收#1:本弹窗(.chv-ovl z=60)高于预约弹窗(.modal-backdrop z=50)——
      // 打开期间临时抬到 70,关闭(hidden)即还原,不影响其他入口的层级语义
      const am = document.getElementById("appointmentModal");
      if (am) {
        am.style.zIndex = "70";
        if (!am.dataset.chvZGuard) {
          am.dataset.chvZGuard = "1";
          new MutationObserver(() => { if (am.hidden) am.style.zIndex = ""; })
            .observe(am, {attributes: true, attributeFilter: ["hidden"]});
        }
      }
    }
    else window.alert("预约模块未就绪");
  });
  // 左页签(懒加载)
  ovl.querySelectorAll("[data-chv-ptab]").forEach(b => b.addEventListener("click", () => {
    if (_chvOverlayPending(ovl)) return;
    ovl.querySelectorAll("[data-chv-ptab]").forEach(x => x.classList.toggle("on", x === b));
    _chvRenderPane(ovl, b.dataset.chvPtab, pid, rvId);
  }));
  _chvRenderPane(ovl, "info", pid, rvId);
}

// 弹窗左侧五页签内容(只读,复用患者工作区接口,无权=明确提示)
async function _chvRenderPane(ovl, tab, pid, rvId) {
  const pane = ovl.querySelector("#chvPane");
  const gen = (ovl._paneGen = (ovl._paneGen || 0) + 1);   // 页签代数:切页签后迟到的旧响应不得覆盖(复核轮9-P2)
  const stale = () => gen !== ovl._paneGen;
  const permitted = tab === "info" ? hasPerm("patient.profile.view")
    : tab === "emr" ? hasPerm("medical_record.view")
    : tab === "hist" ? hasPerm("return_visit.view")
    : tab === "img" ? hasPerm("patient.image.view")
    : tab === "comm" ? hasPerm("patient.profile.view") && hasPerm("communication.view")
    : false;
  if (!permitted) {
    pane.innerHTML = '<div class="chv-empty">无权限，该板块不可用</div>';
    return false;
  }
  pane.innerHTML = `<div class="module-loading">载入中...</div>`;
  if (!pid) { pane.innerHTML = `<div class="chv-empty">该回访未关联患者</div>`; return; }
  const enc = _chvEncodeComponent(pid);
  try {
    if (tab === "info") {
      const p = await _chvFetchJson(`/api/patients/${enc}`);
      if (stale()) return;
      const pt = p.patient || p;
      const kv = (label, v) => `<div class="chv-kv"><small>${label}</small>${escapeHtml(String(v || "—"))}</div>`;
      pane.innerHTML = `<div class="chv-grid2">
        ${kv("姓名", pt.display_name)}${kv("性别/年龄", [pt.sex, pt.age].filter(Boolean).join(" · "))}
        ${kv("手机", pt.phone)}${kv("病历号", pt.chart_no)}
        ${kv("主治医生", pt.responsible_doctor)}${kv("分组", pt.patient_group)}
      </div>`;
    } else if (tab === "emr") {
      const res = await _chvFetchJson(`/api/patients/${enc}/record-items`);
      if (stale()) return;
      const timeline = _chvContractApi().normalizeMedicalRecordTimeline(res);
      const records = timeline.medical_records;
      pane.innerHTML = records.length ? `<div class="chv-tl">${records.slice(0, 10).map(record => {
        const date = record.visit_time || record.updated_at || record.created_at || "日期未记录";
        const recordType = record.record_type || "类型未记录";
        const doctorName = record.doctor_name || "医生未记录";
        const summary = record.items.map(item => item.content)
          .filter(Boolean).slice(0, 3).join("；");
        return `<div class="chv-tli"><span class="chv-sub">${escapeHtml(date.slice(0, 10))}</span>
          <b>${escapeHtml(recordType)} · ${escapeHtml(doctorName)}</b>
          <div class="chv-sub">${escapeHtml(summary || "暂无结构化明细")}</div></div>`;
      }).join("")}</div>` : `<div class="chv-empty">无病历记录</div>`;
    } else if (tab === "hist") {
      // patient 精确匹配(patient_q 是 LIKE,会把 p1 连带出 p10 的历史)
      const res = await _chvFetchJson(
        `/api/return-visits?date_from=2000-01-01&date_to=2099-12-31&status=all&patient=${enc}&pagesize=50`);
      if (stale()) return;
      pane.innerHTML = (res.list || []).length ? `<div class="chv-tl">${res.list.map(r => `
        <div class="chv-tli"><b>${escapeHtml(r.item_name || "")}</b>
        <span class="chv-sub">${escapeHtml(r.due_time || "")}</span> ${_chvStatusBadge(r)}
        ${r.return_result ? `<div class="chv-sub">${escapeHtml(r.return_result)}</div>` : ""}</div>`).join("")}</div>`
        : `<div class="chv-empty">无历史回访</div>`;
    } else if (tab === "img") {
      const res = await _chvFetchJson(`/api/patients/${enc}/images?pagesize=9`);
      if (stale()) return;
      const imgs = res.images || res.list || [];
      pane.innerHTML = imgs.length ? `<div class="chv-imgbox">${imgs.slice(0, 9).map(im => `
        <img src="/api/patients/${enc}/images/${_chvEncodeComponent(im.image_id)}/file" loading="lazy" alt="">`).join("")}</div>`
        : `<div class="chv-empty">无影像</div>`;
    } else if (tab === "comm") {
      // 区分「真失败(网络/500)」与「无权(403)」与「正常空」(不把失败误报成空/无权)
      const probe = async url => {
        try { return {data: await _chvFetchJson(url)}; }
        catch (error) {
          return error && error.status === 403 ? {forbidden: true} : {err: true};
        }
      };
      const [cm, cl] = await Promise.all([
        probe(`/api/patients/${enc}/communications`),
        probe(`/api/patients/${enc}/calls`),
      ]);
      if (stale()) return;
      if (cm.err) { pane.innerHTML = `<div class="chv-empty">沟通记录载入失败,请重试</div>`; return; }
      const comms = (cm.data && cm.data.communications) || [];
      const calls = (cl.data && cl.data.calls) || [];
      pane.innerHTML = `
        <div class="chv-phead2"><h4>沟通时间线</h4>
          ${hasPerm("patient.profile.view") && hasPerm("communication.manage") ? `<button type="button" class="chv-btn" data-chv-addcomm2="1">＋ 新增沟通</button>` : ""}</div>
        ${comms.length ? `<div class="chv-tl">${comms.slice(0, 20).map(c => `
          <div class="chv-tli"><b>${escapeHtml(_chvWayText(c.channel, c.direction))}${c.reason ? " · " + escapeHtml(c.reason) : ""}</b>
          <span class="chv-sub">${escapeHtml(c.contacted_at || "")}</span>
          <div>${escapeHtml(c.content || "")}</div>
          <span class="chv-sub">沟通人:${escapeHtml(c.operator || "—")}</span></div>`).join("")}</div>` : `<div class="chv-empty">无沟通记录</div>`}
        <div class="chv-phead2"><h4>历史电话录音</h4></div>
        ${cl.forbidden ? `<div class="chv-empty">无录音查看权限</div>`
          : cl.err ? `<div class="chv-empty">录音载入失败,请重试</div>`
          : calls.length ? calls.slice(0, 10).map(k => `
            <div class="chv-audio"><button type="button" class="chv-mini" data-chv-callplay="${escapeAttr(k.call_id)}">▶</button>
            <div>${escapeHtml((k.started_at || "").slice(0, 16))} · ${escapeHtml(k.direction === "inbound" ? "呼入" : "呼出")}</div>
            <span>${escapeHtml(String(k.duration_sec || 0))}s</span></div>`).join("")
          : `<div class="chv-empty">无录音</div>`}`;
      pane.querySelectorAll("[data-chv-callplay]").forEach(b => b.addEventListener("click", () => {
        new Audio(`/api/calls/${_chvEncodeComponent(b.dataset.chvCallplay)}/recording`).play()
          .catch(() => window.alert("播放失败"));
      }));
      const add = pane.querySelector("[data-chv-addcomm2]");
      if (add) add.addEventListener("click", () => _chvOpenAddComm(pid));
    }
  } catch (err) {
    if (stale() || !ovl || ovl.removed || ovl.isConnected === false) return false;
    pane.innerHTML = tab === "emr"
      ? `<div class="chv-empty">病历载入失败，请重试</div>`
      : `<div class="chv-empty">载入失败(可能无该板块查看权限)</div>`;
  }
}

// ============ 常用语字典设置弹窗(增删改,整表替换保存) ============

async function _chvOpenDict(name) {
  const titles = {return_type: "回访类型", visit_content: "回访内容常用语",
                  visit_result: "回访结果常用语", comm_reason: "沟通事由"};
  const permission = name === "comm_reason" ? "communication.manage"
    : ["return_type", "visit_content", "visit_result"].includes(name)
      ? "return_visit.manage" : "";
  if (!permission || !_chvHasAllPermissions("patient.profile.view", permission)) return false;
  const lifecycleToken = _chvCaptureLifecycleToken();
  const opener = document.activeElement;
  const parentOverlay = opener && typeof opener.closest === "function"
    ? opener.closest(".chv-ovl") : null;
  const originCurrent = () => _chvLifecycleIsCurrent(lifecycleToken)
    && (!opener || _chvNodeConnected(opener))
    && (!parentOverlay || _chvNodeConnected(parentOverlay));
  let dicts;
  try {
    dicts = await _chvDictsGet(permission);
  } catch (error) {
    if (_chvDictionaryRequestStale(error) || !originCurrent()) return false;
    throw error;
  }
  if (!originCurrent()) return false;
  let items = [...(dicts[name] || [])];
  const ovl = _chvOvl("chvDictOvl", `
    <div class="chv-phead"><h3>${escapeHtml(titles[name] || name)}设置</h3>
      <button type="button" class="chv-btn" data-chv-close="1">✕</button></div>
    <div class="chv-pbody">
      <div id="chvDictList"></div>
      <div class="chv-actions" style="margin-top:8px">
        <input id="chvDictNew" placeholder="新增一条…" style="flex:1">
        <button type="button" class="chv-btn pri" data-chv-add="1">✓ 添加</button>
      </div>
      <div class="chv-actions" style="justify-content:flex-end;margin-top:10px">
        <button type="button" class="chv-btn pri" data-chv-save="1">保存</button>
        <button type="button" class="chv-btn" data-chv-close="1">取消</button>
      </div>
      <div class="chv-note">全诊所共用一套,保存后处处生效;改动留痕</div>
    </div>`);
  _chvGuardDirty(ovl);
  const renderList = () => {
    ovl.querySelector("#chvDictList").innerHTML = items.map((t, i) => `
      <div class="chv-dictrow"><span>${escapeHtml(t)}</span>
        <button type="button" class="chv-mini" data-de="${i}">✎</button>
        <button type="button" class="chv-mini danger" data-dx="${i}">🗑</button></div>`).join("")
      || `<div class="chv-empty">暂无条目</div>`;
    ovl.querySelectorAll("[data-de]").forEach(b => b.addEventListener("click", () => {
      const index = +b.dataset.de;
      if (!Number.isInteger(index) || index < 0 || index >= items.length) return;
      const oldValue = items[index];
      const v = (window.prompt("编辑这条:", oldValue) || "").trim();
      if (v && v !== oldValue) {
        items[index] = v;
        ovl._chvDirty = true;
        renderList();
      }
    }));
    ovl.querySelectorAll("[data-dx]").forEach(b => b.addEventListener("click", () => {
      const index = +b.dataset.dx;
      if (!Number.isInteger(index) || index < 0 || index >= items.length) return;
      items.splice(index, 1);
      ovl._chvDirty = true;
      renderList();
    }));
  };
  renderList();
  ovl.querySelector("[data-chv-add]").addEventListener("click", () => {
    const v = ovl.querySelector("#chvDictNew").value.trim();
    if (!v) return;
    items.push(v);
    ovl._chvDirty = true;
    ovl.querySelector("#chvDictNew").value = "";
    renderList();
  });
  const saveBtn = ovl.querySelector("[data-chv-save]");
  saveBtn.addEventListener("click", async () => {
    if (!_chvHasAllPermissions("patient.profile.view", permission)) return false;
    if (_chvOverlayPending(ovl)) return;
    let result;
    try {
      result = await _chvWriteInOverlay(ovl, saveBtn, "dictionary", {
        action: "update",
        fields: {[name]: items},
      });
    } catch { window.alert("保存失败(网络异常),请重试"); return; }   // 复核轮9-P2:字典保存也捕获网络异常
    if (!result.ok) { _chvWriteError(result, "字典保存"); return; }
    const saved = result.response && result.response.data ? result.response.data : {};
    ovl._chvClean = true;
    _chvDicts = {...(_chvDicts || {}), ...saved};
    const parentSelect = parentOverlay && _chvNodeConnected(parentOverlay)
      ? parentOverlay.querySelector(`[data-chv-dict-insert="${name}"]`) : null;
    if (parentSelect && _chvNodeConnected(parentSelect) && Array.isArray(_chvDicts[name])) {
      parentSelect.innerHTML = `<option value="">插入常用语…</option>`
        + _chvDicts[name].map(value => `<option>${escapeHtml(value)}</option>`).join("");
      parentSelect.selectedIndex = 0;
    }
    _chvDisposeOverlay(ovl);
  });
}

// 区间选择器入口(列表工具条委托)
document.addEventListener("click", e => {
  if (e.target.closest("[data-chv-range]")) _chvOpenRange();
});

Object.assign(window, {
  loadCustomerHubV2,
  requestLeaveCustomerHubV2,
});
