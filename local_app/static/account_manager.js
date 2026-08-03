// 账号管理（管理员）：用户列表 + 新增 + 重置密码 + 停用/启用 + 改角色；任意用户自助改密。
// modal 动态建到 body，避免改 index.html 结构。

// 与后端预置角色(auth.PRESET_ROLES)对齐；权限由「角色权限」矩阵页配置，这里只选身份。
// #425:按权限树蓝本补齐(增 主任/收银员/客服/助理2)。#502:去「技师」,旧账号迁 assistant。
const AM_ROLES = [["admin", "管理员"], ["director", "主任"], ["doctor", "医生"], ["nurse", "护士"],
                  ["consultant", "咨询师"], ["reception", "前台"], ["cashier", "收银员"],
                  ["assistant", "助理"], ["assistant2", "助理2"], ["support", "客服"]];
let _amVisibleUsers = [];

function amCanCreateStaff() {
  return Boolean(window.__accessV3 === true
    && typeof hasPerm === "function"
    && hasPerm("staff.edit"));
}

function amEnsureModal() {
  let m = document.getElementById("accountManagerModal");
  if (m) return m;
  m = document.createElement("div");
  m.id = "accountManagerModal";
  m.className = "modal-backdrop";
  m.hidden = true;
  m.innerHTML = `
    <section class="appt-modal am-modal" role="dialog" aria-modal="true" aria-label="账号管理">
      <div class="modal-head"><strong>账号管理</strong>
        <span class="am-head-right">
          ${amCanCreateStaff() ? '<button type="button" class="plain-button" onclick="amOpenNewStaff()">＋新增员工</button>' : ""}
          <button type="button" class="plain-button" onclick="amClose()">×</button>
        </span></div>
      <div class="am-body" id="amBody">加载中...</div>
    </section>`;
  document.body.appendChild(m);
  return m;
}

function amClose() {
  const m = document.getElementById("accountManagerModal");
  if (m) m.hidden = true;
}

async function amOpenNewStaff() {
  if (!amCanCreateStaff()) return false;
  const bridge = window.openNewStaffFromConfig;
  if (typeof bridge !== "function") {
    amSetMsg("无法打开新增员工，请稍后重试");
    return false;
  }
  let result;
  try { result = await bridge(); }
  catch {
    amSetMsg("无法打开新增员工，请稍后重试");
    return false;
  }
  if (result === true) {
    amClose();
    return true;
  }
  if (result === null) return null;
  amSetMsg("无法打开新增员工，请稍后重试");
  return false;
}

async function openAccountManager() {
  const m = amEnsureModal();
  m.hidden = false;
  await amRender();
}

function amRoleOptions(selected) {
  return AM_ROLES.map(([v, label]) => `<option value="${v}"${v === selected ? " selected" : ""}>${label}</option>`).join("");
}

function amRoleText(user) {
  if (user && user.is_system_admin === true) return "系统管理员";
  const fallback = Object.fromEntries(AM_ROLES);
  const labels = typeof _ROLE_LABELS === "object" ? _ROLE_LABELS : fallback;
  const keys = user && Array.isArray(user.role_keys)
    ? user.role_keys
    : (user && user.role ? [user.role] : []);
  const text = keys.filter(Boolean).map(key => labels[key] || fallback[key] || key).join(" / ");
  if (text) return text;
  if (user && user.account_kind === "independent_admin") return "独立管理员";
  if (user && user.account_kind === "break_glass") return "应急管理员";
  return "未分配岗位";
}

function accountManagerVisibleUsers(users) {
  if (window.__accessV3 !== true) return users;
  const directPerms = window.__userPerms;
  const canViewStaff = Boolean(
    directPerms && typeof directPerms.has === "function" && directPerms.has("staff.view")
  );
  return canViewStaff ? users.filter(user => user.account_kind !== "staff") : users;
}

function amActionError(action, status) {
  if (status === null) return `${action}失败：网络异常，请稍后重试`;
  if (status === 400) return `${action}失败：请求内容不正确`;
  if (status === 403) return `${action}失败：权限不足或需要重新验证`;
  if (status === 409) return `${action}失败：当前状态不允许此操作`;
  if (status >= 500) return `${action}失败：服务异常，请稍后重试`;
  return `${action}失败，请稍后重试`;
}

function amCanManageUser(user) {
  return Boolean(user) && (user.is_system_admin !== true || window.__isSystemAdmin === true);
}

function amCanManageUserId(id) {
  return amCanManageUser(_amVisibleUsers.find(user => user.id === id));
}

function amUsersFromPayload(payload) {
  if (!payload || typeof payload !== "object" || !Array.isArray(payload.users)) return null;
  const valid = payload.users.every(user => user && typeof user === "object"
    && typeof user.id === "string" && Boolean(user.id.trim())
    && typeof user.username === "string" && Boolean(user.username.trim())
    && typeof user.is_active === "boolean");
  return valid ? payload.users : null;
}

async function amRender() {
  const body = document.getElementById("amBody");
  if (!body) return;
  _amVisibleUsers = [];
  let users = [];
  let res;
  try { res = await fetch("/api/users"); }
  catch { body.innerHTML = amActionError("加载", null); return; }
  if (!res.ok) { body.innerHTML = amActionError("加载", res.status); return; }
  try {
    const payload = await res.json();
    const parsedUsers = amUsersFromPayload(payload);
    if (!parsedUsers) { body.innerHTML = "账号数据加载失败"; return; }
    users = parsedUsers;
  }
  catch { body.innerHTML = "加载失败，请稍后重试"; return; }
  users = accountManagerVisibleUsers(users);
  _amVisibleUsers = users;
  const targetMode = window.__accessV3 === true;
  const canReset = !targetMode || (typeof hasPerm === "function" && hasPerm("account.security"));
  const canToggle = !targetMode || (typeof hasPerm === "function" && hasPerm("account.open"));
  const rows = users.map((u, index) => {
    const canManage = amCanManageUser(u);
    return `
    <div class="am-row${u.is_active ? "" : " am-disabled"}">
      <span class="am-uname">${escapeHtml(u.username)}</span>
      <span>${escapeHtml(u.display_name || "")}</span>
      ${targetMode
        ? `<span class="am-role-text">${escapeHtml(amRoleText(u))}</span>`
        : `<select class="cs-input am-role" data-uid="${escapeAttr(u.id)}" onchange="amSetRole(this)">${amRoleOptions(u.role)}</select>`}
      <span class="am-status">${u.is_active ? "启用" : "已停用"}</span>
      <span class="am-ops">
        ${canReset && canManage ? `<button type="button" class="plain-button" onclick="amResetPwAt(${index})">重置密码</button>` : ""}
        ${canToggle && canManage ? `<button type="button" class="plain-button" onclick="amToggleAt(${index}, ${u.is_active ? "false" : "true"})">${u.is_active ? "停用" : "启用"}</button>` : ""}
      </span>
    </div>`;
  }).join("");
  const addPanel = targetMode
    ? `<div class="am-add"><span id="amMsg" class="am-msg">新账号请在「诊所人员管理」中找到对应人员，再点“开通登录”。账号岗位跟随人员岗位，不在这里单独改角色。</span></div>`
    : `<div class="am-add">
      <input class="cs-input" id="amNewUser" placeholder="用户名(登录用)">
      <input class="cs-input" id="amNewName" placeholder="姓名">
      <input class="cs-input" id="amNewPw" type="password" placeholder="初始密码">
      <select class="cs-input" id="amNewRole">${amRoleOptions("reception")}</select>
      <button type="button" class="tooth-confirm-btn" onclick="amCreate()">新增账号</button>
      <span id="amMsg" class="am-msg"></span>
    </div>`;
  body.innerHTML = `
    ${addPanel}
    <div class="am-list">
      <div class="am-row am-head"><span>用户名</span><span>姓名</span><span>${targetMode ? "岗位 / 身份" : "角色"}</span><span>状态</span><span>操作</span></div>
      ${rows || '<div class="module-empty">暂无账号</div>'}
    </div>
    <div class="am-self"><button type="button" class="plain-button" onclick="amChangeOwnPassword()">修改我的密码</button></div>`;
}

function amUserAt(index) {
  return Number.isInteger(index) && index >= 0 ? (_amVisibleUsers[index] || null) : null;
}

async function amResetPwAt(index) {
  const user = amUserAt(index);
  if (!user || !amCanManageUser(user)) return;
  return amResetPw(user.id, user.username);
}

async function amToggleAt(index, active) {
  const user = amUserAt(index);
  if (!user || !amCanManageUser(user)) return;
  return amToggle(user.id, active);
}

function amSetMsg(text, ok) {
  const el = document.getElementById("amMsg");
  if (el) { el.textContent = text; el.classList.toggle("txt-ok", ok); el.classList.toggle("txt-danger", !ok); el.style.color = ""; }
}

async function amCreate() {
  if (window.__accessV3 === true) {
    amSetMsg("请在诊所人员管理中为对应人员开通登录账号");
    return;
  }
  const username = (document.getElementById("amNewUser") || {}).value || "";
  const display_name = (document.getElementById("amNewName") || {}).value || "";
  const password = (document.getElementById("amNewPw") || {}).value || "";
  const role = (document.getElementById("amNewRole") || {}).value || "reception";
  if (!username.trim()) { amSetMsg("用户名必填"); return; }
  if (!password.trim()) { amSetMsg("密码不能为空"); return; }
  let res;
  try { res = await fetch("/api/users", {method: "POST", headers: {"Content-Type": "application/json"},
    body: JSON.stringify({username: username.trim(), display_name: display_name.trim(), password, role})}); }
  catch { amSetMsg(amActionError("新增", null)); return; }
  if (!res.ok) { amSetMsg(amActionError("新增", res.status)); return; }
  amSetMsg("已新增", true);
  await amRender();
}

async function amResetPw(id, username) {
  if (!amCanManageUserId(id)) return;
  const pw = await appPrompt(`给「${username}」设置新密码（对方下次用新密码登录）：`, "", {
    type: "password", autocomplete: "new-password",
  });
  if (pw === null) return;
  if (!pw.trim()) { window.alert("密码不能为空"); return; }
  let res;
  try {
    res = await fetch(`/api/users/${encodeURIComponent(id)}/reset-password`,
      {method: "POST", headers: {"Content-Type": "application/json"}, body: JSON.stringify({password: pw})});
  } catch { window.alert(amActionError("重置", null)); return; }
  if (!res.ok) { window.alert(amActionError("重置", res.status)); return; }
  window.alert("已重置");
}

async function amToggle(id, active) {
  if (!amCanManageUserId(id)) return;
  let res;
  try {
    res = await fetch(`/api/users/${encodeURIComponent(id)}`,
      {method: "PUT", headers: {"Content-Type": "application/json"}, body: JSON.stringify({is_active: active})});
  } catch { window.alert(amActionError("操作", null)); return; }
  if (!res.ok) { window.alert(amActionError("操作", res.status)); return; }
  await amRender();
}

async function amSetRole(sel) {
  if (window.__accessV3 === true) return;
  const id = sel.dataset.uid;
  let res;
  try {
    res = await fetch(`/api/users/${encodeURIComponent(id)}`,
      {method: "PUT", headers: {"Content-Type": "application/json"}, body: JSON.stringify({role: sel.value})});
  } catch {
    window.alert(amActionError("改角色", null));
    await amRender();
    return;
  }
  if (!res.ok) {
    window.alert(amActionError("改角色", res.status));
    await amRender();
  }
}

async function amChangeOwnPassword() {
  const oldpw = await appPrompt("请输入原密码：", "", {
    type: "password", autocomplete: "current-password",
  });
  if (oldpw === null) return;
  const newpw = await appPrompt("请输入新密码：", "", {
    type: "password", autocomplete: "new-password",
  });
  if (newpw === null) return;
  if (!newpw.trim()) { window.alert("新密码不能为空"); return; }
  let res;
  try {
    res = await fetch("/api/auth/change-password", {method: "POST", headers: {"Content-Type": "application/json"},
      body: JSON.stringify({old_password: oldpw, new_password: newpw})});
  } catch { window.alert(amActionError("改密", null)); return; }
  if (!res.ok) { window.alert(amActionError("改密", res.status)); return; }
  window.alert("密码已修改");
}

window.openAccountManager = openAccountManager;
window.amOpenNewStaff = amOpenNewStaff;
window.amClose = amClose;
window.accountManagerVisibleUsers = accountManagerVisibleUsers;
