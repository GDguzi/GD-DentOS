// 影像组图查看:正畸标准模板位版式 + 两期对比 + 导出拼图(纯前端,零后端)。
// 约定:顶层只放常量与函数声明(test_image_grouping_logic.mjs 用 vm 整文件求值),DOM 只在函数内碰。

// 视图位映射:标签逐字对齐 patient_images.category 的实际取值(全量枚举过,别改字)。
const IG_FACE_SLOTS = ["右侧面像", "右侧面微笑像", "右45度像", "右45度微笑像", "正面像", "正面微笑像", "左45度微笑像", "左45度像", "左侧面微笑像", "左侧面像"];
const IG_INTRA_SLOTS = ["上颌合面像", "覆盖像", "右侧咬合像", "正面咬合像", "左侧咬合像", "下颌合面像"];
const IG_XRAY_SLOTS = ["全景片", "正位片", "侧位片", "小牙片"];
// X光tab 里除模板位外还收这些散图分类(放射相关但无标准位)。
// 注意:category=dcm 是 WADO 渲染的高清口内照 JPG(见记忆/台账),是正常可显示图,不算放射散图。
const IG_XRAY_LOOSE = ["CR", "DX", "PX"];

function igDateKey(image) {
  return String(image.image_date || "").slice(0, 10) || "未知日期";
}

// 按拍摄日分组,升序(最早=治疗前 排最上,和 外部系统 一致)
function groupImagesByDate(images) {
  const byDate = new Map();
  for (const im of images) {
    const d = igDateKey(im);
    if (!byDate.has(d)) byDate.set(d, []);
    byDate.get(d).push(im);
  }
  return Array.from(byDate.entries())
    .sort((a, b) => (a[0] < b[0] ? -1 : 1))
    .map(([date, list]) => ({ date, images: list }));
}

// 整月差(不按日修正:2024-06-29→2025-08-18 记 14 个月,同 外部系统 口径),异常/倒序钳 0
function monthsBetween(baseDate, date) {
  const a = String(baseDate).split("-").map(Number);
  const b = String(date).split("-").map(Number);
  if (!a[0] || !b[0]) return 0;
  return Math.max(0, (b[0] - a[0]) * 12 + ((b[1] || 0) - (a[1] || 0)));
}

// 期列表:[{date, months, count, images}],months 以最早一期为 0
function buildPeriodModel(images) {
  const groups = groupImagesByDate(images);
  const base = groups.length ? groups[0].date : "";
  return groups.map(g => ({ date: g.date, months: monthsBetween(base, g.date), count: g.images.length, images: g.images }));
}

// 只有真 .dcm 文件浏览器显示不了;category=dcm 的 rel_path 都是 WADO 渲染的 .jpg,照常显示
function igIsDcm(image) {
  return /\.dcm$/i.test(String(image.file_name || ""));
}

// 单期模板:{slots: {标签→图}, others: [散图]};同位多张后来的顶位(列表序即库序,靠后≈更新)
function buildTemplateModel(dayImages) {
  const slotSet = new Set([].concat(IG_FACE_SLOTS, IG_INTRA_SLOTS, IG_XRAY_SLOTS));
  const slots = {};
  const others = [];
  for (const im of dayImages) {
    const c = String(im.category || "");
    if (slotSet.has(c)) {
      if (slots[c]) others.push(slots[c]);
      slots[c] = im;
    } else {
      others.push(im);
    }
  }
  return { slots, others };
}

// 两期对比:按标准位序出行,任一边有图该行就出;left=较早期,right=较晚期
function buildCompareModel(modelA, modelB) {
  const rows = [];
  for (const label of [].concat(IG_FACE_SLOTS, IG_INTRA_SLOTS, IG_XRAY_SLOTS)) {
    const left = modelA.slots[label] || null;
    const right = modelB.slots[label] || null;
    if (left || right) rows.push({ label, left, right });
  }
  return rows;
}

// 散图区按 tab 过滤:X光tab只看放射类散图,其他照片tab剔除放射类
function splitOthersForTab(others, tab) {
  const isXrayLoose = im => IG_XRAY_LOOSE.includes(String(im.category || "")) || igIsDcm(im);
  if (tab === "xray") return others.filter(isXrayLoose);
  if (tab === "others") return others.filter(im => !isXrayLoose(im));
  return others.slice();
}

// 该期是否为组图:只认落库标记(医生手点"做组图"或历史成套种子),自动不判(产品决策
function igIsSetDate(setDates, date) {
  return Array.isArray(setDates) && setDates.indexOf(date) !== -1;
}

// 对比勾选状态机:最多2期,勾第3个顶掉最早勾的;重复勾=取消
function igToggleCompare(current, date) {
  const next = current.filter(d => d !== date);
  if (next.length === current.length) {
    next.push(date);
    while (next.length > 2) next.shift();
  }
  return next;
}

// 拼图布局:纯计算不碰 DOM(可单测)。格 320x240,口外每排5、口内每排3、X光每排2、散图每排5;dcm 不进拼图。
function igCollagePlan(model) {
  const CW = 320, CH = 240, GAP = 12, HEAD = 64, LABEL = 26;
  const sections = [
    ["口外", IG_FACE_SLOTS.map(l => [l, model.slots[l]]).filter(x => x[1]), 5],
    ["口内", IG_INTRA_SLOTS.map(l => [l, model.slots[l]]).filter(x => x[1]), 3],
    ["X光", IG_XRAY_SLOTS.map(l => [l, model.slots[l]]).filter(x => x[1]), 2],
    ["其他", model.others.filter(im => !igIsDcm(im)).map(im => [String(im.category || ""), im]), 5],
  ].filter(s => s[1].length);
  const maxCols = sections.length ? Math.max.apply(null, sections.map(s => Math.min(s[2], s[1].length))) : 0;
  const width = sections.length ? GAP + maxCols * (CW + GAP) : 0;
  let y = HEAD;
  const cells = [];
  for (const [name, items, colMax] of sections) {
    y += LABEL;
    const cols = Math.min(colMax, items.length);
    items.forEach(([label, image], i) => {
      const col = i % cols, row = Math.floor(i / cols);
      cells.push({ section: name, label, image, x: GAP + col * (CW + GAP), y: y + row * (CH + GAP), w: CW, h: CH });
    });
    y += Math.ceil(items.length / cols) * (CH + GAP) + GAP;
  }
  return { width, height: sections.length ? y : 0, cells };
}

// ---------- 以下为浮层渲染与交互(碰 DOM,依赖 workspace_tabs.js 的全局) ----------

// 浮层状态(单患者会话内;openImageGrouping 时重置)
const igState = { periods: [], activeDate: null, compareDates: [], tab: "all", flatList: [], setDates: [] };

function igEscapeHtml(s) {
  return String(s == null ? "" : s).replace(/[&<>"']/g, c => (
    { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&;" }[c]));
}

function igFileUrl(image) {
  return `/api/patients/${encodeURIComponent(workspacePatientId)}/images/${encodeURIComponent(image.image_id)}/file`;
}

// 一块模板位:有图出图(角标签名),没图虚线占位,dcm 出图标块;idx 是 flatList 下标(-1=不可点)
function igSlotHtml(label, image, idx) {
  if (!image) return `<div class="ig-slot empty">${igEscapeHtml(label)}<br>(缺)</div>`;
  if (igIsDcm(image)) return `<div class="ig-slot dcm">🩻 DCM<span>${igEscapeHtml(image.file_name || "")}</span></div>`;
  return `<div class="ig-slot"><span class="ig-slot-tag">${igEscapeHtml(label)}</span>
    <img src="${igEscapeHtml(igFileUrl(image))}" loading="lazy" data-ig-idx="${idx}" alt="" title="双击打开查看器"></div>`;
}

function openImageGrouping() {
  // 组图只排照片:内窥镜录像(webm/mp4)不进正畸组图
  igState.periods = buildPeriodModel((typeof workspaceImageList !== "undefined" ? (workspaceImageList || []) : [])
    .filter(im => !/\.(webm|mp4)$/i.test(String(im.file_name || ""))));
  igState.setDates = (typeof workspaceImageSetDates !== "undefined" ? workspaceImageSetDates : []) || [];
  if (!igState.periods.length) { alert("该患者暂无影像"); return; }
  igState.activeDate = igState.periods[igState.periods.length - 1].date;  // 默认看最新一期
  igState.compareDates = [];
  igState.tab = "all";
  const ov = document.getElementById("imageGroupingOverlay");
  ov.hidden = false;
  igBindOnce(ov);
  igRender();
}

function closeImageGrouping() {
  const ov = document.getElementById("imageGroupingOverlay");
  if (ov) ov.hidden = true;
}

// 事件委托只绑一次(面板反复重渲染,重复 addEventListener 会累积——见 教训)
function igBindOnce(ov) {
  if (ov.dataset.igBound) return;
  ov.dataset.igBound = "1";
  ov.addEventListener("click", e => {
    const t = e.target;
    if (t.closest("[data-ig-close]")) { closeImageGrouping(); return; }
    const tab = t.closest("[data-ig-tab]");
    if (tab) { igState.tab = tab.dataset.igTab; igRender(); return; }
    const cmp = t.closest("[data-ig-cmp]");
    if (cmp) {
      igState.compareDates = igToggleCompare(igState.compareDates, cmp.dataset.igCmp);
      igRender(); return;
    }
    if (t.closest("[data-ig-export]")) {
      if (typeof igExportCollage === "function") igExportCollage();
      return;
    }
    const mk = t.closest("[data-ig-makeset]");
    if (mk) { igMarkSet(mk.dataset.igMakeset, true); return; }
    const un = t.closest("[data-ig-unset]");
    if (un) { igMarkSet(un.dataset.igUnset, false); return; }
    const period = t.closest("[data-ig-date]");
    if (period) { igState.activeDate = period.dataset.igDate; igRender(); return; }
  });
  ov.addEventListener("dblclick", e => {
    const img = e.target.closest("img[data-ig-idx]");
    if (!img) return;
    const idx = Number(img.dataset.igIdx);
    if (idx >= 0 && typeof openImageLightbox === "function") openImageLightbox(idx, igState.flatList);
  });
}

// 做/撤组图:落库标记后本期照片按标签归位显示(未标签的留散图区,等归位功能)
async function igMarkSet(date, on) {
  const pid = encodeURIComponent(workspacePatientId);
  try {
    const res = on
      ? await fetch(`/api/patients/${pid}/image-sets`, { method: "POST",
          headers: { "Content-Type": "application/json" }, body: JSON.stringify({ image_date: date }) })
      : await fetch(`/api/patients/${pid}/image-sets/${encodeURIComponent(date)}`, { method: "DELETE" });
    if (!res.ok) { alert("操作失败,请重试"); return; }
  } catch { alert("操作失败,请重试"); return; }
  igState.setDates = on
    ? igState.setDates.concat(igState.setDates.includes(date) ? [] : [date])
    : igState.setDates.filter(d => d !== date);
  if (typeof workspaceImageSetDates !== "undefined") workspaceImageSetDates = igState.setDates.slice();
  if (!on) igState.compareDates = igState.compareDates.filter(d => d !== date);
  igRender();
}

function igActivePeriod() {
  return igState.periods.find(p => p.date === igState.activeDate) || igState.periods[0];
}

// 登记进 flatList 并返回下标(lightbox 循环用;空位/dcm 不登记,返回 -1)
function igReg(image) {
  if (!image || igIsDcm(image)) return -1;
  igState.flatList.push(image);
  return igState.flatList.length - 1;
}

function igRender() {
  const ov = document.getElementById("imageGroupingOverlay");
  const p = (typeof workspaceData === "object" && workspaceData && workspaceData.patient) || {};
  const comparing = igState.compareDates.length === 2;
  igState.flatList = [];
  const side = igState.periods.map(x => {
    const hasSet = igIsSetDate(igState.setDates, x.date);
    const cmp = hasSet
      ? `<label><input type="checkbox" data-ig-cmp="${igEscapeHtml(x.date)}"${igState.compareDates.includes(x.date) ? " checked" : ""}> 对比</label>`
      : "";
    return `
    <div class="ig-period${x.date === igState.activeDate && !comparing ? " active" : ""}" data-ig-date="${igEscapeHtml(x.date)}">
      <div class="ig-m">${x.months ? "+" + x.months + " 个月" : "0 个月"}</div>
      <div class="ig-d">${igEscapeHtml(x.date)}</div>
      <div class="ig-n">${x.count} 张${hasSet ? ' · <span class="ig-set-badge">正畸组图</span>' : ""}</div>
      ${cmp}
    </div>`;
  }).join("");
  const tabs = [["all", "全类影像"], ["xray", "X光影像"], ["face", "面型照"], ["intra", "口内照"], ["others", "其他照片"]]
    .map(([k, name]) => `<button type="button" class="ig-tab${igState.tab === k ? " active" : ""}" data-ig-tab="${k}">${name}</button>`).join("");
  const main = comparing ? igRenderCompare() : igRenderSingle();
  ov.innerHTML = `
    <div class="ig-head"><h2>正畸组图</h2>
      <span class="ig-meta">${igEscapeHtml(p.display_name || "")}　No. ${igEscapeHtml(typeof workspacePatientId !== "undefined" ? workspacePatientId : "")}</span>
      <button type="button" class="plain-button" data-ig-export>🖼 导出拼图</button>
      <label class="ig-meta"><input type="checkbox" id="igExportName" checked> 拼图含姓名</label>
      <button type="button" class="ig-close" data-ig-close title="关闭">✕</button></div>
    <div class="ig-body">
      <div class="ig-side"><div class="ig-side-title">拍摄记录(勾两期可对比)</div>${side}</div>
      <div class="ig-main"><div class="ig-tabs">${tabs}</div>${main}</div>
    </div>`;
}

function igRenderZones(model) {
  const tab = igState.tab;
  let html = "";
  if (tab === "all" || tab === "face") {
    html += `<div class="ig-zone-label">口外</div><div class="ig-grid ig-grid-face">
      ${IG_FACE_SLOTS.map(l => igSlotHtml(l, model.slots[l], igReg(model.slots[l]))).join("")}</div>`;
  }
  if (tab === "all" || tab === "intra" || tab === "xray") {
    const intra = (tab !== "xray") ? `<div style="flex:3"><div class="ig-zone-label">口内</div>
      <div class="ig-grid ig-grid-intra">
        <div></div>${igSlotHtml("上颌合面像", model.slots["上颌合面像"], igReg(model.slots["上颌合面像"]))}${igSlotHtml("覆盖像", model.slots["覆盖像"], igReg(model.slots["覆盖像"]))}
        ${["右侧咬合像", "正面咬合像", "左侧咬合像"].map(l => igSlotHtml(l, model.slots[l], igReg(model.slots[l]))).join("")}
        <div></div>${igSlotHtml("下颌合面像", model.slots["下颌合面像"], igReg(model.slots["下颌合面像"]))}<div></div>
      </div></div>` : "";
    const xray = (tab !== "intra") ? `<div style="flex:2"><div class="ig-zone-label">X 光</div>
      <div class="ig-grid ig-grid-xray">
        ${IG_XRAY_SLOTS.map(l => igSlotHtml(l, model.slots[l], igReg(model.slots[l]))).join("")}
      </div></div>` : "";
    html += `<div class="ig-x2">${intra}${xray}</div>`;
  }
  // 散图区只在 all/xray/others 三个 tab 出现(face/intra 是纯模板位视角)
  if (tab === "all" || tab === "xray" || tab === "others") {
    const loose = splitOthersForTab(model.others, tab);
    if (loose.length) {
      html += `<div class="ig-zone-label">当天其他照片(${loose.length})</div><div class="ig-grid ig-grid-loose">
        ${loose.map(im => igSlotHtml(im.category || "未分类", im, igReg(im))).join("")}</div>`;
    }
  }
  return html;
}

function igRenderSingle() {
  const period = igActivePeriod();
  const model = buildTemplateModel(period.images);
  // 未标组图的日期:不摆空模板,平铺当天全部照片 + 「做组图」按钮(点了才在本期范围内归位成组)
  if (!igIsSetDate(igState.setDates, period.date)) {
    if (igState.tab === "face" || igState.tab === "intra") {
      return '<div class="workspace-placeholder">本期未做正畸组图</div>';
    }
    const all = period.images.filter(im => igState.tab === "all"
      || splitOthersForTab([im], igState.tab).length);
    const makeBtn = `<button type="button" class="plain-button" data-ig-makeset="${igEscapeHtml(period.date)}">🗂 做正畸组图(把本期照片归到标准位)</button>`;
    if (!all.length) return `<div class="ig-toolbar">${makeBtn}</div><div class="workspace-placeholder">该分类暂无影像</div>`;
    return `<div class="ig-toolbar">${makeBtn}<span class="ig-zone-label" style="margin:0">本期照片(${all.length})</span></div>
      <div class="ig-grid ig-grid-loose">
      ${all.map(im => igSlotHtml(im.category || "未分类", im, igReg(im))).join("")}</div>`;
  }
  const unsetBtn = `<div class="ig-toolbar"><button type="button" class="plain-button" data-ig-unset="${igEscapeHtml(period.date)}">撤销正畸组图(恢复普通照片列表)</button></div>`;
  return (igRenderZones(model) || '<div class="workspace-placeholder">该分类暂无影像</div>') + unsetBtn;
}

function igLoadImage(url) {
  return new Promise(res => {
    const img = new Image();
    img.onload = () => res(img);
    img.onerror = () => res(null);
    img.src = url;
  });
}

// 导出拼图:当前单期模板区 canvas 拼成一张 png 下载(纯本地生成,不出机器)
async function igExportCollage() {
  if (igState.compareDates.length === 2) { alert("对比模式暂不支持导出,先取消对比勾选"); return; }
  const period = igActivePeriod();
  // 与单期渲染同口径:未手点成组的日期不导出「正畸组图」
  if (!igIsSetDate(igState.setDates, period.date)) { alert("本期未做正畸组图,先点「做正畸组图」把照片归位再导出"); return; }
  const plan = igCollagePlan(buildTemplateModel(period.images));
  if (!plan.cells.length) { alert("本期没有可导出的照片"); return; }
  const canvas = document.createElement("canvas");
  canvas.width = plan.width;
  canvas.height = plan.height;
  const ctx = canvas.getContext("2d");
  ctx.fillStyle = "#ffffff";
  ctx.fillRect(0, 0, plan.width, plan.height);
  const nameBox = document.getElementById("igExportName");
  const withName = !nameBox || nameBox.checked;
  const p = (typeof workspaceData === "object" && workspaceData && workspaceData.patient) || {};
  ctx.fillStyle = "#1a2230";
  ctx.font = "bold 26px sans-serif";
  ctx.fillText(`${withName ? (p.display_name || "") + "　" : ""}${period.date}　正畸组图`, 16, 40);
  ctx.font = "14px sans-serif";
  for (const cell of plan.cells) {
    const img = await igLoadImage(igFileUrl(cell.image));
    ctx.fillStyle = "#eef1f5";
    ctx.fillRect(cell.x, cell.y, cell.w, cell.h);
    if (img) {
      const r = Math.min(cell.w / img.width, cell.h / img.height);
      const w = img.width * r, h = img.height * r;
      ctx.drawImage(img, cell.x + (cell.w - w) / 2, cell.y + (cell.h - h) / 2, w, h);
    }
    if (cell.label) {
      ctx.fillStyle = "rgba(20,26,36,.72)";
      ctx.fillRect(cell.x, cell.y, Math.min(cell.w, 12 + cell.label.length * 14), 22);
      ctx.fillStyle = "#fff";
      ctx.fillText(cell.label, cell.x + 6, cell.y + 16);
    }
  }
  const a = document.createElement("a");
  a.href = canvas.toDataURL("image/png");
  a.download = `正畸组图_${typeof workspacePatientId !== "undefined" ? workspacePatientId : ""}_${period.date}.png`;
  a.click();
}

function igRenderCompare() {
  const dates = igState.compareDates.slice().sort();
  const pA = igState.periods.find(p => p.date === dates[0]);
  const pB = igState.periods.find(p => p.date === dates[1]);
  if (!pA || !pB) return "";
  const rows = buildCompareModel(buildTemplateModel(pA.images), buildTemplateModel(pB.images));
  if (!rows.length) return '<div class="workspace-placeholder">两期没有可对比的标准位照片</div>';
  const head = `<div class="ig-cmp-row"><div class="ig-cmp-label">视图位</div>
    <div class="ig-zone-label">${igEscapeHtml(dates[0])}(早)</div><div class="ig-zone-label">${igEscapeHtml(dates[1])}(晚)</div></div>`;
  return head + rows.map(r => `<div class="ig-cmp-row">
    <div class="ig-cmp-label">${igEscapeHtml(r.label)}</div>
    ${igSlotHtml(r.label, r.left, igReg(r.left))}${igSlotHtml(r.label, r.right, igReg(r.right))}
  </div>`).join("");
}
