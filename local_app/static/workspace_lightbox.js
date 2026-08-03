// 患者工作区·影像 tab+灯箱/标注/对比查看器(呈现层,只读+图片管理)。
// 独立子系统:自带 lb* 状态机与 canvas 绘图引擎(原 workspace_tabs.js 拆出)。

// ---------- 影像 tab（只读，独立端点懒加载 + 前端分类过滤 + lightbox）----------

// 当前患者影像列表/分类缓存；filter 为 null 表示"全部"（区别于空字符串分类）。
let workspaceImageList = [];
let workspaceImageSetDates = [];   // 该患者已标"组图"的日期(组图查看用)
let workspaceImageCategories = [];
let workspaceImageFilter = null;
let workspaceImageBatch = false;          // 批量选择模式
let workspaceImageSel = new Set();        // 批量选中的 image_id
let workspaceConsentDocs = [];            // 该患者已签电子同意书(影像区「同意书」分类)
const WS_CONSENT_FILTER = "__consent__";  // 同意书分类的过滤哨兵
// lightbox 打开时固化的过滤集 + 当前下标（循环切换用）。
let workspaceLightboxImages = [];
let workspaceLightboxIndex = -1;

async function renderWorkspaceImagesTab(panel) {
  const identity = workspacePatientId;
  panel.textContent = "加载中...";
  let data;
  try {
    const res = await fetch(`/api/patients/${encodeURIComponent(identity)}/images`);
    if (identity !== workspacePatientId) return;
    if (!res.ok) {
      // 失败可重试：取消已加载标记，再点 tab 重新 fetch。
      workspaceLoadedTabs.delete("images");
      panel.textContent = "影像资料载入失败";
      return;
    }
    data = await res.json();
    if (identity !== workspacePatientId) return;
  } catch {
    if (identity !== workspacePatientId) return;
    workspaceLoadedTabs.delete("images");
    panel.textContent = "影像资料载入失败";
    return;
  }
  workspaceImageList = data.list || [];
  workspaceImageSetDates = data.set_dates || [];
  workspaceImageCategories = data.categories || [];
  workspaceImageFilter = null;
  // 同意书自动归类:一并拉该患者已签电子同意书(consent_documents),作影像区一个分类
  try {
    const cr = await fetch(`/api/patients/${encodeURIComponent(identity)}/consent-documents`);
    workspaceConsentDocs = (cr.ok && identity === workspacePatientId) ? ((await cr.json()).documents || []) : [];
  } catch { workspaceConsentDocs = []; }
  // CT/三维影像分区(ct_studies.js,扩展文件不在场→分区隐藏)
  if (typeof loadWorkspaceCtStudies === "function") await loadWorkspaceCtStudies(identity);
  if (identity !== workspacePatientId) return;
  renderWorkspaceImagesPanel(panel);
}

function renderWorkspaceImagesPanel(panel) {
  const batchBar = workspaceImageBatch ? `<div class="image-batch-bar">
    <button type="button" class="plain-button" id="wsSelAll">全选/全不选</button>
    <button type="button" class="plain-button steril-del" id="wsBatchDel">删除所选 (<span id="wsSelN">0</span>)</button>
    <span class="image-drop-hint">勾选要删的影像</span></div>` : "";
  const bar = `<div class="image-capture-bar">
    <button type="button" class="tooth-confirm-btn" id="wsEndoBtn">📷 内窥镜拍照</button>
    <button type="button" class="plain-button" id="wsLocalUpBtn">⬆ 本地上传</button>
    <input type="file" id="wsLocalUpInput" accept="image/*" multiple hidden>
    <button type="button" class="plain-button" id="wsBatchToggle">${workspaceImageBatch ? "✓ 批量中" : "☑ 批量选择"}</button>
    <button type="button" class="plain-button" id="wsGroupViewBtn">🗂 正畸组图</button>
    <span class="image-drop-hint">（可把图片/文件夹拖到此处上传到本患者）</span></div>${batchBar}`;
  const hasAny = workspaceImageList.length || workspaceConsentDocs.length;
  let body;
  if (workspaceImageFilter === WS_CONSENT_FILTER) {
    body = renderWorkspaceConsentCards();
  } else if (!workspaceImageList.length) {
    body = '<div class="workspace-placeholder">暂无影像资料</div>';
  } else {
    const filtered = filteredWorkspaceImages();
    body = filtered.length ? renderWorkspaceImageGroups(filtered) : '<div class="workspace-placeholder">该分类暂无影像</div>';
  }
  const chips = hasAny ? `<div class="image-filter-chips">${renderWorkspaceImageChips()}</div>` : "";
  // CT/三维影像分区(ct_studies.js,扩展文件不在场→分区隐藏)
  const ctSection = typeof renderWorkspaceCtSection === "function" ? renderWorkspaceCtSection() : "";
  panel.innerHTML = bar + ctSection + chips + body;
  const ctBtn = panel.querySelector("#wsCtRefresh");
  if (ctBtn) ctBtn.addEventListener("click", () => refreshWorkspaceCtStudies(panel, ctBtn));
  const endoBtn = panel.querySelector("#wsEndoBtn");
  if (endoBtn) endoBtn.addEventListener("click", () => {
    if (typeof openEndoscopeCapture === "function") openEndoscopeCapture(workspacePatientId, panel);
  });
  // 本地上传:按钮→文件选择;以及整个面板拖拽上传
  const upBtn = panel.querySelector("#wsLocalUpBtn");
  const upInput = panel.querySelector("#wsLocalUpInput");
  if (upBtn && upInput) {
    upBtn.addEventListener("click", () => upInput.click());
    upInput.addEventListener("change", () => { uploadWorkspaceLocalFiles(upInput.files); upInput.value = ""; });
  }
  // 拖拽上传:只绑一次(panel 元素跨重渲染复用,重复 addEventListener 会累积致一次 drop 触发多份→重复上传 #315)。
  // handler 在 drop 时读当前 workspacePatientId,绑一次即对所有患者/分类正确。
  if (!panel.dataset.endoDropBound) {
    panel.dataset.endoDropBound = "1";
    panel.addEventListener("dragover", e => { e.preventDefault(); panel.classList.add("image-drop-over"); });
    panel.addEventListener("dragleave", e => { if (e.target === panel) panel.classList.remove("image-drop-over"); });
    panel.addEventListener("drop", async e => {
      e.preventDefault(); panel.classList.remove("image-drop-over");
      if (!e.dataTransfer) return;
      const files = await _filesFromDataTransfer(e.dataTransfer);   // 支持拖文件夹(递归)
      uploadWorkspaceLocalFiles(files);
    });
  }
  // 删除按钮(每图)
  panel.querySelectorAll("[data-del-img]").forEach(b =>
    b.addEventListener("click", () => deleteWorkspaceImage(b.dataset.delImg)));
  // 批量选择
  const bt = panel.querySelector("#wsBatchToggle");
  if (bt) bt.addEventListener("click", () => {
    workspaceImageBatch = !workspaceImageBatch;
    if (!workspaceImageBatch) workspaceImageSel.clear();
    renderWorkspaceImagesPanel(panel);
  });
  // 组图查看(image_grouping.js)
  const gv = panel.querySelector("#wsGroupViewBtn");
  if (gv) gv.addEventListener("click", () => {
    if (typeof openImageGrouping === "function") openImageGrouping();
  });
  panel.querySelectorAll("[data-sel-img]").forEach(cb => {
    cb.checked = workspaceImageSel.has(cb.dataset.selImg);
    cb.addEventListener("change", () => {
      if (cb.checked) workspaceImageSel.add(cb.dataset.selImg);
      else workspaceImageSel.delete(cb.dataset.selImg);
      const n = panel.querySelector("#wsSelN"); if (n) n.textContent = String(workspaceImageSel.size);
    });
  });
  const selAll = panel.querySelector("#wsSelAll");
  if (selAll) selAll.addEventListener("click", () => {
    const ids = filteredWorkspaceImages().map(im => im.image_id);
    const allOn = ids.every(id => workspaceImageSel.has(id));
    ids.forEach(id => allOn ? workspaceImageSel.delete(id) : workspaceImageSel.add(id));
    renderWorkspaceImagesPanel(panel);
  });
  panel.querySelectorAll("[data-sel-date]").forEach(b => b.addEventListener("click", () => {
    const ids = (b.dataset.selDate || "").split(",").filter(Boolean);
    const allOn = ids.every(id => workspaceImageSel.has(id));
    ids.forEach(id => allOn ? workspaceImageSel.delete(id) : workspaceImageSel.add(id));
    renderWorkspaceImagesPanel(panel);
  }));
  const batchDel = panel.querySelector("#wsBatchDel");
  if (batchDel) batchDel.addEventListener("click", () => batchDeleteWorkspaceImages(panel));
  const selN = panel.querySelector("#wsSelN"); if (selN) selN.textContent = String(workspaceImageSel.size);
  // 同意书卡片「查看」
  panel.querySelectorAll("[data-consent-doc]").forEach(b =>
    b.addEventListener("click", () => openWorkspaceConsentDoc(b.dataset.consentDoc)));
}

// 同意书分类:把该患者已签电子同意书渲染成卡片(模板名+签署时间+状态+查看)
function renderWorkspaceConsentCards() {
  if (!workspaceConsentDocs.length) return '<div class="workspace-placeholder">该患者暂无已签同意书</div>';
  const statusText = s => ({signed: "已签", voided: "已作废"}[s] || s || "");
  return `<div class="consent-card-list">` + workspaceConsentDocs.map(d => `
    <div class="consent-card${d.status === "voided" ? " voided" : ""}">
      <div class="consent-card-name">${escapeHtml(d.template_name || "同意书")}</div>
      <div class="consent-card-meta">${escapeHtml(String(d.signed_at || "").slice(0, 16))}　${escapeHtml(statusText(d.status))}${d.has_tsa ? "　🔒可信时间戳" : ""}</div>
      <button type="button" class="plain-button" data-consent-doc="${escapeAttr(d.document_id || "")}">查看</button>
    </div>`).join("") + `</div>`;
}

// 查看一份签署件:拉完整内容(正文+签名图)弹层显示,带防篡改自检
async function openWorkspaceConsentDoc(documentId) {
  if (!documentId) return;
  let d;
  try {
    const r = await fetch(`/api/consent-documents/${encodeURIComponent(documentId)}`);
    if (!r.ok) { alert("载入同意书失败：" + r.status); return; }
    d = await r.json();
  } catch { alert("载入同意书失败（网络异常）"); return; }
  let m = document.getElementById("consentViewModal");
  if (m) m.remove();
  m = document.createElement("div");
  m.id = "consentViewModal";
  m.className = "modal-backdrop";
  const sign = (label, data) => data
    ? `<div class="consent-sign"><span>${label}</span><img src="${escapeAttr(data)}" alt=""></div>` : "";
  m.innerHTML = `
    <section class="appt-modal consent-view" role="dialog" aria-modal="true" aria-label="同意书">
      <div class="modal-head"><strong>${escapeHtml(d.template_name || "同意书")}</strong>
        <button type="button" class="plain-button" id="consentViewClose">×</button></div>
      <div class="consent-view-body">
        <div class="consent-view-meta">签署时间：${escapeHtml(String(d.signed_at || "").slice(0, 16))}
          ${d.hash_valid ? '<span class="ok">✓ 内容未篡改</span>' : '<span class="fail">⚠ 哈希不一致</span>'}</div>
        <pre class="consent-view-text">${escapeHtml(d.content_text || "")}</pre>
        <div class="consent-signs">${sign("患者签名", d.patient_sign)}${sign("医生签名", d.doctor_sign)}</div>
      </div>
      <div class="modal-actions"><button type="button" class="plain-button" id="consentViewClose2">关闭</button></div>
    </section>`;
  document.body.appendChild(m);
  m.querySelector("#consentViewClose").addEventListener("click", () => m.remove());
  m.querySelector("#consentViewClose2").addEventListener("click", () => m.remove());
}

// 批量删除选中影像(硬删,二次确认)
async function batchDeleteWorkspaceImages(panel) {
  const ids = [...workspaceImageSel];
  if (!ids.length) { alert("请先勾选要删除的影像"); return; }
  if (!window.confirm(`确定删除选中的 ${ids.length} 张影像？删除后不可恢复（仅留审计）。`)) return;
  const id = workspacePatientId;
  const hint = panel.querySelector(".image-drop-hint");
  let ok = 0, fail = 0;
  for (const imgId of ids) {
    if (hint) hint.textContent = `删除中 ${ok + fail + 1}/${ids.length}...`;
    try {
      const r = await fetch(`/api/patients/${encodeURIComponent(id)}/images/${encodeURIComponent(imgId)}`,
        {method: "DELETE"});
      if (r.ok) ok++; else fail++;
    } catch { fail++; }
  }
  workspaceImageSel.clear();
  if (fail) alert(`删除完成：成功 ${ok}，失败 ${fail}`);
  const p = document.querySelector('[data-workspace-panel="images"]');
  if (p && id === workspacePatientId) renderWorkspaceImagesTab(p);
}

// 从拖拽 DataTransfer 取文件:支持拖整个文件夹(webkitGetAsEntry 递归读子目录)。
// 注意:entry 必须在 await 前同步取出(DataTransferItemList 仅在事件期间有效)。
function _filesFromDataTransfer(dt) {
  const items = dt.items;
  if (items && items.length && items[0] && items[0].webkitGetAsEntry) {
    const entries = [];
    for (const it of items) { const e = it.webkitGetAsEntry && it.webkitGetAsEntry(); if (e) entries.push(e); }
    if (entries.length) {
      return (async () => { const out = []; for (const e of entries) await _collectEntryFiles(e, out); return out; })();
    }
  }
  return Promise.resolve(Array.from(dt.files || []));
}
function _entryFile(entry) {
  return new Promise(res => entry.file(f => res(f), () => res(null)));
}
function _readAllEntries(reader) {   // readEntries 分页,需循环读到空
  return new Promise(res => {
    const all = [];
    const step = () => reader.readEntries(b => { if (!b.length) return res(all); all.push(...b); step(); }, () => res(all));
    step();
  });
}
async function _collectEntryFiles(entry, out) {
  if (!entry) return;
  if (entry.isFile) { const f = await _entryFile(entry); if (f) out.push(f); }
  else if (entry.isDirectory) {
    const es = await _readAllEntries(entry.createReader());
    for (const e of es) await _collectEntryFiles(e, out);
  }
}

// 本地选图/拖拽(含文件夹) → 上传到当前患者(category=本地上传),完成刷新影像 tab
async function uploadWorkspaceLocalFiles(fileList) {
  const okExt = /\.(jpe?g|png|webp)$/i;
  const files = Array.from(fileList || []).filter(f => /^image\//.test(f.type || "") || okExt.test(f.name || ""));
  if (!files.length) return;
  const id = workspacePatientId;
  const hint = document.querySelector('[data-workspace-panel="images"] .image-drop-hint');
  let ok = 0, fail = 0;
  for (const f of files) {
    if (hint) hint.textContent = `上传中 ${ok + fail + 1}/${files.length}...`;
    const fd = new FormData();
    fd.append("file", f, f.name || "upload.jpg");
    fd.append("slot", "本地上传");
    try {
      const r = await fetch(`/api/patients/${encodeURIComponent(id)}/images`, {method: "POST", body: fd});
      if (r.ok) ok++; else fail++;
    } catch { fail++; }
  }
  if (hint) hint.textContent = `上传完成：成功 ${ok}${fail ? "，失败 " + fail : ""}`;
  const panel = document.querySelector('[data-workspace-panel="images"]');
  if (panel && id === workspacePatientId) renderWorkspaceImagesTab(panel);
}

// 删除一张影像(硬删,二次确认),完成刷新
async function deleteWorkspaceImage(imageId) {
  if (!imageId) return;
  if (!window.confirm("确定删除这张影像？删除后不可恢复（仅留审计）。")) return;
  const id = workspacePatientId;
  try {
    const r = await fetch(`/api/patients/${encodeURIComponent(id)}/images/${encodeURIComponent(imageId)}`,
      {method: "DELETE"});
    if (!r.ok) { alert("删除失败：" + r.status); return; }
  } catch { alert("删除失败（网络异常）"); return; }
  const panel = document.querySelector('[data-workspace-panel="images"]');
  if (panel && id === workspacePatientId) renderWorkspaceImagesTab(panel);
}

function renderWorkspaceImageChips() {
  const allChip = `
    <button type="button" class="image-chip${workspaceImageFilter === null ? " active" : ""}"
      onclick="setWorkspaceImageFilter(this)">全部(${formatCount(workspaceImageList.length)})</button>
  `;
  const categoryChips = workspaceImageCategories.map(row => {
    const category = String(row.category || "");
    const active = workspaceImageFilter === category;
    return `
      <button type="button" class="image-chip${active ? " active" : ""}"
        data-image-category="${escapeAttr(category)}"
        onclick="setWorkspaceImageFilter(this)">${escapeHtml(category || "未分类")}(${formatCount(row.count || 0)})</button>
    `;
  }).join("");
  // 同意书:自动汇总该患者已签电子同意书(独立来源,非 patient_images 分类)
  const consentChip = `
    <button type="button" class="image-chip${workspaceImageFilter === WS_CONSENT_FILTER ? " active" : ""}"
      data-image-category="${WS_CONSENT_FILTER}"
      onclick="setWorkspaceImageFilter(this)">同意书(${formatCount(workspaceConsentDocs.length)})</button>`;
  return allChip + categoryChips + consentChip;
}

// chip 点击：分类名从 data 属性取（不内联进 JS 字符串，避免引号注入）。
function setWorkspaceImageFilter(el) {
  workspaceImageFilter = "imageCategory" in el.dataset ? el.dataset.imageCategory : null;
  const panel = document.querySelector('[data-workspace-panel="images"]');
  if (panel) renderWorkspaceImagesPanel(panel);
}

function filteredWorkspaceImages() {
  if (workspaceImageFilter === null) return workspaceImageList;
  return workspaceImageList.filter(image => String(image.category || "") === workspaceImageFilter);
}

function workspaceImageFileUrl(image) {
  const identity = workspacePatientId;
  // _v:保存旋转后置的客户端版本号,绕过浏览器对同URL的缓存
  const v = image && image._v ? `?v=${image._v}` : "";
  return `/api/patients/${encodeURIComponent(identity)}/images/${encodeURIComponent(image.image_id)}/file${v}`;
}

// 内窥镜录像(webm/mp4)混在 patient_images 里:缩略图用 <video>,双击弹播放器,不进图片 lightbox
function wsIsVideoFile(name) { return /\.(webm|mp4)$/i.test(String(name || "")); }

function wsStillImages() { return filteredWorkspaceImages().filter(im => !wsIsVideoFile(im.file_name)); }

function openWorkspaceVideo(imageId) {
  const im = workspaceImageList.find(x => x.image_id === imageId);
  if (!im) return;
  let m = document.getElementById("wsVideoModal");
  if (m) m.remove();
  m = document.createElement("div");
  m.id = "wsVideoModal";
  m.className = "modal-backdrop";
  m.innerHTML = `
    <section class="appt-modal ws-video-modal" role="dialog" aria-modal="true" aria-label="录像回放">
      <div class="modal-head"><strong>▶ 录像回放</strong>
        <span class="ws-video-meta">${escapeHtml(String(im.image_date || "").slice(0, 10))}</span>
        <button type="button" class="plain-button" id="wsVideoClose">×</button></div>
      <video src="${escapeAttr(workspaceImageFileUrl(im))}" controls autoplay class="ws-video-player"></video>
    </section>`;
  document.body.appendChild(m);
  m.querySelector("#wsVideoClose").addEventListener("click", () => m.remove());
  m.addEventListener("click", e => { if (e.target === m) m.remove(); });
}

// 按 image_date 分组的缩略图栅格；index 是图片在"纯图片集"(wsStillImages)里的下标——
// 录像不进 lightbox,故 lightbox 下标按剔除视频后的序列算,视频项下标为 -1。
function renderWorkspaceImageGroups(images) {
  const dateOrder = [];
  const byDate = new Map();
  let stillIdx = 0;
  images.forEach(image => {
    const date = String(image.image_date || "").slice(0, 10) || "未知日期";
    if (!byDate.has(date)) {
      byDate.set(date, []);
      dateOrder.push(date);
    }
    byDate.get(date).push([image, wsIsVideoFile(image.file_name) ? -1 : stillIdx++]);
  });
  return dateOrder.map(date => {
    const ids = byDate.get(date).map(([im]) => im.image_id).filter(Boolean);
    const dayBtn = workspaceImageBatch
      ? `<button type="button" class="image-day-sel" data-sel-date="${escapeAttr(ids.join(","))}">全选本日(${ids.length})</button>`
      : "";
    return `
    <div class="image-date-group">
      <div class="image-date-title">${escapeHtml(date)}${dayBtn}</div>
      <div class="image-grid">
        ${byDate.get(date).map(([image, index]) => `
          <div class="image-thumb-wrap${workspaceImageBatch ? " batch" : ""}">
            ${index < 0 ? `
            <button type="button" class="image-thumb image-thumb-video" ondblclick="openWorkspaceVideo('${escapeAttr(image.image_id || "")}')" title="双击播放录像">
              <video src="${escapeAttr(workspaceImageFileUrl(image))}" preload="metadata" muted></video>
              <span class="image-video-badge">▶</span>
              <span class="image-thumb-name">${escapeHtml(image.file_name || "")}</span>
            </button>` : `
            <button type="button" class="image-thumb" ondblclick="openImageLightbox(${index}, wsStillImages())" title="双击打开查看器">
              <img src="${escapeAttr(workspaceImageFileUrl(image))}" loading="lazy" alt="${escapeAttr(image.file_name || "")}">
              <span class="image-thumb-name">${escapeHtml(image.file_name || "")}</span>
            </button>`}
            ${workspaceImageBatch
              ? `<label class="image-sel"><input type="checkbox" data-sel-img="${escapeAttr(image.image_id || "")}"></label>`
              : `<button type="button" class="image-del" data-del-img="${escapeAttr(image.image_id || "")}" title="删除影像">×</button>`}
          </div>
        `).join("")}
      </div>
    </div>`;
  }).join("");
}

// lightbox 增强状态：缩放倍率 / 标注工具(null/arrow/pen/eraser) / 对比模式开关。
let lbZoom = 1;
let lbTool = null;
let lbCompare = false;
let lbDrawing = false;
let lbStart = null;        // 箭头起点(canvas 像素坐标)
let lbSnapshot = null;     // 画箭头时的画布快照(随鼠标重绘)
let lbActiveSlot = "A";    // 对比模式下被激活的格(选片填入这格)
let lbSlotIndex = {A: -1, B: -1};  // 两格各自显示的图片下标
// 视图调整(每张图独立，换图重置)：旋转角 / 水平镜像 / 垂直镜像 / 亮度 / 对比度
let lbRotate = 0, lbFlipX = 1, lbFlipY = 1, lbBright = 1, lbContrast = 1;

// 主图 wrap 的合成变换：平移(拖拽)+缩放+旋转+翻转，缩放/拖拽/旋转/翻转共用
function lbWrapTransform(tx) {
  return `translate3d(${tx || 0}px,0,0) scale(${lbZoom}) rotate(${lbRotate}deg) scale(${lbFlipX},${lbFlipY})`;
}

// 亮度/对比度作用在图片本身(不碰标注画布，标注颜色不被调暗)
function applyLbFilter() {
  const f = `brightness(${lbBright}) contrast(${lbContrast})`;
  ["imageLightboxImg", "imageLightboxImgB"].forEach(id => {
    const el = document.getElementById(id);
    if (el) el.style.filter = f;
  });
}

function resetLbView() {
  lbRotate = 0; lbFlipX = 1; lbFlipY = 1; lbBright = 1; lbContrast = 1;
  const b = document.getElementById("lbBright"); if (b) b.value = 100;
  const c = document.getElementById("lbContrast"); if (c) c.value = 100;
  applyLbFilter();
}

function rotateLightbox() { lbRotate = (lbRotate + 90) % 360; applyLightboxZoom(); }

// 把当前旋转/镜像永久写回图片文件(后端PIL重写,内窥镜拍反的片子矫正一次管永久)
async function saveLightboxOrient() {
  if (lbCompare) { window.alert("对比模式下不支持保存旋转"); return; }
  if (lbRotate === 0 && lbFlipX === 1 && lbFlipY === 1) { window.alert("当前没有旋转/镜像改动"); return; }
  const image = workspaceLightboxImages[workspaceLightboxIndex];
  if (!image) return;
  if (wsIsVideoFile(image.file_name)) { window.alert("视频不支持旋转保存"); return; }
  if (!window.confirm("把当前旋转/镜像永久保存到图片文件？(原图将被覆盖)")) return;
  let res;
  try {
    res = await fetch(`/api/patients/${encodeURIComponent(workspacePatientId)}/images/${encodeURIComponent(image.image_id)}/orient`, {
      method: "POST", headers: {"Content-Type": "application/json"},
      body: JSON.stringify({rotate: lbRotate, flip_x: lbFlipX === -1, flip_y: lbFlipY === -1}),
    });
  } catch { window.alert("保存失败（网络异常）"); return; }
  if (!res.ok) { const m = await res.json().catch(() => ({})); window.alert("保存失败：" + (m.detail || res.status)); return; }
  image._v = Date.now();
  resetLbView();            // 文件本体已是新方向,视图归零再显示
  updateImageLightbox();
  // 缩略图墙同步换新图
  const panel = document.querySelector('[data-workspace-panel="images"]');
  if (panel && typeof renderWorkspaceImagesPanel === "function") renderWorkspaceImagesPanel(panel);
}
function flipLightbox(axis) { if (axis === "x") lbFlipX *= -1; else lbFlipY *= -1; applyLightboxZoom(); }
function setLbAdjust() {
  const b = document.getElementById("lbBright"), c = document.getElementById("lbContrast");
  if (b) lbBright = (Number(b.value) || 100) / 100;
  if (c) lbContrast = (Number(c.value) || 100) / 100;
  applyLbFilter();
}

// list 可选:传入自定义图片列表(如组图浮层的当前平铺集);缺省用影像tab当前过滤集
function openImageLightbox(index, list) {
  workspaceLightboxImages = list || filteredWorkspaceImages();
  if (index < 0 || index >= workspaceLightboxImages.length) return;
  workspaceLightboxIndex = index;
  lbZoom = 1;
  setLightboxCompare(false);
  setLightboxTool(null);
  updateImageLightbox();
  renderLightboxDateSidebar();
  hideLightboxSidebars();      // 默认两栏收起
  const modal = document.getElementById("imageLightbox");
  if (modal) modal.hidden = false;
}

// ---------- 左边栏：按日期折叠的选片器 ----------
function renderLightboxDateSidebar() {
  const box = document.getElementById("lbDateAccordion");
  if (!box) return;
  const dateOrder = [];
  const byDate = new Map();
  workspaceLightboxImages.forEach((image, index) => {
    const date = String(image.image_date || "").slice(0, 10) || "未知日期";
    if (!byDate.has(date)) { byDate.set(date, []); dateOrder.push(date); }
    byDate.get(date).push([image, index]);
  });
  // 当前主图所在日期默认展开
  const cur = workspaceLightboxImages[workspaceLightboxIndex];
  const openDate = cur ? (String(cur.image_date || "").slice(0, 10) || "未知日期") : dateOrder[0];
  box.innerHTML = dateOrder.map(date => `
    <div class="lb-date-block${date === openDate ? " open" : ""}">
      <button type="button" class="lb-date-head" onclick="toggleLbDateBlock(this)">
        <span class="lb-date-caret">▸</span>${escapeHtml(date)}
        <span class="lb-date-count">${formatCount(byDate.get(date).length)}</span>
      </button>
      <div class="lb-date-grid">
        ${byDate.get(date).map(([image, index]) => `
          <button type="button" class="lb-date-thumb" onclick="pickLightboxImage(${index})"
                  draggable="true" ondragstart="lbDragStart(event, ${index})" title="${escapeAttr((image.file_name || "") + " · 拖到画面可对比")}">
            <img src="${escapeAttr(workspaceImageFileUrl(image))}" loading="lazy" alt="" draggable="false">
          </button>
        `).join("")}
      </div>
    </div>
  `).join("");
}

function toggleLbDateBlock(headEl) {
  const block = headEl.closest(".lb-date-block");
  if (block) block.classList.toggle("open");
}

// 选片落位：单图模式→换主图；对比模式→填入当前激活格
function pickLightboxImage(index) {
  if (index < 0 || index >= workspaceLightboxImages.length) return;
  if (lbCompare) {
    lbSlotIndex[lbActiveSlot] = index;
    updateCompareSlot(lbActiveSlot);
  } else {
    workspaceLightboxIndex = index;
    updateImageLightbox();
  }
}

// ---------- 拖拽对比：从左边栏拖缩略图，丢到某个格子 ----------
let lbDragIndex = -1;

function lbDragStart(ev, index) {
  lbDragIndex = index;
  if (ev.dataTransfer) {
    ev.dataTransfer.setData("text/plain", String(index));
    ev.dataTransfer.effectAllowed = "copy";
  }
}

function lbDragOver(ev) {
  ev.preventDefault();                       // 允许放置
  if (ev.dataTransfer) ev.dataTransfer.dropEffect = "copy";
  const pane = ev.currentTarget;
  if (pane) pane.classList.add("dragover");
}

function lbDragLeave(ev) {
  const pane = ev.currentTarget;
  if (pane) pane.classList.remove("dragover");
}

function lbDrop(ev, slot) {
  ev.preventDefault();
  const pane = ev.currentTarget;
  if (pane) pane.classList.remove("dragover");
  let index = lbDragIndex;
  if (index < 0 && ev.dataTransfer) index = Number(ev.dataTransfer.getData("text/plain"));
  lbDragIndex = -1;
  if (!(index >= 0 && index < workspaceLightboxImages.length)) return;
  if (!lbCompare) {
    // 单图模式拖入 → 自动进对比：当前图当左图，拖入的当右图
    setLightboxCompare(true);
    lbSlotIndex.B = index;
    updateCompareSlot("B");
    setCompareActiveSlot("B");
  } else {
    lbSlotIndex[slot] = index;
    updateCompareSlot(slot);
    setCompareActiveSlot(slot);
  }
}

// 左右循环切换（单图→换主图；对比→换当前激活格那张）。箭头/键盘/滚轮/滑动共用。
function stepImageLightbox(step) {
  const total = workspaceLightboxImages.length;
  if (!total) return;
  if (lbCompare) {
    const cur = lbSlotIndex[lbActiveSlot] >= 0 ? lbSlotIndex[lbActiveSlot] : 0;
    lbSlotIndex[lbActiveSlot] = (cur + step + total) % total;
    updateCompareSlot(lbActiveSlot);
  } else {
    workspaceLightboxIndex = (workspaceLightboxIndex + step + total) % total;
    updateImageLightbox();
  }
}

function updateImageLightbox() {
  const image = workspaceLightboxImages[workspaceLightboxIndex];
  if (!image) return;
  resetLbView();   // 换图重置旋转/翻转/调窗，每张图从干净视图开始
  const img = document.getElementById("imageLightboxImg");
  if (img) {
    img.onload = () => { resetLightboxCanvas(); };
    img.src = workspaceImageFileUrl(image);
    img.alt = image.file_name || "";
  }
  const caption = document.getElementById("imageLightboxCaption");
  if (caption) {
    caption.textContent = [String(image.image_date || "").slice(0, 10), image.category, image.file_name]
      .filter(Boolean).join(" · ");
  }
  applyLightboxZoom();
}

// 画布尺寸对齐图片原始分辨率(标注按原图坐标存)，并清空旧标注。
function resetLightboxCanvas() {
  const img = document.getElementById("imageLightboxImg");
  const canvas = document.getElementById("lbCanvasA");
  if (!img || !canvas) return;
  canvas.width = img.naturalWidth || img.width || 800;
  canvas.height = img.naturalHeight || img.height || 600;
  const ctx = canvas.getContext("2d");
  ctx.clearRect(0, 0, canvas.width, canvas.height);
}

function clearLightboxAnnotation() {
  const canvas = document.getElementById("lbCanvasA");
  if (!canvas) return;
  canvas.getContext("2d").clearRect(0, 0, canvas.width, canvas.height);
}

function setLightboxTool(tool) {
  lbTool = tool || null;
  document.querySelectorAll("#lbAnnotTools [data-lb-tool]").forEach(btn => {
    btn.classList.toggle("active", btn.dataset.lbTool === lbTool);
  });
  const canvas = document.getElementById("lbCanvasA");
  if (canvas) canvas.style.pointerEvents = lbTool ? "auto" : "none";
}

function zoomLightbox(dir) {
  if (dir === 0) { lbZoom = 1; resetLbView(); }   // 复位：缩放+旋转+翻转+调窗全部还原
  else lbZoom = Math.min(5, Math.max(0.25, lbZoom + (dir > 0 ? 0.25 : -0.25)));
  applyLightboxZoom();
}

function applyLightboxZoom() {
  const wrapA = document.getElementById("lbWrapA");
  if (wrapA) wrapA.style.transform = lbWrapTransform(0);   // 主图带旋转/翻转
  const wrapB = document.getElementById("lbWrapB");
  if (wrapB) wrapB.style.transform = `scale(${lbZoom})`;   // 对比右格仅缩放
  const label = document.getElementById("lbZoomLabel");
  if (label) label.textContent = Math.round(lbZoom * 100) + "%";
}

// 把图片+标注合成导出 PNG（同源图片，画布不会被污染）。
function exportLightboxAnnotated() {
  const img = document.getElementById("imageLightboxImg");
  const canvas = document.getElementById("lbCanvasA");
  if (!img || !canvas || !img.naturalWidth) return;
  const out = document.createElement("canvas");
  out.width = img.naturalWidth;
  out.height = img.naturalHeight;
  const ctx = out.getContext("2d");
  ctx.filter = `brightness(${lbBright}) contrast(${lbContrast})`;   // 导出带上调窗效果
  ctx.drawImage(img, 0, 0, out.width, out.height);
  ctx.filter = "none";                                               // 标注层保持原色
  ctx.drawImage(canvas, 0, 0, out.width, out.height);
  out.toBlob(blob => {
    if (!blob) return;
    const a = document.createElement("a");
    const image = workspaceLightboxImages[workspaceLightboxIndex] || {};
    a.href = URL.createObjectURL(blob);
    a.download = `标注_${(image.file_name || "image").replace(/\.[^.]+$/, "")}.png`;
    a.click();
    URL.revokeObjectURL(a.href);
  }, "image/png");
}

// ---------- 对比模式（左右两格并排，从左边栏选片填入激活格） ----------
function setLightboxCompare(on) {
  lbCompare = !!on;
  const paneB = document.getElementById("lbPaneB");
  const panes = document.getElementById("lbPanes");
  const tools = document.getElementById("lbAnnotTools");
  const btn = document.getElementById("lbCompareBtn");
  if (paneB) paneB.hidden = !lbCompare;
  if (panes) panes.classList.toggle("compare", lbCompare);
  if (tools) tools.style.display = lbCompare ? "none" : "";   // 对比时不标注
  if (btn) btn.classList.toggle("active", lbCompare);
  if (lbCompare) {
    setLightboxTool(null);
    // 进对比：左格=当前主图，右格默认下一张；默认激活右格便于换对比图
    const total = workspaceLightboxImages.length;
    lbSlotIndex.A = workspaceLightboxIndex;
    lbSlotIndex.B = total > 1 ? (workspaceLightboxIndex + 1) % total : workspaceLightboxIndex;
    updateCompareSlot("A");
    updateCompareSlot("B");
    setCompareActiveSlot("B");
  } else {
    setCompareActiveSlot("A");
  }
}

function toggleCompareMode() {
  setLightboxCompare(!lbCompare);
  applyLightboxZoom();
}

// 点格子激活它（之后从左边栏选片就填到这格）；非对比模式忽略。
function setCompareActiveSlot(slot) {
  if (slot !== "A" && slot !== "B") return;
  lbActiveSlot = slot;
  const a = document.getElementById("lbPaneA");
  const b = document.getElementById("lbPaneB");
  // 单图模式只有 A，永远高亮 A；对比模式按激活格高亮
  if (a) a.classList.toggle("active", !lbCompare || slot === "A");
  if (b) b.classList.toggle("active", lbCompare && slot === "B");
}

function updateCompareSlot(slot) {
  const imgEl = document.getElementById(slot === "A" ? "imageLightboxImg" : "imageLightboxImgB");
  if (!imgEl) return;
  const image = workspaceLightboxImages[lbSlotIndex[slot]];
  if (image) { imgEl.src = workspaceImageFileUrl(image); imgEl.alt = image.file_name || ""; }
  if (slot === "A") { imgEl.onload = () => resetLightboxCanvas(); }
  applyLightboxZoom();
}

// ---------- 左右边栏：默认收起，靠近屏边弹出 ----------
function showLightboxSidebar(side) {
  const el = document.getElementById(side === "left" ? "lbDateSidebar" : "lbToolSidebar");
  if (el) el.classList.add("show");
}
function hideLightboxSidebars() {
  ["lbDateSidebar", "lbToolSidebar"].forEach(id => {
    const el = document.getElementById(id);
    if (el) el.classList.remove("show");
  });
}

function closeImageLightbox() {
  const modal = document.getElementById("imageLightbox");
  if (modal) modal.hidden = true;
  // 隐私：清掉大图/对比图/标注，避免 DOM 残留 / 下次打开闪旧图。
  ["imageLightboxImg", "imageLightboxImgB"].forEach(id => {
    const img = document.getElementById(id);
    if (img) { img.removeAttribute("src"); img.alt = ""; }
  });
  clearLightboxAnnotation();
  const caption = document.getElementById("imageLightboxCaption");
  if (caption) caption.textContent = "";
  workspaceLightboxImages = [];
  workspaceLightboxIndex = -1;
  lbZoom = 1;
  lbSlotIndex = {A: -1, B: -1};
  setLightboxCompare(false);
  setLightboxTool(null);
  hideLightboxSidebars();
  const acc = document.getElementById("lbDateAccordion");
  if (acc) acc.innerHTML = "";   // 隐私：清掉日期选片器里的缩略图
}

// 患者切换时调用（resetWorkspacePanels）：清空影像模块全部状态，医疗影像不跨患者残留。
function resetWorkspaceImageState() {
  closeImageLightbox();
  workspaceImageList = [];
  workspaceImageCategories = [];
  workspaceImageFilter = null;
}

// 鼠标/触摸坐标 → 画布像素坐标。用 offsetX/Y（元素本地坐标，浏览器已扣除祖先的
// 缩放/旋转变换），所以缩放、旋转下都能落到正确像素。
function lbCanvasPoint(canvas, ev) {
  const cw = canvas.clientWidth || canvas.width;
  const ch = canvas.clientHeight || canvas.height;
  let ox = ev.offsetX, oy = ev.offsetY;
  if (ox == null) { const r = canvas.getBoundingClientRect(); ox = ev.clientX - r.left; oy = ev.clientY - r.top; }
  return { x: ox * (canvas.width / cw), y: oy * (canvas.height / ch) };
}

function lbStroke(canvas) {
  // 线宽按原图分辨率放大，缩略图与高清片都看得清
  return Math.max(3, Math.round(canvas.width / 400));
}

// 背景点击 / Esc 关闭 + 画布标注绘制（脚本在 body 末尾加载，DOM 已就位）。
(() => {
  const modal = document.getElementById("imageLightbox");
  if (!modal) return;
  modal.addEventListener("click", event => {
    if (event.target === modal) closeImageLightbox();
  });
  document.addEventListener("keydown", event => {
    if (modal.hidden) return;
    if (event.key === "Escape") closeImageLightbox();
    else if (event.key === "ArrowLeft") stepImageLightbox(-1);
    else if (event.key === "ArrowRight") stepImageLightbox(1);
  });

  // 边栏自动隐藏/靠边弹出：鼠标距左右屏边 < 阈值则弹出，移开收起；
  // 指针在边栏自身上时保持展开。
  const EDGE = 48;
  modal.addEventListener("pointermove", ev => {
    if (modal.hidden) return;
    if (ev.target.closest && ev.target.closest(".lb-sidebar")) return;  // 在边栏内不改状态
    const dl = document.getElementById("lbDateSidebar");
    const dt = document.getElementById("lbToolSidebar");
    if (dl) dl.classList.toggle("show", ev.clientX <= EDGE);
    if (dt) dt.classList.toggle("show", ev.clientX >= window.innerWidth - EDGE);
  });
  ["lbDateSidebar", "lbToolSidebar"].forEach(id => {
    const el = document.getElementById(id);
    if (el) el.addEventListener("mouseleave", () => el.classList.remove("show"));
  });
  // 触屏：点感应条拉手弹出对应边栏
  const el = document.getElementById("lbEdgeLeft");
  const er = document.getElementById("lbEdgeRight");
  if (el) el.addEventListener("click", () => showLightboxSidebar("left"));
  if (er) er.addEventListener("click", () => showLightboxSidebar("right"));

  // 滚轮 / 触控板左右(或上下)滑动翻图：在画面区滑动即可，不用够边上的箭头。
  // 取主轴位移累加到阈值翻一张，翻后短暂上锁，避免一次惯性滑动连翻多张。
  const stage = modal.querySelector(".image-lightbox-stage");
  let wheelAcc = 0, wheelLock = false;
  if (stage) {
    stage.addEventListener("wheel", ev => {
      if (ev.target.closest && ev.target.closest(".lb-sidebar")) return;  // 边栏自己滚
      ev.preventDefault();
      if (wheelLock) return;
      const d = Math.abs(ev.deltaX) > Math.abs(ev.deltaY) ? ev.deltaX : ev.deltaY;
      wheelAcc += d;
      if (Math.abs(wheelAcc) >= 24) {
        stepImageLightbox(wheelAcc > 0 ? -1 : 1);   // 向左/上滑→下一张（方向已校正）
        wheelAcc = 0;
        wheelLock = true;
        setTimeout(() => { wheelLock = false; }, 220);
      }
    }, {passive: false});

  }

  // 拖拽轮播：单图模式下按住主图横向拖动，图片实时跟手平移；松手过阈值则缓动滑出、
  // 下一张从相反方向滑入；不够则弹回。鼠标/触屏/触控笔统一走 pointer 事件。
  const wrapA = document.getElementById("lbWrapA");
  if (wrapA) {
    let dragging = false, startX = 0, dx = 0;
    // translate3d 触发 GPU 合成层，拖动每帧只做合成不重绘 → 不卡顿
    const tf = (x) => lbWrapTransform(x);
    const viewAdjusted = () => lbRotate !== 0 || lbFlipX !== 1 || lbFlipY !== 1;

    wrapA.addEventListener("pointerdown", ev => {
      if (lbCompare || lbTool || lbZoom !== 1 || viewAdjusted()) return;   // 对比/标注/缩放/旋转翻转时不翻页
      if (workspaceLightboxImages.length < 2) return;
      if (ev.pointerType === "mouse" && ev.button !== 0) return;
      dragging = true; startX = ev.clientX; dx = 0;
      wrapA.style.transition = "none";
      try { wrapA.setPointerCapture(ev.pointerId); } catch (e) {}
    });
    wrapA.addEventListener("pointermove", ev => {
      if (!dragging) return;
      dx = ev.clientX - startX;
      wrapA.style.transform = tf(dx);
    });
    const DUR = 200;   // ms，与 CSS transition 时长一致
    const endDrag = () => {
      if (!dragging) return;
      dragging = false;
      const w = wrapA.getBoundingClientRect().width || window.innerWidth;
      const threshold = Math.min(120, w * 0.18);
      if (Math.abs(dx) > threshold) {
        const dir = dx < 0 ? 1 : -1;                            // 左拖→下一张
        const off = dx < 0 ? -w : w;
        wrapA.style.transition = `transform ${DUR}ms ease`;
        wrapA.style.transform = tf(off);                        // 旧图缓动滑出
        setTimeout(() => {
          stepImageLightbox(dir);                               // 换图(内部会重置画布/缩放)
          wrapA.style.transition = "none";
          wrapA.style.transform = tf(-off);                     // 新图瞬移到相反侧(起点)
          void wrapA.offsetWidth;                               // 强制重排，确保起点生效再过渡
          wrapA.style.transition = `transform ${DUR}ms ease`;
          wrapA.style.transform = tf(0);                        // 新图缓动滑入
        }, DUR);
      } else {
        wrapA.style.transition = `transform ${DUR}ms ease`;
        wrapA.style.transform = tf(0);                          // 不够阈值，弹回
      }
      dx = 0;
    };
    wrapA.addEventListener("pointerup", endDrag);
    wrapA.addEventListener("pointercancel", endDrag);
  }

  const canvas = document.getElementById("lbCanvasA");
  if (!canvas) return;
  const ctx = canvas.getContext("2d");

  canvas.addEventListener("pointerdown", ev => {
    if (!lbTool) return;
    ev.preventDefault();
    const p = lbCanvasPoint(canvas, ev);
    // L/R 标记：点一下盖个大字母，不进入拖拽
    if (lbTool === "markL" || lbTool === "markR") {
      drawMarker(ctx, canvas, p, lbTool === "markL" ? "L" : "R");
      return;
    }
    canvas.setPointerCapture(ev.pointerId);
    lbDrawing = true;
    lbStart = p;
    if (lbTool === "arrow" || lbTool === "measure") {
      lbSnapshot = ctx.getImageData(0, 0, canvas.width, canvas.height);
    } else {
      ctx.beginPath();
      ctx.moveTo(p.x, p.y);
    }
  });

  canvas.addEventListener("pointermove", ev => {
    if (!lbDrawing || !lbTool) return;
    const p = lbCanvasPoint(canvas, ev);
    const w = lbStroke(canvas);
    if (lbTool === "pen") {
      ctx.globalCompositeOperation = "source-over";
      ctx.strokeStyle = "#ff2d2d";
      ctx.lineWidth = w;
      ctx.lineCap = "round";
      ctx.lineJoin = "round";
      ctx.lineTo(p.x, p.y);
      ctx.stroke();
    } else if (lbTool === "eraser") {
      ctx.globalCompositeOperation = "destination-out";
      ctx.lineWidth = w * 6;
      ctx.lineCap = "round";
      ctx.lineTo(p.x, p.y);
      ctx.stroke();
    } else if (lbTool === "arrow" && lbSnapshot) {
      ctx.putImageData(lbSnapshot, 0, 0);   // 还原后按当前终点重画箭头
      drawArrow(ctx, lbStart, p, w);
    } else if (lbTool === "measure" && lbSnapshot) {
      ctx.putImageData(lbSnapshot, 0, 0);   // 还原后按当前终点重画测量线+长度
      drawMeasure(ctx, lbStart, p, w);
    }
  });

  const finish = () => {
    if (!lbDrawing) return;
    lbDrawing = false;
    lbStart = null;
    lbSnapshot = null;
    ctx.globalCompositeOperation = "source-over";
  };
  canvas.addEventListener("pointerup", finish);
  canvas.addEventListener("pointercancel", finish);
})();

function drawArrow(ctx, from, to, w) {
  ctx.globalCompositeOperation = "source-over";
  ctx.strokeStyle = "#ff2d2d";
  ctx.fillStyle = "#ff2d2d";
  ctx.lineWidth = w;
  ctx.lineCap = "round";
  ctx.beginPath();
  ctx.moveTo(from.x, from.y);
  ctx.lineTo(to.x, to.y);
  ctx.stroke();
  const ang = Math.atan2(to.y - from.y, to.x - from.x);
  const head = w * 5;
  ctx.beginPath();
  ctx.moveTo(to.x, to.y);
  ctx.lineTo(to.x - head * Math.cos(ang - Math.PI / 6), to.y - head * Math.sin(ang - Math.PI / 6));
  ctx.lineTo(to.x - head * Math.cos(ang + Math.PI / 6), to.y - head * Math.sin(ang + Math.PI / 6));
  ctx.closePath();
  ctx.fill();
}

// 测量：两端带横杠的直线 + 中点像素长度标签（本期为像素级粗测，未做物理标定）
function drawMeasure(ctx, from, to, w) {
  ctx.globalCompositeOperation = "source-over";
  ctx.strokeStyle = "#19c37d";
  ctx.fillStyle = "#19c37d";
  ctx.lineWidth = w;
  ctx.lineCap = "round";
  ctx.beginPath();
  ctx.moveTo(from.x, from.y);
  ctx.lineTo(to.x, to.y);
  ctx.stroke();
  // 端点小横杠
  const ang = Math.atan2(to.y - from.y, to.x - from.x);
  const cap = w * 3;
  [from, to].forEach(pt => {
    ctx.beginPath();
    ctx.moveTo(pt.x - cap * Math.cos(ang + Math.PI / 2), pt.y - cap * Math.sin(ang + Math.PI / 2));
    ctx.lineTo(pt.x + cap * Math.cos(ang + Math.PI / 2), pt.y + cap * Math.sin(ang + Math.PI / 2));
    ctx.stroke();
  });
  const len = Math.round(Math.hypot(to.x - from.x, to.y - from.y));
  const fs = Math.max(14, Math.round(ctx.canvas.width / 45));
  ctx.font = `${fs}px sans-serif`;
  ctx.textAlign = "center";
  ctx.textBaseline = "bottom";
  const mx = (from.x + to.x) / 2, my = (from.y + to.y) / 2;
  const label = `${len}px`;
  ctx.lineWidth = Math.max(3, fs / 5);
  ctx.strokeStyle = "rgba(0,0,0,0.55)";   // 描边底，浅图也看得清
  ctx.strokeText(label, mx, my - 4);
  ctx.fillText(label, mx, my - 4);
}

// L/R 标记：在点击处盖一个红色大字母（带黑描边）
function drawMarker(ctx, canvas, p, letter) {
  ctx.globalCompositeOperation = "source-over";
  const fs = Math.max(28, Math.round(canvas.width / 14));
  ctx.font = `bold ${fs}px sans-serif`;
  ctx.textAlign = "center";
  ctx.textBaseline = "middle";
  ctx.lineWidth = Math.max(4, fs / 6);
  ctx.strokeStyle = "rgba(0,0,0,0.6)";
  ctx.fillStyle = "#ff2d2d";
  ctx.strokeText(letter, p.x, p.y);
  ctx.fillText(letter, p.x, p.y);
}


Object.assign(window, {
  renderWorkspaceImagesTab,
  renderWorkspaceImagesPanel,
  renderWorkspaceImageChips,
  setWorkspaceImageFilter,
  filteredWorkspaceImages,
  workspaceImageFileUrl,
  wsIsVideoFile,
  wsStillImages,
  openWorkspaceVideo,
  renderWorkspaceImageGroups,
  openImageLightbox,
  stepImageLightbox,
  updateImageLightbox,
  closeImageLightbox,
  resetWorkspaceImageState,
  setLightboxTool,
  clearLightboxAnnotation,
  zoomLightbox,
  rotateLightbox,
  saveLightboxOrient,
  flipLightbox,
  setLbAdjust,
  exportLightboxAnnotated,
  toggleCompareMode,
  renderLightboxDateSidebar,
  toggleLbDateBlock,
  pickLightboxImage,
  lbDragStart,
  lbDragOver,
  lbDragLeave,
  lbDrop,
  setCompareActiveSlot,
  updateCompareSlot,
  showLightboxSidebar,
  hideLightboxSidebars,
});
