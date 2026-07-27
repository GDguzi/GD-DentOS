// 配置管理（集中式设置中心）：左侧导航一级模块。
// 管理向/低频功能统一收进这里，顶层导航只留日常门诊工作——
// 子页含人员与权限(人员/角色权限/账号管理)、收费项目、同步中心、影像资料、操作历史、回收站。

let _cfgTab = "access";
let _accessCenterTab = "staff";

// 各子页所需权限(后端对应守卫)，按权限显示子 tab，避免"看得到入口点进去却被后端 403"。
// 回收站(第2步)：recycle.manage 只控这个入口是否出现在配置管理里，默认只给 admin，
// 收窄"人人可见"的暴露面；后端还原逻辑不变(仍是 patient.view + 本人/admin 才能还原)，
// 护士/前台/助理撤销自己删错的东西不受影响——产品决策明确只藏入口,不锁后端。
function _cfgTabDefs() {
  const has = k => typeof hasPerm === "function" && hasPerm(k);
  const hasAny = (...keys) => keys.some(has);
  const legacy = window.__accessV3 !== true;
  const systemAdmin = window.__isSystemAdmin === true
    || (legacy && window.__userRole === "admin");
  const defs = [];
  // 第3步:全店设置(诊所名/营业时间/诊室/会员开关集中编辑,复用既有 PUT 接口)
  if (hasAny("clinic_settings.manage", "settings.manage")) defs.push({key: "shop", label: "全店设置"});
  // 人员档案、角色权限和账号管理统一收进一个外层入口；内部再按精确资格拆分，避免重复入口。
  const canAccessCenter = hasAny("staff.view", "staff.manage", "account.view", "account.open", "account.security", "user.manage")
    || systemAdmin || (legacy && has("role.manage"));
  if (canAccessCenter) defs.push({key: "access", label: "人员与权限"});
  if (hasAny("master_data.view", "master_data.manage")) defs.push({key: "charge-items", label: "收费项目"});
  // 同步中心/影像资料运维是可选迁移扩展配套,后端没带扩展(/api/capabilities sync=false)不显示
  const syncCap = !window.__capabilities || window.__capabilities.sync !== false;
  const canSync = systemAdmin || (legacy && has("sync.manage"));
  if (syncCap && canSync) defs.push({key: "sync", label: "同步中心"});
  if (syncCap && canSync) defs.push({key: "images", label: "影像资料"});
  if (has("audit.view")) defs.push({key: "audit", label: "操作历史"});
  if (hasAny("recycle.view", "recycle.manage")) defs.push({key: "recycle", label: "回收站"});
  // 报表已移到顶层导航「数据报表」,不再放配置管理子tab
  // 第4步:数据备份——后端 require_admin,前端同口径只对管理员显示入口
  if (systemAdmin) defs.push({key: "backup", label: "数据备份"});
  return defs;
}

function _accessCenterTabs() {
  const has = k => typeof hasPerm === "function" && hasPerm(k);
  const hasAny = (...keys) => keys.some(has);
  const legacy = window.__accessV3 !== true;
  const systemAdmin = window.__isSystemAdmin === true
    || (legacy && window.__userRole === "admin");
  let canStaff = false;
  if (legacy) {
    canStaff = hasAny("staff.view", "staff.manage");
  } else {
    // V3 管理员的 hasPerm 会全通过，普通 v3 用户不能据此放大人员档案访问；
    // 但 is_system_admin 是设计内的超级用户（已可下载整库备份/授予任意权限，
    // 人员档案对其不构成额外边界）——且空库首启只有这一个账号，不放行会形成
    // 「无人能进人员管理」死锁（2026-07-26 产品决策）。
    const directPerms = window.__userPerms;
    const hasDirectStaff = directPerms && typeof directPerms.has === "function"
      && (directPerms.has("staff.view") || directPerms.has("staff.manage"));
    canStaff = systemAdmin
      || hasDirectStaff
      || (!directPerms && hasAny("staff.view", "staff.manage"));
  }
  const tabs = [];
  if (canStaff) tabs.push({key: "staff", label: "人员"});
  if (systemAdmin || (legacy && has("role.manage"))) {
    tabs.push({key: "roles", label: "角色与权限"});
  }
  return tabs;
}

function _canAccessAccountsFromConfig() {
  if (typeof canAccessAccounts === "function") return canAccessAccounts();
  if (typeof hasAnyPerm === "function") {
    return hasAnyPerm("account.view", "account.open", "account.security", "user.manage");
  }
  return typeof hasPerm === "function" && (
    hasPerm("account.view") || hasPerm("account.open")
      || hasPerm("account.security") || hasPerm("user.manage")
  );
}

async function _requestLeaveRolePermissionsIfPresent() {
  if (typeof requestLeaveRolePermissions !== "function") return false;
  try {
    return (await requestLeaveRolePermissions()) === true;
  } catch {
    return false;
  }
}

function _rolePermissionsActive() {
  return _cfgTab === "roles" || (_cfgTab === "access" && _accessCenterTab === "roles");
}

async function requestLeaveConfigModule() {
  if (!_rolePermissionsActive()) return true;
  return _requestLeaveRolePermissionsIfPresent();
}

function loadConfigModule() {
  const modulePanel = document.getElementById("modulePanel");
  if (!modulePanel) return;
  const defs = _cfgTabDefs();
  // 默认选中第一个有权限的子 tab(当前 _cfgTab 无权时回退)
  if (!defs.some(d => d.key === _cfgTab)) _cfgTab = defs.length ? defs[0].key : "";
  const subtabs = defs.map(d =>
    `<button type="button" class="module-subtab" data-cfg-tab="${d.key}">${d.label}</button>`).join("");
  modulePanel.innerHTML = `
    <div class="module-subtabs cfg-subtabs">${subtabs}</div>
    <div id="cfgBody" class="cfg-body">${defs.length ? '<div class="module-loading">载入中...</div>' : '<div class="cfg-hint">暂无可用配置项（需相应权限）</div>'}</div>
  `;
  modulePanel.querySelectorAll("[data-cfg-tab]").forEach(btn =>
    btn.addEventListener("click", async () => {
      const nextTab = btn.dataset.cfgTab;
      if (nextTab === _cfgTab) return;
      if (_rolePermissionsActive() && !(await _requestLeaveRolePermissionsIfPresent())) return;
      _cfgTab = nextTab;
      renderCfgTabs(modulePanel);
      await loadCfgTab();
    }));
  renderCfgTabs(modulePanel);
  if (defs.length) loadCfgTab();
}

function renderCfgTabs(root) {
  root.querySelectorAll("[data-cfg-tab]").forEach(b =>
    b.classList.toggle("active", b.dataset.cfgTab === _cfgTab));
}

async function loadCfgTab() {
  const body = document.getElementById("cfgBody");
  if (!body) return;
  if (_cfgTab === "access") {
    await renderAccessCenter();
  } else if (_cfgTab === "charge-items") {
    if (typeof loadChargeItemsModule === "function") await loadChargeItemsModule();
  } else if (_cfgTab === "sync") {
    if (typeof loadSyncModule === "function") await loadSyncModule();
  } else if (_cfgTab === "images") {
    if (typeof loadImageModule === "function") await loadImageModule();
  } else if (_cfgTab === "audit") {
    if (typeof loadAuditLogModule === "function") await loadAuditLogModule();
  } else if (_cfgTab === "recycle") {
    if (typeof loadRecycleBin === "function") await loadRecycleBin();
  } else if (_cfgTab === "reports") {
    if (typeof loadReportsModule === "function") loadReportsModule();
  } else if (_cfgTab === "shop") {
    await renderShopSettings(body);
  } else if (_cfgTab === "backup") {
    await renderBackupTab(body);
  }
}

function renderAccessCenterTabs(root) {
  root.querySelectorAll("[data-access-center-tab]").forEach(button =>
    button.classList.toggle("active", button.dataset.accessCenterTab === _accessCenterTab));
}

async function renderAccessCenter() {
  const body = document.getElementById("cfgBody");
  if (!body) return;
  const tabs = _accessCenterTabs();
  if (!tabs.some(tab => tab.key === _accessCenterTab)) {
    _accessCenterTab = tabs.length ? tabs[0].key : "";
  }
  const tabButtons = tabs.map(tab =>
    `<button type="button" class="module-subtab" data-access-center-tab="${tab.key}">${tab.label}</button>`
  ).join("");
  body.innerHTML = `
    <section class="panel access-center-shell">
      <div class="panel-head"><span>人员与权限中心</span>
        <span class="cfg-hint">集中管理诊所人员、角色权限与登录账号；页面会按当前权限显示可用内容</span></div>
      <div class="panel-body">
        <div class="module-subtabs access-center-tabs"${tabs.length <= 1 ? " hidden" : ""}>${tabButtons}</div>
        <div id="accessCenterBody"></div>
      </div>
    </section>`;
  body.querySelectorAll("[data-access-center-tab]").forEach(button =>
    button.addEventListener("click", async () => {
      const nextTab = button.dataset.accessCenterTab;
      if (nextTab === _accessCenterTab) return;
      if (_accessCenterTab === "roles" && !(await _requestLeaveRolePermissionsIfPresent())) return;
      _accessCenterTab = nextTab;
      renderAccessCenterTabs(body);
      await _loadAccessCenterTab();
    }));
  renderAccessCenterTabs(body);
  await _loadAccessCenterTab();
}

async function _loadAccessCenterTab() {
  const inner = document.getElementById("accessCenterBody");
  if (!inner) return;
  const accountAccess = _canAccessAccountsFromConfig();
  const acctBtn = accountAccess
    ? `<button type="button" class="plain-button" onclick="openAccountManager()">账号管理</button>` : "";
  if (_accessCenterTab === "staff") {
    inner.innerHTML = `
      <div class="panel-head"><span>人员</span>
        <span class="cfg-hint">医生 / 护士 / 咨询师 / 助理档案，供处置配台、绩效统计与回访人选择</span>
        ${acctBtn}</div>
      <div id="cfgStaffBody">载入中...</div>`;
    if (typeof renderStaffInto === "function") await renderStaffInto("cfgStaffBody");
  } else if (_accessCenterTab === "roles") {
    inner.innerHTML = `
      <div class="panel-head"><span>角色与权限</span>
        <span class="cfg-hint">勾选每个角色能进哪些模块、执行哪些操作；系统管理员始终全通过</span>
        ${acctBtn}</div>
      <div id="cfgRolePerms">载入中...</div>`;
    if (typeof renderRolePermsInto === "function") await renderRolePermsInto("cfgRolePerms");
  } else if (accountAccess) {
    // 只有账号权限时绝不挂载人员组件，避免请求 /api/staff-admin/list 或泄露人员档案。
    inner.innerHTML = `
      <div class="cfg-hint">当前仅有账号管理权限，不能查看人员档案或角色权限。</div>
      <div>${acctBtn}</div>`;
  } else {
    inner.innerHTML = `<div class="cfg-hint">暂无可用的人员与权限配置项</div>`;
  }
}

// ===== 第3步 全店设置 =====
async function renderShopSettings(body) {
  body.innerHTML = `<div class="module-loading">载入中...</div>`;
  let clinic, appt, rooms, feats;
  try {
    [clinic, appt, rooms, feats] = await Promise.all([
      fetch("/api/settings/clinic").then(r => r.json()),
      fetch("/api/settings/appointment").then(r => r.json()),
      fetch("/api/settings/rooms").then(r => r.json()),
      fetch("/api/settings/features").then(r => r.json()),
    ]);
  } catch { body.innerHTML = `<div class="module-loading">载入失败（网络异常）</div>`; return; }
  body.innerHTML = `
    <section class="panel">
      <div class="panel-head"><span>全店设置</span>
        <span class="cfg-hint">诊所名显示在登录页/回访卡/打印单头；营业时间驱动预约网格；诊室供分诊选择</span></div>
      <div class="panel-body cfg-shop">
        <label>诊所名称 <input class="ord-input" data-shop="name" value="${escapeAttr(clinic.name || "")}"></label>
        <label>营业开始 <input class="ord-input" data-shop="business_start" value="${escapeAttr(appt.business_start || "")}" placeholder="08:00"></label>
        <label>营业结束 <input class="ord-input" data-shop="business_end" value="${escapeAttr(appt.business_end || "")}" placeholder="21:30"></label>
        <label>预约最小单位(分钟) <select class="ord-input" data-shop="min_slot_minutes">${[5, 10, 15, 20, 30, 60].map(v =>
          `<option value="${v}"${v === appt.min_slot_minutes ? " selected" : ""}>${v}</option>`).join("")}</select></label>
        <label><input type="checkbox" data-shop="show_doctor_column"${appt.show_doctor_column ? " checked" : ""}> 预约按医生分列</label>
        <label class="cfg-wide">诊室列表(逗号分隔) <input class="ord-input" data-shop="rooms" value="${escapeAttr((rooms.list || []).join(","))}"></label>
        <label><input type="checkbox" data-shop="membership_enabled"${feats.membership_enabled ? " checked" : ""}> 启用会员储值模块</label>
        <div class="cfg-shop-foot">
          <button type="button" class="tooth-confirm-btn" data-shop-save>保存全部</button>
          <span class="record-save-status" data-shop-status></span>
        </div>
      </div>
    </section>`;
  const val = k => body.querySelector(`[data-shop="${k}"]`);
  body.querySelector("[data-shop-save]").addEventListener("click", async () => {
    const status = body.querySelector("[data-shop-status]");
    status.textContent = "保存中...";
    try {
      const results = await Promise.all([
        fetch("/api/settings/clinic", {method: "PUT", headers: {"Content-Type": "application/json"},
          body: JSON.stringify({name: val("name").value.trim()})}),
        fetch("/api/settings/appointment", {method: "PUT", headers: {"Content-Type": "application/json"},
          body: JSON.stringify({business_start: val("business_start").value.trim(),
            business_end: val("business_end").value.trim(),
            min_slot_minutes: Number(val("min_slot_minutes").value),
            show_doctor_column: val("show_doctor_column").checked})}),
        fetch("/api/settings/rooms", {method: "PUT", headers: {"Content-Type": "application/json"},
          body: JSON.stringify({list: val("rooms").value.split(/[,，]/).map(s => s.trim()).filter(Boolean)})}),
        fetch("/api/settings/features", {method: "PUT", headers: {"Content-Type": "application/json"},
          body: JSON.stringify({membership_enabled: val("membership_enabled").checked})}),
      ]);
      const bad = results.find(r => !r.ok);
      if (bad) { const m = await bad.json().catch(() => ({})); status.textContent = "保存失败：" + (m.detail || bad.status); return; }
      status.textContent = "已保存 ✓";
      const name = val("name").value.trim();
      if (name && typeof window !== "undefined") {   // 标题/打印头即时跟随
        window.CLINIC_NAME = name;
        document.title = `${name} · 本地系统`;
      }
    } catch { status.textContent = "保存失败（网络异常）"; }
  });
}

// ===== 第4步 数据备份(仅管理员;后端 require_admin 是闸门) =====
async function renderBackupTab(body) {
  body.innerHTML = `
    <section class="panel">
      <div class="panel-head"><span>数据备份</span>
        <span class="cfg-hint">整库热备(不锁库)存到 data/backups；可勾选打包患者影像；下载后请妥善保管(含全量隐私)</span></div>
      <div class="panel-body">
        <div class="rpt-filter">
          <label><input type="checkbox" data-bk-images> 连患者影像一起打包(zip,较大)</label>
          <button type="button" class="tooth-confirm-btn" data-bk-now>立即备份</button>
          <span class="record-save-status" data-bk-status></span>
        </div>
        <div data-bk-list><div class="module-loading">载入中...</div></div>
      </div>
    </section>`;
  const listEl = body.querySelector("[data-bk-list]");
  async function refreshList() {
    let d;
    try { d = await (await fetch("/api/backup/list")).json(); }
    catch { listEl.innerHTML = `<div class="module-loading">备份列表载入失败</div>`; return; }
    const items = d.list || [];
    if (!items.length) { listEl.innerHTML = `<div class="empty">还没有备份</div>`; return; }
    const mb = b => (b / 1048576).toFixed(1) + " MB";
    listEl.innerHTML = `
      <table class="data-table">
        <thead><tr><th>文件</th><th>类型</th><th style="text-align:right">大小</th><th>时间</th><th></th></tr></thead>
        <tbody>${items.map(it => `
          <tr>
            <td>${escapeHtml(it.name)}</td><td>${escapeHtml(it.kind)}</td>
            <td style="text-align:right">${mb(it.size_bytes || 0)}</td>
            <td>${escapeHtml(it.created_at || "")}</td>
            <td><a href="/api/backup/${encodeURIComponent(it.name)}/download" download>下载</a></td>
          </tr>`).join("")}</tbody>
      </table>`;
  }
  body.querySelector("[data-bk-now]").addEventListener("click", async () => {
    const status = body.querySelector("[data-bk-status]");
    status.textContent = "备份中...";
    try {
      const r = await fetch("/api/backup", {method: "POST", headers: {"Content-Type": "application/json"},
        body: JSON.stringify({include_images: body.querySelector("[data-bk-images]").checked})});
      if (!r.ok) { const m = await r.json().catch(() => ({})); status.textContent = "备份失败：" + (m.detail || r.status); return; }
      status.textContent = "备份完成 ✓";
      refreshList();
    } catch { status.textContent = "备份失败（网络异常）"; }
  });
  refreshList();
}

Object.assign(window, {loadConfigModule, requestLeaveConfigModule});
