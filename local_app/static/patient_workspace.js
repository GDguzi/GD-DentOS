// 患者工作区：左栏档案 + 多 tab 面板（懒加载）。
// 路由（hash 解析、openPatientWorkspace）在 app.js，本文件只负责渲染。

// 过敏 / VIP 候选键：键名清单待用户提供真实 source_json 键后更新，只改这些常量即可。
const ALLERGY_KEYS = ["过敏史", "药物过敏", "Allergy", "allergy_history"];
const VIP_KEYS = ["VIP", "VIP等级", "会员等级", "vip_level"];

// source_json 只读分组映射：键名 → 分组 + 中文标签。
// 分组：medical=医疗警示（红色置顶）/ identity=身份信息 / contact=联系方式 /
//       clinic=就诊信息 / finance=费用信息 / marketing=营销信息，
// 未匹配的键进"其他字段"兜底组，键名直接做标签。
// 同时收录中文键（旧/手填）和原系统 SaaS 的英文键（source_json 实际键名）。
const SOURCE_FIELD_GROUPS = {
  // —— 医疗警示 ——
  "过敏史": {group: "medical", label: "过敏史"},
  "药物过敏": {group: "medical", label: "药物过敏"},
  "Allergy": {group: "medical", label: "过敏史"},
  "allergy_history": {group: "medical", label: "过敏史"},
  "既往史": {group: "medical", label: "既往史"},
  "病史": {group: "medical", label: "既往史"},
  "medical_history": {group: "medical", label: "既往史"},
  // —— 身份信息 ——
  "姓名": {group: "identity", label: "姓名"},
  "patientname": {group: "identity", label: "姓名"},
  "性别": {group: "identity", label: "性别"},
  "sex": {group: "identity", label: "性别"},
  "生日": {group: "identity", label: "生日"},
  "birthday": {group: "identity", label: "生日"},
  "出生日期": {group: "identity", label: "出生日期"},
  "年龄": {group: "identity", label: "年龄"},
  "age": {group: "identity", label: "年龄"},
  "身份证号": {group: "identity", label: "身份证号"},
  "病历号": {group: "identity", label: "病历号"},
  "患者编号": {group: "identity", label: "患者编号"},
  "patientid": {group: "identity", label: "门诊号"},
  "groupname": {group: "identity", label: "患者分组"},
  "pattype": {group: "identity", label: "患者类型"},
  "isvip": {group: "identity", label: "是否VIP"},
  "patientstar": {group: "identity", label: "患者星级"},
  // —— 联系方式 ——
  "电话": {group: "contact", label: "电话"},
  "phone": {group: "contact", label: "电话"},
  "ph": {group: "contact", label: "电话"},
  "手机": {group: "contact", label: "手机"},
  "手机号": {group: "contact", label: "手机号"},
  "phonevestee2": {group: "contact", label: "其他联系电话"},
  "地址": {group: "contact", label: "地址"},
  "address": {group: "contact", label: "地址"},
  "住址": {group: "contact", label: "住址"},
  "微信": {group: "contact", label: "微信"},
  "邮箱": {group: "contact", label: "邮箱"},
  // —— 就诊信息 ——
  "firstdate": {group: "clinic", label: "建档日期"},
  "firstdoct": {group: "clinic", label: "首诊医生"},
  "createtime": {group: "clinic", label: "创建时间"},
  "latelydate": {group: "clinic", label: "最近就诊"},
  "latelydoct": {group: "clinic", label: "最近就诊医生"},
  "nextdoctorname": {group: "clinic", label: "下次就诊医生"},
  "finalreturnvisittime": {group: "clinic", label: "末次回访时间"},
  "isfirst": {group: "clinic", label: "是否首诊"},
  "isfee": {group: "clinic", label: "是否收费"},
  // —— 费用信息 ——
  "advancepay": {group: "finance", label: "预付款"},
  "deposit": {group: "finance", label: "押金/预存"},
  "debts": {group: "finance", label: "欠款"},
  "payment": {group: "finance", label: "已缴费"},
  // —— 来源/营销 ——
  "VIP": {group: "marketing", label: "VIP 等级"},
  "VIP等级": {group: "marketing", label: "VIP 等级"},
  "会员等级": {group: "marketing", label: "会员等级"},
  "vip_level": {group: "marketing", label: "VIP 等级"},
  "患者来源": {group: "marketing", label: "患者来源"},
  "来源": {group: "marketing", label: "来源"},
  "comefrom": {group: "marketing", label: "患者来源"},
  "comefrom2": {group: "marketing", label: "来源渠道"},
  "comefromconcat1": {group: "marketing", label: "来源（组合）"},
  "推荐人": {group: "marketing", label: "推荐人"},
  "introducer": {group: "marketing", label: "推荐人"},
  "患者印象": {group: "marketing", label: "患者印象"},
  "标签": {group: "marketing", label: "标签"},
  // —— 就诊信息（续）——
  "visitname": {group: "clinic", label: "就诊类型"},
  "visitstatus": {group: "clinic", label: "就诊状态"},
  "treatment": {group: "clinic", label: "治疗内容"},
  "register": {group: "clinic", label: "登记台"},
  "updatetime": {group: "clinic", label: "更新时间"},
  // —— 预约信息 ——
  "schtime": {group: "appt", label: "预约时间"},
  "schendtime": {group: "appt", label: "预约结束"},
  "schitems": {group: "appt", label: "预约项目"},
  // —— 转诊信息 ——
  "referraldate": {group: "referral", label: "转诊日期"},
  "referraldoct": {group: "referral", label: "转诊医生"},
  // —— 备注 ——
  "remark": {group: "note", label: "备注"},
  // —— 外部导入的患者详情/列表补充字段：补中文标签,避免落「其他字段」堆英文 ——
  "occupation": {group: "identity", label: "职业"},
  "patgroup": {group: "identity", label: "患者分组"},
  "allergichistory": {group: "medical", label: "过敏史"},
  "diseasehistory": {group: "medical", label: "既往史"},
  "phone1": {group: "contact", label: "备用电话"},
  "schremark": {group: "appt", label: "预约备注"},
  "comefrom3": {group: "marketing", label: "三级来源"},
  "comefromconcat2": {group: "marketing", label: "来源渠道(组合)"},
  "comefromconcat3": {group: "marketing", label: "来源细分(组合)"},
  "comefromconcat4": {group: "marketing", label: "来源4(组合)"},
  "counselor": {group: "marketing", label: "咨询师"},
  "firtstcounselor": {group: "marketing", label: "首位咨询师"},
  "developer": {group: "marketing", label: "开发人"},
  "impressioninfo": {group: "marketing", label: "患者印象"},
  "marketcardname": {group: "marketing", label: "营销卡"},
  "viptype": {group: "marketing", label: "会员类型"},
  "vipnumber": {group: "marketing", label: "会员卡号"},
  "disconut": {group: "marketing", label: "折扣率"},
  "equitycardtype": {group: "marketing", label: "权益卡类型"},
  "vipover": {group: "marketing", label: "VIP是否到期"},
  "vipoverdate": {group: "marketing", label: "VIP到期日"},
};

// 纯技术/内部字段：不翻译也不显示（内部指针 id、匹配元数据、版本号等）。
const SOURCE_HIDDEN_KEYS = new Set([
  "_mapping", "clinicuniqueid", "customerid", "comefrom2pid", "comefrom3pid",
  "nextschid", "img", "num",
  "EMRVersion", "EMRVERSION", "emrversion",
  // 拼音检索 / 内部指针ID / 排序码 / 标志位：显示出来是噪音
  "pinyin", "referraldatesort", "referraldoctidentity", "scheduleidentity",
  "userid", "visitsort", "wechat",
  // 导入数据带来的内部/重复/噪音键：生日(已在身份/编辑区)、本店名(每人都一样)、
  // 患者照片URL、各类内部 id 与标志位、来源子码(可读的看 comefromconcat)
  "agebirthday", "birthday1", "chainpat", "clinicname", "name", "patgroupid",
  "picture", "registeridentity", "ybreadinfo",
  "impressioninfoid", "oldimpressioninfoid", "impcolour", "vipicon", "patcardidentity",
  "lastsmssendtime", "comefrom21", "comefrom31", "comefrom41",
]);

const SOURCE_GROUP_ORDER = [
  ["medical", "医疗警示"],
  ["identity", "身份信息"],
  ["contact", "联系方式"],
  ["clinic", "就诊信息"],
  ["appt", "预约信息"],
  ["referral", "转诊信息"],
  ["finance", "费用信息"],
  ["marketing", "营销信息"],
  ["note", "备注"],
  ["other", "其他字段"],
];

let workspacePatientId = "";
let workspaceData = null;
// true = 最近一次 loadWorkspacePatient 失败：tab 切换显示"载入失败"而非"加载中..."。
let workspaceLoadError = false;
const workspaceLoadedTabs = new Set();
// 档案渲染宿主：默认全屏页；患者管理右栏内嵌时设为该容器(所有 workspacePageEl() 查询都作用其内)。
let _wsHost = null;
let _workspaceRenderRevision = 0;
let _workspaceTabIntentRevision = 0;

function captureWorkspaceContinuation(patientId = workspacePatientId) {
  return {
    patientId,
    renderRevision: _workspaceRenderRevision,
    tabIntentRevision: _workspaceTabIntentRevision,
  };
}

function workspaceContinuationIsCurrent(continuation) {
  return Boolean(continuation)
    && continuation.patientId === workspacePatientId
    && continuation.renderRevision === _workspaceRenderRevision
    && continuation.tabIntentRevision === _workspaceTabIntentRevision;
}

function workspacePageEl() {
  return _wsHost || document.getElementById("patientWorkspacePage");
}

async function showPatientWorkspacePage(identity, tab = "", host = null) {
  const renderRevision = ++_workspaceRenderRevision;
  const hostChanged = _wsHost !== host;  // #118 宿主切换(内嵌↔全屏)即使同患者也要重渲到新宿主
  _wsHost = host;                       // host=null → 全屏页；传入容器 → 内嵌该容器
  const page = workspacePageEl();
  if (!page) return;
  if (!host) page.hidden = false;       // 全屏页才需显隐；内嵌宿主一直可见
  // 同步全局选中患者：今日队列行和搜索列表的高亮依赖 activePatientId。
  activePatientId = identity;
  if (identity !== workspacePatientId || !workspaceData || hostChanged) {
    workspacePatientId = identity;
    workspaceData = null;
    resetWorkspacePanels(identity);
    await loadWorkspacePatient(identity, renderRevision);
    // 竞态守卫：await 期间用户可能已切到其他患者，过期的延续不再渲染 tab。
    if (renderRevision !== _workspaceRenderRevision || identity !== workspacePatientId) return;
  }
  if (renderRevision !== _workspaceRenderRevision || identity !== workspacePatientId) return;
  switchWorkspaceTab(tab || "profile");
}

function hidePatientWorkspacePage() {
  _workspaceRenderRevision += 1;
  workspacePatientId = "";
  _wsHost = null;                                          // 先脱离内嵌宿主,避免误隐藏右栏
  const page = document.getElementById("patientWorkspacePage");
  if (page) page.hidden = true;
  // 关闭即失效缓存：工作区外的操作（如确认 AI 草稿）会改数据，重开同一患者必须重新拉取。
  workspaceData = null;
  workspaceLoadedTabs.clear();
}

function resetWorkspacePanels(identity) {
  const page = workspacePageEl();
  if (!page) return;
  workspaceLoadedTabs.clear();
  workspaceProfileEditing = false;
  if (typeof resetTreatmentPlanEditor === "function") resetTreatmentPlanEditor();
  // 患者切换：关掉可能残留的牙位弹窗（medical_editor.js），清空影像模块状态（workspace_tabs.js）。
  closeToothSelector();
  resetWorkspaceImageState();
  const rail = page.querySelector(".workspace-rail");
  if (rail) rail.textContent = "患者档案载入中...";
  const crumb = page.querySelector(".workspace-breadcrumb");
  if (crumb) crumb.textContent = identity || "";
  page.querySelectorAll("[data-workspace-panel]").forEach(panel => {
    panel.textContent = "加载中...";
  });
  page.querySelectorAll("[data-workspace-count]").forEach(badge => {
    badge.hidden = true;
    badge.textContent = "";
  });
}

async function loadWorkspacePatient(identity, renderRevision = _workspaceRenderRevision) {
  const rail = workspacePageEl() && workspacePageEl().querySelector(".workspace-rail");
  workspaceLoadError = false;
  try {
    const [detailRes, summaryRes] = await Promise.all([
      fetch(`/api/patients/${encodeURIComponent(identity)}`),
      fetch(`/api/patients/${encodeURIComponent(identity)}/summary`),
    ]);
    if (renderRevision !== _workspaceRenderRevision || identity !== workspacePatientId) return;
    if (!detailRes.ok || !summaryRes.ok) {
      markWorkspaceLoadFailed(rail);
      return;
    }
    const detail = await detailRes.json();
    if (renderRevision !== _workspaceRenderRevision || identity !== workspacePatientId) return;
    const summary = await summaryRes.json();
    if (renderRevision !== _workspaceRenderRevision || identity !== workspacePatientId) return;
    // detail 整体缓存：medical/billing/appointments 等 tab（workspace_tabs.js）从这里取数据。
    workspaceData = {patient: detail.patient, summary, detail};
    const crumb = workspacePageEl() && workspacePageEl().querySelector(".workspace-breadcrumb");
    if (crumb) crumb.textContent = detail.patient.display_name || "(无名)";
    renderWorkspaceRail(detail.patient, summary);
    renderWorkspaceTabCounts(summary.counts || {});
    // 数据到达：active tab 若在加载飞行中被点过（只显示了"加载中..."），补一次渲染。
    renderActiveWorkspaceTabIfPending();
  } catch {
    // 网络异常：避免 rail 永卡"载入中"和 unhandled rejection。
    if (renderRevision !== _workspaceRenderRevision || identity !== workspacePatientId) return;
    markWorkspaceLoadFailed(rail);
  }
}

// 载入失败：左栏 + 所有未渲染面板统一提示；已渲染过的面板（如保存备注后的重载失败）不覆盖。
function markWorkspaceLoadFailed(rail) {
  workspaceLoadError = true;
  if (rail) rail.textContent = "患者档案载入失败";
  const page = workspacePageEl();
  if (!page) return;
  page.querySelectorAll("[data-workspace-panel]").forEach(panel => {
    if (!workspaceLoadedTabs.has(panel.dataset.workspacePanel)) panel.textContent = "患者档案载入失败";
  });
}

function renderActiveWorkspaceTabIfPending() {
  const page = workspacePageEl();
  if (!page) return;
  const active = page.querySelector(".workspace-tab.active");
  if (active && !workspaceLoadedTabs.has(active.dataset.workspaceTab)) {
    switchWorkspaceTab(active.dataset.workspaceTab);
  }
}

// #398/#547：tab按钮文本(去掉角标数字span)当标题用，标签改名时自动跟着变，不用额外维护一份映射
function workspaceTabLabel(page, tab) {
  const btn = page.querySelector(`[data-workspace-tab="${cssEscape(tab)}"]`);
  if (!btn) return "";
  const clone = btn.cloneNode(true);
  clone.querySelectorAll(".workspace-tab-count").forEach(el => el.remove());
  return clone.textContent.trim();
}

function switchWorkspaceTab(tab) {
  const page = workspacePageEl();
  if (!page) return;
  if (!page.querySelector(`[data-workspace-tab="${cssEscape(tab)}"]`)) tab = "profile";
  const activeTab = page.querySelector(".workspace-tab.active")?.dataset.workspaceTab || "";
  const changesIntent = tab !== activeTab;
  if (changesIntent) {
    _workspaceTabIntentRevision += 1;
  } else if (workspaceLoadedTabs.has(tab)) {
    return;
  }
  // 切离基本资料时退出编辑态，并踢出已加载集合，确保切回时重渲为只读（而非残留编辑表单）
  if (tab !== "profile" && workspaceProfileEditing) {
    workspaceProfileEditing = false;
    workspaceLoadedTabs.delete("profile");
  }
  page.querySelectorAll("[data-workspace-tab]").forEach(button => {
    button.classList.toggle("active", button.dataset.workspaceTab === tab);
  });
  page.querySelectorAll("[data-workspace-panel]").forEach(panel => {
    const active = panel.dataset.workspacePanel === tab;
    panel.classList.toggle("active", active);
    panel.hidden = !active;
  });
  if (!workspaceLoadedTabs.has(tab)) {
    workspaceLoadedTabs.add(tab);
    loadWorkspaceTabPanel(tab);
  }
  // #398/#547：URL hash 和顶部 H1 跟着当前患者+tab 走，不再停在"今日工作台"/旧路由
  // (仅全屏页；内嵌右栏没有独立路由，不动 hash/H1)
  if (!_wsHost) {
    const next = `#patient/${encodeURIComponent(workspacePatientId)}/${encodeURIComponent(tab)}`;
    // 用 replaceState 替换不压栈:直接赋 location.hash 会每切一次tab压一条历史,
    // 「← 返回」的 history.back() 在tab历史里打转出不去(真机#返回键失灵)
    if (location.hash !== next) {
      if (typeof history !== "undefined" && history.replaceState) history.replaceState(history.state || null, "", next);
      else location.hash = next;
    }
    if (typeof markPatientWorkspaceHashCommitted === "function") {
      markPatientWorkspaceHashCommitted(next);
    }
    const name = (workspaceData && workspaceData.patient && workspaceData.patient.display_name) || "";
    const label = workspaceTabLabel(page, tab);
    if (typeof workspaceTitle !== "undefined" && workspaceTitle) {
      workspaceTitle.textContent = name ? `${name} · ${label}` : label;
    }
  }
  // 切 tab 回顶部：避免沿用上个视图的滚动位置(仅全屏页;内嵌右栏不滚整页)
  if (!_wsHost) window.scrollTo(0, 0);
}

function loadWorkspaceTabPanel(tab) {
  const page = workspacePageEl();
  if (!page) return;
  const panel = page.querySelector(`[data-workspace-panel="${cssEscape(tab)}"]`);
  if (!panel) return;
  // 初始加载飞行中 / 失败：不进 renderer。飞行中不标记 loadedTabs，数据到达后由
  // loadWorkspacePatient → renderActiveWorkspaceTabIfPending 补渲染。
  if (!workspaceData) {
    workspaceLoadedTabs.delete(tab);
    panel.textContent = workspaceLoadError ? "患者档案载入失败" : "加载中...";
    return;
  }
  const renderers = {
    profile: renderWorkspaceProfile,
    plan: renderWorkspaceTreatmentPlanTab,
    surgery: renderWorkspaceSurgeryTab,
    "oral-exam": renderWorkspaceOralExamTab,
    visits: renderWorkspaceVisitsTab,
    medical: renderWorkspaceMedicalTab,
    treatments: renderWorkspaceTreatmentsTab,
    "lab-orders": renderWorkspaceLabOrdersTab,
    images: renderWorkspaceImagesTab,
    billing: renderWorkspaceBillingTab,
    "return-visits": renderWorkspaceReturnVisitsTab,
    member: renderWorkspaceMemberTab,
    notes: renderWorkspaceNotesTab,
  };
  const renderer = renderers[tab];
  if (renderer) renderer(panel);
}

// 经营概览卡(大客户经营)：累计消费/到诊次数/首末诊一眼看全
function renderBizOverview(biz) {
  if (!biz) return "";
  const money = n => "¥" + Number(n || 0).toLocaleString("zh-CN", {minimumFractionDigits: 2, maximumFractionDigits: 2});
  const d10 = s => String(s || "").slice(0, 10) || "—";
  const spend = Object.prototype.hasOwnProperty.call(biz, "total_spend")
    ? `<div class="biz-row biz-spend"><span>累计消费</span><strong>${money(biz.total_spend)}</strong></div>`
    : "";
  return `
    <div class="biz-overview">
      ${spend}
      <div class="biz-row"><span>到诊次数</span><strong>${Number(biz.visit_count || 0)} 次</strong></div>
      <div class="biz-row"><span>首诊 → 末诊</span><strong>${d10(biz.first_visit)} → ${d10(biz.last_visit)}</strong></div>
    </div>`;
}

function renderWorkspaceRail(p, summary) {
  const rail = workspacePageEl() && workspacePageEl().querySelector(".workspace-rail");
  if (!rail) return;
  const source = parseSourceJson(p.source_json);
  // P0-2：优先本地业务列(本地录入的过敏史)，回退 source_json 镜像值
  const allergy = String(p.allergy_history || "").trim() || firstSourceValue(source, ALLERGY_KEYS);
  const vip = firstSourceValue(source, VIP_KEYS);
  const name = p.display_name || "(无名)";
  const avatarChar = (String(name).match(/[一-龥A-Za-z]/) || ["患"])[0];
  const pid = p.patient_identity || "";
  const avatarInner = p.avatar_path
    ? `<img src="/api/patients/${encodeURIComponent(pid)}/avatar?t=${encodeURIComponent(p.local_updated_at || "")}" alt="头像">`
    : escapeHtml(avatarChar);
  const sv = k => escapeHtml(sourceValueText(source[k]));
  const pair = (label, value) =>
    `<div class="wh-field"><span class="wh-flabel">${label}</span><span class="wh-fvalue">${value || "—"}</span></div>`;
  rail.innerHTML = `
    <div class="wh-avatar" title="点击上传头像" data-avatar-upload>${avatarInner}</div>
    <div class="workspace-rail-name">${escapeHtml(name)}</div>
    <div class="workspace-rail-sub">${escapeHtml(workspaceSexAgeText(p))}</div>
    <div class="rail-actions">
      <button type="button" class="plain-button" data-rail-act="reg">挂号</button>
      <button type="button" class="plain-button" data-rail-act="appt">预约</button>
      <button type="button" class="plain-button" data-rail-act="triage">分诊</button>
      <button type="button" class="plain-button" data-rail-act="followup" title="快速新建回访(待回访计划)">回访</button>
    </div>
    ${allergy ? `<div class="workspace-allergy-badge">过敏：${escapeHtml(allergy)}</div>` : ""}
    ${vip ? `<div class="workspace-vip-badge">VIP：${escapeHtml(vip)}</div>` : ""}
    <div class="rail-tags" id="railTags"></div>
    ${renderBizOverview(summary.business)}
    <button type="button" class="plain-button rail-fields-toggle" data-rail-fields-toggle>详细资料</button>
    <div class="workspace-rail-fields">
      ${pair("电话", escapeHtml(p.phone || ""))}
      ${pair("病历号", escapeHtml(p.chart_no || "") || sv("patientid") || escapeHtml(p.patient_identity || ""))}
      ${pair("创建人", sv("register"))}
      ${pair("创建时间", sv("createtime"))}
      ${pair("上次就诊", sv("latelydoct"))}
      ${pair("上次日期", escapeHtml((summary.last_visit_time || "").replace(/(\d{2}:\d{2}:\d{2})\.\d+$/, "$1")) || sv("latelydate"))}
      ${pair("来源", sv("comefromconcat1") || sv("comefrom"))}
      ${pair("备注", sv("remark"))}
    </div>
  `;
  // GD-02:手机端详细字段收起态按患者记账——换患者收起,同患者重渲染(如收款后刷新)保留展开
  if (rail.dataset.railExpandedFor !== pid) {
    rail.classList.remove("rail-expanded");
    delete rail.dataset.railExpandedFor;
  }
  const fieldsToggle = rail.querySelector("[data-rail-fields-toggle]");
  if (fieldsToggle) fieldsToggle.addEventListener("click", () => {
    if (rail.classList.toggle("rail-expanded")) rail.dataset.railExpandedFor = pid;
    else delete rail.dataset.railExpandedFor;
  });
  // #105 头像上传用闭包绑定(pid 不拼进 inline JS,避免注入)
  const av = rail.querySelector("[data-avatar-upload]");
  if (av) av.addEventListener("click", () => uploadPatientAvatar(pid));
  // 挂号/预约/分诊 快捷(闭包绑定,不拼内联JS)
  const railName = p.display_name || name;
  rail.querySelectorAll("[data-rail-act]").forEach(b => b.addEventListener("click", () => railAction(b.dataset.railAct, pid, railName)));
  if (typeof loadPatientTags === "function") loadPatientTags(pid);   // 专属标签(大客户经营)
}

// 患者档案左卡片：挂号直接进今日候诊；预约/分诊仍复用各自弹框。
async function railAction(act, pid, pname) {
  if (act === "followup") {
    // 快速新建回访(按钮文案「回访」,用户拍板 2026-07-26,原「跟进」更名):跳客户沟通 tab →
    // 强制落「回访」子tab → 展开"新增计划(待回访)"表单。复用回访机制,非群发。
    if (typeof switchWorkspaceTab === "function") switchWorkspaceTab("return-visits");
    if (typeof showCustomerCommReturnVisit === "function") showCustomerCommReturnVisit();   // #710 面板可能已加载在面诊子tab,显式切回访否则下方轮询新增表单永远找不到
    let tries = 0;
    const open = () => {
      const page = (typeof workspacePageEl === "function") ? workspacePageEl() : null;
      if (page && page.querySelector("[data-new-rv-panel]")) {
        if (typeof showReturnVisitForm === "function") showReturnVisitForm("计划");
      } else if (tries++ < 20) {
        setTimeout(open, 50);
      }
    };
    open();
    return;
  }
  if (act === "reg") {
    await checkInPatient({
      patientIdentity: pid,
      displayName: pname,
      openTriageAfter: false,
      switchToToday: true,
      showSuccess: true,
    });
    return;
  }
  if (act === "appt") {
    if (typeof openNewAppointment === "function") openNewAppointment({});
    if (typeof pickApptPatient === "function") pickApptPatient({dataset: {pid: pid, pname: pname, chart: ""}});
    return;
  }
  if (act === "triage") {
    let v;
    try { v = await (await fetch(`/api/patients/${encodeURIComponent(pid)}/today-visit`)).json(); }
    catch { window.alert("分诊失败（网络异常）"); return; }
    if (!v || !v.has_today) { window.alert("该患者今天还没挂号，请先挂号再分诊"); return; }
    if (typeof openTriage === "function") {
      openTriage(v.appointment_id, {doctor_name: v.doctor_name, visit_type: v.visit_type, room: v.room, display_name: pname, status: v.status});
    }
  }
}

// 点患者页头像 → 选图上传 → 刷新显示
function uploadPatientAvatar(pid) {
  if (!pid) return;
  const inp = document.createElement("input");
  inp.type = "file";
  inp.accept = "image/*";
  inp.onchange = async () => {
    const f = inp.files && inp.files[0];
    if (!f) return;
    const fd = new FormData();
    fd.append("file", f);
    let res;
    try { res = await fetch(`/api/patients/${encodeURIComponent(pid)}/avatar`, {method: "POST", body: fd}); }
    catch { window.alert("上传失败（网络异常）"); return; }
    if (!res.ok) { const m = await res.json().catch(() => ({})); window.alert("上传失败：" + (m.detail || res.status)); return; }
    if (typeof refreshWorkspaceDetail === "function") await refreshWorkspaceDetail();
    else location.reload();
  };
  inp.click();
}
window.uploadPatientAvatar = uploadPatientAvatar;

function renderWorkspaceTabCounts(counts) {
  const page = workspacePageEl();
  if (!page) return;
  const hasBillingCount = Object.prototype.hasOwnProperty.call(counts, "bills");
  const mapping = {
    medical: counts.medical_records,
    // #381 用户拍板:处置 tab 不标数值(项目数/单数口径都易误读),看单直接进 tab
    images: counts.images,
    // #726 「客户沟通」含 回访/沟通咨询/面诊 三子tab,只标回访数会误导(有沟通没回访会显示0),去掉父tab角标
    notes: counts.local_notes,
  };
  Object.entries(mapping).forEach(([tab, count]) => {
    const badge = page.querySelector(`[data-workspace-count="${cssEscape(tab)}"]`);
    if (!badge) return;
    badge.textContent = formatCount(count || 0);
    badge.hidden = false;
  });
  const billingBadge = page.querySelector(`[data-workspace-count="${cssEscape("billing")}"]`);
  if (billingBadge) {
    billingBadge.textContent = hasBillingCount ? formatCount(counts.bills || 0) : "";
    billingBadge.hidden = !hasBillingCount;
  }
}

function workspaceSexAgeText(p) {
  const sex = String(p.sex || "").trim() || "—";
  const age = ageFromBirthday(p.birthday);
  const ageText = age !== null ? `${age}岁` : (String(p.birthday || "").trim() || "—");
  return `${sex} · ${ageText}`;
}

function ageFromBirthday(birthday) {
  const s = String(birthday || "").trim();
  const match = /^(\d{4})-(\d{1,2})-(\d{1,2})/.exec(s);
  const yearOnly = !match && /^(\d{4})$/.test(s);   // 年龄直填只存推算出生年
  if (!match && !yearOnly) return null;
  const year = Number(match ? match[1] : s);
  const now = bjToday();
  let age = now.getFullYear() - year;
  if (match) {
    const [month, day] = [Number(match[2]), Number(match[3])];
    if (now.getMonth() + 1 < month || (now.getMonth() + 1 === month && now.getDate() < day)) age -= 1;
  }
  return age >= 0 && age <= 150 ? age : null;
}

// 基本资料默认只读，点"修改"进编辑态；只有 PROFILE_EDITABLE 白名单字段可改，其余既定事实只读。
const PROFILE_EDITABLE_FIELDS = [
  {label: "姓名", name: "display_name", key: "display_name"},
  {label: "电话", name: "phone", key: "phone"},
  {label: "性别", name: "sex", key: "sex"},
  {label: "生日", name: "birthday", key: "birthday"},
  {label: "地址", name: "address", key: "address", className: "wide"},
  {label: "过敏史", name: "allergy_history", key: "allergy_history", className: "wide", dict: "AllergyHistory"},   // P0-2 医疗安全 / P0-4 字典
  {label: "用药史", name: "medication_history", key: "medication_history", className: "wide"},
  {label: "身份证号", name: "id_card", key: "id_card"},          // P0-3 建档扩字段
  {label: "微信", name: "wechat", key: "wechat"},
  {label: "邮箱", name: "email", key: "email"},
  {label: "职业", name: "occupation", key: "occupation", dict: "Occupation"},          // P0-4 字典
  {label: "工作单位", name: "work_unit", key: "work_unit"},
  {label: "患者类型", name: "patient_type", key: "patient_type", dict: "PatientKind"},  // P0-4 字典
  {label: "责任医生", name: "responsible_doctor", key: "responsible_doctor", staffRole: "医生"},   // P1-6 选人下拉
  {label: "咨询师", name: "consultant", key: "consultant", staffRole: "咨询师"},
  {label: "患者来源", name: "referral_source", key: "referral_source", dict: "ComeFrom"},   // 收费框架A / P0-4 字典
  {label: "二级来源", name: "referral_source2", key: "referral_source2", dict: "ComeFrom"},
  {label: "三级来源", name: "referral_source3", key: "referral_source3", dict: "ComeFrom"},
  {label: "来源4", name: "referral_source4", key: "referral_source4", dict: "ComeFrom"},
  {label: "介绍人类型", name: "introducer_type", key: "introducer_type"},
  {label: "介绍人", name: "introducer_name", key: "introducer_name"},
  {label: "机主关系", name: "phone_vestee", key: "phone_vestee"},
  {label: "备注", name: "remark", key: "remark", className: "wide"},
];
let workspaceProfileEditing = false;

function renderWorkspaceProfile(panel) {
  if (!workspaceData || !workspaceData.patient) {
    panel.textContent = "患者档案载入失败";
    return;
  }
  const p = workspaceData.patient;
  const headExtra = workspaceProfileEditing
    ? `<span class="panel-head-right"><span>编辑中</span></span>`
    : `<span class="panel-head-right"><span>本地可编辑</span><button type="button" class="panel-head-action" onclick="toggleWorkspaceProfileEdit()">修改</button></span>`;
  const body = workspaceProfileEditing
    ? `
        <div class="edit-grid workspace-profile-edit">
          ${PROFILE_EDITABLE_FIELDS.map(f =>
            workspaceEditField(f.label, f.name, p[f.key], f.className || "", f.dict || "", f.staffRole || "")).join("")}
          <div class="edit-actions">
            <button type="button" onclick="saveWorkspaceProfile('${escapeAttr(p.patient_identity)}')">保存资料</button>
            <button type="button" class="ghost-button" onclick="cancelWorkspaceProfileEdit()">取消</button>
            <span id="workspaceProfileSaveStatus"></span>
          </div>
        </div>`
    : `
        <div class="workspace-source-card">
          ${PROFILE_EDITABLE_FIELDS.map(f => `
            <div class="workspace-source-row">
              <span class="field-label">${f.label}</span>
              <span class="field-value">${escapeHtml(sourceValueText(p[f.key]) || "—")}</span>
            </div>
          `).join("")}
        </div>`;
  panel.innerHTML = `
    <section class="panel">
      <div class="panel-head"><span>基本资料</span>${headExtra}</div>
      <div class="panel-body">${body}</div>
    </section>
    ${renderSourceGroups(p.source_json)}
  `;
  // P0-4：编辑态下给带 data-dict 的字段挂字典 datalist；带 data-staff-role 的挂员工选人 datalist
  if (workspaceProfileEditing) {
    if (typeof bindDictInputs === "function") bindDictInputs(panel);
    if (typeof bindStaffInputs === "function") bindStaffInputs(panel);
  }
}

function toggleWorkspaceProfileEdit() {
  workspaceProfileEditing = true;
  rerenderWorkspaceProfilePanel();
}

function cancelWorkspaceProfileEdit() {
  workspaceProfileEditing = false;
  rerenderWorkspaceProfilePanel();
}

function rerenderWorkspaceProfilePanel() {
  const panel = workspacePageEl() &&
    workspacePageEl().querySelector('[data-workspace-panel="profile"]');
  if (panel) renderWorkspaceProfile(panel);
}

function workspaceEditField(label, name, value, className = "", dict = "", staffRole = "") {
  const dictAttr = dict ? ` data-dict="${escapeAttr(dict)}"` : "";
  const staffAttr = staffRole ? ` data-staff-role="${escapeAttr(staffRole)}"` : "";
  return `
    <label class="${className}">
      <span class="field-label">${label}</span>
      <input class="workspace-profile-input" data-field="${escapeAttr(name)}"${dictAttr}${staffAttr} value="${escapeAttr(value || "")}">
    </label>
  `;
}

async function saveWorkspaceProfile(patientId) {
  const continuation = captureWorkspaceContinuation(patientId);
  const status = document.getElementById("workspaceProfileSaveStatus");
  const payload = {};
  document.querySelectorAll(".workspace-profile-input").forEach(input => {
    payload[input.dataset.field] = input.value.trim();
  });
  if (status) status.textContent = "保存中...";
  let res;
  try {
    res = await fetch(`/api/patients/${encodeURIComponent(patientId)}`, {
      method: "PUT",
      headers: {"Content-Type": "application/json"},
      body: JSON.stringify(payload),
    });
  } catch {
    if (!workspaceContinuationIsCurrent(continuation)) return;
    if (status) status.textContent = "保存失败（网络异常），请确认后重试";
    return;
  }
  if (!workspaceContinuationIsCurrent(continuation)) return;
  if (!res.ok) {
    if (status) status.textContent = "保存失败";
    return;
  }
  if (status) status.textContent = "已保存";
  workspaceProfileEditing = false;
  await loadWorkspacePatient(patientId, continuation.renderRevision);
  if (!workspaceContinuationIsCurrent(continuation)) return;
  workspaceLoadedTabs.delete("profile");
  switchWorkspaceTab("profile");
  await loadAuditLogs();
}

function parseSourceJson(raw) {
  if (!raw) return {};
  try {
    const parsed = JSON.parse(raw);
    if (parsed && typeof parsed === "object" && !Array.isArray(parsed)) return parsed;
  } catch {
    return {};
  }
  return {};
}

function firstSourceValue(source, keys) {
  for (const key of keys) {
    const text = sourceValueText(source[key]);
    if (text) return text;
  }
  return "";
}

function sourceValueText(value) {
  if (value === null || value === undefined) return "";
  if (typeof value === "object") {
    try {
      return JSON.stringify(value);
    } catch {
      return "";
    }
  }
  return String(value).trim();
}

function renderSourceGroups(raw) {
  const source = parseSourceJson(raw);
  const grouped = {medical: [], identity: [], contact: [], clinic: [], appt: [], referral: [], finance: [], marketing: [], note: [], other: []};
  Object.entries(source).forEach(([key, value]) => {
    if (SOURCE_HIDDEN_KEYS.has(key)) return;
    const text = sourceValueText(value);
    if (!text) return;
    const mapping = SOURCE_FIELD_GROUPS[key];
    if (mapping) {
      grouped[mapping.group].push([mapping.label, text]);
    } else {
      grouped.other.push([key, text]);
    }
  });
  const cards = SOURCE_GROUP_ORDER
    .filter(([group]) => grouped[group].length)
    .map(([group, title]) => sourceGroupCard(group, title, grouped[group]));
  if (!cards.length) return "";
  return `
    <section class="panel">
      <div class="panel-head">原始档案<span>SaaS 同步只读</span></div>
      <div class="panel-body workspace-source-groups">${cards.join("")}</div>
    </section>
  `;
}

function sourceGroupCard(group, title, entries) {
  return `
    <div class="workspace-source-card${group === "medical" ? " workspace-source-alert" : ""}">
      <div class="workspace-source-title">${escapeHtml(title)}</div>
      ${entries.map(([label, text]) => `
        <div class="workspace-source-row">
          <span class="field-label">${escapeHtml(label)}</span>
          <span class="field-value">${escapeHtml(text)}</span>
        </div>
      `).join("")}
    </div>
  `;
}

Object.assign(window, {
  captureWorkspaceContinuation,
  workspaceContinuationIsCurrent,
  showPatientWorkspacePage,
  hidePatientWorkspacePage,
  resetWorkspacePanels,
  loadWorkspacePatient,
  markWorkspaceLoadFailed,
  renderActiveWorkspaceTabIfPending,
  switchWorkspaceTab,
  loadWorkspaceTabPanel,
  renderWorkspaceRail,
  renderWorkspaceTabCounts,
  workspaceSexAgeText,
  ageFromBirthday,
  renderWorkspaceProfile,
  toggleWorkspaceProfileEdit,
  cancelWorkspaceProfileEdit,
  workspaceEditField,
  saveWorkspaceProfile,
  parseSourceJson,
  firstSourceValue,
  sourceValueText,
  renderSourceGroups,
  sourceGroupCard,
});
