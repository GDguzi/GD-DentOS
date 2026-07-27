// 操作历史(审计日志)：全量浏览本地系统的所有操作动作,可按 动作关键词/操作人/日期 筛选 + 翻页。
// 数据源 /api/audit-logs(分页+筛选)。只读展示,记录由各业务操作写入 audit_logs。

// 动作码 → 中文(查不到就显原码,新动作不至于断)
const AUDIT_ACTION_LABEL = {
  create_appointment: "新增预约", update_appointment: "改预约", delete_appointment: "删预约",
  create_treatment_order: "开处置单", edit_treatment_order: "改处置单",
  price_treatment_order: "处置划价", void_treatment_order: "作废处置单",
  create_arrival_order: "到达建单",
  create_payment: "收款", confirm_payment: "确认收款", refund: "退费", refund_payment: "退费",
  membership_recharge: "会员充值", membership_consume: "会员消费", membership_refund: "会员退款",
  create_return_visit: "新增回访", update_return_visit: "改回访", soft_delete_return_visit: "删回访",
  sign_consent: "签同意书", void_consent: "作废同意书",
  upload_patient_image: "上传影像", delete_patient_image: "删影像",
  create_patient: "新建患者", update_patient: "改患者档案", merge_patient: "合并患者",
  create_backup: "数据备份", pre_sync_backup: "同步前备份",
  create_handle_item: "新增收费项目", update_handle_item: "修改收费项目",
  stop_handle_item: "停用收费项目", restore_handle_item: "恢复收费项目",
  suspect_cancelled: "对账标记疑似取消", suspect_recleared: "对账撤销疑似取消",
};

const AUDIT_ENTITY_LABEL = {
  appointment: "预约", treatment_order: "处置单", bill: "账单", payment: "收款",
  consent: "同意书", consent_document: "同意书", return_visit: "回访",
  patient_image: "影像", patient: "患者", backup: "备份", membership: "会员",
  medical_record: "病历", treatment_plan: "治疗计划", lab_order: "技工单",
  member_account: "会员", sterilize_dispatch: "消毒送消", patient_tag: "患者标签",
  referral_source: "患者来源", handle_item: "收费项目", sync_resync: "同步",
  clinic: "全店设置", rooms: "诊室设置", features: "功能开关", ai_voice: "AI语音",
  pay_methods: "付款方式",
};
// 板块筛选下拉的顺序(常用在前);值=audit_logs.entity_type 精确匹配
const AUDIT_ENTITY_OPTIONS = [
  "patient", "medical_record", "appointment", "treatment_order", "treatment_plan",
  "bill", "payment", "return_visit", "consent_document", "patient_image", "lab_order",
  "member_account", "patient_tag", "referral_source", "handle_item", "pay_methods",
  "sterilize_dispatch", "sync_resync", "backup", "clinic", "rooms", "features", "ai_voice",
];

let _auditState = {pageno: 1, pagesize: 50, q: "", operator: "", entity_type: "", date_from: "", date_to: ""};

function auditActionText(a) { return AUDIT_ACTION_LABEL[a] || a || "—"; }
function auditEntityText(t) { return AUDIT_ENTITY_LABEL[t] || t || "—"; }

function auditHandleItemChangeHtml(row) {
  if (!row || row.entity_type !== "handle_item" || !row.change) return "";
  const fields = [
    ["code", "编码"], ["name", "名称"], ["fee_type", "费用分类"],
    ["unit", "单位"], ["price", "单价"], ["stopped", "状态"],
  ];
  const snapshotHtml = (snapshot, title) => {
    if (!snapshot) return `<section><strong>${title}</strong><span>—</span></section>`;
    const cells = fields.map(([key, label]) => {
      let value = snapshot[key];
      if (key === "stopped") value = value ? "已停用" : "在用";
      if (value == null || value === "") value = "—";
      return `<span><em>${label}</em>${escapeHtml(String(value))}</span>`;
    }).join("");
    return `<section><strong>${title}</strong><div>${cells}</div></section>`;
  };
  return `
    <details class="audit-handle-change">
      <summary>查看变更前后</summary>
      <div class="audit-handle-change-grid">
        ${snapshotHtml(row.change.before, "变更前")}
        ${snapshotHtml(row.change.after, "变更后")}
      </div>
    </details>`;
}

let _auditOperators = null;   // 去重操作人缓存(进模块拉一次)

async function loadAuditLogModule() {
  // 已从顶层导航搬进配置管理"操作历史"子tab，渲染进 cfgBody(局部变量覆盖 app.js 的全局 modulePanel)。
  const modulePanel = document.getElementById("cfgBody");
  if (!modulePanel) return;
  ++moduleViewToken;
  if (_auditOperators === null) {
    try {
      const r = await fetch("/api/audit-logs/operators");
      _auditOperators = r.ok ? ((await r.json()).operators || []) : [];
    } catch { _auditOperators = []; }
  }
  const s = _auditState;
  modulePanel.innerHTML = `
    <div class="module-head">
      <strong>操作历史</strong>
      <span>本地系统所有操作的动作记录 / 可追溯</span>
    </div>
    <div class="audit-filters">
      <select id="auditEntity" title="板块" onchange="applyAuditFilter()">
        <option value="">全部板块</option>
        ${AUDIT_ENTITY_OPTIONS.map(v => `<option value="${v}"${v === s.entity_type ? " selected" : ""}>${AUDIT_ENTITY_LABEL[v]}</option>`).join("")}
      </select>
      <input id="auditQ" placeholder="动作/对象关键词" value="${escapeAttr(s.q)}" onkeydown="if(event.key==='Enter')applyAuditFilter()">
      <select id="auditOperator" title="操作人" onchange="applyAuditFilter()">
        <option value="">全部操作人</option>
        ${(_auditOperators || []).map(o => `<option value="${escapeAttr(o)}"${o === s.operator ? " selected" : ""}>${escapeHtml(o)}</option>`).join("")}
      </select>
      <input id="auditFrom" type="date" value="${escapeAttr(s.date_from)}" title="起始日期">
      <input id="auditTo" type="date" value="${escapeAttr(s.date_to)}" title="结束日期">
      <button type="button" onclick="applyAuditFilter()">筛选</button>
      <button type="button" class="plain-button" onclick="resetAuditFilter()">重置</button>
    </div>
    <section id="auditLogPanel" class="audit-log-panel">载入中...</section>
  `;
  await renderAuditLogPage();
}

function applyAuditFilter() {
  _auditState.entity_type = document.getElementById("auditEntity")?.value || "";
  _auditState.q = (document.getElementById("auditQ")?.value || "").trim();
  _auditState.operator = document.getElementById("auditOperator")?.value || "";
  _auditState.date_from = (document.getElementById("auditFrom")?.value || "").trim();
  _auditState.date_to = (document.getElementById("auditTo")?.value || "").trim();
  _auditState.pageno = 1;
  renderAuditLogPage();
}

function resetAuditFilter() {
  _auditState = {pageno: 1, pagesize: 50, q: "", operator: "", entity_type: "", date_from: "", date_to: ""};
  loadAuditLogModule();
}

function gotoAuditPage(n) { _auditState.pageno = Math.max(1, n); renderAuditLogPage(); }

async function renderAuditLogPage() {
  const panel = document.getElementById("auditLogPanel");
  if (!panel) return;
  const s = _auditState;
  const qs = new URLSearchParams({
    pageno: s.pageno, pagesize: s.pagesize,
    q: s.q, operator: s.operator, entity_type: s.entity_type,
    date_from: s.date_from, date_to: s.date_to,
  }).toString();
  let d;
  try {
    const res = await fetch(`/api/audit-logs?${qs}`);
    if (!res.ok) { panel.textContent = "操作历史载入失败"; return; }
    d = await res.json();
  } catch { panel.textContent = "操作历史载入失败"; return; }
  const list = d.list || [];
  const total = d.totalcount || 0;
  const pages = Math.max(1, Math.ceil(total / s.pagesize));
  if (!list.length) {
    panel.innerHTML = `<div class="reconcile-empty">无匹配的操作记录。</div>`;
    return;
  }
  const rows = list.map(r => `
    <div class="audit-log-item">
      <div class="audit-log-row">
        <span class="audit-time">${escapeHtml(r.created_at || "")}</span>
        <span class="audit-operator">${escapeHtml(r.operator || "—")}</span>
        <span class="audit-action">${escapeHtml(auditActionText(r.action))}</span>
        <span class="audit-entity">${escapeHtml(auditEntityText(r.entity_type))}</span>
        <span class="audit-eid" title="${escapeAttr(r.entity_id || "")}">${escapeHtml(r.entity_id || "")}</span>
      </div>
      ${auditHandleItemChangeHtml(r)}
    </div>`).join("");
  panel.innerHTML = `
    <div class="audit-log-head audit-log-row">
      <span>时间</span><span>操作人</span><span>动作</span><span>对象</span><span>对象ID</span>
    </div>
    <div class="audit-log-list">${rows}</div>
    <div class="audit-log-pager">
      <button type="button" class="plain-button" ${s.pageno <= 1 ? "disabled" : ""} onclick="gotoAuditPage(${s.pageno - 1})">上一页</button>
      <span>第 ${s.pageno} / ${pages} 页 · 共 ${total} 条</span>
      <button type="button" class="plain-button" ${s.pageno >= pages ? "disabled" : ""} onclick="gotoAuditPage(${s.pageno + 1})">下一页</button>
    </div>
  `;
}

Object.assign(window, {
  loadAuditLogModule, applyAuditFilter, resetAuditFilter, gotoAuditPage,
  renderAuditLogPage, auditActionText, auditEntityText, auditHandleItemChangeHtml,
});
