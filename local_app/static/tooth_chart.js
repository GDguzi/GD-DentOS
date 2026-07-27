// 病历页常驻全口牙位图：牙位结构化条目（检查/诊断/治疗/计划）的总览+录入入口。
// 数据源：GET /api/patients/{id}/teeth/summary（着色）、
//        GET /api/patients/{id}/teeth/{tooth}/history（点牙历史）、
//        GET/PUT /api/medical-records/{id}/items（条目读写，整组替换）。
// 由 workspace_tabs.js 的 renderWorkspaceMedicalTab 挂载。

const ITEM_TYPE_LABELS = {exam: "检查", diagnosis: "诊断", treatment: "治疗", plan: "计划"};

let toothChartSummary = {};
let toothChartSelectedTooth = "";

// FDI 编码全口布局：上排 18→11 | 21→28，下排 48→41 | 31→38。
function fdiToothRows() {
  const upper = [];
  for (let i = 8; i >= 1; i--) upper.push(`1${i}`);
  for (let i = 1; i <= 8; i++) upper.push(`2${i}`);
  const lower = [];
  for (let i = 8; i >= 1; i--) lower.push(`4${i}`);
  for (let i = 1; i <= 8; i++) lower.push(`3${i}`);
  return [upper, lower];
}

function renderToothChartSkeleton() {
  return `
    <div class="tooth-chart-head">
      <strong>全口牙位图</strong>
      <span class="tooth-chart-hint">点选一颗牙查看/录入该牙的检查、诊断、治疗、计划</span>
    </div>
    <div class="tooth-chart-layout">
      <div class="tooth-chart-grid" data-tooth-chart-grid>载入中...</div>
      <div class="tooth-chart-ops">
        ${Object.entries(ITEM_TYPE_LABELS).map(([type, label]) => `
          <button type="button" class="tooth-chart-op" onclick="startToothChartEntry('${type}')">+ ${label}</button>
        `).join("")}
      </div>
    </div>
    <div class="tooth-chart-detail" data-tooth-chart-detail></div>
  `;
}

// P1-3 收口：把已建好的全口牙位图面板挂成独立工作区 tab（隔离，不动病历页布局）。
function renderWorkspaceToothChartTab(panel) {
  if (!panel) return;
  panel.innerHTML = renderToothChartSkeleton();
  loadToothChartPanel();
}

async function loadToothChartPanel() {
  toothChartSelectedTooth = "";
  const grid = document.querySelector("[data-tooth-chart-grid]");
  if (!grid || !workspacePatientId) return;
  try {
    const res = await fetch(`/api/patients/${encodeURIComponent(workspacePatientId)}/teeth/summary`);
    if (!res.ok) throw new Error(String(res.status));
    const data = await res.json();
    toothChartSummary = {};
    (data.teeth || []).forEach(t => { toothChartSummary[t.tooth] = t; });
  } catch {
    grid.textContent = "牙位图载入失败";
    return;
  }
  renderToothChartGrid();
  const detail = document.querySelector("[data-tooth-chart-detail]");
  if (detail) detail.innerHTML = "";
}

function renderToothChartGrid() {
  const grid = document.querySelector("[data-tooth-chart-grid]");
  if (!grid) return;
  grid.innerHTML = fdiToothRows().map(row => `
    <div class="tooth-chart-row">
      ${row.map(tooth => toothChartButton(tooth)).join("")}
    </div>
  `).join("");
}

function toothChartButton(tooth) {
  const summary = toothChartSummary[tooth];
  const total = summary
    ? Object.values(summary.counts || {}).reduce((a, b) => a + b, 0)
    : 0;
  const classes = ["tooth-chart-tooth"];
  if (total > 0) classes.push("has-items");
  if (tooth === toothChartSelectedTooth) classes.push("selected");
  return `
    <button type="button" class="${classes.join(" ")}" onclick="selectChartTooth('${escapeAttr(tooth)}')">
      ${escapeHtml(tooth)}
      ${total > 0 ? `<span class="tooth-chart-count">${total}</span>` : ""}
    </button>
  `;
}

async function selectChartTooth(tooth, pendingType) {
  toothChartSelectedTooth = tooth;
  renderToothChartGrid();
  const detail = document.querySelector("[data-tooth-chart-detail]");
  if (!detail || !workspacePatientId) return;
  detail.innerHTML = '<div class="workspace-placeholder">载入牙位历史中...</div>';
  let data;
  try {
    const res = await fetch(
      `/api/patients/${encodeURIComponent(workspacePatientId)}/teeth/${encodeURIComponent(tooth)}/history`
    );
    if (!res.ok) throw new Error(String(res.status));
    data = await res.json();
  } catch {
    detail.innerHTML = '<div class="workspace-placeholder">牙位历史载入失败</div>';
    return;
  }
  if (tooth !== toothChartSelectedTooth) return; // 已切换到别的牙，丢弃过期响应
  detail.innerHTML = `
    <div class="tooth-detail-head"><strong>${escapeHtml(tooth)} 牙</strong>的临床记录</div>
    ${renderToothHistory(data.visits || [])}
    ${renderToothItemForm(tooth, pendingType || "")}
  `;
}

function renderToothHistory(visits) {
  if (!visits.length) {
    return '<div class="workspace-placeholder">这颗牙还没有结构化条目</div>';
  }
  return `
    <div class="tooth-history">
      ${visits.map(visit => `
        <div class="tooth-history-visit">
          <div class="tooth-history-date">
            ${escapeHtml(String(visit.visit_time || "").slice(0, 10) || "未知日期")}
            ${visit.doctor_name ? `<span class="tooth-history-doctor">${escapeHtml(visit.doctor_name)}</span>` : ""}
          </div>
          ${(visit.items || []).map(item => `
            <div class="tooth-history-item">
              <span class="tooth-item-type tooth-item-type-${escapeAttr(item.item_type)}">${escapeHtml(ITEM_TYPE_LABELS[item.item_type] || item.item_type)}</span>
              <span>${escapeHtml(item.content)}</span>
            </div>
          `).join("")}
        </div>
      `).join("")}
    </div>
  `;
}

// 录入表单：条目挂在选中的正式病历上（默认最新一份）。
function renderToothItemForm(tooth, pendingType) {
  const records = chartTargetRecords();
  if (!records.length) {
    return '<div class="workspace-placeholder">先在下方「+ 新建病历」建一份病历，才能录入牙位条目</div>';
  }
  const options = records.map((row, idx) => `
    <option value="${escapeAttr(row.record_id)}"${idx === 0 ? " selected" : ""}>
      ${escapeHtml(String(row.visit_time || row.updated_at || "").slice(0, 10))} ${escapeHtml(row.record_type || "病历")}
    </option>
  `).join("");
  const typeOptions = Object.entries(ITEM_TYPE_LABELS).map(([type, label]) =>
    `<option value="${type}"${type === pendingType ? " selected" : ""}>${label}</option>`
  ).join("");
  return `
    <div class="tooth-item-form" data-tooth-item-form>
      <div class="tooth-item-form-row">
        <select data-tooth-item-type>${typeOptions}</select>
        <select data-tooth-item-record>${options}</select>
      </div>
      <textarea data-tooth-item-content placeholder="为 ${escapeAttr(tooth)} 牙录入内容..."></textarea>
      <div class="tooth-item-form-actions">
        <button type="button" onclick="submitToothChartItem('${escapeAttr(tooth)}')">保存条目</button>
        <span class="record-save-status" data-tooth-item-status></span>
      </div>
    </div>
  `;
}

function chartTargetRecords() {
  const data = typeof workspaceDetailData === "function" ? workspaceDetailData() : null;
  if (!data) return [];
  return (data.medical_records || []).filter(row => !(row.draft_status || ""));
}

// 右侧操作区入口：先选类型；没点牙就提示先点牙。
function startToothChartEntry(itemType) {
  const detail = document.querySelector("[data-tooth-chart-detail]");
  if (!toothChartSelectedTooth) {
    if (detail) {
      detail.innerHTML = `<div class="workspace-placeholder">请先在上方牙位图点选一颗牙，再新增${escapeHtml(ITEM_TYPE_LABELS[itemType] || "")}</div>`;
    }
    return;
  }
  selectChartTooth(toothChartSelectedTooth, itemType);
}

// 追加条目 = 取目标病历现有 items + 新条目 → PUT 整组替换。
async function submitToothChartItem(tooth) {
  const form = document.querySelector("[data-tooth-item-form]");
  if (!form) return;
  const status = form.querySelector("[data-tooth-item-status]");
  const itemType = form.querySelector("[data-tooth-item-type]").value;
  const recordId = form.querySelector("[data-tooth-item-record]").value;
  const content = form.querySelector("[data-tooth-item-content]").value.trim();
  if (!content) {
    status.textContent = "内容不能为空";
    return;
  }
  status.textContent = "保存中...";
  try {
    const current = await fetch(`/api/medical-records/${encodeURIComponent(recordId)}/items`);
    if (!current.ok) throw new Error(String(current.status));
    const existing = (await current.json()).items || [];
    const items = existing
      .map(i => ({tooth: i.tooth, item_type: i.item_type, content: i.content}))
      .concat([{tooth, item_type: itemType, content}]);
    const res = await fetch(`/api/medical-records/${encodeURIComponent(recordId)}/items`, {
      method: "PUT",
      headers: {"Content-Type": "application/json"},
      body: JSON.stringify({items}),
    });
    if (!res.ok) throw new Error(String(res.status));
  } catch {
    status.textContent = "保存失败";
    return;
  }
  // 重载着色 + 该牙历史（表单会重渲染清空）。
  const selected = toothChartSelectedTooth;
  await loadToothChartPanel();
  if (selected) await selectChartTooth(selected);
}

Object.assign(window, {
  fdiToothRows,
  renderWorkspaceToothChartTab,
  renderToothChartSkeleton,
  loadToothChartPanel,
  renderToothChartGrid,
  toothChartButton,
  selectChartTooth,
  renderToothHistory,
  renderToothItemForm,
  chartTargetRecords,
  startToothChartEntry,
  submitToothChartItem,
});
