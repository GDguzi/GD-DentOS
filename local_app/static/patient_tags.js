// 患者专属标签(精品路线·大客户经营)：档案侧栏显示 + 增删。VIP/怕疼/偏好/忌讳等。
// 预设快捷标签(名称+颜色)，也可自定义。
const PT_PRESETS = [
  ["VIP", "red"], ["大客户", "orange"], ["老客户介绍", "purple"],
  ["怕疼", "blue"], ["偏好下午", "green"], ["需提醒", "gray"],
];

// 就近取标签容器(全屏页/患者管理内嵌页两种宿主),避免全局 id 串患者
function _railTagsBox() {
  const page = (typeof workspacePageEl === "function") ? workspacePageEl() : null;
  return (page && page.querySelector) ? page.querySelector("#railTags") : document.getElementById("railTags");
}

async function loadPatientTags(pid) {
  if (!pid || !_railTagsBox()) return;
  let list = [];
  try { list = (await (await fetch(`/api/patients/${encodeURIComponent(pid)}/tags`)).json()).list || []; } catch { list = []; }
  // 切患者守卫:异步返回时已不是当前患者→丢弃,不把 A 的标签写进 B 的侧栏
  if (typeof workspacePatientId !== "undefined" && pid !== workspacePatientId) return;
  const box = _railTagsBox();   // 重新就近取:可能已被新患者重渲染
  if (!box) return;
  box.innerHTML =
    list.map(t => `<span class="pt-chip pt-${escapeAttr(t.color)}">${escapeHtml(t.name)}` +
      `<button type="button" class="pt-del" data-tag-del="${escapeAttr(t.tag_id)}" title="删除标签">×</button></span>`).join("") +
    `<button type="button" class="pt-add" data-tag-add>+ 标签</button>`;
  box.querySelectorAll("[data-tag-del]").forEach(b =>
    b.addEventListener("click", () => removePatientTag(b.dataset.tagDel, pid)));
  const addBtn = box.querySelector("[data-tag-add]");
  if (addBtn) addBtn.addEventListener("click", () => openTagPicker(pid));
}

function openTagPicker(pid) {
  closeTagPicker();
  const box = _railTagsBox();
  if (!box) return;
  const panel = document.createElement("div");
  panel.className = "pt-picker";
  panel.id = "ptPicker";
  panel.innerHTML = `
    <div class="pt-picker-presets">
      ${PT_PRESETS.map(([n, c]) =>
        `<button type="button" class="pt-chip pt-${c}" data-preset="${escapeAttr(n)}" data-color="${c}">${escapeHtml(n)}</button>`).join("")}
    </div>
    <div class="pt-picker-custom">
      <input type="text" class="ord-input" id="ptCustom" placeholder="自定义(≤12字)" maxlength="12" autocomplete="off">
      <button type="button" class="tooth-confirm-btn" id="ptCustomAdd">加</button>
      <button type="button" class="plain-button" id="ptCancel">取消</button>
    </div>`;
  box.appendChild(panel);
  panel.querySelectorAll("[data-preset]").forEach(b =>
    b.addEventListener("click", () => addPatientTag(pid, b.dataset.preset, b.dataset.color)));
  const ci = panel.querySelector("#ptCustomAdd");
  if (ci) ci.addEventListener("click", () => {
    const v = ((document.getElementById("ptCustom") || {}).value || "").trim();
    if (v) addPatientTag(pid, v, "gray");
  });
  const cancel = panel.querySelector("#ptCancel");
  if (cancel) cancel.addEventListener("click", closeTagPicker);
  const inp = panel.querySelector("#ptCustom");
  if (inp) { inp.focus(); inp.addEventListener("keydown", e => { if (e.key === "Enter" && inp.value.trim()) addPatientTag(pid, inp.value.trim(), "gray"); }); }
}

function closeTagPicker() {
  const p = document.getElementById("ptPicker");
  if (p) p.remove();
}

async function addPatientTag(pid, name, color) {
  try {
    const res = await fetch(`/api/patients/${encodeURIComponent(pid)}/tags`,
      {method: "POST", headers: {"Content-Type": "application/json"}, body: JSON.stringify({name, color})});
    if (!res.ok) { const d = await res.json().catch(() => ({})); window.alert(d.detail || "添加失败"); return; }
  } catch { window.alert("添加失败（网络异常）"); return; }
  closeTagPicker();
  loadPatientTags(pid);
}

async function removePatientTag(tagId, pid) {
  try {
    const res = await fetch(`/api/patient-tags/${encodeURIComponent(tagId)}`, {method: "DELETE"});
    if (!res.ok) { window.alert("删除失败"); return; }
  } catch { window.alert("删除失败（网络异常）"); return; }
  loadPatientTags(pid);
}
