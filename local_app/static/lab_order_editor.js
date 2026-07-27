// 技工单 tab：列表 + 新增/编辑/删除/标记返回 + 配图上传 + 技工所/项目/比色选项设置。
// 仿处置单，但单据更轻（无划价收费）。状态 sent已送出 / returned已返回。

let labOrdersCache = [];
let labEditId = null;        // 编辑中的 order_id；null=新增
let labConfigCache = {factories: [], projects: [], colors: []};

const LAB_STATUS_LABEL = {sent: "已送出", returned: "已返回"};

async function renderWorkspaceLabOrdersTab(panel) {
  const pid = workspacePatientId;
  panel.textContent = "加载中...";
  let data;
  try {
    const res = await fetch(`/api/patients/${encodeURIComponent(pid)}/lab-orders`);
    if (pid !== workspacePatientId) return;
    if (!res.ok) { panel.textContent = "技工单载入失败"; return; }
    data = await res.json();
    if (pid !== workspacePatientId) return;
  } catch {
    if (pid === workspacePatientId) panel.textContent = "技工单载入失败";
    return;
  }
  labOrdersCache = data.orders || [];
  const cards = labOrdersCache.length
    ? labOrdersCache.map(renderLabOrderCard).join("")
    : `<div class="workspace-placeholder">暂无技工单</div>`;
  panel.innerHTML = `
    <div class="lab-toolbar">
      <button type="button" onclick="openLabOrderModal()">+ 新增技工单</button>
      <button type="button" class="plain-button" onclick="openLabConfigModal()">选项设置</button>
    </div>
    <div class="lab-order-list">${cards}</div>
  `;
}

function labOrdersRefresh() {
  const panel = document.querySelector('[data-workspace-panel="lab-orders"]');
  if (panel) renderWorkspaceLabOrdersTab(panel);
}

function renderLabOrderCard(o) {
  const imgs = (o.images || []).map(im => `
    <div class="lab-img">
      <img src="${escapeAttr(im.url)}" loading="lazy" alt="${escapeAttr(im.file_name || "")}"
           onclick="openLabImageView('${escapeAttr(im.url)}')">
      <button type="button" class="lab-img-del" title="删除配图" onclick="deleteLabImage('${escapeAttr(im.img_id)}')">×</button>
    </div>`).join("");
  const returned = o.status === "returned";
  return `
    <div class="lab-order-card lab-status-${escapeAttr(o.status)}">
      <div class="lab-card-head">
        <strong>${escapeHtml(o.project_type || "(未填项目)")}</strong>
        <span class="lab-badge">${escapeHtml(LAB_STATUS_LABEL[o.status] || o.status || "")}</span>
        ${o.tooth_codes ? `<span class="lab-teeth">牙位 ${escapeHtml(o.tooth_codes)}</span>` : ""}
      </div>
      <div class="lab-card-meta">
        ${o.lab_name ? `<span>技工所：${escapeHtml(o.lab_name)}</span>` : ""}
        ${o.color ? `<span>比色：${escapeHtml(o.color)}</span>` : ""}
        ${o.sent_date ? `<span>送出：${escapeHtml(o.sent_date)}</span>` : ""}
        ${o.return_date ? `<span>返回：${escapeHtml(o.return_date)}</span>` : ""}
      </div>
      ${o.notes ? `<div class="lab-card-notes">${escapeHtml(o.notes)}</div>` : ""}
      <div class="lab-card-imgs">${imgs}
        <label class="lab-img-add">+ 配图
          <input type="file" accept="image/*" hidden onchange="uploadLabImage('${escapeAttr(o.order_id)}', this)">
        </label>
      </div>
      <div class="lab-card-actions">
        ${returned ? "" : `<button type="button" onclick="markLabReturned('${escapeAttr(o.order_id)}')">标记返回</button>`}
        <button type="button" class="plain-button" onclick="openLabOrderModal('${escapeAttr(o.order_id)}')">编辑</button>
        <button type="button" class="plain-button lab-del" onclick="deleteLabOrder('${escapeAttr(o.order_id)}')">删除</button>
      </div>
    </div>`;
}

// ---------- 新增/编辑模态 ----------
function _labModalEl() {
  let el = document.getElementById("labOrderModal");
  if (el) return el;
  el = document.createElement("div");
  el.id = "labOrderModal";
  el.className = "modal-backdrop";
  el.hidden = true;
  el.addEventListener("click", e => { if (e.target === el) closeLabOrderModal(); });
  document.body.appendChild(el);
  return el;
}

async function openLabOrderModal(orderId) {
  labEditId = orderId || null;
  await loadLabConfig();
  const o = labEditId ? (labOrdersCache.find(x => x.order_id === labEditId) || {}) : {};
  const opts = (arr) => arr.map(v => `<option value="${escapeAttr(v)}"></option>`).join("");
  const el = _labModalEl();
  el.innerHTML = `
    <section class="modal-card lab-modal">
      <div class="modal-head">${labEditId ? "编辑技工单" : "新增技工单"}</div>
      <div class="modal-body lab-form">
        <label>项目 *<input id="labProject" list="labProjectList" value="${escapeAttr(o.project_type || "")}" placeholder="如 全瓷冠 / 活动义齿"></label>
        <datalist id="labProjectList">${opts(labConfigCache.projects)}</datalist>
        <label>牙位<input id="labTeeth" value="${escapeAttr(o.tooth_codes || "")}" placeholder="逗号分隔，如 16,17"></label>
        <label>技工所<input id="labFactory" list="labFactoryList" value="${escapeAttr(o.lab_name || "")}"></label>
        <datalist id="labFactoryList">${opts(labConfigCache.factories)}</datalist>
        <label>比色<input id="labColor" list="labColorList" value="${escapeAttr(o.color || "")}" placeholder="如 A2"></label>
        <datalist id="labColorList">${opts(labConfigCache.colors)}</datalist>
        <label>送出日期<input id="labSent" type="date" value="${escapeAttr(o.sent_date || "")}"></label>
        <label>返回日期<input id="labReturn" type="date" value="${escapeAttr(o.return_date || "")}"></label>
        <label class="lab-form-wide">备注<textarea id="labNotes" rows="2">${escapeHtml(o.notes || "")}</textarea></label>
      </div>
      <div class="modal-actions">
        <span id="labOrderStatus" class="lab-status-msg"></span>
        <button type="button" class="plain-button" onclick="closeLabOrderModal()">取消</button>
        <button type="button" onclick="saveLabOrder()">保存</button>
      </div>
    </section>`;
  el.hidden = false;
}

function closeLabOrderModal() {
  const el = document.getElementById("labOrderModal");
  if (el) el.hidden = true;
  labEditId = null;
}

async function saveLabOrder() {
  const status = document.getElementById("labOrderStatus");
  const body = {
    project_type: document.getElementById("labProject").value.trim(),
    tooth_codes: document.getElementById("labTeeth").value.trim(),
    lab_name: document.getElementById("labFactory").value.trim(),
    color: document.getElementById("labColor").value.trim(),
    sent_date: document.getElementById("labSent").value,
    return_date: document.getElementById("labReturn").value,
    notes: document.getElementById("labNotes").value.trim(),
  };
  if (!body.project_type) { status.textContent = "项目必填"; return; }
  const pid = workspacePatientId;
  status.textContent = "保存中...";
  const url = labEditId
    ? `/api/patients/${encodeURIComponent(pid)}/lab-orders/${encodeURIComponent(labEditId)}`
    : `/api/patients/${encodeURIComponent(pid)}/lab-orders`;
  let res;
  try {
    res = await fetch(url, {
      method: labEditId ? "PUT" : "POST",
      headers: {"Content-Type": "application/json"},
      body: JSON.stringify(body),
    });
  } catch { status.textContent = "保存失败（网络异常）"; return; }
  if (!res.ok) {
    const d = await res.json().catch(() => ({}));
    status.textContent = d.detail || "保存失败";
    return;
  }
  closeLabOrderModal();
  labOrdersRefresh();
}

async function markLabReturned(orderId) {
  const today = localDateValue();
  const date = await appPrompt("返回日期", today);
  if (date === null) return;
  let res;
  try {
    res = await fetch(`/api/lab-orders/${encodeURIComponent(orderId)}/return`, {
      method: "PUT", headers: {"Content-Type": "application/json"},
      body: JSON.stringify({return_date: date.trim() || today}),
    });
  } catch { return; }
  if (res.ok) labOrdersRefresh();
}

async function deleteLabOrder(orderId) {
  if (!window.confirm("删除该技工单及其配图？")) return;
  const pid = workspacePatientId;
  let res;
  try {
    res = await fetch(`/api/patients/${encodeURIComponent(pid)}/lab-orders/${encodeURIComponent(orderId)}`,
      {method: "DELETE"});
  } catch { return; }
  if (res.ok) labOrdersRefresh();
}

async function uploadLabImage(orderId, input) {
  const file = input.files && input.files[0];
  if (!file) return;
  const fd = new FormData();
  fd.append("file", file);
  let res;
  try {
    res = await fetch(`/api/lab-orders/${encodeURIComponent(orderId)}/images`, {method: "POST", body: fd});
  } catch { input.value = ""; return; }
  input.value = "";
  if (res.ok) labOrdersRefresh();
  else {
    const d = await res.json().catch(() => ({}));
    window.alert(d.detail || "配图上传失败");
  }
}

async function deleteLabImage(imgId) {
  if (!window.confirm("删除这张配图？")) return;
  let res;
  try { res = await fetch(`/api/lab-orders/images/${encodeURIComponent(imgId)}`, {method: "DELETE"}); }
  catch { return; }
  if (res.ok) labOrdersRefresh();
}

// 复用影像 lightbox 看配图（若已加载），否则新标签页打开
function openLabImageView(url) {
  window.open(url, "_blank");
}

// ---------- 选项设置（技工所/项目/比色，存 app_settings） ----------
async function loadLabConfig() {
  try {
    const [f, p, c] = await Promise.all([
      fetch("/api/lab-config/factories").then(r => r.json()),
      fetch("/api/lab-config/projects").then(r => r.json()),
      fetch("/api/lab-config/colors").then(r => r.json()),
    ]);
    labConfigCache = {factories: f.list || [], projects: p.list || [], colors: c.list || []};
  } catch { /* 配置加载失败不阻断主流程，datalist 为空即可 */ }
}

async function openLabConfigModal() {
  await loadLabConfig();
  const el = _labModalEl();
  const ta = (id, arr, label) =>
    `<label class="lab-form-wide">${label}（每行一个）<textarea id="${id}" rows="4">${escapeHtml(arr.join("\n"))}</textarea></label>`;
  el.innerHTML = `
    <section class="modal-card lab-modal">
      <div class="modal-head">技工单选项设置</div>
      <div class="modal-body lab-form">
        ${ta("cfgFactories", labConfigCache.factories, "技工所")}
        ${ta("cfgProjects", labConfigCache.projects, "项目")}
        ${ta("cfgColors", labConfigCache.colors, "比色")}
      </div>
      <div class="modal-actions">
        <span id="labOrderStatus" class="lab-status-msg"></span>
        <button type="button" class="plain-button" onclick="closeLabOrderModal()">取消</button>
        <button type="button" onclick="saveLabConfig()">保存</button>
      </div>
    </section>`;
  el.hidden = false;
}

async function saveLabConfig() {
  const status = document.getElementById("labOrderStatus");
  const lines = id => document.getElementById(id).value.split("\n").map(s => s.trim()).filter(Boolean);
  const payloads = {
    factories: lines("cfgFactories"),
    projects: lines("cfgProjects"),
    colors: lines("cfgColors"),
  };
  status.textContent = "保存中...";
  try {
    await Promise.all(Object.entries(payloads).map(([kind, list]) =>
      fetch(`/api/lab-config/${kind}`, {
        method: "PUT", headers: {"Content-Type": "application/json"},
        body: JSON.stringify({list}),
      })));
  } catch { status.textContent = "保存失败"; return; }
  closeLabOrderModal();
}

Object.assign(window, {
  renderWorkspaceLabOrdersTab, labOrdersRefresh, renderLabOrderCard,
  openLabOrderModal, closeLabOrderModal, saveLabOrder,
  markLabReturned, deleteLabOrder, uploadLabImage, deleteLabImage, openLabImageView,
  openLabConfigModal, saveLabConfig,
});
