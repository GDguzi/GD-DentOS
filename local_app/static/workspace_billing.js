
// 患者工作区·收费 tab(呈现+收退款操作):账单表格/支付拆分/退费/作废。
// 独立模块:只经 /api/bills* 业务路由操作,全局函数挂 window(原 workspace_tabs.js 拆出)。

// ---------- 收费 tab（只读）----------

function renderWorkspaceBillingTab(panel) {
  const data = requireWorkspaceDetail(panel);
  if (!data) return;
  const bills = data.bills || [];
  const payments = data.payments || [];
  const paymentsByBill = new Map();
  payments.forEach(payment => {
    const key = String(payment.bill_id || "");
    if (!paymentsByBill.has(key)) paymentsByBill.set(key, []);
    paymentsByBill.get(key).push(payment);
  });
  // 收费单明细：按 bill_id 归组(收费单卡列出本单有哪些诊疗项目)
  const itemsByBill = new Map();
  (data.treatment_items || []).forEach(it => {
    const key = String(it.bill_id || "");
    if (!itemsByBill.has(key)) itemsByBill.set(key, []);
    itemsByBill.get(key).push(it);
  });
  const rows = bills.map(bill => {
    const pays = paymentsByBill.get(String(bill.bill_id)) || [];
    paymentsByBill.delete(String(bill.bill_id));
    return renderBillTableRow(bill, itemsByBill.get(String(bill.bill_id)) || [], pays);
  }).join("");
  // 合计跳过撤单/作废单(is_void)；应收用后端算好的 net_receivable(=总价-优惠;本地单已是净额)，欠费=应收-已收
  const sum = bills.reduce((a, b) => {
    if (b.is_void) return a;
    a.total += b.total_fee || 0;
    a.receivable += (b.net_receivable != null ? Number(b.net_receivable) : (b.total_fee || 0));
    a.paid += b.paid_fee || 0;
    return a;
  }, {total: 0, receivable: 0, paid: 0});
  const sumReceivable = sum.receivable;
  // 0 元空流水(外部导入带回)不进「未关联付款」，只碰展示不碰数据
  const orphanPayments = Array.from(paymentsByBill.values()).flat().filter((p) => Number(p.amount) !== 0);
  panel.innerHTML = `
    <section class="panel">
      <div class="panel-head">账单列表<span>${bills.length} 条 · 只读</span></div>
      <div class="panel-body bill-table-wrap">
        <table class="bill-table">
          <thead><tr>
            <th>账单编号</th><th>开单时间</th><th>医生</th><th>开单人</th><th>咨询师</th>
            <th class="num">总费用</th><th class="num">应收</th><th class="num">已收</th><th class="num">欠费</th>
            <th>折扣率</th><th class="num">减免</th><th>优惠活动</th><th>备注</th>
            <th>状态</th><th class="bt-ops-h">操作</th>
          </tr></thead>
          <tbody>${rows || '<tr><td colspan="15" class="bt-empty">0 条记录</td></tr>'}</tbody>
          ${bills.length ? `<tfoot><tr><td colspan="5">合计</td>
            <td class="num">${formatMoney(sum.total)}</td><td class="num">${formatMoney(sumReceivable)}</td>
            <td class="num">${formatMoney(sum.paid)}</td><td class="num">${formatMoney(sumReceivable - sum.paid)}</td>
            <td colspan="6"></td></tr></tfoot>` : ""}
        </table>
      </div>
    </section>
    ${orphanPayments.length ? recordsPanel("未关联付款", orphanPayments, renderPayment) : ""}
  `;
  // 从今日工作台「收费」进来:只有一张待收账单就直接弹收款框(直接吊起收费)。
  // 意图绑患者id。收费tab一渲染就**无条件消费**这个意图(清掉),只有 id 等于当前患者才弹;
  // 不匹配也清,避免过期意图残留→之后正常打开该患者收费页被误弹收款框(改钱入口)。
  const _wantPay = window._queueWantsPay;
  window._queueWantsPay = null;
  if (_wantPay && _wantPay === workspacePatientId) {
    const payableBills = bills.filter(b => (b.state === "pending" || b.state === "partial") && b.bill_id);
    if (payableBills.length === 1 && typeof confirmBillPayment === "function") {
      const b = payableBills[0];
      const net = b.net_receivable != null ? Number(b.net_receivable) : (b.total_fee || 0);
      confirmBillPayment(b.bill_id, Math.round((net - (b.paid_fee || 0)) * 100) / 100);
    }
  }
}

// 表格式账单行(对标官方账单列表) + 可展开详情(诊疗项目 + 各笔付款及方式)
function renderBillTableRow(b, items, pays) {
  // 应收=net_receivable(后端算:总价-优惠;本地单已是净额)，欠费=应收-已收
  const netReceivable = b.net_receivable != null ? Number(b.net_receivable) : (b.total_fee || 0);
  const remaining = Math.round((netReceivable - (b.paid_fee || 0)) * 100) / 100;
  const payable = b.state === "pending" || b.state === "partial";
  const doctor = b.bill_doctor || (items.find(i => i.doctor_name) || {}).doctor_name || "";
  const discount = Number(b.discount_fee) || 0;
  const discRate = billDiscountRate(b);
  const ops = [];
  if (payable) ops.push(`<a class="bt-op" onclick="confirmBillPayment('${escapeAttr(b.bill_id)}', ${remaining})">收费</a>`);
  if (b.state === "pending" && (b.paid_fee || 0) <= 0 && String(b.bill_id).startsWith("local-bill-")) ops.push(`<a class="bt-op" onclick="voidBillOrder('${escapeAttr(b.bill_id)}')">撤单</a>`);
  if (b.state === "paid" && String(b.bill_id).startsWith("local-bill-") && canRefund()) ops.push(`<a class="bt-op bt-op-refund" onclick="confirmBillRefund('${escapeAttr(b.bill_id)}')">退费</a>`);
  if (b.bill_id) ops.push(`<a class="bt-op" data-bill-id="${escapeAttr(b.bill_id)}" data-patient-id="${escapeAttr(workspacePatientId)}" data-patient-name="${escapeAttr((workspaceData && workspaceData.patient) ? (workspaceData.patient.display_name || "") : "")}" data-total="${b.total_fee != null ? b.total_fee : ""}" data-paid="${b.paid_fee != null ? b.paid_fee : ""}" data-state="${escapeAttr(b.state || "")}" data-bill-no="${escapeAttr(b.bill_no || "")}" data-bill-time="${escapeAttr(b.bill_time || b.updated_at || "")}" onclick="printBillFromEl(this)">打印</a>`);
  const hasDetail = items.length || pays.length;
  if (hasDetail) ops.push(`<a class="bt-op" onclick="toggleBillDetail('${escapeAttr(b.bill_id)}')">详情</a>`);
  const itemRows = items.map(it => {
    const fee = it.total_fee != null ? it.total_fee : ((it.unit_price || 0) * (it.quantity || 1));
    return `<tr><td>${escapeHtml(it.item_name || "")}</td><td class="bt-ft">${escapeHtml(it.fee_type || "")}</td><td class="num">×${escapeHtml(it.quantity)}</td><td class="num">${formatMoney(fee)}</td></tr>`;
  }).join("");
  const payRows = pays.map(p => {
    const method = p.pay_method || "";
    const voided = String(p.state) === "1";
    return `<div class="bt-pay${voided ? " bt-pay-void" : ""}">${escapeHtml(String(p.pay_time || "").slice(0, 16))}　¥${formatMoney(p.amount)}　${escapeHtml(method || "未填")}${voided ? "　(已作废)" : ""}</div>`;
  }).join("");
  return `
    <tr class="bt-row ${billStateClass(b.state, netReceivable, b.paid_fee)}">
      <td class="bt-no">${escapeHtml(b.bill_no || "—")}</td>
      <td class="bt-time">${escapeHtml(String(b.bill_time || b.updated_at || "").slice(0, 16))}</td>
      <td>${escapeHtml(doctor || "")}</td>
      <td>${escapeHtml(b.operator_name || "")}</td>
      <td>${escapeHtml(b.consultant_name || "")}</td>
      <td class="num">${formatMoney(b.total_fee)}</td>
      <td class="num">${formatMoney(netReceivable)}</td>
      <td class="num">${formatMoney(b.paid_fee)}</td>
      <td class="num ${remaining > 0 ? "bt-owe" : ""}">${formatMoney(remaining)}</td>
      <td>${escapeHtml(discRate)}</td>
      <td class="num">${discount > 0 ? formatMoney(discount) : ""}</td>
      <td>${escapeHtml(b.discount_memo || "")}</td>
      <td class="bt-remark">${escapeHtml(b.bill_remark || "")}</td>
      <td><span class="bc-state">${escapeHtml(billStateLabel(b.state, netReceivable, b.paid_fee))}</span></td>
      <td class="bt-ops">${ops.join('<span class="bt-sep">|</span>')}</td>
    </tr>
    ${hasDetail ? `<tr class="bt-detail" data-bill-detail="${escapeAttr(b.bill_id)}" hidden><td colspan="15">
      ${itemRows ? `<div class="bt-dtitle">诊疗项目</div><table class="bt-items"><tbody>${itemRows}</tbody></table>` : ""}
      ${payRows ? `<div class="bt-dtitle">付款记录（${pays.length}笔，可不同方式）</div>${payRows}` : ""}
    </td></tr>` : ""}`;
}

function toggleBillDetail(billId) {
  const row = document.querySelector(`[data-bill-detail="${cssEscape(billId)}"]`);
  if (row) row.hidden = !row.hidden;
}

// 折扣率口径按账单来源区分——外部系统 单 total_fee=优惠前总额(应收在 net_receivable)，
// 折扣率=net/total；本地单(local-bill-*) total_fee 已是净额，折扣率=total/(total+优惠)。
function billDiscountRate(b) {
  const discount = Number(b.discount_fee) || 0;
  if (discount <= 0) return "";
  const total = Number(b.total_fee) || 0;
  const isLocal = String(b.bill_id || "").startsWith("local-bill-");
  const gross = isLocal ? total + discount : total;
  const net = isLocal ? total
    : (b.net_receivable != null ? Number(b.net_receivable) : total - discount);
  return gross > 0 ? Math.round(net / gross * 100) + "%" : "";
}

const BILL_STATE_LABEL = {pending: "待收费", partial: "部分收款", paid: "已收清", voided: "已作废", refunded: "已退费"};   // 补 refunded,否则退费单状态列显「—」
// 导入的历史账单状态是数字码(如100)，按金额派生可读状态更可靠；本地状态直接映射。
// 撤单/作废账单状态(历史数字撤单码 + 本地 voided)，与后端 VOIDED_BILL_STATES 对齐
const VOID_BILL_STATES = ["900", "500", "400", "200", "voided"];
function billStateLabel(s, total, paid) {
  if (VOID_BILL_STATES.includes(String(s)) || String(s) === "1") return "已作废";
  if (BILL_STATE_LABEL[s]) return BILL_STATE_LABEL[s];
  total = total || 0; paid = paid || 0;
  if (total > 0 && paid + 1e-6 >= total) return "已收清";
  if (paid > 0) return "部分收款";
  return total > 0 ? "待收费" : "—";
}
function billStateClass(s, total, paid) {
  if (VOID_BILL_STATES.includes(String(s)) || String(s) === "1") return "bc-voided";
  if (s === "refunded") return "bc-refunded";   // 已退费
  if (s === "paid") return "bc-paid";
  total = total || 0; paid = paid || 0;
  if (total > 0 && paid + 1e-6 >= total) return "bc-paid";
  if (paid > 0 || s === "partial" || s === "pending") return "bc-pending";
  return "";
}

// 收费单卡片化（对标处置单卡，清爽极简）。items=本单诊疗项目明细
function renderBill(row, items) {
  const payable = row.state === "pending" || row.state === "partial";
  const remaining = (row.total_fee || 0) - (row.paid_fee || 0);
  const itemRows = (items || []).map(it => {
    const fee = it.total_fee != null ? it.total_fee : ((it.unit_price || 0) * (it.quantity || 1));
    const ft = it.fee_type ? `<span class="bc-itft">${escapeHtml(it.fee_type)}</span>` : "";
    return `<div class="bc-item"><span class="bc-iname">${escapeHtml(it.item_name || "")}</span>${ft}<span class="bc-iqty">×${escapeHtml(it.quantity)}</span><span class="bc-ifee">${formatMoney(fee)}</span></div>`;
  }).join("");
  return `
    <div class="bc-card ${billStateClass(row.state, row.total_fee, row.paid_fee)}">
      <div class="bc-head">
        <span class="bc-no">账单 ${escapeHtml(row.bill_no || "—")}</span>
        <span class="bc-time">${escapeHtml(row.bill_time || row.updated_at || "")}</span>
        <span class="bc-state">${escapeHtml(billStateLabel(row.state, row.total_fee, row.paid_fee))}</span>
      </div>
      ${itemRows ? `<div class="bc-items">${itemRows}</div>` : ""}
      <div class="bc-amounts">
        <span class="bc-amt">总额 <b>${formatMoney(row.total_fee)}</b></span>
        <span class="bc-amt">已收 <b>${formatMoney(row.paid_fee)}</b></span>
        ${remaining > 0 ? `<span class="bc-amt bc-remain">待收 <b>${formatMoney(remaining)}</b></span>` : ""}
      </div>
      ${row.bill_id ? `
        <div class="bc-actions">
          ${payable ? `<button type="button" class="bill-pay-btn" onclick="confirmBillPayment('${escapeAttr(row.bill_id)}', ${remaining})">收款</button>` : ""}
          ${row.state === "pending" && (row.paid_fee || 0) <= 0 && String(row.bill_id).startsWith("local-bill-") ? `<button type="button" class="bill-void-btn" onclick="voidBillOrder('${escapeAttr(row.bill_id)}')">撤销</button>` : ""}
          <button type="button" class="bill-print-btn"
            data-bill-id="${escapeAttr(row.bill_id)}"
            data-patient-id="${escapeAttr(workspacePatientId)}"
            data-patient-name="${escapeAttr((workspaceData && workspaceData.patient) ? (workspaceData.patient.display_name || "") : "")}"
            data-total="${row.total_fee != null ? row.total_fee : ""}" data-paid="${row.paid_fee != null ? row.paid_fee : ""}"
            data-state="${escapeAttr(row.state || "")}" data-bill-no="${escapeAttr(row.bill_no || "")}" data-bill-time="${escapeAttr(row.bill_time || row.updated_at || "")}"
            onclick="printBillFromEl(this)">打印</button>
          <span data-bill-pay-status="${escapeAttr(row.bill_id)}"></span>
        </div>` : ""}
    </div>
  `;
}

// 就诊时间轴(visits)是 病历/处置/收费/预约/回访 的聚合视图，任一业务成功变更后都要清它的缓存，
// 否则用户做完操作回"就诊"仍看旧时间轴(要重开患者才刷)。各业务刷新点统一调它。
function evictVisitsCache() {
  if (typeof workspaceLoadedTabs !== "undefined") workspaceLoadedTabs.delete("visits");
}

// 收费处撤销 → 填理由 → 按账单找处置单撤销 → 刷新
async function voidBillOrder(billId) {
  const reason = await appPrompt("撤销这张收费单(及其处置单)，请填写撤销理由：", "");
  if (reason === null) return;
  if (!reason.trim()) { window.alert("必须填写撤销理由"); return; }
  const status = document.querySelector(`[data-bill-pay-status="${cssEscape(billId)}"]`);
  if (status) status.textContent = "撤销中...";
  let res;
  try {
    res = await fetch(`/api/bills/${encodeURIComponent(billId)}/void-order`, {
      method: "POST", headers: {"Content-Type": "application/json"},
      body: JSON.stringify({reason: reason.trim()})});
  } catch { if (status) status.textContent = "撤销失败（网络异常）"; return; }
  if (!res.ok) { const m = await res.json().catch(() => ({})); if (status) status.textContent = "撤销失败：" + (m.detail || res.status); return; }
  if (typeof refreshWorkspaceDetail === "function") await refreshWorkspaceDetail();
  workspaceLoadedTabs.delete("billing");
  workspaceLoadedTabs.delete("treatments");
  evictVisitsCache();
  const page = workspacePageEl();
  const active = page && page.querySelector(".workspace-tab.active");
  switchWorkspaceTab(active ? active.dataset.workspaceTab : "billing");
  if (typeof loadAuditLogs === "function") await loadAuditLogs();
}

// 前台收款确认：录金额 → POST /api/bills/{id}/pay → 刷新收费 tab
// 正式收款弹框：账单编号+收银员 + 逐项明细(单价/数量/折/总价/减免) + 合计应收/已收/欠款/找零 + 收费方式
const PAY_CASHIER_LS = "dental_cashier_last";   // 记住上次收银员
// 付款方式改全店可自定义(收费弹窗⚙️),运行时从设置拉;null=未加载/失败(显式报错,禁止兜底旧名单)
let PAY_METHODS = null;
let payContext = null;
// 收退款请求号。LAN http(iPad/手机)是非安全上下文没有 crypto.randomUUID,
// 统一走 getRandomValues(全上下文可用),单一代码路径。
function newPaymentRequestId() {
  const a = new Uint8Array(16);
  crypto.getRandomValues(a);
  return Array.from(a, b => b.toString(16).padStart(2, "0")).join("");
}

async function loadPayMethods(force) {
  if (PAY_METHODS && !force) return PAY_METHODS;
  try {
    const r = await fetch("/api/settings/pay-methods");
    PAY_METHODS = r.ok ? (((await r.json()).list) || null) : null;
  } catch { PAY_METHODS = null; }
  return PAY_METHODS;
}

function renderPayMethodOptions() {
  const sel = document.getElementById("payMethod");
  if (!sel) return;
  if (!PAY_METHODS || !PAY_METHODS.length) {
    sel.innerHTML = `<option value="">付款方式加载失败,请重开弹窗</option>`;
    return;
  }
  const keep = sel.value;
  sel.innerHTML = PAY_METHODS.map(x => `<option value="${escapeAttr(x)}">${escapeHtml(x)}</option>`).join("");
  sel.value = PAY_METHODS.includes(keep) ? keep : PAY_METHODS[0];
}

// ---------- 付款方式设置(⚙️):列表增删/上下排序,billing.pay 可改 ----------
let _pmDraft = null;   // 编辑中的名单副本,保存才生效

function openPayMethodSettings() {
  _pmDraft = (PAY_METHODS || []).slice();
  const m = document.getElementById("payMethodSettingsModal");
  if (!m) return;
  setText("pmStatus", "");
  renderPayMethodSettings();
  m.hidden = false;
}

function closePayMethodSettings() {
  const m = document.getElementById("payMethodSettingsModal");
  if (m) m.hidden = true;
  _pmDraft = null;
}

function renderPayMethodSettings() {
  const box = document.getElementById("pmList");
  if (!box || !_pmDraft) return;
  box.innerHTML = _pmDraft.map((x, i) => `
    <div class="pm-row">
      <span class="pm-name">${escapeHtml(x)}</span>
      <button type="button" class="plain-button" title="上移" ${i === 0 ? "disabled" : ""} onclick="pmMove(${i},-1)">↑</button>
      <button type="button" class="plain-button" title="下移" ${i === _pmDraft.length - 1 ? "disabled" : ""} onclick="pmMove(${i},1)">↓</button>
      <button type="button" class="plain-button pm-del" title="删除「${escapeAttr(x)}」" onclick="pmRemove(${i})">删除</button>
    </div>`).join("") || `<div class="pm-empty">名单空了——至少要留一条才能保存</div>`;
}

function pmMove(i, d) {
  const j = i + d;
  if (!_pmDraft || j < 0 || j >= _pmDraft.length) return;
  [_pmDraft[i], _pmDraft[j]] = [_pmDraft[j], _pmDraft[i]];
  renderPayMethodSettings();
}

function pmRemove(i) {
  if (!_pmDraft) return;
  _pmDraft.splice(i, 1);
  renderPayMethodSettings();
}

function pmAdd() {
  const input = document.getElementById("pmNewName");
  if (!input || !_pmDraft) return;
  const name = (input.value || "").trim();
  if (!name) return;
  if (name.length > 20) { setText("pmStatus", "名字太长(超20字)"); return; }
  if (_pmDraft.includes(name)) { setText("pmStatus", "已有同名方式"); return; }
  _pmDraft.push(name);
  input.value = "";
  setText("pmStatus", "");
  renderPayMethodSettings();
}

async function pmSave() {
  if (!_pmDraft) return;
  if (!_pmDraft.length) { setText("pmStatus", "至少留一条"); return; }
  let res;
  try {
    res = await fetch("/api/settings/pay-methods", {
      method: "PUT", headers: {"Content-Type": "application/json"},
      body: JSON.stringify({list: _pmDraft}),
    });
  } catch { setText("pmStatus", "保存失败(网络异常)"); return; }
  if (!res.ok) {
    const m = await res.json().catch(() => ({}));
    setText("pmStatus", "保存失败:" + (m.detail || res.status));
    return;
  }
  PAY_METHODS = (await res.json()).list;
  renderPayMethodOptions();   // 收款下拉即时生效
  closePayMethodSettings();
}

// 多方式拆分：加一行(方式+金额)。有拆分行时提交按 methods[] 走,单方式输入忽略
function payAddSplit() {
  const wrap = document.getElementById("paySplitRows");
  if (!wrap) return;
  if (!PAY_METHODS || !PAY_METHODS.length) { window.alert("付款方式未加载,请重开收款弹窗"); return; }
  const row = document.createElement("div");
  row.className = "pay-split-row";
  const opts = PAY_METHODS.map(x => `<option value="${escapeAttr(x)}">${escapeHtml(x)}</option>`).join("");
  row.innerHTML = `<select class="pay-input pay-split-method">${opts}</select>`
    + `<input class="pay-input pay-split-amt" inputmode="decimal" placeholder="金额" oninput="paySplitUpdate()">`
    + `<button type="button" class="plain-button" onclick="this.parentNode.remove(); paySplitUpdate();">×</button>`;
  wrap.appendChild(row);
  paySplitUpdate();
}
function paySplitRows() {
  return Array.from(document.querySelectorAll("#paySplitRows .pay-split-row")).map(r => ({
    method: (r.querySelector(".pay-split-method") || {}).value || "",
    amount: parseFloat((r.querySelector(".pay-split-amt") || {}).value),
  }));
}
function paySplitUpdate() {
  const rows = paySplitRows();
  const box = document.getElementById("paySplitSum");
  const single = document.getElementById("payAmount");
  const singleMethod = document.getElementById("payMethod");
  const active = rows.length > 0;
  if (box) box.hidden = !active;
  if (single) single.disabled = active;          // 拆分时禁用单方式输入,避免歧义
  if (singleMethod) singleMethod.disabled = active;
  if (active) {
    const sum = rows.reduce((a, r) => a + (isNaN(r.amount) ? 0 : r.amount), 0);
    setText("paySplitSumVal", formatMoney(Math.round(sum * 100) / 100));
    setText("payChange", 0);   // 拆分模式不走找零,清掉单方式残留找零提示
  } else {
    payUpdateChange();
  }
}
function confirmBillPayment(billId, remaining, onDone) {
  // 一次弹窗=一个收款意图=一个请求号;弹窗内重试复用同号(后端同号同载荷重放,不落第二笔)
  payContext = {billId: billId, remaining: remaining || 0, requestId: newPaymentRequestId(),
                onDone: typeof onDone === "function" ? onDone : null};
  const m = document.getElementById("payModal");
  if (!m) return;
  // 先按已知 remaining 兜底渲染，再异步拉账单明细补全
  setText("payRemaining", remaining || 0);
  setText("paySumDue", ""); setText("paySumPaid", ""); setText("payBillNo", "—");
  setText("payChange", 0);
  const detail = document.getElementById("payDetail");
  if (detail) detail.innerHTML = "";
  const amt = document.getElementById("payAmount");
  if (amt) amt.value = (remaining && remaining > 0) ? String(remaining) : "";
  // 下拉从全店设置拉(缓存),默认选名单第一条;⚙️按付款方式管理权限显示
  loadPayMethods().then(() => renderPayMethodOptions());
  const gear = document.getElementById("payMethodGear");
  if (gear) gear.hidden = typeof canManagePaymentMethods === "function"
    && !canManagePaymentMethods();
  const cashier = document.getElementById("payCashier");
  if (cashier) { try { cashier.value = localStorage.getItem(PAY_CASHIER_LS) || ""; } catch { cashier.value = ""; } }
  // 收款经办人接人员库选人(产品决策:只放前台岗),弹窗每次打开绑定/刷新 datalist
  if (typeof bindStaffInputs === "function") bindStaffInputs(m);
  const splitRows = document.getElementById("paySplitRows");
  if (splitRows) splitRows.innerHTML = "";        // 清上次拆分行
  ["payInvoiceNo", "payInvoiceRemark", "payRemark"].forEach(id => { const e = document.getElementById(id); if (e) e.value = ""; });
  paySplitUpdate();
  const status = document.getElementById("payStatus");
  if (status) status.textContent = "";
  m.hidden = false;
  payUpdateChange();
  loadPayBillDetail(billId);
}
// 退费弹框（对标官方）：仅本地已结清单(local-bill-)。按整单(填总退款额) / 按处置(逐项勾选填额)。
async function confirmBillRefund(billId, onDone) {
  if (!billId || billId.indexOf("local-bill-") !== 0) {
    alert("仅本地账单可退费，不能退 导入单"); return;
  }
  let d;
  try { d = await fetch(`/api/bills/${encodeURIComponent(billId)}`).then(r => r.json()); }
  catch { alert("载入账单失败（网络异常）"); return; }
  if (!d || !d.bill_id) { alert("账单不存在"); return; }
  const items = d.items || [];
  const paid = Number(d.paid_fee) || 0;
  // ①:退款不再冲减 paid_fee,可退额度=已收−已退(后端派生),默认退款额/封顶都用它
  const refunded = Number(d.refunded_fee) || 0;
  const refundable = Number(d.refundable) || 0;
  const nowStr = bjNowStr();

  const ov = document.createElement("div");
  ov.className = "modal-backdrop";
  ov.innerHTML = `
    <section class="refund-modal" role="dialog" aria-modal="true" aria-label="退费">
      <div class="modal-head"><strong>退费</strong><button type="button" class="plain-button" data-rf="close">×</button></div>
      <div class="refund-scroll">
        <div class="refund-tabs">
          <button type="button" class="refund-tab active" data-rf-mode="whole">按整单</button>
          <button type="button" class="refund-tab" data-rf-mode="item">按处置</button>
        </div>
        <div class="refund-body" data-rf-body></div>
        <div class="refund-sums">应收 <b>${formatMoney(d.total_fee || 0)}</b> · 已收 <b>${formatMoney(paid)}</b> · 已退 <b>${formatMoney(refunded)}</b> · 可退 <b>${formatMoney(refundable)}</b> · 减免 <b>${formatMoney(d.discount || 0)}</b></div>
        <div class="refund-form">
          <label>退款金额* <input data-rf="amount" class="ord-input" inputmode="decimal" value="${refundable}"></label>
          <label>退款方式 <select data-rf="method" class="ord-input"><option>现金</option><option>微信</option><option>支付宝</option><option>银行卡</option><option>医保</option></select></label>
          <label>退款原因* <input data-rf="reason" class="ord-input" placeholder="必填"></label>
          <label>退费医生 <input data-rf="doctor" data-staff-role="医生" class="ord-input" placeholder="可选"></label>
          <label>退费时间 <input data-rf="time" class="ord-input" value="${escapeAttr(nowStr)}" readonly></label>
        </div>
        <div class="refund-status" data-rf="status"></div>
      </div>
      <div class="modal-actions"><button type="button" class="tooth-confirm-btn" data-rf="ok">确定</button><button type="button" class="plain-button" data-rf="cancel">取消</button></div>
    </section>`;
  document.body.appendChild(ov);
  if (typeof bindStaffInputs === "function") bindStaffInputs(ov);   // 二批:退费医生选人
  const $ = sel => ov.querySelector(sel);
  const close = () => ov.remove();
  let mode = "whole";

  const renderTable = () => {
    const head = `<tr><th>处置名称</th><th>单价</th><th>数量</th><th>总价</th><th>已收</th><th>已退</th><th>费用类型</th>${mode === "item" ? "<th>本次退款</th>" : ""}</tr>`;
    const rows = items.length ? items.map((it, i) => `
      <tr>
        <td>${escapeHtml(it.item_name || "")}</td><td>${it.unit_price || 0}</td><td>${it.quantity || 0}</td>
        <td>${formatMoney(it.gross || 0)}</td><td>${formatMoney(it.line_fee || 0)}</td><td>${formatMoney(it.refunded_fee || 0)}</td><td>${escapeHtml(it.fee_type || "")}</td>
        ${mode === "item" ? `<td><input class="ord-input rf-item-amt" data-iid="${escapeAttr(it.treatment_item_id || "")}" data-line="${it.line_fee || 0}" inputmode="decimal" style="width:90px" placeholder="可退 ${Number(((it.line_fee || 0) - (it.refunded_fee || 0)).toFixed(2))}"></td>` : ""}
      </tr>`).join("") : `<tr><td colspan="8">无明细</td></tr>`;
    $("[data-rf-body]").innerHTML = `<table class="wh-table">${head}${rows}</table>`;
    if (mode === "item") {
      ov.querySelectorAll(".rf-item-amt").forEach(inp => inp.addEventListener("input", recalc));
    }
  };
  const recalc = () => {
    if (mode !== "item") return;
    let sum = 0;
    ov.querySelectorAll(".rf-item-amt").forEach(inp => { const v = parseFloat(inp.value); if (!isNaN(v) && v > 0) sum += v; });
    $("[data-rf=amount]").value = sum ? Number(sum.toFixed(2)) : "";
  };
  renderTable();

  ov.querySelectorAll("[data-rf-mode]").forEach(btn => btn.addEventListener("click", () => {
    mode = btn.dataset.rfMode;
    ov.querySelectorAll("[data-rf-mode]").forEach(b => b.classList.toggle("active", b === btn));
    const amt = $("[data-rf=amount]");
    if (mode === "item") { amt.readOnly = true; amt.value = ""; } else { amt.readOnly = false; amt.value = refundable; }
    renderTable();
  }));
  $("[data-rf=close]").onclick = close;
  $("[data-rf=cancel]").onclick = close;
  const _rfOk = $("[data-rf=ok]");
  _rfOk.onclick = () => guardSubmit(_rfOk, async () => {   // 防双击重复退费
    const st = $("[data-rf=status]");
    const reason = $("[data-rf=reason]").value.trim();
    const method = $("[data-rf=method]").value;
    const doctor = $("[data-rf=doctor]").value.trim();
    if (!reason) { st.textContent = "退款原因必填"; return; }
    // 退费弹窗一次打开=一个请求号(挂弹窗节点,重试复用;重开弹窗=新意图新号)
    if (!ov.dataset.requestId) ov.dataset.requestId = newPaymentRequestId();
    const body = {refund_reason: reason, refund_method: method, doctor, request_id: ov.dataset.requestId};
    if (mode === "item") {
      const its = [];
      ov.querySelectorAll(".rf-item-amt").forEach(inp => {
        const v = parseFloat(inp.value);
        if (!isNaN(v) && v > 0) its.push({treatment_item_id: inp.dataset.iid, amount: v});
      });
      if (!its.length) { st.textContent = "请至少给一项填写退款金额"; return; }
      body.items = its;
    } else {
      const amount = parseFloat($("[data-rf=amount]").value);
      if (isNaN(amount) || amount <= 0) { st.textContent = "退款金额无效"; return; }
      body.refund_amount = amount;
    }
    st.textContent = "退费中...";
    let res;
    try { res = await fetch(`/api/bills/${encodeURIComponent(billId)}/refund`, {method: "POST", headers: {"Content-Type": "application/json"}, body: JSON.stringify(body)}); }
    catch { st.textContent = "退费失败（网络异常）"; return; }
    if (!res.ok) { const m = await res.json().catch(() => ({})); st.textContent = "退费失败：" + (m.detail || res.status); return; }
    close();
    if (typeof onDone === "function") { onDone(); if (typeof loadAuditLogs === "function") loadAuditLogs(); return; }
    if (typeof refreshWorkspaceDetail === "function") await refreshWorkspaceDetail();
    const page = workspacePageEl();
    const active = page && page.querySelector(".workspace-tab.active");
    const tab = active ? active.dataset.workspaceTab : "billing";
    workspaceLoadedTabs.delete(tab);
    evictVisitsCache();
    switchWorkspaceTab(tab);
    // 退费后即时刷新今日工作台统计/队列(冲减实收/现金流入),不必等手动刷新页面
    if (typeof loadTodayWork === "function") loadTodayWork();
    if (typeof loadAuditLogs === "function") await loadAuditLogs();
  });
}
function setText(id, v) { const el = document.getElementById(id); if (el) el.textContent = v; }
async function loadPayBillDetail(billId) {
  let d;
  try { d = await (await fetch(`/api/bills/${encodeURIComponent(billId)}`)).json(); }
  catch { return; }
  if (!d || !payContext || payContext.billId !== billId) return;
  setText("payBillNo", d.bill_no || "—");
  setText("paySumDue", formatMoney(d.total_fee));
  setText("paySumPaid", formatMoney(d.paid_fee));
  setText("payRemaining", formatMoney(d.remaining));
  payContext.remaining = d.remaining || 0;
  const detail = document.getElementById("payDetail");
  if (detail) {
    const rows = (d.items || []).map(it => {
      const rate = it.gross > 0 ? (it.line_fee / it.gross * 10) : 10;
      const rateTxt = (rate >= 10) ? "原价" : (rate.toFixed(1) + "折");
      return `<div class="pd-row">
        <span class="pd-name">${escapeHtml(it.item_name || "")}</span>
        <span>${formatMoney(it.unit_price)}</span>
        <span>×${escapeHtml(it.quantity)}</span>
        <span>${rateTxt}</span>
        <span>${formatMoney(it.gross)}</span>
        <span class="pd-reduce">${it.reduce > 0 ? "-" + formatMoney(it.reduce) : ""}</span>
        <span class="pd-line">${formatMoney(it.line_fee)}</span>
      </div>`;
    }).join("");
    detail.innerHTML = `<div class="pd-row pd-head"><span class="pd-name">处置</span><span>单价</span><span>数量</span><span>折</span><span>总价</span><span>减免</span><span>本次应收</span></div>${rows}`;
  }
  payUpdateChange();
}
// 找零 = max(0, 本次收款 - 欠款)
function payUpdateChange() {
  const amt = parseFloat((document.getElementById("payAmount") || {}).value);
  const rem = payContext ? (payContext.remaining || 0) : 0;
  const change = (!isNaN(amt) && amt > rem) ? (amt - rem) : 0;
  setText("payChange", formatMoney(Math.round(change * 100) / 100));
}
function closePayModal() {
  const m = document.getElementById("payModal");
  if (m) m.hidden = true;
  payContext = null;
}
async function submitBillPayment() {
  if (!payContext) return;
  const status = document.getElementById("payStatus");
  const cashier = ((document.getElementById("payCashier") || {}).value || "").trim();
  const invoice_no = ((document.getElementById("payInvoiceNo") || {}).value || "").trim();
  const invoice_remark = ((document.getElementById("payInvoiceRemark") || {}).value || "").trim();
  const remark = ((document.getElementById("payRemark") || {}).value || "").trim();
  const split = paySplitRows();
  let body;
  if (split.length > 0) {
    // 多方式拆分：逐行校验,合计不能超欠款(找零口径不适用拆分)
    for (const r of split) {
      if (!r.method) { if (status) status.textContent = "拆分行请选付款方式"; return; }
      if (isNaN(r.amount) || r.amount <= 0) { if (status) status.textContent = "拆分行金额无效"; return; }
    }
    const sum = Math.round(split.reduce((a, r) => a + r.amount, 0) * 100) / 100;
    if (sum > (payContext.remaining || 0) + 1e-6) { if (status) status.textContent = "拆分合计超过欠款"; return; }
    body = {methods: split, cashier, invoice_no, invoice_remark, remark, request_id: payContext.requestId};
  } else {
    const entered = parseFloat((document.getElementById("payAmount") || {}).value);
    if (isNaN(entered) || entered <= 0) { if (status) status.textContent = "请输入有效金额"; return; }
    const payMethod = (document.getElementById("payMethod") || {}).value || "";
    // 单方式分支与拆分分支同强度校验——付款方式加载失败时下拉只剩空值项,必须在这里挡住
    if (!payMethod) { if (status) status.textContent = "请选择付款方式"; return; }
    // 现金多收找零 → 实际入账只记欠款额，多出的是找零(不入账)
    const rem = payContext.remaining || 0;
    const amount = (rem > 0 && entered > rem) ? rem : entered;
    // 架构铁律#禁止兼容层：单方式=一行methods,与拆分同一契约(旧amount+pay_method双轨已删)
    body = {methods: [{method: payMethod, amount: amount}], cashier, invoice_no, invoice_remark, remark,
            request_id: payContext.requestId};
  }
  if (cashier) { try { localStorage.setItem(PAY_CASHIER_LS, cashier); } catch { /* ignore */ } }
  if (status) status.textContent = "收款中...";
  let res;
  try {
    res = await fetch(`/api/bills/${encodeURIComponent(payContext.billId)}/pay`, {
      method: "POST", headers: {"Content-Type": "application/json"},
      body: JSON.stringify(body),
    });
  } catch { if (status) status.textContent = "收款失败（网络异常）"; return; }
  if (!res.ok) {
    const m = await res.json().catch(() => ({}));
    if (status) status.textContent = "收款失败：" + (m.detail || res.status); return;
  }
  const onDone = payContext && payContext.onDone;
  closePayModal();
  // 模块上下文(前台收费台)用自己的 onDone 刷新；否则走患者工作区刷新
  if (onDone) { onDone(); if (typeof loadAuditLogs === "function") loadAuditLogs(); return; }
  // 收款后先重新拉患者详情(收费 tab 用 workspaceData.detail 缓存)，再重渲，避免显示旧待收金额
  if (typeof refreshWorkspaceDetail === "function") await refreshWorkspaceDetail();
  const page = workspacePageEl();
  const active = page && page.querySelector(".workspace-tab.active");
  const tab = active ? active.dataset.workspaceTab : "billing";
  workspaceLoadedTabs.delete(tab);
  evictVisitsCache();
  switchWorkspaceTab(tab);
  // 收款后即时刷新今日工作台统计/队列(收款常从今日队列发起),不必等手动刷新页面
  if (typeof loadTodayWork === "function") loadTodayWork();
  if (typeof loadAuditLogs === "function") await loadAuditLogs();
}

function renderPayment(row) {
  return `
    <div class="record-row readonly-money-row">
      <div class="money-grid">
        ${moneyField("付款时间", row.pay_time || row.updated_at)}
        ${moneyField("金额", formatMoney(row.amount))}
        ${moneyField("状态", row.state)}
        ${moneyField("收费单ID", row.bill_id)}
      </div>
    </div>
  `;
}

function moneyField(label, value) {
  return `
    <div>
      <div class="field-label">${label}</div>
      <div class="field-value">${escapeHtml(value || "")}</div>
    </div>
  `;
}

function recordsPanel(title, rows, renderer) {
  const body = rows.length ? rows.map(renderer).join("") : "0 条记录";
  return `<section class="panel"><div class="panel-head">${title}<span>${rows.length} 条</span></div><div class="panel-body">${body}</div></section>`;
}


Object.assign(window, {
  renderWorkspaceBillingTab,
  confirmBillPayment,
  openPayMethodSettings,
  closePayMethodSettings,
  pmMove,
  pmRemove,
  pmAdd,
  pmSave,
  voidBillOrder,
  payUpdateChange,
  payAddSplit,
  paySplitUpdate,
  evictVisitsCache,
  toggleBillDetail,
  closePayModal,
  submitBillPayment,
  renderBill,
  renderPayment,
  moneyField,
  recordsPanel,
});
