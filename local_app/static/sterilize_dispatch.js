// 消毒管理 · 送消单子视图：列表 + 新建(选器械+数量) + 送审/审核/撤销审核/删除。
// 渲染进共享 modulePanel(挂在 sterilization.js 的子视图切换下)。data-* + 事件委托防注入。

let dispatchToken = 0;
const DISPATCH_STATUS = {draft: "草稿", submitted: "已送审", audited: "已审核", cancelled: "已取消"};

async function loadDispatchSubview() {
  if (!modulePanel) return;
  const token = ++dispatchToken;
  modulePanel.innerHTML = `<div class="module-loading">送消单载入中...</div>`;
  let data;
  try {
    const res = await fetch("/api/dispatch");
    if (token !== dispatchToken) return;
    data = res.ok ? await res.json() : {dispatches: []};
    if (token !== dispatchToken) return;
  } catch {
    if (token !== dispatchToken) return;
    modulePanel.innerHTML = `<div class="module-loading">送消单载入失败</div>`;
    return;
  }
  const rows = data.dispatches || [];
  modulePanel.innerHTML = `
    ${typeof sterilSubTabs === "function" ? sterilSubTabs("dispatch") : ""}
    <div class="module-head">
      <strong>器械送消单</strong>
      <span>${escapeHtml(formatCount(data.totalcount))} 单</span>
      <button type="button" class="plain-button" data-dispatch-new="1">新建送消单</button>
    </div>
    <div class="module-table">
      <div class="module-row module-row-head">
        <span>送消单号</span><span>科室</span><span>送消人</span><span>状态</span><span>审核人</span><span>操作</span>
      </div>
      ${rows.map(dispatchRow).join("") || '<div class="module-empty">暂无送消单</div>'}
    </div>
  `;
  if (typeof bindSterilSubTabs === "function") bindSterilSubTabs(modulePanel);
  bindDispatch(modulePanel);
}

function dispatchRow(r) {
  const st = r.status || "draft";
  const btns = [];
  if (st === "draft") {
    btns.push(`<button type="button" class="plain-button" data-dispatch-submit="${escapeAttr(r.id)}">送审</button>`);
    btns.push(`<button type="button" class="plain-button steril-del" data-dispatch-del="${escapeAttr(r.id)}">删除</button>`);
  } else if (st === "submitted") {
    btns.push(`<button type="button" class="plain-button" data-dispatch-audit="${escapeAttr(r.id)}">审核</button>`);
  } else if (st === "audited") {
    btns.push(`<button type="button" class="plain-button" data-dispatch-cancel="${escapeAttr(r.id)}">撤销审核</button>`);
  }
  return `
    <div class="module-row">
      <span>${escapeHtml(r.dispatch_no || "")}</span>
      <span>${escapeHtml(r.department || "")}</span>
      <span>${escapeHtml(r.dispatcher || "")}</span>
      <span class="steril-status steril-${escapeAttr(st)}">${escapeHtml(DISPATCH_STATUS[st] || st)}</span>
      <span>${escapeHtml(r.auditor || "")}</span>
      <span class="steril-actions">${btns.join("")}</span>
    </div>`;
}

function bindDispatch(container) {
  if (!container) return;
  const add = container.querySelector("[data-dispatch-new]");
  if (add) add.addEventListener("click", openNewDispatch);
  container.querySelectorAll("[data-dispatch-submit]").forEach(b => b.addEventListener("click", () => submitDispatch(b.dataset.dispatchSubmit)));
  container.querySelectorAll("[data-dispatch-audit]").forEach(b => b.addEventListener("click", () => auditDispatch(b.dataset.dispatchAudit)));
  container.querySelectorAll("[data-dispatch-cancel]").forEach(b => b.addEventListener("click", () => cancelAuditDispatch(b.dataset.dispatchCancel)));
  container.querySelectorAll("[data-dispatch-del]").forEach(b => b.addEventListener("click", () => deleteDispatch(b.dataset.dispatchDel)));
}

// ---- 新建送消单弹窗 ----
async function openNewDispatch() {
  const m = document.getElementById("dispatchModal");
  if (!m) return;
  document.getElementById("dispatchDept").value = "";
  document.getElementById("dispatchDispatcher").value = "";
  document.getElementById("dispatchStatus").textContent = "";
  const list = document.getElementById("dispatchInstrList");
  list.innerHTML = `<div class="module-loading">载入器械...</div>`;
  m.hidden = false;
  let instruments = [];
  try { instruments = (await (await fetch("/api/instruments?status=active")).json()).instruments || []; }
  catch { list.innerHTML = `<div class="module-empty">器械载入失败</div>`; return; }
  if (!instruments.length) { list.innerHTML = `<div class="module-empty">无可用器械，请先在器械库新增</div>`; return; }
  list.innerHTML = instruments.map(i => `
    <label class="dispatch-instr-opt">
      <input type="checkbox" data-di-id="${escapeAttr(i.id)}">
      ${escapeHtml(i.code || "")} ${escapeHtml(i.name || "")}
      <input type="number" min="1" value="1" class="ord-input dispatch-qty" data-di-qty="${escapeAttr(i.id)}" style="width:64px">
    </label>`).join("");
}

function closeNewDispatch() {
  const m = document.getElementById("dispatchModal");
  if (m) m.hidden = true;
}

async function submitNewDispatch() {
  const status = document.getElementById("dispatchStatus");
  const items = [];
  document.querySelectorAll("[data-di-id]").forEach(cb => {
    if (cb.checked) {
      const qtyEl = document.querySelector(`[data-di-qty="${CSS.escape(cb.dataset.diId)}"]`);
      const qty = qtyEl ? parseFloat(qtyEl.value) : 1;
      items.push({instrument_id: cb.dataset.diId, qty: qty > 0 ? qty : 1});
    }
  });
  if (!items.length) { if (status) status.textContent = "请至少勾选一件器械"; return; }
  const department = document.getElementById("dispatchDept").value.trim();
  const dispatcher = document.getElementById("dispatchDispatcher").value.trim();
  if (!department) { if (status) status.textContent = "请填送消科室"; return; }
  if (!dispatcher) { if (status) status.textContent = "请填送消人"; return; }
  const payload = {department, dispatcher, items};
  if (status) status.textContent = "保存中...";
  let res;
  try { res = await fetch("/api/dispatch", {method: "POST", headers: {"Content-Type": "application/json"}, body: JSON.stringify(payload)}); }
  catch { if (status) status.textContent = "保存失败（网络异常）"; return; }
  if (!res.ok) { const m = await res.json().catch(() => ({})); if (status) status.textContent = "保存失败：" + (m.detail || res.status); return; }
  closeNewDispatch();
  loadDispatchSubview();
}

async function submitDispatch(id) {
  if (!window.confirm("送审该送消单？送审后不可再编辑/删除。")) return;
  await _dispatchAction(`/api/dispatch/${encodeURIComponent(id)}/submit`, {});
}

async function auditDispatch(id) {
  const auditor = await appPrompt("审核人姓名：", "");
  if (auditor === null) return;
  if (!auditor.trim()) { window.alert("审核人必填"); return; }
  await _dispatchAction(`/api/dispatch/${encodeURIComponent(id)}/audit`, {auditor: auditor.trim()});
}

async function cancelAuditDispatch(id) {
  const operator = await appPrompt("撤销审核操作人姓名：", "");
  if (operator === null) return;
  if (!operator.trim()) { window.alert("操作人必填"); return; }
  await _dispatchAction(`/api/dispatch/${encodeURIComponent(id)}/cancel-audit`, {operator: operator.trim()});
}

async function deleteDispatch(id) {
  if (!window.confirm("删除该草稿送消单？")) return;
  try {
    const res = await fetch(`/api/dispatch/${encodeURIComponent(id)}`, {method: "DELETE"});
    if (!res.ok) { const m = await res.json().catch(() => ({})); window.alert("删除失败：" + (m.detail || res.status)); return; }
  } catch { window.alert("删除失败（网络异常）"); return; }
  loadDispatchSubview();
}

async function _dispatchAction(url, body) {
  try {
    const res = await fetch(url, {method: "POST", headers: {"Content-Type": "application/json"}, body: JSON.stringify(body)});
    if (!res.ok) { const m = await res.json().catch(() => ({})); window.alert("操作失败：" + (m.detail || res.status)); return; }
  } catch { window.alert("操作失败（网络异常）"); return; }
  loadDispatchSubview();
}

Object.assign(window, {
  loadDispatchSubview, openNewDispatch, closeNewDispatch, submitNewDispatch,
  submitDispatch, auditDispatch, cancelAuditDispatch, deleteDispatch,
});
