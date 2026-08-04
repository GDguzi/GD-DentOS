// 配台人员 (#4)：人员管理(增删改) + 处置配台下拉 + 记住上次选择。
// 角色：医生/护士/咨询师/助理。后端 /api/staff-members CRUD。

const STAFF_ROLES = ["医生", "护士", "咨询师", "助理"];
// 诊所全部岗位(对齐后端 staff.ROLES)：员工管理页用于多岗位勾选 + 分组显示
// #420:按权限树蓝本补齐岗位(增 客服/助理2)。#502:去「技师」,技工职能并入助理/前台。
const ALL_STAFF_ROLES = ["医生", "护士", "咨询师", "助理", "前台", "收银员", "主任", "管理员", "客服", "助理2"];
// 主岗位优先级(多岗位时取第一个命中的做 role，临床优先)
const ROLE_PRIORITY = ["医生", "护士", "咨询师", "助理", "助理2", "前台", "收银员", "客服", "主任", "管理员"];
// 岗位→账号角色映射(开通登录时默认带出)。#420:主任/收银员/客服/助理2 用各自独立角色,不再并到 admin/assistant/reception。
const ROLE_TO_ACCT = {
  "管理员": "admin", "主任": "director", "医生": "doctor", "护士": "nurse",
  "助理": "assistant", "助理2": "assistant2", "咨询师": "consultant",
  "前台": "reception", "收银员": "cashier", "客服": "support",
};
let staffAccts = {};   // staff_id → 账号摘要({username, account_role, is_active}) 或 undefined(未开通)
let staffFull = [];    // 员工管理页明细(含档案字段，来自守 staff.manage 的 /api/staff-admin/list)
// 处置 model 字段 ↔ 角色
const STAFF_FIELD_ROLE = [
  ["doctor_name", "医生"],
  ["nurse_name", "护士"],
  ["consultant_name", "咨询师"],
  ["assistant_name", "助理"],
];
const STAFF_TEAM_LS = "dental_team_last";   // 记住上次配台

let staffCache = null;
let _smTarget = "staffManagerBody";   // 人员管理渲染目标容器(弹框 或 配置管理整页)
let _smRenderRevision = 0;
let _smEditId = null;                  // #224 正在编辑的员工 staff_id(null=新增态)

async function ensureStaff(force) {
  if (staffCache && !force) return staffCache;
  try {
    const res = await fetch("/api/staff-members");
    staffCache = (await res.json()).members || [];
  } catch { staffCache = null; return []; }   // 失败不缓存空名单,下次调用重试
  refreshStaffDatalists();
  return staffCache;
}

// 页面上已存在的员工 datalist 全量重填。ensureStaff 每次真正取数后自动调用,
// 所以人员新增/修改/离职/复职后走 ensureStaff(true) 即可让所有打开中的选人框拿到新候选。
function refreshStaffDatalists() {
  document.querySelectorAll('datalist[id^="staff-"]').forEach(dl => {
    const role = dl.id.slice("staff-".length);
    dl.innerHTML = staffByRole(role).map(m => `<option value="${escapeAttr(m.name)}"></option>`).join("");
  });
}

function staffByRole(role) {
  if (role === "*") return staffCache || [];   // #420二批:经手人/领用人/收银员筛选等不限岗位,全员候选
  // 多岗位：主岗位 role 命中 或 roles 列表含该角色(与后端 ?role= 口径一致)
  return (staffCache || []).filter(m => m.role === role || (m.roles || []).includes(role));
}

// 选人 datalist 机制(同 dict.js 的 data-dict)：给 <input data-staff-role="医生"> 自动挂
// 员工建议项，可搜可自由填(外院医生/临时名字照填)，契合"都可以填"。多岗位走 staffByRole 匹配。
async function ensureStaffDatalist(role) {
  await ensureStaff();
  const id = `staff-${role}`;
  let dl = document.getElementById(id);
  if (!dl) { dl = document.createElement("datalist"); dl.id = id; document.body.appendChild(dl); }
  dl.innerHTML = staffByRole(role).map(m => `<option value="${escapeAttr(m.name)}"></option>`).join("");
  return id;
}

// 给 root 内带 data-staff-role 的输入框批量挂员工 datalist。面板渲染后调用。
function bindStaffInputs(root) {
  const scope = root || document;
  scope.querySelectorAll("input[data-staff-role]:not([list])").forEach(input => {
    const role = input.dataset.staffRole;
    if (!role) return;
    input.setAttribute("list", `staff-${role}`);
    ensureStaffDatalist(role);
  });
}

// 某角色的下拉 options（含"（不填）"），selected 为当前选中姓名
function staffOptionsHtml(role, selected) {
  const opts = ['<option value="">（不填）</option>'];
  staffByRole(role).forEach(m => {
    opts.push(`<option value="${escapeAttr(m.name)}"${m.name === selected ? " selected" : ""}>${escapeHtml(m.name)}</option>`);
  });
  // 当前选中的人若已被删/不在库，也保留显示
  if (selected && !staffByRole(role).some(m => m.name === selected)) {
    opts.push(`<option value="${escapeAttr(selected)}" selected>${escapeHtml(selected)}（已停用）</option>`);
  }
  return opts.join("");
}

// 记住上次配台
function rememberTeam(team) {
  try { localStorage.setItem(STAFF_TEAM_LS, JSON.stringify(team || {})); } catch { /* ignore */ }
}
function lastTeam() {
  try { return JSON.parse(localStorage.getItem(STAFF_TEAM_LS) || "{}") || {}; } catch { return {}; }
}
// #52：把"上次配台"过滤为仍在职人员——离职/停用的不带入新单(否则继续记到新业务、污染绩效)。
// 需先 ensureStaff() 载入人员库。
function activeTeam(team) {
  const out = {};
  STAFF_FIELD_ROLE.forEach(([field, role]) => {
    const name = (team || {})[field] || "";
    out[field] = (name && staffByRole(role).some(m => m.name === name)) ? name : "";
  });
  return out;
}

// ---------- 人员管理弹框 ----------
const STAFF_PINYIN_BOUNDARIES = [
  ["A", "阿"], ["B", "芭"], ["C", "擦"], ["D", "搭"], ["E", "蛾"],
  ["F", "发"], ["G", "噶"], ["H", "哈"], ["J", "击"], ["K", "喀"],
  ["L", "垃"], ["M", "妈"], ["N", "拿"], ["O", "哦"], ["P", "啪"],
  ["Q", "期"], ["R", "然"], ["S", "撒"], ["T", "塌"], ["W", "挖"],
  ["X", "昔"], ["Y", "压"], ["Z", "匝"],
];
const STAFF_PINYIN_COLLATOR = new Intl.Collator("zh-CN-u-co-pinyin");

const _accessStaffState = {
  members: [], loadState: "idle", error: "",
  query: "", roleFilter: "", employmentFilter: "",
  accountFilter: "", selectedStaffId: null, listScrollTop: 0,
  formOpen: false, formStaffId: null, formError: "", formPending: false, formRevision: 0,
  action: null, actionError: "", actionPending: false,
};

const ACCESS_STAFF_ROLES = [
  ["doctor", "医生"], ["nurse", "护士"], ["consultant", "咨询师"],
  ["assistant", "助理"], ["assistant2", "助理2"], ["reception", "前台"],
  ["cashier", "收银员"], ["director", "主任"], ["support", "客服"],
];
const ACCESS_STAFF_ROLE_ORDER = ACCESS_STAFF_ROLES.map(([key]) => key);

function staffPinyinInitials(name) {
  return [...String(name || "")].map(char => {
    if (!/[\u3400-\u9fff\uf900-\ufaff]/.test(char)) return char.toLowerCase();
    let initial = char;
    for (const [letter, boundary] of STAFF_PINYIN_BOUNDARIES) {
      if (STAFF_PINYIN_COLLATOR.compare(char, boundary) < 0) break;
      initial = letter;
    }
    return initial.toLowerCase();
  }).join("");
}

function _accessStaffCanEdit() {
  if (typeof hasPerm !== "function") return false;
  if (hasPerm("staff.edit")) return true;
  return window.__accessV3 !== true && hasPerm("staff.manage");
}

function _accessStaffHasPerm(permission) {
  return typeof hasPerm === "function" && hasPerm(permission);
}

function _accessStaffCanManageAccount(member) {
  return !_accessStaffIsSystemAdmin(member) || window.__isSystemAdmin === true;
}

function _accessRoleLabel(role) {
  if (!role) return "（未设岗位）";
  const fallback = Object.fromEntries(ACCESS_STAFF_ROLES);
  const labels = typeof _ROLE_LABELS === "object" ? _ROLE_LABELS : fallback;
  return labels[role] || fallback[role] || role;
}

function _accessRoleKey(role) {
  const match = ACCESS_STAFF_ROLES.find(([key, label]) => role === key || role === label);
  return match ? match[0] : role;
}

function _accessLegacyRole(role) {
  const match = ACCESS_STAFF_ROLES.find(([key]) => role === key);
  return match ? match[1] : role;
}

function _accessPrimaryRole(member) {
  return member.primary_role || member.role || "";
}

function _accessRoleKeys(member) {
  const roles = Array.isArray(member.role_keys) && member.role_keys.length
    ? member.role_keys
    : (Array.isArray(member.roles) && member.roles.length ? member.roles : [_accessPrimaryRole(member)]);
  return [...new Set(roles.filter(Boolean))];
}

function _accessStaffIsLeft(member) {
  return member.employment_status === "left" || member.employment_status === "inactive"
    || member.employment_status === "离职" || Boolean(member.left_at);
}

function _accessStaffAccountInvalid(member) {
  const account = member && member.account;
  if (account === null || typeof account === "undefined") return false;
  return typeof account !== "object" || Array.isArray(account)
    || typeof account.user_id !== "string" || !account.user_id.trim();
}

function _accessStaffValidAccount(member) {
  return _accessStaffAccountInvalid(member) ? null : ((member && member.account) || null);
}

function _accessStaffIsSystemAdmin(member) {
  const account = _accessStaffValidAccount(member);
  return Boolean(account && (account.is_system_admin
    || (window.__accessV3 !== true && account.account_role === "admin")));
}

function _accessMask(value, visibleStart = 3, visibleEnd = 4) {
  const text = String(value || "");
  if (!text) return "";
  if (text.length <= visibleStart + visibleEnd) return "*".repeat(text.length);
  return text.slice(0, visibleStart) + "*".repeat(Math.max(4, text.length - visibleStart - visibleEnd)) + text.slice(-visibleEnd);
}

function _accessStaffRememberScroll() {
  const box = document.getElementById(_smTarget);
  const list = box && box.querySelector(".access-staff-list-scroll");
  if (list) _accessStaffState.listScrollTop = list.scrollTop || 0;
}

function _accessStaffRestoreScroll() {
  const box = document.getElementById(_smTarget);
  const list = box && box.querySelector(".access-staff-list-scroll");
  if (list) list.scrollTop = _accessStaffState.listScrollTop;
}

async function openStaffManager() {
  const m = document.getElementById("staffManagerModal");
  if (!m) return;
  _smTarget = "staffManagerBody";
  m.hidden = false;
  await renderStaffInto(_smTarget);
}
function closeStaffManager() {
  const m = document.getElementById("staffManagerModal");
  if (m) m.hidden = true;
}

function _isCurrentStaffRender(revision, targetId, box) {
  return revision === _smRenderRevision
    && _smTarget === targetId
    && document.getElementById(targetId) === box;
}

// 把人员管理渲染进指定容器(配置管理整页用)。
async function renderStaffInto(targetId) {
  const renderRevision = ++_smRenderRevision;
  _smTarget = targetId;
  const box = document.getElementById(targetId);
  if (!box) return;
  _accessStaffState.loadState = "loading";
  _accessStaffState.error = "";
  renderStaffManager();
  try {
    const res = await fetch("/api/staff-admin/list");
    if (!_isCurrentStaffRender(renderRevision, targetId, box)) return;
    if (!res.ok) throw new Error("load failed");
    const payload = await res.json();
    if (!_isCurrentStaffRender(renderRevision, targetId, box)) return;
    _accessStaffState.members = Array.isArray(payload.members) ? payload.members : [];
    staffFull = _accessStaffState.members;
    staffAccts = {};
    staffFull.forEach(member => {
      const account = _accessStaffValidAccount(member);
      if (account) staffAccts[member.staff_id] = account;
    });
    _accessStaffState.loadState = "ready";
  } catch {
    if (!_isCurrentStaffRender(renderRevision, targetId, box)) return;
    _accessStaffState.members = [];
    staffFull = [];
    staffAccts = {};
    _accessStaffState.loadState = "error";
    _accessStaffState.error = "人员数据加载失败，请稍后重试";
  }
  renderStaffManager();
}

async function ensureStaffAccts() {
  await renderStaffInto(_smTarget);
}

function _accessStaffSearchMatches(member, query) {
  const needle = String(query || "").trim().toLowerCase();
  if (!needle) return true;
  const account = _accessStaffValidAccount(member) || {};
  return [member.name, account.username, member.job_no, member.phone, staffPinyinInitials(member.name)]
    .some(value => String(value || "").toLowerCase().includes(needle));
}

function _accessStaffFilteredMembers() {
  return _accessStaffState.members.filter(member => {
    if (!_accessStaffSearchMatches(member, _accessStaffState.query)) return false;
    if (_accessStaffState.roleFilter && !_accessRoleKeys(member).includes(_accessStaffState.roleFilter)) return false;
    const left = _accessStaffIsLeft(member);
    if (_accessStaffState.employmentFilter === "employed" && left) return false;
    if (_accessStaffState.employmentFilter === "left" && !left) return false;
    const account = _accessStaffValidAccount(member);
    if (_accessStaffState.accountFilter === "none" && account) return false;
    if (_accessStaffState.accountFilter === "opened" && !account) return false;
    if (_accessStaffState.accountFilter === "enabled" && (!account || !account.is_active)) return false;
    if (_accessStaffState.accountFilter === "disabled" && (!account || account.is_active)) return false;
    if (_accessStaffState.accountFilter === "admin" && !_accessStaffIsSystemAdmin(member)) return false;
    return true;
  });
}

function _accessStaffRowHtml(member) {
  const left = _accessStaffIsLeft(member);
  const accountInvalid = _accessStaffAccountInvalid(member);
  const account = _accessStaffValidAccount(member);
  const primaryRole = _accessPrimaryRole(member);
  const secondaryRoles = _accessRoleKeys(member)
    .filter(role => role !== primaryRole)
    .map(_accessRoleLabel);
  const accountText = accountInvalid ? "账号数据异常" : !account ? "未开通账号"
    : (account.is_active ? "账号正常" : "账号已停用");
  const systemAdminText = `系统管理员：${_accessStaffIsSystemAdmin(member) ? "是" : "否"}`;
  const edit = _accessStaffCanEdit() && !left
    ? `<button type="button" class="plain-button access-staff-edit" onclick="openStaffForm('${escapeAttr(member.staff_id)}')">编辑</button>`
    : "";
  return `
    <div class="access-staff-row${left ? " is-left" : ""}" data-staff-id="${escapeAttr(member.staff_id)}">
      <button type="button" class="access-staff-row-main" onclick="openStaffDetails('${escapeAttr(member.staff_id)}')">
        <span class="access-staff-name">${escapeHtml(member.name || "未命名")}</span>
        <span class="access-staff-role">
          <span class="access-staff-role-tag is-primary">主岗位：${escapeHtml(_accessRoleLabel(primaryRole))}</span>
          <span class="access-staff-role-tag is-secondary">兼任岗位：${escapeHtml(secondaryRoles.join("、") || "无")}</span>
        </span>
        <span class="access-staff-meta">${escapeHtml([member.job_no && `工号 ${member.job_no}`, member.department, member.title].filter(Boolean).join(" · "))}</span>
        <span class="access-staff-sensitive">手机 ${escapeHtml(_accessMask(member.phone))} · 身份证 ${escapeHtml(_accessMask(member.id_card))} · 执业证 ${escapeHtml(_accessMask(member.license_no))}</span>
        <span class="access-staff-account"><span>${escapeHtml(accountText)}</span><span class="access-staff-admin-state">${systemAdminText}</span></span>
        <span class="access-staff-employment">${left ? "离职" : "在职"}</span>
      </button>
      ${edit}
    </div>`;
}

function _accessStaffGroupsHtml() {
  const buckets = {};
  _accessStaffFilteredMembers().forEach(member => { (buckets[_accessPrimaryRole(member) || "（未设岗位）"] ||= []).push(member); });
  const orderedRoles = ACCESS_STAFF_ROLE_ORDER.filter(role => buckets[role])
    .concat(Object.keys(buckets).filter(role => !ACCESS_STAFF_ROLE_ORDER.includes(role)).sort());
  const groups = orderedRoles.map(role => {
    buckets[role].sort((a, b) => Number(_accessStaffIsLeft(a)) - Number(_accessStaffIsLeft(b))
      || String(a.name || "").localeCompare(String(b.name || ""), "zh-CN"));
    return `<section class="access-staff-group"><h3 class="access-staff-group-title">${escapeHtml(_accessRoleLabel(role))}（${buckets[role].length}）</h3>${buckets[role].map(_accessStaffRowHtml).join("")}</section>`;
  }).join("");
  return groups || '<div class="access-staff-empty">没有符合条件的人员</div>';
}

function _accessStaffOverviewHtml() {
  const all = _accessStaffState.members;
  const counts = [
    ["人员总数", all.length],
    ["在职", all.filter(member => !_accessStaffIsLeft(member)).length],
    ["离职", all.filter(_accessStaffIsLeft).length],
    ["已开通账号", all.filter(member => Boolean(_accessStaffValidAccount(member))).length],
    ["系统管理员", all.filter(_accessStaffIsSystemAdmin).length],
  ];
  return counts.map(([label, count]) => `<div class="access-center-overview-card"><span>${label}</span><strong>${count}</strong></div>`).join("");
}

function _accessStaffRoleOptionsHtml() {
  const keys = [...new Set(_accessStaffState.members.flatMap(_accessRoleKeys))];
  keys.sort((a, b) => {
    const ai = ACCESS_STAFF_ROLE_ORDER.indexOf(a); const bi = ACCESS_STAFF_ROLE_ORDER.indexOf(b);
    return (ai < 0 ? 999 : ai) - (bi < 0 ? 999 : bi) || _accessRoleLabel(a).localeCompare(_accessRoleLabel(b), "zh-CN");
  });
  return ['<option value="">全部岗位</option>'].concat(keys.map(role =>
    `<option value="${escapeAttr(role)}"${_accessStaffState.roleFilter === role ? " selected" : ""}>${escapeHtml(_accessRoleLabel(role))}</option>`
  )).join("");
}

function renderStaffManager() {
  const box = document.getElementById(_smTarget);
  if (!box) return;
  _accessStaffRememberScroll();
  if (_accessStaffState.loadState === "loading") {
    box.innerHTML = '<div class="access-center-loading">正在加载人员数据…</div>';
    return;
  }
  if (_accessStaffState.loadState === "error") {
    box.innerHTML = `<div class="access-center-error">${escapeHtml(_accessStaffState.error)}</div>`;
    return;
  }
  const addButton = _accessStaffCanEdit()
    ? '<button type="button" class="tooth-confirm-btn access-staff-add" onclick="openStaffForm(null)">新增员工</button>'
    : "";
  box.innerHTML = `
    <section class="access-center-staff">
      <div class="access-center-overview">${_accessStaffOverviewHtml()}</div>
      <div class="access-staff-toolbar">
        <input class="ord-input access-staff-search" value="${escapeAttr(_accessStaffState.query)}" placeholder="搜姓名、拼音首字母、用户名、工号或手机" oninput="accessStaffSetFilter('query',this.value)">
        <select class="ord-input access-staff-role-filter" onchange="accessStaffSetFilter('roleFilter',this.value)">${_accessStaffRoleOptionsHtml()}</select>
        <select class="ord-input access-staff-employment-filter" onchange="accessStaffSetFilter('employmentFilter',this.value)">
          <option value="">全部在职状态</option><option value="employed"${_accessStaffState.employmentFilter === "employed" ? " selected" : ""}>在职</option><option value="left"${_accessStaffState.employmentFilter === "left" ? " selected" : ""}>离职</option>
        </select>
        <select class="ord-input access-staff-account-filter" onchange="accessStaffSetFilter('accountFilter',this.value)">
          <option value="">全部账号状态</option><option value="none"${_accessStaffState.accountFilter === "none" ? " selected" : ""}>未开通</option><option value="opened"${_accessStaffState.accountFilter === "opened" ? " selected" : ""}>已开通</option><option value="enabled"${_accessStaffState.accountFilter === "enabled" ? " selected" : ""}>账号正常</option><option value="disabled"${_accessStaffState.accountFilter === "disabled" ? " selected" : ""}>账号已停用</option><option value="admin"${_accessStaffState.accountFilter === "admin" ? " selected" : ""}>系统管理员</option>
        </select>
        ${addButton}
      </div>
      <div class="access-staff-list-scroll">${_accessStaffGroupsHtml()}</div>
      ${_accessStaffState.formOpen ? _accessStaffFormHtml() : ""}
      ${_accessStaffState.selectedStaffId ? _accessStaffDetailsHtml() : ""}
    </section>`;
  _accessStaffRestoreScroll();
}

function accessStaffSetFilter(field, value) {
  if (!["query", "roleFilter", "employmentFilter", "accountFilter"].includes(field)) return;
  _accessStaffRememberScroll();
  _accessStaffState[field] = String(value || "");
  const box = document.getElementById(_smTarget);
  const list = box && box.querySelector(".access-staff-list-scroll");
  if (!list) { renderStaffManager(); return; }
  list.innerHTML = _accessStaffGroupsHtml();
  _accessStaffRestoreScroll();
}

function _accessStaffFormHtml() {
  const member = _accessStaffState.formStaffId
    ? _accessStaffState.members.find(item => item.staff_id === _accessStaffState.formStaffId) : null;
  const primary = member ? _accessRoleKey(_accessPrimaryRole(member)) : "";
  const roles = member ? _accessRoleKeys(member).map(_accessRoleKey) : [];
  const value = field => escapeAttr(member && member[field] || "");
  const roleOptions = ['<option value="">请选主岗位</option>'].concat(ACCESS_STAFF_ROLES.map(([key, label]) =>
    `<option value="${key}"${primary === key ? " selected" : ""}>${label}</option>`
  )).join("");
  const roleChecks = ACCESS_STAFF_ROLES.map(([key, label]) =>
    `<label class="access-staff-role-check"><input type="checkbox" class="access-staff-secondary-role" value="${key}"${roles.includes(key) && key !== primary ? " checked" : ""}>${label}</label>`
  ).join("");
  const sexOptions = ["", "男", "女"].map(sex => `<option value="${sex}"${member && (member.sex || "") === sex ? " selected" : ""}>${sex || "请选性别"}</option>`).join("");
  return `
    <div class="access-center-modal-backdrop" role="presentation">
      <section class="access-center-modal" role="dialog" aria-modal="true" aria-labelledby="accessStaffFormTitle">
        <header class="access-center-modal-head"><h2 id="accessStaffFormTitle">${member ? "编辑人员" : "新增员工"}</h2><button id="accessStaffFormClose" type="button" class="plain-button" onclick="closeStaffForm()"${_accessStaffState.formPending ? " disabled" : ""}>关闭</button></header>
        <div class="access-staff-form-grid">
          <label>姓名 *<input id="accessStaffName" class="ord-input" value="${value("name")}"></label>
          <label>主岗位 *<select id="accessStaffPrimaryRole" class="ord-input">${roleOptions}</select></label>
          <fieldset class="access-staff-secondary-roles"><legend>兼任岗位</legend>${roleChecks}</fieldset>
          <label>性别<select id="accessStaffSex" class="ord-input">${sexOptions}</select></label>
          <label>工号<input id="accessStaffJobNo" class="ord-input" value="${value("job_no")}"></label>
          <label>手机<input id="accessStaffPhone" class="ord-input" value="${value("phone")}"></label>
          <label>职称<input id="accessStaffTitle" class="ord-input" value="${value("title")}"></label>
          <label>科室<input id="accessStaffDepartment" class="ord-input" value="${value("department")}"></label>
          <label>执业证号<input id="accessStaffLicenseNo" class="ord-input" value="${value("license_no")}"></label>
          <label>身份证号<input id="accessStaffIdCard" class="ord-input" value="${value("id_card")}"></label>
          <label class="access-staff-form-note">备注<textarea id="accessStaffNote" class="ord-input">${escapeHtml(member && member.note || "")}</textarea></label>
        </div>
        <div id="accessStaffFormError" class="access-center-form-error">${escapeHtml(_accessStaffState.formError)}</div>
        <footer class="access-center-modal-actions"><button id="accessStaffFormCancel" type="button" class="plain-button" onclick="closeStaffForm()"${_accessStaffState.formPending ? " disabled" : ""}>取消</button><button id="accessStaffFormSave" type="button" class="tooth-confirm-btn" onclick="saveStaffForm()"${_accessStaffState.formPending ? " disabled" : ""}>保存</button></footer>
      </section>
    </div>
  `;
}

function openStaffForm(staffId) {
  if (!_accessStaffCanEdit()) return;
  if (staffId) {
    const member = _accessStaffState.members.find(item => item.staff_id === staffId);
    if (!member || _accessStaffIsLeft(member)) return;
  }
  _accessStaffRememberScroll();
  _accessStaffState.formOpen = true;
  _accessStaffState.formStaffId = staffId || null;
  _accessStaffState.formError = "";
  _accessStaffState.formPending = false;
  _accessStaffState.formRevision += 1;
  _smEditId = staffId || null;
  renderStaffFormModal();
}

function openNewStaffForm() {
  if (_accessStaffState.loadState !== "ready" || !_accessStaffCanEdit()) return false;
  openStaffForm(null);
  return _accessStaffState.formOpen === true;
}

function renderStaffFormModal() {
  renderStaffManager();
}

function closeStaffForm() {
  _accessStaffState.formOpen = false;
  _accessStaffState.formStaffId = null;
  _accessStaffState.formError = "";
  _accessStaffState.formPending = false;
  _accessStaffState.formRevision += 1;
  _smEditId = null;
  renderStaffManager();
}

function _accessFormValue(id) {
  return String((document.getElementById(id) || {}).value || "").trim();
}

function _accessStaffSetFormPending(pending) {
  _accessStaffState.formPending = pending === true;
  ["accessStaffFormClose", "accessStaffFormCancel", "accessStaffFormSave"].forEach(id => {
    const button = document.getElementById(id);
    if (button) button.disabled = _accessStaffState.formPending;
  });
}

async function saveStaffForm() {
  if (!_accessStaffState.formOpen || !_accessStaffCanEdit() || _accessStaffState.formPending) return;
  const formRevision = _accessStaffState.formRevision;
  const isCurrent = () => _accessStaffState.formRevision === formRevision;
  const name = _accessFormValue("accessStaffName");
  const primaryRole = _accessFormValue("accessStaffPrimaryRole");
  const status = document.getElementById("accessStaffFormError");
  const setError = message => {
    if (!isCurrent()) return;
    _accessStaffState.formError = message;
    if (status) status.textContent = message;
  };
  if (!name) { setError("请填写姓名"); return; }
  if (!primaryRole) { setError("请选择主岗位"); return; }
  const secondaryRoles = [...document.querySelectorAll('.access-staff-secondary-role:checked')].map(input => input.value);
  const roleKeys = [...new Set([primaryRole, ...secondaryRoles.filter(Boolean)])];
  const profile = {
    sex: _accessFormValue("accessStaffSex"), job_no: _accessFormValue("accessStaffJobNo"),
    phone: _accessFormValue("accessStaffPhone"), title: _accessFormValue("accessStaffTitle"),
    department: _accessFormValue("accessStaffDepartment"), license_no: _accessFormValue("accessStaffLicenseNo"),
    id_card: _accessFormValue("accessStaffIdCard"), note: _accessFormValue("accessStaffNote"),
  };
  const payload = window.__accessV3 === true
    ? {name, primary_role: primaryRole, role_keys: roleKeys, ...profile}
    : {name, role: _accessLegacyRole(primaryRole), roles: [...new Set(roleKeys.map(_accessLegacyRole))], ...profile};
  const editing = _accessStaffState.formStaffId;
  const url = editing ? `/api/staff-members/${encodeURIComponent(editing)}` : "/api/staff-members";
  const method = editing ? "PUT" : "POST";
  _accessStaffSetFormPending(true);
  setError("正在保存…");
  let res;
  try {
    res = await fetch(url, {method, headers: {"Content-Type": "application/json"}, body: JSON.stringify(payload)});
  } catch {
    if (!isCurrent()) return;
    _accessStaffSetFormPending(false);
    setError("保存失败，请检查网络后重试");
    return;
  }
  if (!isCurrent()) return;
  if (!res.ok) {
    _accessStaffSetFormPending(false);
    setError("保存失败，请检查填写内容后重试");
    return;
  }
  _accessStaffSetFormPending(false);
  _accessStaffState.formOpen = false;
  _accessStaffState.formStaffId = null;
  _accessStaffState.formError = "";
  _accessStaffState.formRevision += 1;
  const refreshRevision = _accessStaffState.formRevision;
  _smEditId = null;
  await renderStaffInto(_smTarget);
  if (_accessStaffState.formRevision !== refreshRevision) return;
  await ensureStaff(true);
  if (_accessStaffState.formRevision !== refreshRevision) return;
  refreshOrderTeamSelects();
}

function addStaff() {
  if (_accessStaffState.formOpen) return saveStaffForm();
  openStaffForm(null);
}
function editStaff(id) { openStaffForm(id); }
function cancelEditStaff() { closeStaffForm(); }

function _accessStaffActionButtonsHtml(member, group) {
  const staffId = escapeAttr(member.staff_id);
  const accountInvalid = _accessStaffAccountInvalid(member);
  const account = _accessStaffValidAccount(member);
  const left = _accessStaffIsLeft(member);
  const buttons = [];
  if (group === "employment" && _accessStaffHasPerm("staff.employment")) {
    buttons.push(left
      ? `<button type="button" class="plain-button" onclick="beginStaffReinstate('${staffId}')">复职</button>`
      : `<button type="button" class="plain-button" onclick="beginStaffDeparture('${staffId}')">离职</button>`);
  }
  if (accountInvalid) return buttons.join("");
  if (group === "account" && _accessStaffHasPerm("account.open") && !left && _accessStaffCanManageAccount(member)) {
    if (!account) {
      buttons.push(`<button type="button" class="plain-button" onclick="beginStaffAccountOpen('${staffId}')">开户</button>`);
    } else {
      buttons.push(`<button type="button" class="plain-button" onclick="toggleStaffAccount('${staffId}', ${account.is_active ? "false" : "true"})">${account.is_active ? "停用" : "启用"}</button>`);
    }
  }
  if (group === "account" && _accessStaffHasPerm("account.security") && account && _accessStaffCanManageAccount(member)) {
    buttons.push(`<button type="button" class="plain-button" onclick="beginStaffPasswordReset('${staffId}')">重置密码</button>`);
  }
  if (group === "systemAdmin" && window.__isSystemAdmin === true && account) {
    if (_accessStaffIsSystemAdmin(member)) {
      buttons.push(`<button type="button" class="plain-button" onclick="beginSystemAdminChange('${staffId}', false)">撤销系统管理员</button>`);
    } else if (account.is_active) {
      buttons.push(`<button type="button" class="plain-button" onclick="beginSystemAdminChange('${staffId}', true)">授予系统管理员</button>`);
    }
  }
  return buttons.join("");
}

function _accessStaffActionModalHtml(member) {
  const action = _accessStaffState.action;
  if (!action || action.staffId !== member.staff_id) return "";
  let title = "";
  let detail = "";
  let confirm = "";
  if (action.kind === "departure") {
    title = "确认人员离职";
    detail = "<p>离职后将：</p><ul><li>业务选人中移除</li><li>账号停用</li><li>现有登录退出</li><li>档案和历史记录保留</li></ul>";
    confirm = "confirmStaffDeparture()";
  } else if (action.kind === "reinstate") {
    title = "确认人员复职";
    detail = "<p>复职只恢复在职状态，登录账号仍保持停用。</p>";
    confirm = "confirmStaffReinstate()";
  } else if (action.kind === "systemAdmin") {
    title = action.grant ? "授予系统管理员" : "撤销系统管理员";
    detail = `<p>请说明${action.grant ? "授予" : "撤销"}原因。</p>`;
    confirm = "confirmSystemAdminChange()";
  } else {
    return "";
  }
  return `
    <div class="access-center-modal-backdrop" role="presentation">
      <section class="access-center-modal" role="dialog" aria-modal="true" aria-label="${escapeAttr(title)}">
        <header class="access-center-modal-head"><h2>${escapeHtml(title)}</h2><button type="button" class="plain-button" onclick="cancelStaffAction()">关闭</button></header>
        <div class="access-center-modal-body">
          ${detail}
          <label>原因 *<textarea id="accessStaffActionReason" class="ord-input"></textarea></label>
          <div id="accessStaffActionError" class="access-center-form-error">${escapeHtml(_accessStaffState.actionError)}</div>
        </div>
        <footer class="access-center-modal-actions"><button type="button" class="plain-button" onclick="cancelStaffAction()">取消</button><button id="accessStaffActionConfirm" type="button" class="tooth-confirm-btn" onclick="${confirm}"${_accessStaffState.actionPending ? " disabled" : ""}>确认</button></footer>
      </section>
    </div>`;
}

function _accessStaffDetailsHtml() {
  const member = _accessStaffState.members.find(item => item.staff_id === _accessStaffState.selectedStaffId);
  if (!member) return "";
  const primary = _accessPrimaryRole(member);
  const otherRoles = _accessRoleKeys(member).filter(role => role !== primary).map(_accessRoleLabel).join("、") || "无";
  const accountInvalid = _accessStaffAccountInvalid(member);
  const account = _accessStaffValidAccount(member);
  const left = _accessStaffIsLeft(member);
  const edit = _accessStaffCanEdit() && !left
    ? `<button type="button" class="plain-button access-sheet-edit" onclick="openStaffForm('${escapeAttr(member.staff_id)}')">编辑</button>` : "";
  const employmentActions = _accessStaffActionButtonsHtml(member, "employment");
  const accountActions = _accessStaffActionButtonsHtml(member, "account");
  const systemAdminActions = _accessStaffActionButtonsHtml(member, "systemAdmin");
  return `
    <div class="access-sheet-backdrop" onclick="closeStaffDetails()"></div>
    <aside class="access-sheet-panel" aria-label="人员详情">
      <header class="access-sheet-head"><div><h2>${escapeHtml(member.name || "未命名")}</h2><span>${left ? "离职" : "在职"}</span></div><div>${edit}<button type="button" class="plain-button" onclick="closeStaffDetails()">关闭</button></div></header>
      <section class="access-sheet-section"><h3>人员资料</h3><p>工号：${escapeHtml(member.job_no || "未填")}</p><p>性别：${escapeHtml(member.sex || "未填")}</p><p>手机：${escapeHtml(_accessMask(member.phone))}</p><p>职称：${escapeHtml(member.title || "未填")}</p><p>科室：${escapeHtml(member.department || "未填")}</p><p>执业证号：${escapeHtml(_accessMask(member.license_no))}</p><p>身份证号：${escapeHtml(_accessMask(member.id_card))}</p><p>备注：${escapeHtml(member.note || "无")}</p></section>
      <section class="access-sheet-section"><h3>岗位与日常权限</h3><p>主岗位：${escapeHtml(_accessRoleLabel(primary))}</p><p>兼任岗位：${escapeHtml(otherRoles)}</p><p>功能权限取所有岗位权限的并集。</p><p>在职状态：${left ? "已离职" : "在职"}</p>${employmentActions ? `<div class="access-sheet-actions">${employmentActions}</div>` : ""}</section>
      <section class="access-sheet-section"><h3>登录账号</h3><p>${accountInvalid ? "账号数据异常" : account ? `用户名：${escapeHtml(account.username || "未设")}</p><p>账号状态：${account.is_active ? "正常" : "已停用"}` : "未开通登录账号"}</p>${accountActions ? `<div class="access-sheet-actions">${accountActions}</div>` : ""}</section>
      <section class="access-sheet-section"><h3>系统管理员</h3><p>${_accessStaffIsSystemAdmin(member) ? "是" : "否"}</p><p class="cfg-hint access-sheet-risk">系统管理员相当于诊所总钥匙，可使用全部功能和高风险管理能力；授予或撤销都需要验证当前密码并填写原因。</p>${systemAdminActions ? `<div class="access-sheet-actions">${systemAdminActions}</div>` : ""}</section>
    </aside>
    ${_accessStaffActionModalHtml(member)}`;
}

function openStaffDetails(staffId) {
  if (!_accessStaffState.members.some(member => member.staff_id === staffId)) return;
  _accessStaffRememberScroll();
  _accessStaffState.action = null;
  _accessStaffState.actionError = "";
  _accessStaffState.selectedStaffId = staffId;
  renderStaffDetails();
}

function renderStaffDetails() {
  renderStaffManager();
}

function closeStaffDetails() {
  _accessStaffRememberScroll();
  _accessStaffState.action = null;
  _accessStaffState.actionError = "";
  _accessStaffState.selectedStaffId = null;
  renderStaffManager();
}

// 兼容旧调用方；V3 人员中心从 beginStaffAccountOpen 入口做权限和状态判断。
async function openStaffAccount(staffId, jobNo, role) {
  const username = await appPrompt("登录用户名（默认工号）", jobNo || "");
  if (username === null || !username.trim()) return false;
  const password = await appPrompt("初始密码（员工可后续自助改）", "", {
    type: "password", autocomplete: "new-password",
  });
  if (password === null) return false;
  if (!password.trim()) { window.alert("密码不能为空"); return false; }
  const payload = {username: username.trim(), password};
  if (window.__accessV3 !== true) {
    payload.role = ROLE_TO_ACCT[role] || "reception";
  }
  let res;
  try {
    res = await fetch(`/api/staff-members/${encodeURIComponent(staffId)}/account`, {
      method: "POST", headers: {"Content-Type": "application/json"},
      body: JSON.stringify(payload),
    });
  } catch { window.alert(_accessStaffActionError("开户", null)); return false; }
  if (!res.ok) { window.alert(_accessStaffActionError("开户", res.status)); return false; }
  await ensureStaffAccts();
  return true;
}

function _accessStaffActionError(action, status) {
  if (status === null) return `${action}失败：网络异常，请稍后重试`;
  if (status === 400) return `${action}失败：请求内容不正确`;
  if (status === 403) return `${action}失败：权限不足或需要重新验证`;
  if (status === 409) return `${action}失败：当前状态不允许此操作`;
  if (status >= 500) return `${action}失败：服务异常，请稍后重试`;
  return `${action}失败，请稍后重试`;
}

function _accessStaffFind(staffId) {
  return _accessStaffState.members.find(member => member.staff_id === staffId) || null;
}

function _accessStaffSetActionError(message) {
  _accessStaffState.actionError = message;
  const status = document.getElementById("accessStaffActionError");
  if (status) status.textContent = message;
}

function _accessStaffSetActionPending(pending) {
  _accessStaffState.actionPending = pending === true;
  const confirm = document.getElementById("accessStaffActionConfirm");
  if (confirm) confirm.disabled = _accessStaffState.actionPending;
}

function _accessStaffActionReason() {
  return String((document.getElementById("accessStaffActionReason") || {}).value || "").trim();
}

async function _accessStaffRequest(url, options, action) {
  let res;
  try { res = await fetch(url, options); }
  catch { return {ok: false, error: _accessStaffActionError(action, null)}; }
  if (!res.ok) return {ok: false, error: _accessStaffActionError(action, res.status)};
  return {ok: true};
}

async function _accessStaffRefresh(refreshSelectionCache) {
  await renderStaffInto(_smTarget);
  if (!refreshSelectionCache) return;
  await ensureStaff(true);
  refreshOrderTeamSelects();
}

function _accessStaffBeginReasonAction(kind, staffId, grant) {
  if (_accessStaffState.actionPending) return;
  _accessStaffState.selectedStaffId = staffId;
  _accessStaffState.action = {kind, staffId, grant: grant === true};
  _accessStaffState.actionError = "";
  renderStaffDetails();
}

function cancelStaffAction() {
  if (_accessStaffState.actionPending) return;
  _accessStaffState.action = null;
  _accessStaffState.actionError = "";
  renderStaffDetails();
}

function beginStaffDeparture(staffId) {
  const member = _accessStaffFind(staffId);
  if (!_accessStaffHasPerm("staff.employment") || !member || _accessStaffIsLeft(member)) return;
  _accessStaffBeginReasonAction("departure", staffId);
}

async function confirmStaffDeparture() {
  if (_accessStaffState.actionPending) return;
  const action = _accessStaffState.action;
  const member = action && _accessStaffFind(action.staffId);
  if (!action || action.kind !== "departure" || !_accessStaffHasPerm("staff.employment")
      || !member || _accessStaffIsLeft(member)) return;
  const reason = _accessStaffActionReason();
  if (!reason) { _accessStaffSetActionError("原因不能为空"); return; }
  _accessStaffSetActionPending(true);
  const result = await _accessStaffRequest(`/api/staff-members/${encodeURIComponent(action.staffId)}`, {
    method: "DELETE", headers: {"Content-Type": "application/json"}, body: JSON.stringify({reason}),
  }, "离职");
  if (!result.ok) {
    _accessStaffSetActionPending(false);
    _accessStaffSetActionError(result.error);
    return;
  }
  _accessStaffState.action = null;
  _accessStaffState.actionError = "";
  _accessStaffSetActionPending(false);
  await _accessStaffRefresh(true);
}

function beginStaffReinstate(staffId) {
  const member = _accessStaffFind(staffId);
  if (!_accessStaffHasPerm("staff.employment") || !member || !_accessStaffIsLeft(member)) return;
  _accessStaffBeginReasonAction("reinstate", staffId);
}

async function confirmStaffReinstate() {
  if (_accessStaffState.actionPending) return;
  const action = _accessStaffState.action;
  const member = action && _accessStaffFind(action.staffId);
  if (!action || action.kind !== "reinstate" || !_accessStaffHasPerm("staff.employment")
      || !member || !_accessStaffIsLeft(member)) return;
  const reason = _accessStaffActionReason();
  if (!reason) { _accessStaffSetActionError("原因不能为空"); return; }
  _accessStaffSetActionPending(true);
  const result = await _accessStaffRequest(`/api/staff-members/${encodeURIComponent(action.staffId)}/reinstate`, {
    method: "POST", headers: {"Content-Type": "application/json"}, body: JSON.stringify({reason}),
  }, "复职");
  if (!result.ok) {
    _accessStaffSetActionPending(false);
    _accessStaffSetActionError(result.error);
    return;
  }
  _accessStaffState.action = null;
  _accessStaffState.actionError = "";
  _accessStaffSetActionPending(false);
  await _accessStaffRefresh(true);
  window.alert("人员已复职，账号仍停用");
}

async function beginStaffAccountOpen(staffId) {
  const member = _accessStaffFind(staffId);
  if (!_accessStaffHasPerm("account.open") || !member || _accessStaffIsLeft(member)
      || _accessStaffAccountInvalid(member) || _accessStaffValidAccount(member)) return false;
  return openStaffAccount(member.staff_id, member.job_no || "", _accessPrimaryRole(member));
}

async function beginStaffPasswordReset(staffId) {
  const member = _accessStaffFind(staffId);
  const account = _accessStaffValidAccount(member);
  if (!_accessStaffHasPerm("account.security") || !account || !_accessStaffCanManageAccount(member)) return false;
  const password = await appPrompt(`给「${account.username || "该账号"}」设置新密码：`, "", {
    type: "password", autocomplete: "new-password",
  });
  if (password === null) return false;
  if (!password.trim()) { window.alert("密码不能为空"); return false; }
  const result = await _accessStaffRequest(`/api/users/${encodeURIComponent(account.user_id)}/reset-password`, {
    method: "POST", headers: {"Content-Type": "application/json"}, body: JSON.stringify({password}),
  }, "重置密码");
  if (!result.ok) { window.alert(result.error); return false; }
  window.alert("密码已重置");
  return true;
}

async function toggleStaffAccount(staffId, active) {
  const member = _accessStaffFind(staffId);
  const account = _accessStaffValidAccount(member);
  const desired = active === true;
  if (!_accessStaffHasPerm("account.open") || !account || !_accessStaffCanManageAccount(member)
      || (desired && _accessStaffIsLeft(member))) return false;
  if (Boolean(account.is_active) === desired) return true;
  const result = await _accessStaffRequest(`/api/users/${encodeURIComponent(account.user_id)}`, {
    method: "PUT", headers: {"Content-Type": "application/json"}, body: JSON.stringify({is_active: desired}),
  }, desired ? "启用账号" : "停用账号");
  if (!result.ok) { window.alert(result.error); return false; }
  await _accessStaffRefresh(false);
  return true;
}

function beginSystemAdminChange(staffId, grant) {
  const member = _accessStaffFind(staffId);
  const account = _accessStaffValidAccount(member);
  const desired = grant === true;
  if (window.__isSystemAdmin !== true || !account) return;
  if (desired && !account.is_active) return;
  if (desired && _accessStaffIsSystemAdmin(member)) return;
  if (!desired && !_accessStaffIsSystemAdmin(member)) return;
  _accessStaffBeginReasonAction("systemAdmin", staffId, desired);
}

async function confirmSystemAdminChange() {
  if (_accessStaffState.actionPending) return;
  const action = _accessStaffState.action;
  const member = action && _accessStaffFind(action.staffId);
  const account = _accessStaffValidAccount(member);
  if (!action || action.kind !== "systemAdmin" || window.__isSystemAdmin !== true || !account) return;
  const reason = _accessStaffActionReason();
  if (!reason) { _accessStaffSetActionError("原因不能为空"); return; }
  const verb = action.grant ? "grant" : "revoke";
  _accessStaffSetActionPending(true);
  const result = await _accessStaffRequest(`/api/users/${encodeURIComponent(account.user_id)}/system-admin/${verb}`, {
    method: "POST", headers: {"Content-Type": "application/json"}, body: JSON.stringify({reason}),
  }, action.grant ? "授予系统管理员" : "撤销系统管理员");
  if (!result.ok) {
    _accessStaffSetActionPending(false);
    _accessStaffSetActionError(result.error);
    return;
  }
  _accessStaffState.action = null;
  _accessStaffState.actionError = "";
  _accessStaffSetActionPending(false);
  await _accessStaffRefresh(false);
}

async function deleteStaff(staffId) {
  if (window.__accessV3 === true) {
    beginStaffDeparture(staffId);
    return;
  }
  if (!window.confirm("删除这名人员？(历史处置里的配台姓名不受影响)")) {
    return;
  }
  try {
    const res = await fetch(`/api/staff-members/${encodeURIComponent(staffId)}`, {method: "DELETE"});
    if (!res.ok) return;
  } catch { return; }
  if (staffId === _smEditId) _smEditId = null;   // #231 删的是当前编辑对象→立即退出编辑态
  await ensureStaff(true);
  await ensureStaffAccts();   // 删员工级联停用了账号，刷新账号状态
  refreshOrderTeamSelects();
}

// 处置编辑器若开着，重渲配台下拉以反映人员变动（由 treatment_order_editor.js 实现）
function refreshOrderTeamSelects() {
  if (typeof renderTreatmentOrderEditor === "function" && typeof orderModel !== "undefined" && orderModel) {
    if (typeof orderSyncFromDom === "function") orderSyncFromDom();
    renderTreatmentOrderEditor();
  }
}

Object.assign(window, {
  ensureStaff, staffOptionsHtml, bindStaffInputs, rememberTeam, lastTeam, activeTeam,
  openStaffManager, closeStaffManager, renderStaffInto, staffPinyinInitials,
  openStaffForm, openNewStaffForm, renderStaffFormModal, saveStaffForm, closeStaffForm,
  openStaffDetails, renderStaffDetails, closeStaffDetails, accessStaffSetFilter,
  addStaff, deleteStaff, editStaff, cancelEditStaff, openStaffAccount,
  beginStaffDeparture, confirmStaffDeparture, beginStaffReinstate, confirmStaffReinstate,
  beginStaffAccountOpen, beginStaffPasswordReset, toggleStaffAccount,
  beginSystemAdminChange, confirmSystemAdminChange, cancelStaffAction,
});
