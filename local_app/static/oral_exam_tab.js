// 口腔检查 tab：检查头(日期/医生/健康程度/复查/备注) + 逐牙发现(牙位/症状/程度/建议处理/备注) + 列表。
// 对接 GET/POST /api/patients/{id}/oral-exams。复用 openToothSelectorWith(FDI牙位)。

const OE_HEALTH = ["", "健康", "良好", "一般", "不佳"];
const OE_SEVERITY = ["", "较轻", "中等", "严重"];

let oeModel = null;
let oeSeq = 0;
function oeNewFinding() { oeSeq += 1; return {fid: "oe-" + oeSeq, tooth: "", symptom: "", severity: "", suggest_time: "", note: ""}; }
function oeFresh() { return {exam_date: "", exam_doctor: "", health_level: "", next_followup: "", note: "", findings: [oeNewFinding()]}; }

function renderWorkspaceOralExamTab(panel) {
  const identity = workspacePatientId;
  panel.textContent = "加载中...";
  fetch(`/api/patients/${encodeURIComponent(identity)}/oral-exams`).then(r => r.json()).then(data => {
    if (identity !== workspacePatientId) return;
    oeModel = oeFresh();
    const list = (data.exams || []).map(oeViewCard).join("");
    panel.innerHTML = `
      <section class="panel"><div class="panel-head"><span>新增口腔检查</span></div>
        <div class="panel-body" data-oe-form>${renderOeForm()}</div></section>
      <section class="panel"><div class="panel-head"><span>口腔检查记录</span></div>
        <div class="panel-body">${list || '<span class="empty">暂无口腔检查</span>'}</div></section>`;
    if (typeof bindStaffInputs === "function") bindStaffInputs(panel);   // #420二批:检查医生选人
  }).catch(() => { if (identity === workspacePatientId) panel.textContent = "口腔检查载入失败"; });
}

function renderOeForm() {
  const m = oeModel;
  const findings = m.findings.map((f, i) => {
    const teeth = String(f.tooth || "").split(/[\s,，]+/).filter(Boolean);
    return `
      <div class="oe-finding" data-fi="${i}">
        <button type="button" class="oe-tooth plain-button" onclick="oePickTooth(${i})" title="选牙位">${teeth.length ? toothCrossHtml(teeth) : '<span class="ord-tooth-empty">选牙位</span>'}</button>
        <input class="cs-input" data-f="symptom" value="${escapeAttr(f.symptom)}" placeholder="症状">
        <select class="cs-input" data-f="severity">${OE_SEVERITY.map(s => `<option value="${s}"${f.severity === s ? " selected" : ""}>${s || "程度"}</option>`).join("")}</select>
        <input class="cs-input" data-f="suggest_time" value="${escapeAttr(f.suggest_time)}" placeholder="建议处理">
        <input class="cs-input" data-f="note" value="${escapeAttr(f.note)}" placeholder="备注">
        <button type="button" class="plain-button ord-del" onclick="oeRemoveFinding(${i})">✕</button>
      </div>`;
  }).join("");
  return `
    <div class="cs-grid">
      <label>检查日期 <input class="cs-input" data-meta="exam_date" value="${escapeAttr(m.exam_date)}"></label>
      <label>检查医生 <input class="cs-input" data-meta="exam_doctor" data-staff-role="医生" value="${escapeAttr(m.exam_doctor)}"></label>
      <label>口腔健康 <select class="cs-input" data-meta="health_level">${OE_HEALTH.map(h => `<option value="${h}"${m.health_level === h ? " selected" : ""}>${h || "（未评）"}</option>`).join("")}</select></label>
      <label>复查时间 <input class="cs-input" data-meta="next_followup" value="${escapeAttr(m.next_followup)}"></label>
    </div>
    <label class="cs-wide">备注 <input class="cs-input" data-meta="note" value="${escapeAttr(m.note)}"></label>
    <div class="oe-findings-head">逐牙发现</div>
    <div class="oe-findings">${findings}</div>
    <div class="cs-actions"><button type="button" class="plain-button" onclick="oeAddFinding()">+ 加发现</button>
      <button type="button" onclick="saveOralExam()">保存口腔检查</button><span data-oe-status></span></div>`;
}

function oeSyncFromDom() {
  const form = workspacePageEl() && workspacePageEl().querySelector("[data-oe-form]");
  if (!form || !oeModel) return;
  form.querySelectorAll("[data-meta]").forEach(i => { oeModel[i.dataset.meta] = i.value; });
  form.querySelectorAll(".oe-finding").forEach(fEl => {
    const f = oeModel.findings[Number(fEl.dataset.fi)];
    if (f) fEl.querySelectorAll("[data-f]").forEach(i => { f[i.dataset.f] = i.value; });
  });
}
function oeRerender() { oeSyncFromDom(); const f = workspacePageEl().querySelector("[data-oe-form]"); if (f) { f.innerHTML = renderOeForm(); if (typeof bindStaffInputs === "function") bindStaffInputs(f); } }   // #420二批:检查医生选人
function oeAddFinding() { oeSyncFromDom(); oeModel.findings.push(oeNewFinding()); oeRerender(); }
function oeRemoveFinding(i) { oeSyncFromDom(); if (oeModel.findings.length > 1) oeModel.findings.splice(i, 1); oeRerender(); }
function oePickTooth(i) {
  oeSyncFromDom();
  const f = oeModel.findings[i]; if (!f) return;
  openToothSelectorWith(f.tooth, teeth => { f.tooth = (teeth || []).join(","); oeRerender(); });
}

async function saveOralExam() {
  oeSyncFromDom();
  const status = workspacePageEl().querySelector("[data-oe-status]");
  const findings = oeModel.findings.filter(f => String(f.symptom || "").trim()).map(f => ({
    tooth: f.tooth, symptom: f.symptom, severity: f.severity, suggest_time: f.suggest_time, note: f.note,
  }));
  const payload = {
    exam_date: oeModel.exam_date, exam_doctor: oeModel.exam_doctor, health_level: oeModel.health_level,
    next_followup: oeModel.next_followup, note: oeModel.note, findings,
  };
  if (!findings.length && !oeModel.health_level && !String(oeModel.note || "").trim()) {
    if (status) status.textContent = "请至少填一条发现或健康程度"; return;
  }
  if (status) status.textContent = "保存中...";
  let res;
  try {
    res = await fetch(`/api/patients/${encodeURIComponent(workspacePatientId)}/oral-exams`, {
      method: "POST", headers: {"Content-Type": "application/json"}, body: JSON.stringify(payload),
    });
  } catch { if (status) status.textContent = "保存失败（网络异常）"; return; }
  if (!res.ok) { const m = await res.json().catch(() => ({})); if (status) status.textContent = "保存失败：" + (m.detail || res.status); return; }
  workspaceLoadedTabs.delete("oral-exam");
  switchWorkspaceTab("oral-exam");
  if (typeof loadAuditLogs === "function") await loadAuditLogs();
}

function oeViewCard(e) {
  const findings = (e.findings || []).map(f => {
    const teeth = String(f.tooth || "").split(/[\s,，]+/).filter(Boolean);
    return `<div class="oe-view-finding">${teeth.length ? toothCrossHtml(teeth) : ""}<span class="field-value">${escapeHtml(f.symptom || "")}</span>${f.severity ? `<span class="plan-view-tag">${escapeHtml(f.severity)}</span>` : ""}${f.suggest_time ? `<span class="muted">${escapeHtml(f.suggest_time)}</span>` : ""}${f.note ? `<span class="oe-fnote">${escapeHtml(f.note)}</span>` : ""}</div>`;
  }).join("");
  return `
    <div class="cs-card">
      <div class="cs-card-head"><strong>${escapeHtml(e.exam_date || "口腔检查")}</strong>
        ${e.health_level ? `<span class="plan-view-tag">${escapeHtml(e.health_level)}</span>` : ""}
        ${e.exam_doctor ? `<span class="muted">${escapeHtml(e.exam_doctor)}</span>` : ""}
        ${e.next_followup ? `<span class="muted">复查 ${escapeHtml(e.next_followup)}</span>` : ""}</div>
      ${findings}${e.note ? `<div class="cs-view-row"><span class="field-label">备注</span><span class="field-value">${escapeHtml(e.note)}</span></div>` : ""}
    </div>`;
}

Object.assign(window, {
  renderWorkspaceOralExamTab, oeAddFinding, oeRemoveFinding, oePickTooth, saveOralExam,
});
