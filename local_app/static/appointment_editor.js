// 新增预约弹窗 + 取消预约。配合 module_views.js 的预约模块（视图/状态过滤在那）。
// 防注入：患者数据用 data-* + 事件委托，不拼进内联 JS（同 #71 同意书选择器）。

let _apptPicked = null;       // 选中的患者 {patient_identity, display_name}
let _apptSearchSeq = 0;       // 患者搜索请求序号，防乱序覆盖
let _apptEditId = null;       // 非空=编辑既有预约(PUT)，空=新增(POST)(#275 双击改约)

// 预约项目常用清单；勾选项 + 自填"其他项目"合并为 item_name
const APPT_ITEMS = [
  "补交欠款", "拍片", "牙疼", "检查", "洁牙", "复查", "咨询正畸", "正畸复诊",
  "调节义齿", "拆托槽", "粘托槽", "戴保持器", "间隙保持器", "矫治器",
  "种植", "种植二期", "种牙复诊", "打桩备牙", "上牙套", "加牙",
  "清理蛀牙", "调磨抛光", "拔乳牙", "上药", "窝沟封闭", "沟通方案",
];

function renderApptItemGrid() {
  const grid = document.getElementById("apptItemGrid");
  if (!grid) return;
  grid.innerHTML = APPT_ITEMS.map(it =>
    `<label class="appt-item-opt"><input type="checkbox" value="${escapeAttr(it)}">${escapeHtml(it)}</label>`
  ).join("");
}

// 预约项目搜索:按关键字过滤勾选网格
function filterApptItemGrid(q) {
  q = String(q || "").trim();
  document.querySelectorAll("#apptItemGrid .appt-item-opt").forEach(l => {
    l.style.display = (!q || l.textContent.includes(q)) ? "" : "none";
  });
}
window.filterApptItemGrid = filterApptItemGrid;

// ---------- 日历 + 快捷日期 + 时段表 ----------
let _apptDate = "";        // 选中日期 YYYY-MM-DD
let _apptTime = "";        // 选中时刻 HH:MM
let _apptCalMonth = null;  // 日历当前显示月(Date,该月1号)

function _apptYmd(d) {
  return `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, "0")}-${String(d.getDate()).padStart(2, "0")}`;
}
function _apptDateFromYmd(s) {
  const [y, m, d] = String(s || "").split("-").map(Number);
  return (y && m && d) ? new Date(y, m - 1, d) : bjToday();
}

function renderApptCalendar() {
  const el = document.getElementById("apptCalendar");
  if (!el) return;
  const cur = _apptCalMonth || _apptDateFromYmd(_apptDate);
  const year = cur.getFullYear(), month = cur.getMonth();
  const startDow = (new Date(year, month, 1).getDay() + 6) % 7;   // 周一=0
  const days = new Date(year, month + 1, 0).getDate();
  const today = _apptYmd(bjToday());
  let cells = "";
  for (let i = 0; i < startDow; i++) cells += `<span class="appt-cal-cell empty"></span>`;
  for (let d = 1; d <= days; d++) {
    const ymd = `${year}-${String(month + 1).padStart(2, "0")}-${String(d).padStart(2, "0")}`;
    const cls = [ymd === _apptDate ? "sel" : "", ymd === today ? "today" : ""].filter(Boolean).join(" ");
    cells += `<button type="button" class="appt-cal-cell ${cls}" data-ymd="${ymd}">${d}</button>`;
  }
  el.innerHTML = `
    <div class="appt-cal-head">
      <button type="button" class="appt-cal-nav" data-cal-nav="-1" title="上月">‹</button>
      <span>${year}年${month + 1}月</span>
      <button type="button" class="appt-cal-nav" data-cal-nav="1" title="下月">›</button>
    </div>
    <div class="appt-cal-dow"><span>一</span><span>二</span><span>三</span><span>四</span><span>五</span><span>六</span><span>日</span></div>
    <div class="appt-cal-grid">${cells}</div>`;
  el.querySelectorAll("[data-ymd]").forEach(b => b.addEventListener("click", () => setApptDate(b.dataset.ymd)));
  el.querySelectorAll("[data-cal-nav]").forEach(b => b.addEventListener("click", () => {
    _apptCalMonth = new Date(year, month + Number(b.dataset.calNav), 1);
    renderApptCalendar();
  }));
}

const _APPT_QUICK = [["7天后", 7, "d"], ["1个月后", 1, "M"], ["3个月后", 3, "M"], ["半年后", 6, "M"], ["1年后", 12, "M"]];
function renderApptQuickDates() {
  const el = document.getElementById("apptQuickDates");
  if (!el) return;
  el.innerHTML = _APPT_QUICK.map((o, i) => `<button type="button" class="appt-quick" data-qi="${i}">${o[0]}</button>`).join("");
  el.querySelectorAll("[data-qi]").forEach(b => b.addEventListener("click", () => {
    const [, n, unit] = _APPT_QUICK[Number(b.dataset.qi)];
    const d = bjToday();
    if (unit === "d") d.setDate(d.getDate() + n); else d.setMonth(d.getMonth() + n);
    setApptDate(_apptYmd(d));
  }));
}

function setApptDate(ymd) {
  _apptDate = ymd;
  _apptCalMonth = _apptDateFromYmd(ymd);
  renderApptCalendar();
  renderApptTimeline();
  updateApptStart();
}
function setApptTime(hhmm) { _apptTime = hhmm; renderApptTimeline(); updateApptStart(); }

function updateApptStart() {
  const start = document.getElementById("apptStart");
  const end = document.getElementById("apptEnd");
  const slot = document.getElementById("apptSelectedSlot");
  if (_apptDate && _apptTime) {
    const [h, m] = _apptTime.split(":").map(Number);
    const e = new Date(2000, 0, 1, h, m + 60);   // 默认时长 60 分
    const ehhmm = `${String(e.getHours()).padStart(2, "0")}:${String(e.getMinutes()).padStart(2, "0")}`;
    if (start) start.value = `${_apptDate} ${_apptTime}`;
    if (end) end.value = `${_apptDate} ${ehhmm}`;
    if (slot) slot.textContent = `已选 ${_apptDate} ${_apptTime}-${ehhmm}`;
  } else {
    if (start) start.value = "";
    if (end) end.value = "";
    if (slot) slot.textContent = _apptDate ? `${_apptDate}（请在时段表点选时间）` : "";
  }
}

let _apptTimelineSeq = 0;
async function renderApptTimeline() {
  const el = document.getElementById("apptTimeline");
  const sub = document.getElementById("apptTimelineDate");
  if (!el || !_apptDate) return;
  const dd = _apptDateFromYmd(_apptDate);
  if (sub) sub.textContent = `${_apptDate}（${["周日", "周一", "周二", "周三", "周四", "周五", "周六"][dd.getDay()]}）`;
  const doctor = ((document.getElementById("apptDoctor") || {}).value || "").trim();
  const seq = ++_apptTimelineSeq;
  let appts = [];
  try {
    appts = (await (await fetch(`/api/appointments?date=${encodeURIComponent(_apptDate)}` +
      (doctor ? `&doctor_name=${encodeURIComponent(doctor)}` : "") + `&pagesize=200`)).json()).list || [];
  } catch { appts = []; }
  if (seq !== _apptTimelineSeq) return;   // 旧请求/已换日期,丢弃
  // 占用判断:排除已取消/爽约,且排除正在编辑的本预约(#408:否则编辑时把自己的时段判为占用)
  const _occupies = (a, hhmm) => String(a.start_time || "").slice(11, 16) === hhmm
    && String(a.status || "") !== "已取消" && String(a.status || "") !== "3"
    && !(_apptEditId && a.appointment_id === _apptEditId);
  // 扫荡#396:若当前预选时刻已被占用(且不是本预约自己),取消预选(不诱导重叠),让用户改选空档
  if (_apptTime && appts.some(a => _occupies(a, _apptTime))) {
    _apptTime = "";
    if (typeof updateApptStart === "function") updateApptStart();   // 同步清 start/end + 已选标签
  }
  const slots = [];
  for (let h = 8; h < 21; h++) { slots.push([h, 0]); slots.push([h, 30]); }
  el.innerHTML = slots.map(([h, m]) => {
    const hhmm = `${String(h).padStart(2, "0")}:${String(m).padStart(2, "0")}`;
    const occ = appts.find(a => _occupies(a, hhmm));
    if (occ) {
      return `<div class="appt-slot occ"><span class="appt-slot-t">${hhmm}</span>` +
        `<span class="appt-slot-info">${escapeHtml(occ.display_name || "")} ${escapeHtml(occ.item_name || "")}</span></div>`;
    }
    return `<button type="button" class="appt-slot${_apptTime === hhmm ? " sel" : ""}" data-slot="${hhmm}">` +
      `<span class="appt-slot-t">${hhmm}</span></button>`;
  }).join("");
  el.querySelectorAll("[data-slot]").forEach(b => b.addEventListener("click", () => setApptTime(b.dataset.slot)));
}

function openNewAppointment(prefill) {
  prefill = prefill || {};
  _apptPicked = null;
  _apptEditId = null;
  const m = document.getElementById("appointmentModal");
  if (!m) return;
  // 复位为「新增」模式 UI(编辑模式会在 openEditAppointment 里覆盖)
  const isWalkIn = prefill.registerType === "到店";
  const title = document.getElementById("apptModalTitle"); if (title) title.textContent = isWalkIn ? "新增挂号" : "新增预约";
  const sbtn = document.getElementById("apptSubmitBtn"); if (sbtn) sbtn.textContent = isWalkIn ? "确认挂号" : "保存预约";
  const pfield = document.getElementById("apptPatientField"); if (pfield) { pfield.hidden = false; pfield.style.display = ""; }
  const search = document.getElementById("apptPatientSearch");
  if (search) search.disabled = false;   // #329:新增模式恢复可搜可编辑
  const results = document.getElementById("apptPatientResults");
  const picked = document.getElementById("apptPatientPicked");
  const status = document.getElementById("apptStatus");
  if (search) search.value = "";
  if (results) { results.hidden = true; results.innerHTML = ""; }
  if (picked) { picked.hidden = true; picked.innerHTML = ""; }
  if (status) status.textContent = "";
  // #332:支持预选患者(候诊队列/患者详情点「预约」带入当前患者);#343:带入后锁住搜索框,不可改选别人
  if (prefill.patient && prefill.patient.patient_identity) {
    _apptPicked = {patient_identity: prefill.patient.patient_identity, display_name: prefill.patient.display_name || ""};
    if (picked) { picked.hidden = false; picked.textContent = `已选患者：${_apptPicked.display_name || _apptPicked.patient_identity}`; }
    if (pfield) { pfield.hidden = true; pfield.style.display = "none"; }
    if (search) { search.disabled = true; search.value = ""; }
  }
  // 日期/时刻:有 prefill.start("YYYY-MM-DD HH:MM")用它,否则日期=当前工作日、时刻待点选
  const base = (typeof workDate !== "undefined" && workDate && workDate.value) ? workDate.value : _apptYmd(bjToday());
  const ps = String(prefill.start || "").trim();
  _apptDate = ps.slice(0, 10) || base;
  _apptTime = ps.length >= 16 ? ps.slice(11, 16) : "";
  _apptCalMonth = _apptDateFromYmd(_apptDate);
  const doctor = document.getElementById("apptDoctor");
  const item = document.getElementById("apptItem");
  if (doctor) doctor.value = prefill.doctor || "";
  if (item) item.value = "";
  const itemSearch = document.getElementById("apptItemSearch"); if (itemSearch) itemSearch.value = "";
  const bookedBy = document.getElementById("apptBookedBy"); if (bookedBy) bookedBy.value = "";
  const remarkEl = document.getElementById("apptRemark"); if (remarkEl) remarkEl.value = "";
  // 重置新增字段：项目网格 + 就诊类型/预约类型/来源/咨询师/病历号
  renderApptItemGrid();
  filterApptItemGrid("");
  const firstVt = document.querySelector('input[name="apptVisitType"][value="初诊"]');
  if (firstVt) firstVt.checked = true;
  const normalType = document.querySelector('input[name="apptType"][value="普通预约"]');
  if (normalType) normalType.checked = true;
  const normalReg = document.querySelector('input[name="apptRegisterType"][value="预约"]');
  if (normalReg) normalReg.checked = true;
  // #339:＋挂号 走「到店登记」而非默认「预约」,名实相符
  if (prefill.registerType) {
    const reg = document.querySelector(`input[name="apptRegisterType"][value="${prefill.registerType}"]`);
    if (reg) reg.checked = true;
  }
  const srcEl = document.getElementById("apptSource"); if (srcEl) srcEl.value = "";
  const consEl = document.getElementById("apptConsultant"); if (consEl) consEl.value = "";
  const chartEl = document.getElementById("apptChartNo"); if (chartEl) { chartEl.hidden = true; chartEl.textContent = ""; }
  // 日历/快捷日期/时段表渲染 + 选中态回填
  renderApptCalendar();
  renderApptQuickDates();
  renderApptTimeline();
  updateApptStart();
  // 医生改了 → 时段表按该医生重拉(看他当天排班)
  if (doctor) doctor.oninput = () => renderApptTimeline();
  if (typeof bindStaffInputs === "function") bindStaffInputs(m);   // 预约医生/咨询师选人 datalist
  // GD-01:打开/重开滚动位置回顶(手机单滚动容器=appt-dialog;桌面各栏独立滚)
  m.querySelectorAll(".appt-dialog, .appt-col").forEach(el => { el.scrollTop = 0; });
  m.hidden = false;
  if (search) {
    search.oninput = () => searchApptPatients(search.value);
    search.focus();
  }
}

function closeNewAppointment() {
  const m = document.getElementById("appointmentModal");
  if (m) m.hidden = true;
}

function _apptSetRadio(name, val) {
  val = String(val || "").trim();
  const group = document.querySelectorAll(`input[name="${name}"]`);
  // #297：原值为空→全不选(提交回空,保留原空值),不继承新增模式的默认值(初诊/普通预约)
  if (!val) { group.forEach(el => { el.checked = false; }); return; }
  const el = document.querySelector(`input[name="${name}"][value="${CSS.escape(val)}"]`);
  if (el) el.checked = true;
}

// #275 双击预约块 → 以「编辑既有预约」模式打开弹窗并回填(患者锁定,保存走 PUT)。
// appt 需含完整字段(由 /api/appointments/{id} 取),见 appointment_grid.js 的双击钩子。
function openEditAppointment(appt) {
  if (!appt) return;
  if (typeof closeApptCard === "function") closeApptCard();
  // 先按新增流程渲染/重置(带入日期时刻+医生)，再切到编辑模式覆盖
  openNewAppointment({start: String(appt.start_time || ""), doctor: appt.doctor_name || ""});
  _apptEditId = appt.appointment_id || "";
  const title = document.getElementById("apptModalTitle"); if (title) title.textContent = "编辑预约";
  const sbtn = document.getElementById("apptSubmitBtn"); if (sbtn) sbtn.textContent = "保存修改";
  // 患者锁定:隐藏+禁用搜索框,显示当前患者(PUT 不改患者)。#329:[hidden] 会被 .appt-field display 覆盖,
  // 故同时 style 强隐 + disable 输入框,确保改约时患者字段不可编辑。
  _apptPicked = {patient_identity: appt.patient_identity, display_name: appt.display_name || ""};
  const pfield = document.getElementById("apptPatientField"); if (pfield) { pfield.hidden = true; pfield.style.display = "none"; }
  const psearchLock = document.getElementById("apptPatientSearch"); if (psearchLock) psearchLock.disabled = true;
  const picked = document.getElementById("apptPatientPicked");
  if (picked) { picked.hidden = false; picked.textContent = `患者：${_apptPicked.display_name || _apptPicked.patient_identity}`; }
  const chartEl = document.getElementById("apptChartNo");
  if (chartEl) {
    if (appt.chart_no) { chartEl.hidden = false; chartEl.textContent = `病历号：${appt.chart_no}`; }
    else { chartEl.hidden = true; chartEl.textContent = ""; }
  }
  // 回填预约项目:item_name 按「、」拆,命中勾选网格则勾,余下进自填
  const want = new Set(String(appt.item_name || "").split("、").map(s => s.trim()).filter(Boolean));
  const grid = document.getElementById("apptItemGrid");
  if (grid) grid.querySelectorAll('input[type="checkbox"]').forEach(c => {
    if (want.has(c.value)) { c.checked = true; want.delete(c.value); }
  });
  const customEl = document.getElementById("apptItem"); if (customEl) customEl.value = [...want].join("、");
  // 回填单选/文本字段
  _apptSetRadio("apptVisitType", appt.visit_type);
  _apptSetRadio("apptType", appt.appointment_type);
  _apptSetRadio("apptRegisterType", appt.register_type);
  const set = (id, v) => { const el = document.getElementById(id); if (el) el.value = v || ""; };
  set("apptConsultant", appt.consultant);
  set("apptSource", appt.patient_source);
  set("apptBookedBy", appt.booked_by);
  set("apptRemark", appt.remark);
  // #297：显式回填原始 start/end —— openNewAppointment 经 updateApptStart 会把 apptEnd 默认成"开始+60分",
  // 若不覆盖,双击后即使不改时间直接保存,也会把原时长(如 09:00-09:30)静默改成 60 分。覆盖回真实值。
  const startEl = document.getElementById("apptStart"), endEl = document.getElementById("apptEnd");
  if (startEl && appt.start_time) startEl.value = String(appt.start_time).slice(0, 16);
  if (endEl && appt.end_time) endEl.value = String(appt.end_time).slice(0, 16);
}

async function searchApptPatients(keyword) {
  const results = document.getElementById("apptPatientResults");
  if (!results) return;
  keyword = String(keyword || "").trim();
  if (!keyword) { results.hidden = true; results.innerHTML = ""; return; }
  const seq = ++_apptSearchSeq;
  let data;
  try { data = await (await fetch(`/api/patients?q=${encodeURIComponent(keyword)}&pagesize=8`)).json(); }
  catch {
    // 网络失败不再静默——否则看起来像"搜不到"(接口本身支持拼音/首字母/手机号)
    if (seq !== _apptSearchSeq) return;
    results.hidden = false;
    results.innerHTML = `<div class="appt-patient-empty">搜索失败（网络异常），请重试</div>`;
    return;
  }
  if (seq !== _apptSearchSeq) return;   // 旧搜索响应，丢弃
  const list = data.list || [];
  if (!list.length) { results.hidden = false; results.innerHTML = `<div class="appt-patient-empty">无匹配患者</div>`; return; }
  results.hidden = false;
  results.innerHTML = list.map(p => `
    <button type="button" class="appt-patient-opt" data-pid="${escapeAttr(p.patient_identity)}" data-pname="${escapeAttr(p.display_name || "")}" data-chart="${escapeAttr(p.chart_no || "")}">
      ${escapeHtml(p.display_name || "(无名)")} <small>${escapeHtml(p.phone || "")}</small>
    </button>`).join("");
  results.querySelectorAll("[data-pid]").forEach(btn => {
    btn.addEventListener("click", () => pickApptPatient(btn));
  });
}

function pickApptPatient(el) {
  if (!el) return;
  _apptPicked = {patient_identity: el.dataset.pid, display_name: el.dataset.pname || ""};
  const results = document.getElementById("apptPatientResults");
  const picked = document.getElementById("apptPatientPicked");
  const search = document.getElementById("apptPatientSearch");
  if (results) { results.hidden = true; results.innerHTML = ""; }
  if (search) search.value = "";
  if (picked) { picked.hidden = false; picked.textContent = `已选患者：${_apptPicked.display_name || _apptPicked.patient_identity}`; }
  const chartEl = document.getElementById("apptChartNo");   // 显示病历号
  if (chartEl) {
    const chart = el.dataset.chart || "";
    if (chart) { chartEl.hidden = false; chartEl.textContent = `病历号：${chart}`; }
    else { chartEl.hidden = true; chartEl.textContent = ""; }
  }
}

async function submitNewAppointment() {
  const status = document.getElementById("apptStatus");
  if (!_apptPicked || !_apptPicked.patient_identity) { if (status) status.textContent = "请先选患者"; return; }
  const start = (document.getElementById("apptStart") || {}).value || "";
  if (!start.trim()) { if (status) status.textContent = "请填预约时间"; return; }
  const doctorName = ((document.getElementById("apptDoctor") || {}).value || "").trim();
  if (!doctorName) { if (status) status.textContent = "请选主治医生"; return; }
  // 预约项目 = 勾选网格 + 自填"其他项目"，合并去重
  const checked = Array.from(document.querySelectorAll('#apptItemGrid input[type="checkbox"]:checked')).map(c => c.value);
  const custom = ((document.getElementById("apptItem") || {}).value || "").trim();
  const itemName = [...new Set([...checked, ...(custom ? [custom] : [])])].join("、");   // #115 去重(自填与勾选相同不重复)
  if (!itemName) { if (status) status.textContent = "请选/填预约项目"; return; }
  const picked = name => (document.querySelector(`input[name="${name}"]:checked`) || {}).value || "";
  const editing = !!_apptEditId;
  const payload = {
    start_time: start.trim(),
    end_time: ((document.getElementById("apptEnd") || {}).value || "").trim(),
    doctor_name: doctorName,
    item_name: itemName,
    visit_type: picked("apptVisitType"),
    appointment_type: picked("apptType"),
    register_type: picked("apptRegisterType") || "预约",
    patient_source: ((document.getElementById("apptSource") || {}).value || "").trim(),
    consultant: ((document.getElementById("apptConsultant") || {}).value || "").trim(),
    booked_by: ((document.getElementById("apptBookedBy") || {}).value || "").trim(),
    remark: ((document.getElementById("apptRemark") || {}).value || "").trim(),
  };
  if (!editing) payload.patient_identity = _apptPicked.patient_identity;   // PUT 不改患者
  const url = editing ? `/api/appointments/${encodeURIComponent(_apptEditId)}` : "/api/appointments";
  const method = editing ? "PUT" : "POST";
  if (status) status.textContent = "保存中...";
  let res, data;
  try { res = await fetch(url, {method, headers: {"Content-Type": "application/json"}, body: JSON.stringify(payload)}); }
  catch { if (status) status.textContent = "保存失败（网络异常）"; return; }
  if (!res.ok) { const m = await res.json().catch(() => ({})); if (status) status.textContent = "保存失败：" + (m.detail || res.status); return; }
  data = await res.json().catch(() => ({}));
  // P1-3 时段冲突：仅新增(POST)会返回 conflict，前端二次确认后带 force 重提；编辑(PUT)无此流程
  if (!editing && data && data.conflict) {
    if (!window.confirm((data.warning || "该时段可能有冲突。") + "\n仍要新增此预约？")) {
      if (status) status.textContent = "已取消（时段冲突）"; return;
    }
    try { res = await fetch("/api/appointments", {method: "POST", headers: {"Content-Type": "application/json"}, body: JSON.stringify({...payload, force: true})}); }
    catch { if (status) status.textContent = "保存失败（网络异常）"; return; }
    if (!res.ok) { const m = await res.json().catch(() => ({})); if (status) status.textContent = "保存失败：" + (m.detail || res.status); return; }
  }
  closeNewAppointment();
  if (typeof reloadModuleFromFirstPage === "function") reloadModuleFromFirstPage("loadAppointmentModule");
  _refreshVisitsAfterApptChange();   // 预约并入就诊:若正看患者就诊页,改完即时刷新
  if (typeof loadTodayWork === "function") loadTodayWork();   // #544:保存后即时刷新今日工作台,不必手点"刷新今日"
}

// 预约改/取消后:失效就诊缓存;若就诊面板正显示则即时重渲(预约已并入就诊时间轴)
function _refreshVisitsAfterApptChange() {
  if (typeof evictVisitsCache === "function") evictVisitsCache();
  const vp = document.querySelector('[data-workspace-panel="visits"]');
  if (vp && !vp.hidden && typeof renderWorkspaceVisitsTab === "function") {
    if (typeof workspaceLoadedTabs !== "undefined") workspaceLoadedTabs.delete("visits");
    renderWorkspaceVisitsTab(vp);
  }
}

// 取消预约：填原因 → PUT status=已取消 + cancel_reason
async function cancelAppointment(appointmentId) {
  if (!appointmentId) return;
  const reason = await appPrompt("取消原因（必填）：", "");
  if (reason === null) return;            // 用户取消操作
  if (!reason.trim()) { window.alert("取消原因不能为空"); return; }
  let res;
  try {
    res = await fetch(`/api/appointments/${encodeURIComponent(appointmentId)}`, {
      method: "PUT", headers: {"Content-Type": "application/json"},
      body: JSON.stringify({status: "已取消", cancel_reason: reason.trim()})});
  } catch { window.alert("取消失败（网络异常）"); return; }
  if (!res.ok) { const m = await res.json().catch(() => ({})); window.alert("取消失败：" + (m.detail || res.status)); return; }
  if (typeof reloadModuleFromFirstPage === "function") reloadModuleFromFirstPage("loadAppointmentModule");
  _refreshVisitsAfterApptChange();
  if (typeof loadTodayWork === "function") loadTodayWork();   // #544:取消后即时刷新今日队列,不留旧状态(如仍显示已到达)
}

Object.assign(window, {
  openNewAppointment, openEditAppointment, closeNewAppointment, searchApptPatients,
  pickApptPatient, submitNewAppointment, cancelAppointment,
});
