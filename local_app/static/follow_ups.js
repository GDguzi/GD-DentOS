// 今日待跟进(派生)：工作台内联看板。按患者价值(累计消费)分 高/中/普通，
// 来源=回访到期(未回访)+待缴费(有余额)。纯只读，数据来自 /api/today-follow-ups。

let _followUpView = "kanban";     // kanban | table
let _followUpAssignees = [];      // 计划跟进人下拉(首次全量加载收集)
const FU_TIERS = [["high", "高价值患者"], ["mid", "中等价值患者"], ["normal", "普通患者"]];

async function _fetchFollowUps(date, assignee, q) {
  const url = `/api/today-follow-ups?date=${encodeURIComponent(date)}`
    + `&assignee=${encodeURIComponent(assignee || "")}`
    + `&q=${encodeURIComponent(q || "")}`;
  return (await (await fetch(url)).json());
}

async function openTodayFollowUps(btn) {
  document.querySelectorAll(".today-saas-entry").forEach(e => e.classList.remove("active"));
  if (btn) btn.classList.add("active");
  const main = document.getElementById("todaySaasMain");
  if (!main) return;
  main.innerHTML = `<div class="module-loading">今日待跟进载入中...</div>`;
  let data;
  try { data = await _fetchFollowUps(workDate.value, "", ""); }
  catch { main.innerHTML = `<div class="module-loading">今日待跟进载入失败</div>`; return; }
  const set = new Set();
  FU_TIERS.forEach(([t]) => (data.groups[t] || []).forEach(p => { if (p.assignee) set.add(p.assignee); }));
  _followUpAssignees = [...set].sort();
  main.innerHTML = renderFollowUpShell(data);
  renderFollowUpBody(data);
  bindFollowUps();
}

function renderFollowUpShell(data) {
  return `
    <div class="fu-wrap">
      <div class="fu-bar">
        <span class="fu-bar-title">📍 今日待跟进</span>
        <span class="fu-bar-date">回访到期 ${escapeHtml(workDate.value)}</span>
        <label>跟进人
          <select id="fuAssignee" class="ord-input fu-select">
            <option value="">全部</option>
            ${_followUpAssignees.map(a => `<option value="${escapeAttr(a)}">${escapeHtml(a)}</option>`).join("")}
          </select>
        </label>
        <input id="fuSearch" class="ord-input fu-search" placeholder="姓名/手机号" autocomplete="off">
        <button type="button" class="tooth-confirm-btn" onclick="loadFollowUps()">查询</button>
        <span class="fu-bar-spacer"></span>
        <span class="fu-viewtoggle">
          <button type="button" class="fu-view-btn${_followUpView === "kanban" ? " active" : ""}" data-fu-view="kanban">看板</button>
          <button type="button" class="fu-view-btn${_followUpView === "table" ? " active" : ""}" data-fu-view="table">表格</button>
        </span>
        <button type="button" class="plain-button" onclick="loadTodayWork()">← 返回候诊</button>
      </div>
      <div id="fuBody" class="fu-body"></div>
    </div>`;
}

async function loadFollowUps() {
  const body = document.getElementById("fuBody");
  if (!body) return;
  const assignee = (document.getElementById("fuAssignee") || {}).value || "";
  const q = ((document.getElementById("fuSearch") || {}).value || "").trim();
  body.innerHTML = `<div class="module-loading">查询中...</div>`;
  let data;
  try { data = await _fetchFollowUps(workDate.value, assignee, q); }
  catch { body.innerHTML = `<div class="module-loading">查询失败</div>`; return; }
  renderFollowUpBody(data);
}

function renderFollowUpBody(data) {
  const body = document.getElementById("fuBody");
  if (!body) return;
  if (!data.total) { body.innerHTML = `<div class="fu-empty">今日无待跟进患者</div>`; return; }
  body.innerHTML = _followUpView === "table" ? followUpTable(data) : followUpKanban(data);
  bindPatientDetailRows(body);
}

function _fuReasonChips(reasons) {
  return (reasons || []).map(r =>
    `<span class="fu-chip fu-chip-${r.type === "待缴费" ? "owe" : "due"}">${escapeHtml(r.type)} ${escapeHtml(r.detail || "")}</span>`
  ).join("");
}

function followUpKanban(data) {
  return FU_TIERS.map(([tier, label]) => {
    const list = data.groups[tier] || [];
    return `
      <section class="fu-group">
        <div class="fu-group-head"><span class="fu-group-bar fu-${tier}"></span>${label} <strong>${list.length}</strong></div>
        <div class="fu-cards">
          ${list.length ? list.map(p => `
            <div class="fu-card" data-patient-id="${escapeAttr(p.patient_identity)}">
              <div class="fu-card-head"><strong>${escapeHtml(p.name || "(无名)")}</strong><span class="fu-value">¥${escapeHtml(String(p.value))}</span></div>
              <div class="fu-reasons">${_fuReasonChips(p.reasons)}</div>
              <div class="fu-card-foot">
                <span class="fu-assignee">跟进人 ${escapeHtml(p.assignee || "—")}</span>
                <span class="fu-phone">${escapeHtml(p.phone || "")}</span>
              </div>
            </div>`).join("") : `<div class="fu-empty-sm">无</div>`}
        </div>
      </section>`;
  }).join("");
}

function followUpTable(data) {
  const rows = [];
  FU_TIERS.forEach(([tier, label]) => (data.groups[tier] || []).forEach(p => rows.push([label, p])));
  return `
    <table class="data-table fu-table">
      <thead><tr><th>价值</th><th>姓名</th><th>手机号</th><th>累计消费</th><th>跟进原因</th><th>计划跟进人</th></tr></thead>
      <tbody>
        ${rows.map(([label, p]) => `
          <tr data-patient-id="${escapeAttr(p.patient_identity)}">
            <td>${escapeHtml(label)}</td>
            <td>${escapeHtml(p.name || "(无名)")}</td>
            <td>${escapeHtml(p.phone || "")}</td>
            <td>¥${escapeHtml(String(p.value))}</td>
            <td class="fu-reasons">${_fuReasonChips(p.reasons)}</td>
            <td>${escapeHtml(p.assignee || "—")}</td>
          </tr>`).join("")}
      </tbody>
    </table>`;
}

function bindFollowUps() {
  document.querySelectorAll("[data-fu-view]").forEach(b =>
    b.addEventListener("click", () => {
      _followUpView = b.dataset.fuView;
      document.querySelectorAll("[data-fu-view]").forEach(x => x.classList.toggle("active", x === b));
      // 用当前筛选重渲染主体
      loadFollowUps();
    }));
  const search = document.getElementById("fuSearch");
  if (search) search.addEventListener("keydown", e => { if (e.key === "Enter") loadFollowUps(); });
}
