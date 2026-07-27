// 面诊/咨询沟通 + 手术记录 两个 tab：列表展示 + 新增表单。对接已有后端
// GET/POST /api/patients/{id}/consults 与 /surgeries。

// ---------- 通用：tab 载入(竞态/网络守卫，复用 workspace_tabs 范式) ----------
async function loadSimpleTab(panel, urlPath, render, failText) {
  const identity = workspacePatientId;
  // 面诊作为「客户沟通」子tab:记下起始子tab,await 期间若切走(含失败态)不写回共享 body。
  // 手术是顶层 tab(panel 无 data-cs-cur)→ sub0=undefined,stale 恒 false,行为不变。
  const sub0 = panel.dataset ? panel.dataset.csCur : undefined;
  const stale = () => sub0 !== undefined && panel.dataset.csCur !== sub0;
  panel.textContent = "加载中...";
  let data;
  try {
    const res = await fetch(`/api/patients/${encodeURIComponent(identity)}/${urlPath}`);
    if (identity !== workspacePatientId || stale()) return;
    if (!res.ok) { workspaceLoadedTabs.delete(urlPath === "consults" ? "consult" : "surgery"); panel.textContent = failText; return; }
    data = await res.json();
    if (identity !== workspacePatientId || stale()) return;
  } catch {
    if (identity !== workspacePatientId || stale()) return;
    panel.textContent = failText; return;
  }
  render(panel, data);
}

function field(label, name, value, wide) {
  return `<label class="${wide ? "cs-wide" : ""}"><span>${label}</span>
    <input class="cs-input" data-field="${escapeAttr(name)}" value="${escapeAttr(value || "")}"></label>`;
}
function area(label, name) {
  return `<label class="cs-wide"><span>${label}</span>
    <textarea class="cs-input cs-area" data-field="${escapeAttr(name)}"></textarea></label>`;
}

// ---------- 面诊 / 咨询沟通 ----------
function renderWorkspaceConsultTab(panel) {
  loadSimpleTab(panel, "consults", (p, data) => {   // 过期守卫统一在 loadSimpleTab(含失败态),此处只管渲染
    const list = (data.consults || []).map(c => `
      <div class="cs-card">
        <div class="cs-card-head"><strong>${escapeHtml(c.consult_date || "未填日期")}</strong>
          ${c.consult_type ? `<span class="plan-view-tag">${escapeHtml(c.consult_type)}</span>` : ""}
          ${c.consult_doctor ? `<span class="muted">${escapeHtml(c.consult_doctor)}</span>` : ""}</div>
        ${csRow("主诉", c.chief_complaint)}${csRow("方案", c.plan)}${csRow("沟通", c.communication)}
        ${csRow("成交意向", c.potential)}${csRow("建议", c.advice)}
      </div>`).join("");
    p.innerHTML = `
      <section class="panel"><div class="panel-head"><span>新增面诊</span></div>
        <div class="panel-body cs-form" data-cs-form="consult">
          <div class="cs-grid">
            ${field("面诊日期", "consult_date", "")}${field("咨询医生", "consult_doctor", "")}${field("面诊类型", "consult_type", "")}
          </div>
          ${area("主诉", "chief_complaint")}${area("方案", "plan")}${area("沟通", "communication")}
          ${field("成交意向", "potential", "", true)}${area("建议", "advice")}
          <div class="cs-actions"><button type="button" onclick="saveConsult()">保存面诊</button><span data-cs-status></span></div>
        </div></section>
      <section class="panel"><div class="panel-head"><span>面诊记录</span></div>
        <div class="panel-body">${list || '<span class="empty">暂无面诊记录</span>'}</div></section>`;
  }, "面诊记录载入失败");
}

function csRow(label, value) {
  if (!value) return "";
  return `<div class="cs-view-row"><span class="field-label">${label}</span><span class="field-value">${escapeHtml(value)}</span></div>`;
}

async function saveConsult() { await saveSimpleForm("consult", "consults", refreshConsultSubtab); }

// 面诊已并入「客户沟通」子tab:保存成功后就地重渲面诊子页(不再 switchWorkspaceTab("consult")——
// 顶层已无该tab,会被静默退回基本资料且父级 return-visits 缓存不刷新)。
function refreshConsultSubtab() {
  const page = (typeof workspacePageEl === "function") ? workspacePageEl() : null;
  const body = page && page.querySelector("[data-cs-sub-body]");
  if (body && typeof renderWorkspaceConsultTab === "function") {
    body.dataset.csCur = "面诊";
    renderWorkspaceConsultTab(body);
  }
}

// ---------- 手术记录 ----------
function renderWorkspaceSurgeryTab(panel) {
  loadSimpleTab(panel, "surgeries", (p, data) => {
    const list = (data.surgeries || []).map(s => `
      <div class="cs-card">
        <div class="cs-card-head"><strong>${escapeHtml(s.surgery_name || "手术")}</strong>
          ${s.surgery_date ? `<span class="muted">${escapeHtml(s.surgery_date)}</span>` : ""}
          ${s.surgeon ? `<span class="muted">${escapeHtml(s.surgeon)}</span>` : ""}
          ${s.tooth ? toothCrossHtml(String(s.tooth).split(/[\s,，]+/).filter(Boolean)) : ""}</div>
        ${csRow("麻醉", s.anesthesia)}${csRow("手术过程", s.process)}${csRow("术后医嘱", s.postop_advice)}
      </div>`).join("");
    p.innerHTML = `
      <section class="panel"><div class="panel-head"><span>新增手术记录</span></div>
        <div class="panel-body cs-form" data-cs-form="surgery">
          <div class="cs-grid">
            ${field("手术日期", "surgery_date", "")}${field("术者", "surgeon", "")}${field("手术名称*", "surgery_name", "")}${field("牙位", "tooth", "")}
          </div>
          ${field("麻醉", "anesthesia", "", true)}${area("手术过程", "process")}${area("术后医嘱", "postop_advice")}
          <div class="cs-actions"><button type="button" onclick="saveSurgery()">保存手术记录</button><span data-cs-status></span></div>
        </div></section>
      <section class="panel"><div class="panel-head"><span>手术记录</span></div>
        <div class="panel-body">${list || '<span class="empty">暂无手术记录</span>'}</div></section>`;
  }, "手术记录载入失败");
}

async function saveSurgery() { await saveSimpleForm("surgery", "surgeries"); }

// ---------- 通用保存 ----------
// onSaved: 保存成功后的刷新方式。手术仍是顶层 tab → 默认 switchWorkspaceTab 重载;
// 面诊已并入子tab → 传 refreshConsultSubtab 就地重渲(见 )。
async function saveSimpleForm(tab, urlPath, onSaved) {
  const page = workspacePageEl();
  const form = page && page.querySelector(`[data-cs-form="${tab}"]`);
  if (!form) return;
  const status = form.querySelector("[data-cs-status]");
  const payload = {};
  form.querySelectorAll("[data-field]").forEach(i => { payload[i.dataset.field] = i.value.trim(); });
  if (status) status.textContent = "保存中...";
  const pid = workspacePatientId;
  let res;
  try {
    res = await fetch(`/api/patients/${encodeURIComponent(pid)}/${urlPath}`, {
      method: "POST", headers: {"Content-Type": "application/json"}, body: JSON.stringify(payload),
    });
  } catch { if (status) status.textContent = "保存失败（网络异常）"; return; }
  if (!res.ok) {
    const m = await res.json().catch(() => ({}));
    if (status) status.textContent = "保存失败：" + (m.detail || res.status); return;
  }
  if (typeof onSaved === "function") {
    onSaved();
  } else {
    workspaceLoadedTabs.delete(tab);
    switchWorkspaceTab(tab);
  }
  if (typeof loadAuditLogs === "function") await loadAuditLogs();
}

Object.assign(window, {
  renderWorkspaceConsultTab, renderWorkspaceSurgeryTab, saveConsult, saveSurgery,
});
