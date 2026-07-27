/* 今日全量工作检查 —— 点击式逐条核验面板
 * 每个维度（预约/处置/账单/回访）列出全量条目，自动标核验状态：
 *   ✅ 正常  ⚠️ 警告  ❌ 缺失/错误
 * 点击条目可展开详情 */
let latestInspectionData = null;

async function loadTodayInspection() {
  if (!todayWorkPanel) return;
  todayWorkPanel.textContent = "全量检查载入中...";
  try {
    const res = await fetch(`/api/today-inspection?date=${encodeURIComponent(workDate.value)}`);
    if (!res.ok) {
      todayWorkPanel.textContent = "全量检查载入失败";
      return;
    }
    const data = await res.json();
    latestInspectionData = data;
    renderInspection(data);
  } catch {
    todayWorkPanel.textContent = "全量检查载入失败";
  }
}

function renderInspection(data) {
  const s = data.summary;
  const hasAudit = Boolean(s && typeof s === "object");
  const hasBilling = Object.prototype.hasOwnProperty.call(data, "bills");
  const appointments = data.appointments || [];
  const orders = data.orders || [];
  const bills = hasBilling ? (data.bills || []) : [];
  const returnVisits = data.return_visits || [];
  const walkins = data.walkins || [];
  const total = hasAudit
    ? (Number(s.appointments) || 0) + (Number(s.orders) || 0) + (Number(s.bills) || 0)
    : appointments.length + orders.length + bills.length + returnVisits.length + walkins.length;
  const cleanRate = hasAudit && total > 0
    ? Math.round((Number(s.ok) || 0) / total * 100)
    : 100;
  const summaryHtml = hasAudit ? `
        <div class="inspection-summary-strip">
          <div class="inspection-stat ${s.ok > 0 ? 'ok' : ''}">
            <span class="insp-stat-num">${s.ok}</span><span class="insp-stat-label">✅正常</span>
          </div>
          <div class="inspection-stat ${s.warn > 0 ? 'warn' : ''}">
            <span class="insp-stat-num">${s.warn}</span><span class="insp-stat-label">⚠️警告</span>
          </div>
          <div class="inspection-stat ${s.missing > 0 ? 'error' : ''}">
            <span class="insp-stat-num">${s.missing}</span><span class="insp-stat-label">❌缺失</span>
          </div>
          <div class="inspection-stat ${s.error > 0 ? 'error' : ''}">
            <span class="insp-stat-num">${s.error}</span><span class="insp-stat-label">💥金额异常</span>
          </div>
          <div class="inspection-stat total">
            <span class="insp-stat-num">${total}</span><span class="insp-stat-label">总计</span>
          </div>
          <div class="inspection-stat clean">
            <span class="insp-stat-num">${cleanRate}%</span><span class="insp-stat-label">健康度</span>
          </div>
        </div>
        <div class="inspection-quick-stats">
          预约${s.appointments} &nbsp; 处置${s.orders}
          ${hasBilling ? ` &nbsp; 账单${s.bills}` : ''}
          &nbsp; 回访到期${s.return_visits_due}
          ${s.walkins > 0 ? ` &nbsp; 无预约到店${s.walkins}` : ''}
        </div>` : `
        <div class="inspection-summary-strip">
          <div class="inspection-stat total">
            <span class="insp-stat-num">${total}</span><span class="insp-stat-label">总计</span>
          </div>
        </div>
        <div class="inspection-quick-stats">
          预约${appointments.length} &nbsp; 处置${orders.length}
          ${hasBilling ? ` &nbsp; 账单${bills.length}` : ''}
          &nbsp; 回访到期${returnVisits.length}
          ${walkins.length > 0 ? ` &nbsp; 无预约到店${walkins.length}` : ''}
        </div>`;
  const checkSuffix = hasAudit ? "检查" : "";

  todayWorkPanel.innerHTML = `
    <div class="inspection-dashboard">
      <div class="inspection-header">
        <div class="inspection-date-nav">
          <button type="button" class="today-date-arrow" onclick="changeInspectionDate(-1)">‹</button>
          <span class="inspection-date-label"><strong>${escapeHtml(inspectionDateLabel())}</strong> 全量工作检查</span>
          <button type="button" class="today-date-arrow" onclick="changeInspectionDate(1)">›</button>
          <button type="button" class="today-date-jump" onclick="changeInspectionDate(0)">今天</button>
          <button type="button" class="plain-button" onclick="loadTodayInspection()" style="margin-left:8px">刷新</button>
        </div>
        ${summaryHtml}
      </div>

      ${renderInspectionSection(`📋 预约${checkSuffix}`, appointments, renderAppointmentItem, 'appointments', hasAudit)}
      ${renderInspectionSection(`🩺 处置单${checkSuffix}`, orders, renderOrderItem, 'orders', hasAudit)}
      ${hasBilling ? renderInspectionSection(`💰 账单${checkSuffix}`, bills, renderBillItem, 'bills', hasAudit) : ''}
      ${renderInspectionSection(`📞 回访${checkSuffix}`, returnVisits, renderReturnVisitItem, 'return_visits', hasAudit)}
      ${renderWalkinSection(walkins, hasAudit, hasBilling)}
    </div>
  `;
}

function inspectionDateLabel() {
  const d = dateFromWorkValue(workDate.value);
  const wk = ["周日","周一","周二","周三","周四","周五","周六"][d.getDay()];
  const tag = workDate.value === localDateValue() ? " · 今天" : "";
  return `${d.getFullYear()}年${d.getMonth()+1}月${d.getDate()}日 ${wk}${tag}`;
}

function changeInspectionDate(offset) {
  changeWorkDate(offset);
  loadTodayInspection();
}

function renderInspectionSection(title, items, renderFn, sectionId, hasAudit) {
  if (!items.length) return "";
  let auditCounts = "";
  if (hasAudit) {
    const okCount = items.filter(i => i.overall === "ok").length;
    const warnCount = items.filter(i => i.overall === "warn").length;
    const errCount = items.filter(i => i.overall === "missing" || i.overall === "error").length;
    auditCounts = `
          <span class="count-ok">✅${okCount}</span>
          ${warnCount > 0 ? `<span class="count-warn">⚠️${warnCount}</span>` : ""}
          ${errCount > 0 ? `<span class="count-error">❌${errCount}</span>` : ""}`;
  }

  return `
    <section class="inspection-section" id="insp-${sectionId}">
      <div class="inspection-section-head">
        <span class="inspection-section-title">${title}</span>
        <span class="inspection-section-counts">
          ${auditCounts}
          <span class="count-total">共${items.length}条</span>
        </span>
      </div>
      <div class="inspection-list">
        ${items.map((item, idx) => renderFn(item, idx, sectionId, hasAudit)).join("")}
      </div>
    </section>
  `;
}

function renderWalkinSection(walkins, hasAudit, hasBilling) {
  if (!walkins.length) return "";
  return `
    <section class="inspection-section" id="insp-walkins">
      <div class="inspection-section-head ${hasAudit ? 'bg-warn-soft' : ''}">
        <span class="inspection-section-title">🚶 无预约到店</span>
        <span class="inspection-section-counts">
          ${hasAudit ? `<span class="count-warn">⚠️${walkins.length}</span>` : ''}
          <span class="count-total">共${walkins.length}条</span>
        </span>
      </div>
      <div class="inspection-list">
        ${walkins.map(w => `
          <div class="insp-item walkin ${hasAudit ? 'warn' : ''}" onclick="openPatientWorkspace('${escapeAttr(w.patient_identity)}')">
            ${hasAudit ? '<span class="insp-item-status warn">⚠️</span>' : ''}
            <span class="insp-item-name">${escapeHtml(w.display_name || "")}</span>
            <span class="insp-item-phone">${escapeHtml(w.phone || "")}</span>
            <span class="insp-item-tag">当天有处置/病历${hasBilling ? '/收费' : ''}但无预约记录</span>
          </div>
        `).join("")}
      </div>
    </section>
  `;
}

function inspectionAuditParts(item, hasAudit) {
  if (!hasAudit || !item.overall) {
    return { statusCls: "", statusHtml: "", checksHtml: "" };
  }
  const icon = {ok: "✅", warn: "⚠️", missing: "❌", error: "💥"}[item.overall];
  if (!icon) return { statusCls: "", statusHtml: "", checksHtml: "" };
  const checks = Array.isArray(item.checks) ? item.checks : [];
  const renderedChecks = checks.map(c => {
    const checkIcon = {ok: "✅", warn: "⚠️", missing: "❌", error: "💥"}[c.status] || "";
    return `<span class="insp-check ${c.status}">${checkIcon} ${escapeHtml(c.check || "")}${c.detail ? ': ' + escapeHtml(c.detail) : ''}</span>`;
  }).join("");
  return {
    statusCls: item.overall,
    statusHtml: `<span class="insp-item-status">${icon}</span>`,
    checksHtml: renderedChecks
      ? `<div class="insp-item-checks" hidden>${renderedChecks}</div>`
      : "",
  };
}

/* ─── 预约条目 ─── */
function renderAppointmentItem(item, idx, sectionId, hasAudit) {
  const audit = inspectionAuditParts(item, hasAudit);

  return `
    <div class="insp-item ${audit.statusCls}" ${audit.checksHtml ? 'onclick="toggleInspectionDetail(this)"' : ''} data-patient-id="${escapeAttr(item.patient_identity)}">
      <div class="insp-item-row">
        ${audit.statusHtml}
        <span class="insp-item-name">${escapeHtml(item.display_name || "—")}</span>
        <span class="insp-item-time">${escapeHtml((item.start_time || "").slice(11, 16))}</span>
        <span class="insp-item-detail">${escapeHtml(item.doctor_name || "")} · ${escapeHtml(item.item_name || "")}</span>
        <span class="insp-item-tag">${escapeHtml(item.visit_type || "")} ${escapeHtml(item.room || "")}</span>
        <button class="insp-link-btn" onclick="event.stopPropagation();openPatientWorkspace('${escapeAttr(item.patient_identity)}')">→患者</button>
      </div>
      ${audit.checksHtml}
    </div>
  `;
}

/* ─── 处置单条目 ─── */
function renderOrderItem(item, idx, sectionId, hasAudit) {
  const audit = inspectionAuditParts(item, hasAudit);
  const orderStatusLabel = {recorded: "待划价", priced: "待收费", paid: "已收费", voided: "已撤销"};
  const financeParts = [];
  if (item.status != null && item.status !== "") {
    financeParts.push(orderStatusLabel[item.status] || escapeHtml(item.status));
  }
  if (item.item_total != null) financeParts.push(`合计${item.item_total}元`);
  const financeHtml = financeParts.length
    ? `<span class="insp-item-tag">${financeParts.join(" ")}</span>`
    : "";

  return `
    <div class="insp-item ${audit.statusCls}" ${audit.checksHtml ? 'onclick="toggleInspectionDetail(this)"' : ''} data-patient-id="${escapeAttr(item.patient_identity)}">
      <div class="insp-item-row">
        ${audit.statusHtml}
        <span class="insp-item-name">${escapeHtml(item.display_name || "—")}</span>
        <span class="insp-item-order-no">${escapeHtml(item.order_no || "")}</span>
        <span class="insp-item-detail">${escapeHtml(item.doctor_name || "")} · ${escapeHtml(item.diagnosis || "")}</span>
        ${financeHtml}
        <button class="insp-link-btn" onclick="event.stopPropagation();openPatientWorkspace('${escapeAttr(item.patient_identity)}')">→患者</button>
      </div>
      ${item.item_summary ? `<div class="insp-item-items">${escapeHtml(item.item_summary)}</div>` : ""}
      ${audit.checksHtml}
    </div>
  `;
}

/* ─── 账单条目 ─── */
function renderBillItem(item, idx, sectionId, hasAudit) {
  const audit = inspectionAuditParts(item, hasAudit);
  const unpaidText = item.unpaid_fee != null
    ? (item.unpaid_fee > 0.005
        ? `<span class="txt-danger">欠${item.unpaid_fee}</span>`
        : `<span class="txt-ok">已结清</span>`)
    : "——";

  return `
    <div class="insp-item ${audit.statusCls}" ${audit.checksHtml ? 'onclick="toggleInspectionDetail(this)"' : ''} data-patient-id="${escapeAttr(item.patient_identity)}">
      <div class="insp-item-row">
        ${audit.statusHtml}
        <span class="insp-item-name">${escapeHtml(item.display_name || "—")}</span>
        <span class="insp-item-bill-no">${escapeHtml(item.bill_no || "")}</span>
        <span class="insp-item-detail">${escapeHtml(item.items || "")}</span>
        <span class="insp-item-tag">应收${item.total_fee != null ? item.total_fee : '——'} 实收${item.paid_fee != null ? item.paid_fee : '——'} ${unpaidText}</span>
        <button class="insp-link-btn" onclick="event.stopPropagation();openPatientWorkspace('${escapeAttr(item.patient_identity)}')">→患者</button>
      </div>
      ${audit.checksHtml}
    </div>
  `;
}

/* ─── 回访条目 ─── */
function renderReturnVisitItem(item, idx, sectionId, hasAudit) {
  const audit = inspectionAuditParts(item, hasAudit);
  const visitStatus = hasAudit
    ? (item.overall === "ok" ? "已回访" : "待回访")
    : escapeHtml(item.status || "");

  return `
    <div class="insp-item ${audit.statusCls}" data-patient-id="${escapeAttr(item.patient_identity)}">
      <div class="insp-item-row">
        ${audit.statusHtml}
        <span class="insp-item-name">${escapeHtml(item.display_name || "—")}</span>
        <span class="insp-item-detail">${escapeHtml(item.item_name || "")}</span>
        <span class="insp-item-tag">${visitStatus} ${item.actual_date ? '实际' + escapeHtml(item.actual_date) : ''}</span>
        <button class="insp-link-btn" onclick="event.stopPropagation();openPatientWorkspace('${escapeAttr(item.patient_identity)}')">→患者</button>
      </div>
      ${item.note ? `<div class="insp-item-note">${escapeHtml(item.note)}</div>` : ""}
    </div>
  `;
}

/* ─── 点击展开/收起详情 ─── */
function toggleInspectionDetail(el) {
  const checks = el.querySelector(".insp-item-checks");
  if (!checks) return;
  checks.hidden = !checks.hidden;
  el.classList.toggle("insp-item-expanded", !checks.hidden);
}
