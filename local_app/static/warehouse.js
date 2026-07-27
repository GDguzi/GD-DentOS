// 库存管理（进销存）前端：读视图(库存/物品/供应商/单据) + 写表单(物品建档/供应商/出入库录入与确认)。
// 后端已完整(warehouse_*)，本文件纯对接；渲染进共享 modulePanel。
// 安全：弹框/行操作一律走 data-* + 事件委托，不拼内联 JS(防注入)。

let _whTab = "stock";

const WH_IN_TYPES = [["purchase", "进货"], ["return", "退领"]];
const WH_OUT_TYPES = [["use", "领用"], ["return_supplier", "退货"]];
const WH_STATUS_CN = {draft: "草稿", confirmed: "已确认", cancelled: "已作废"};

function loadWarehouseModule() {
  const modulePanel = document.getElementById("modulePanel");
  if (!modulePanel) return;
  modulePanel.innerHTML = `
    <div class="module-subtabs wh-subtabs">
      <button type="button" class="module-subtab" data-wh-tab="stock">库存查询</button>
      <button type="button" class="module-subtab" data-wh-tab="items">物品档案</button>
      <button type="button" class="module-subtab" data-wh-tab="suppliers">供应商</button>
      <button type="button" class="module-subtab" data-wh-tab="receipts">出入库/盘点单</button>
    </div>
    <div id="whBody" class="wh-body"><div class="module-loading">载入中...</div></div>
  `;
  modulePanel.querySelectorAll("[data-wh-tab]").forEach(btn =>
    btn.addEventListener("click", () => { _whTab = btn.dataset.whTab; renderWhTabs(modulePanel); loadWhTab(); }));
  renderWhTabs(modulePanel);
  loadWhTab();
}

function renderWhTabs(root) {
  root.querySelectorAll("[data-wh-tab]").forEach(b =>
    b.classList.toggle("active", b.dataset.whTab === _whTab));
}

async function loadWhTab() {
  const body = document.getElementById("whBody");
  if (!body) return;
  body.innerHTML = `<div class="module-loading">载入中...</div>`;
  try {
    if (_whTab === "stock") return await renderWhStock(body);
    if (_whTab === "items") return await renderWhItems(body);
    if (_whTab === "suppliers") return await renderWhSuppliers(body);
    if (_whTab === "receipts") return await renderWhReceipts(body);
  } catch {
    body.innerHTML = `<div class="module-loading">载入失败，请稍后重试</div>`;
  }
}

// 通用只读表格：columns=[{key,label,money?}]，rowClass(row)→可选行类(低库存标红)
function whTable(rows, columns, rowClass) {
  if (!rows || !rows.length) return `<div class="wh-empty">暂无数据</div>`;
  const head = columns.map(c => `<th>${escapeHtml(c.label)}</th>`).join("");
  const body = rows.map(r => {
    const cls = (typeof rowClass === "function" && rowClass(r)) ? ` class="${rowClass(r)}"` : "";
    const tds = columns.map(c => {
      if (c.render) return `<td>${c.render(r)}</td>`;
      let v = r[c.key];
      v = (v === null || v === undefined) ? "" : v;
      if (c.money) v = formatMoney(v);
      return `<td>${escapeHtml(String(v))}</td>`;
    }).join("");
    return `<tr${cls}>${tds}</tr>`;
  }).join("");
  return `<table class="wh-table"><thead><tr>${head}</tr></thead><tbody>${body}</tbody></table>`;
}

// ---------- 弹框 ----------
function whCloseModal() {
  const m = document.getElementById("whModal");
  if (m) m.remove();
}

// title 标题, inner 表单 HTML, onSubmit 回调(返回 true 关闭)
function whOpenModal(title, inner, onSubmit) {
  whCloseModal();
  const wrap = document.createElement("div");
  wrap.id = "whModal";
  wrap.className = "wh-modal-overlay";
  wrap.innerHTML = `
    <div class="wh-modal" role="dialog" aria-modal="true" aria-label="${escapeAttr(title)}">
      <div class="wh-modal-head"><strong>${escapeHtml(title)}</strong>
        <button type="button" class="plain-button" data-wh-close>×</button></div>
      <div class="wh-modal-body">${inner}</div>
      <div class="wh-modal-foot">
        <span class="wh-modal-status" data-wh-status></span>
        <button type="button" class="plain-button" data-wh-close>取消</button>
        <button type="button" class="wh-modal-ok" data-wh-ok>保存</button>
      </div>
    </div>`;
  document.body.appendChild(wrap);
  if (typeof bindStaffInputs === "function") bindStaffInputs(wrap);   // 二批:经手人/领用人选人
  wrap.querySelectorAll("[data-wh-close]").forEach(b => b.addEventListener("click", whCloseModal));
  wrap.addEventListener("click", e => { if (e.target === wrap) whCloseModal(); });
  wrap.querySelector("[data-wh-ok]").addEventListener("click", async () => {
    const status = wrap.querySelector("[data-wh-status]");
    if (status) status.textContent = "保存中...";
    try {
      const ok = await onSubmit(wrap, status);
      if (ok) { whCloseModal(); loadWhTab(); }
    } catch (err) {
      if (status) status.textContent = "保存失败";
    }
  });
  return wrap;
}

function whVal(wrap, name) {
  const el = wrap.querySelector(`[data-f="${name}"]`);
  return el ? el.value.trim() : "";
}

async function whPost(url, payload, method) {
  const res = await fetch(url, {
    method: method || "POST",
    headers: {"Content-Type": "application/json"},
    body: JSON.stringify(payload),
  });
  if (!res.ok) {
    const m = await res.json().catch(() => ({}));
    throw new Error(m.detail || ("HTTP " + res.status));
  }
  return res.json().catch(() => ({}));
}

// ---------- 库存查询 ----------
async function renderWhStock(body) {
  const [balRes, alertRes] = await Promise.all([
    fetch("/api/stock-balance").then(r => r.json()).catch(() => ({items: []})),
    fetch("/api/stock-balance/alerts").then(r => r.json()).catch(() => ({alerts: []})),
  ]);
  const items = balRes.items || [];
  const alerts = alertRes.alerts || [];
  const low = r => (Number(r.min_qty) > 0 && Number(r.qty) <= Number(r.min_qty)) ? "wh-row-low" : "";
  const cols = [
    {key: "code", label: "编码"}, {key: "name", label: "品名"},
    {key: "category_name", label: "分类"}, {key: "unit", label: "单位"},
    {key: "qty", label: "库存"}, {key: "min_qty", label: "下限"}, {key: "max_qty", label: "上限"},
  ];
  body.innerHTML = `
    <div class="wh-alert-bar${alerts.length ? " on" : ""}">低库存预警：${alerts.length} 项${alerts.length ? "（下表标红）" : ""}</div>
    ${whTable(items, cols, low)}`;
}

// ---------- 物品档案（建档/编辑/删除） ----------
async function renderWhItems(body) {
  const [d, catD] = await Promise.all([
    fetch("/api/stock-items").then(r => r.json()).catch(() => ({items: []})),
    fetch("/api/stock-categories").then(r => r.json()).catch(() => ({categories: []})),
  ]);
  const cats = catD.categories || [];
  const cols = [
    {key: "code", label: "编码"}, {key: "name", label: "品名"}, {key: "category_name", label: "分类"},
    {key: "spec", label: "规格"}, {key: "unit", label: "单位"}, {key: "brand", label: "品牌"},
    {key: "price", label: "单价", money: true},
    {label: "操作", render: r => `<a class="wh-link" data-wh-item-edit="${escapeAttr(r.id)}">编辑</a> <a class="wh-link wh-link-danger" data-wh-item-del="${escapeAttr(r.id)}">删除</a>`},
  ];
  body.innerHTML = `
    <div class="wh-toolbar"><button type="button" class="wh-add-btn" data-wh-add-item>+ 新增物品</button></div>
    ${whTable(d.items || [], cols)}`;
  body.querySelector("[data-wh-add-item]").addEventListener("click", () => whItemForm(cats, null));
  body.querySelectorAll("[data-wh-item-edit]").forEach(a => a.addEventListener("click", () => {
    const row = (d.items || []).find(x => x.id === a.dataset.whItemEdit);
    if (row) whItemForm(cats, row);
  }));
  body.querySelectorAll("[data-wh-item-del]").forEach(a => a.addEventListener("click", () => whDeleteItem(a.dataset.whItemDel)));
}

function whItemForm(cats, row) {
  const catOpts = cats.map(c => `<option value="${escapeAttr(c.id)}"${row && row.category_id === c.id ? " selected" : ""}>${escapeHtml(c.name)}</option>`).join("");
  const v = (k) => row ? escapeAttr(row[k] == null ? "" : String(row[k])) : "";
  const inner = `
    <div class="wh-form-grid">
      <label>编码*<input data-f="code" value="${v("code")}"></label>
      <label>品名*<input data-f="name" value="${v("name")}"></label>
      <label>分类<select data-f="category_id"><option value="">无</option>${catOpts}</select></label>
      <label>规格<input data-f="spec" value="${v("spec")}"></label>
      <label>单位<input data-f="unit" value="${v("unit")}"></label>
      <label>品牌<input data-f="brand" value="${v("brand")}"></label>
      <label>单价<input data-f="price" type="number" step="0.01" value="${v("price")}"></label>
      <label>库存下限<input data-f="min_qty" type="number" step="0.01" value="${v("min_qty")}"></label>
      <label>库存上限<input data-f="max_qty" type="number" step="0.01" value="${v("max_qty")}"></label>
    </div>`;
  whOpenModal(row ? "编辑物品" : "新增物品", inner, async (wrap, status) => {
    const payload = {
      code: whVal(wrap, "code"), name: whVal(wrap, "name"),
      category_id: whVal(wrap, "category_id"), spec: whVal(wrap, "spec"),
      unit: whVal(wrap, "unit"), brand: whVal(wrap, "brand"),
      price: whVal(wrap, "price"), min_qty: whVal(wrap, "min_qty"), max_qty: whVal(wrap, "max_qty"),
    };
    if (!payload.code || !payload.name) { status.textContent = "编码和品名必填"; return false; }
    try {
      if (row) await whPost(`/api/stock-items/${encodeURIComponent(row.id)}`, payload, "PUT");
      else await whPost("/api/stock-items", payload);
      return true;
    } catch (e) { status.textContent = String(e.message || "保存失败"); return false; }
  });
}

async function whDeleteItem(id) {
  if (!id || !window.confirm("删除该物品？")) return;
  try { await whPost(`/api/stock-items/${encodeURIComponent(id)}`, {}, "DELETE"); loadWhTab(); }
  catch (e) { window.alert("删除失败：" + (e.message || "")); }
}

// ---------- 供应商（建档/编辑/删除） ----------
async function renderWhSuppliers(body) {
  const d = await fetch("/api/suppliers").then(r => r.json()).catch(() => ({suppliers: []}));
  const cols = [
    {key: "name", label: "供应商"}, {key: "contact", label: "联系人"},
    {key: "phone", label: "电话"}, {key: "note", label: "备注"},
    {label: "操作", render: r => `<a class="wh-link" data-wh-sup-edit="${escapeAttr(r.id)}">编辑</a> <a class="wh-link wh-link-danger" data-wh-sup-del="${escapeAttr(r.id)}">删除</a>`},
  ];
  body.innerHTML = `
    <div class="wh-toolbar"><button type="button" class="wh-add-btn" data-wh-add-sup>+ 新增供应商</button></div>
    ${whTable(d.suppliers || [], cols)}`;
  body.querySelector("[data-wh-add-sup]").addEventListener("click", () => whSupplierForm(null));
  body.querySelectorAll("[data-wh-sup-edit]").forEach(a => a.addEventListener("click", () => {
    const row = (d.suppliers || []).find(x => x.id === a.dataset.whSupEdit);
    if (row) whSupplierForm(row);
  }));
  body.querySelectorAll("[data-wh-sup-del]").forEach(a => a.addEventListener("click", () => whDeleteSupplier(a.dataset.whSupDel)));
}

function whSupplierForm(row) {
  const v = (k) => row ? escapeAttr(row[k] == null ? "" : String(row[k])) : "";
  const inner = `
    <div class="wh-form-grid">
      <label>名称*<input data-f="name" value="${v("name")}"></label>
      <label>联系人<input data-f="contact" value="${v("contact")}"></label>
      <label>电话<input data-f="phone" value="${v("phone")}"></label>
      <label class="wide">备注<input data-f="note" value="${v("note")}"></label>
    </div>`;
  whOpenModal(row ? "编辑供应商" : "新增供应商", inner, async (wrap, status) => {
    const payload = {name: whVal(wrap, "name"), contact: whVal(wrap, "contact"), phone: whVal(wrap, "phone"), note: whVal(wrap, "note")};
    if (!payload.name) { status.textContent = "供应商名称必填"; return false; }
    try {
      if (row) await whPost(`/api/suppliers/${encodeURIComponent(row.id)}`, payload, "PUT");
      else await whPost("/api/suppliers", payload);
      return true;
    } catch (e) { status.textContent = String(e.message || "保存失败"); return false; }
  });
}

async function whDeleteSupplier(id) {
  if (!id || !window.confirm("删除该供应商？")) return;
  try { await whPost(`/api/suppliers/${encodeURIComponent(id)}`, {}, "DELETE"); loadWhTab(); }
  catch (e) { window.alert("删除失败：" + (e.message || "")); }
}

// ---------- 出入库/盘点单（列表 + 新建 + 确认） ----------
async function renderWhReceipts(body) {
  const [ins, outs, takes] = await Promise.all([
    fetch("/api/stock-in").then(r => r.json()).catch(() => ({})),
    fetch("/api/stock-out").then(r => r.json()).catch(() => ({})),
    fetch("/api/stock-take").then(r => r.json()).catch(() => ({})),
  ]);
  const statusCN = s => WH_STATUS_CN[s] || s || "";
  const mkCols = (kind) => [
    {key: "doc_no", label: "单号"}, {key: "type", label: "类型"}, {key: "status_cn", label: "状态"},
    {key: "operator", label: "经手人"}, {key: "created_at", label: "创建时间"},
    {label: "操作", render: r => r.status === "draft" ? `<a class="wh-link" data-wh-confirm="${escapeAttr(kind)}:${escapeAttr(r.id)}">确认${kind === "in" ? "入库" : kind === "out" ? "出库" : "盘点"}</a>` : ""},
  ];
  const prep = rows => (rows || []).map(r => ({
    ...r, doc_no: r.in_no || r.out_no || r.take_no || "", status_cn: statusCN(r.status), type: r.type || "",
  }));
  body.innerHTML = `
    <div class="wh-toolbar">
      <button type="button" class="wh-add-btn" data-wh-new-in>+ 新建入库单</button>
      <button type="button" class="wh-add-btn" data-wh-new-out>+ 新建出库单</button>
    </div>
    <h4 class="wh-sec">入库单</h4>${whTable(prep(ins.stock_in || []), mkCols("in"))}
    <h4 class="wh-sec">出库单</h4>${whTable(prep(outs.stock_out || []), mkCols("out"))}
    <h4 class="wh-sec">盘点单</h4>${whTable(prep(takes.stock_take || []), mkCols("take"))}`;
  body.querySelector("[data-wh-new-in]").addEventListener("click", () => whReceiptForm("in"));
  body.querySelector("[data-wh-new-out]").addEventListener("click", () => whReceiptForm("out"));
  body.querySelectorAll("[data-wh-confirm]").forEach(a => a.addEventListener("click", () => {
    const [kind, id] = a.dataset.whConfirm.split(":");
    whConfirmReceipt(kind, id);
  }));
}

async function whConfirmReceipt(kind, id) {
  const path = kind === "in" ? "stock-in" : kind === "out" ? "stock-out" : "stock-take";
  const label = kind === "in" ? "入库" : kind === "out" ? "出库" : "盘点";
  if (!window.confirm(`确认${label}？确认后库存即变动，不可撤销。`)) return;
  try { await whPost(`/api/${path}/${encodeURIComponent(id)}/confirm`, {}); loadWhTab(); }
  catch (e) { window.alert(`确认${label}失败：` + (e.message || "")); }
}

// 入库/出库单录入：表头(类型/供应商/经手人[/领用人]) + 动态明细行
let _whRowSeq = 0;
async function whReceiptForm(kind) {
  const isIn = kind === "in";
  const [itemD, supD] = await Promise.all([
    fetch("/api/stock-items").then(r => r.json()).catch(() => ({items: []})),
    fetch("/api/suppliers").then(r => r.json()).catch(() => ({suppliers: []})),
  ]);
  const items = itemD.items || [];
  const suppliers = supD.suppliers || [];
  const typeOpts = (isIn ? WH_IN_TYPES : WH_OUT_TYPES).map(([v, l]) => `<option value="${v}">${escapeHtml(l)}</option>`).join("");
  const supOpts = `<option value="">选择供应商</option>` + suppliers.map(s => `<option value="${escapeAttr(s.id)}">${escapeHtml(s.name)}</option>`).join("");
  const inner = `
    <div class="wh-form-grid">
      <label>类型*<select data-f="type">${typeOpts}</select></label>
      <label>供应商<span class="wh-field-hint">（进货/退货必填）</span><select data-f="supplier_id">${supOpts}</select></label>
      <label>经手人<input data-f="operator" data-staff-role="*"></label>
      ${isIn ? "" : `<label>领用人<input data-f="receiver" data-staff-role="*"></label>`}
    </div>
    <div class="wh-items-head"><span>明细</span><button type="button" class="wh-add-row" data-wh-add-row>+ 加一行</button></div>
    <div class="wh-items" data-wh-items></div>`;
  const wrap = whOpenModal(isIn ? "新建入库单" : "新建出库单", inner, async (w, status) => {
    const rows = Array.from(w.querySelectorAll("[data-wh-row]"));
    const its = rows.map(r => {
      const o = {stock_item_id: r.querySelector('[data-rf="stock_item_id"]').value, qty: r.querySelector('[data-rf="qty"]').value.trim()};
      if (isIn) {
        o.unit_price = r.querySelector('[data-rf="unit_price"]').value.trim();
        o.batch_no = r.querySelector('[data-rf="batch_no"]').value.trim();
        o.expire_date = r.querySelector('[data-rf="expire_date"]').value.trim();
      }
      return o;
    }).filter(o => o.stock_item_id && Number(o.qty) > 0);
    // 供应商仅 进货(purchase)/退货(return_supplier) 必填；领用(use)/退领(return) 可空(诊室领用最高频)
    const needSupplier = ["purchase", "return_supplier"].includes(whVal(w, "type"));
    if (needSupplier && !whVal(w, "supplier_id")) { status.textContent = "进货/退货需选择供应商"; return false; }
    if (!its.length) { status.textContent = "至少录一条有效明细(选物品+数量>0)"; return false; }
    const payload = {type: whVal(w, "type"), supplier_id: whVal(w, "supplier_id"), operator: whVal(w, "operator"), items: its};
    if (!isIn) payload.receiver = whVal(w, "receiver");
    try { await whPost(isIn ? "/api/stock-in" : "/api/stock-out", payload); return true; }
    catch (e) { status.textContent = String(e.message || "保存失败"); return false; }
  });
  const itemsBox = wrap.querySelector("[data-wh-items]");
  const addRow = () => {
    _whRowSeq += 1;
    const opts = `<option value="">选择物品</option>` + items.map(i => `<option value="${escapeAttr(i.id)}">${escapeHtml(i.code + " " + i.name)}</option>`).join("");
    const div = document.createElement("div");
    div.className = "wh-item-row";
    div.setAttribute("data-wh-row", String(_whRowSeq));
    div.innerHTML = `
      <select data-rf="stock_item_id">${opts}</select>
      <input data-rf="qty" type="number" step="0.01" placeholder="数量" class="wh-qty">
      ${isIn ? `<input data-rf="unit_price" type="number" step="0.01" placeholder="单价" class="wh-qty">
                <input data-rf="batch_no" placeholder="批号" class="wh-batch">
                <input data-rf="expire_date" type="date" class="wh-exp">` : ""}
      <button type="button" class="wh-del-row" data-wh-del-row>×</button>`;
    div.querySelector("[data-wh-del-row]").addEventListener("click", () => div.remove());
    itemsBox.appendChild(div);
  };
  wrap.querySelector("[data-wh-add-row]").addEventListener("click", addRow);
  addRow();
}

Object.assign(window, {loadWarehouseModule});
