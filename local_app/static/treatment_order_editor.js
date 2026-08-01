// 处置单两步工作流前端：
//   新增处置(只录项目+牙位，待划价) → 划价(逐项收费/免费/打折+整单优惠，待收费) → 收费/撤销。
// 复用 openHandlePicker(处置选择器) + openToothSelectorWith(FDI牙位)。

const ORDER_STATUS_LABEL = {recorded: "待划价", priced: "待收费", paid: "已收费", voided: "已撤销", refunded: "已退费"};
const ORDER_STATUS_CLASS = {recorded: "st-recorded", priced: "st-priced", paid: "st-paid", voided: "st-voided", refunded: "st-refunded"};

// 处置→病情诊断 联想（与病种联想检查 诊断→检查 互为反向）。
// 处置名里命中关键词 → 推荐对应常见病情诊断。
const HANDLE_DG_HINTS = [
  {kw: ["根管", "开髓", "拔髓", "盖髓", "充根", "牙髓", "干髓"], dg: "牙髓炎/根尖周炎"},
  {kw: ["充填", "树脂", "补牙", "玻璃离子", "玻璃离", "嵌体"], dg: "龋齿"},
  {kw: ["洁治", "洁牙", "龈上", "龈下", "刮治", "喷砂"], dg: "牙龈炎/牙周炎"},
  {kw: ["拔除", "拔牙", "微创拔", "残根", "阻生"], dg: "残根/阻生齿"},
  {kw: ["种植"], dg: "牙列缺损/牙缺失"},
  {kw: ["全冠", "烤瓷", "全瓷", "桩核", "贴面", "冠修复"], dg: "牙体缺损"},
  {kw: ["矫正", "正畸", "托槽", "保持器"], dg: "错颌畸形"},
  {kw: ["脱敏", "楔状"], dg: "楔状缺损/牙本质过敏"},
  {kw: ["窝沟封闭", "封闭", "涂氟", "防龋"], dg: "预防保健"},
];

let orderModel = null;
let orderRowSeq = 0;

function newOrderRow() {
  orderRowSeq += 1;
  return {rowid: "ord-" + orderRowSeq, handle_id: "", item_name: "", item_code: "",
          tooth: "", unit_price: "", quantity: 1, fee_type: ""};
}
function freshOrderModel() {
  // 配台默认带出上次选择(记住上次)；离职/停用人员不带入新单
  let last = (typeof lastTeam === "function") ? lastTeam() : {};
  if (typeof activeTeam === "function") last = activeTeam(last);
  return {
    doctor_name: last.doctor_name || "", nurse_name: last.nurse_name || "",
    consultant_name: last.consultant_name || "", assistant_name: last.assistant_name || "",
    diagnosis: "", rows: [newOrderRow()],
  };
}

// 处置配台字段 ↔ 角色（下拉用）
const ORDER_TEAM_FIELDS = [
  ["doctor_name", "医生"], ["nurse_name", "护士"],
  ["consultant_name", "咨询师"], ["assistant_name", "助理"],
];

let _orderTodayVisit = null;   // 本次开处置时该患者今日挂号状态(先挂号后处置)

// ---------- 新增处置（只录，不定价） ----------
// editOrder 传入时=编辑既有待划价单(预填项目),否则新建。
async function openTreatmentOrder(editOrder) {
  if (!workspacePatientId) return;
  const m = document.getElementById("treatmentOrderModal");
  if (!m) return;
  if (typeof ensureStaff === "function") await ensureStaff();  // 先载人员库；freshOrderModel据此过滤离职配台
  const pid = workspacePatientId;
  _orderTodayVisit = null;
  try {
    const r = await fetch(`/api/patients/${encodeURIComponent(pid)}/today-visit`);
    if (pid !== workspacePatientId) return;   // 竞态守卫：已换患者
    if (r.ok) _orderTodayVisit = await r.json();
  } catch { _orderTodayVisit = null; }
  if (pid !== workspacePatientId) return;
  // 编辑既有单标题应为「编辑处置」,不再笼统显示「新增处置」
  const title = document.getElementById("treatmentOrderTitle");
  if (title) title.textContent = editOrder ? "编辑处置" : "新增处置";
  if (editOrder) {
    orderModel = orderModelFromOrder(editOrder);   // 编辑:预填现有项目,原地改不另起新单
  } else {
    orderModel = freshOrderModel();
    // 处置医生默认 = 今日挂号医生(优先于"上次配台")
    if (_orderTodayVisit && _orderTodayVisit.has_today && _orderTodayVisit.doctor_name) {
      orderModel.doctor_name = _orderTodayVisit.doctor_name;
    }
  }
  renderTreatmentOrderEditor();
  m.hidden = false;
}

// 把既有待划价单转成编辑器模型(带 _editId 标记走 PUT 更新)。
function orderModelFromOrder(o) {
  const base = freshOrderModel();
  base._editId = o.order_id;
  base.doctor_name = o.doctor_name || "";
  base.nurse_name = o.nurse_name || "";
  base.consultant_name = o.consultant_name || "";
  base.assistant_name = o.assistant_name || "";
  base.diagnosis = o.diagnosis || "";
  const rows = (o.items || []).map(it => {
    orderRowSeq += 1;
    return {rowid: "ord-" + orderRowSeq, handle_id: it.item_code || "", item_name: it.item_name || "",
            item_code: it.item_code || "", tooth: it.tooth || "",
            unit_price: it.unit_price != null ? it.unit_price : "", quantity: it.quantity || 1,
            fee_type: it.fee_type || ""};
  });
  base.rows = rows.length ? rows : [newOrderRow()];
  return base;
}

// 处置 tab 卡片「编辑」入口:从缓存取该单 → 打开编辑器预填。
function editLocalOrder(orderId) {
  const o = (localOrdersCache || []).find(x => x.order_id === orderId);
  if (o) openTreatmentOrder(o);
}
function closeTreatmentOrder() {
  const m = document.getElementById("treatmentOrderModal");
  if (m) m.hidden = true;
  orderModel = null;
}
function orderEditorEl() { return document.getElementById("treatmentOrderBody"); }

function renderOrderRow(ri, row) {
  const teeth = String(row.tooth || "").split(/[\s,，]+/).filter(Boolean);
  return `
    <div class="ord-row" data-ri="${ri}">
      <button type="button" class="ord-tooth plain-button" onclick="pickOrderTooth(${ri})" title="选择牙位">
        ${teeth.length ? toothCrossHtml(teeth) : '<span class="ord-tooth-empty">选牙位</span>'}
      </button>
      <button type="button" class="ord-handle plain-button" onclick="pickOrderHandle(${ri})">${row.item_name ? escapeHtml(row.item_name) : "选择处置"}</button>
      <input class="ord-input ord-price" data-field="unit_price" value="${escapeAttr(row.unit_price)}" placeholder="参考单价" inputmode="decimal">
      <input class="ord-input ord-qty" data-field="quantity" value="${escapeAttr(row.quantity)}" inputmode="numeric">
      <button type="button" class="plain-button ord-del" onclick="removeOrderRow(${ri})">✕</button>
    </div>`;
}

function renderTreatmentOrderEditor() {
  const box = orderEditorEl();
  if (!box || !orderModel) return;
  // 先挂号后处置：今天没挂号时给「挂号并继续」引导(选医生→建今日挂号→默认带入)
  const noReg = _orderTodayVisit && _orderTodayVisit.has_today === false;
  const regBanner = noReg ? `
    <div class="ord-reg-banner">
      <span>⚠ 该患者今天还没挂号。</span>
      <select class="ord-input" data-reg-doctor>${
        (typeof staffOptionsHtml === "function") ? staffOptionsHtml("医生", "") : '<option value="">选医生</option>'
      }</select>
      <button type="button" class="tooth-confirm-btn" onclick="registerTodayThenOrder()">挂号并继续</button>
    </div>` : "";
  box.innerHTML = `
    ${regBanner}
    <div class="ord-meta">
      ${ORDER_TEAM_FIELDS.map(([field, label]) => `
        <label>${label} <select class="ord-input ord-team" data-meta="${field}">${
          (typeof staffOptionsHtml === "function") ? staffOptionsHtml(label, orderModel[field])
            : `<option value="${escapeAttr(orderModel[field] || "")}" selected>${escapeHtml(orderModel[field] || "（不填）")}</option>`
        }</select></label>`).join("")}
      <label class="ord-diag-wrap">临床诊断 <input class="ord-input ord-diag" data-meta="diagnosis" value="${escapeAttr(orderModel.diagnosis)}" placeholder="病情诊断"></label>
      <button type="button" class="plain-button ord-dg-suggest" onclick="suggestOrderDiagnosis()" title="按已选处置推荐病情诊断">💡按处置推荐</button>
    </div>
    <div class="ord-tip">先录处置项目和牙位；价格在「划价」一步定（可优惠/免费/打折）</div>
    <div class="ord-rows-head"><span>牙位</span><span>处置</span><span>参考单价</span><span>数量</span><span></span></div>
    <div class="ord-rows">${orderModel.rows.map((r, ri) => renderOrderRow(ri, r)).join("")}</div>
    <div class="ord-foot"><button type="button" class="plain-button" onclick="addOrderRow()">+ 加一行</button></div>`;
}

function orderSyncFromDom() {
  const box = orderEditorEl();
  if (!box || !orderModel) return;
  box.querySelectorAll("[data-meta]").forEach(i => { orderModel[i.dataset.meta] = i.value; });
  box.querySelectorAll(".ord-row").forEach(rEl => {
    const row = orderModel.rows[Number(rEl.dataset.ri)];
    if (row) rEl.querySelectorAll("[data-field]").forEach(i => { row[i.dataset.field] = i.value; });
  });
}
function addOrderRow() { orderSyncFromDom(); orderModel.rows.push(newOrderRow()); renderTreatmentOrderEditor(); }
function removeOrderRow(ri) { orderSyncFromDom(); orderModel.rows.splice(ri, 1); renderTreatmentOrderEditor(); }   // 允许删到空(保存时仍有"至少选一个"守卫,不会存空单)
function pickOrderHandle(ri) {
  orderSyncFromDom();
  const row = orderModel.rows[ri]; if (!row) return;
  openHandlePicker(it => { row.handle_id = it.handle_id; row.item_name = it.name; row.item_code = it.code || ""; row.fee_type = it.fee_type || ""; if (it.price != null) row.unit_price = it.price; renderTreatmentOrderEditor(); });
}
function pickOrderTooth(ri) {
  orderSyncFromDom();
  const row = orderModel.rows[ri]; if (!row) return;
  openToothSelectorWith(row.tooth, teeth => { row.tooth = (teeth || []).join(","); renderTreatmentOrderEditor(); });
}
// 按已选处置推荐病情诊断，命中的诊断去重追加到诊断框(不覆盖已填)
function suggestOrderDiagnosis() {
  orderSyncFromDom();
  const names = orderModel.rows.map(r => String(r.item_name || "")).join(" ");
  const hits = [];
  HANDLE_DG_HINTS.forEach(h => {
    if (h.kw.some(k => names.includes(k)) && !hits.includes(h.dg)) hits.push(h.dg);
  });
  const status = document.getElementById("treatmentOrderStatus");
  if (!hits.length) {
    if (status) { status.textContent = names.trim() ? "无匹配诊断建议" : "请先选处置"; setTimeout(() => { if (status) status.textContent = ""; }, 1500); }
    return;
  }
  let val = String(orderModel.diagnosis || "").trim();
  hits.forEach(h => { if (!val.includes(h)) val = val ? val + "、" + h : h; });
  orderModel.diagnosis = val;
  renderTreatmentOrderEditor();
}

async function saveTreatmentOrder(priceNow = false) {
  orderSyncFromDom();
  const pid = workspacePatientId;   // 快照本次开单的患者，跨 await 不再读全局，防开单期间切患者把本单弹到别人档案
  const status = document.getElementById("treatmentOrderStatus");
  const items = orderModel.rows.filter(r => String(r.item_name || "").trim()).map(r => ({
    handle_id: r.handle_id, item_name: r.item_name, item_code: r.item_code, tooth: r.tooth, fee_type: r.fee_type || "",
    unit_price: r.unit_price === "" ? 0 : parseFloat(r.unit_price), quantity: parseInt(r.quantity, 10) || 1,
  }));
  if (!items.length) { if (status) status.textContent = "至少选一个处置"; return; }
  if (status) status.textContent = priceNow ? "开单划价中..." : "保存中...";
  // 配台人员落库 + 记住上次选择
  const team = {
    doctor_name: orderModel.doctor_name || "", nurse_name: orderModel.nurse_name || "",
    consultant_name: orderModel.consultant_name || "", assistant_name: orderModel.assistant_name || "",
  };
  if (typeof rememberTeam === "function") rememberTeam(team);
  const editId = orderModel._editId;   // 有=编辑既有待划价单(PUT原地改),无=新建(POST)
  const body = JSON.stringify({...team, diagnosis: orderModel.diagnosis, items});
  let res;
  try {
    res = editId
      ? await fetch(`/api/treatment-orders/${encodeURIComponent(editId)}`,
          {method: "PUT", headers: {"Content-Type": "application/json"}, body})
      : await fetch(`/api/patients/${encodeURIComponent(pid)}/treatment-orders`,
          {method: "POST", headers: {"Content-Type": "application/json"}, body});
  } catch { if (status) status.textContent = "保存失败（网络异常）"; return; }
  if (!res.ok) { const m = await res.json().catch(() => ({})); if (status) status.textContent = "保存失败：" + (m.detail || res.status); return; }
  const resp = await res.json().catch(() => ({}));
  closeTreatmentOrder();
  // 开单期间已切到别的患者 → 丢弃后续刷新/弹同意书，避免把本单弹到当前别人的档案
  if (pid !== workspacePatientId) return;
  refreshTreatmentsTab();
  // 「开单并划价」：处置存为"待划价"后**自动进入划价界面**(选价/打折/免单)→确认才生成待收费单。
  // 此前是后端按原价直接出单、不弹划价；用户反馈"不会自动进入划价界面"。
  if (priceNow && resp && resp.order_id && typeof openPriceOrder === "function") {
    openPriceOrder(resp.order_id);
    return;   // 划价进行中：不叠加同意书弹框(可在划价/收费后或从同意书入口触发)
  }
  // 不再"保存即自动弹同意书"(避免选N个弹N个)。需要签时点卡片「知情同意书」,
  // 届时把本单匹配的模板作「推荐签署」置顶供优先勾选(见 openOrderConsent)。
}

function refreshTreatmentsTab() {
  if (typeof workspaceLoadedTabs !== "undefined") workspaceLoadedTabs.delete("treatments");
  if (typeof evictVisitsCache === "function") evictVisitsCache();  // 处置/划价影响就诊时间轴
  if (typeof switchWorkspaceTab === "function") switchWorkspaceTab("treatments");
  if (typeof loadAuditLogs === "function") loadAuditLogs();
}

// ---------- 本地处置单列表（处置 tab 顶部，带状态+按钮） ----------
async function loadLocalOrders() {
  const box = document.querySelector("[data-local-orders]");
  if (!box) return;
  const pid = workspacePatientId;
  let data, visit = {};
  try {
    const [oRes, vRes] = await Promise.all([
      fetch(`/api/patients/${encodeURIComponent(pid)}/treatment-orders`),
      fetch(`/api/patients/${encodeURIComponent(pid)}/today-visit`),
    ]);
    if (pid !== workspacePatientId) return;
    if (!oRes.ok) { box.innerHTML = ""; return; }
    data = await oRes.json();
    if (vRes.ok) visit = await vRes.json();
  } catch { return; }
  if (pid !== workspacePatientId) return;
  let orders = data.orders || [];
  // 到达就生成:已到达且今日还没有任何未撤销单 → 自动建一张空号待划价单(后端幂等;今日已有则不重建)。
  // (修bug:旧逻辑只看"待划价",划价后单子变"待收费"会被误判为没单,又自动冒一张空单。)
  const hasActiveOrder = orders.some(o => o.status !== "voided");
  if (visit && visit.arrived && !hasActiveOrder) {
    try { await fetch(`/api/patients/${encodeURIComponent(pid)}/treatment-orders/ensure-arrival`, {method: "POST"}); } catch { /* ignore */ }
    if (pid !== workspacePatientId) return;
    try { const re = await fetch(`/api/patients/${encodeURIComponent(pid)}/treatment-orders`); if (re.ok) orders = (await re.json()).orders || orders; } catch { /* ignore */ }
    if (pid !== workspacePatientId) return;
  }
  localOrdersCache = orders;
  // 产品决策:撤销/作废的单不显示;退费的显示(钱退了要留痕)。
  // 缓存仍存全量——编辑/划价按 order_id 查缓存不受影响。
  const shownOrders = orders.filter(o => (o.effective_status || o.status) !== "voided");
  if (!shownOrders.length) { box.innerHTML = ""; return; }
  box.innerHTML = `<div class="local-orders-title">本地处置单</div>` + shownOrders.map(renderLocalOrderCard).join("");
}
let localOrdersCache = [];

function renderLocalOrderCard(o) {
  const st = o.effective_status || o.status;
  // 费用展示对齐下面历史账单卡(renderTreatmentItemsTable):同款 项目/单价/数量/金额/分类 表 + 两位小数 + 合计。
  // 牙位放进"项目"格,信息不丢。
  const orderItems = o.items || [];
  const lineFee = it => (it.total_fee != null ? Number(it.total_fee) : (Number(it.unit_price) || 0) * (Number(it.quantity) || 1));
  const grossFee = orderItems.reduce((s, it) => s + lineFee(it), 0);   // 各项原价合计
  // 已划价单合计要用账单净额(扣整单优惠后),与收费单一致,不能只显示各项原价和。
  const discount = Number(o.discount) || 0;
  const hasBill = !!(o.bill_id && o.bill_total != null);
  const totalFee = hasBill ? Number(o.bill_total) : Math.max(grossFee - discount, 0);
  const lines = !orderItems.length ? `<div class="lo-empty-hint">${(st === "recorded" && !o.closed) ? "空单 · 点「编辑」录入处置项目" : (o.closed ? "空单 · 患者已离开，已关闭" : "空单")}</div>` : `
    <table class="treatment-items lo-items">
      <thead><tr><th>项目</th><th>单价</th><th>数量</th><th>金额</th><th>分类</th></tr></thead>
      <tbody>
        ${orderItems.map(it => {
          const teeth = String(it.tooth || "").split(/[\s,，]+/).filter(Boolean);
          return `<tr>
            <td>${teeth.length ? toothCrossHtml(teeth) : ""}${escapeHtml(it.item_name || "")}</td>
            <td>${escapeHtml(formatMoney(it.unit_price))}</td>
            <td>${escapeHtml(it.quantity ?? "")}</td>
            <td>${escapeHtml(formatMoney(lineFee(it)))}</td>
            <td>${it.fee_type ? `<span class="treatment-fee-badge">${escapeHtml(it.fee_type)}</span>` : ""}</td>
          </tr>`;
        }).join("")}
      </tbody>
    </table>`;
  const btns = [];
  // 离开锁定:closed=true(今日单+患者已离开)隐藏「编辑」,划价/收费/撤销仍可
  if (o.status === "recorded" && !o.closed) btns.push(`<button type="button" class="lo-btn lo-edit" onclick="editLocalOrder('${escapeAttr(o.order_id)}')">编辑</button>`);
  // 空单(无明细)不能划价(否则出0元空账单);有项目才显示划价
  if (o.status === "recorded" && orderItems.length) btns.push(`<button type="button" class="lo-btn lo-price" onclick="openPriceOrder('${escapeAttr(o.order_id)}')">划价</button>`);
  // 用户:处置页面不放"收费"。划价后收费统一走收费tab/收费模块,处置页不再出收费按钮。
  // 已退费(effective_status=refunded)不能再撤销/收费,只读展示。
  if (st !== "paid" && st !== "refunded" && o.status !== "voided") btns.push(`<button type="button" class="lo-btn lo-void" onclick="voidOrder('${escapeAttr(o.order_id)}')">撤销</button>`);
  if (o.status === "priced" || st === "paid" || st === "refunded") btns.push(`<button type="button" class="lo-btn" onclick="printOrder('${escapeAttr(o.order_id)}')">打印</button>`);
  // 每张处置单上签它对应的知情同意书(带本次处置项目/费用，自动匹配类别)
  if (o.status !== "voided") btns.push(`<button type="button" class="lo-btn lo-consent" onclick="openOrderConsent('${escapeAttr(o.order_id)}')">知情同意书</button>`);
  return `
    <div class="lo-card ${st === "voided" ? "lo-voided" : ""}${st === "refunded" ? " lo-refunded" : ""}">
      <div class="lo-head">
        <strong>${escapeHtml(o.order_date || "")}</strong>
        ${o.order_no ? `<span class="lo-no">${escapeHtml(o.order_no)}</span>` : ""}
        <span class="lo-status ${ORDER_STATUS_CLASS[st] || ""}">${ORDER_STATUS_LABEL[st] || st}</span>
        ${o.closed ? '<span class="lo-closed-tag">已关闭</span>' : ''}
        ${o.doctor_name ? `<span class="muted">${escapeHtml(o.doctor_name)}</span>` : ""}
        <span class="treatment-visit-total">合计 ${formatMoney(totalFee)}${discount > 0 ? `<small class="lo-disc">（原价 ${formatMoney(grossFee)} · 优惠 ${formatMoney(discount)}）</small>` : ""}</span>
        <span class="lo-actions">${btns.join("")}</span>
      </div>
      ${(o.status === "recorded" && !o.closed) ? `<div class="lo-editable" onclick="editLocalOrder('${escapeAttr(o.order_id)}')" title="点此编辑处置">${lines}</div>` : lines}
      <span data-order-status="${escapeAttr(o.order_id)}" class="lo-msg"></span>
    </div>`;
}

// 从处置单开同意书：带本次处置项目/费用，按类别匹配，多类别逐个排队签
function openOrderConsent(orderId) {
  const o = (localOrdersCache || []).find(x => x.order_id === orderId);
  if (!o) { if (typeof openConsentForm === "function") openConsentForm(workspacePatientId); return; }
  const cats = [];
  (o.items || []).forEach(it => {
    const c = (typeof treatmentConsentCategory === "function") ? treatmentConsentCategory(it.item_name) : "";
    if (c && !cats.includes(c)) cats.push(c);
  });
  const orderItems = (o.items || []).map(it => ({item_name: it.item_name, quantity: it.quantity,
    line_fee: it.total_fee != null ? it.total_fee : ((parseFloat(it.unit_price) || 0) * (parseInt(it.quantity, 10) || 1))}));
  const total = (o.bill_total != null && o.bill_total > 0) ? o.bill_total
                : orderItems.reduce((s, it) => s + (Number(it.line_fee) || 0), 0);
  // 不再逐个弹:打开同意书表单,把匹配类别作「推荐签署」多选面板置顶(cats为空则只开表单)
  if (typeof openConsentForm === "function") {
    openConsentForm(workspacePatientId, o.bill_id || "", total, cats.length ? cats : "", orderItems, o.order_id || "");
  }
}

// 撤单理由预设；末项「其他」走手填
const VOID_REASONS = ["收费方式选择错误", "收费医生选择错误", "折扣错误", "收费金额错误",
  "处置选择错误", "患者个人原因", "患者要求改方案"];

function voidOrder(orderId) {
  // 撤销必须填理由。改为 外部系统 风格预设下拉(选一个)+其他手填
  let m = document.getElementById("voidReasonModal");
  if (!m) { m = document.createElement("div"); m.id = "voidReasonModal"; m.className = "modal-backdrop"; document.body.appendChild(m); }
  m.hidden = false;
  const opts = VOID_REASONS.map(r => `<option value="${escapeAttr(r)}">${escapeHtml(r)}</option>`).join("");
  m.innerHTML = `
    <section class="appt-modal" role="dialog" aria-modal="true" aria-label="撤销处置单">
      <div class="modal-head"><strong>撤销处置单</strong><button type="button" class="plain-button" onclick="closeVoidReason()">×</button></div>
      <div class="appt-body">
        <p class="appt-field">未收费的会一并作废收费单。请选择撤销理由：</p>
        <label class="appt-field">撤销理由
          <select id="vrReason" class="ord-input" onchange="onVoidReasonChange()">
            ${opts}
            <option value="__other__">其他（手填）</option>
          </select>
        </label>
        <label class="appt-field" id="vrOtherWrap" style="display:none">补充 <input id="vrOther" class="ord-input" autocomplete="off" placeholder="请填写撤销理由"></label>
      </div>
      <div class="modal-actions">
        <span id="vrStatus" class="today-refresh-status"></span>
        <button type="button" class="tooth-confirm-btn" onclick="guardSubmit(this, () => submitVoidReason('${escapeAttr(orderId)}'))">确认撤销</button>
        <button type="button" class="plain-button" onclick="closeVoidReason()">取消</button>
      </div>
    </section>`;
}
function closeVoidReason() { const m = document.getElementById("voidReasonModal"); if (m) m.hidden = true; }
function onVoidReasonChange() {
  const sel = document.getElementById("vrReason"); const w = document.getElementById("vrOtherWrap");
  if (sel && w) w.style.display = sel.value === "__other__" ? "" : "none";
}
async function submitVoidReason(orderId) {
  const sel = document.getElementById("vrReason");
  const status = document.getElementById("vrStatus");
  let reason = sel ? sel.value : "";
  if (reason === "__other__") reason = ((document.getElementById("vrOther") || {}).value || "").trim();
  if (!reason) { if (status) status.textContent = "请填写撤销理由"; return; }
  if (status) status.textContent = "撤销中...";
  let res;
  try {
    res = await fetch(`/api/treatment-orders/${encodeURIComponent(orderId)}/void`, {
      method: "POST", headers: {"Content-Type": "application/json"},
      body: JSON.stringify({reason: reason})});
  } catch { if (status) status.textContent = "撤销失败（网络异常）"; return; }
  if (!res.ok) { const m = await res.json().catch(() => ({})); if (status) status.textContent = "撤销失败：" + (m.detail || res.status); return; }
  closeVoidReason();
  refreshTreatmentsTab();
}

function payOrderBill(billId, remaining) {
  if (typeof confirmBillPayment === "function") confirmBillPayment(billId, remaining != null ? remaining : 0);
}

// 共享：构建并打开收费凭单打印窗口
function openReceiptPrint(opts) {
  const rows = (opts.items || []).map(it => {
    const teeth = String(it.tooth || "").split(/[\s,，]+/).filter(Boolean).join(" ");
    return `<tr><td>${escapeHtml(it.item_name || "")}</td><td>${escapeHtml(teeth)}</td><td class="r">${it.unit_price != null ? it.unit_price : ""}</td><td class="r">${it.quantity != null ? it.quantity : ""}</td><td class="r">${it.total_fee != null ? it.total_fee : ""}</td></tr>`;
  }).join("");
  const metaLines = [];
  metaLines.push(`患者：${escapeHtml(opts.patient || "")}`);
  const line2 = [];
  if (opts.date) line2.push(`日期：${escapeHtml(opts.date)}`);
  if (opts.doctor) line2.push(`医生：${escapeHtml(opts.doctor)}`);
  if (line2.length) metaLines.push(line2.join("　"));
  if (opts.status) metaLines.push(`状态：${escapeHtml(opts.status)}`);
  if (opts.diagnosis) metaLines.push(`诊断：${escapeHtml(opts.diagnosis)}`);
  if (opts.bill_no) metaLines.push(`单号：${escapeHtml(opts.bill_no)}`);
  const html = `<!doctype html><html><head><meta charset="utf-8"><title>收费小票</title>
    <style>
      body{font-family:-apple-system,"PingFang SC",sans-serif;padding:16px;color:#1b2733;width:300px;}
      h2{text-align:center;margin:0 0 4px;font-size:16px;}
      .sub{text-align:center;color:#5b6b7b;font-size:12px;margin-bottom:10px;}
      .meta{font-size:12px;margin-bottom:8px;line-height:1.6;}
      table{width:100%;border-collapse:collapse;font-size:12px;}
      th,td{text-align:left;padding:3px 2px;border-bottom:1px dashed #ccc;}
      td.r,th.r{text-align:right;}
      .tot{display:flex;justify-content:space-between;font-size:13px;margin-top:8px;font-weight:700;}
      .foot{text-align:center;color:#8a96a3;font-size:11px;margin-top:14px;}
    </style></head><body onload="window.print()">
    <h2>${escapeHtml(window.CLINIC_NAME || "GD · DentOS")}</h2>
    <div class="sub">${escapeHtml(opts.subtitle || "收费凭单")}</div>
    <div class="meta">${metaLines.join("<br>")}</div>
    <table><thead><tr><th>项目</th><th>牙位</th><th class="r">单价</th><th class="r">数量</th><th class="r">金额</th></tr></thead><tbody>${rows || '<tr><td colspan="5">（无明细）</td></tr>'}</tbody></table>
    <div class="tot"><span>合计</span><span>${opts.total != null ? opts.total : ""}</span></div>
    ${opts.paid != null && opts.paid !== "" ? `<div class="tot" style="font-weight:400;"><span>已收</span><span>${opts.paid}</span></div>` : ""}
    <div class="foot">明细金额仅供参考，以收费单为准</div>
    </body></html>`;
  const w = window.open("", "_blank", "width=360,height=560");
  if (!w) return;
  w.document.write(html);
  w.document.close();
}

// 处置单打印（从本地缓存取数）
function printOrder(orderId) {
  const o = localOrdersCache.find(x => x.order_id === orderId);
  if (!o) return;
  openReceiptPrint({
    subtitle: "处置收费凭单",
    patient: (workspaceData && workspaceData.patient) ? workspaceData.patient.display_name : "",
    date: o.order_date, doctor: o.doctor_name,
    status: ORDER_STATUS_LABEL[o.effective_status || o.status] || "",
    diagnosis: o.diagnosis, items: o.items,
    total: o.bill_total != null ? o.bill_total : "", paid: o.bill_paid,
  });
}

// 从按钮 data-* 读取参数（HTML属性转义安全），避免把患者姓名拼进 inline onclick 的 JS 字符串
function printBillFromEl(el) {
  if (!el) return;
  const d = el.dataset;
  const num = v => (v === "" || v == null) ? undefined : Number(v);
  printBill(d.billId, d.patientId, d.patientName || "", {
    total: num(d.total), paid: num(d.paid), state: d.state || "",
    bill_no: d.billNo || "", bill_time: d.billTime || "",
  });
}

// 账单打印（收费 tab / 前台收费台）：按 bill_id 拉该患者处置明细后打印
async function printBill(billId, patientId, patientName, meta) {
  meta = meta || {};
  let items = [];
  try {
    const data = await (await fetch(`/api/patients/${encodeURIComponent(patientId)}/treatments`)).json();
    const visit = (data.visits || []).find(v => v.bill_id === billId);
    if (visit) items = visit.items || [];
  } catch { /* 拉明细失败仍打印摘要 */ }
  const total = meta.total != null ? meta.total
    : items.reduce((s, it) => s + (it.total_fee || 0), 0);
  openReceiptPrint({
    subtitle: "收费账单",
    patient: patientName || "", date: meta.bill_time || "", status: meta.state || "",
    bill_no: meta.bill_no || "", items: items,
    total: total, paid: meta.paid,
  });
}

// ---------- 划价弹框（逐项 收费/免费/打折 + 整单优惠） ----------
let priceModel = null;

async function openPriceOrder(orderId) {
  const pid = workspacePatientId;
  let data;
  try { data = await (await fetch(`/api/patients/${encodeURIComponent(pid)}/treatment-orders`)).json(); }
  catch { return; }
  const order = (data.orders || []).find(o => o.order_id === orderId);
  if (!order) return;
  priceModel = {
    order_id: orderId, discount: "",
    items: (order.items || []).map(it => {
      const up = it.unit_price || 0, q = it.quantity || 1;
      const gross = up * q;
      // 直填"本项金额"(默认=原价)，自动算折率；免单勾选
      return { item_id: it.item_id, item_name: it.item_name, unit_price: up,
               quantity: q, gross: gross, free: false, amount: String(gross) };
    }),
  };
  const m = document.getElementById("orderPriceModal");
  if (!m) return;
  renderPriceEditor();
  m.hidden = false;
}
function closePriceOrder() { const m = document.getElementById("orderPriceModal"); if (m) m.hidden = true; priceModel = null; }

// 架构铁律#禁止兜底：金额必须是非负数字，非法返回 null(界面标「无效」,提交被拦)，
// 与后端400口径一致，不再静默退回原价
function priceValidAmount(v) {
  const s = String(v == null ? "" : v).trim();
  if (s === "") return null;
  const n = Number(s);
  return (Number.isFinite(n) && n >= 0) ? n : null;
}
function priceLineFee(it) {
  if (it.free) return 0;
  return priceValidAmount(it.amount);
}
// 自动折率显示：本项金额/原价。免单→留空(免单复选框已表意,避免与其并列成"免单 免单")；
// 原价及以上→"原价"；否则 x.x折
function priceRateText(it) {
  if (it.free) return "—";
  const fee = priceLineFee(it);
  if (fee == null) return "无效";
  const g = it.gross || 0;
  if (g <= 0) return "—";
  if (fee <= 0.005) return "免费";   // 改价为0=免费,不显示误导的「0.0折」
  const r = fee / g;
  if (r >= 1) return "原价";
  return (r * 10).toFixed(1) + "折";
}
function priceTotal() {
  let s = 0, bad = false;
  (priceModel.items || []).forEach(it => { const f = priceLineFee(it); if (f == null) bad = true; else s += f; });
  if (bad) return null;   // 有非法金额时合计无意义，不显示假数
  return Math.max(s - (parseFloat(priceModel.discount) || 0), 0);
}
function priceTotalText() {
  const t = priceTotal();
  return t == null ? "金额无效" : t;
}

function renderPriceEditor() {
  const box = document.getElementById("orderPriceBody");
  if (!box || !priceModel) return;
  const rows = priceModel.items.map((it, i) => `
    <div class="pr-row">
      <span class="pr-name">${escapeHtml(it.item_name)}</span>
      <span class="pr-gross">${escapeHtml(it.unit_price)}×${escapeHtml(it.quantity)}=${it.gross}</span>
      <input class="pr-input pr-amount" value="${escapeAttr(it.amount)}" inputmode="decimal" title="本项金额(折后实收)"
        ${it.free ? "disabled" : ""} oninput="priceSetAmount(${i},this.value)">
      <span class="pr-rate" data-pr-rate="${i}">${priceRateText(it)}</span>
      <label class="pr-free"><input type="checkbox" ${it.free ? "checked" : ""} onchange="priceToggleFree(${i},this.checked)">免单</label>
      <span class="pr-line" data-pr-line="${i}">${priceLineFee(it) == null ? "无效" : priceLineFee(it)}</span>
    </div>`).join("");
  // 全部免单/小计为0时,整单优惠无意义→禁用并提示,避免前台以为又减了一笔
  const subtotal = (priceModel.items || []).reduce((s, it) => s + (priceLineFee(it) || 0), 0);
  const noDiscount = subtotal <= 0.005;
  box.innerHTML = `
    <div class="pr-head"><span>处置</span><span>原价</span><span>金额</span><span>折率</span><span>免单</span><span>小计</span></div>
    ${rows}
    <div class="pr-foot">
      <label class="pr-discount">整单优惠 <input class="pr-input" value="${escapeAttr(priceModel.discount)}" inputmode="decimal" ${noDiscount ? "disabled" : ""} oninput="priceSetDiscount(this.value)"></label>
      ${noDiscount ? '<span class="pr-discount-hint">已全免单，无需整单优惠</span>' : ""}
      <span class="pr-total">合计 ${priceTotalText()}</span>
    </div>`;
}
// 直填本项金额：只更新该行金额/折率/合计，不整体重渲(保住焦点)
function priceSetAmount(i, val) {
  const it = priceModel.items[i];
  if (!it) return;
  it.amount = val;
  const lineEl = document.querySelector(`[data-pr-line="${i}"]`);
  if (lineEl) lineEl.textContent = priceLineFee(it) == null ? "无效" : priceLineFee(it);
  const rateEl = document.querySelector(`[data-pr-rate="${i}"]`);
  if (rateEl) rateEl.textContent = priceRateText(it);
  const t = document.querySelector("#orderPriceBody .pr-total");
  if (t) t.textContent = "合计 " + priceTotalText();
}
// 免单勾选：要重渲以禁用/启用金额框（复选框重渲不丢焦点）
function priceToggleFree(i, checked) {
  if (!priceModel.items[i]) return;
  priceModel.items[i].free = checked;
  renderPriceEditor();
}
function priceSetDiscount(val) {
  priceModel.discount = val;
  const t = document.querySelector("#orderPriceBody .pr-total");
  if (t) t.textContent = "合计 " + priceTotalText();
}
async function submitPriceOrder() {
  const status = document.getElementById("orderPriceStatus");
  // 架构铁律#禁止兜底：非法金额/优惠在提交前拦下(与后端400口径一致)
  const badRow = (priceModel.items || []).find(it => !it.free && priceValidAmount(it.amount) == null);
  if (badRow) { if (status) status.textContent = `划价失败：「${badRow.item_name}」金额无效，请填非负数字或勾选免单`; return; }
  const rawDiscount = String(priceModel.discount == null ? "" : priceModel.discount).trim();
  if (rawDiscount !== "" && priceValidAmount(rawDiscount) == null) {
    if (status) status.textContent = "划价失败：整单优惠必须是非负数字"; return;
  }
  const payload = {
    discount: rawDiscount === "" ? 0 : Number(rawDiscount),
    items: priceModel.items.map(it => ({
      item_id: it.item_id, free: !!it.free, amount: it.amount,
    })),
  };
  if (status) status.textContent = "划价中...";
  let res;
  try {
    res = await fetch(`/api/treatment-orders/${encodeURIComponent(priceModel.order_id)}/price`, {
      method: "POST", headers: {"Content-Type": "application/json"}, body: JSON.stringify(payload),
    });
  } catch { if (status) status.textContent = "划价失败（网络异常）"; return; }
  if (!res.ok) { const m = await res.json().catch(() => ({})); if (status) status.textContent = "划价失败：" + (m.detail || res.status); return; }
  closePriceOrder();
  // 划价生成本地待收费单 → 刷新患者详情缓存并清收费 tab,否则收费 tab 仍是旧的0账单
  if (typeof refreshWorkspaceDetail === "function") await refreshWorkspaceDetail();
  if (typeof workspaceLoadedTabs !== "undefined") workspaceLoadedTabs.delete("billing");
  refreshTreatmentsTab();
}

// 「挂号并继续」：选医生→建今日挂号(已到诊)→处置医生默认带入→去掉横幅继续录处置
async function registerTodayThenOrder() {
  const sel = orderEditorEl() && orderEditorEl().querySelector("[data-reg-doctor]");
  const doctor = (sel && sel.value || "").trim();
  if (!doctor) { window.alert("请先选挂号医生"); return; }
  const pid = workspacePatientId;
  // 建号前重查一次——横幅弹出后前台/其他设备可能已挂号,此时并入当前就诊上下文,
  // 绝不再造同日第二条"挂号"队列行;重查失败明确提示,不盲建。
  let recheck;
  try {
    const r = await fetch(`/api/patients/${encodeURIComponent(pid)}/today-visit`);
    if (!r.ok) { window.alert("挂号状态查询失败：" + r.status); return; }
    recheck = await r.json();
  } catch { window.alert("挂号状态查询失败（网络异常），请重试"); return; }
  if (pid !== workspacePatientId) return;
  if (recheck && recheck.has_today) {
    if (typeof orderSyncFromDom === "function") orderSyncFromDom();
    _orderTodayVisit = recheck;
    if (orderModel && recheck.doctor_name) orderModel.doctor_name = recheck.doctor_name;
    renderTreatmentOrderEditor();   // 横幅消失，带入已挂号医生
    return;
  }
  const start = bjNowStr().slice(0, 16);   // 北京 "YYYY-MM-DD HH:MM"
  let res;
  try {
    res = await fetch("/api/appointments", {
      method: "POST", headers: {"Content-Type": "application/json"},
      body: JSON.stringify({patient_identity: pid, start_time: start, doctor_name: doctor,
        item_name: "挂号", status: "已到诊", force: true}),
    });
  } catch { window.alert("挂号失败（网络异常）"); return; }
  if (!res.ok) { const m = await res.json().catch(() => ({})); window.alert("挂号失败：" + (m.detail || res.status)); return; }
  if (pid !== workspacePatientId) return;
  if (typeof orderSyncFromDom === "function") orderSyncFromDom();  // 重渲前先存已填的诊断/单价/数量,别丢
  _orderTodayVisit = {has_today: true, doctor_name: doctor};
  if (orderModel) orderModel.doctor_name = doctor;
  renderTreatmentOrderEditor();   // 横幅消失，医生已默认带入
  if (typeof evictVisitsCache === "function") evictVisitsCache();
}

Object.assign(window, {
  openTreatmentOrder, editLocalOrder, closeTreatmentOrder, addOrderRow, removeOrderRow, registerTodayThenOrder,
  pickOrderHandle, pickOrderTooth, suggestOrderDiagnosis, saveTreatmentOrder, loadLocalOrders,
  voidOrder, closeVoidReason, onVoidReasonChange, submitVoidReason,
  openOrderConsent, payOrderBill, printOrder, printBill, printBillFromEl, openReceiptPrint, openPriceOrder, closePriceOrder,
  priceSetAmount, priceToggleFree, priceSetDiscount, submitPriceOrder,
});
