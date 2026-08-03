// 病历编辑器：对标官方"病历填写"版面（docs/官方UI截图/04/05）。
// 左栏 = 病历模版目录树 + 科目导航；右栏 = 详细病历数据（文本科目 + 带牙位的条目科目）。
// 检查/诊断/治疗方案/治疗 四类条目存 medical_record_items（牙位×类型×内容），
// 文本科目仍存 content_json（保存时与原 JSON 合并，未知科目键不丢失）。
// 由工作区病历 tab（workspace_tabs.js 的 editWorkspaceMedicalCard）调用。

const toothModal = document.getElementById("toothModal");
const toothGrid = document.getElementById("toothGrid");
const toothSelectionText = document.getElementById("toothSelectionText");

let activeToothRecordId = "";
// #41：缓存最近聚焦的条目输入框。点快捷词条按钮时焦点已落到按钮上，
// document.activeElement 拿不到用户刚编辑的行，故用 focusin 提前记下。
let lastFocusedItemContent = null;
document.addEventListener("focusin", (e) => {
  const t = e.target;
  if (t && t.classList && t.classList.contains("item-content")) {
    lastFocusedItemContent = t;
  }
});
let activeToothSelection = {};
let activeToothItemRow = null; // 牙位选择器当前服务的条目行（DOM元素）
let toothSelectCallback = null; // 通用回调模式：确定时把 FDI 牙位码数组交给调用方（如治疗计划行）

// 条目科目（带牙位、可多条）：官方"检查/诊断/治疗方案/治疗"
const ITEM_SUBJECTS = [
  {type: "exam", label: "检查", textKey: "Exam"},
  {type: "diagnosis", label: "诊断", textKey: "DG"},
  {type: "plan", label: "治疗方案", textKey: ""},
  {type: "treatment", label: "治疗", textKey: "TR"},
];

// 检查栏常用结果快捷词条：点一下加一条预填条目（再选牙位）。常见牙科检查所见。
const EXAM_QUICK = [
  "龋坏", "牙体缺损", "残根", "残冠", "继发龋", "充填体脱落", "楔状缺损",
  "冷热刺激痛", "叩痛(+)", "叩痛(-)", "松动Ⅰ°", "松动Ⅱ°", "松动Ⅲ°",
  "牙龈红肿", "探诊出血", "牙石(+)", "食物嵌塞", "牙周袋形成",
];

// 文本科目（官方顺序：主诉/现病史/既往史在前，辅助检查/医嘱/护理在后）
const TEXT_SUBJECTS_TOP = [
  ["主诉", "PC"],
  ["现病史", "HPI"],
  ["既往史", "PI"],
];
const TEXT_SUBJECTS_BOTTOM = [
  ["辅助检查", "AE"],
  ["医嘱", "Plan"],
];

// SaaS 结构化科目预处理：条目科目(Exam/DG/TR)的 {"item":[...]} 拆成牙位条目行，
// 文本科目转可读文本。返回 {content: 处理后的科目文本, initialRows: {exam:[{teeth,text}],...}}
function explodeSaasContent(rawContent) {
  const content = {...rawContent};
  const initialRows = {};
  for (const subject of ITEM_SUBJECTS) {
    if (!subject.textKey) continue;
    const entries = parseSaasSubject(content[subject.textKey]);
    if (entries === null) continue;
    initialRows[subject.type] = entries;
    content[subject.textKey] = "";
  }
  for (const key of ["PC", "HPI", "PI", "AE", "Plan", "DA", "Nurse"]) {
    const text = saasSubjectToText(content[key]);
    if (text !== null) content[key] = text;
  }
  return {content, initialRows};
}

function renderMedicalRecord(row) {
  const exploded = explodeSaasContent(parseMedicalContent(row.content_json));
  const content = exploded.content;
  const initialRows = exploded.initialRows;
  const recordType = (row.record_type || "").trim();
  const followUp = recordType === "复诊";
  const subjectNav = [
    ...TEXT_SUBJECTS_TOP.map(([label]) => label),
    ...ITEM_SUBJECTS.map(s => s.label),
    ...TEXT_SUBJECTS_BOTTOM.map(([label]) => label),
  ];
  // 照 #77 快照范式:打开编辑器那一刻把当前患者ID快照进编辑器根节点,
  // 新建保存只认这个快照——编辑期间浏览器后退等切了患者,病历也不会写进别人档案。
  return `
    <div class="record-row medical-record-editor official-editor" data-record-id="${escapeAttr(row.record_id)}" data-patient-snapshot="${escapeAttr(workspacePatientId)}">
      <div class="editor-topbar">
        <label>医生
          <input data-record-field="doctor_name" data-staff-role="医生" value="${escapeAttr(row.doctor_name || "")}" placeholder="检查医生">
        </label>
        <label>就诊日期
          <input data-record-field="visit_time" value="${escapeAttr(String(row.visit_time || row.updated_at || "").replace(/\.\d+$/, ""))}" placeholder="YYYY-MM-DD HH:MM">
        </label>
      </div>
      <div class="editor-layout">
        <aside class="editor-left">
          <div class="editor-left-tabs">
            <span class="editor-left-tab active">病历模版</span>
            ${["病历词条", "特殊符号", "处置记录", "历史病历"].map(t =>
              `<span class="editor-left-tab disabled" title="本地版暂未开通">${t}</span>`
            ).join("")}
          </div>
          <div class="template-tree" data-template-tree>模板载入中...</div>
          <div class="editor-left-head">科目导航</div>
          <nav class="subject-nav">
            ${subjectNav.map(label => {
              const fvOnly = label === "现病史" || label === "既往史";
              return `
              <a href="javascript:void(0)"${fvOnly ? ' data-firstvisit-only="1"' : ""}${fvOnly && followUp ? ' style="display:none"' : ""} onclick="scrollToSubject(this, '${escapeAttr(label)}')">${escapeHtml(label)}</a>`;
            }).join("")}
          </nav>
        </aside>
        <section class="editor-main">
          <div class="editor-main-title-row">
            <div class="editor-main-title">详细病历数据</div>
            <span class="editor-record-type" data-record-type-group>
              病历类型：
              ${["初诊", "复诊"].map(t => `
                <label class="record-type-radio">
                  <input type="radio" name="recordType-${escapeAttr(row.record_id)}"
                    value="${t}" ${recordType === t ? "checked" : ""}
                    onchange="applyRecordTypeVisibility(this)"> ${t}
                </label>
              `).join("")}
              <input type="text" data-record-field="record_type" value="${escapeAttr(recordType)}" class="record-type-free" placeholder="其他">
            </span>
          </div>
          <div class="medical-chart">
            ${TEXT_SUBJECTS_TOP.map(([label, key]) => {
              const fvOnly = key === "HPI" || key === "PI";
              return medicalChartField(label, key, content[key], fvOnly, fvOnly && followUp);
            }).join("")}
            ${ITEM_SUBJECTS.map(s => itemSubjectBlock(s, content, initialRows[s.type] || [])).join("")}
            ${TEXT_SUBJECTS_BOTTOM.map(([label, key]) =>
              medicalChartField(label, key, key === "Plan" ? (content.Plan || content.DA) : content[key])
            ).join("")}
          </div>
        </section>
      </div>
      <textarea data-record-field="content_json" class="medical-json-field" hidden>${escapeHtml(JSON.stringify(content))}</textarea>
      <textarea data-record-field="tooth_json" class="tooth-json-field" hidden>${escapeHtml(row.tooth_json || "{}")}</textarea>
      <div class="record-actions editor-footer">
        <button type="button" class="plain-button" onclick="closeMedicalEditorOverlay()">取消/返回</button>
        <button type="button" class="plain-button" onclick="saveAsTemplate('${escapeAttr(row.record_id)}')">另存为模板</button>
        <button type="button" class="plain-button" onclick="saveMedicalRecord('${escapeAttr(row.record_id)}', {keepOpen: true})">暂存</button>
        <button type="button" class="editor-save-btn" onclick="saveMedicalRecord('${escapeAttr(row.record_id)}')">保存</button>
        <span class="record-save-status" id="recordStatus-${escapeAttr(row.record_id)}"></span>
      </div>
      ${recordVersionList("病历", "medical_record", row.record_id)}
    </div>
  `;
}

function parseMedicalContent(raw) {
  if (!raw) return {};
  try {
    const parsed = JSON.parse(raw);
    if (parsed && typeof parsed === "object" && !Array.isArray(parsed)) return parsed;
  } catch {
    return {PC: raw};
  }
  return {PC: String(raw)};
}

function medicalChartField(label, key, value, firstVisitOnly = false, hidden = false) {
  return `
    <div class="chart-row" data-subject="${escapeAttr(label)}"${firstVisitOnly ? ' data-firstvisit-only="1"' : ""}${hidden ? ' style="display:none"' : ""}>
      <div class="chart-label">${label}</div>
      <div class="chart-content">
        <textarea class="chart-textarea" data-medical-field="${escapeAttr(key)}" placeholder="${label}">${escapeHtml(value || "")}</textarea>
      </div>
    </div>
  `;
}

// 复诊不需要现病史(HPI)/既往史(PI)：按病历类型显隐 data-firstvisit-only 的科目字段与导航项
function applyRecordTypeVisibility(input) {
  const editor = input.closest(".medical-record-editor");
  if (!editor) return;
  const followUp = input.value === "复诊";
  editor.querySelectorAll('[data-firstvisit-only="1"]').forEach(el => {
    el.style.display = followUp ? "none" : "";
  });
}

// 条目科目块：整体文本（如有）+ 逐牙条目行 + 右侧"+"（对标官方每行牙位格+加号）
function itemSubjectBlock(subject, content, initialRows = []) {
  // 检查/诊断/治疗：没有已存条目时默认摆一行空条目，牙位选择器直接可见可点（治疗方案除外）
  const rows = initialRows.length
    ? initialRows
    : (subject.type === "plan" ? [] : [{teeth: [], text: ""}]);
  return `
    <div class="chart-row item-subject" data-subject="${escapeAttr(subject.label)}" data-item-type="${escapeAttr(subject.type)}">
      <div class="chart-label">
        <span>${subject.label}</span>
        <button type="button" class="item-add-btn" title="新增${subject.label}条目" onclick="addRecordItemRow(this)">+ 加条目</button>
      </div>
      <div class="chart-content">
        ${subject.textKey ? `
          <textarea class="chart-textarea item-subject-text" data-medical-field="${escapeAttr(subject.textKey)}"
            placeholder="${subject.label}（整体描述）">${escapeHtml(content[subject.textKey] || "")}</textarea>
        ` : ""}
        <div class="item-rows" data-item-rows>${rows.map(r => recordItemRow(r.teeth, r.text)).join("")}</div>
        ${subject.type === "exam" ? `
          <div class="exam-quick" title="点词条加一条检查、再选牙位">
            <button type="button" class="exam-suggest-btn" onclick="suggestExamByDiagnosis(this)" title="按已填诊断推荐常见检查项">💡按诊断推荐</button>
            ${EXAM_QUICK.map(t => `<button type="button" class="exam-quick-btn" onclick="addExamQuick(this, '${escapeAttr(t)}')">${escapeHtml(t)}</button>`).join("")}
          </div>` : ""}
      </div>
    </div>
  `;
}

// 检查快捷词条：在检查栏加一条预填该词条的条目（牙位待选）
function addExamQuick(btn, text) {
  // #16：多词拼进"当前"条目行（空格隔开），不再每点一个新增一行。
  // 目标行：优先用户正聚焦的条目输入框，其次最后一行；都没有才新建一行。
  const block = btn.closest(".item-subject");
  const rows = block.querySelector("[data-item-rows]");
  let target = null;
  // #41：用 focusin 缓存的"最近聚焦输入框"（须仍在文档内且属于本科目块），
  // 而非 document.activeElement（点按钮后焦点已在按钮上）。
  const cached = lastFocusedItemContent;
  if (cached && cached.isConnected && block.contains(cached)) {
    target = cached;
  }
  if (!target) {
    const inputs = rows.querySelectorAll(".item-content");
    target = inputs.length ? inputs[inputs.length - 1] : null;
  }
  if (!target) {
    rows.insertAdjacentHTML("beforeend", recordItemRow([], ""));
    target = rows.querySelector(".item-content");
  }
  const cur = (target.value || "").trim();
  target.value = cur ? cur + " " + text : text;
  target.focus();
  if (typeof markMedicalEditorDirty === "function") markMedicalEditorDirty();  // #335
}

// 病种→建议检查：诊断里出现某关键词，推荐对应常见检查项
const DIAGNOSIS_EXAM_HINTS = {
  "龋": ["龋坏", "探及龋洞", "冷热刺激痛"],
  "牙髓": ["叩痛(+)", "冷热刺激痛", "夜间自发痛", "温度测试异常"],
  "根尖": ["叩痛(+)", "根尖部压痛", "牙龈瘘管", "X线根尖暗影"],
  "牙周": ["探诊出血", "牙周袋形成", "牙石(+)", "松动度"],
  "牙龈": ["牙龈红肿", "探诊出血", "牙石(+)"],
  "智齿": ["阻生", "冠周红肿", "张口受限"],
  "阻生": ["阻生", "冠周红肿", "X线阻生牙位"],
  "外伤": ["牙体缺损", "牙折", "松动度", "叩痛(+)"],
  "牙折": ["牙体缺损", "牙折", "松动度"],
  "楔状": ["楔状缺损", "冷热刺激痛"],
  "缺失": ["缺牙间隙", "邻牙倾斜", "对颌牙伸长"],
};

// 按已填诊断推荐常见检查项，一键加入检查栏（去重，不覆盖已有）
function suggestExamByDiagnosis(btn) {
  const editor = btn.closest(".medical-record-editor");
  if (!editor) return;
  let dgText = "";
  const dgBlock = editor.querySelector('.item-subject[data-item-type="diagnosis"]');
  if (dgBlock) {
    const ta = dgBlock.querySelector('[data-medical-field="DG"]');
    if (ta) dgText += " " + ta.value;
    dgBlock.querySelectorAll(".item-content").forEach(i => { dgText += " " + i.value; });
  }
  const examBlock = btn.closest(".item-subject");
  const rows = examBlock.querySelector("[data-item-rows]");
  // 已有内容，避免重复加
  const existing = new Set();
  rows.querySelectorAll(".item-content").forEach(i => { if (i.value.trim()) existing.add(i.value.trim()); });
  const toAdd = [];
  for (const [kw, exams] of Object.entries(DIAGNOSIS_EXAM_HINTS)) {
    if (dgText.includes(kw)) {
      exams.forEach(e => { if (!existing.has(e) && !toAdd.includes(e)) toAdd.push(e); });
    }
  }
  if (!toAdd.length) {
    btn.textContent = dgText.trim() ? "💡无匹配建议" : "💡先填诊断";
    setTimeout(() => { btn.textContent = "💡按诊断推荐"; }, 1500);
    return;
  }
  toAdd.forEach(e => rows.insertAdjacentHTML("beforeend", recordItemRow([], e)));
  if (typeof markMedicalEditorDirty === "function") markMedicalEditorDirty();  // #335
}

function recordItemRow(teeth, content) {
  const teethAttr = escapeAttr((teeth || []).join(","));
  return `
    <div class="item-row" data-teeth="${teethAttr}">
      <button type="button" class="item-tooth-cell" onclick="openItemToothSelector(this)"
        title="选择牙位">${(teeth && teeth.length) ? toothCrossHtml(teeth) : toothCrossHtml([])}</button>
      <input type="text" class="item-content" value="${escapeAttr(content || "")}" placeholder="条目内容">
      <button type="button" class="item-remove-btn" title="删除条目" onclick="this.closest('.item-row').remove(); markMedicalEditorDirty && markMedicalEditorDirty()">×</button>
    </div>
  `;
}

function formatTeethShort(teeth) {
  if (!teeth || !teeth.length) return "牙位";
  return teeth.join(" ");
}

function addRecordItemRow(btn) {
  const block = btn.closest(".item-subject");
  const rows = block.querySelector("[data-item-rows]");
  rows.insertAdjacentHTML("beforeend", recordItemRow([], ""));
}

function scrollToSubject(link, label) {
  const editor = link.closest(".medical-record-editor");
  const row = editor.querySelector(`[data-subject="${cssEscape(label)}"]`);
  if (row) row.scrollIntoView({behavior: "smooth", block: "center"});
}

// ---------- 编辑器装载：模板树 + 已有条目 ----------

// renderMedicalRecord 插入 DOM 后由调用方触发（workspace_tabs.js）
async function hydrateMedicalEditor(recordId) {
  const editor = document.querySelector(`.medical-record-editor[data-record-id="${cssEscape(recordId)}"]`);
  if (!editor) return;
  await Promise.all([
    loadEditorTemplateTree(editor),
    loadEditorRecordItems(editor, recordId),
  ]);
}

async function loadEditorTemplateTree(editor) {
  const tree = editor.querySelector("[data-template-tree]");
  if (!tree) return;
  let data;
  try {
    const res = await fetch("/api/medical-templates");
    if (!res.ok) throw new Error(String(res.status));
    data = await res.json();
  } catch {
    tree.textContent = "模板载入失败";
    return;
  }
  const categories = data.categories || [];
  if (!categories.length) {
    tree.textContent = "暂无模板";
    return;
  }
  window.medicalTemplateCache = window.medicalTemplateCache || {};
  tree.innerHTML = categories.map(group => `
    <div class="template-folder">
      <div class="template-folder-name" onclick="toggleTemplateFolder(this)">📁 ${escapeHtml(group.category || "未分类")}</div>
      <div class="template-folder-items" hidden>
        ${(group.templates || []).map(tpl => {
          window.medicalTemplateCache[tpl.template_id] = tpl.content_json || {};
          return `<a href="javascript:void(0)" onclick="applyTemplateFromTree(this, '${escapeAttr(tpl.template_id)}')">${escapeHtml(tpl.template_name)}</a>`;
        }).join("")}
      </div>
    </div>
  `).join("");
}

function toggleTemplateFolder(el) {
  const items = el.nextElementSibling;
  if (items) items.hidden = !items.hidden;
}

// 套用模板：填充编辑器各文本科目（只覆盖模板里有值的科目），与官方点模板行为一致。
// 适配：模板科目存为 SaaS 富文本——可能是 JSON 结构化({item:[]})或 XML(<?xml..><span>)，
// 先走 saasSubjectToText(JSON)，再走 saasXmlToText(XML)，都不是才原样，避免把 XML 标签塞进文本框。
function applyTemplateFromTree(link, templateId) {
  const content = (window.medicalTemplateCache || {})[templateId];
  const editor = link.closest(".medical-record-editor");
  if (!content || !editor) return;
  editor.querySelectorAll("[data-medical-field]").forEach(input => {
    const raw = content[input.dataset.medicalField];
    if (raw === undefined) return;
    let text = saasSubjectToText(raw);          // JSON 结构化科目
    if (text === null) text = saasXmlToText(raw);  // XML 富文本科目 / 纯文本
    input.value = text;
  });
  if (typeof markMedicalEditorDirty === "function") markMedicalEditorDirty();  // #335
}

async function loadEditorRecordItems(editor, recordId) {
  if (recordId === "new") return;
  let items;
  try {
    const res = await fetch(`/api/medical-records/${encodeURIComponent(recordId)}/items`);
    if (!res.ok) throw new Error(String(res.status));
    items = (await res.json()).items || [];
  } catch {
    return; // 条目载入失败不阻塞编辑器
  }
  // 相邻同类型同内容的条目并成一行多牙位（保存时再展开）
  const rowsByType = {};
  for (const item of items) {
    const rows = rowsByType[item.item_type] = rowsByType[item.item_type] || [];
    const last = rows[rows.length - 1];
    if (last && last.content === item.content) {
      if (item.tooth) last.teeth.push(item.tooth);
    } else {
      rows.push({teeth: item.tooth ? [item.tooth] : [], content: item.content});
    }
  }
  for (const subject of ITEM_SUBJECTS) {
    const block = editor.querySelector(`.item-subject[data-item-type="${subject.type}"] [data-item-rows]`);
    if (!block) continue;
    const rows = rowsByType[subject.type] || [];
    // 库里没有该类条目时保留初始行（SaaS 结构化科目解析出来的，首次保存才落库）
    if (rows.length) {
      block.innerHTML = rows.map(r => recordItemRow(r.teeth, r.content)).join("");
    }
  }
}

// ---------- 牙位选择器（对标官方"选择牙位"弹窗，docs/官方UI截图/05） ----------

function toothQuadrants() {
  return [
    ["右上", "RT", [8, 7, 6, 5, 4, 3, 2, 1]],
    ["左上", "LT", [1, 2, 3, 4, 5, 6, 7, 8]],
    ["右下", "RB", [8, 7, 6, 5, 4, 3, 2, 1]],
    ["左下", "LB", [1, 2, 3, 4, 5, 6, 7, 8]],
  ];
}

// FDI 编码转换：象限码+牙号 ↔ FDI 两位码
// 恒牙象限 1=右上 2=左上 3=左下 4=右下；乳牙象限 5=右上 6=左上 7=左下 8=右下
const QUAD_NAME = {RT: "右上", LT: "左上", LB: "左下", RB: "右下"};   // #373 牙位象限可读名
const FDI_QUADRANT = {RT: "1", LT: "2", LB: "3", RB: "4"};
const QUADRANT_BY_FDI = {1: "RT", 2: "LT", 3: "LB", 4: "RB"};
const FDI_DECIDUOUS_QUADRANT = {RT: "5", LT: "6", LB: "7", RB: "8"};
const DECIDUOUS_QUADRANT_BY_FDI = {5: "RT", 6: "LT", 7: "LB", 8: "RB"};
const DECIDUOUS_LETTERS = ["A", "B", "C", "D", "E"]; // A=1(乳中切) ... E=5(第二乳磨)

let activeDeciduousSelection = {}; // {RT: ["E","D"], ...}

function selectionToFdi(selection, deciduous) {
  const out = [];
  for (const [quadrant, digit] of Object.entries(FDI_QUADRANT)) {
    for (const tooth of selection[quadrant] || []) out.push(digit + tooth);
  }
  for (const [quadrant, digit] of Object.entries(FDI_DECIDUOUS_QUADRANT)) {
    for (const letter of (deciduous || {})[quadrant] || []) {
      out.push(digit + String(DECIDUOUS_LETTERS.indexOf(letter) + 1));
    }
  }
  return out.sort();
}

function fdiToSelection(codes) {
  const selection = {};
  for (const code of codes || []) {
    const quadrant = QUADRANT_BY_FDI[String(code)[0]];
    const tooth = String(code).slice(1);
    if (!quadrant || !tooth) continue;
    (selection[quadrant] = selection[quadrant] || []).push(tooth);
  }
  return selection;
}

function fdiToDeciduousSelection(codes) {
  const selection = {};
  for (const code of codes || []) {
    const quadrant = DECIDUOUS_QUADRANT_BY_FDI[String(code)[0]];
    const letter = DECIDUOUS_LETTERS[Number(String(code).slice(1)) - 1];
    if (!quadrant || !letter) continue;
    (selection[quadrant] = selection[quadrant] || []).push(letter);
  }
  return selection;
}

// 牙形 SVG 图标（切牙/尖牙/前磨牙/磨牙），下牙弓垂直镜像——对标官方牙列图
function toothIconSvg(num, upper) {
  const n = Number(num);
  let path;
  if (n <= 2) {
    path = "M9 30 L10 12 Q12 4 14 12 L15 30 Q12 34 9 30 Z"; // 切牙：窄冠单根
  } else if (n === 3) {
    path = "M8 30 L11 8 Q12 4 13 8 L16 30 Q12 35 8 30 Z"; // 尖牙：尖冠
  } else if (n <= 5) {
    path = "M7 28 L8 12 Q12 6 16 12 L17 28 Q12 33 7 28 Z M10 14 Q12 17 14 14"; // 前磨
  } else {
    path = "M4 28 L5 12 Q8 6 12 9 Q16 6 19 12 L20 28 Q16 32 12 30 Q8 32 4 28 Z"; // 磨牙：宽冠双尖
  }
  const transform = upper ? "" : ' transform="scale(1,-1) translate(0,-36)"';
  return `<svg viewBox="0 0 24 36" class="tooth-icon-svg"><g${transform}><path d="${path}" fill="#f4f6f8" stroke="#aeb9c4" stroke-width="1.2"/></g></svg>`;
}

// 条目行的牙位格点击入口
function openItemToothSelector(cell) {
  activeToothItemRow = cell.closest(".item-row");
  activeToothRecordId = "";
  const teeth = (activeToothItemRow.dataset.teeth || "").split(",").filter(Boolean);
  activeToothSelection = fdiToSelection(teeth);
  activeDeciduousSelection = fdiToDeciduousSelection(teeth);
  renderToothGrid();
  toothModal.hidden = false;
}

// 通用入口：传入初始 FDI 牙位(逗号/空格分隔字符串) + 回调，确定时把 FDI 码数组交给回调。
// 供病历页之外的模块（如治疗计划行）复用同一个官方 FDI 牙位弹窗。
function openToothSelectorWith(teethCsv, callback) {
  activeToothItemRow = null;
  activeToothRecordId = "";
  toothSelectCallback = callback || null;
  const teeth = String(teethCsv || "").split(/[\s,，]+/).filter(Boolean);
  activeToothSelection = fdiToSelection(teeth);
  activeDeciduousSelection = fdiToDeciduousSelection(teeth);
  renderToothGrid();
  toothModal.hidden = false;
}

// 旧入口保留（按 record_id 编辑整体牙位；牙位徽标等处仍可用）
function openToothSelector(recordId) {
  activeToothRecordId = recordId;
  activeToothItemRow = null;
  toothSelectCallback = null;
  const container = document.querySelector(`.medical-record-editor[data-record-id="${cssEscape(recordId)}"]`);
  const raw = container ? container.querySelector('[data-record-field="tooth_json"]').value.trim() : "{}";
  activeToothSelection = parseToothJson(raw);
  activeDeciduousSelection = {};
  renderToothGrid();
  toothModal.hidden = false;
}

function closeToothSelector() {
  toothModal.hidden = true;
  activeToothItemRow = null;
  toothSelectCallback = null;
}

// 官方快捷选区：乳牙/全口/左上/左下/右上/右下/上半口/下半口/清除
function applyToothQuickPick(kind) {
  const FULL = ["1", "2", "3", "4", "5", "6", "7", "8"];
  if (kind === "清除") {
    activeToothSelection = {};
    activeDeciduousSelection = {};
    renderToothGrid();
    return;
  }
  if (kind === "乳牙") {
    // toggle：乳牙全选中→整体取消，否则整体选上（不动恒牙，可与恒牙区共存）
    const all = [...DECIDUOUS_LETTERS];
    const full = ["RT", "LT", "RB", "LB"].every(
      q => all.every(l => (activeDeciduousSelection[q] || []).includes(l))
    );
    activeDeciduousSelection = full ? {} : {RT: [...all], LT: [...all], RB: [...all], LB: [...all]};
    renderToothGrid();
    return;
  }
  const sets = {
    "全口": {RT: FULL, LT: FULL, RB: FULL, LB: FULL},
    "左上": {LT: FULL}, "左下": {LB: FULL}, "右上": {RT: FULL}, "右下": {RB: FULL},
    "上半口": {RT: FULL, LT: FULL}, "下半口": {RB: FULL, LB: FULL},
  };
  if (!(kind in sets)) return;
  const target = sets[kind];
  // toggle：该区的牙若已全部选中→移除该区，否则并入该区；多个区可共存
  const allSelected = Object.entries(target).every(
    ([q, teeth]) => teeth.every(t => (activeToothSelection[q] || []).includes(String(t)))
  );
  for (const [quadrant, teeth] of Object.entries(target)) {
    const values = new Set(activeToothSelection[quadrant] || []);
    for (const t of teeth) {
      if (allSelected) values.delete(String(t)); else values.add(String(t));
    }
    const arr = FULL.filter(t => values.has(t));
    if (arr.length) activeToothSelection[quadrant] = arr;
    else delete activeToothSelection[quadrant];
  }
  renderToothGrid();
}

function toggleDeciduousSelection(quadrant, letter) {
  const values = new Set(activeDeciduousSelection[quadrant] || []);
  if (values.has(letter)) values.delete(letter); else values.add(letter);
  activeDeciduousSelection[quadrant] = DECIDUOUS_LETTERS.filter(l => values.has(l));
  if (!activeDeciduousSelection[quadrant].length) delete activeDeciduousSelection[quadrant];
  renderToothGrid();
}

// 解剖面/多生牙：插入到当前条目行的内容输入框（对标官方把牙面记进条目）
function insertToothAnnotation(text) {
  if (!activeToothItemRow || !activeToothItemRow.isConnected) return;
  const input = activeToothItemRow.querySelector(".item-content");
  if (!input) return;
  input.value = input.value ? `${input.value.replace(/\s+$/, "")} ${text}` : text;
  if (typeof markMedicalEditorDirty === "function") markMedicalEditorDirty();  // #335
}

function deciduousButton(quadrant, letter) {
  const selected = (activeDeciduousSelection[quadrant] || []).includes(letter);
  const fdi = FDI_DECIDUOUS_QUADRANT[quadrant] + String(DECIDUOUS_LETTERS.indexOf(letter) + 1);   // #373
  return `
    <button type="button" class="tooth-button tooth-deciduous${selected ? " selected" : ""}"
      data-tooth="${fdi}" aria-label="${QUAD_NAME[quadrant] || quadrant}乳牙${letter} FDI ${fdi}" title="${QUAD_NAME[quadrant] || quadrant}乳${letter}"
      onclick="toggleDeciduousSelection('${quadrant}', '${letter}')">${letter}</button>
  `;
}

function toothIconButton(quadrant, tooth, upper) {
  const selected = (activeToothSelection[quadrant] || []).includes(String(tooth));
  const fdi = FDI_QUADRANT[quadrant] + tooth;   // #373:完整 FDI 码进 data-tooth/aria-label/title
  return `
    <button type="button" class="tooth-icon-btn${selected ? " selected" : ""}"
      data-tooth="${fdi}" aria-label="${QUAD_NAME[quadrant] || quadrant}${tooth}号 FDI ${fdi}" title="${QUAD_NAME[quadrant] || quadrant} ${fdi}"
      onclick="toggleToothSelection('${quadrant}', '${tooth}')">${toothIconSvg(tooth, upper)}</button>
  `;
}

function renderToothGrid() {
  // 官方排布：上弓 牙形图标行→恒牙数字行(右上8→1｜左上1→8)→乳牙字母行(E→A｜A→E)
  //          下弓 乳牙字母行→恒牙数字行(右下8→1｜左下1→8)→牙形图标行
  const upperArch = [["右上", "RT", [8, 7, 6, 5, 4, 3, 2, 1]], ["左上", "LT", [1, 2, 3, 4, 5, 6, 7, 8]]];
  const lowerArch = [["右下", "RB", [8, 7, 6, 5, 4, 3, 2, 1]], ["左下", "LB", [1, 2, 3, 4, 5, 6, 7, 8]]];
  const deciduousRow = quads => quads.map(([label, quadrant, teeth]) => `
    <div class="tooth-quadrant" data-quadrant="${quadrant}" title="${label}乳牙">
      <div class="tooth-buttons">
        ${teeth.map(t => t <= 5
          ? deciduousButton(quadrant, DECIDUOUS_LETTERS[t - 1])
          : '<span class="tooth-spacer"></span>').join("")}
      </div>
    </div>
  `).join("");
  const numberRow = quads => quads.map(([label, quadrant, teeth]) => `
    <div class="tooth-quadrant" data-quadrant="${quadrant}" title="${label}">
      <div class="tooth-buttons">
        ${teeth.map(tooth => toothButton(quadrant, tooth)).join("")}
      </div>
    </div>
  `).join("");
  const iconRow = (quads, upper) => quads.map(([label, quadrant, teeth]) => `
    <div class="tooth-quadrant" data-quadrant="${quadrant}">
      <div class="tooth-buttons">
        ${teeth.map(tooth => toothIconButton(quadrant, tooth, upper)).join("")}
      </div>
    </div>
  `).join("");

  toothGrid.innerHTML = `
    <div class="tooth-quick-row">
      ${["乳牙", "全口", "左上", "左下", "右上", "右下", "上半口", "下半口", "清除"].map(k =>
        `<button type="button" class="tooth-quick-btn" onclick="applyToothQuickPick('${k}')">${k}</button>`
      ).join("")}
    </div>
    <div class="tooth-selector-columns">
      <div class="tooth-chart-area">
        <div class="tooth-arch">${iconRow(upperArch, true)}</div>
        <div class="tooth-arch">${numberRow(upperArch)}</div>
        <div class="tooth-arch tooth-arch-deciduous">${deciduousRow(upperArch)}</div>
        <div class="tooth-arch-divider"></div>
        <div class="tooth-arch tooth-arch-deciduous">${deciduousRow(lowerArch)}</div>
        <div class="tooth-arch">${numberRow(lowerArch)}</div>
        <div class="tooth-arch">${iconRow(lowerArch, false)}</div>
      </div>
      <div class="tooth-side-panel">
        <div class="tooth-side-row"><span class="tooth-side-label">当前选择:</span>
          <span class="tooth-side-selection" data-side-selection>暂未选择牙位</span>
        </div>
        <div class="tooth-side-row"><span class="tooth-side-label">解剖面:</span>
          <div class="tooth-side-grid">
            ${["M", "D", "B", "La", "O", "I", "P", "L"].map(f =>
              `<button type="button" class="tooth-quick-btn" onclick="insertToothAnnotation('${f}面')">${f}</button>`
            ).join("")}
          </div>
        </div>
        <div class="tooth-side-row"><span class="tooth-side-label">多生牙:</span>
          <div class="tooth-side-grid">
            <button type="button" class="tooth-quick-btn" onclick="insertToothAnnotation('近中多生牙')">近中</button>
            <button type="button" class="tooth-quick-btn" onclick="insertToothAnnotation('远中多生牙')">远中</button>
          </div>
        </div>
        <div class="tooth-side-hint">解剖面/多生牙点选后写入该条目内容</div>
      </div>
    </div>
  `;
  updateToothSelectionText();
}

function updateToothSelectionText() {
  const parts = [];
  const permanent = formatToothSummary(JSON.stringify(activeToothSelection));
  if (permanent !== "未选择牙位") parts.push(permanent);
  const labels = {RT: "右上", LT: "左上", RB: "右下", LB: "左下"};
  for (const [quadrant, label] of Object.entries(labels)) {
    const letters = activeDeciduousSelection[quadrant] || [];
    if (letters.length) parts.push(`${label}乳牙 ${letters.join(" ")}`);
  }
  const summary = parts.length ? parts.join("；") : "暂未选择牙位";
  if (toothSelectionText) toothSelectionText.textContent = parts.length ? `当前选择：${summary}` : "";
  const side = document.querySelector("[data-side-selection]");
  if (side) side.textContent = summary;
}

function toothButton(quadrant, tooth) {
  const selected = (activeToothSelection[quadrant] || []).includes(String(tooth));
  const fdi = FDI_QUADRANT[quadrant] + tooth;   // #373
  return `
    <button
      type="button"
      class="tooth-button${selected ? " selected" : ""}"
      data-tooth="${fdi}" aria-label="${QUAD_NAME[quadrant] || quadrant}${tooth}号 FDI ${fdi}" title="${QUAD_NAME[quadrant] || quadrant} ${fdi}"
      onclick="toggleToothSelection('${quadrant}', '${tooth}')"
    >${tooth}</button>
  `;
}

function toggleToothSelection(quadrant, tooth) {
  const values = new Set(activeToothSelection[quadrant] || []);
  if (values.has(tooth)) {
    values.delete(tooth);
  } else {
    values.add(tooth);
  }
  activeToothSelection[quadrant] = Array.from(values).sort((a, b) => Number(a) - Number(b));
  if (!activeToothSelection[quadrant].length) delete activeToothSelection[quadrant];
  renderToothGrid();
}

function clearToothSelection() {
  activeToothSelection = {};
  activeDeciduousSelection = {};
  renderToothGrid();
}

function applyToothSelection() {
  // 通用回调模式：把 FDI 牙位码交给调用方（治疗计划行等），优先于条目/整体模式
  if (toothSelectCallback) {
    const teeth = selectionToFdi(activeToothSelection, activeDeciduousSelection);
    const cb = toothSelectCallback;
    toothSelectCallback = null;
    closeToothSelector();
    cb(teeth);
    return;
  }
  // 条目行模式：写回该行 data-teeth + 牙位格文本
  if (activeToothItemRow) {
    if (!activeToothItemRow.isConnected) { closeToothSelector(); return; }
    const teeth = selectionToFdi(activeToothSelection, activeDeciduousSelection);
    activeToothItemRow.dataset.teeth = teeth.join(",");
    const cell = activeToothItemRow.querySelector(".item-tooth-cell");
    if (cell) cell.innerHTML = toothCrossHtml(teeth);
    if (typeof markMedicalEditorDirty === "function") markMedicalEditorDirty();  // #335
    closeToothSelector();
    return;
  }
  // 整体牙位模式（旧契约）
  const container = document.querySelector(`.medical-record-editor[data-record-id="${cssEscape(activeToothRecordId)}"]`);
  // 患者切换后编辑卡已不在 DOM：静默关弹窗，不应用选择。
  if (!container) {
    closeToothSelector();
    return;
  }
  const toothJson = JSON.stringify(activeToothSelection);
  container.querySelector('[data-record-field="tooth_json"]').value = toothJson;
  const summary = document.getElementById(`toothSummary-${activeToothRecordId}`);
  if (summary) summary.textContent = formatToothSummary(toothJson);
  if (typeof markMedicalEditorDirty === "function") markMedicalEditorDirty();  // #335
  closeToothSelector();
}

function parseToothJson(raw) {
  try {
    const parsed = JSON.parse(raw || "{}");
    const out = {};
    for (const key of ["RT", "LT", "RB", "LB"]) {
      if (Array.isArray(parsed[key])) {
        out[key] = parsed[key].map(value => String(value));
      } else if (typeof parsed[key] === "string" && parsed[key].trim()) {
        out[key] = parsed[key].split("").map(value => String(value));
      }
    }
    return out;
  } catch {
    return {};
  }
}

function formatToothSummary(raw) {
  const parsed = parseToothJson(raw);
  const labels = {RT: "右上", LT: "左上", RB: "右下", LB: "左下"};
  const parts = Object.entries(labels)
    .filter(([key]) => parsed[key] && parsed[key].length)
    .map(([key, label]) => `${label} ${parsed[key].join(" ")}`);
  return parts.length ? parts.join("；") : "未选择牙位";
}

// ---------- 保存 ----------

function collectEditorItems(container) {
  const items = [];
  container.querySelectorAll(".item-subject").forEach(block => {
    const itemType = block.dataset.itemType;
    block.querySelectorAll(".item-row").forEach(row => {
      const content = row.querySelector(".item-content").value.trim();
      if (!content) return;
      const teeth = (row.dataset.teeth || "").split(",").filter(Boolean);
      if (!teeth.length) {
        items.push({tooth: "", item_type: itemType, content});
      } else {
        // 一行多牙位 → 每颗牙一条（支撑"点一颗牙看它的全部历史"）
        for (const tooth of teeth) items.push({tooth, item_type: itemType, content});
      }
    });
  });
  return items;
}

async function saveMedicalRecord(recordId, opts) {
  const keepOpen = !!(opts && opts.keepOpen);   // #335 暂存:保存但不关编辑层
  const container = document.querySelector(`.medical-record-editor[data-record-id="${cssEscape(recordId)}"]`);
  const status = document.getElementById(`recordStatus-${recordId}`);
  const rawTooth = container.querySelector('[data-record-field="tooth_json"]').value.trim();

  // 合并保存：以原 content_json 为底，编辑过的科目覆盖进去——未渲染的科目键不丢失。
  let contentJson = {};
  try {
    contentJson = JSON.parse(container.querySelector('[data-record-field="content_json"]').value || "{}") || {};
  } catch {
    contentJson = {};
  }
  container.querySelectorAll("[data-medical-field]").forEach(input => {
    let key = input.dataset.medicalField;
    const value = input.value.trim();
    // 医嘱：原数据用 DA 键的写回 DA，不改名成 Plan
    if (key === "Plan" && contentJson.Plan === undefined && contentJson.DA !== undefined) {
      key = "DA";
    }
    if (value) {
      contentJson[key] = value;
    } else {
      delete contentJson[key];
    }
  });

  let toothJson = {};
  try {
    toothJson = rawTooth ? JSON.parse(rawTooth) : {};
  } catch {
    status.textContent = "牙位JSON格式错误";
    return;
  }

  // 病历类型：radio 优先，自由输入兜底
  const typeRadio = container.querySelector(`input[name="recordType-${cssEscape(recordId)}"]:checked`);
  const typeFree = container.querySelector('[data-record-field="record_type"]').value.trim();
  const recordType = typeRadio ? typeRadio.value : typeFree;

  const payload = {
    record_type: recordType,
    doctor_name: container.querySelector('[data-record-field="doctor_name"]').value.trim(),
    visit_time: container.querySelector('[data-record-field="visit_time"]').value.trim(),
    content_json: contentJson,
    tooth_json: toothJson,
  };
  const items = collectEditorItems(container);

  status.textContent = "保存中...";
  // 新建模式（无 record_id，sentinel "new"）走 POST 新建；否则走版本化 PUT。
  const isNew = recordId === "new";
  let res;
  try {
    if (isNew) {
      // #77 快照范式:用打开编辑器时快照进 DOM 的患者ID,不读此刻的全局(编辑期间可能已切患者)。
      // 快照缺失不兜底回全局:留空让后端 400(patient_identity is required)明确报错。
      payload.patient_identity = container.dataset.patientSnapshot || "";
      res = await fetch("/api/medical-records", {
        method: "POST",
        headers: {"Content-Type": "application/json"},
        body: JSON.stringify(payload),
      });
    } else {
      res = await fetch(`/api/medical-records/${encodeURIComponent(recordId)}`, {
        method: "PUT",
        headers: {"Content-Type": "application/json"},
        body: JSON.stringify(payload),
      });
    }
  } catch {
    status.textContent = "保存失败（网络异常），请确认后重试";
    return;
  }
  if (!res.ok) {
    status.textContent = "保存失败";
    return;
  }

  // 病历主体已保存，再整组替换牙位条目
  let savedRecordId = recordId;
  let createdRow = null;
  if (isNew) {
    try {
      createdRow = (await res.json()).medical_record;
      savedRecordId = createdRow.record_id;
    } catch {
      savedRecordId = "";
    }
  }
  if (savedRecordId && savedRecordId !== "new") {
    try {
      const itemsRes = await fetch(`/api/medical-records/${encodeURIComponent(savedRecordId)}/items`, {
        method: "PUT",
        headers: {"Content-Type": "application/json"},
        body: JSON.stringify({items}),
      });
      if (!itemsRes.ok) throw new Error(String(itemsRes.status));
    } catch {
      status.textContent = "病历已保存，但牙位条目保存失败，请重新打开编辑";
      return;
    }
  }

  // 保存成功→清脏标记,后续关闭/退出不再误拦(#335)
  const overlay = document.getElementById("medicalEditorOverlay");
  if (overlay) overlay.dataset.dirty = "";
  if (typeof evictVisitsCache === "function") evictVisitsCache();  // #57 病历改动影响就诊时间轴
  if (keepOpen) {
    // 暂存:不关编辑层。新建首次暂存后,用真实 record_id 重开覆盖层,避免再次暂存重复 POST。
    status.textContent = "已暂存";
    if (typeof refreshWorkspaceDetail === "function") await refreshWorkspaceDetail();
    if (isNew && createdRow) {
      openMedicalEditorOverlay(createdRow);
    } else {
      const body = overlay ? overlay.querySelector("[data-editor-overlay-body]") : null;
      if (body && typeof loadEditableRecordVersions === "function") loadEditableRecordVersions(body);
    }
  } else if (isNew) {
    // 新建成功：关覆盖层，重取详情并重渲染整个病历 tab（回到时间线）。
    closeMedicalEditorOverlay(true);
    await refreshWorkspaceDetail();
    const panel = document.querySelector('[data-workspace-panel="medical"]');
    if (panel) renderWorkspaceMedicalTab(panel);
  } else {
    // 保存成功：刷新该卡片并回到展示态（workspace_tabs.js），同步首页审计面板。
    await refreshWorkspaceMedicalCard(recordId);
  }
  await loadAuditLogs();
}

async function saveAsTemplate(recordId) {
  // 照官方病历页「另存为模板」：把当前编辑区科目存成新模板（POST /api/medical-templates）。
  const container = document.querySelector(`.medical-record-editor[data-record-id="${cssEscape(recordId)}"]`);
  const status = document.getElementById(`recordStatus-${recordId}`);
  const name = ((await appPrompt("另存为模板，输入模板名称：")) || "").trim();
  if (!name) return;
  const contentJson = {};
  container.querySelectorAll("[data-medical-field]").forEach(input => {
    const value = input.value.trim();
    if (value) contentJson[input.dataset.medicalField] = value;
  });
  if (Object.keys(contentJson).length === 0) {
    status.textContent = "无可保存的科目";
    return;
  }
  status.textContent = "保存模板中...";
  try {
    const res = await fetch("/api/medical-templates", {
      method: "POST",
      headers: {"Content-Type": "application/json"},
      body: JSON.stringify({template_name: name, category: "我的模板", content_json: contentJson}),
    });
    status.textContent = res.ok ? "模板已保存" : "模板保存失败";
  } catch {
    status.textContent = "模板保存失败";
  }
}

Object.assign(window, {
  renderMedicalRecord,
  parseMedicalContent,
  explodeSaasContent,
  saveAsTemplate,
  medicalChartField,
  itemSubjectBlock,
  applyRecordTypeVisibility,
  addExamQuick,
  suggestExamByDiagnosis,
  recordItemRow,
  addRecordItemRow,
  scrollToSubject,
  hydrateMedicalEditor,
  loadEditorTemplateTree,
  toggleTemplateFolder,
  applyTemplateFromTree,
  loadEditorRecordItems,
  toothQuadrants,
  toothIconSvg,
  openToothSelector,
  openToothSelectorWith,
  openItemToothSelector,
  applyToothQuickPick,
  toggleDeciduousSelection,
  insertToothAnnotation,
  closeToothSelector,
  renderToothGrid,
  updateToothSelectionText,
  toothButton,
  toggleToothSelection,
  clearToothSelection,
  applyToothSelection,
  parseToothJson,
  formatToothSummary,
  collectEditorItems,
  saveMedicalRecord,
});
