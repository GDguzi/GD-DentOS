// 预约·天视图泳道排班网格：左小月历 + 主区「时间轴行 × 医生泳道列」。
// 预约块按 start/end 落格、状态配色；点块弹患者卡片；点空格新增预约预填医生+时段。
// 取数：/api/settings/appointment(营业时间/最小单位/医生列) + /api/staff-members?role=医生 + /api/appointments?date=

let _apptGridToken = 0;

// 本地日期 YYYY-MM-DD(不能用 toISOString,它转UTC会把东八区午夜算成前一天 → 差一天)
function localISO(d) {
  return `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, "0")}-${String(d.getDate()).padStart(2, "0")}`;
}

function apptBaseDate() {
  return (typeof workDate !== "undefined" && workDate && workDate.value)
    ? workDate.value : localDateValue();
}

// 状态(含SaaS数字码)→标签/配色，复用 today_work 的 formatAppointmentStatus
function apptStatusLabel(status) {
  return (typeof formatAppointmentStatus === "function") ? formatAppointmentStatus(status) : String(status || "");
}

function hm2min(hm) {
  const m = /^(\d{1,2}):(\d{2})/.exec(String(hm || ""));
  return m ? (parseInt(m[1], 10) * 60 + parseInt(m[2], 10)) : null;
}

// 预约时间字符串 "YYYY-MM-DD HH:MM[:SS]" → 当天分钟数
function apptStartMin(row) { return hm2min(String(row.start_time || "").slice(11, 16)); }
function apptEndMin(row, slotMin) {
  const e = hm2min(String(row.end_time || "").slice(11, 16));
  const s = apptStartMin(row);
  if (e != null && s != null && e > s) return e;
  return s != null ? s + slotMin : null;   // 无结束时间默认占一个最小单位
}

const APPT_STATUS_CODE = {"1": "appt-blk-arrived", "2": "appt-blk-done", "3": "appt-blk-cancelled"};
const APPT_STATUS_COLOR = [
  [["已取消", "取消"], "appt-blk-cancelled"],
  [["已到", "到诊", "到达", "分诊"], "appt-blk-arrived"],
  [["完成", "已离开"], "appt-blk-done"],
  [["待确定", "待确认"], "appt-blk-pending"],
];
function apptBlockClass(status) {
  const s = String(status || "").trim();
  if (APPT_STATUS_CODE[s]) return APPT_STATUS_CODE[s];          // SaaS 数字码
  for (const [kws, cls] of APPT_STATUS_COLOR) if (kws.some(k => s.includes(k))) return cls;
  return "appt-blk-booked";   // 默认/0 已预约蓝
}

// 医生配色：按医生名稳定映射到固定调色板,不同医生不同色(一眼区分)。未指定=灰。
const DOCTOR_PALETTE = ["#4e79a7", "#e15759", "#59a14f", "#f28e2b", "#76b7b2",
  "#b07aa1", "#d4a017", "#ff9da7", "#9c755f", "#499894", "#86bcb6", "#a0795f"];
function doctorColor(name) {
  const s = String(name || "").trim();
  if (!s) return "#9aa5b1";
  let h = 0;
  for (let i = 0; i < s.length; i++) h = (h * 31 + s.charCodeAt(i)) >>> 0;
  return DOCTOR_PALETTE[h % DOCTOR_PALETTE.length];
}

async function loadAppointmentDayGrid() {
  if (!modulePanel) return;
  const token = ++_apptGridToken;
  const base = apptBaseDate();
  modulePanel.innerHTML = `<div class="module-loading">排班载入中...</div>`;
  let settings, doctors, appts;
  try {
    // #122：天视图也要带状态过滤(状态 chip),否则点了 chip 高亮但网格仍显示全部状态
    const sQ = (typeof appointmentStatusFilter !== "undefined" && appointmentStatusFilter)
      ? `&status=${encodeURIComponent(appointmentStatusFilter)}` : "";
    const [rS, rD, rA] = await Promise.all([
      fetch("/api/settings/appointment"),
      fetch("/api/staff-members?role=医生"),
      fetch(`/api/appointments?date=${encodeURIComponent(base)}&pagesize=200${sQ}`),
    ]);
    if (token !== _apptGridToken) return;
    settings = rS.ok ? await rS.json() : {};
    doctors = rD.ok ? (await rD.json()).list || [] : [];
    appts = rA.ok ? (await rA.json()).list || [] : [];
    if (token !== _apptGridToken) return;
  } catch {
    if (token !== _apptGridToken) return;
    modulePanel.innerHTML = `<div class="module-loading">排班载入失败</div>`;
    return;
  }
  // 状态统计直接从当天预约列表算(与网格同源,杜绝 list/summary 不一致)
  const _byStatus = {};
  appts.forEach(a => { const k = String(a.status || ""); _byStatus[k] = (_byStatus[k] || 0) + 1; });
  const summary = {by_status: Object.entries(_byStatus).map(([key, count]) => ({key, count}))};
  modulePanel.innerHTML = `
    <div class="module-head"><strong>预约排班</strong><span>${escapeHtml(base)}</span></div>
    ${scheduleViewTabs()}
    ${appointmentDateNav(base)}
    ${scheduleStatusFilters(summary)}
    <div class="appt-day-layout">
      <aside class="appt-minical">${renderMiniCalendar(base)}</aside>
      <div class="appt-grid-wrap">${renderDayGrid(settings, doctors, appts)}</div>
    </div>
  `;
  if (typeof bindAppointmentInteractions === "function") bindAppointmentInteractions(modulePanel);
  bindDayGrid(modulePanel, appts);
}

function appointmentDateNav(base) {
  return `
    <div class="appt-date-nav">
      <button type="button" class="plain-button" data-appt-date-shift="-1">◀ 前一天</button>
      <strong>${escapeHtml(base)}</strong>
      <button type="button" class="plain-button" data-appt-date-shift="1">后一天 ▶</button>
      <button type="button" class="plain-button" data-appt-date-today="1">今天</button>
      <span class="appt-nav-spacer"></span>
      <button type="button" class="plain-button" data-appt-settings="1">⚙ 营业时间设置</button>
    </div>`;
}

// 同医生重叠预约 → 一左一右并排：给每条分配列号 col(0..n-1) 和该簇总列数 ncols。
// 标准区间分列：按 start 排序,放进第一个"末尾不晚于本条 start"的列,否则新列;与所有列都不重叠时结束一簇。
function layoutLaneColumns(items) {
  items.sort((a, b) => a.s - b.s || a.e - b.e);
  const result = {};
  let cluster = [], colEnds = [];
  const flush = () => {
    const ncols = colEnds.length;
    cluster.forEach(it => { result[it.id].ncols = ncols; });
    cluster = []; colEnds = [];
  };
  for (const it of items) {
    if (colEnds.length && it.s >= Math.max(...colEnds)) flush();   // 与本簇所有列都不重叠→新簇
    let col = colEnds.findIndex(end => end <= it.s);
    if (col === -1) { col = colEnds.length; colEnds.push(it.e); }
    else colEnds[col] = it.e;
    result[it.id] = {col, ncols: 1};
    cluster.push(it);
  }
  flush();
  return result;
}

function renderDayGrid(settings, doctors, appts) {
  const startMin = hm2min(settings.business_start) ?? 480;
  const endMin = hm2min(settings.business_end) ?? 1080;
  const slot = Number(settings.min_slot_minutes) || 30;
  const showDoctor = settings.show_doctor_column !== false;
  const nSlots = Math.max(1, Math.ceil((endMin - startMin) / slot));

  // 列 = 医生(+未指定)。无医生数据时退化为单列。
  let lanes = showDoctor ? doctors.map(d => d.name).filter(Boolean) : [];
  const apptDoctors = new Set(appts.map(a => (a.doctor_name || "").trim()).filter(Boolean));
  apptDoctors.forEach(n => { if (!lanes.includes(n)) lanes.push(n); });   // 历史数据里有但人员库没有的医生也建列
  const hasUnassigned = appts.some(a => !(a.doctor_name || "").trim());
  if (!lanes.length || hasUnassigned) lanes.push("未指定");
  const laneIndex = {};
  lanes.forEach((n, i) => { laneIndex[n] = i; });

  const SLOT_H = 22;   // 每个最小单位行高(px)
  const cols = `64px repeat(${lanes.length}, minmax(96px, 1fr))`;
  const rows = `28px repeat(${nSlots}, ${SLOT_H}px)`;

  // 表头：医生名
  let cells = `<div class="appt-grid-head appt-grid-corner" style="grid-row:1;grid-column:1">时间</div>`;
  lanes.forEach((name, i) => {
    cells += `<div class="appt-grid-head" style="grid-row:1;grid-column:${i + 2}"><span>${escapeHtml(name)}</span></div>`;
  });
  // 时间轴 + 空格(可点新增)
  for (let s = 0; s < nSlots; s++) {
    const min = startMin + s * slot;
    const hh = String(Math.floor(min / 60)).padStart(2, "0");
    const mm = String(min % 60).padStart(2, "0");
    const isHour = (min % 60) === 0;
    cells += `<div class="appt-grid-time${isHour ? " appt-grid-hour" : ""}" style="grid-row:${s + 2};grid-column:1">${isHour ? hh + ":00" : mm}</div>`;
    lanes.forEach((name, i) => {
      const startStr = `${hh}:${mm}`;
      cells += `<div class="appt-grid-cell" data-cell-doctor="${escapeAttr(name === "未指定" ? "" : name)}" data-cell-time="${escapeAttr(startStr)}" style="grid-row:${s + 2};grid-column:${i + 2}"></div>`;
    });
  }
  // 重叠并排：按医生分组,给重叠的预约分配左右子列
  const _laneItems = {};
  appts.forEach(a => {
    const s = apptStartMin(a);
    if (s == null) return;
    const ln = (a.doctor_name || "").trim() || "未指定";
    (_laneItems[ln] = _laneItems[ln] || []).push({id: a.appointment_id, s, e: apptEndMin(a, slot)});
  });
  const _ovl = {};
  Object.values(_laneItems).forEach(items => Object.assign(_ovl, layoutLaneColumns(items)));
  // 预约块：按 start/end 落 grid-row 跨格
  appts.forEach(a => {
    const sMin = apptStartMin(a);
    if (sMin == null) return;
    const eMin = apptEndMin(a, slot);
    const lane = (a.doctor_name || "").trim() || "未指定";
    const col = (laneIndex[lane] != null ? laneIndex[lane] : laneIndex["未指定"]);
    if (col == null) return;
    const lay = _ovl[a.appointment_id] || {col: 0, ncols: 1};
    const splitStyle = lay.ncols > 1 ? `justify-self:start;width:calc(100% / ${lay.ncols});transform:translateX(${lay.col * 100}%);` : "";
    let rowStart = Math.floor((sMin - startMin) / slot);
    let rowEnd = Math.ceil((eMin - startMin) / slot);
    rowStart = Math.max(0, Math.min(rowStart, nSlots - 1));
    rowEnd = Math.max(rowStart + 1, Math.min(rowEnd, nSlots));
    const startHM = String(a.start_time || "").slice(11, 16);
    const endHM = String(a.end_time || "").slice(11, 16);
    const timeText = (endHM && endHM !== startHM) ? `${startHM}–${endHM}` : startHM;   // 几点-几点
    const dColor = doctorColor(lane === "未指定" ? "" : lane);
    cells += `
      <button type="button" class="appt-block ${apptBlockClass(a.status)}"
        data-appt-block="${escapeAttr(a.appointment_id || "")}"
        data-appt-dur="${Math.max(slot, eMin - sMin)}"
        style="grid-row:${rowStart + 2}/${rowEnd + 2};grid-column:${col + 2};border-left:5px solid ${dColor};${splitStyle}">
        <span class="appt-block-name">${escapeHtml(a.display_name || "(无名)")}</span>
        <span class="appt-block-item">${escapeHtml(a.item_name || "")}</span>
        <span class="appt-block-time">${escapeHtml(timeText)}</span>
        <span class="appt-block-resize" data-appt-resize="${escapeAttr(a.appointment_id || "")}" title="拖拽下边缘改时长"></span>
      </button>`;
  });
  return `<div class="appt-grid" data-slot="${slot}" style="grid-template-columns:${cols};grid-template-rows:${rows}">${cells}</div>`;
}

// 左侧小月历：点某天 → 设 workDate → 重载
function renderMiniCalendar(base) {
  const d = new Date(base + "T00:00:00");
  const year = d.getFullYear(), month = d.getMonth();
  const first = new Date(year, month, 1);
  const startDow = (first.getDay() + 6) % 7;   // 周一为0
  const daysInMonth = new Date(year, month + 1, 0).getDate();
  const iso = x => localISO(x);
  let cells = ["一", "二", "三", "四", "五", "六", "日"].map(w => `<span class="mc-dow">${w}</span>`).join("");
  for (let i = 0; i < startDow; i++) cells += `<span class="mc-empty"></span>`;
  for (let day = 1; day <= daysInMonth; day++) {
    const ds = iso(new Date(year, month, day));
    const on = ds === base ? " mc-on" : "";
    cells += `<button type="button" class="mc-day${on}" data-mc-date="${escapeAttr(ds)}">${day}</button>`;
  }
  return `
    <div class="mc-head">
      <button type="button" class="plain-button" data-mc-month="-1">‹</button>
      <strong>${year}年${month + 1}月</strong>
      <button type="button" class="plain-button" data-mc-month="1">›</button>
    </div>
    <div class="mc-grid">${cells}</div>`;
}

function shiftWorkDate(days) {
  const d = new Date(apptBaseDate() + "T00:00:00");
  d.setDate(d.getDate() + days);
  if (typeof workDate !== "undefined" && workDate) workDate.value = localISO(d);
  loadAppointmentModule();
}
function setWorkDateTo(ds) {
  if (typeof workDate !== "undefined" && workDate) workDate.value = ds;
  loadAppointmentModule();
}

function bindDayGrid(container, appts) {
  if (!container) return;
  container.querySelectorAll("[data-appt-date-shift]").forEach(b =>
    b.addEventListener("click", () => shiftWorkDate(parseInt(b.dataset.apptDateShift, 10))));
  container.querySelectorAll("[data-appt-date-today]").forEach(b =>
    b.addEventListener("click", () => setWorkDateTo(localDateValue())));
  container.querySelectorAll("[data-appt-settings]").forEach(b =>
    b.addEventListener("click", openApptSettings));
  container.querySelectorAll("[data-mc-date]").forEach(b =>
    b.addEventListener("click", () => setWorkDateTo(b.dataset.mcDate)));
  container.querySelectorAll("[data-mc-month]").forEach(b =>
    b.addEventListener("click", () => {
      const d = new Date(apptBaseDate() + "T00:00:00");
      d.setMonth(d.getMonth() + parseInt(b.dataset.mcMonth, 10));
      setWorkDateTo(localISO(d));
    }));
  // 点空格新增预约(带入医生+时段)
  container.querySelectorAll("[data-cell-time]").forEach(cell =>
    cell.addEventListener("click", () => {
      if (typeof openNewAppointment !== "function") return;
      openNewAppointment({doctor: cell.dataset.cellDoctor || "", start: `${apptBaseDate()} ${cell.dataset.cellTime}`});
    }));
  // 预约块:指针拖拽改约(跟手实时移动+落点高亮,比原生 drag 顺滑);没拖动=点击弹卡片;
  // 双击=进编辑弹窗回填(#275)。
  const byId = {};
  (appts || []).forEach(a => { byId[a.appointment_id] = a; });
  container.querySelectorAll("[data-appt-block]").forEach(blk => {
    blk.addEventListener("pointerdown", e => startApptBlockDrag(e, blk, byId, appts));
    blk.addEventListener("dblclick", () => openApptEditById(blk.dataset.apptBlock));
  });
  // 拖块下边缘=拉伸改时长(stopPropagation 不触发上面的移动拖拽)
  container.querySelectorAll("[data-appt-resize]").forEach(h =>
    h.addEventListener("pointerdown", e => {
      e.stopPropagation();
      startApptBlockResize(e, h.closest("[data-appt-block]"), byId, appts);
    }));
}

// 拖块下边缘改时长：跟手实时拉高/缩短(吸附到最小单位)，松手按新时长存(保 start/医生不变)。
function startApptBlockResize(e, blk, byId, appts) {
  if (!blk || e.button !== 0) return;
  e.preventDefault();
  const id = blk.dataset.apptBlock;
  const a = byId && byId[id];
  if (!a) return;
  const grid = blk.closest(".appt-grid");
  const SLOT_H = 22;                                  // 与 renderDayGrid 行高一致
  const slot = parseInt(grid && grid.dataset.slot, 10) || 30;
  const gr = String(blk.style.gridRow || "").split("/").map(x => parseInt(x, 10));
  const rowStart = gr[0], origRowEnd = gr[1] || (rowStart + 1);
  const startY = e.clientY;
  blk.classList.add("appt-block-resizing");
  const onMove = ev => {
    const slots = Math.round((ev.clientY - startY) / SLOT_H);
    const newRowEnd = Math.max(rowStart + 1, origRowEnd + slots);   // 至少 1 格
    blk.style.gridRowEnd = String(newRowEnd);
  };
  const onUp = () => {
    document.removeEventListener("pointermove", onMove);
    document.removeEventListener("pointerup", onUp);
    blk.classList.remove("appt-block-resizing");
    const newRowEnd = parseInt(blk.style.gridRowEnd, 10) || origRowEnd;
    const newDur = (newRowEnd - rowStart) * slot;
    const origDur = (origRowEnd - rowStart) * slot;
    if (newDur === origDur) return;                   // 没变,不提交
    // 重叠检测：拉伸后与同医生其他预约时段重叠则先确认,不静默保存重叠
    const ns = apptStartMin(a), ne = ns + newDur;
    const doctor = (a.doctor_name || "").trim();
    const overlap = ns != null && (appts || []).some(o => o.appointment_id !== a.appointment_id
      && (o.doctor_name || "").trim() === doctor
      && (() => { const r = _apptMinRange(o); return r && ns < r[1] && r[0] < ne; })());
    if (overlap && !window.confirm("拉伸后与该医生其他预约时段重叠，仍要改时长？")) { loadAppointmentModule(); return; }
    resizeApptSave(a, newDur);
  };
  document.addEventListener("pointermove", onMove);
  document.addEventListener("pointerup", onUp);
}

async function resizeApptSave(a, newDurMin) {
  const m = String(a.start_time || "").match(/^(\d{4}-\d{2}-\d{2})[ T](\d{2}):(\d{2})/);
  if (!m) return;
  const date = m[1], h = +m[2], mi = +m[3];
  const endD = new Date(2000, 0, 1, h, mi + newDurMin);
  const pad = n => String(n).padStart(2, "0");
  const payload = {
    start_time: `${date} ${pad(h)}:${pad(mi)}`,
    end_time: `${date} ${pad(endD.getHours())}:${pad(endD.getMinutes())}`,
  };
  try {
    const res = await fetch(`/api/appointments/${encodeURIComponent(a.appointment_id)}`,
      {method: "PUT", headers: {"Content-Type": "application/json"}, body: JSON.stringify(payload)});
    if (!res.ok) {
      const d = await res.json().catch(() => ({}));
      alert("改时长失败：" + (d.detail || res.status));
    }
  } catch { alert("改时长失败（网络异常）"); }
  loadAppointmentModule();
}

// 取单条预约完整字段后进编辑弹窗(双击/卡片「编辑」入口共用)
async function openApptEditById(id) {
  if (!id || typeof openEditAppointment !== "function") return;
  closeApptCard();
  let appt = null;
  try {
    const r = await fetch(`/api/appointments/${encodeURIComponent(id)}`);
    if (r.ok) appt = (await r.json()).appointment;
  } catch { appt = null; }
  if (!appt) { alert("载入预约详情失败"); return; }
  openEditAppointment(appt);
}

// 同医生其他预约的分钟区间(用 start/end 字符串,无需 slot)
function _apptMinRange(a) {
  const s = hm2min(String(a.start_time || "").slice(11, 16));
  if (s == null) return null;
  let en = hm2min(String(a.end_time || "").slice(11, 16));
  if (en == null || en <= s) en = s + 30;
  return [s, en];
}

// 预约块与时间格在同一 CSS Grid 上叠放；仅允许穿透同网格预约块，避免穿过 sticky 表头等覆盖层。
function apptDropCellAtPoint(x, y, grid) {
  const stack = document.elementsFromPoint(x, y);
  const top = stack[0];
  if (!top || !top.closest) return null;
  const directCell = top.closest("[data-cell-time]");
  if (directCell) return !grid || top.closest(".appt-grid") === grid ? directCell : null;
  const coverBlock = top.closest("[data-appt-block]");
  if (!coverBlock || (grid && coverBlock.closest(".appt-grid") !== grid)) return null;
  for (const el of stack.slice(1)) {
    const cell = el && el.closest ? el.closest("[data-cell-time]") : null;
    if (cell && (!grid || el.closest(".appt-grid") === grid)) return cell;
  }
  return null;
}

// 指针拖拽:实时跟手移动预约块,松手按光标下的格改约;移动距离<阈值视为点击弹卡片
function startApptBlockDrag(e, blk, byId, appts) {
  if (e.button !== 0) return;
  const startX = e.clientX, startY = e.clientY;
  const id = blk.dataset.apptBlock, dur = parseInt(blk.dataset.apptDur, 10) || 30;
  const grid = blk.closest(".appt-grid");
  const origTransform = blk.style.transform;   // 并排块的 translateX,松手要还原而非清空(#361)
  let dragging = false, lastCell = null;
  const onMove = ev => {
    const dx = ev.clientX - startX, dy = ev.clientY - startY;
    if (!dragging && Math.hypot(dx, dy) < 5) return;   // 阈值内不算拖动
    if (!dragging) { dragging = true; blk.classList.add("appt-block-dragging"); blk.style.pointerEvents = "none"; closeApptCard(); }
    blk.style.transform = `translate(${dx}px, ${dy}px)`;   // 实时跟手
    const cell = apptDropCellAtPoint(ev.clientX, ev.clientY, grid);
    if (cell !== lastCell) {
      if (lastCell) lastCell.classList.remove("appt-cell-drophover");
      if (cell) cell.classList.add("appt-cell-drophover");
      lastCell = cell;
    }
  };
  const onUp = () => {
    document.removeEventListener("pointermove", onMove);
    document.removeEventListener("pointerup", onUp);
    blk.style.transform = origTransform; blk.style.pointerEvents = ""; blk.classList.remove("appt-block-dragging");   // 还原并排 translateX,不清空(#361)
    if (lastCell) lastCell.classList.remove("appt-cell-drophover");
    if (!dragging) { showApptCard(byId[id], blk); return; }   // 没拖动=点击
    if (!lastCell) return;                                     // 拖到网格外,丢弃
    const cellTime = lastCell.dataset.cellTime, doctor = lastCell.dataset.cellDoctor || "";
    // 落点冲突提醒(#270):目标时段与同医生其他预约重叠则先确认
    const ns = hm2min(cellTime); const ne = ns + dur;
    const overlap = (appts || []).some(a => a.appointment_id !== id
      && (a.doctor_name || "").trim() === doctor.trim()
      && (() => { const r = _apptMinRange(a); return r && ns < r[1] && r[0] < ne; })());
    if (overlap && !window.confirm("该时段已有同医生预约（时段重叠），仍要改到此时段？")) { loadAppointmentModule(); return; }
    rescheduleApptByDrag({id, dur}, cellTime, doctor);
  };
  document.addEventListener("pointermove", onMove);
  document.addEventListener("pointerup", onUp);
}

// 拖动改约:把预约改到 cellTime 起的时段(沿用原时长),拖到别的医生泳道则一并改医生
async function rescheduleApptByDrag(drag, cellTime, doctor) {
  const date = apptBaseDate();
  const [h, m] = String(cellTime || "").split(":").map(Number);
  if (isNaN(h) || isNaN(m)) return;
  const endD = new Date(2000, 0, 1, h, m + drag.dur);
  const pad = n => String(n).padStart(2, "0");
  const payload = {
    start_time: `${date} ${pad(h)}:${pad(m)}`,
    end_time: `${date} ${pad(endD.getHours())}:${pad(endD.getMinutes())}`,
  };
  if (doctor) payload.doctor_name = doctor;
  try {
    const res = await fetch(`/api/appointments/${encodeURIComponent(drag.id)}`,
      {method: "PUT", headers: {"Content-Type": "application/json"}, body: JSON.stringify(payload)});
    if (!res.ok) {
      const d = await res.json().catch(() => ({}));
      alert("改约失败：" + (d.detail || res.status));
    }
  } catch { alert("改约失败（网络异常）"); }
  loadAppointmentModule();
}

// 患者卡片浮层：患者/时段/状态 + 到店/完成/取消/编辑/打开患者
function showApptCard(appt, anchor) {
  closeApptCard();
  if (!appt) return;
  const card = document.createElement("div");
  card.className = "appt-card";
  card.id = "apptCard";
  const arrived = /已到|到诊|到达|分诊/.test(String(appt.status || ""));
  card.innerHTML = `
    <div class="appt-card-head">
      <button type="button" class="appt-card-name" data-card-open="1" title="打开患者">${escapeHtml(appt.display_name || "(无名)")}</button>
      <button type="button" class="plain-button" data-card-close="1">×</button>
    </div>
    <div class="appt-card-body">
      <div>时间：${escapeHtml(String(appt.start_time || "").slice(0, 16))}</div>
      <div>医生：${escapeHtml(appt.doctor_name || "未指定")}</div>
      <div>项目：${escapeHtml(appt.item_name || "")}</div>
      <div>状态：${escapeHtml(apptStatusLabel(appt.status))}</div>
    </div>
    <div class="appt-card-actions">
      ${!arrived ? `<button type="button" class="plain-button" data-card-arrive="1">到店</button>` : ""}
      <button type="button" class="plain-button" data-card-done="1">完成</button>
      <button type="button" class="plain-button" data-card-edit="1">编辑</button>
      <button type="button" class="plain-button" data-card-open="1">打开患者</button>
      <button type="button" class="plain-button steril-del" data-card-cancel="1">取消预约</button>
    </div>`;
  document.body.appendChild(card);
  const r = anchor.getBoundingClientRect();
  card.style.top = `${Math.min(window.innerHeight - 200, r.top + window.scrollY)}px`;
  card.style.left = `${Math.min(window.innerWidth - 240, r.right + 8)}px`;
  card.querySelector("[data-card-close]").addEventListener("click", closeApptCard);
  const arriveBtn = card.querySelector("[data-card-arrive]");
  if (arriveBtn) arriveBtn.addEventListener("click", () => setApptStatus(appt.appointment_id, "已到诊"));
  card.querySelector("[data-card-done]").addEventListener("click", () => setApptStatus(appt.appointment_id, "完成"));
  card.querySelectorAll("[data-card-open]").forEach(el => el.addEventListener("click", () => {
    closeApptCard();
    if (typeof openPatientWorkspace === "function") openPatientWorkspace(appt.patient_identity);
  }));
  card.querySelector("[data-card-edit]").addEventListener("click", () => openApptEditById(appt.appointment_id));
  card.querySelector("[data-card-cancel]").addEventListener("click", () => {
    closeApptCard();
    if (typeof cancelAppointment === "function") cancelAppointment(appt.appointment_id);
  });
}

function closeApptCard() {
  const c = document.getElementById("apptCard");
  if (c) c.remove();
}

async function setApptStatus(id, status) {
  try {
    const res = await fetch(`/api/appointments/${encodeURIComponent(id)}`, {
      method: "PUT", headers: {"Content-Type": "application/json"}, body: JSON.stringify({status})});
    if (!res.ok) { const m = await res.json().catch(() => ({})); window.alert("操作失败：" + (m.detail || res.status)); return; }
  } catch { window.alert("操作失败（网络异常）"); return; }
  closeApptCard();
  loadAppointmentModule();
}

// ---- 预约界面设置(营业时间/最小单位/医生列) ----
async function openApptSettings() {
  const m = document.getElementById("apptSettingsModal");
  if (!m) return;
  const st = document.getElementById("apptSettingsStatus");
  if (st) st.textContent = "";
  let s = {};
  try { s = await (await fetch("/api/settings/appointment")).json(); } catch { s = {}; }
  const set = (id, v) => { const el = document.getElementById(id); if (el) el.value = v; };
  try { const c = await (await fetch("/api/settings/clinic")).json(); set("setClinicName", (c && c.name) || ""); } catch { /* 留空用默认 */ }
  set("setBizStart", s.business_start || "08:00");
  set("setBizEnd", s.business_end || "18:00");
  set("setMinSlot", String(s.min_slot_minutes || 30));
  const show = document.getElementById("setShowDoctor");
  if (show) show.checked = s.show_doctor_column !== false;
  m.hidden = false;
}

function closeApptSettings() {
  const m = document.getElementById("apptSettingsModal");
  if (m) m.hidden = true;
}

async function saveApptSettings() {
  const st = document.getElementById("apptSettingsStatus");
  const val = id => (document.getElementById(id) || {}).value || "";
  const payload = {
    business_start: val("setBizStart"),
    business_end: val("setBizEnd"),
    min_slot_minutes: parseInt(val("setMinSlot"), 10) || 30,
    show_doctor_column: !!(document.getElementById("setShowDoctor") || {}).checked,
  };
  if (st) st.textContent = "保存中...";
  let res;
  try { res = await fetch("/api/settings/appointment", {method: "PUT", headers: {"Content-Type": "application/json"}, body: JSON.stringify(payload)}); }
  catch { if (st) st.textContent = "保存失败（网络异常）"; return; }
  if (!res.ok) { const m = await res.json().catch(() => ({})); if (st) st.textContent = "保存失败：" + (m.detail || res.status); return; }
  const clinicName = val("setClinicName").trim();
  if (clinicName) {
    try {
      const cr = await fetch("/api/settings/clinic", {method: "PUT", headers: {"Content-Type": "application/json"}, body: JSON.stringify({name: clinicName})});
      if (!cr.ok) { const m = await cr.json().catch(() => ({})); if (st) st.textContent = "诊所名保存失败：" + (m.detail || cr.status); return; }
    } catch { if (st) st.textContent = "诊所名保存失败（网络异常）"; return; }
  }
  closeApptSettings();
  loadAppointmentModule();   // 按新营业时间重排网格
}

Object.assign(window, {
  loadAppointmentDayGrid, renderDayGrid, renderMiniCalendar, bindDayGrid,
  shiftWorkDate, setWorkDateTo, showApptCard, closeApptCard, setApptStatus, openApptEditById,
  apptBlockClass, localISO, apptStatusLabel,
  openApptSettings, closeApptSettings, saveApptSettings,
});
