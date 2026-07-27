// 收费项目配置页：所有可见角色均可查看，只有系统管理员可以新增、编辑、停用或恢复。
// 保留 loadChargeItemsModule 等既有全局接口，继续由 config_center.js 装载到 cfgBody。

let _ciItems = [];
let _ciCounts = {active: 0, stopped: 0};
let _ciById = Object.create(null);
let _ciSearch = "";
let _ciCat = "";
let _ciStatus = "active";
let _ciEditingId = null;
let _ciSaving = null;
let _ciActiveDialog = null;
let _ciReloadPending = false;
let _ciDialogToken = 0;
let _ciSaveToken = 0;
let _ciToken = 0;

function ciCanManage() {
  return window.__isSystemAdmin === true;
}

function ciFlattenGroups(groups) {
  return (groups || []).flatMap(group => (group.items || []).map(item => ({
    ...item,
    fee_type: item.fee_type || group.category || group.handle_type || "",
    status: "active",
    origin: item.origin === "local" || item.origin === "imported" ? item.origin : "unknown",
  })));
}

async function ciResponseError(response) {
  const message = await response.json().catch(() => ({}));
  return message.detail || response.status;
}

async function ciFetchItems() {
  const canManage = ciCanManage();
  const url = canManage
    ? `/api/handle-items/manage?status=${encodeURIComponent(_ciStatus)}`
    : "/api/handle-items";
  const response = await fetch(url);
  if (!response.ok) throw new Error(await ciResponseError(response));
  const data = await response.json();
  if (canManage) {
    return {
      items: data.items || [],
      counts: data.counts || {active: 0, stopped: 0},
    };
  }
  const items = ciFlattenGroups(data.groups);
  return {items, counts: {active: items.length, stopped: 0}};
}

// 防止模块内重复加载以及切换配置子页后的晚到响应覆盖当前页面。
function _ciStale(token) {
  return token !== _ciToken ||
    (typeof _cfgTab !== "undefined" && _cfgTab !== "charge-items");
}

async function loadChargeItemsModule() {
  const modulePanel = document.getElementById("cfgBody");
  if (!modulePanel || (typeof _cfgTab !== "undefined" && _cfgTab !== "charge-items")) return;
  const token = ++_ciToken;
  modulePanel.innerHTML = '<div class="module-loading">收费项目载入中...</div>';
  try {
    const result = await ciFetchItems();
    if (_ciStale(token)) return;
    _ciItems = result.items;
    _ciCounts = result.counts;
    _ciById = Object.create(null);
    _ciItems.forEach(item => { _ciById[String(item.handle_id || "")] = item; });
    if (_ciStale(token)) return;
    modulePanel.innerHTML = `
      ${ciPageHeader()}
      ${ciFilterBar()}
      <div data-ci-results></div>
    `;
    bindChargeItems(modulePanel);
    renderChargeItemsList(modulePanel);
  } catch (error) {
    if (_ciStale(token)) return;
    const message = `收费项目载入失败：${error.message || "网络异常"}`;
    modulePanel.innerHTML = `<div class="module-loading">${escapeHtml(message)}</div>`;
  }
}

async function ciReloadAfterMutation() {
  if (document.querySelector("[data-ci-dialog]")) {
    _ciReloadPending = true;
    return false;
  }
  _ciReloadPending = false;
  await loadChargeItemsModule();
  return true;
}

async function ciFlushPendingReload() {
  if (!_ciReloadPending || document.querySelector("[data-ci-dialog]")) return false;
  _ciReloadPending = false;
  await loadChargeItemsModule();
  return true;
}

function ciPageHeader() {
  const summary = ciCanManage()
    ? `在用 ${Number(_ciCounts.active || 0)} 项 · 已停用 ${Number(_ciCounts.stopped || 0)} 项`
    : `在用 ${Number(_ciCounts.active || 0)} 项 · 只读`;
  const add = ciCanManage()
    ? '<button type="button" class="tooth-confirm-btn" data-ci-add>新增收费项目</button>'
    : "";
  return `
    <div class="module-head">
      <div><strong>收费项目</strong><span>${escapeHtml(summary)}</span></div>
      ${add}
    </div>`;
}

function ciFilterBar() {
  const categories = [...new Set(_ciItems.map(item => item.fee_type || "其他"))];
  if (_ciCat && !categories.includes(_ciCat)) _ciCat = "";
  const categoryOptions = ['<option value="">全部分类</option>'].concat(
    categories.map(category => `<option value="${escapeAttr(category)}"${category === _ciCat ? " selected" : ""}>${escapeHtml(category)}</option>`),
  ).join("");
  const statusFilter = ciCanManage() ? `
    <select class="ord-input" data-ci-status aria-label="状态筛选">
      <option value="active"${_ciStatus === "active" ? " selected" : ""}>在用</option>
      <option value="stopped"${_ciStatus === "stopped" ? " selected" : ""}>已停用</option>
      <option value="all"${_ciStatus === "all" ? " selected" : ""}>全部状态</option>
    </select>` : "";
  return `
    <div class="ci-toolbar">
      <input class="ord-input" data-ci-search placeholder="搜索名称/编码" value="${escapeAttr(_ciSearch)}">
      <select class="ord-input" data-ci-cat aria-label="费用分类筛选">${categoryOptions}</select>
      ${statusFilter}
    </div>`;
}

function renderChargeItemsList(root) {
  const results = root.querySelector("[data-ci-results]");
  if (!results) return;
  const keyword = _ciSearch.trim().toLowerCase();
  const items = _ciItems.filter(item => {
    const matchesSearch = !keyword ||
      String(item.name || "").toLowerCase().includes(keyword) ||
      String(item.code || "").toLowerCase().includes(keyword);
    return matchesSearch && (!_ciCat || (item.fee_type || "其他") === _ciCat);
  });
  results.innerHTML = items.length ? `
    <div class="ci-table">
      <div class="ci-table-row ci-table-head">
        <span>编码</span>
        <span>名称</span>
        <span>费用分类</span>
        <span>单位</span>
        <span>单价</span>
        <span>状态/操作</span>
      </div>
      ${items.map(ciRow).join("")}
    </div>` : '<div class="module-empty">无匹配收费项目</div>';

  results.querySelectorAll("[data-ci-edit]").forEach(button =>
    button.addEventListener("click", () => startEditChargeItem(button.dataset.ciEdit)));
  results.querySelectorAll("[data-ci-stop]").forEach(button =>
    button.addEventListener("click", () => stopChargeItem(button.dataset.ciStop)));
  results.querySelectorAll("[data-ci-restore]").forEach(button =>
    button.addEventListener("click", () => restoreChargeItem(button.dataset.ciRestore)));
  results.querySelectorAll("[data-ci-repair-restore]").forEach(button =>
    button.addEventListener("click", () => openChargeItemDialog(button.dataset.ciRepairRestore, "restore")));
}

function ciRow(item) {
  const originLabel = item.origin === "local" ? "本地" : (item.origin === "imported" ? "历史导入" : "只读");
  let actions = `<span class="ci-origin">${originLabel}</span>`;
  if (ciCanManage()) {
    if (item.status === "stopped" && item.restore_issue) {
      actions = `<span class="ci-origin" title="${escapeAttr(item.restore_issue)}">资料不完整</span>` +
        `<button type="button" class="plain-button" data-ci-repair-restore="${escapeAttr(item.handle_id)}">补全并恢复</button>`;
    } else if (item.status === "stopped") {
      actions = `<span class="ci-origin">已停用</span>` +
        `<button type="button" class="plain-button" data-ci-restore="${escapeAttr(item.handle_id)}">恢复</button>`;
    } else {
      actions = `<span class="ci-origin">在用</span><button type="button" class="plain-button" data-ci-edit="${escapeAttr(item.handle_id)}">编辑</button>` +
        `<button type="button" class="plain-button steril-del" data-ci-stop="${escapeAttr(item.handle_id)}">停用</button>`;
    }
  }
  return `
    <div class="ci-table-row">
      <span>${escapeHtml(item.code || "")}</span>
      <span>${escapeHtml(item.name || "")}</span>
      <span>${escapeHtml(item.fee_type || "")}</span>
      <span>${escapeHtml(item.unit || "")}</span>
      <span>${item.price == null ? "" : escapeHtml(String(item.price))}</span>
      <span class="ci-actions">${actions}</span>
    </div>`;
}

function bindChargeItems(container) {
  const add = container.querySelector("[data-ci-add]");
  if (add) add.addEventListener("click", () => openChargeItemDialog());
  const search = container.querySelector("[data-ci-search]");
  if (search) search.addEventListener("input", () => {
    _ciSearch = search.value;
    renderChargeItemsList(container);
  });
  const category = container.querySelector("[data-ci-cat]");
  if (category) category.addEventListener("change", () => {
    _ciCat = category.value;
    renderChargeItemsList(container);
  });
  const status = container.querySelector("[data-ci-status]");
  if (status) status.addEventListener("change", () => {
    if (!ciCanManage()) return;
    _ciStatus = status.value;
    _ciCat = "";
    loadChargeItemsModule();
  });
}

function openChargeItemDialog(id = null, mode = "edit") {
  if (!ciCanManage()) return;
  const container = document.getElementById("cfgBody");
  if (!container) return;
  const item = id == null ? null : _ciById[String(id)];
  if (id != null && !item) return;
  container.querySelector("[data-ci-dialog]")?.remove();
  _ciActiveDialog = null;
  _ciEditingId = item ? String(item.handle_id) : null;
  _ciSaving = null;
  const repairing = mode === "restore";
  const dialogTitle = repairing ? "补全并恢复收费项目" : (_ciEditingId ? "编辑收费项目" : "新增收费项目");
  const form = {
    name: item?.name || "",
    code: item?.code || "",
    fee_type: item?.fee_type || "",
    unit: item?.unit || "",
    price: item?.price == null ? "" : String(item.price),
  };
  container.insertAdjacentHTML("beforeend", `
    <div class="modal-backdrop" data-ci-dialog>
      <section class="appt-modal" role="dialog" aria-modal="true" aria-label="${dialogTitle}">
        <div class="modal-head">
          <strong>${dialogTitle}</strong>
          <button type="button" class="plain-button" data-ci-close>关闭</button>
        </div>
        <div class="ci-form-grid">
          <label><span>名称</span><input class="ord-input" data-ci-f="name" value="${escapeAttr(form.name)}"></label>
          <label><span>编码</span><input class="ord-input" data-ci-f="code" value="${escapeAttr(form.code)}"></label>
          <label><span>费用分类</span><input class="ord-input" data-ci-f="fee_type" value="${escapeAttr(form.fee_type)}"></label>
          <label><span>单位</span><input class="ord-input" data-ci-f="unit" value="${escapeAttr(form.unit)}"></label>
          <label><span>单价</span><input class="ord-input" data-ci-f="price" inputmode="decimal" value="${escapeAttr(form.price)}"></label>
        </div>
        <div class="modal-actions">
          <span class="ci-dialog-status" data-ci-dialog-status></span>
          <div>
            <button type="button" class="plain-button" data-ci-cancel>取消</button>
            <button type="button" class="tooth-confirm-btn" data-ci-save>${repairing ? "补全并恢复" : (_ciEditingId ? "保存修改" : "新增")}</button>
          </div>
        </div>
      </section>
    </div>`);
  const dialog = container.querySelector("[data-ci-dialog]");
  if (!dialog) return;
  const dialogState = {dialog, editingId: _ciEditingId, mode, token: ++_ciDialogToken};
  _ciActiveDialog = dialogState;
  dialog.querySelector("[data-ci-close]")?.addEventListener("click", () => closeChargeItemDialog(dialogState));
  dialog.querySelector("[data-ci-cancel]")?.addEventListener("click", () => closeChargeItemDialog(dialogState));
  dialog?.querySelector("[data-ci-save]")?.addEventListener("click", saveChargeItem);
}

function ciDialogIsCurrent(dialogState) {
  return Boolean(dialogState) && _ciActiveDialog === dialogState &&
    _ciActiveDialog.token === dialogState.token &&
    document.querySelector("[data-ci-dialog]") === dialogState.dialog;
}

function ciSaveIsCurrent(saveState) {
  return Boolean(saveState) && _ciSaving === saveState &&
    _ciSaving.token === saveState.token && ciDialogIsCurrent(saveState.dialogState);
}

function closeChargeItemDialog(dialogState = _ciActiveDialog, flushReload = true) {
  if (!ciDialogIsCurrent(dialogState)) return;
  dialogState.dialog.remove();
  _ciActiveDialog = null;
  _ciEditingId = null;
  _ciSaving = null;
  if (flushReload) return ciFlushPendingReload();
}

function ciReadForm(dialog) {
  const get = field => (dialog?.querySelector(`[data-ci-f="${field}"]`)?.value || "").trim();
  return {
    name: get("name"),
    code: get("code"),
    fee_type: get("fee_type"),
    unit: get("unit"),
    price: get("price"),
  };
}

function ciSetDialogPending(dialog, pending) {
  dialog?.querySelectorAll("button, input").forEach(control => { control.disabled = pending; });
}

function startEditChargeItem(id) {
  openChargeItemDialog(id);
}

async function saveChargeItem() {
  const dialogState = _ciActiveDialog;
  if (!ciCanManage() || !ciDialogIsCurrent(dialogState) ||
      (_ciSaving && _ciSaving.dialogState.token === dialogState.token)) return;
  const dialog = dialogState.dialog;
  const editingId = dialogState.editingId;
  const restoring = dialogState.mode === "restore";
  const payload = ciReadForm(dialog);
  const status = dialog.querySelector("[data-ci-dialog-status]");
  if (!payload.name || !payload.fee_type || payload.price === "") {
    if (status) status.textContent = "名称、费用分类和单价必填";
    return;
  }
  const saveState = {dialogState, token: ++_ciSaveToken};
  _ciSaving = saveState;
  ciSetDialogPending(dialog, true);
  try {
    const url = restoring
      ? `/api/handle-items/${encodeURIComponent(editingId)}/restore`
      : (editingId ? `/api/handle-items/${encodeURIComponent(editingId)}` : "/api/handle-items");
    const response = await fetch(url, {
      method: restoring ? "POST" : (editingId ? "PUT" : "POST"),
      headers: {"Content-Type": "application/json"},
      body: JSON.stringify(payload),
    });
    if (!response.ok) throw new Error(await ciResponseError(response));
    const stillCurrent = ciSaveIsCurrent(saveState);
    if (stillCurrent) closeChargeItemDialog(dialogState, false);
    if (typeof invalidateHandleItemsCache === "function") invalidateHandleItemsCache();
    await ciReloadAfterMutation();
  } catch (error) {
    if (ciSaveIsCurrent(saveState) && status) {
      status.textContent = `保存失败：${error.message || "网络异常"}`;
    }
  } finally {
    const ownsSaving = _ciSaving === saveState && _ciSaving.token === saveState.token;
    const unlockCurrent = ciSaveIsCurrent(saveState);
    if (ownsSaving) {
      _ciSaving = null;
      if (unlockCurrent) {
        ciSetDialogPending(dialog, false);
      } else if (_ciActiveDialog === dialogState && _ciActiveDialog.token === dialogState.token) {
        _ciActiveDialog = null;
        _ciEditingId = null;
      }
    }
  }
}

async function stopChargeItem(id) {
  if (!ciCanManage()) return;
  if (!window.confirm("停用后，这个项目将不再出现在以后新开的处置中；历史处置和收费记录不会改变，可稍后恢复。")) return;
  try {
    const response = await fetch(`/api/handle-items/${encodeURIComponent(id)}`, {method: "DELETE"});
    if (!response.ok) throw new Error(await ciResponseError(response));
    if (typeof invalidateHandleItemsCache === "function") invalidateHandleItemsCache();
    await ciReloadAfterMutation();
  } catch (error) {
    window.alert(`停用失败：${error.message || "网络异常"}`);
  }
}

async function restoreChargeItem(id) {
  if (!ciCanManage()) return;
  try {
    const response = await fetch(`/api/handle-items/${encodeURIComponent(id)}/restore`, {method: "POST"});
    if (!response.ok) throw new Error(await ciResponseError(response));
    if (typeof invalidateHandleItemsCache === "function") invalidateHandleItemsCache();
    await ciReloadAfterMutation();
  } catch (error) {
    window.alert(`恢复失败：${error.message || "网络异常"}`);
  }
}

// 兼容旧页面可能保留的调用名；动作语义已统一为“停用”。
function deleteChargeItem(id) {
  return stopChargeItem(id);
}

Object.assign(window, {
  loadChargeItemsModule,
  saveChargeItem,
  startEditChargeItem,
  deleteChargeItem,
  stopChargeItem,
  restoreChargeItem,
});
