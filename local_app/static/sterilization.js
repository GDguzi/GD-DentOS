// 消毒管理 · 器械库前端：器械列表 + 新增/编辑(内联表单) + 停用/删除 + 分类管理。
// 渲染进共享 modulePanel。数据走 data-* + 事件委托，不拼内联JS(防注入)。

let sterilCategories = [];
let sterilEditingId = null;       // null=新增, 否则编辑该器械
let sterilCategoryFilter = "";
let sterilToken = 0;
let sterilFormCache = {code: "", name: "", category_id: "", spec: "", note: ""};
let sterilSubview = "library";    // library器械库 / dispatch送消单

// 消毒管理入口：渲染子视图切换条，按当前子视图分发
function loadSterilizationModule() {
  if (sterilSubview === "dispatch" && typeof loadDispatchSubview === "function") return loadDispatchSubview();
  return loadLibrarySubview();
}

function sterilSubTabs(active) {
  const tabs = [["library", "器械库"], ["dispatch", "送消单"]];
  return `<nav class="module-tabs" aria-label="消毒子模块">${tabs.map(([v, label]) =>
    `<button type="button" class="plain-button${v === active ? " active" : ""}" data-steril-subview="${v}">${label}</button>`).join("")}</nav>`;
}

function bindSterilSubTabs(container) {
  if (!container) return;
  container.querySelectorAll("[data-steril-subview]").forEach(b =>
    b.addEventListener("click", () => { sterilSubview = b.dataset.sterilSubview; loadSterilizationModule(); }));
}

async function loadLibrarySubview() {
  if (!modulePanel) return;
  const token = ++sterilToken;
  modulePanel.innerHTML = `<div class="module-loading">器械库载入中...</div>`;
  let cats, instr;
  try {
    const catRes = await fetch("/api/instrument-categories");
    const filterQuery = sterilCategoryFilter ? `category_id=${encodeURIComponent(sterilCategoryFilter)}` : "";
    const insRes = await fetch(`/api/instruments?${filterQuery}`);
    if (token !== sterilToken) return;
    cats = catRes.ok ? await catRes.json() : {categories: []};
    instr = insRes.ok ? await insRes.json() : {instruments: []};
    if (token !== sterilToken) return;
  } catch {
    if (token !== sterilToken) return;
    modulePanel.innerHTML = `<div class="module-loading">器械库载入失败</div>`;
    return;
  }
  sterilCategories = cats.categories || [];
  const rows = instr.instruments || [];
  modulePanel.innerHTML = `
    ${sterilSubTabs("library")}
    <div class="module-head">
      <strong>器械库</strong>
      <span>${escapeHtml(formatCount(instr.totalcount))} 件</span>
    </div>
    ${sterilCategoryBar()}
    ${sterilInstrumentForm()}
    <div class="module-table">
      <div class="module-row module-row-head">
        <span>编号</span><span>名称</span><span>分类</span><span>规格</span><span>状态</span><span>操作</span>
      </div>
      ${rows.map(sterilRow).join("") || '<div class="module-empty">暂无器械</div>'}
    </div>
  `;
  bindSterilSubTabs(modulePanel);
  bindSterilization(modulePanel);
}

function sterilCategoryBar() {
  const opts = [`<option value="">全部分类</option>`].concat(
    sterilCategories.map(c => `<option value="${escapeAttr(c.id)}"${c.id === sterilCategoryFilter ? " selected" : ""}>${escapeHtml(c.name)}</option>`));
  // 选中具体分类时才出现 改名/删除（针对该分类）
  const catOps = sterilCategoryFilter
    ? `<button type="button" class="plain-button" data-steril-rename-cat="1">改名</button>` +
      `<button type="button" class="plain-button" data-steril-del-cat="1">删除</button>`
    : "";
  return `
    <div class="steril-cat-bar">
      <label>分类筛选 <select data-steril-cat-filter>${opts.join("")}</select></label>
      <button type="button" class="plain-button" data-steril-add-cat="1">新增分类</button>
      ${catOps}
    </div>`;
}

function sterilCategoryOptions(selectedId) {
  return [`<option value="">请选择分类</option>`].concat(
    sterilCategories.map(c => `<option value="${escapeAttr(c.id)}"${c.id === selectedId ? " selected" : ""}>${escapeHtml(c.name)}</option>`)).join("");
}

function sterilInstrumentForm() {
  return `
    <div class="steril-form" data-steril-form>
      <strong>${sterilEditingId ? "编辑器械" : "新增器械"}</strong>
      <input class="ord-input" data-steril-f="code" placeholder="编号*" value="${escapeAttr(sterilFormCache.code)}">
      <input class="ord-input" data-steril-f="name" placeholder="名称*" value="${escapeAttr(sterilFormCache.name)}">
      <select class="ord-input" data-steril-f="category_id">${sterilCategoryOptions(sterilFormCache.category_id)}</select>
      <input class="ord-input" data-steril-f="spec" placeholder="规格" value="${escapeAttr(sterilFormCache.spec)}">
      <input class="ord-input" data-steril-f="note" placeholder="备注" value="${escapeAttr(sterilFormCache.note)}">
      <button type="button" class="tooth-confirm-btn" data-steril-save="1">${sterilEditingId ? "保存修改" : "新增"}</button>
      ${sterilEditingId ? `<button type="button" class="plain-button" data-steril-cancel-edit="1">取消编辑</button>` : ""}
      <span data-steril-status></span>
    </div>`;
}

function sterilRow(r) {
  const statusLabel = r.status === "disabled" ? "停用" : "启用";
  const disableBtn = r.status === "disabled" ? "" : `<button type="button" class="plain-button" data-steril-disable="${escapeAttr(r.id)}">停用</button>`;
  return `
    <div class="module-row">
      <span>${escapeHtml(r.code || "")}</span>
      <span>${escapeHtml(r.name || "")}</span>
      <span>${escapeHtml(r.category_name || "")}</span>
      <span>${escapeHtml(r.spec || "")}</span>
      <span class="steril-status steril-${escapeAttr(r.status || "active")}">${statusLabel}</span>
      <span class="steril-actions">
        <button type="button" class="plain-button" data-steril-edit="${escapeAttr(r.id)}">编辑</button>
        ${disableBtn}
        <button type="button" class="plain-button steril-del" data-steril-del="${escapeAttr(r.id)}">删除</button>
      </span>
    </div>`;
}

function sterilReadForm() {
  const get = f => {
    const el = document.querySelector(`[data-steril-f="${f}"]`);
    return el ? el.value.trim() : "";
  };
  return {code: get("code"), name: get("name"), category_id: get("category_id"), spec: get("spec"), note: get("note")};
}

function bindSterilization(container) {
  if (!container) return;
  const filter = container.querySelector("[data-steril-cat-filter]");
  if (filter) filter.addEventListener("change", () => { sterilCategoryFilter = filter.value; loadSterilizationModule(); });
  const addCat = container.querySelector("[data-steril-add-cat]");
  if (addCat) addCat.addEventListener("click", addSterilCategory);
  const renameCat = container.querySelector("[data-steril-rename-cat]");
  if (renameCat) renameCat.addEventListener("click", renameSterilCategory);
  const delCat = container.querySelector("[data-steril-del-cat]");
  if (delCat) delCat.addEventListener("click", deleteSterilCategory);
  const save = container.querySelector("[data-steril-save]");
  if (save) save.addEventListener("click", saveSterilInstrument);
  const cancelEdit = container.querySelector("[data-steril-cancel-edit]");
  if (cancelEdit) cancelEdit.addEventListener("click", () => { sterilEditingId = null; sterilFormCache = {code: "", name: "", category_id: "", spec: "", note: ""}; loadSterilizationModule(); });
  container.querySelectorAll("[data-steril-edit]").forEach(b => b.addEventListener("click", () => startEditSteril(b.dataset.sterilEdit)));
  container.querySelectorAll("[data-steril-disable]").forEach(b => b.addEventListener("click", () => disableSteril(b.dataset.sterilDisable)));
  container.querySelectorAll("[data-steril-del]").forEach(b => b.addEventListener("click", () => deleteSteril(b.dataset.sterilDel)));
}

async function addSterilCategory() {
  const name = await appPrompt("新增器械分类名称：", "");
  if (name === null) return;
  if (!name.trim()) { window.alert("分类名称不能为空"); return; }
  try {
    const res = await fetch("/api/instrument-categories", {method: "POST", headers: {"Content-Type": "application/json"}, body: JSON.stringify({name: name.trim()})});
    if (!res.ok) { const m = await res.json().catch(() => ({})); window.alert("新增分类失败：" + (m.detail || res.status)); return; }
  } catch { window.alert("新增分类失败（网络异常）"); return; }
  loadSterilizationModule();
}

async function renameSterilCategory() {
  const cat = sterilCategories.find(c => c.id === sterilCategoryFilter);
  if (!cat) return;
  const name = await appPrompt("修改分类名称：", cat.name);
  if (name === null) return;
  if (!name.trim()) { window.alert("分类名称不能为空"); return; }
  try {
    const res = await fetch(`/api/instrument-categories/${encodeURIComponent(cat.id)}`, {method: "PUT", headers: {"Content-Type": "application/json"}, body: JSON.stringify({name: name.trim()})});
    if (!res.ok) { const m = await res.json().catch(() => ({})); window.alert("改名失败：" + (m.detail || res.status)); return; }
  } catch { window.alert("改名失败（网络异常）"); return; }
  loadSterilizationModule();
}

async function deleteSterilCategory() {
  const cat = sterilCategories.find(c => c.id === sterilCategoryFilter);
  if (!cat) return;
  // 分类必填：删前先确认该分类下没有器械，否则会留下无分类的孤儿器械
  try {
    const data = await (await fetch(`/api/instruments?category_id=${encodeURIComponent(cat.id)}`)).json();
    const n = data.totalcount != null ? data.totalcount : (data.instruments || []).length;
    if (n > 0) { window.alert(`该分类下还有 ${n} 件器械，请先把它们改到其他分类再删除。`); return; }
  } catch { window.alert("校验分类失败（网络异常）"); return; }
  if (!window.confirm(`确认删除分类「${cat.name}」？`)) return;
  try {
    const res = await fetch(`/api/instrument-categories/${encodeURIComponent(cat.id)}`, {method: "DELETE"});
    if (!res.ok) { const m = await res.json().catch(() => ({})); window.alert("删除失败：" + (m.detail || res.status)); return; }
  } catch { window.alert("删除失败（网络异常）"); return; }
  sterilCategoryFilter = "";  // 删后回到「全部分类」
  loadSterilizationModule();
}

function startEditSteril(id) {
  // 从当前已渲染数据回填表单（再发请求拿单条也可，这里读列表行避免多余请求）
  fetch(`/api/instruments?`).then(r => r.json()).then(data => {
    const r = (data.instruments || []).find(x => x.id === id);
    if (!r) return;
    sterilEditingId = id;
    sterilFormCache = {code: r.code || "", name: r.name || "", category_id: r.category_id || "", spec: r.spec || "", note: r.note || ""};
    loadSterilizationModule();
  });
}

async function saveSterilInstrument() {
  const status = document.querySelector("[data-steril-status]");
  const form = sterilReadForm();
  if (!form.code || !form.name) { if (status) status.textContent = "编号和名称必填"; return; }
  if (!form.category_id) { if (status) status.textContent = "请选择器械分类"; return; }
  const url = sterilEditingId ? `/api/instruments/${encodeURIComponent(sterilEditingId)}` : "/api/instruments";
  const method = sterilEditingId ? "PUT" : "POST";
  if (status) status.textContent = "保存中...";
  let res;
  try { res = await fetch(url, {method, headers: {"Content-Type": "application/json"}, body: JSON.stringify(form)}); }
  catch { if (status) status.textContent = "保存失败（网络异常）"; return; }
  if (!res.ok) { const m = await res.json().catch(() => ({})); if (status) status.textContent = "保存失败：" + (m.detail || res.status); return; }
  sterilEditingId = null;
  sterilFormCache = {code: "", name: "", category_id: "", spec: "", note: ""};
  loadSterilizationModule();
}

async function disableSteril(id) {
  if (!window.confirm("停用该器械？停用后不可再被新单据选用，历史追溯保留。")) return;
  try {
    const res = await fetch(`/api/instruments/${encodeURIComponent(id)}/disable`, {method: "PUT"});
    if (!res.ok) { const m = await res.json().catch(() => ({})); window.alert("停用失败：" + (m.detail || res.status)); return; }
  } catch { window.alert("停用失败（网络异常）"); return; }
  loadSterilizationModule();
}

async function deleteSteril(id) {
  if (!window.confirm("删除该器械？（软删，历史单据引用仍可追溯）")) return;
  try {
    const res = await fetch(`/api/instruments/${encodeURIComponent(id)}`, {method: "DELETE"});
    if (!res.ok) { const m = await res.json().catch(() => ({})); window.alert("删除失败：" + (m.detail || res.status)); return; }
  } catch { window.alert("删除失败（网络异常）"); return; }
  loadSterilizationModule();
}

Object.assign(window, {
  loadSterilizationModule, loadLibrarySubview, sterilSubTabs, bindSterilSubTabs,
  bindSterilization, saveSterilInstrument,
  startEditSteril, disableSteril, deleteSteril, addSterilCategory,
});
