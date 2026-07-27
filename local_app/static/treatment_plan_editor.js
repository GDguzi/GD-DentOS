// 治疗计划 tab：分组录入（都做/二选一）+ 处置选择带价 + 牙位 + 转划价。
// 后端：/api/patients/{id}/treatment-plans、/treatment-plan-groups/{gid}/select、
//       /treatment-plans/{pid}/status、/treatment-plans/{pid}/bill、/api/handle-items。

let planHandleItems = null;          // 处置主数据扁平表（缓存，供 find/lookup）
let planHandleGroups = null;         // 按官方费用分类分组（供 optgroup 渲染）
let planHandleItemsGeneration = 0;   // 写操作失效后，阻止此前在途响应回写旧缓存
let planEditorModel = null;          // 新建计划编辑态：{groups:[{gtid,group_name,group_type,rows:[...]}]}
let planTmpSeq = 0;

function planTmpId(prefix) {
  planTmpSeq += 1;
  return `${prefix}-${planTmpSeq}`;
}

function invalidateHandleItemsCache() {
  planHandleItemsGeneration += 1;
  planHandleItems = null;
  planHandleGroups = null;
}

async function ensureHandleItems() {
  if (planHandleItems !== null) return planHandleItems;
  const generation = planHandleItemsGeneration;
  try {
    const res = await fetch("/api/handle-items");
    const data = await res.json();
    const flat = [];
    const groups = [];
    (data.groups || []).forEach(g => {
      const category = g.category || g.handle_type || "其他";
      const items = (g.items || []).map(it => ({
        handle_id: it.handle_id, name: it.name, price: it.price,
        code: it.code, unit: it.unit, category,
      }));
      items.forEach(it => flat.push(it));
      groups.push({category, items});
    });
    if (generation !== planHandleItemsGeneration) return ensureHandleItems();
    planHandleItems = flat;
    planHandleGroups = groups;
  } catch {
    if (generation !== planHandleItemsGeneration) return ensureHandleItems();
    planHandleItems = [];
    planHandleGroups = [];
  }
  return planHandleItems;
}

function newPlanRow() {
  return {rowid: planTmpId("row"), tooth: "", handle_id: "", item_name: "", unit_price: "", quantity: 1, note: ""};
}

function newPlanGroup(group_type = "all") {
  return {gtid: planTmpId("grp"), group_name: "", group_type, rows: [newPlanRow()]};
}

function freshPlanModel() {
  return {plan_name: "", doctor_name: "", plan_date: "", groups: [newPlanGroup("all")]};
}

async function renderWorkspaceTreatmentPlanTab(panel) {
  if (!workspaceData || !workspaceData.patient) {
    panel.textContent = "患者档案载入失败";
    return;
  }
  await ensureHandleItems();
  if (!planEditorModel) planEditorModel = freshPlanModel();
  const pid = workspaceData.patient.patient_identity;
  let listHtml = '<div class="plan-list" data-plan-list>载入中...</div>';
  panel.innerHTML = `
    <section class="panel">
      <div class="panel-head"><span>新建治疗计划</span></div>
      <div class="panel-body" data-plan-editor>${renderPlanEditor()}</div>
    </section>
    <section class="panel">
      <div class="panel-head"><span>已有治疗计划</span></div>
      <div class="panel-body">${listHtml}</div>
    </section>
  `;
  loadTreatmentPlans(pid);
}

function renderPlanRow(gi, ri, row, isAlt) {
  const teeth = String(row.tooth || "").split(/[\s,，]+/).filter(Boolean);
  return `
    <div class="plan-row" data-gi="${gi}" data-ri="${ri}">
      ${isAlt ? `<input type="radio" name="alt-pick-${gi}" title="病人选定此方案" disabled>` : ""}
      <button type="button" class="plan-tooth-cell plain-button" onclick="pickPlanRowTooth(${gi},${ri})"
              title="点击选择牙位（FDI）">
        ${teeth.length ? toothCrossHtml(teeth) : '<span class="plan-tooth-empty">选牙位</span>'}
      </button>
      <button type="button" class="plan-input plan-handle-btn" onclick="openPlanHandlePicker(${gi},${ri})">${row.item_name ? escapeHtml(row.item_name) : "选择处置"}</button>
      <input class="plan-input plan-name" data-field="item_name" value="${escapeAttr(row.item_name)}" placeholder="处置名称">
      <input class="plan-input plan-price" data-field="unit_price" value="${escapeAttr(row.unit_price)}" placeholder="金额" inputmode="decimal" oninput="planLiveTotal()">
      <input class="plan-input plan-qty" data-field="quantity" value="${escapeAttr(row.quantity)}" inputmode="numeric" oninput="planLiveTotal()">
      <input class="plan-input plan-note" data-field="note" value="${escapeAttr(row.note)}" placeholder="文字补充">
      <button type="button" class="plain-button plan-del" onclick="removePlanRow(${gi},${ri})">✕</button>
    </div>
  `;
}

function renderPlanEditor() {
  const m = planEditorModel;
  const groups = m.groups.map((g, gi) => {
    const isAlt = g.group_type === "alt";
    const rows = g.rows.map((r, ri) => renderPlanRow(gi, ri, r, isAlt)).join("");
    return `
      <div class="plan-group" data-gi="${gi}">
        <div class="plan-group-head">
          <input class="plan-input plan-group-name" data-field="group_name" data-gi="${gi}"
                 value="${escapeAttr(g.group_name)}" placeholder="分组名 如根管/补牙">
          <select class="plan-input plan-group-type" onchange="setPlanGroupType(${gi},this.value)">
            <option value="all"${!isAlt ? " selected" : ""}>都做</option>
            <option value="alt"${isAlt ? " selected" : ""}>二选一</option>
          </select>
          <button type="button" class="plain-button" onclick="removePlanGroup(${gi})">删分组</button>
        </div>
        <div class="plan-rows">${rows}</div>
        <div class="plan-group-foot">
          <button type="button" class="plain-button" onclick="addPlanRow(${gi})">+ 加一行</button>
          <span class="plan-quick">快捷：
            <button type="button" class="plain-button" onclick="addPlanQuick(${gi},'根管套餐')">根管套餐</button>
            <button type="button" class="plain-button" onclick="addPlanQuick(${gi},'桩冠修复')">桩冠修复</button>
            <button type="button" class="plain-button" onclick="addPlanQuick(${gi},'牙套')">牙套</button>
          </span>
        </div>
      </div>
    `;
  }).join("");
  return `
    <div class="plan-meta">
      <input class="plan-input" data-meta="plan_name" value="${escapeAttr(m.plan_name)}" placeholder="计划名称 如王先生6号牙">
      <input class="plan-input" data-meta="doctor_name" data-staff-role="医生" value="${escapeAttr(m.doctor_name)}" placeholder="医生">
      <input class="plan-input" data-meta="plan_date" value="${escapeAttr(m.plan_date)}" placeholder="日期 2026-06-13">
      <input class="plan-input" data-meta="category" value="${escapeAttr(m.category || "")}" placeholder="类别 如种植/正畸">
      <input class="plan-input" data-meta="label" value="${escapeAttr(m.label || "")}" placeholder="标签">
    </div>
    <div class="plan-meta plan-meta-doc">
      <input class="plan-input" data-meta="diagnosis" value="${escapeAttr(m.diagnosis || "")}" placeholder="诊断">
      <input class="plan-input" data-meta="treatment_goals" value="${escapeAttr(m.treatment_goals || "")}" placeholder="治疗目标">
      <input class="plan-input" data-meta="precautions" value="${escapeAttr(m.precautions || "")}" placeholder="注意事项">
    </div>
    ${groups}
    <div class="plan-editor-foot">
      <button type="button" class="plain-button" onclick="addPlanGroup()">+ 加分组</button>
      <span class="plan-total">合计 ${planEditorTotal()}</span>
      <button type="button" onclick="saveNewPlan()">保存计划</button>
      <span data-plan-save-status></span>
    </div>
  `;
}

// 把当前 DOM 输入同步回 model（加/删/重渲前调用，避免丢未保存输入）
function planSyncFromDom() {
  const editor = workspacePageEl() && workspacePageEl().querySelector("[data-plan-editor]");
  if (!editor || !planEditorModel) return;
  editor.querySelectorAll("[data-meta]").forEach(inp => {
    planEditorModel[inp.dataset.meta] = inp.value;
  });
  editor.querySelectorAll(".plan-group").forEach(gEl => {
    const gi = Number(gEl.dataset.gi);
    const g = planEditorModel.groups[gi];
    if (!g) return;
    const nameEl = gEl.querySelector(".plan-group-name");
    if (nameEl) g.group_name = nameEl.value;
    gEl.querySelectorAll(".plan-row").forEach(rEl => {
      const ri = Number(rEl.dataset.ri);
      const row = g.rows[ri];
      if (!row) return;
      rEl.querySelectorAll("[data-field]").forEach(inp => {
        row[inp.dataset.field] = inp.value;
      });
    });
  });
}

function rerenderPlanEditor() {
  const editor = workspacePageEl() && workspacePageEl().querySelector("[data-plan-editor]");
  if (editor) editor.innerHTML = renderPlanEditor();
  if (editor && typeof bindStaffInputs === "function") bindStaffInputs(editor);   // 二批:计划医生选人
}

function planSyncAndRerender() {
  planSyncFromDom();
  rerenderPlanEditor();
}

// 金额/数量逐字输入时：只同步模型 + 更新合计，不整体重渲（避免输入框失焦/光标丢失）
function planLiveTotal() {
  planSyncFromDom();
  const el = workspacePageEl() && workspacePageEl().querySelector(".plan-total");
  if (el) el.textContent = `合计 ${planEditorTotal()}`;
}

// 换患者时清空未保存的计划草稿（避免跨患者残留/误录）
function resetTreatmentPlanEditor() {
  planEditorModel = null;
}

function planEditorTotal() {
  let total = 0;
  (planEditorModel.groups || []).forEach(g => {
    g.rows.forEach(r => {
      const price = parseFloat(r.unit_price);
      const qty = parseInt(r.quantity, 10);
      if (!isNaN(price)) total += price * (isNaN(qty) ? 1 : qty);
    });
  });
  return total;
}

// 复用病历页官方 FDI 牙位弹窗（medical_editor.js），确定后写回该行
function pickPlanRowTooth(gi, ri) {
  planSyncFromDom();
  const row = planEditorModel.groups[gi] && planEditorModel.groups[gi].rows[ri];
  if (!row) return;
  openToothSelectorWith(row.tooth, teeth => {
    row.tooth = (teeth || []).join(",");
    rerenderPlanEditor();
  });
}

// 治疗计划行点"选择处置"→打开处置列表弹框，选中回填该行
function openPlanHandlePicker(gi, ri) {
  planSyncFromDom();
  const row = planEditorModel.groups[gi] && planEditorModel.groups[gi].rows[ri];
  if (!row) return;
  openHandlePicker(it => {
    row.handle_id = it.handle_id;
    row.item_name = it.name;
    if (it.price != null) row.unit_price = it.price;
    rerenderPlanEditor();
  });
}

// ===== 处置列表选择器（单独弹框：左侧费用分类 + 搜索 + 带编码/单位/单价的列表）=====
let hpOnPick = null;
let hpCategory = "全部";
let hpSearch = "";

async function openHandlePicker(onPick) {
  hpOnPick = onPick || null;
  hpCategory = "全部";
  hpSearch = "";
  const m = document.getElementById("handlePickerModal");
  if (!m) return;
  await ensureHandleItems();
  const search = document.getElementById("hpSearch");
  if (search) search.value = "";
  renderHandlePickerCats();
  renderHandlePickerList();
  m.hidden = false;
}

function closeHandlePicker() {
  const m = document.getElementById("handlePickerModal");
  if (m) m.hidden = true;
  hpOnPick = null;
}

function renderHandlePickerCats() {
  const box = document.getElementById("hpCats");
  if (!box) return;
  const cats = ["全部", ...(planHandleGroups || []).map(g => g.category)];
  box.innerHTML = cats.map(c =>
    `<button type="button" class="hp-cat${c === hpCategory ? " active" : ""}" onclick="setHandlePickerCat('${escapeAttr(c)}')">${escapeHtml(c)}</button>`
  ).join("");
}

function setHandlePickerCat(c) {
  hpCategory = c;
  renderHandlePickerCats();
  renderHandlePickerList();
}

function onHandlePickerSearch(v) {
  hpSearch = String(v || "").trim().toLowerCase();
  renderHandlePickerList();
}

function renderHandlePickerList() {
  const box = document.getElementById("hpList");
  if (!box) return;
  let items = [];
  (planHandleGroups || []).forEach(g => {
    if (hpCategory !== "全部" && g.category !== hpCategory) return;
    g.items.forEach(it => items.push(it));
  });
  if (hpSearch) {
    items = items.filter(it =>
      String(it.name || "").toLowerCase().includes(hpSearch) ||
      String(it.code || "").toLowerCase().includes(hpSearch));
  }
  const shown = items.slice(0, 300);
  box.innerHTML = `
    <div class="hp-row hp-head"><span class="hp-name">处置名称</span><span>编码</span><span>单位</span><span class="hp-price">单价</span></div>
    ${shown.map(it => `
      <div class="hp-row" onclick="pickHandleItem('${escapeAttr(it.handle_id)}')">
        <span class="hp-name">${escapeHtml(it.name)}</span>
        <span>${escapeHtml(it.code || "")}</span>
        <span>${escapeHtml(it.unit || "")}</span>
        <span class="hp-price">${it.price != null ? it.price : ""}</span>
      </div>`).join("")}
    ${items.length > 300 ? `<div class="hp-more">仅显示前 300 条，请用搜索缩小范围（共 ${items.length} 条）</div>` : ""}
    ${!items.length ? '<div class="hp-more">无匹配处置</div>' : ""}
  `;
}

function pickHandleItem(handleId) {
  const it = (planHandleItems || []).find(h => h.handle_id === handleId);
  const cb = hpOnPick;
  closeHandlePicker();
  if (cb && it) cb(it);
}

function setPlanGroupType(gi, type) {
  planSyncFromDom();
  if (planEditorModel.groups[gi]) planEditorModel.groups[gi].group_type = type;
  rerenderPlanEditor();
}

function addPlanRow(gi) {
  planSyncFromDom();
  if (planEditorModel.groups[gi]) planEditorModel.groups[gi].rows.push(newPlanRow());
  rerenderPlanEditor();
}

function removePlanRow(gi, ri) {
  planSyncFromDom();
  const g = planEditorModel.groups[gi];
  if (g && g.rows.length > 1) g.rows.splice(ri, 1);
  rerenderPlanEditor();
}

function addPlanGroup() {
  planSyncFromDom();
  planEditorModel.groups.push(newPlanGroup("all"));
  rerenderPlanEditor();
}

function removePlanGroup(gi) {
  planSyncFromDom();
  if (planEditorModel.groups.length > 1) planEditorModel.groups.splice(gi, 1);
  rerenderPlanEditor();
}

// 快捷套餐：常见后续处置，一键加一行（名称匹配处置主数据带出价格）
function addPlanQuick(gi, name) {
  planSyncFromDom();
  const g = planEditorModel.groups[gi];
  if (!g) return;
  const row = newPlanRow();
  const hit = (planHandleItems || []).find(h => h.name && h.name.includes(name));
  if (hit) {
    row.handle_id = hit.handle_id;
    row.item_name = hit.name;
    if (hit.price != null) row.unit_price = hit.price;
  } else {
    row.item_name = name;
  }
  g.rows.push(row);
  rerenderPlanEditor();
}

async function saveNewPlan() {
  planSyncFromDom();
  const status = workspacePageEl().querySelector("[data-plan-save-status]");
  const m = planEditorModel;
  const groups = m.groups
    .map(g => ({
      group_name: g.group_name,
      group_type: g.group_type,
      items: g.rows
        .filter(r => String(r.item_name || "").trim())
        .map(r => ({
          item_name: r.item_name,
          handle_id: r.handle_id,
          tooth: r.tooth,
          quantity: parseInt(r.quantity, 10) || 1,
          unit_price: r.unit_price === "" ? null : parseFloat(r.unit_price),
          note: r.note,
        })),
    }))
    .filter(g => g.items.length);
  if (!String(m.plan_name || "").trim()) { if (status) status.textContent = "请填计划名称"; return; }
  if (!groups.length) { if (status) status.textContent = "至少填一个处置项"; return; }
  if (status) status.textContent = "保存中...";
  const pid = workspaceData.patient.patient_identity;
  let res;
  try {
    res = await fetch(`/api/patients/${encodeURIComponent(pid)}/treatment-plans`, {
      method: "POST",
      headers: {"Content-Type": "application/json"},
      body: JSON.stringify({
        plan_name: m.plan_name, doctor_name: m.doctor_name,
        plan_date: m.plan_date, groups,
        category: m.category || "", label: m.label || "",
        diagnosis: m.diagnosis || "", treatment_goals: m.treatment_goals || "",
        precautions: m.precautions || "",
      }),
    });
  } catch {
    if (status) status.textContent = "保存失败（网络异常）"; return;
  }
  if (!res.ok) { if (status) status.textContent = "保存失败"; return; }
  planEditorModel = freshPlanModel();
  renderWorkspaceTreatmentPlanTab(workspacePageEl().querySelector('[data-workspace-panel="plan"]'));
  await loadAuditLogs();
}

async function loadTreatmentPlans(pid) {
  const box = workspacePageEl() && workspacePageEl().querySelector("[data-plan-list]");
  if (!box) return;
  let data;
  try {
    const res = await fetch(`/api/patients/${encodeURIComponent(pid)}/treatment-plans`);
    // await 期间可能切到别的患者，晚到响应不能写进当前面板/_loadedPlansById(跨患者文书错配)
    if (typeof workspacePatientId !== "undefined" && pid !== workspacePatientId) return;
    if (!res.ok) { box.textContent = "载入失败"; return; }
    data = await res.json();
    if (typeof workspacePatientId !== "undefined" && pid !== workspacePatientId) return;
  } catch {
    if (typeof workspacePatientId !== "undefined" && pid !== workspacePatientId) return;
    box.textContent = "载入失败（网络异常）"; return;
  }
  const plans = data.plans || [];
  _loadedPlansById = {};
  plans.forEach(p => { _loadedPlansById[p.plan_id] = p; });   // P1-7 打印方案书用
  if (!plans.length) { box.innerHTML = '<span class="empty">暂无治疗计划</span>'; return; }
  box.innerHTML = plans.map(renderExistingPlan).join("");
}

let _loadedPlansById = {};   // P1-7 当前患者已载入计划，按 plan_id 查

function renderPlanItemLine(it, isAlt, selected) {
  const teeth = String(it.tooth || "").split(/[\s,，]+/).filter(Boolean);
  const mark = isAlt ? (selected === it.item_id ? "● " : "○ ") : "";
  return `
    <div class="plan-view-item">
      <span>${escapeHtml(mark)}</span>
      ${teeth.length ? toothCrossHtml(teeth) : ""}
      <span class="field-value">${escapeHtml(it.item_name)}</span>
      <span class="plan-view-price">${it.total_price != null ? it.total_price : (it.unit_price != null ? it.unit_price : "")}</span>
      ${it.note ? `<span class="plan-view-note">${escapeHtml(it.note)}</span>` : ""}
    </div>
  `;
}

function renderExistingPlan(p) {
  const ungrouped = (p.items || []).map(it => renderPlanItemLine(it, false, "")).join("");
  const groups = (p.groups || []).map(g => {
    const isAlt = g.group_type === "alt";
    const lines = (g.items || []).map(it => {
      const line = renderPlanItemLine(it, isAlt, g.selected_item_id);
      if (isAlt) {
        return line.replace(
          '<div class="plan-view-item">',
          `<div class="plan-view-item plan-view-pick" onclick="selectPlanAlt('${escapeAttr(g.group_id)}','${escapeAttr(it.item_id)}','${escapeAttr(p.plan_id)}')" title="选定此方案">`
        );
      }
      return line;
    }).join("");
    return `
      <div class="plan-view-group">
        <div class="plan-view-group-head">${escapeHtml(g.group_name || "(未命名分组)")}
          <span class="plan-view-tag">${isAlt ? "二选一" : "都做"}</span></div>
        ${lines}
      </div>
    `;
  }).join("");
  const statusText = {draft: "草稿", confirmed: "已确认", done: "已完成", withdrawn: "已撤回"}[p.status] || p.status;
  const actions = [];
  if (p.status === "draft") actions.push(`<button type="button" onclick="confirmPlanThen('${escapeAttr(p.plan_id)}')">确认方案</button>`);
  if (p.status === "confirmed") actions.push(`<button type="button" onclick="openPlanBillDialog('${escapeAttr(p.plan_id)}', ${p.total_price || 0})">转划价</button>`);
  actions.push(`<button type="button" class="plain-button" onclick="printTreatmentPlan('${escapeAttr(p.plan_id)}')">打印方案书</button>`);
  actions.push(`<button type="button" class="plain-button plan-del-btn" onclick="deleteTreatmentPlan('${escapeAttr(p.plan_id)}')">删除</button>`);
  return `
    <div class="plan-card" data-plan-id="${escapeAttr(p.plan_id)}">
      <div class="plan-card-head">
        <strong>${escapeHtml(p.plan_name)}</strong>
        <span class="plan-view-tag">${escapeHtml(statusText)}</span>
        ${p.doctor_name ? `<span class="muted">${escapeHtml(p.doctor_name)}</span>` : ""}
        <span class="plan-card-total">合计 ${p.total_price != null ? p.total_price : 0}</span>
      </div>
      ${ungrouped}${groups}
      <div class="plan-card-actions">${actions.join("")}<span data-plan-action-status="${escapeAttr(p.plan_id)}"></span></div>
    </div>
  `;
}

async function deleteTreatmentPlan(planId) {
  if (!window.confirm("删除该诊疗计划？（软删，保留留痕可追溯；已转划价的账单不受影响）")) return;
  const status = workspacePageEl().querySelector(`[data-plan-action-status="${cssEscape(planId)}"]`);
  if (status) status.textContent = "删除中...";
  let res;
  try {
    res = await fetch(`/api/treatment-plans/${encodeURIComponent(planId)}`, {method: "DELETE"});
  } catch { if (status) status.textContent = "删除失败（网络异常）"; return; }
  if (!res.ok) { const m = await res.json().catch(() => ({})); if (status) status.textContent = "删除失败：" + (m.detail || res.status); return; }
  loadTreatmentPlans(workspaceData.patient.patient_identity);
  if (typeof loadAuditLogs === "function") await loadAuditLogs();
}

async function selectPlanAlt(groupId, itemId, planId) {
  try {
    await fetch(`/api/treatment-plan-groups/${encodeURIComponent(groupId)}/select`, {
      method: "POST", headers: {"Content-Type": "application/json"},
      body: JSON.stringify({item_id: itemId}),
    });
  } catch { /* 忽略，下面刷新会反映真实状态 */ }
  loadTreatmentPlans(workspaceData.patient.patient_identity);
}

async function confirmPlanThen(planId) {
  const status = workspacePageEl().querySelector(`[data-plan-action-status="${cssEscape(planId)}"]`);
  try {
    const res = await fetch(`/api/treatment-plans/${encodeURIComponent(planId)}/status`, {
      method: "POST", headers: {"Content-Type": "application/json"},
      body: JSON.stringify({status: "confirmed"}),
    });
    if (!res.ok) { if (status) status.textContent = "确认失败"; return; }
  } catch { if (status) status.textContent = "确认失败（网络异常）"; return; }
  loadTreatmentPlans(workspaceData.patient.patient_identity);
  await loadAuditLogs();
}

async function openPlanBillDialog(planId, total) {
  const discountStr = await appPrompt(`转划价：生成待收费单（前台再收费）。\n合计 ${total}。\n整单优惠金额（无则填0）：`, "0");
  if (discountStr === null) return;
  const discount = parseFloat(discountStr) || 0;
  const status = workspacePageEl().querySelector(`[data-plan-action-status="${cssEscape(planId)}"]`);
  if (status) status.textContent = "划价中...";
  let res;
  try {
    res = await fetch(`/api/treatment-plans/${encodeURIComponent(planId)}/bill`, {
      method: "POST", headers: {"Content-Type": "application/json"},
      body: JSON.stringify({discount}),
    });
  } catch { if (status) status.textContent = "划价失败（网络异常）"; return; }
  if (!res.ok) {
    const msg = await res.json().catch(() => ({}));
    if (status) status.textContent = "划价失败：" + (msg.detail || res.status);
    return;
  }
  const body = await res.json();
  if (status) status.textContent = `已生成待收费单 ${body.total_fee} 元（前台收费确认）`;
  loadTreatmentPlans(workspaceData.patient.patient_identity);
  await loadAuditLogs();
}

// P1-7：诊疗方案书套打——复用 consent_form 的 window.open 打印模式，把计划字段填进方案书版式
function _planPrintRows(p) {
  const rows = [];
  const line = (it, mark) => {
    const price = it.total_price != null ? it.total_price : (it.unit_price != null ? it.unit_price : "");
    rows.push(`<tr><td>${escapeHtml(mark)}${escapeHtml(it.item_name || "")}</td>`
      + `<td>${escapeHtml(it.tooth || "")}</td><td style="text-align:center">${escapeHtml(String(it.quantity ?? ""))}</td>`
      + `<td style="text-align:right">${escapeHtml(String(price))}</td>`
      + `<td>${escapeHtml(it.note || "")}</td></tr>`);
  };
  (p.items || []).forEach(it => line(it, ""));
  (p.groups || []).forEach(g => {
    const isAlt = g.group_type === "alt";
    rows.push(`<tr class="grp"><td colspan="5"><b>${escapeHtml(g.group_name || "(未命名分组)")}</b>　${isAlt ? "（二选一）" : "（都做）"}</td></tr>`);
    (g.items || []).forEach(it => line(it, isAlt ? (g.selected_item_id === it.item_id ? "● " : "○ ") : ""));
  });
  return rows.join("");
}

function printTreatmentPlan(planId) {
  const p = _loadedPlansById[planId];
  if (!p) return;
  const patient = (workspaceData && workspaceData.patient) || {};
  const pname = patient.display_name || patient.name || "";
  // 计划日期为空时用本地日期(localDateValue)，别用 UTC toISOString 否则中国时区凌晨打印偏前一天
  const date = p.plan_date || (typeof localDateValue === "function" ? localDateValue() : "");
  const meta2 = [];
  if (p.category) meta2.push(`类别：${escapeHtml(p.category)}`);
  if (p.label) meta2.push(`标签：${escapeHtml(p.label)}`);
  const sec = (label, val) => val ? `<div class="sec"><b>${label}：</b>${escapeHtml(val)}</div>` : "";
  const w = window.open("", "_blank", "width=800,height=900");
  if (!w) return;
  w.document.write(`<html><head><meta charset="utf-8"><title>${escapeHtml(p.plan_name || "诊疗方案书")}</title>
    <style>body{font-family:"宋体",serif;padding:24px;line-height:1.7;font-size:14px}h2{text-align:center;margin:0 0 12px}
    .meta{margin-bottom:8px}.meta2{color:#444;margin-bottom:8px}
    .sec{margin:8px 0;white-space:pre-wrap}
    table{border-collapse:collapse;width:100%;margin:10px 0}th,td{border:1px solid #999;padding:4px 8px;font-size:13px}
    th{background:#f2f2f2}tr.grp td{background:#fafafa}
    .total{font-weight:bold;text-align:right;margin-top:6px}
    .signs{display:flex;gap:40px;margin-top:36px}.sign{flex:1}.sign-line{border-bottom:1px solid #333;height:60px}</style></head><body>
    <h2>诊疗方案书</h2>
    <div class="meta">患者：${escapeHtml(pname)}　医生：${escapeHtml(p.doctor_name || "")}　日期：${escapeHtml(date)}　方案：${escapeHtml(p.plan_name || "")}</div>
    ${meta2.length ? `<div class="meta2">${meta2.join("　")}</div>` : ""}
    ${sec("诊断", p.diagnosis)}
    ${sec("治疗目标", p.treatment_goals)}
    <table><thead><tr><th>项目</th><th>牙位</th><th>数量</th><th>单价/金额</th><th>备注</th></tr></thead>
    <tbody>${_planPrintRows(p)}</tbody></table>
    <div class="total">合计：${p.total_price != null ? p.total_price : 0} 元</div>
    ${sec("注意事项", p.precautions)}
    ${p.note ? sec("备注", p.note) : ""}
    <div class="signs">
      <div class="sign">患者签名：<div class="sign-line"></div></div>
      <div class="sign">医生签名：<div class="sign-line"></div></div>
    </div></body></html>`);
  w.document.close();
  w.focus();
  setTimeout(() => { try { w.print(); } catch (e) { /* ignore */ } }, 300);
}

Object.assign(window, {
  invalidateHandleItemsCache,
  renderWorkspaceTreatmentPlanTab,
  resetTreatmentPlanEditor,
  planSyncAndRerender,
  planLiveTotal,
  pickPlanRowTooth,
  openPlanHandlePicker,
  openHandlePicker,
  closeHandlePicker,
  setHandlePickerCat,
  onHandlePickerSearch,
  pickHandleItem,
  setPlanGroupType,
  addPlanRow,
  removePlanRow,
  addPlanGroup,
  removePlanGroup,
  addPlanQuick,
  saveNewPlan,
  selectPlanAlt,
  confirmPlanThen,
  openPlanBillDialog,
  printTreatmentPlan,
  deleteTreatmentPlan,
});
