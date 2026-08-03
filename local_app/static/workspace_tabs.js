// 患者工作区 tab 渲染：病历时间线（展示态/编辑态切换）+ 收费/预约/回访/备注/审计。
// 病历编辑器本体在 medical_editor.js；本文件从 workspaceData.detail（patient_workspace.js 缓存）取数据。

// 病历 content_json 科目键映射：已知键给中文标签，未知键兜底原样展示（与 SOURCE_FIELD_GROUPS 同思路）。
const MEDICAL_SECTION_LABELS = {
  PC: "主诉",
  HPI: "现病史",
  PI: "既往史",
  Exam: "检查",
  AE: "辅助检查",
  DG: "诊断",
  TR: "治疗",
  Plan: "医嘱",
  DA: "医嘱",
  Nurse: "护理",
};

// （架构体检遗留③已清：此处原有顶层 medicalTemplateCache 死变量——真缓存是
//   medical_editor.js 的 window.medicalTemplateCache，两者同名不连通，本文件从未读写它。）

function workspaceDetailData() {
  return workspaceData && workspaceData.detail ? workspaceData.detail : null;
}

// 空数据守卫：detail 缓存未就绪时面板显示失败提示并返回 null（各 tab renderer 共用）。
function requireWorkspaceDetail(panel) {
  const data = workspaceDetailData();
  if (!data) panel.textContent = "患者档案载入失败";
  return data;
}

// 单次重取患者详情并更新缓存（不重渲染左栏；竞态守卫同 loadWorkspacePatient）。
// #544:连带拉一次 /summary 重算 tab 角标——此前只拉 detail，各保存流程(病历/回访等)调完
// 这里角标却不重算(停在"病历0/回访0")，统一下沉到这里，各调用方不用各自记得再拉一次。
// 网络异常返回 null，不让 rejection 沿 saveMedicalRecord 链路外溢。
async function refreshWorkspaceDetail() {
  const identity = workspacePatientId;
  if (!identity) return null;
  try {
    const res = await fetch(`/api/patients/${encodeURIComponent(identity)}`);
    if (!res.ok || identity !== workspacePatientId || !workspaceData) return null;
    const data = await res.json();
    if (identity !== workspacePatientId || !workspaceData) return null;
    workspaceData.detail = data;
    workspaceData.patient = data.patient;
    try {
      const summaryRes = await fetch(`/api/patients/${encodeURIComponent(identity)}/summary`);
      if (summaryRes.ok && identity === workspacePatientId) {
        const summary = await summaryRes.json();
        if (typeof renderWorkspaceTabCounts === "function") renderWorkspaceTabCounts(summary.counts || {});
      }
    } catch { /* 角标刷新失败不影响 detail 已刷新的主流程 */ }
    return data;
  } catch {
    return null;
  }
}

// ---------- 病历 tab ----------

function renderWorkspaceMedicalTab(panel) {
  const data = requireWorkspaceDetail(panel);
  if (!data) return;
  // 兜底：只展示正式病历（draft_status 为空），把待审草稿/已丢弃排除在外。
  const records = (data.medical_records || []).filter(row => !(row.draft_status || ""));
  // API 已按 coalesce(visit_time, updated_at, created_at) desc 排序。
  const timeline = records.length
    ? `
      <div class="medical-timeline">
        ${records.map(row => `
          <div class="medical-timeline-item">
            <div class="medical-timeline-date">${escapeHtml(medicalVisitDate(row))}</div>
            <div class="medical-card" data-medical-card="${escapeAttr(row.record_id)}">
              ${renderWorkspaceMedicalCardBody(row)}
            </div>
          </div>
        `).join("")}
      </div>
    `
    : '<div class="workspace-placeholder">暂无病历</div>';
  panel.innerHTML = `
    <div class="medical-tab-toolbar">
      <button type="button" class="medical-new-record-btn" onclick="openNewMedicalRecord()">+ 新建病历</button>
    </div>
    ${timeline}
  `;
  loadMedicalCardItems();
}

// 批量取该患者全部牙位条目，注入各病历展示卡（一次请求，不逐卡打接口）
async function loadMedicalCardItems() {
  if (!workspacePatientId) return;
  let data;
  try {
    const res = await fetch(`/api/patients/${encodeURIComponent(workspacePatientId)}/record-items`);
    if (!res.ok) throw new Error(String(res.status));
    data = await res.json();
  } catch {
    return; // 条目装载失败不影响病历文本展示
  }
  const byRecord = data.records || {};
  document.querySelectorAll("[data-medical-card]").forEach(card => {
    const items = byRecord[card.dataset.medicalCard];
    const slot = card.querySelector("[data-card-items]");
    if (!slot || !items || !items.length) return;
    slot.innerHTML = renderCardItems(items);
  });
}

const CARD_ITEM_TYPE_LABELS = {exam: "检查", diagnosis: "诊断", plan: "治疗方案", treatment: "治疗"};

// 一个科目块：标签出现一次，多条目横向铺在右侧（分割线只在科目之间，不在同科目条目之间）
function medicalSubjectBlock(label, entries) {
  const lines = entries.map(({teeth, text}) => `
    <div class="section-entry">
      ${teeth && teeth.length ? toothCrossHtml(teeth) : ""}
      <span class="field-value">${escapeHtml(text)}</span>
    </div>
  `).join("");
  return `
    <div class="medical-card-section">
      <span class="field-label">${escapeHtml(label)}</span>
      <div class="section-entries">${lines}</div>
    </div>
  `;
}

// 展示卡条目：官方样式（类型标签 + 牙位 + 内容），相邻同内容合并多牙位，再按类型归组
function renderCardItems(items) {
  const rows = [];
  for (const item of items) {
    const last = rows[rows.length - 1];
    if (last && last.type === item.item_type && last.content === item.content) {
      if (item.tooth) last.teeth.push(item.tooth);
    } else {
      rows.push({type: item.item_type, teeth: item.tooth ? [item.tooth] : [], content: item.content});
    }
  }
  const groups = [];
  for (const r of rows) {
    const label = CARD_ITEM_TYPE_LABELS[r.type] || r.type;
    const last = groups[groups.length - 1];
    if (last && last.label === label) last.entries.push({teeth: r.teeth, text: r.content});
    else groups.push({label, entries: [{teeth: r.teeth, text: r.content}]});
  }
  return groups.map(g => medicalSubjectBlock(g.label, g.entries)).join("");
}

// 顶部「+ 新建病历」：对标官方——弹全屏「病历填写」覆盖层（sentinel record_id "new"）。
async function openNewMedicalRecord() {
  openMedicalEditorOverlay({
    record_id: "new",
    record_type: "",
    doctor_name: "",
    visit_time: localDateValue(),
    content_json: "{}",
    tooth_json: "{}",
  });
}

function cancelNewMedicalRecord() {
  closeMedicalEditorOverlay();
}

// 全屏病历填写覆盖层（对标官方：左模板树+右详细病历数据+底部操作条）
function openMedicalEditorOverlay(row) {
  const overlay = document.getElementById("medicalEditorOverlay");
  const body = overlay ? overlay.querySelector("[data-editor-overlay-body]") : null;
  if (!overlay || !body) return;
  body.innerHTML = renderMedicalRecord(row);
  if (typeof bindStaffInputs === "function") bindStaffInputs(body);   // 病历医生选人 datalist
  overlay.hidden = false;
  overlay.dataset.dirty = "";                 // 新开/重开编辑层→未脏(#335)
  body.oninput = markMedicalEditorDirty;      // 文本/日期输入即标脏
  body.onchange = markMedicalEditorDirty;     // 单选/下拉/datalist
  document.body.classList.add("editor-overlay-open");
  hydrateMedicalEditor(row.record_id);
  loadEditableRecordVersions(body);
}

// #335 脏标记:用户改过未保存→dataset.dirty=1。JS 直写字段(牙位/模板/快捷)不触发
// input 事件,那些地方各自调本函数补标。
function markMedicalEditorDirty() {
  const overlay = document.getElementById("medicalEditorOverlay");
  if (overlay && !overlay.hidden) overlay.dataset.dirty = "1";
}

// #335 守卫式关闭:有未保存内容时先确认,避免「退出/×/切模块」静默丢病历。
// force=true 由保存成功路径调用(此时已清脏),跳过确认。
function closeMedicalEditorOverlay(force) {
  const overlay = document.getElementById("medicalEditorOverlay");
  if (!overlay || overlay.hidden) return;
  if (!force && overlay.dataset.dirty === "1"
      && !window.confirm("病历有未保存的内容，确认放弃并退出？")) return;
  overlay.hidden = true;
  overlay.dataset.dirty = "";
  const body = overlay.querySelector("[data-editor-overlay-body]");
  if (body) body.innerHTML = "";
  document.body.classList.remove("editor-overlay-open");
  closeToothSelector();
  // 扫荡#393:暂存(keepOpen)已创建记录,但取消/返回只关层、不重渲病历 tab → 列表仍「暂无病历」。
  // 关层后按当前(暂存已 refreshWorkspaceDetail 过的)缓存重渲病历 tab。force=true 是保存成功路径,
  // 它自己会重渲,跳过避免双渲。
  if (!force) {
    const panel = document.querySelector('[data-workspace-panel="medical"]');
    if (panel && typeof renderWorkspaceMedicalTab === "function") renderWorkspaceMedicalTab(panel);
  }
}


function medicalVisitDate(row) {
  return String(row.visit_time || row.updated_at || "").slice(0, 10) || "未知日期";
}

// 展示态卡片：头部（就诊日期 + 类型徽标 + 医生 + 牙位徽标）+ content_json 科目分区。
const MEDICAL_SECTION_ORDER = ["PC", "HPI", "PI", "Exam", "AE", "DG", "TR", "Plan", "DA", "Nurse"];
// SaaS 同步的内部字段，不在病历卡展示
const HIDDEN_CONTENT_KEYS = new Set(["EMRVersion", "EMRVERSION", "emrversion", "Nurse"]);

// 单科目展示：SaaS结构化值解析成 牙位+文本 行；空 item 不渲染；普通值原样
function medicalCardSection(key, value) {
  const label = MEDICAL_SECTION_LABELS[key] || key;
  const entries = parseSaasSubject(value);
  if (entries !== null) {
    if (!entries.length) return "";
    return medicalSubjectBlock(label, entries.map(e => ({teeth: e.teeth, text: e.text})));
  }
  const text = sourceValueText(value);
  if (!text) return "";
  return medicalSubjectBlock(label, [{teeth: [], text}]);
}

function renderWorkspaceMedicalCardBody(row) {
  const content = parseMedicalContent(row.content_json);
  // 官方科目顺序优先，未知科目殿后，内部字段隐藏
  const orderedKeys = [
    ...MEDICAL_SECTION_ORDER.filter(key => key in content),
    ...Object.keys(content).filter(key => !MEDICAL_SECTION_ORDER.includes(key)),
  ].filter(key => !HIDDEN_CONTENT_KEYS.has(key));
  const sections = orderedKeys
    .map(key => medicalCardSection(key, content[key]))
    .join("");
  // 护士显示在卡头（官方样式），不再出现在正文科目区
  const nurse = sourceValueText(content.Nurse);
  return `
    <div class="medical-card-head official-card-head">
      <span class="card-head-field">就诊时间：<strong>${escapeHtml(formatVisitTimeText(row.visit_time || row.updated_at || ""))}</strong></span>
      <span class="card-head-field">就诊诊所：${escapeHtml(window.CLINIC_NAME || "口腔门诊部")}</span>
      ${row.doctor_name ? `<span class="card-head-field">医生：${escapeHtml(row.doctor_name)}</span>` : ""}
      ${nurse ? `<span class="card-head-field">护士：${escapeHtml(nurse)}</span>` : ""}
      ${row.record_type ? `<span class="record-type-stamp">${escapeHtml(row.record_type)}</span>` : ""}
      <span class="card-head-actions">
        <button type="button" class="plain-button medical-card-edit" onclick="editWorkspaceMedicalCard('${escapeAttr(row.record_id)}')">修改</button>
      </span>
    </div>
    <div class="medical-card-body">
      ${sections || '<span class="empty">无病历内容</span>'}
      <div data-card-items></div>
    </div>
  `;
}

function findWorkspaceMedicalRecord(recordId) {
  const data = workspaceDetailData();
  if (!data) return null;
  return (data.medical_records || []).find(row => String(row.record_id) === String(recordId)) || null;
}

function workspaceMedicalCardEl(recordId) {
  return document.querySelector(`[data-medical-card="${cssEscape(recordId)}"]`);
}

// 「修改」：对标官方——弹全屏「病历填写」覆盖层编辑该病历。
function editWorkspaceMedicalCard(recordId) {
  const row = findWorkspaceMedicalRecord(recordId);
  if (!row) return;
  openMedicalEditorOverlay(row);
}

// 编辑态 → 展示态（不保存，用缓存数据回渲染）。
function cancelWorkspaceMedicalCard(recordId) {
  const row = findWorkspaceMedicalRecord(recordId);
  const card = workspaceMedicalCardEl(recordId);
  if (!row || !card) return;
  card.innerHTML = renderWorkspaceMedicalCardBody(row);
}

// 保存成功后由 saveMedicalRecord（medical_editor.js）调用：关覆盖层+整 tab 重渲染。
async function refreshWorkspaceMedicalCard(recordId) {
  const data = await refreshWorkspaceDetail();
  if (!data) {
    // 数据已保存成功，只是重取详情失败：留在覆盖层并如实提示，不误报保存失败。
    const status = document.querySelector("#medicalEditorOverlay .record-save-status");
    if (status) status.textContent = "已保存，但刷新失败，请关闭后重新打开";
    return;
  }
  closeMedicalEditorOverlay();
  const panel = document.querySelector('[data-workspace-panel="medical"]');
  if (panel) renderWorkspaceMedicalTab(panel);
}

// ---------- 预约 / 回访 tab ----------

async function renderWorkspaceAppointmentsTab(panel) {
  const data = requireWorkspaceDetail(panel);
  if (!data) return;
  panel.innerHTML = recordsPanel("预约", data.appointments || [], renderAppointment);
  await loadEditableRecordVersions(panel);
}

let _rvStaffCache = [];        // 回访 tab 的人员库(回访医生/回访人下拉用)
const RV_TYPES = ["电话", "到店", "微信", "其他"];
const RV_STATUSES = ["待回访", "已回访"];
const RV_INTENTS = ["高意向", "中意向", "低意向", "无意向"];        // P2-19 意向等级
const RV_SATISFACTIONS = ["非常满意", "满意", "一般", "不满意"];   // P2-19 满意度

function rvStaffOptions(selected, role, allLabel) {
  const list = role ? _rvStaffCache.filter(s => s.role === role) : _rvStaffCache;
  return [`<option value="">${allLabel || "请选择"}</option>`].concat(
    list.map(s => `<option value="${escapeAttr(s.name)}"${s.name === selected ? " selected" : ""}>${escapeHtml(s.name)}${(!role && s.role) ? `（${escapeHtml(s.role)}）` : ""}</option>`),
    // 历史值不在人员库时也保留可选
    (selected && !list.some(s => s.name === selected)) ? [`<option value="${escapeAttr(selected)}" selected>${escapeHtml(selected)}</option>`] : [],
  ).join("");
}

function rvOptions(values, selected) {
  return [`<option value="">请选择</option>`].concat(
    values.map(v => `<option value="${escapeAttr(v)}"${v === selected ? " selected" : ""}>${escapeHtml(v)}</option>`),
    (selected && !values.includes(selected)) ? [`<option value="${escapeAttr(selected)}" selected>${escapeHtml(selected)}</option>`] : [],
  ).join("");
}

// 「客户沟通」tab(原「回访」):内部分三子tab —— 回访(回访卡+录音) / 沟通咨询(步骤2接入) / 面诊(并入的原顶层面诊)
let _csSubTab = "回访";
const CS_SUBTABS = ["回访", "沟通咨询", "面诊"];

function renderWorkspaceReturnVisitsTab(panel) {
  if (!requireWorkspaceDetail(panel)) return;   // 无数据时保留其载入/失败提示，不覆盖
  _csSubTab = "回访";   // #710 每次(重)载客户沟通 tab 都回到默认「回访」子tab,不跨患者残留(换患者 loadedTabs.clear 会重跑本函数),保「今日待回访/跟进」深链落回访
  panel.innerHTML = `
    <div class="module-subtabs cs-subtabs">
      ${CS_SUBTABS.map(k => `<button type="button" class="module-subtab" data-cs-sub="${k}">${k}</button>`).join("")}
    </div>
    <div class="cs-sub-body" data-cs-sub-body><div class="module-loading">载入中...</div></div>`;
  panel.querySelectorAll("[data-cs-sub]").forEach(btn =>
    btn.addEventListener("click", () => { _csSubTab = btn.dataset.csSub; renderCsSubTabs(panel); loadCsSubTab(panel); }));
  renderCsSubTabs(panel);
  loadCsSubTab(panel);
}

function renderCsSubTabs(panel) {
  panel.querySelectorAll("[data-cs-sub]").forEach(b => b.classList.toggle("active", b.dataset.csSub === _csSubTab));
}

function loadCsSubTab(panel) {
  const body = panel.querySelector("[data-cs-sub-body]");
  if (!body) return;
  body.dataset.csCur = _csSubTab;   // #709 竞态守卫:记下本次要渲染的子tab,异步渲染回来前若已切走则丢弃过期结果
  if (_csSubTab === "面诊") return renderWorkspaceConsultTab(body);   // 并入的原面诊，逻辑不变
  if (_csSubTab === "沟通咨询") return renderCommunicationsSub(body);   // 电话/微信沟通记录 + 截图 + 录音
  return renderCsReturnVisitsSub(body);
}

// 明确「回访意图」入口(左卡片「跟进」等)调用:强制客户沟通 tab 落到「回访」子tab —— 即使面板已加载在别的子tab也切回来
function showCustomerCommReturnVisit() {
  _csSubTab = "回访";
  const page = (typeof workspacePageEl === "function") ? workspacePageEl() : null;
  const panel = page && page.querySelector('[data-workspace-panel="return-visits"]');
  if (panel && panel.querySelector("[data-cs-sub]")) { renderCsSubTabs(panel); loadCsSubTab(panel); }
}

// 回访子tab:新增回访/计划表单 + 回访卡列表 + 通话录音(原「回访」tab 全部内容原样搬入)
async function renderCsReturnVisitsSub(panel) {
  const data = requireWorkspaceDetail(panel);
  if (!data) return;
  const pid = workspacePatientId;   // #19 竞态守卫：await 期间可能换患者
  try { _rvStaffCache = (await (await fetch("/api/staff-members")).json()).members || []; } catch { _rvStaffCache = []; }  // #111 接口返回 members 不是 list
  try { const c = await (await fetch("/api/settings/clinic")).json(); if (c && c.name) RV_CLINIC = c.name; } catch { /* 用默认 */ }
  if (pid !== workspacePatientId || !workspaceData) return;   // 已换患者/离开 → 丢弃,不拿旧data渲染
  if (panel.dataset.csCur && panel.dataset.csCur !== "回访") return;   // #709 await 期间已切到别的子tab → 过期渲染不写回共享 body
  const header = `
    <div class="rv-tab-head">
      <button type="button" class="rv-new-btn" onclick="showReturnVisitForm('回访')">＋ 新增回访</button>
      <button type="button" class="rv-new-btn rv-new-plan" onclick="showReturnVisitForm('计划')">＋ 新增计划</button>
    </div>`;
  const addForm = `
    <section class="panel" data-new-rv-panel hidden><div class="panel-head"><span data-new-rv-title>新增回访</span></div>
      <div class="panel-body cs-form" data-new-rv>
        <input type="hidden" data-rv="status" value="待回访">
        <div class="cs-grid">
          <label>回访时间 <input class="cs-input" data-rv="due_time" placeholder="2026-06-20"></label>
          <label>回访内容 <input class="cs-input" data-rv="item_name" placeholder="如 种牙复诊"></label>
          <label>回访医生 <select class="cs-input" data-rv="return_doctor">${rvStaffOptions("", "医生", "选择医生")}</select></label>
          <label>回访人 <select class="cs-input" data-rv="visitor">${rvStaffOptions("", "", "选择回访人")}</select></label>
          <label>回访类型 <select class="cs-input" data-rv="return_type">${rvOptions(RV_TYPES, "")}</select></label>
        </div>
        <div class="cs-actions"><button type="button" onclick="createReturnVisit()">保存</button>
          <button type="button" class="plain-button" onclick="hideReturnVisitForm()">取消</button>
          <span data-rv-status></span></div>
      </div></section>`;
  panel.innerHTML = header + addForm + recordsPanel("回访", data.return_visits || [], renderReturnVisit)
    + '<div data-consult-calls><div class="panel-body">通话录音加载中...</div></div>';
  await loadEditableRecordVersions(panel);
  // 通话录音并入回访(回访+录音一起管理):录音上传/手机录音/回放/成交状态
  const cc = panel.querySelector("[data-consult-calls]");
  if (cc && typeof loadCallsTab === "function") loadCallsTab(cc);
}

// 左上角「新增回访/新增计划」→ 展开录入表单(计划=待回访待办, 回访=实际执行记录)
function showReturnVisitForm(mode) {
  const page = workspacePageEl();
  const sec = page && page.querySelector("[data-new-rv-panel]");
  if (!sec) return;
  sec.hidden = false;
  const title = sec.querySelector("[data-new-rv-title]");
  if (title) title.textContent = mode === "计划" ? "新增计划（待回访）" : "新增回访";
  const statusEl = sec.querySelector('[data-rv="status"]');
  if (statusEl) statusEl.value = mode === "计划" ? "待回访" : "待回访";
}
function hideReturnVisitForm() {
  const page = workspacePageEl();
  const sec = page && page.querySelector("[data-new-rv-panel]");
  if (sec) sec.hidden = true;
}

async function createReturnVisit() {
  const page = workspacePageEl();
  const form = page && page.querySelector("[data-new-rv]");
  if (!form) return;
  const status = form.querySelector("[data-rv-status]");
  const payload = {};
  form.querySelectorAll("[data-rv]").forEach(i => { payload[i.dataset.rv] = i.value.trim(); });
  if (!payload.item_name || !payload.due_time) { if (status) status.textContent = "回访时间和项目都要填"; return; }
  if (status) status.textContent = "保存中...";
  let res;
  try {
    res = await fetch(`/api/patients/${encodeURIComponent(workspacePatientId)}/return-visits`, {
      method: "POST", headers: {"Content-Type": "application/json"}, body: JSON.stringify(payload),
    });
  } catch { if (status) status.textContent = "保存失败（网络异常）"; return; }
  if (!res.ok) { const m = await res.json().catch(() => ({})); if (status) status.textContent = "保存失败：" + (m.detail || res.status); return; }
  if (typeof refreshWorkspaceDetail === "function") await refreshWorkspaceDetail();
  workspaceLoadedTabs.delete("return-visits");
  evictVisitsCache();
  switchWorkspaceTab("return-visits");
  if (typeof loadAuditLogs === "function") await loadAuditLogs();
}

function renderAppointment(row) {
  const id = escapeAttr(row.appointment_id);
  // 时间去掉秒/微秒尾巴(YYYY-MM-DD HH:MM)，有结束时间则补 ~HH:MM
  const start = String(row.start_time || "").slice(0, 16);
  const endHm = String(row.end_time || "").slice(11, 16);
  const timeText = start + (endHm ? ` ~ ${endHm}` : "");
  const statusBadge = (typeof appointmentStatusBadge === "function")
    ? appointmentStatusBadge(row.status)
    : `<span>${escapeHtml(row.status || "")}</span>`;
  // 改约徽标：本地改过时间(当前 start_time ≠ SaaS 原约 original_start_time,后端派生)→ 标"改约·原X"。
  // 本地编辑只动 start_time 不动 SaaS 原始镜像,故两者一比即知;同日显示时分,跨日显示月-日 时分。
  const origHm = String(row.original_start_time || "").trim().slice(0, 16);
  const rescheduled = origHm && origHm !== start;
  const origShow = (origHm.slice(0, 10) === start.slice(0, 10)) ? origHm.slice(11) : origHm.slice(5);
  const reschedBadge = rescheduled
    ? `<span class="appt-resched-badge" title="SaaS 原约：${escapeAttr(origHm)}，本地已改约">改约·原${escapeHtml(origShow)}</span>`
    : "";
  return `
    <div class="record-row appointment-editor" data-appointment-id="${id}">
      <div class="appt-rec-view">
        <div class="appt-rec-main">
          <strong>${escapeHtml(timeText || "(无时间)")}</strong>
          ${reschedBadge}
          ${row.doctor_name ? `<span class="appt-rec-doc">${escapeHtml(row.doctor_name)}</span>` : ""}
          ${row.item_name ? `<span class="appt-rec-item">${escapeHtml(row.item_name)}</span>` : ""}
          ${statusBadge}
        </div>
        <button type="button" class="plain-button" onclick="toggleAppointmentEdit('${id}')">编辑</button>
      </div>
      <div class="appt-rec-edit" hidden>
        <div class="record-meta">
          <input data-appointment-field="start_time" value="${escapeAttr(row.start_time || "")}" placeholder="开始时间 YYYY-MM-DD HH:MM">
          <input data-appointment-field="end_time" value="${escapeAttr(row.end_time || "")}" placeholder="结束时间">
          <input data-appointment-field="doctor_name" data-staff-role="医生" value="${escapeAttr(row.doctor_name || "")}" placeholder="医生">
        </div>
        <div class="record-meta">
          <input data-appointment-field="item_name" value="${escapeAttr(row.item_name || "")}" placeholder="预约项目">
          <input data-appointment-field="status" value="${escapeAttr(row.status || "")}" placeholder="状态">
        </div>
        <button type="button" onclick="saveAppointment('${id}')">保存预约</button>
        <span class="record-save-status" id="appointmentStatus-${id}"></span>
      </div>
      ${recordVersionList("预约", "appointment", row.appointment_id)}
    </div>
  `;
}

// 切换预约行的 只读↔编辑
function toggleAppointmentEdit(appointmentId) {
  const c = document.querySelector(`.appointment-editor[data-appointment-id="${cssEscape(appointmentId)}"]`);
  if (!c) return;
  const edit = c.querySelector(".appt-rec-edit");
  if (edit) edit.hidden = !edit.hidden;
}

let RV_CLINIC = "口腔门诊部";   // 就诊诊所名(从 /api/settings/clinic 读，可在设置里改；默认=后端中性默认)

// 同步来的回访文本常带转义的 \n 和包裹引号 → 还原换行、去引号后安全渲染
function rvText(s) {
  let t = String(s || "");
  t = t.replace(/\\r\\n|\\n|\\r/g, "\n").replace(/\\"/g, '"').replace(/\\t/g, " ").trim();
  if (t.length >= 2 && t.startsWith('"') && t.endsWith('"')) t = t.slice(1, -1).trim();
  return escapeHtml(t).replace(/\n/g, "<br>");
}

// 患者档案回访卡(对标官方"回访就诊")：头部(时间/回访人/诊所/斜章/操作)+正文(就诊时间/回访内容/回访类型)
function renderReturnVisit(row) {
  const id = escapeAttr(row.return_visit_id);
  const revision = escapeAttr(row.revision);
  const done = String(row.status || "").includes("已回访") || String(row.status || "").includes("已完成");
  return `
    <div class="record-row return-visit-editor rv-card${done ? " rv-done" : ""}" data-return-visit-id="${id}" data-return-visit-revision="${revision}">
      <div class="rv-card-head">
        <span class="rv-card-time">${escapeHtml(String(row.due_time || "").slice(0, 16) || "(无时间)")}</span>
        ${row.visitor ? `<span class="rv-card-field">回访人：<b>${escapeHtml(row.visitor)}</b></span>` : ""}
        <span class="rv-card-field">就诊诊所：<b>${escapeHtml(RV_CLINIC)}</b></span>
        <span class="rv-stamp ${done ? "rv-stamp-done" : "rv-stamp-pending"}">${done ? "已回访" : "未回访"}</span>
        <span class="rv-card-actions">
          <button type="button" class="rv-act" onclick="toggleReturnVisitEdit('${id}')">${done ? "编辑" : "回访"}</button>
          <button type="button" class="rv-act rv-act-del" onclick="deleteReturnVisitRow('${id}')">删除</button>
          <button type="button" class="rv-act" onclick="toggleReturnVisitLog('${id}')">日志</button>
        </span>
      </div>
      <div class="rv-card-body">
        <div class="rv-card-line">就诊时间：${escapeHtml(row.actual_date || "")}</div>
        <div class="rv-card-line">回访内容：${rvText(row.item_name)}</div>
        ${row.note ? `<div class="rv-card-note">${rvText(row.note)}</div>` : ""}
        ${row.return_result ? `<div class="rv-card-line">回访结果：${rvText(row.return_result)}</div>` : ""}
        <div class="rv-card-line">回访类型：${escapeHtml(row.return_type || "")}</div>
        ${(row.intent_level || row.satisfaction) ? `<div class="rv-card-line">${row.intent_level ? `意向：${escapeHtml(row.intent_level)}　` : ""}${row.satisfaction ? `满意度：${escapeHtml(row.satisfaction)}` : ""}</div>` : ""}
      </div>
      <div class="appt-rec-edit rv-edit" hidden>
        <div class="record-meta">
          <input data-return-visit-field="due_time" value="${escapeAttr(row.due_time || "")}" placeholder="应回访时间">
          <input data-return-visit-field="item_name" value="${escapeAttr(row.item_name || "")}" placeholder="回访内容/事项">
          <select data-return-visit-field="status">${rvOptions(RV_STATUSES, row.status || "")}</select>
        </div>
        <div class="record-meta">
          <select data-return-visit-field="return_type">${rvOptions(RV_TYPES, row.return_type || "")}</select>
          <select data-return-visit-field="visitor">${rvStaffOptions(row.visitor || "", "", "选择回访人")}</select>
          <select data-return-visit-field="return_doctor">${rvStaffOptions(row.return_doctor || "", "医生", "选择医生")}</select>
        </div>
        <div class="record-meta">
          <select data-return-visit-field="intent_level">${rvOptions(RV_INTENTS, row.intent_level || "")}</select>
          <select data-return-visit-field="satisfaction">${rvOptions(RV_SATISFACTIONS, row.satisfaction || "")}</select>
        </div>
        <textarea data-return-visit-field="note" placeholder="回访记录（回访时说了什么）">${escapeHtml(row.note || "")}</textarea>
        <textarea data-return-visit-field="return_result" placeholder="回访结果">${escapeHtml(row.return_result || "")}</textarea>
        <div class="rv-actions">
          ${done ? "" : `<button type="button" class="rv-done-btn" onclick="markReturnDone('${id}')">标记已回访</button>`}
          <button type="button" onclick="saveReturnVisit('${id}')">保存</button>
          <span class="record-save-status" id="returnVisitStatus-${id}"></span>
        </div>
      </div>
      <div class="rv-log" hidden>${recordVersionList("回访", "return_visit", row.return_visit_id)}</div>
    </div>
  `;
}

function toggleReturnVisitEdit(returnVisitId) {
  const c = document.querySelector(`.return-visit-editor[data-return-visit-id="${cssEscape(returnVisitId)}"]`);
  if (!c) return;
  const edit = c.querySelector(".rv-edit");
  if (edit) edit.hidden = !edit.hidden;
}

function toggleReturnVisitLog(returnVisitId) {
  const c = document.querySelector(`.return-visit-editor[data-return-visit-id="${cssEscape(returnVisitId)}"]`);
  if (!c) return;
  const log = c.querySelector(".rv-log");
  if (log) log.hidden = !log.hidden;
}

async function refreshReturnVisitWorkspaceFromServer() {
  await refreshWorkspaceDetail();
  workspaceLoadedTabs.delete("return-visits");
  evictVisitsCache();
  switchWorkspaceTab("return-visits");
}

// 患者档案里删除一条回访(软删+版本快照)，删后刷新回访 tab
async function deleteReturnVisitRow(returnVisitId) {
  const container = document.querySelector(`.return-visit-editor[data-return-visit-id="${cssEscape(returnVisitId)}"]`);
  const revision = Number(container && container.dataset.returnVisitRevision);
  if (!Number.isInteger(revision) || revision < 1) {
    window.alert("删除失败：回访记录版本已失效，请刷新后重试");
    return;
  }
  if (!window.confirm("删除该回访记录？（软删，保留版本快照可追溯）")) return;
  try {
    const res = await fetch(`/api/return-visits/${encodeURIComponent(returnVisitId)}`, {
      method: "DELETE",
      headers: {"Content-Type": "application/json"},
      body: JSON.stringify({expected_revision: revision}),
    });
    if (res.status === 409) {
      window.alert("数据已被其他操作更新，请确认最新内容后重试");
      await refreshReturnVisitWorkspaceFromServer();
      return;
    }
    if (!res.ok) { const m = await res.json().catch(() => ({})); window.alert("删除失败：" + (m.detail || res.status)); return; }
  } catch { window.alert("删除失败（网络异常）"); return; }
  await refreshReturnVisitWorkspaceFromServer();
  if (typeof loadAuditLogs === "function") loadAuditLogs();
}

// 一键标记已回访：只提交状态，实际回访日期由服务端用北京时间派生。
function markReturnDone(returnVisitId) {
  const c = document.querySelector(`.return-visit-editor[data-return-visit-id="${cssEscape(returnVisitId)}"]`);
  if (!c) return;
  const statusEl = c.querySelector('[data-return-visit-field="status"]');
  if (statusEl) statusEl.value = "已回访";
  saveReturnVisit(returnVisitId);
}

async function saveAppointment(appointmentId) {
  const container = document.querySelector(`.appointment-editor[data-appointment-id="${cssEscape(appointmentId)}"]`);
  const status = document.getElementById(`appointmentStatus-${appointmentId}`);
  const payload = {};
  container.querySelectorAll("[data-appointment-field]").forEach(input => {
    payload[input.dataset.appointmentField] = input.value.trim();
  });
  status.textContent = "保存中...";
  let res;
  try {
    res = await fetch(`/api/appointments/${encodeURIComponent(appointmentId)}`, {
      method: "PUT",
      headers: {"Content-Type": "application/json"},
      body: JSON.stringify(payload),
    });
  } catch {
    status.textContent = "保存失败（网络异常），请确认后重试";
    return;
  }
  status.textContent = res.ok ? "已保存" : "保存失败";
  if (res.ok) {
    evictVisitsCache();  // #57 预约改动影响就诊时间轴
    await loadRecordVersions("appointment", appointmentId, container.querySelector("[data-record-versions]"));
    await loadAuditLogs();
  }
}

async function saveReturnVisit(returnVisitId) {
  const container = document.querySelector(`.return-visit-editor[data-return-visit-id="${cssEscape(returnVisitId)}"]`);
  const status = document.getElementById(`returnVisitStatus-${returnVisitId}`);
  const revision = Number(container && container.dataset.returnVisitRevision);
  if (!Number.isInteger(revision) || revision < 1) {
    if (status) status.textContent = "保存失败：回访记录版本已失效，请刷新后重试";
    return;
  }
  const payload = {expected_revision: revision};
  container.querySelectorAll("[data-return-visit-field]").forEach(input => {
    payload[input.dataset.returnVisitField] = input.value.trim();
  });
  status.textContent = "保存中...";
  let res;
  try {
    res = await fetch(`/api/return-visits/${encodeURIComponent(returnVisitId)}`, {
      method: "PUT",
      headers: {"Content-Type": "application/json"},
      body: JSON.stringify(payload),
    });
  } catch {
    status.textContent = "保存失败（网络异常），请确认后重试";
    return;
  }
  if (res.status === 409) {
    status.textContent = "数据已被其他操作更新，请确认最新内容后重试";
    await refreshReturnVisitWorkspaceFromServer();
    return;
  }
  status.textContent = res.ok ? "已保存" : "保存失败";
  if (res.ok) {
    // #544:保存(含标记已回访)成功后重渲整个tab，让已回访状态/戳/按钮立即生效，
    // 不再停在编辑态；同 createReturnVisit 一致的收尾顺序。
    await refreshReturnVisitWorkspaceFromServer();
    if (typeof loadAuditLogs === "function") await loadAuditLogs();
  }
}

// ---------- 处置 tab（只读，独立端点懒加载）----------

async function renderWorkspaceTreatmentsTab(panel) {
  const identity = workspacePatientId;
  panel.textContent = "加载中...";
  let data;
  try {
    const res = await fetch(`/api/patients/${encodeURIComponent(identity)}/treatments`);
    // 竞态守卫：await 期间切换患者，过期延续不再渲染。
    if (identity !== workspacePatientId) return;
    if (!res.ok) {
      // 失败可重试：取消已加载标记，再点 tab 重新 fetch。
      workspaceLoadedTabs.delete("treatments");
      panel.textContent = "处置记录载入失败";
      return;
    }
    data = await res.json();
    if (identity !== workspacePatientId) return;
  } catch {
    if (identity !== workspacePatientId) return;
    workspaceLoadedTabs.delete("treatments");
    panel.textContent = "处置记录载入失败";
    return;
  }
  const visits = data.visits || [];
  const unlinked = data.unlinked_items || [];
  const hasFinance = visits.some(visit =>
    Object.prototype.hasOwnProperty.call(visit, "bill_total_fee")
      || treatmentItemsHaveFinance(visit.items || []))
    || treatmentItemsHaveFinance(unlinked);
  const note = hasFinance
    ? '<div class="treatments-note">明细金额仅供参考，结算以收费单为准</div>'
    : "";
  if (!visits.length && !unlinked.length) {
    panel.innerHTML = `${treatmentsToolbar()}<div data-local-orders></div>${note}<div class="workspace-placeholder">暂无历史处置记录</div>`;
    if (typeof loadLocalOrders === "function") loadLocalOrders();
    return;
  }
  panel.innerHTML = `
    ${treatmentsToolbar()}
    <div data-local-orders></div>
    ${note}
    ${visits.map((visit, index) => renderTreatmentVisit(visit, index === 0)).join("")}
    ${unlinked.length ? renderUnlinkedTreatments(unlinked) : ""}
  `;
  if (typeof loadLocalOrders === "function") loadLocalOrders();
}

function treatmentsToolbar() {
  // #371:主流程是编辑挂号到诊自动生成的处置单(下方「本地处置单」卡的「编辑」),
  // 故「+ 新增处置」弱化为次级按钮并改名「另开处置单」,只用于不挂号/补开独立单的场景,
  // 避免和自动单并行导致医生归属/业绩/收费责任人混淆。
  return '<div class="treatments-toolbar">'
    + '<button type="button" class="plain-button treatments-add-extra" onclick="openTreatmentOrder()" '
    + 'title="已挂号到诊会自动生成处置单，请优先在下方「编辑」那张；本按钮用于另开一张独立处置单（如未挂号/补单）">+ 另开处置单</button>'
    + '<button type="button" class="consent-entry-btn" onclick="openConsentForm(workspacePatientId)">📝 知情同意书签字</button>'
    + '</div>';
}

function treatmentItemsHaveFinance(items) {
  return items.some(item =>
    Object.prototype.hasOwnProperty.call(item, "unit_price")
      || Object.prototype.hasOwnProperty.call(item, "total_fee"));
}

// 按就诊折叠卡片：默认最近一次（首张）展开，其余折叠。
function renderTreatmentVisit(visit, open) {
  const date = String(visit.bill_time || "").slice(0, 10) || "未知日期";
  const doctors = (visit.doctor_names || []).join(", ");
  const total = Object.prototype.hasOwnProperty.call(visit, "bill_total_fee")
    ? `<span class="treatment-visit-total">合计 ${escapeHtml(formatMoney(visit.bill_total_fee))}</span>`
    : "";
  return `
    <details class="treatment-visit"${open ? " open" : ""}>
      <summary>
        <strong>${escapeHtml(date)}</strong>
        ${visit.bill_no ? `<span class="treatment-visit-no">${escapeHtml(visit.bill_no)}</span>` : ""}
        ${doctors ? `<span class="treatment-visit-doctor">${escapeHtml(doctors)}</span>` : ""}
        ${total}
      </summary>
      ${renderTreatmentItemsTable(visit.items || [])}
    </details>
  `;
}

function renderUnlinkedTreatments(items) {
  return `
    <details class="treatment-visit treatment-unlinked">
      <summary>
        <strong>未关联就诊</strong>
        <span class="treatment-visit-total">${items.length} 条</span>
      </summary>
      ${renderTreatmentItemsTable(items)}
    </details>
  `;
}

function renderTreatmentItemsTable(items) {
  const hasFinance = treatmentItemsHaveFinance(items);
  return `
    <table class="treatment-items">
      <thead>
        <tr><th>项目</th>${hasFinance ? "<th>单价</th>" : ""}<th>数量</th>${hasFinance ? "<th>金额</th>" : ""}<th>分类</th></tr>
      </thead>
      <tbody>
        ${items.map(item => `
          <tr>
            <td>
              ${escapeHtml(item.item_name || "")}
              ${Number(item.is_refund) === 1 ? '<span class="treatment-refund-badge">退费</span>' : ""}
            </td>
            ${hasFinance ? `<td>${Object.prototype.hasOwnProperty.call(item, "unit_price") ? escapeHtml(formatMoney(item.unit_price)) : ""}</td>` : ""}
            <td>${escapeHtml(item.quantity ?? "")}</td>
            ${hasFinance ? `<td>${Object.prototype.hasOwnProperty.call(item, "total_fee") ? escapeHtml(formatMoney(item.total_fee)) : ""}</td>` : ""}
            <td>${item.fee_type ? `<span class="treatment-fee-badge">${escapeHtml(item.fee_type)}</span>` : ""}</td>
          </tr>
        `).join("")}
      </tbody>
    </table>
  `;
}

// ---------- 备注 tab ----------

function renderWorkspaceNotesTab(panel) {
  const data = requireWorkspaceDetail(panel);
  if (!data) return;
  panel.innerHTML = `
    ${recordsPanel("本地备注", data.local_notes || [], note => `
      <div class="record-row">${escapeHtml(note.note_text)}<div class="patient-sub">${escapeHtml(note.updated_at)}</div></div>
    `)}
    <section class="panel">
      <div class="panel-head">新增本地备注</div>
      <div class="panel-body">
        <textarea id="noteText"></textarea>
        <button type="button" onclick="saveWorkspaceNote('${escapeAttr(workspacePatientId)}')">保存备注</button>
        <span id="noteSaveStatus"></span>
      </div>
    </section>
  `;
}

async function saveWorkspaceNote(patientId) {
  const continuation = captureWorkspaceContinuation(patientId);
  const note = document.getElementById("noteText").value.trim();
  const status = document.getElementById("noteSaveStatus");
  if (!note) {
    status.textContent = "请输入备注";
    return;
  }
  status.textContent = "保存中...";
  let res;
  try {
    res = await fetch(`/api/patients/${encodeURIComponent(patientId)}/notes`, {
      method: "POST",
      headers: {"Content-Type": "application/json"},
      body: JSON.stringify({note_text: note}),
    });
  } catch {
    if (!workspaceContinuationIsCurrent(continuation)) return;
    status.textContent = "保存失败（网络异常），请确认后重试";
    return;
  }
  if (!workspaceContinuationIsCurrent(continuation)) return;
  if (!res.ok) {
    status.textContent = "保存失败";
    return;
  }
  // 重载档案（刷新左栏和 tab 计数）后重渲染备注列表。
  await loadWorkspacePatient(patientId, continuation.renderRevision);
  if (!workspaceContinuationIsCurrent(continuation)) return;
  const page = workspacePageEl();
  const panel = page ? page.querySelector('[data-workspace-panel="notes"]') : null;
  if (panel) renderWorkspaceNotesTab(panel);
  await loadAuditLogs();
}

// ---------- 审计 tab ----------

async function renderWorkspaceAuditTab(panel) {
  const id = workspacePatientId;
  panel.innerHTML = `
    <section class="panel">
      <div class="panel-head">版本记录<span>患者资料</span></div>
      <div class="panel-body version-list" data-patient-versions="${escapeAttr(id)}">版本记录载入中...</div>
    </section>
    <section class="panel">
      <div class="panel-head">关联审计<span>患者 / 病历 / 预约 / 回访</span></div>
      <div class="panel-body patient-audit-list" data-patient-audits="${escapeAttr(id)}">审计记录载入中...</div>
    </section>
  `;
  const results = await Promise.all([loadPatientVersions(id, panel), loadPatientAudits(id, panel)]);
  // 失败可重试：任一子面板载入失败时取消已加载标记，再点 tab 重新 fetch（同 visits/treatments tab）。
  if (results.includes(false)) workspaceLoadedTabs.delete("audit");
}

// 返回是否载入成功（false = 失败，供 renderWorkspaceAuditTab 取消已加载标记）。
async function loadPatientVersions(patientId, target) {
  const container = target.querySelector(`[data-patient-versions="${cssEscape(patientId)}"]`);
  if (!container) return;
  let data;
  try {
    const res = await fetch(`/api/versions?entity_type=patient&entity_id=${encodeURIComponent(patientId)}&pagesize=5`);
    if (!res.ok) {
      container.textContent = "版本记录载入失败";
      return false;
    }
    data = await res.json();
  } catch {
    container.textContent = "版本记录载入失败";
    return false;
  }
  const rows = data.list || [];
  container.innerHTML = rows.length ? rows.map(renderVersionRow).join("") : '<span class="empty">暂无本地版本记录</span>';
  return true;
}

function renderVersionRow(row) {
  return `
    <div class="version-row">
      <span>${escapeHtml(row.changed_at || "")}</span>
      <span>${escapeHtml(row.version_hash || "")}</span>
      <span>${escapeHtml(row.batch_id || "本地编辑")}</span>
    </div>
  `;
}

// 返回是否载入成功（false = 失败，供 renderWorkspaceAuditTab 取消已加载标记）。
async function loadPatientAudits(patientId, target) {
  const container = target.querySelector(`[data-patient-audits="${cssEscape(patientId)}"]`);
  if (!container) return;
  let data;
  try {
    const res = await fetch(`/api/patients/${encodeURIComponent(patientId)}/audit-logs?pagesize=8`);
    if (!res.ok) {
      container.textContent = "审计记录载入失败";
      return false;
    }
    data = await res.json();
  } catch {
    container.textContent = "审计记录载入失败";
    return false;
  }
  const rows = data.list || [];
  container.innerHTML = rows.length ? rows.map(renderPatientAuditRow).join("") : '<span class="empty">暂无本患者本地审计记录</span>';
  return true;
}

function renderPatientAuditRow(row) {
  return `
    <div class="patient-audit-row">
      <span>${escapeHtml(row.action || "")}</span>
      <span>${escapeHtml(row.entity_type || "")}</span>
      <span>${escapeHtml(row.operator || "")}</span>
      <span>${escapeHtml(row.created_at || "")}</span>
    </div>
  `;
}

// ---------- 记录级版本列表（病历/预约/回访编辑器共用）----------

function recordVersionList(label, entityType, entityId) {
  const loadingText = {
    medical_record: "病历版本记录载入中...",
    appointment: "预约版本记录载入中...",
    return_visit: "回访版本记录载入中...",
  }[entityType] || `${label}版本记录载入中...`;
  return `
    <div
      class="record-version-block version-list"
      data-record-versions="true"
      data-version-entity-type="${escapeAttr(entityType)}"
      data-version-entity-id="${escapeAttr(entityId || "")}"
    >${loadingText}</div>
  `;
}

async function loadEditableRecordVersions(target) {
  const containers = target.querySelectorAll("[data-record-versions]");
  await Promise.all(Array.from(containers).map(container => {
    return loadRecordVersions(
      container.dataset.versionEntityType,
      container.dataset.versionEntityId,
      container,
    );
  }));
}

async function loadRecordVersions(entityType, entityId, container) {
  if (!container || !entityType || !entityId) return;
  const versionUrls = {
    medical_record: `/api/versions?entity_type=medical_record&entity_id=${encodeURIComponent(entityId)}&pagesize=3`,
    appointment: `/api/versions?entity_type=appointment&entity_id=${encodeURIComponent(entityId)}&pagesize=3`,
    return_visit: `/api/versions?entity_type=return_visit&entity_id=${encodeURIComponent(entityId)}&pagesize=3`,
  };
  const url = versionUrls[entityType];
  if (!url) {
    container.textContent = "版本记录类型不支持";
    return;
  }
  let data;
  try {
    const res = await fetch(url);
    if (!res.ok) {
      container.textContent = "版本记录载入失败";
      return;
    }
    data = await res.json();
  } catch {
    // 网络异常：避免面板永卡"载入中"和 unhandled rejection
    container.textContent = "版本记录载入失败";
    return;
  }
  const rows = data.list || [];
  container.innerHTML = rows.length ? rows.map(renderVersionRow).join("") : '<span class="empty">暂无本地版本记录</span>';
}

// 就诊时间轴 tab：按天聚合病历/处置/收费/预约/回访（只读），对应官方"就诊信息"页。
async function renderWorkspaceVisitsTab(panel) {
  const identity = workspacePatientId;
  panel.textContent = "加载中...";
  let data;
  try {
    const res = await fetch(`/api/patients/${encodeURIComponent(identity)}/visits`);
    if (identity !== workspacePatientId) return;  // 竞态守卫
    if (!res.ok) {
      workspaceLoadedTabs.delete("visits");
      panel.textContent = "就诊记录载入失败";
      return;
    }
    data = await res.json();
    if (identity !== workspacePatientId) return;
  } catch {
    if (identity !== workspacePatientId) return;
    workspaceLoadedTabs.delete("visits");
    panel.textContent = "就诊记录载入失败";
    return;
  }
  const visits = data.visits || [];
  if (!visits.length) {
    panel.innerHTML = '<div class="workspace-placeholder">暂无就诊记录</div>';
    return;
  }
  panel.innerHTML = visits.map((v, i) => renderVisitDay(v, i === 0)).join("");
  if (typeof bindStaffInputs === "function") bindStaffInputs(panel);   // #420二批:预约历史编辑医生选人
}

// 单日就诊卡：默认最近一天展开，列出各类条目。
function renderVisitDay(visit, open) {
  const lines = [];
  (visit.medical_records || []).forEach(r =>
    lines.push(visitLine("病历", `${r.record_type || ""} ${r.doctor_name || ""}`.trim() || "病历记录")));
  (visit.treatments || []).forEach(t =>
    lines.push(visitLine("处置", `${t.item_name || ""}${t.quantity ? " ×" + t.quantity : ""}`.trim())));
  (visit.bills || []).forEach(b => {
    const parts = [b.bill_no || b.bill_number || ""].filter(Boolean);
    if (Object.prototype.hasOwnProperty.call(b, "total_fee")) {
      parts.push(`合计${formatMoney(b.total_fee)}`);
    }
    if (Object.prototype.hasOwnProperty.call(b, "paid_fee")) {
      parts.push(`已付${formatMoney(b.paid_fee)}`);
    }
    lines.push(visitLine("收费", parts.join(" ")));
  });
  // 预约并入就诊(用户校准:不单独开页)。每条带「改时间」内联编辑(只改时间,部分更新,不碰医生/项目/状态)。
  (visit.appointments || []).forEach(a => lines.push(visitApptLine(a)));
  (visit.return_visits || []).forEach(rv =>
    lines.push(visitLine("回访", rv.item_name || "回访")));
  return `
    <details class="visit-day"${open ? " open" : ""}>
      <summary>
        <strong>${escapeHtml(visit.date)}</strong>
        <span class="visit-day-count">${lines.length} 项</span>
      </summary>
      <div class="visit-day-body">${lines.join("")}</div>
    </details>
  `;
}

function visitLine(kind, text) {
  return `<div class="visit-line"><span class="visit-kind">${escapeHtml(kind)}</span><span class="visit-text">${escapeHtml(text)}</span></div>`;
}

// 就诊时间轴里的「预约」行：显示 时分+项目+医生+状态，「改时间」拉起完整预约弹窗(同预约管理双击)。
function visitApptLine(a) {
  const id = escapeAttr(a.appointment_id);
  const hm = String(a.start_time || "").slice(11, 16);
  const badge = (typeof appointmentStatusBadge === "function")
    ? appointmentStatusBadge(a.status) : escapeHtml(a.status || "");
  const desc = [a.item_name, a.doctor_name].map(s => String(s || "").trim()).filter(Boolean).join(" ");
  const timeText = hm ? `<b>${escapeHtml(hm)}</b>` : `<span class="visit-appt-notime">未排时间</span>`;
  return `
    <div class="visit-line visit-appt" data-appointment-id="${id}">
      <span class="visit-kind">预约</span>
      <span class="visit-text">
        ${timeText} ${escapeHtml(desc || "预约")} ${badge}
        <button type="button" class="plain-button visit-appt-edit-btn" onclick="openApptEditById('${id}')">改时间</button>
      </span>
    </div>`;
}

Object.assign(window, {
  requireWorkspaceDetail,
  refreshWorkspaceDetail,
  renderWorkspaceVisitsTab,
  renderVisitDay,
  visitLine,
  visitApptLine,
  renderWorkspaceMedicalTab,
  openNewMedicalRecord,
  cancelNewMedicalRecord,
  renderWorkspaceMedicalCardBody,
  editWorkspaceMedicalCard,
  cancelWorkspaceMedicalCard,
  refreshWorkspaceMedicalCard,
  renderWorkspaceTreatmentsTab,
  renderTreatmentVisit,
  renderUnlinkedTreatments,
  renderTreatmentItemsTable,
  createReturnVisit,
  renderWorkspaceAppointmentsTab,
  renderWorkspaceReturnVisitsTab,
  renderAppointment,
  renderReturnVisit, markReturnDone,
  saveAppointment,
  saveReturnVisit,
  renderWorkspaceNotesTab,
  saveWorkspaceNote,
  renderWorkspaceAuditTab,
  loadPatientVersions,
  loadPatientAudits,
  renderVersionRow,
  renderPatientAuditRow,
  recordVersionList,
  loadEditableRecordVersions,
  loadRecordVersions,
});
