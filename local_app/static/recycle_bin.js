// 回收站：列出软删/作废记录(谁、何时删) + 还原。
// 已从顶层导航搬进配置管理"回收站"子tab，渲染进 cfgBody(局部变量覆盖 app.js 的全局 modulePanel)。
async function loadRecycleBin() {
  const modulePanel = document.getElementById("cfgBody");
  if (!modulePanel) return;
  modulePanel.innerHTML = `<div class="module-loading">回收站载入中...</div>`;
  let data;
  try { data = await (await fetch("/api/recycle-bin")).json(); }
  catch { modulePanel.innerHTML = `<div class="module-loading">回收站载入失败</div>`; return; }
  const rows = (data.items || []).map(it => `
    <div class="rb-row">
      <span class="rb-kind">${escapeHtml(it.kind || "")}</span>
      <span class="rb-title">${escapeHtml(it.title || "")}${it.subtitle ? ` <small class="muted">${escapeHtml(it.subtitle)}</small>` : ""}</span>
      <span>${escapeHtml(it.deleted_by || "—")}</span>
      <span class="muted">${escapeHtml(it.deleted_at || "")}</span>
      <span><button type="button" class="plain-button" onclick="rbRestore('${escapeAttr(it.entity_type)}','${escapeAttr(it.entity_id)}')">还原</button></span>
    </div>`).join("");
  modulePanel.innerHTML = `
    <div class="rb-panel">
      <div class="rb-bar">已删除 / 作废的记录，可还原（共 ${data.totalcount || 0} 条）。账单作废请到收费管理处理。</div>
      <div class="rb-row rb-head-row"><span>类型</span><span>内容</span><span>删除人</span><span>时间</span><span>操作</span></div>
      ${rows || '<div class="module-empty">回收站是空的</div>'}
    </div>`;
}

async function rbRestore(entityType, entityId) {
  if (!window.confirm("确认还原这条记录？还原后会重新出现在原模块。")) return;
  let res;
  try {
    res = await fetch("/api/recycle-bin/restore", {method: "POST", headers: {"Content-Type": "application/json"},
      body: JSON.stringify({entity_type: entityType, entity_id: entityId})});
  } catch { window.alert("还原失败（网络异常）"); return; }
  if (!res.ok) { const m = await res.json().catch(() => ({})); window.alert("还原失败：" + (m.detail || res.status)); return; }
  loadRecycleBin();
}

window.loadRecycleBin = loadRecycleBin;
window.rbRestore = rbRestore;
