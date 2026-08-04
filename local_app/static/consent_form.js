// 知情同意书 (#15c)：收费阶段 选模板→带入患者/费用/付款方式→手写签名(患者+医生)→签署入库→打印。
// 工作流：沟通完选完处置→同意书+费用一起出→确认签字→一起付。

let consentCtx = null;
let _consentSeq = 0;     // #62：每次打开的请求令牌，防快速切患者/账单时旧异步结果写错弹框
let _consentPickSeq = 0; // #72：模板选择序号，防快速点A再点B时乱序覆盖成错模板
let _consentQueue = [];  // 多类别处置待签队列(逐个自动排队签)
let _consentQueueBase = null;

// 开处置匹配出多个类别 → 逐个排队签。签完一份自动弹下一份；手动关闭则中止队列。
function startConsentQueue(patientId, billId, feeTotal, categories, items, orderId) {
  _consentQueueBase = {patientId, billId: billId || "", orderId: orderId || "",
                       feeTotal: (feeTotal != null ? feeTotal : null),
                       items: Array.isArray(items) ? items : null};
  _consentQueue = (categories || []).slice();
  advanceConsentQueue();
}
function advanceConsentQueue() {
  if (!_consentQueue.length || !_consentQueueBase) { _consentQueue = []; _consentQueueBase = null; return; }
  const cat = _consentQueue.shift();
  const b = _consentQueueBase;
  openConsentForm(b.patientId, b.billId, b.feeTotal, cat, b.items, b.orderId);
}
const _signPads = {};   // canvasId → { drawn:bool }

// #62：异步返回后确认还是同一次打开(同 patientId/billId)，否则丢弃，不写 UI/状态
function consentStale(token) {
  return !consentCtx || consentCtx.token !== token;
}
// #63：只接受 canvas 的 data:image base64 作为 <img src>，否则返回空(防注入)
function safeSign(s) {
  return (typeof s === "string" && /^data:image\/(png|jpeg);base64,[A-Za-z0-9+/=]+$/.test(s)) ? s : "";
}

// 处置名 → 同意书类别(开处置后自动匹配要签哪份)。与后端 consent_category 同口径。
const TREATMENT_CONSENT_CAT = [
  [["种植"], "种植"], [["正畸", "矫正", "托槽", "保持"], "正畸"],
  [["根管", "根尖", "隐裂", "开髓", "拔髓", "盖髓"], "根管"],
  [["拔牙", "拔除", "阻生", "残根", "残冠"], "拔牙"],
  [["充填", "补牙", "树脂", "玻璃离子"], "充填"],
  [["贴面", "嵌体", "烤瓷", "全瓷", "桩核", "义齿", "美学", "修复", "冠"], "修复"],
  [["洁牙", "洁治", "牙周", "龈下", "龈上", "刮治", "喷砂"], "牙周"],
  [["麻醉"], "麻醉"], [["放射", "CT", "CBCT", "拍片", "X线", "曲面"], "检查"],
  [["窝沟", "涂氟", "儿童", "预成冠", "间隙保持", "助萌"], "儿童"],
  [["手术", "活检", "囊肿", "翻瓣", "切除"], "手术"],
];
function treatmentConsentCategory(name) {
  const t = String(name || "");
  for (const [kws, cat] of TREATMENT_CONSENT_CAT) {
    if (kws.some(k => t.includes(k))) return cat;
  }
  return "";
}

// 处置名 → 精准匹配「唯一」同意书模板。每条:[处置关键词, 模板名判定]。
// 按顺序取首条命中的处置关键词;再用判定从 active 模板里挑出对应那一份(不再整类别全推)。
// 顺序有讲究:更专的在前(预成冠先于冠、隐裂/根尖先于根管、保持器先于正畸)。
const TREATMENT_CONSENT_RULES = [
  [["种植"], n => n.includes("种植")],
  [["隐裂"], n => n.includes("隐裂")],
  [["根尖", "囊肿"], n => n.includes("根尖") || n.includes("囊肿")],
  [["根管", "开髓", "拔髓", "盖髓"], n => n.includes("根管治疗") && !n.includes("隐裂") && !n.includes("根尖")],
  [["拔牙", "拔除", "阻生", "残根", "残冠"], n => n.includes("拔牙")],
  [["充填", "补牙", "树脂", "玻璃离子"], n => n.includes("充填修复")],
  [["嵌体"], n => n.includes("嵌体")],
  [["贴面"], n => n.includes("贴面")],
  [["预成冠"], n => n.includes("预成冠")],
  // 冠/桥族(#242)：收敛到唯一模板——全瓷处置→全瓷模板;桥/冠桥→桥模板;烤瓷/普通冠→烤瓷冠模板;
  // 库里没对应专属(如只有合并的"烤瓷冠、桥修复")→取冠桥族第一份兜底,绝不外溢到整类修复。第3元素=收敛器。
  [["烤瓷", "桥", "全瓷", "冠"], null, (n, templates) => {
    const fam = (templates || []).filter(t => /烤瓷|全瓷|冠|桥/.test(t.name) && !t.name.includes("预成"));
    if (!fam.length) return [];
    let pick;
    if (n.includes("全瓷")) pick = fam.find(t => t.name.includes("全瓷"));
    else if (n.includes("桥")) pick = fam.find(t => t.name.includes("桥"));
    else pick = fam.find(t => t.name.includes("烤瓷")) || fam.find(t => t.name.includes("冠") && !t.name.includes("全瓷"));
    return [pick || fam[0]];
  }],
  [["美学"], n => n.includes("美学修复")],
  [["全口义齿"], n => n.includes("全口义齿")],
  [["可摘局部", "局部义齿"], n => n.includes("可摘局部义齿")],
  [["活动义齿"], n => n.includes("活动义齿")],
  [["间隙保持"], n => n.includes("间隙保持")],
  [["保持器"], n => n.includes("正畸保持")],
  [["正畸", "矫正", "托槽"], n => n.includes("正畸") && !n.includes("保持")],
  [["洁牙", "洁治", "龈上", "超声", "喷砂"], n => n.includes("超声波洁牙")],
  [["牙周", "龈下", "刮治"], n => n.includes("牙周专科")],
  [["麻醉"], n => n.includes("局部麻醉")],
  [["放射", "CT", "CBCT", "拍片", "X线", "曲面", "牙片"], n => n.includes("放射检查")],
  [["助萌"], n => n.includes("助萌")],
  [["活检", "黏膜"], n => n.includes("黏膜") || n.includes("活检")],
  [["方案更改"], n => n.includes("方案更改")],
];

// 一个处置项 → 应推荐的 active 模板列表。命中精准规则取那一份;规则命中但库里没有对应模板,
// 或没命中任何规则,则回退按粗类别推(保证不漏签)。
function consentTemplatesForItem(itemName, templates) {
  const n = String(itemName || "");
  for (const [kws, match, resolve] of TREATMENT_CONSENT_RULES) {
    if (kws.some(k => n.includes(k))) {
      const hits = resolve ? resolve(n, templates) : (templates || []).filter(t => match(t.name));
      if (hits.length) return hits;
      break;   // 规则命中但无对应模板 → 跳出走类别回退
    }
  }
  const cat = treatmentConsentCategory(n);
  return cat ? (templates || []).filter(t => t.category === cat) : [];
}

async function openConsentForm(patientId, billId, feeTotal, preferCategory, items, orderId) {
  patientId = patientId || (typeof workspacePatientId !== "undefined" ? workspacePatientId : "");
  if (!patientId) return;
  const token = ++_consentSeq;
  consentCtx = {patientId, billId: billId || "", orderId: orderId || "",
                feeTotal: (feeTotal != null ? feeTotal : null),
                orderItems: Array.isArray(items) ? items : null,
                templates: [], bill: null, lastSaved: null, token};
  const m = document.getElementById("consentModal");
  if (!m) return;
  // #61：有账单则拉账单明细(账单号+处置项目/数量/金额)，带入同意书正文/快照/打印
  if (consentCtx.billId) {
    try {
      const b = await (await fetch(`/api/bills/${encodeURIComponent(consentCtx.billId)}`)).json();
      if (consentStale(token)) return;   // #62 已被新的打开取代
      if (b && b.bill_id) { consentCtx.bill = b; if (consentCtx.feeTotal == null) consentCtx.feeTotal = b.total_fee; }
    } catch { if (consentStale(token)) return; }
  }
  // 拉模板列表
  let data;
  try { data = await (await fetch("/api/consent-templates")).json(); }
  catch { data = {templates: []}; }
  if (consentStale(token)) return;   // #62
  consentCtx.templates = data.templates || [];
  const btn = document.getElementById("consentPickBtn");
  if (btn) btn.textContent = "📋 选择同意书";
  const picker = document.getElementById("consentPicker");
  if (picker) { picker.hidden = true; }
  consentCtx.currentBody = "";
  consentCtx.currentName = "";
  consentCtx.currentTemplateId = "";
  const st = document.getElementById("consentStatus");
  if (st) st.textContent = _consentQueue.length ? `本组还剩 ${_consentQueue.length} 份待签` : "";
  m.hidden = false;
  // 初始化两个签名板（弹框显示后画布尺寸才对）
  initSignPad("consentPatientSign");
  initSignPad("consentDoctorSign");
  renderConsentText();
  loadSignedConsents();   // 该患者已签同意书(可重打印)
  // preferCategory 三态：数组=本次处置匹配的类别→列「推荐签署」多选面板(不自动弹);
  // {tid,name}=逐份签队列指定的模板→直接选中; 字符串=单个类别→自动选匹配模板(兼容旧调用)。
  consentCtx.recommendCats = null;
  consentCtx.showFee = true;   // 默认带费用明细;一套多份签时由队列项指定只第一份带
  const rb = document.getElementById("consentRecommend");
  if (rb) { rb.hidden = true; rb.innerHTML = ""; }
  if (Array.isArray(preferCategory)) {
    consentCtx.recommendCats = preferCategory;
    renderConsentRecommend();
  } else if (preferCategory && typeof preferCategory === "object" && preferCategory.tid) {
    consentCtx.showFee = preferCategory.showFee !== false;   // 一套里只第一份 showFee=true
    pickConsentTemplate({dataset: {tid: preferCategory.tid, name: preferCategory.name || ""}});
  } else if (preferCategory) {
    const match = (consentCtx.templates || []).find(t => t.category === preferCategory);
    if (match) pickConsentTemplate({dataset: {tid: match.template_id, name: match.name}});
  }
}

// 「推荐签署」：列出本次处置匹配的同意书模板,多选(默认全勾),点「签署所选」逐份签。
function renderConsentRecommend() {
  const box = document.getElementById("consentRecommend");
  if (!box || !consentCtx) return;
  const tpls = consentCtx.templates || [];
  const items = consentCtx.orderItems || [];
  let recs;
  if (items.length) {
    // 精准:逐个处置项解析对应那一份,按 template_id 去重(同份不重复列)
    const m = new Map();
    items.forEach(it => consentTemplatesForItem(it.item_name, tpls).forEach(t => m.set(t.template_id, t)));
    recs = [...m.values()];
  } else {
    // 无处置项(直接开同意书):回退按传入类别推
    const cats = consentCtx.recommendCats || [];
    recs = tpls.filter(t => cats.includes(t.category));
  }
  if (!recs.length) { box.hidden = true; box.innerHTML = ""; return; }
  box.hidden = false;
  box.innerHTML =
    `<div class="cr-title">推荐签署（本次处置相关 · 可多选）</div>`
    + recs.map(t => `<label class="cr-item"><input type="checkbox" data-tid="${escapeAttr(t.template_id)}" data-name="${escapeAttr(t.name)}" checked> ${escapeHtml(t.name)}<span class="cr-cat">${escapeHtml(t.category)}</span></label>`).join("")
    + `<div class="cr-actions">`
    + `<button type="button" class="tooth-confirm-btn cr-go" onclick="startSelectedConsents()">签署所选（电子签）</button>`
    + `<button type="button" class="plain-button cr-print" onclick="printConsentSet()">🖨 打印整套（手签）</button>`
    + `</div>`;
}

// 把勾选的推荐模板排队,逐份签(签完一份自动进下一份)。
function startSelectedConsents() {
  const checks = Array.from(document.querySelectorAll('#consentRecommend input[type=checkbox]:checked'));
  const st = document.getElementById("consentStatus");
  if (!checks.length) { if (st) st.textContent = "请先勾选要签的同意书"; return; }
  const sel = checks.map(c => ({tid: c.dataset.tid, name: c.dataset.name || ""}));
  _consentQueueBase = {patientId: consentCtx.patientId, billId: consentCtx.billId || "",
                       orderId: consentCtx.orderId || "", feeTotal: consentCtx.feeTotal,
                       items: consentCtx.orderItems || null};
  _consentQueue = sel.map((s, i) => ({...s, showFee: i === 0}));   // 一套里只第一份带费用
  advanceConsentQueue();
}

// 打印整套(手签):勾选的同意书一次打印成多页(第一份带费用、每份留空签名栏),并记纸质签署档。
let _consentSetPrinting = false;
async function printConsentSet() {
  if (_consentSetPrinting) return;   // 防连点重复打印/重复落档(后端也有幂等兜底)
  if (!consentCtx) return;
  const checks = Array.from(document.querySelectorAll('#consentRecommend input[type=checkbox]:checked'));
  const st = document.getElementById("consentStatus");
  if (!checks.length) { if (st) st.textContent = "请先勾选要打印的同意书"; return; }
  const sel = checks.map(c => ({tid: c.dataset.tid, name: c.dataset.name || ""}));
  _consentSetPrinting = true;
  setTimeout(() => { _consentSetPrinting = false; }, 5000);   // 5秒后自动解锁,防任何路径忘复位致卡死
  if (st) st.textContent = "准备打印整套…";
  const docs = [];
  for (const s of sel) {
    let d = null;
    try { d = await (await fetch(`/api/consent-templates/${encodeURIComponent(s.tid)}`)).json(); } catch { d = null; }
    // 纸质打印同样自动填基础信息:filled 既上打印页、又作为 content_text 落档
    if (d && d.body) docs.push({tid: s.tid, name: d.name || s.name, body: fillConsentBasics(d.body)});
  }
  if (!docs.length) { if (st) st.textContent = "模板载入失败"; return; }
  const name = patientDisplayName();
  const date = localDateValue();
  const pay = (document.getElementById("consentPayMethod") || {}).value || "";
  const feeHtml = consentSetFeeHtml(pay);
  const pages = docs.map((d, i) => `
    <section class="cs-page">
      <h2>${escapeHtml(d.name)}</h2>
      <div class="meta">患者：${escapeHtml(name)}　日期：${escapeHtml(date)}</div>
      <pre class="body">${escapeHtml(d.body)}</pre>
      ${i === 0 ? feeHtml : ""}
      <div class="signs"><div class="sign">患者签名：＿＿＿＿＿＿＿＿</div><div class="sign">医生签名：＿＿＿＿＿＿＿＿</div></div>
    </section>`).join("");
  const w = window.open("", "_blank", "width=820,height=960");
  if (!w) { if (st) st.textContent = "打印窗口被拦截(请允许弹窗)"; return; }
  w.document.write(`<html><head><meta charset="utf-8"><title>知情同意书（整套 ${docs.length} 份）</title>
    <style>body{font-family:-apple-system,"PingFang SC",sans-serif;padding:24px;color:#222}
    .cs-page{page-break-after:always}h2{text-align:center}.meta{color:#555;margin:6px 0 12px}
    pre.body{white-space:pre-wrap;font-family:inherit;line-height:1.7;font-size:14px}
    .signs{display:flex;gap:40px;margin-top:36px}.sign{flex:1}
    .ct-fee{border:1px solid #ccc;padding:8px 10px;margin-top:14px;font-size:13px}
    .ct-fee-table{width:100%;border-collapse:collapse;margin-top:6px}
    .ct-fee-table td,.ct-fee-table th{border-bottom:1px solid #eee;padding:3px 4px}
    </style></head><body>${pages}</body></html>`);
  w.document.close(); w.focus(); w.print();
  // 去重:该 order 下已记录(未作废)的同一模板 → 重打印只打印,不再重复记(避免累积)
  // 键优先用 template_id(唯一),同名不同模板不会误判为同一份;无模板回退按模板名。
  const tplKey = (tid, nm) => (tid ? "t:" + tid : "n:" + (nm || ""));
  let existing = [];
  try { existing = (await (await fetch(`/api/patients/${encodeURIComponent(consentCtx.patientId)}/consent-documents`)).json()).documents || []; } catch { existing = []; }
  const recorded = new Set(existing.filter(x => x.status !== "voided" && (x.order_id || "") === (consentCtx.orderId || "")).map(x => tplKey(x.template_id, x.template_name)));
  // 记纸质签署档:仅记尚未记录过的(费用只记本套第一份)
  let newCount = 0;
  for (let i = 0; i < docs.length; i++) {
    const d = docs[i];
    if (recorded.has(tplKey(d.tid, d.name))) continue;   // 已有记录,跳过(只重打印)
    const cj = {patient: name, date, pay_method: pay};
    if (i === 0) {
      cj.fee_total = consentCtx.feeTotal;
      cj.bill_no = consentCtx.bill ? (consentCtx.bill.bill_no || "") : "";
      cj.items = (consentCtx.bill ? (consentCtx.bill.items || []) : (consentCtx.orderItems || [])).map(it => ({item_name: it.item_name, quantity: it.quantity, line_fee: it.line_fee}));
    }
    try {
      const res = await fetch(`/api/patients/${encodeURIComponent(consentCtx.patientId)}/consent-documents`, {
        method: "POST", headers: {"Content-Type": "application/json"},
        body: JSON.stringify({template_id: d.tid, template_name: d.name, bill_id: consentCtx.billId || "",
          order_id: consentCtx.orderId || "", content_text: d.body, content_json: cj, sign_method: "paper"})});
      if (res.ok) { const j = await res.json().catch(() => ({})); if (!j.reused) newCount += 1; }   // 后端去重返回reused则不计新增
    } catch { /* 单份记录失败不阻断 */ }
  }
  if (st) st.textContent = newCount ? `已打印整套 ${docs.length} 份，新记 ${newCount} 份纸质签署` : `已重打印整套 ${docs.length} 份（已有记录，未重复记）`;
  loadSignedConsents();
  if (typeof loadAuditLogs === "function") loadAuditLogs();
}

// 整套打印用费用块
function consentSetFeeHtml(pay) {
  const b = consentCtx && consentCtx.bill;
  const items = b ? (b.items || []) : (consentCtx && consentCtx.orderItems) || [];
  const billNo = b ? (b.bill_no || "—") : "（待收费）";
  const total = b ? b.total_fee : items.reduce((s, it) => s + (Number(it.line_fee) || 0), 0);
  if (!items.length) return `<div class="ct-fee">付款方式：${escapeHtml(pay)}</div>`;
  const rows = items.map(it => `<tr><td>${escapeHtml(it.item_name || "")}</td><td>×${escapeHtml(it.quantity)}</td><td style="text-align:right">${formatMoney(it.line_fee)}</td></tr>`).join("");
  return `<div class="ct-fee"><div>账单编号：${escapeHtml(billNo)}　付款方式：${escapeHtml(pay)}</div>
    <table class="ct-fee-table"><thead><tr><th>本次诊疗项目</th><th>数量</th><th style="text-align:right">金额</th></tr></thead><tbody>${rows}</tbody></table>
    <div style="text-align:right;margin-top:4px">合计应收：¥${formatMoney(total)}</div></div>`;
}

// 该患者已签同意书列表（可重打印/复看）
async function loadSignedConsents() {
  const box = document.getElementById("consentSigned");
  if (!box || !consentCtx) return;
  const token = consentCtx.token;
  let docs = [];
  try { docs = (await (await fetch(`/api/patients/${encodeURIComponent(consentCtx.patientId)}/consent-documents`)).json()).documents || []; }
  catch { if (!consentStale(token)) box.innerHTML = ""; return; }
  if (consentStale(token)) return;   // #62 切了患者，丢弃旧结果
  if (!docs.length) { box.innerHTML = ""; return; }
  box.innerHTML = `<div class="cs-signed-head">已签同意书 ${docs.length} 份</div>` +
    docs.map(d => {
      const voided = d.status === "voided";
      const paper = d.sign_method === "paper";
      return `<div class="cs-signed-row${voided ? ' cs-voided' : ''}">
      <span class="cs-signed-name">${escapeHtml(d.template_name || "—")}
        <span class="cs-method-tag ${paper ? 'cs-mt-paper' : 'cs-mt-elec'}">${paper ? "纸质签" : "电子签"}</span>${voided ? ' <span class="cs-void-tag">已作废</span>' : ''}</span>
      <span class="cs-signed-time">${escapeHtml(d.signed_at || "")}</span>
      ${d.has_tsa ? '<span class="cs-tsa">已存证</span>' : ''}
      <span class="cs-signed-ops">
        <button type="button" class="plain-button" onclick="viewConsentDocument('${escapeAttr(d.document_id)}')">查看</button>
        <button type="button" class="plain-button" onclick="reprintConsent('${escapeAttr(d.document_id)}')">重打印</button>
        ${voided ? '' : `<button type="button" class="plain-button cs-void-btn" onclick="voidConsent('${escapeAttr(d.document_id)}')">作废</button>`}
      </span>
    </div>`;
    }).join("");
}

// 页内预览一份已签同意书:正文快照+双方签名图(纸质签提示签名在纸面)+哈希校验,不走打印
async function viewConsentDocument(documentId) {
  let d;
  try { d = await (await fetch(`/api/consent-documents/${encodeURIComponent(documentId)}`)).json(); }
  catch { window.alert("载入失败（网络异常）"); return; }
  if (!d || !d.document_id) { window.alert("签署件不存在"); return; }
  let m = document.getElementById("consentViewModal");
  if (!m) { m = document.createElement("div"); m.id = "consentViewModal"; m.className = "modal-backdrop"; document.body.appendChild(m); }
  const paper = (d.content_json && d.content_json.sign_method) === "paper";
  const warn = d.hash_valid ? "" : `<div class="cv-warn">⚠ 内容哈希校验不一致，该签署件疑似被篡改</div>`;
  const signBox = (label, img) => `
    <div class="cv-sign"><div class="cv-sign-label">${label}</div>
      ${paper ? '<div class="cv-sign-paper">纸质签署，签名在打印件上</div>'
        : (safeSign(img) ? `<img src="${safeSign(img)}" alt="${label}">` : '<div class="cv-sign-paper">未签</div>')}
    </div>`;
  m.innerHTML = `
    <section class="appt-modal cv-modal" role="dialog" aria-modal="true" aria-label="签署件预览">
      <div class="modal-head"><strong>${escapeHtml(d.template_name || "知情同意书")}
        <span class="cs-method-tag ${paper ? 'cs-mt-paper' : 'cs-mt-elec'}">${paper ? "纸质签" : "电子签"}</span></strong>
        <button type="button" class="plain-button" onclick="closeConsentView()">×</button></div>
      <div class="appt-body">
        ${warn}
        <pre class="cv-content">${escapeHtml(d.content_text || "")}</pre>
        <div class="cv-signs">${signBox("患者签名", d.patient_sign)}${signBox("医生签名", d.doctor_sign)}</div>
        <div class="cv-meta">签署时间 ${escapeHtml(d.signed_at || "")} · 内容指纹 ${escapeHtml(String(d.content_hash || "").slice(0, 16))}…${d.hash_valid ? " ✓未被改动" : ""}</div>
      </div>
      <div class="modal-actions">
        <button type="button" class="plain-button" onclick="reprintConsent('${escapeAttr(d.document_id)}')">🖨 重打印</button>
        <button type="button" class="plain-button" onclick="closeConsentView()">关闭</button>
      </div>
    </section>`;
  m.hidden = false;
}
function closeConsentView() { const m = document.getElementById("consentViewModal"); if (m) m.hidden = true; }

// 重打印/复看一份已签同意书(用存档的正文+签名图)，并提示哈希校验结果
async function reprintConsent(documentId) {
  let d;
  try { d = await (await fetch(`/api/consent-documents/${encodeURIComponent(documentId)}`)).json(); }
  catch { return; }
  if (!d || !d.document_id) return;
  const w = window.open("", "_blank", "width=800,height=900");
  if (!w) return;
  const warn = d.hash_valid ? "" : `<div style="color:#c0392b;font-weight:bold">⚠ 内容哈希校验不一致，该签署件疑似被篡改</div>`;
  w.document.write(`<html><head><meta charset="utf-8"><title>${escapeHtml(d.template_name || "知情同意书")}</title>
    <style>body{font-family:"宋体",serif;padding:24px;line-height:1.7;font-size:14px}pre{white-space:pre-wrap;font-family:inherit}
    .signs{display:flex;gap:40px;margin-top:30px}.sign{flex:1}.sign img{border-bottom:1px solid #333;max-width:280px;height:80px}
    .hash{color:#888;font-size:11px;margin-top:20px}</style></head><body>
    ${warn}
    <pre>${escapeHtml(d.content_text || "")}</pre>
    <div class="signs">
      <div class="sign">患者签名：<br>${safeSign(d.patient_sign) ? `<img src="${safeSign(d.patient_sign)}">` : "________"}</div>
      <div class="sign">医生签名：<br>${safeSign(d.doctor_sign) ? `<img src="${safeSign(d.doctor_sign)}">` : "________"}</div>
    </div>
    <div class="hash">内容哈希：${escapeHtml(d.content_hash || "")}　签署时间：${escapeHtml(d.signed_at || "")}${d.tsa_token ? "　可信时间戳已存证" : ""}</div>
    </body></html>`);
  w.document.close(); w.focus();
  setTimeout(() => { try { w.print(); } catch (e) { /* ignore */ } }, 300);
}
// 作废一份签署件(签了不能改，改只能作废重签，留痕)。作废后重新选模板再签即可。
async function voidConsent(documentId) {
  // 简化:不强填理由,一次确认即作废,系统留痕(审计)即可
  if (!window.confirm("确认作废这份同意书？")) return;
  try {
    const r = await fetch(`/api/consent-documents/${encodeURIComponent(documentId)}/void`, {
      method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({}),
    });
    if (!r.ok) { const e = await r.json().catch(() => ({})); alert(e.detail || "作废失败"); return; }
    await loadSignedConsents();                 // 刷新列表，显示已作废
  } catch { alert("作废失败"); }
}

function closeConsentForm() {
  const m = document.getElementById("consentModal");
  if (m) m.hidden = true;
  consentCtx = null;
  _consentQueue = []; _consentQueueBase = null;   // 手动关闭 → 中止排队
}

// 处置单式选择器：点「选择同意书」→ 弹出按类别分组的列表，点一条选中
function openConsentPicker() {
  const picker = document.getElementById("consentPicker");
  if (!picker || !consentCtx) return;
  if (!picker.hidden) { picker.hidden = true; return; }   // 再点收起
  renderConsentPickerList();
  picker.hidden = false;
}

function renderConsentPickerList() {
  const picker = document.getElementById("consentPicker");
  if (!picker || !consentCtx) return;
  const tpls = consentCtx.templates || [];
  // 模板维护就在本弹窗:随时可新建,不用去别处找配置页
  const newBtn = '<div class="cp-group"><button type="button" class="cp-item cp-new" onclick="showConsentTemplateForm()">＋ 新建模板</button></div>';
  if (!tpls.length) {   // 扫荡#395:无模板时给空态,别弹个空选择器让人以为坏了
    picker.innerHTML = '<div class="cp-empty">暂无同意书模板，点下面「＋ 新建模板」现场创建一份</div>' + newBtn;
    return;
  }
  const cats = {};
  tpls.forEach(t => { (cats[t.category] = cats[t.category] || []).push(t); });
  picker.innerHTML = Object.keys(cats).map(cat => `
    <div class="cp-group"><div class="cp-cat">${escapeHtml(cat)}</div>
    ${cats[cat].map(t => `<button type="button" class="cp-item${t.template_id === consentCtx.currentTemplateId ? " cp-on" : ""}" data-tid="${escapeAttr(t.template_id)}" data-name="${escapeAttr(t.name)}" onclick="pickConsentTemplate(this)">${escapeHtml(t.name)}</button>`).join("")}
    </div>`).join("") + newBtn;
}

// 弹窗内直接新建模板:名称/类别/正文 → 保存即入库并自动选中开签
function showConsentTemplateForm() {
  const picker = document.getElementById("consentPicker");
  if (!picker || !consentCtx) return;
  const cats = [...new Set((consentCtx.templates || []).map(t => t.category).filter(Boolean))];
  picker.innerHTML = `
    <div class="cp-newform">
      <input id="ctNewName" class="ord-input" placeholder="模板名称（如 拔牙知情同意书）">
      <input id="ctNewCat" class="ord-input" list="ctCatList" placeholder="类别（如 外科 / 修复 / 种植）">
      <datalist id="ctCatList">${cats.map(c => `<option value="${escapeAttr(c)}"></option>`).join("")}</datalist>
      <textarea id="ctNewBody" class="ord-input" rows="10" placeholder="同意书正文。签署时会自动带上患者信息、日期和双方签名区"></textarea>
      <div class="cp-newform-actions">
        <button type="button" class="tooth-confirm-btn" onclick="submitConsentTemplate()">保存模板</button>
        <button type="button" class="plain-button" onclick="renderConsentPickerList()">返回</button>
        <span id="ctNewStatus" class="record-save-status"></span>
      </div>
    </div>`;
}

async function submitConsentTemplate() {
  const val = id => ((document.getElementById(id) || {}).value || "").trim();
  const st = document.getElementById("ctNewStatus");
  const payload = {name: val("ctNewName"), category: val("ctNewCat"), body: val("ctNewBody")};
  if (!payload.name || !payload.category || !payload.body) {
    if (st) st.textContent = "名称、类别、正文都要填";
    return;
  }
  if (st) st.textContent = "保存中...";
  let res;
  try {
    res = await fetch("/api/consent-templates", {method: "POST",
      headers: {"Content-Type": "application/json"}, body: JSON.stringify(payload)});
  } catch { if (st) st.textContent = "保存失败（网络异常）"; return; }
  if (!res.ok) { const m = await res.json().catch(() => ({})); if (st) st.textContent = "保存失败：" + (m.detail || res.status); return; }
  const t = await res.json();
  consentCtx.templates = (consentCtx.templates || []).concat([
    {template_id: t.template_id, name: t.name, category: t.category},
  ]);
  pickConsentTemplate({dataset: {tid: t.template_id, name: t.name}});   // 存完直接选中开签
}

// #71：数据走 data-*，不把名字/ID 拼进内联JS(防引号断字/注入)。#72：选择序号防乱序覆盖。
async function pickConsentTemplate(el) {
  if (!consentCtx || !el) return;
  const tid = el.dataset.tid || "";
  const name = el.dataset.name || "";
  const token = consentCtx.token;
  const pickSeq = ++_consentPickSeq;
  const picker = document.getElementById("consentPicker");
  if (picker) picker.hidden = true;
  const btn = document.getElementById("consentPickBtn");
  if (btn) btn.textContent = "📋 " + (name || "选择同意书");
  let d;
  try { d = await (await fetch(`/api/consent-templates/${encodeURIComponent(tid)}`)).json(); }
  catch { return; }
  if (consentStale(token) || pickSeq !== _consentPickSeq) return;   // #62切患者 / #72非最后一次选择 → 丢弃
  consentCtx.currentBody = d.body || "";
  consentCtx.currentName = d.name || "";
  consentCtx.currentTemplateId = d.template_id || "";
  renderConsentText();
}

// #61：本次费用明细(账单号 + 处置项目/数量/金额 + 合计)。bill 为账单详情对象。
// 分期收费空白栏：打印出来留空，前台手填(本次收/余款/下次收款日期)
const CONSENT_INSTALLMENT = '<div class="ct-installment">分期收费（如需·手填）：本次实收 ＿＿＿＿＿　余款 ＿＿＿＿＿　下次收款日期 ＿＿＿＿＿　金额 ＿＿＿＿＿</div>';

function consentFeeHtml() {
  // 一套多份时费用只贴第一份(showFee=false 的份不带费用明细)
  if (consentCtx && consentCtx.showFee === false) return "";
  const b = consentCtx && consentCtx.bill;
  const pay = (document.getElementById("consentPayMethod") || {}).value || "";
  // 处置单+同意书合体：优先用账单明细；没账单时用处置单本身的项目(本次哪些项目/各多少钱)
  const items = b ? (b.items || []) : (consentCtx && consentCtx.orderItems) || [];
  const billNo = b ? (b.bill_no || "—") : "（待收费）";
  const total = b ? b.total_fee : (items.reduce((s, it) => s + (Number(it.line_fee) || 0), 0));
  if (!items.length) {
    const fee = consentCtx && consentCtx.feeTotal != null ? `本次费用合计：¥${formatMoney(consentCtx.feeTotal)}　付款方式：${pay}` : `付款方式：${pay}`;
    return `<div class="ct-fee">${escapeHtml(fee)}${CONSENT_INSTALLMENT}</div>`;
  }
  const rows = items.map(it =>
    `<tr><td>${escapeHtml(it.item_name || "")}</td><td>×${escapeHtml(it.quantity)}</td><td style="text-align:right">${formatMoney(it.line_fee)}</td></tr>`
  ).join("");
  return `<div class="ct-fee">
    <div>账单编号：${escapeHtml(billNo)}　付款方式：${escapeHtml(pay)}</div>
    <table class="ct-fee-table"><thead><tr><th>本次诊疗项目</th><th>数量</th><th style="text-align:right">金额</th></tr></thead>
    <tbody>${rows}</tbody></table>
    <div class="ct-fee-total">合计应收：¥${formatMoney(total)}</div>
    ${CONSENT_INSTALLMENT}
  </div>`;
}

// 正文里把空方框 □/☐ 变成可点选框：点一下 □↔☑，勾选状态进签署快照(content_text)
function consentBodyHtml(body) {
  return escapeHtml(body).replace(/[□☐]/g,
    '<span class="ct-box" onclick="toggleConsentBox(this)">□</span>');
}
function toggleConsentBox(el) {
  el.textContent = (el.textContent === "□") ? "☑" : "□";
  el.classList.toggle("ct-box-on");
}

// ---------- 基础信息自动填充：用系统患者数据填正文里的空格(姓名/性别/年龄/电话/病历号/日期) ----------
// 从工作区患者对象取基础信息(性别列名 sex;病历号空则回退 source_json.patientid;年龄按生日算)。
function consentBasicInfo() {
  let p = {};
  try { p = (typeof workspaceData !== "undefined" && workspaceData && workspaceData.patient) || {}; } catch { p = {}; }
  let sj = {};
  try { sj = typeof p.source_json === "string" ? JSON.parse(p.source_json) : (p.source_json || {}); } catch { sj = {}; }
  return {
    name: p.display_name || "",
    sex: (p.sex === "男" || p.sex === "女") ? p.sex : "",   // 未填/没填/未知 当空
    age: (typeof _ageFromBirthday === "function") ? _ageFromBirthday(p.birthday) : "",
    phone: p.phone || "",
    chart: p.chart_no || (sj && sj.patientid) || "",
    birthday: p.birthday || "",
    date: (typeof localDateValue === "function") ? localDateValue() : "",
  };
}
// 填"标签：空白"——仅当后面确有空白、或紧跟换行/结尾时才填,避免插到已有文字前面。
function _fillConsentLabel(body, labels, value) {
  if (!value) return body;
  const re = new RegExp("((?:" + labels + ")\\s*[：:])([ \\t_＿]*)", "g");
  return body.replace(re, (m, p1, p2, off, str) => {
    const nextEmpty = (off + m.length >= str.length) || str[off + m.length] === "\n";
    return (p2.length > 0 || nextEmpty) ? p1 + value + (p2.length ? "　" : "") : m;
  });
}
// 姓名单独处理：裸"姓名"要排除监护人/家长/代理人等(那不是患者本人)。
function _fillConsentName(body, value) {
  if (!value) return body;
  const re = /(患者姓名|患儿姓名|受检者|姓\s*名)(\s*[：:])([ \t_＿]*)/g;
  return body.replace(re, (m, label, colon, blanks, off, str) => {
    if (/^姓/.test(label)) {
      const pre = str.slice(Math.max(0, off - 3), off);
      if (/监护人|家长|委托人|代理人|亲属|本人/.test(pre)) return m;
    }
    const nextEmpty = (off + m.length >= str.length) || str[off + m.length] === "\n";
    return (blanks.length > 0 || nextEmpty) ? label + colon + value + (blanks.length ? "　" : "") : m;
  });
}
// 性别：有方框就勾对应的(☑),没框就填字;不动另一个框。
function _fillConsentSex(body, sex) {
  if (sex !== "男" && sex !== "女") return body;
  body = body.replace(/性\s*别\s*[：:]\s*男\s*[□☐]\s*女\s*[□☐]/g, sex === "男" ? "性别：男☑ 女□" : "性别：男□ 女☑");
  body = body.replace(/性\s*别\s*[：:]\s*[□☐]\s*男\s*[□☐]\s*女/g, sex === "男" ? "性别：☑男 □女" : "性别：□男 ☑女");
  body = body.replace(/(性\s*别\s*[：:])([ \t_＿]+)/g, (m, p1) => p1 + sex + "　");
  return body;
}
// 日期：先填"年__月__日"槽位,再填裸"日期：___"。
function _fillConsentDate(body, date) {
  const m = String(date || "").match(/(\d{4})-(\d{2})-(\d{2})/);
  if (!m) return body;
  body = body.replace(/((?:就诊日期|签名日期|日期)\s*[：:])([ _＿]*)年([ _＿]*)月([ _＿]*)日/g,
    (mm, p1) => p1 + m[1] + " 年 " + m[2] + " 月 " + m[3] + " 日");
  return _fillConsentLabel(body, "就诊日期|签名日期|日期", date);
}
// 入口：把一份模板正文里的基础信息空格用系统患者数据填好。
function fillConsentBasics(body) {
  if (!body) return body;
  const p = consentBasicInfo();
  body = _fillConsentName(body, p.name);
  body = _fillConsentSex(body, p.sex);
  body = _fillConsentLabel(body, "出生日期|出生年月", p.birthday);
  body = _fillConsentLabel(body, "年\\s*龄", p.age);
  body = _fillConsentLabel(body, "联系电话|联系方式|联系方法|电\\s*话", p.phone);
  body = _fillConsentLabel(body, "病历号|病例编号|病历编号|门诊号", p.chart);
  body = _fillConsentDate(body, p.date);
  return body;
}

// 组合展示正文：抬头(患者+日期) + 模板正文(可勾选) + 本次费用明细+付款方式
function renderConsentText() {
  const box = document.getElementById("consentText");
  if (!box || !consentCtx) return;
  if (!consentCtx.currentBody) { box.textContent = "请选择同意书模板"; return; }
  const name = patientDisplayName();
  const date = localDateValue();
  box.innerHTML =
    `<div class="ct-title">${escapeHtml(consentCtx.currentName)}</div>` +
    `<div class="ct-meta">患者：${escapeHtml(name)}　日期：${escapeHtml(date)}</div>` +
    `<pre class="ct-body">${consentBodyHtml(fillConsentBasics(consentCtx.currentBody))}</pre>` +
    consentFeeHtml();
}

// 换付款方式只更新费用区，不重渲正文(否则已勾的方框会丢)
function updateConsentFee() {
  const box = document.getElementById("consentText");
  const fee = box && box.querySelector(".ct-fee");
  if (fee) fee.outerHTML = consentFeeHtml();
}

function patientDisplayName() {
  try { return (workspaceData && workspaceData.patient && workspaceData.patient.display_name) || ""; }
  catch { return ""; }
}

// ---------- 手写签名板 ----------
function initSignPad(canvasId) {
  const cv = document.getElementById(canvasId);
  if (!cv) return;
  const ctx = cv.getContext("2d");
  ctx.clearRect(0, 0, cv.width, cv.height);
  ctx.lineWidth = 2; ctx.lineCap = "round"; ctx.strokeStyle = "#1a1a1a";
  _signPads[canvasId] = {drawn: false};
  let drawing = false;
  const pos = (e) => {
    const r = cv.getBoundingClientRect();
    const p = (e.touches && e.touches[0]) ? e.touches[0] : e;
    return {x: (p.clientX - r.left) * (cv.width / r.width), y: (p.clientY - r.top) * (cv.height / r.height)};
  };
  const start = (e) => { e.preventDefault(); drawing = true; const {x, y} = pos(e); ctx.beginPath(); ctx.moveTo(x, y); };
  const move = (e) => { if (!drawing) return; e.preventDefault(); const {x, y} = pos(e); ctx.lineTo(x, y); ctx.stroke(); _signPads[canvasId].drawn = true; };
  const end = () => { drawing = false; };
  // 重新绑定前先清旧监听：简单做法是用 onX 赋值（覆盖）
  cv.onmousedown = start; cv.onmousemove = move; cv.onmouseup = end; cv.onmouseleave = end;
  cv.ontouchstart = start; cv.ontouchmove = move; cv.ontouchend = end;
}
function clearSignPad(canvasId) {
  const cv = document.getElementById(canvasId);
  if (!cv) return;
  cv.getContext("2d").clearRect(0, 0, cv.width, cv.height);
  if (_signPads[canvasId]) _signPads[canvasId].drawn = false;
}
function signDataUrl(canvasId) {
  const cv = document.getElementById(canvasId);
  if (!cv || !_signPads[canvasId] || !_signPads[canvasId].drawn) return "";
  return cv.toDataURL("image/png");
}

let _consentSubmitting = false;
async function submitConsent() {
  if (_consentSubmitting) return;   // 防连点把同一份重复签存多份
  if (!consentCtx) return;
  const st = document.getElementById("consentStatus");
  if (!consentCtx.currentBody) { if (st) st.textContent = "请先选同意书模板"; return; }
  const patientSign = signDataUrl("consentPatientSign");
  if (!patientSign) { if (st) st.textContent = "请让患者手写签名"; return; }
  const doctorSign = signDataUrl("consentDoctorSign");
  if (!doctorSign) { if (st) st.textContent = "请医生手写签名（医疗同意书需医患双方签名）"; return; }
  // #64：快照本次打开的 token/patientId，提交跨 await，返回后校验仍是同一次打开再落状态
  const token = consentCtx.token;
  const patientId = consentCtx.patientId;
  const box = document.getElementById("consentText");
  const contentText = box ? box.innerText : consentCtx.currentBody;
  const pay = (document.getElementById("consentPayMethod") || {}).value || "";
  const payload = {
    template_id: consentCtx.currentTemplateId, template_name: consentCtx.currentName,
    bill_id: consentCtx.billId, order_id: consentCtx.orderId, content_text: contentText,
    content_json: {patient: patientDisplayName(), fee_total: consentCtx.feeTotal, pay_method: pay,
                   date: localDateValue(),
                   bill_no: consentCtx.bill ? (consentCtx.bill.bill_no || "") : "",
                   items: (consentCtx.bill ? (consentCtx.bill.items || []) : (consentCtx.orderItems || [])).map(i => ({
                     item_name: i.item_name, quantity: i.quantity, line_fee: i.line_fee}))},
    patient_sign: patientSign, doctor_sign: doctorSign,
  };
  if (st) st.textContent = "签署中...";
  _consentSubmitting = true;
  try {
    let res;
    try {
      res = await fetch(`/api/patients/${encodeURIComponent(patientId)}/consent-documents`, {
        method: "POST", headers: {"Content-Type": "application/json"}, body: JSON.stringify(payload)});
    } catch { if (!consentStale(token)) { const s2 = document.getElementById("consentStatus"); if (s2) s2.textContent = "签署失败（网络异常）"; } return; }
    if (consentStale(token)) return;   // #64 已切到别的患者/账单，丢弃这次结果，不写当前弹框
    if (!res.ok) { const m = await res.json().catch(() => ({})); if (consentStale(token)) return; if (st) st.textContent = "签署失败：" + (m.detail || res.status); return; }   // #65 解析错误body又一异步点，写状态前再校验
    const body = await res.json();
    if (consentStale(token)) return;   // #64 二次校验(await json 又一次异步)
    consentCtx.lastSaved = {...payload, content_hash: body.content_hash, signed_at: body.signed_at};
    if (st) st.textContent = "已签署保存（哈希 " + String(body.content_hash || "").slice(0, 12) + "…）";
    // 签完即清两块签名板：避免再点「签署并保存」把同一份重复存一份
    clearSignPad("consentPatientSign");
    clearSignPad("consentDoctorSign");
    loadSignedConsents();   // 刷新已签列表，刚签的出现在列表里
    if (typeof loadAuditLogs === "function") loadAuditLogs();
    if (_consentQueue.length) {
      // 排队中：签完这份自动弹下一份
      if (st) st.textContent += `　→ 还有 ${_consentQueue.length} 份，正在打开下一份…`;
      setTimeout(advanceConsentQueue, 800);
    } else {
      // 单份/整组签完 → 自动关闭弹框(不再停在已签界面被重复点)
      setTimeout(() => { if (!consentStale(token)) closeConsentForm(); }, 800);
    }
  } finally {
    _consentSubmitting = false;
  }
}

function printConsent() {
  if (!consentCtx || !consentCtx.currentBody) return;
  const name = patientDisplayName();
  const date = localDateValue();
  // 打印带上已勾选的方框(从渲染好的正文取，☑/□ 都在)
  const bodyEl = document.querySelector("#consentText .ct-body");
  const bodyText = bodyEl ? bodyEl.innerText : consentCtx.currentBody;
  const pSign = signDataUrl("consentPatientSign");
  const dSign = signDataUrl("consentDoctorSign");
  const w = window.open("", "_blank", "width=800,height=900");
  if (!w) return;
  w.document.write(`<html><head><meta charset="utf-8"><title>${escapeHtml(consentCtx.currentName)}</title>
    <style>body{font-family:"宋体",serif;padding:24px;line-height:1.7;font-size:14px}h2{text-align:center}
    pre{white-space:pre-wrap;font-family:inherit}.ct-fee{margin:12px 0}
    .ct-fee table{border-collapse:collapse;width:100%;margin:6px 0}.ct-fee th,.ct-fee td{border:1px solid #999;padding:3px 8px;font-size:13px}
    .ct-fee-total{font-weight:bold;text-align:right;margin-top:4px}.ct-installment{margin-top:10px;font-size:13px}
    .signs{display:flex;gap:40px;margin-top:30px}.sign{flex:1}.sign img{border-bottom:1px solid #333;max-width:280px;height:80px}</style></head><body>
    <h2>${escapeHtml(consentCtx.currentName)}</h2>
    <div>患者：${escapeHtml(name)}　日期：${escapeHtml(date)}</div>
    <pre>${escapeHtml(bodyText)}</pre>
    ${consentFeeHtml()}
    <div class="signs">
      <div class="sign">患者签名：<br>${safeSign(pSign) ? `<img src="${safeSign(pSign)}">` : "________________"}</div>
      <div class="sign">医生签名：<br>${safeSign(dSign) ? `<img src="${safeSign(dSign)}">` : "________________"}</div>
    </div></body></html>`);
  w.document.close();
  w.focus();
  setTimeout(() => { try { w.print(); } catch (e) { /* ignore */ } }, 300);
}

Object.assign(window, {
  openConsentForm, closeConsentForm, openConsentPicker, pickConsentTemplate,
  renderConsentText, updateConsentFee, toggleConsentBox,
  startConsentQueue, advanceConsentQueue, treatmentConsentCategory,
  startSelectedConsents, renderConsentRecommend, printConsentSet,
  clearSignPad, submitConsent, printConsent, loadSignedConsents, reprintConsent,
});
