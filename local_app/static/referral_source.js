// 患者来源三级树：级联选择器(Miller 三栏) + 来源设置页(增删改/上移下移/隐藏)
// 对齐官方"患者来源设置"。选择结果写入目标 input 的 value(路径"一级/二级/三级")。

let _refTreeCache = null;

async function loadReferralTree(force) {
  if (_refTreeCache && !force) return _refTreeCache;
  let rows = [];
  try { rows = (await (await fetch("/api/referral-sources")).json()).list || []; } catch { rows = []; }
  const byId = {}, childrenOf = {};
  rows.forEach(r => { byId[r.source_id] = r; (childrenOf[r.parent_id || ""] = childrenOf[r.parent_id || ""] || []).push(r); });
  Object.values(childrenOf).forEach(a => a.sort((x, y) => (x.display_order - y.display_order) || x.name.localeCompare(y.name)));
  _refTreeCache = { rows, byId, childrenOf };
  return _refTreeCache;
}

function _refChildren(tree, parentId) { return tree.childrenOf[parentId || ""] || []; }

// 把路径字符串(名字/名字/名字)解析成 source_id 数组(尽力匹配)
function _refPathToIds(tree, path) {
  const names = String(path || "").split("/").map(s => s.trim()).filter(Boolean);
  const ids = []; let parent = "";
  for (const nm of names) {
    const hit = _refChildren(tree, parent).find(n => n.name === nm);
    if (!hit) break;
    ids.push(hit.source_id); parent = hit.source_id;
  }
  return ids;
}

// ---------- 级联选择器 ----------
let _refPickEl = null, _refPickInput = "", _refPickSel = [], _refPickSearch = "";

function closeRefPicker() {
  if (_refPickEl) { _refPickEl.remove(); _refPickEl = null; _refPickInput = ""; _refPickSel = []; _refPickSearch = ""; }
  document.removeEventListener("mousedown", _refPickOutside);
}
function _refPickOutside(e) { if (_refPickEl && !_refPickEl.contains(e.target)) closeRefPicker(); }

async function openRefPicker(inputId, anchor) {
  const wasSame = _refPickInput === inputId;
  closeRefPicker();
  if (wasSame) return;            // 再点一次=收起
  const tree = await loadReferralTree();
  _refPickInput = inputId;
  _refPickSearch = "";
  _refPickSel = _refPathToIds(tree, (document.getElementById(inputId) || {}).value || "");
  const el = document.createElement("div");
  el.className = "ref-pick";
  document.body.appendChild(el);
  _refPickEl = el;
  renderRefPicker(tree);
  // 定位:锚点下方优先;但靠近视口底部会被截断,故把弹层夹紧在视口内(整块可见,#来源选择器被遮)
  const r = (anchor || document.getElementById(inputId)).getBoundingClientRect();
  const m = 8;
  const w = el.offsetWidth, h = el.offsetHeight;
  el.style.left = Math.max(m, Math.min(r.left, window.innerWidth - w - m)) + "px";
  let top = r.bottom + 4;
  top = Math.min(top, window.innerHeight - h - m);   // 不让底部超出视口(向上提)
  top = Math.max(top, m);                            // 也不让顶部超出视口
  el.style.top = top + "px";
  setTimeout(() => document.addEventListener("mousedown", _refPickOutside), 0);
}

function _refPathNames(tree) { return _refPickSel.map(id => (tree.byId[id] || {}).name).filter(Boolean); }

function renderRefPicker(tree) {
  if (!_refPickEl) return;
  const kw = _refPickSearch.trim();
  let body;
  if (kw) {
    // 搜索态：跨级平铺匹配，点结果选中其完整路径
    const hits = tree.rows.filter(r => !r.hidden && r.name.includes(kw)).slice(0, 60);
    body = `<div class="ref-pick-search-list">${
      hits.length ? hits.map(r => {
        const path = _refIdToPath(tree, r.source_id);
        return `<button type="button" class="ref-pick-hit" data-refpath="${escapeAttr(path)}">${escapeHtml(path)}</button>`;
      }).join("") : '<div class="ref-pick-empty">无匹配</div>'}</div>`;
  } else {
    const cols = [];
    let parent = "";
    for (let lvl = 0; lvl < 3; lvl++) {
      const items = _refChildren(tree, parent).filter(n => !n.hidden);
      if (lvl > 0 && !items.length) break;
      const selId = _refPickSel[lvl] || "";
      cols.push(`<div class="ref-pick-col">${
        items.length ? items.map(n =>
          `<button type="button" class="ref-pick-node${n.source_id === selId ? " sel" : ""}${_refChildren(tree, n.source_id).length ? " has-child" : ""}" data-refid="${escapeAttr(n.source_id)}" data-reflvl="${lvl}">${escapeHtml(n.name)}</button>`
        ).join("") : '<div class="ref-pick-empty">—</div>'}</div>`);
      if (!selId) break;
      parent = selId;
    }
    body = `<div class="ref-pick-cols">${cols.join("")}</div>`;
  }
  const pathTxt = _refPathNames(tree).join(" / ") || "未选择";
  _refPickEl.innerHTML = `
    <div class="ref-pick-head">
      <input class="ref-pick-search" placeholder="输入关键字搜索" value="${escapeAttr(_refPickSearch)}" oninput="onRefPickSearch(this.value)">
    </div>
    ${body}
    <div class="ref-pick-foot">
      <span class="ref-pick-path">${escapeHtml(pathTxt)}</span>
      <span class="ref-pick-actions">
        <button type="button" class="plain-button" onclick="clearRefPick()">清除</button>
        <button type="button" class="tooth-confirm-btn" onclick="confirmRefPick()">确定</button>
      </span>
    </div>`;
  _refPickEl.querySelectorAll("[data-refid]").forEach(b => b.addEventListener("click", () => {
    const lvl = Number(b.dataset.reflvl);
    _refPickSel = _refPickSel.slice(0, lvl).concat(b.dataset.refid);
    renderRefPicker(tree);
  }));
  _refPickEl.querySelectorAll("[data-refpath]").forEach(b => b.addEventListener("click", () => {
    _refSetValue(b.dataset.refpath); closeRefPicker();
  }));
}

function _refIdToPath(tree, sid) {
  const names = []; let cur = tree.byId[sid];
  while (cur) { names.unshift(cur.name); cur = cur.parent_id ? tree.byId[cur.parent_id] : null; }
  return names.join("/");
}

function onRefPickSearch(v) { _refPickSearch = v || ""; loadReferralTree().then(renderRefPicker); }

function clearRefPick() { _refSetValue(""); closeRefPicker(); }

async function confirmRefPick() {
  const tree = await loadReferralTree();
  _refSetValue(_refPathNames(tree).join("/"));
  closeRefPicker();
}

function _refSetValue(path) {
  const inp = document.getElementById(_refPickInput);
  if (inp) inp.value = path;
  const disp = document.querySelector(`[data-ref-display="${_refPickInput}"]`);
  if (disp) disp.textContent = path || "请选择患者来源";
  if (disp) disp.classList.toggle("placeholder", !path);
}

// 渲染一个来源选择槽(隐藏 input 存路径 + 可点显示块)
function referralSlotHtml(inputId, label) {
  return `
    <label class="appt-field">${escapeHtml(label)}
      <span class="ref-slot">
        <button type="button" class="ref-slot-display placeholder" data-ref-display="${inputId}" onclick="openRefPicker('${inputId}', this)">请选择患者来源</button>
        <input type="hidden" id="${inputId}">
      </span>
    </label>`;
}

// ---------- 来源设置页(管理三级树) ----------
let _refSetSel = ["", ""];   // 选中的[一级id, 二级id]

async function openReferralSettings() {
  let m = document.getElementById("refSettingsModal");
  if (!m) { m = document.createElement("div"); m.id = "refSettingsModal"; m.className = "modal-backdrop"; document.body.appendChild(m); }
  m.hidden = false;
  _refSetSel = ["", ""];
  await renderReferralSettings();
}
function closeReferralSettings() { const m = document.getElementById("refSettingsModal"); if (m) m.hidden = true; }

async function renderReferralSettings() {
  const m = document.getElementById("refSettingsModal");
  if (!m) return;
  const tree = await loadReferralTree(true);
  const col = (title, parentId, lvlIdx) => {
    const items = _refChildren(tree, parentId);
    const selId = _refSetSel[lvlIdx];
    return `
      <div class="ref-set-col">
        <div class="ref-set-col-head"><span>${title}</span></div>
        <div class="ref-set-list">
          ${items.map(n => `
            <div class="ref-set-row${n.source_id === selId ? " sel" : ""}${n.hidden ? " hidden-item" : ""}" data-refset-pick="${escapeAttr(n.source_id)}" data-refset-lvl="${lvlIdx}">
              <span class="ref-set-name">${escapeHtml(n.name)}</span>
              <span class="ref-set-ops">
                <button type="button" title="上移" data-refset-move="up" data-refset-id="${escapeAttr(n.source_id)}">↑</button>
                <button type="button" title="下移" data-refset-move="down" data-refset-id="${escapeAttr(n.source_id)}">↓</button>
                <button type="button" title="改名" data-refset-edit="${escapeAttr(n.source_id)}">改</button>
                <button type="button" title="${n.hidden ? "显示" : "隐藏"}" data-refset-hide="${escapeAttr(n.source_id)}" data-refset-hidden="${n.hidden}">${n.hidden ? "显" : "隐"}</button>
                <button type="button" title="删除" class="danger" data-refset-del="${escapeAttr(n.source_id)}">删</button>
              </span>
            </div>`).join("") || '<div class="ref-pick-empty">暂无</div>'}
        </div>
        ${lvlIdx === 0 || _refSetSel[lvlIdx - 1] ? `
        <div class="ref-set-add">
          <input class="ord-input" placeholder="新增${title}" data-refset-addinput="${lvlIdx}">
          <button type="button" class="plain-button" data-refset-add="${lvlIdx}">+ 添加</button>
        </div>` : ""}
      </div>`;
  };
  m.innerHTML = `
    <section class="appt-modal ref-set-modal" role="dialog" aria-modal="true" aria-label="患者来源设置">
      <div class="modal-head"><strong>患者来源设置</strong><button type="button" class="plain-button" onclick="closeReferralSettings()">×</button></div>
      <div class="ref-set-body">
        ${col("一级来源", "", 0)}
        ${col("二级来源", _refSetSel[0], 1)}
        ${col("三级来源", _refSetSel[1], 2)}
      </div>
      <div class="modal-actions"><span id="refSetStatus" class="today-refresh-status"></span><button type="button" class="plain-button" onclick="closeReferralSettings()">关闭</button></div>
    </section>`;
  bindReferralSettings();
}

function bindReferralSettings() {
  const m = document.getElementById("refSettingsModal");
  if (!m) return;
  m.querySelectorAll("[data-refset-pick]").forEach(row => row.addEventListener("click", ev => {
    if (ev.target.closest("[data-refset-move],[data-refset-edit],[data-refset-hide],[data-refset-del]")) return;
    const lvl = Number(row.dataset.refsetLvl);
    _refSetSel[lvl] = row.dataset.refsetPick;
    if (lvl === 0) _refSetSel[1] = "";
    renderReferralSettings();
  }));
  m.querySelectorAll("[data-refset-add]").forEach(btn => btn.addEventListener("click", async () => {
    const lvl = Number(btn.dataset.refsetAdd);
    const inp = m.querySelector(`[data-refset-addinput="${lvl}"]`);
    const name = (inp && inp.value || "").trim();
    if (!name) return;
    const parent = lvl === 0 ? "" : _refSetSel[lvl - 1];
    await _refApi("POST", "/api/referral-sources", { parent_id: parent, name });
    renderReferralSettings();
  }));
  m.querySelectorAll("[data-refset-del]").forEach(btn => btn.addEventListener("click", async () => {
    if (!window.confirm("删除该来源项及其下级？")) return;
    await _refApi("DELETE", `/api/referral-sources/${encodeURIComponent(btn.dataset.refsetDel)}`);
    _refSetSel = ["", ""]; renderReferralSettings();
  }));
  m.querySelectorAll("[data-refset-edit]").forEach(btn => btn.addEventListener("click", async () => {
    const name = await appPrompt("改名为：");
    if (!name || !name.trim()) return;
    await _refApi("PUT", `/api/referral-sources/${encodeURIComponent(btn.dataset.refsetEdit)}`, { name: name.trim() });
    renderReferralSettings();
  }));
  m.querySelectorAll("[data-refset-hide]").forEach(btn => btn.addEventListener("click", async () => {
    await _refApi("PUT", `/api/referral-sources/${encodeURIComponent(btn.dataset.refsetHide)}`, { hidden: btn.dataset.refsetHidden !== "1" });
    renderReferralSettings();
  }));
  m.querySelectorAll("[data-refset-move]").forEach(btn => btn.addEventListener("click", async () => {
    await _refApi("POST", `/api/referral-sources/${encodeURIComponent(btn.dataset.refsetId)}/move`, { dir: btn.dataset.refsetMove });
    renderReferralSettings();
  }));
}

async function _refApi(method, url, body) {
  try {
    await fetch(url, { method, headers: { "Content-Type": "application/json" }, body: body ? JSON.stringify(body) : undefined });
  } catch { /* 忽略，下次刷新 */ }
  _refTreeCache = null;   // 改过就让选择器下次重拉
}

window.openRefPicker = openRefPicker;
window.onRefPickSearch = onRefPickSearch;
window.clearRefPick = clearRefPick;
window.confirmRefPick = confirmRefPick;
window.openReferralSettings = openReferralSettings;
window.closeReferralSettings = closeReferralSettings;
