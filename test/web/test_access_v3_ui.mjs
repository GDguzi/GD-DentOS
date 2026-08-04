import { test } from "node:test";
import assert from "node:assert";
import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import { dirname, join } from "node:path";
import vm from "node:vm";

const here = dirname(fileURLToPath(import.meta.url));
const staticDir = join(here, "..", "..", "local_app", "static");
const helpersSrc = readFileSync(join(staticDir, "helpers.js"), "utf8");
const appSrc = readFileSync(join(staticDir, "app.js"), "utf8");
const moduleViewsSrc = readFileSync(join(staticDir, "module_views.js"), "utf8");
const configSrc = readFileSync(join(staticDir, "config_center.js"), "utf8");
const rolePermissionsSrc = readFileSync(join(staticDir, "role_permissions.js"), "utf8");
const patientWorkspaceSrc = readFileSync(join(staticDir, "patient_workspace.js"), "utf8");
const workspaceTabsSrc = readFileSync(join(staticDir, "workspace_tabs.js"), "utf8");
const indexSrc = readFileSync(join(staticDir, "index.html"), "utf8");
const billingSrc = readFileSync(join(staticDir, "workspace_billing.js"), "utf8");

const permissionHelpers = helpersSrc.slice(
  helpersSrc.indexOf("function hasPerm"),
  helpersSrc.indexOf("// 防双击重复提交"),
);
const roleLabels = appSrc.slice(
  appSrc.indexOf("const _ROLE_LABELS"),
  appSrc.indexOf("let _authReloading"),
);
const initAuth = appSrc.slice(
  appSrc.indexOf("async function initAuthUI"),
  appSrc.indexOf("// 后端能力", appSrc.indexOf("async function initAuthUI")),
);
const authStartupSrc = appSrc.slice(appSrc.indexOf("initAuthUI().then("));
const permissionVisibility = appSrc.slice(
  appSrc.indexOf("function applyPermVisibility"),
  appSrc.indexOf("// #488", appSrc.indexOf("function applyPermVisibility")),
);
const workspaceNavigationSrc = appSrc.slice(
  appSrc.indexOf("let activeModuleView"),
  appSrc.indexOf("function setWorkDateForOffset"),
);
const patientWorkspaceLifecycleSrc = patientWorkspaceSrc.slice(
  patientWorkspaceSrc.indexOf("let workspacePatientId"),
  patientWorkspaceSrc.indexOf("function resetWorkspacePanels"),
);
const workspaceContinuationSrc = patientWorkspaceSrc.slice(
  patientWorkspaceSrc.indexOf("let _workspaceRenderRevision"),
  patientWorkspaceSrc.indexOf("function workspacePageEl"),
);
const patientWorkspaceProfileSaveSrc = patientWorkspaceSrc.slice(
  patientWorkspaceSrc.indexOf("async function saveWorkspaceProfile"),
  patientWorkspaceSrc.indexOf("function parseSourceJson"),
);
const workspaceNoteSaveSrc = workspaceTabsSrc.slice(
  workspaceTabsSrc.indexOf("async function saveWorkspaceNote"),
  workspaceTabsSrc.indexOf("// ---------- 审计 tab ----------"),
);

function makeWorkspaceTabPage(tabNames, activeTab) {
  const makeClassList = active => {
    const classes = new Set(active ? ["active"] : []);
    return {
      contains: name => classes.has(name),
      toggle(name, force) {
        if (force) classes.add(name);
        else classes.delete(name);
      },
    };
  };
  const tabs = tabNames.map(tab => ({
    dataset: {workspaceTab: tab},
    classList: makeClassList(tab === activeTab),
  }));
  const panels = tabNames.map(tab => ({
    dataset: {workspacePanel: tab},
    hidden: tab !== activeTab,
    classList: makeClassList(tab === activeTab),
  }));
  return {
    tabs,
    panels,
    page: {
      querySelector(selector) {
        if (selector === ".workspace-tab.active") {
          return tabs.find(button => button.classList.contains("active")) || null;
        }
        const tab = /^\[data-workspace-tab="([^"]+)"\]$/.exec(selector)?.[1];
        if (tab) return tabs.find(button => button.dataset.workspaceTab === tab) || null;
        const panel = /^\[data-workspace-panel="([^"]+)"\]$/.exec(selector)?.[1];
        return panel ? panels.find(item => item.dataset.workspacePanel === panel) || null : null;
      },
      querySelectorAll(selector) {
        if (selector === "[data-workspace-tab]") return tabs;
        if (selector === "[data-workspace-panel]") return panels;
        return [];
      },
    },
  };
}

function customerHubNavigationSandbox({
  leave,
  workDate: sandboxWorkDate = {value: "2026-07-21"},
  localDateValue: sandboxLocalDateValue = () => "2026-07-21",
} = {}) {
  const calls = {appointments: 0, mounted: []};
  const navButtons = ["today", "appointments", "return-visits"].map(view => ({
    dataset: {workspaceView: view},
    classList: {
      active: view === "today",
      toggle(_name, force) { this.active = force; },
      contains() { return this.active; },
    },
  }));
  const elements = {
    modulePanel: {hidden: true, innerHTML: "", querySelector: () => null},
    patientWorkspace: {hidden: true},
    patientWorkspacePage: {hidden: true},
    todayWorkPanel: {hidden: false},
    workspaceTitle: {textContent: "今日工作台"},
    workspaceSubtitle: {textContent: ""},
  };
  const sandbox = {
    document: {
      getElementById: id => elements[id] || null,
      querySelectorAll: selector => selector === ".nav-item[data-workspace-view]" ? navButtons : [],
      querySelector: () => null,
    },
    q: {value: ""},
    modulePanel: elements.modulePanel,
    patientWorkspace: elements.patientWorkspace,
    todayWorkPanel: elements.todayWorkPanel,
    workspaceTitle: elements.workspaceTitle,
    workspaceSubtitle: elements.workspaceSubtitle,
    workDate: sandboxWorkDate,
    location: {hash: ""},
    history: {state: null, length: 1, replaceState() {}, back() {}},
    localDateValue: sandboxLocalDateValue,
    hasPerm: () => true,
    fetch: async () => ({ok: true, json: async () => ({})}),
    hidePatientWorkspacePage: () => { elements.patientWorkspacePage.hidden = true; },
    syncTodayActions() {},
    loadTodayWork() {},
    loadPatientModule() {},
    loadAppointmentModule() { calls.appointments += 1; },
    loadBillingModule() {},
    loadAiDrafts() {},
    async loadCustomerHubV2(context) {
      calls.mounted.push(context);
      return true;
    },
    async requestLeaveCustomerHubV2() {
      return typeof leave === "function" ? leave() : undefined;
    },
    confirm: () => true,
  };
  sandbox.window = sandbox;
  vm.createContext(sandbox);
  vm.runInContext(moduleViewsSrc + workspaceNavigationSrc + `
    _authenticatedBusinessReady = true;
    this.__setWorkspaceView = view => { activeModuleView = view; };
    this.__getWorkspaceView = () => activeModuleView;
  `, sandbox);
  sandbox.loadAppointmentModule = () => { calls.appointments += 1; };
  return {sandbox, calls, elements};
}

async function authSandbox(responseBody, {ok = true, status = 200} = {}) {
  const controlledEntrypoints = [
    {hidden: false, dataset: {perm: "billing.view"}},
    {hidden: false, dataset: {permAny: "staff.view,account.view"}},
  ];
  const elements = {
    userChip: {hidden: true},
    userChipName: {textContent: ""},
    acctMgrBtn: {hidden: true},
    statusText: {textContent: ""},
  };
  const sandbox = {
    document: {
      getElementById: id => elements[id] || null,
      querySelectorAll: selector => (
        selector === "[data-perm], [data-perm-any]" ? controlledEntrypoints : []
      ),
    },
    _origFetch: async () => ({ok, status, json: async () => responseBody}),
    applyPermVisibility: () => { sandbox.visibilityApplied += 1; },
    visibilityApplied: 0,
    location: {reload() {}},
  };
  sandbox.window = sandbox;
  vm.createContext(sandbox);
  vm.runInContext(permissionHelpers + roleLabels + initAuth, sandbox);
  await sandbox.initAuthUI();
  return {sandbox, elements, controlledEntrypoints};
}

async function authStartupSandbox(responseBody, {deferAuth = false} = {}) {
  const calls = {today: 0, patientHash: 0, clinic: 0, capabilities: 0};
  const response = {ok: true, status: 200, json: async () => responseBody};
  let finishAuth = null;
  const sandbox = {
    document: {
      getElementById: () => null,
      querySelectorAll: () => [],
      scripts: [],
    },
    _origFetch: deferAuth
      ? () => new Promise(resolve => { finishAuth = () => resolve(response); })
      : async () => response,
    applyPermVisibility() {},
    initClinicIdentity: async () => { calls.clinic += 1; },
    initCapabilities: async () => { calls.capabilities += 1; },
    loadTodayWork: () => { calls.today += 1; },
    handlePatientWorkspaceHash: () => { calls.patientHash += 1; },
    registerAuthenticatedBusinessEvents() {},
    _authenticatedBusinessReady: false,
    location: {reload() {}},
  };
  sandbox.window = sandbox;
  vm.createContext(sandbox);
  vm.runInContext(permissionHelpers + roleLabels + initAuth + authStartupSrc, sandbox);
  await new Promise(resolve => setImmediate(resolve));
  return {
    calls,
    async completeAuth() {
      if (finishAuth) finishAuth();
      await new Promise(resolve => setImmediate(resolve));
    },
  };
}

function makeStartupEventTarget(initial = {}) {
  const listeners = new Map();
  const classes = new Set(initial.active ? ["active"] : []);
  return {
    ...initial,
    hidden: Boolean(initial.hidden),
    classList: {
      contains: name => classes.has(name),
      toggle(name, force) {
        if (force) classes.add(name);
        else classes.delete(name);
      },
    },
    addEventListener(type, listener) {
      const registered = listeners.get(type) || [];
      registered.push(listener);
      listeners.set(type, registered);
    },
    dispatch(type, event = {}) {
      for (const listener of listeners.get(type) || []) listener(event);
    },
  };
}

async function realAppInteractionSandbox(responseBody, {deferAuth = false} = {}) {
  const calls = {today: 0, patients: 0, shownPatients: []};
  const qElement = makeStartupEventTarget({value: ""});
  qElement.getBoundingClientRect = () => ({left: 0, bottom: 0, width: 280});
  const searchButton = makeStartupEventTarget();
  const navToday = makeStartupEventTarget({active: true, dataset: {workspaceView: "today"}});
  const navPatients = makeStartupEventTarget({dataset: {workspaceView: "patients"}});
  const navButtons = [navToday, navPatients];
  const elements = {
    q: qElement,
    searchBtn: searchButton,
    statusText: {textContent: ""},
    todayWorkPanel: {hidden: false},
    modulePanel: {hidden: true, innerHTML: ""},
    patientWorkspace: {hidden: true},
    patientWorkspacePage: {hidden: true},
    workspaceTitle: {textContent: "今日工作台"},
    workspaceSubtitle: {textContent: ""},
  };
  const windowListeners = new Map();
  const location = {
    hash: "",
    pathname: "/",
    search: "",
    reload() {},
  };
  const history = {
    state: null,
    length: 1,
    pushState(state, _title, url) {
      history.state = state;
      location.hash = String(url).includes("#") ? String(url).slice(String(url).indexOf("#")) : "";
    },
    replaceState(state, _title, url) {
      history.state = state;
      location.hash = String(url).includes("#") ? String(url).slice(String(url).indexOf("#")) : "";
    },
    back() {},
  };
  const response = payload => ({
    ok: true,
    status: 200,
    json: async () => payload,
  });
  let finishAuth = null;
  const originalFetch = url => {
    const path = String(url);
    if (path === "/api/auth/me" && deferAuth) {
      return new Promise(resolve => { finishAuth = () => resolve(response(responseBody)); });
    }
    if (path === "/api/auth/me") return Promise.resolve(response(responseBody));
    if (path === "/api/settings/clinic") {
      return Promise.resolve(response({name: "合成诊所", configured: true}));
    }
    if (path === "/api/capabilities") return Promise.resolve(response({sync: false}));
    return Promise.resolve(response({}));
  };
  const sandbox = {
    document: {
      title: "",
      scripts: [],
      body: {appendChild() {}},
      getElementById: id => elements[id] || null,
      querySelector(selector) {
        if (selector === ".nav-item.active[data-workspace-view]") {
          return navButtons.find(button => button.classList.contains("active")) || null;
        }
        return null;
      },
      querySelectorAll(selector) {
        if (selector === ".nav-item[data-workspace-view]") return navButtons;
        return [];
      },
      createElement: () => makeStartupEventTarget({style: {}}),
    },
    history,
    location,
    localDateValue: () => "2026-07-17",
    MutationObserver: class {
      observe() {}
    },
    fetch: originalFetch,
    addEventListener(type, listener) {
      const registered = windowListeners.get(type) || [];
      registered.push(listener);
      windowListeners.set(type, registered);
    },
    scrollTo() {},
    loadTodayWork: () => { calls.today += 1; },
    loadPatientModule: () => { calls.patients += 1; },
    loadAppointmentModule() {},
    loadReturnVisitModule() {},
    loadBillingModule() {},
    loadAiDrafts() {},
    syncTodayActions() {},
    hidePatientWorkspacePage: () => { elements.patientWorkspacePage.hidden = true; },
    showPatientWorkspacePage(identity, tab) {
      calls.shownPatients.push([identity, tab]);
      elements.patientWorkspacePage.hidden = false;
    },
  };
  sandbox.window = sandbox;
  vm.createContext(sandbox);
  vm.runInContext(appSrc, sandbox);
  await new Promise(resolve => setImmediate(resolve));
  return {
    calls,
    elements,
    location,
    navPatients,
    searchButton,
    qElement,
    dispatchHash(hash) {
      location.hash = hash;
      for (const listener of windowListeners.get("hashchange") || []) {
        listener({newURL: `http://local.test/${hash}`});
      }
    },
    async completeAuth() {
      if (finishAuth) finishAuth();
      await new Promise(resolve => setImmediate(resolve));
    },
  };
}

test("V3 普通用户只精确命中新权限，兼容操作别名仅由对应按钮判断", async () => {
  const {sandbox} = await authSandbox({user: {
    username: "doctor", display_name: "黄医生", primary_role: "doctor",
    role_keys: ["doctor", "consultant"], is_system_admin: false,
    permissions: ["payment.refund", "patient.profile.export", "payment_method.manage"],
  }});

  assert.strictEqual(sandbox.hasPerm("payment.refund"), true);
  assert.strictEqual(sandbox.hasPerm("billing.refund"), false);
  assert.strictEqual(sandbox.hasPerm("payment"), false);
  assert.strictEqual(sandbox.canRefund(), true);
  assert.strictEqual(sandbox.canExport(), true);
  assert.strictEqual(sandbox.canManagePaymentMethods(), true);
  assert.deepStrictEqual(Array.from(sandbox.__userRoleKeys), ["doctor", "consultant"]);

  const wildcardOrdinary = await authSandbox({user: {
    username: "not-root", primary_role: "doctor", role_keys: ["doctor"],
    is_system_admin: false, permissions: ["*"],
  }});
  assert.strictEqual(wildcardOrdinary.sandbox.hasPerm("any.future.permission"), false);

  for (const permission of ["account.view", "account.open", "account.security"]) {
    const accountUser = await authSandbox({user: {
      username: permission, primary_role: "doctor", role_keys: ["doctor"],
      is_system_admin: false, permissions: [permission],
    }});
    assert.strictEqual(accountUser.sandbox.canAccessAccounts(), true, permission);
    assert.strictEqual(accountUser.elements.acctMgrBtn.hidden, false, permission);
  }
});

test("V3 system admin 前端全权限，primary_role 正确显示", async () => {
  const {sandbox, elements} = await authSandbox({user: {
    username: "root", display_name: "系统管理员", primary_role: "doctor",
    role_keys: ["doctor"], is_system_admin: true, permissions: [],
  }});

  assert.strictEqual(sandbox.hasPerm("any.future.permission"), true);
  assert.strictEqual(sandbox.__isSystemAdmin, true);
  assert.strictEqual(elements.userChipName.textContent, "系统管理员（医生）");
  assert.strictEqual(elements.acctMgrBtn.hidden, false);
});

test("V3 system admin 始终拥有 staff 内页，同时保留后端直接 entitlement", async () => {
  const accessCenterTabsSrc = configSrc.slice(
    configSrc.indexOf("function _accessCenterTabs"),
    configSrc.indexOf("function _canAccessAccountsFromConfig"),
  );
  const cases = [
    {permissions: [], expectedDirect: [], expectedTabs: ["staff", "roles"]},
    {permissions: ["staff.view"], expectedDirect: ["staff.view"], expectedTabs: ["staff", "roles"]},
  ];
  for (const {permissions, expectedDirect, expectedTabs} of cases) {
    const {sandbox} = await authSandbox({user: {
      username: "root", display_name: "系统管理员", primary_role: "admin",
      role_keys: ["admin"], is_system_admin: true, permissions,
    }});
    vm.runInContext(
      accessCenterTabsSrc + "\nthis.__authenticatedAccessCenterTabs = _accessCenterTabs;",
      sandbox,
    );
    assert.deepStrictEqual(
      Array.from(sandbox.__authenticatedAccessCenterTabs(), tab => tab.key),
      expectedTabs,
      JSON.stringify(permissions),
    );
    assert.deepStrictEqual(Array.from(sandbox.__userPerms), expectedDirect, JSON.stringify(permissions));
    assert.strictEqual(sandbox.hasPerm("任意未来权限"), true, JSON.stringify(permissions));
  }
});

test("旧 role/旧权限继续支持，缺角色时不把 undefined 显示给用户", async () => {
  const legacy = await authSandbox({user: {
    username: "front", display_name: "前台", role: "reception",
    permissions: ["billing.refund", "data.export", "billing.pay", "user.manage"],
  }});
  assert.strictEqual(legacy.sandbox.canRefund(), true);
  assert.strictEqual(legacy.sandbox.canExport(), true);
  assert.strictEqual(legacy.sandbox.canManagePaymentMethods(), true);
  assert.strictEqual(legacy.elements.acctMgrBtn.hidden, false);

  const roleless = await authSandbox({user: {
    username: "plain", display_name: "普通人员", permissions: [],
  }});
  assert.strictEqual(roleless.elements.userChipName.textContent, "普通人员");
  assert.doesNotMatch(roleless.elements.userChipName.textContent, /undefined|null/);
});

test("认证失败、畸形合同和裸用户对象都不得建立前端登录态", async () => {
  const validUser = {
    username: "root", display_name: "不应显示", primary_role: "doctor",
    role_keys: ["doctor"], is_system_admin: true, permissions: ["*"],
  };
  const cases = [
    ["401 错误对象", {detail: "未登录"}, {ok: false, status: 401}],
    ["500 即使夹带 user", {user: validUser}, {ok: false, status: 500}],
    ["成功响应缺 user", {detail: "畸形成功响应"}, {ok: true, status: 200}],
    ["成功响应 user 非对象", {user: "root"}, {ok: true, status: 200}],
    ["不再支持裸 user 合同", validUser, {ok: true, status: 200}],
    ["V3 缺 permissions", {user: {
      username: "root", primary_role: "admin", role_keys: ["admin"], is_system_admin: true,
    }}, {ok: true, status: 200}],
    ["V3 permissions 为 null", {user: {
      username: "root", primary_role: "admin", role_keys: ["admin"], is_system_admin: true,
      permissions: null,
    }}, {ok: true, status: 200}],
    ["V3 permissions 非数组", {user: {
      username: "root", primary_role: "admin", role_keys: ["admin"], is_system_admin: true,
      permissions: {staff: true},
    }}, {ok: true, status: 200}],
    ["V3 permissions 含非字符串", {user: {
      username: "root", primary_role: "admin", role_keys: ["admin"], is_system_admin: true,
      permissions: ["staff.view", 1],
    }}, {ok: true, status: 200}],
    ["V3 permissions 含空白键", {user: {
      username: "root", primary_role: "admin", role_keys: ["admin"], is_system_admin: true,
      permissions: ["   "],
    }}, {ok: true, status: 200}],
    ["V3 permissions 含首尾空白", {user: {
      username: "root", primary_role: "admin", role_keys: ["admin"], is_system_admin: true,
      permissions: [" staff.view"],
    }}, {ok: true, status: 200}],
    ["V3 permissions 含内部空格", {user: {
      username: "root", primary_role: "admin", role_keys: ["admin"], is_system_admin: true,
      permissions: ["staff .view"],
    }}, {ok: true, status: 200}],
    ["V3 permissions 含内部制表符", {user: {
      username: "root", primary_role: "admin", role_keys: ["admin"], is_system_admin: true,
      permissions: ["staff\t.view"],
    }}, {ok: true, status: 200}],
    ["V3 permissions 含重复键", {user: {
      username: "root", primary_role: "admin", role_keys: ["admin"], is_system_admin: true,
      permissions: ["staff.view", "staff.view"],
    }}, {ok: true, status: 200}],
  ];
  for (const [label, body, response] of cases) {
    const {sandbox, elements} = await authSandbox(body, response);
    assert.strictEqual(elements.userChip.hidden, true, label);
    assert.strictEqual(elements.userChipName.textContent, "", label);
    assert.strictEqual(sandbox.__userPerms, null, label);
    assert.strictEqual(sandbox.__isSystemAdmin, false, label);
    assert.strictEqual(sandbox.__userRole, null, label);
    assert.deepStrictEqual(Array.from(sandbox.__userRoleKeys), [], label);
    assert.strictEqual(sandbox.visibilityApplied, 0, label);
  }
});

test("损坏的 V3 permissions 显示中文失败态并隐藏全部权限受控入口", async () => {
  const {sandbox, elements, controlledEntrypoints} = await authSandbox({user: {
    username: "root",
    primary_role: "admin",
    role_keys: ["admin"],
    is_system_admin: true,
    permissions: ["staff .view"],
  }});

  assert.strictEqual(elements.userChip.hidden, true);
  assert.strictEqual(elements.userChipName.textContent, "");
  assert.strictEqual(sandbox.__userPerms, null);
  assert.match(elements.statusText.textContent, /登录信息异常/);
  assert.doesNotMatch(elements.statusText.textContent, /staff|permissions|root/i);
  assert.ok(controlledEntrypoints.every(entry => entry.hidden === true));
});

test("业务启动等待有效认证合同，损坏 permissions 时不加载工作台或患者深链", async () => {
  const {calls: invalid} = await authStartupSandbox({user: {
    username: "root",
    primary_role: "admin",
    role_keys: ["admin"],
    is_system_admin: true,
    permissions: ["patient .view"],
  }});
  assert.deepStrictEqual(invalid, {
    today: 0,
    patientHash: 0,
    clinic: 0,
    capabilities: 0,
  });

  const {calls: valid} = await authStartupSandbox({user: {
    username: "root",
    primary_role: "admin",
    role_keys: ["admin"],
    is_system_admin: true,
    permissions: ["patient.view"],
  }});
  assert.deepStrictEqual(valid, {
    today: 1,
    patientHash: 1,
    clinic: 1,
    capabilities: 1,
  });
});

test("认证请求未完成时业务保持关闭，有效合同完成后只启动一次", async () => {
  const pending = await authStartupSandbox({user: {
    username: "root",
    primary_role: "admin",
    role_keys: ["admin"],
    is_system_admin: true,
    permissions: ["patient.view"],
  }}, {deferAuth: true});

  assert.deepStrictEqual(pending.calls, {
    today: 0,
    patientHash: 0,
    clinic: 0,
    capabilities: 0,
  });
  await pending.completeAuth();
  assert.deepStrictEqual(pending.calls, {
    today: 1,
    patientHash: 1,
    clinic: 1,
    capabilities: 1,
  });
});

test("认证 pending 或 permissions 损坏时真实导航、hash 和搜索监听均零业务提交", async () => {
  const validUser = {user: {
    username: "root",
    primary_role: "admin",
    role_keys: ["admin"],
    is_system_admin: true,
    permissions: ["patient.view"],
  }};
  const pending = await realAppInteractionSandbox(validUser, {deferAuth: true});
  const invalid = await realAppInteractionSandbox({user: {
    ...validUser.user,
    permissions: ["patient .view"],
  }});

  for (const current of [pending, invalid]) {
    const before = {
      moduleHidden: current.elements.modulePanel.hidden,
      patientHidden: current.elements.patientWorkspacePage.hidden,
    };
    current.navPatients.dispatch("click");
    current.qElement.value = "synthetic";
    current.searchButton.dispatch("click");
    current.dispatchHash("#patient/synthetic/profile");

    assert.deepStrictEqual(current.calls, {today: 0, patients: 0, shownPatients: []});
    assert.deepStrictEqual({
      moduleHidden: current.elements.modulePanel.hidden,
      patientHidden: current.elements.patientWorkspacePage.hidden,
    }, before);
  }
});

test("有效认证完成后真实导航、hash 和搜索监听才各执行一次", async () => {
  const current = await realAppInteractionSandbox({user: {
    username: "root",
    primary_role: "admin",
    role_keys: ["admin"],
    is_system_admin: true,
    permissions: ["patient.view"],
  }});
  assert.strictEqual(current.calls.today, 1);

  current.navPatients.dispatch("click");
  current.qElement.value = "synthetic";
  current.searchButton.dispatch("click");
  current.dispatchHash("#patient/synthetic/profile");

  assert.strictEqual(current.calls.patients, 2);
  assert.deepStrictEqual(current.calls.shownPatients, [["synthetic", "profile"]]);
});

function navTag(view) {
  const match = indexSrc.match(new RegExp(`<button[^>]*data-workspace-view="${view}"[^>]*>`));
  assert.ok(match, `missing nav ${view}`);
  return match[0];
}

function billingVisibility({perms, accessV3}) {
  const match = indexSrc.match(/<button[^>]*data-workspace-tab="billing"[^>]*>/);
  assert.ok(match, "missing billing workspace tab");
  const perm = /data-perm="([^"]+)"/.exec(match[0])?.[1];
  assert.strictEqual(perm, "billing.view");
  const billingTab = {hidden: false, dataset: {perm}};
  // role_permissions.js 会动态创建权限复选框；它不属于导航显隐范围。
  const rolePermissionCheckbox = {hidden: false, dataset: {perm: "billing.view"}};
  const document = {
    querySelectorAll(selector) {
      if (selector === ".nav-item[data-perm], .workspace-tab[data-perm]"
          || selector === ".workspace-tab[data-perm]") return [billingTab];
      if (selector === "[data-perm]") return [billingTab, rolePermissionCheckbox];
      return [];
    },
  };
  const sandbox = {
    document,
    __accessV3: accessV3,
    __isSystemAdmin: false,
    __userPerms: new Set(perms),
  };
  sandbox.window = sandbox;
  vm.createContext(sandbox);
  vm.runInContext(permissionHelpers + permissionVisibility, sandbox);
  sandbox.applyPermVisibility();
  return {billingTab, rolePermissionCheckbox};
}

test("收费工作区 tab 按 billing.view 显隐且不碰角色权限复选框", () => {
  const denied = billingVisibility({perms: [], accessV3: true});
  assert.strictEqual(denied.billingTab.hidden, true);
  assert.strictEqual(denied.rolePermissionCheckbox.hidden, false);

  const allowed = billingVisibility({perms: ["billing.view"], accessV3: true});
  assert.strictEqual(allowed.billingTab.hidden, false);
  assert.strictEqual(allowed.rolePermissionCheckbox.hidden, false);

  const legacy = billingVisibility({perms: ["billing.view"], accessV3: false});
  assert.strictEqual(legacy.billingTab.hidden, false);
  assert.strictEqual(legacy.rolePermissionCheckbox.hidden, false);
});

test("顶层管理入口同时声明 V3 view/manage 键与旧键", () => {
  const sterilization = navTag("sterilization");
  assert.match(sterilization, /instrument\.view/);
  assert.match(sterilization, /sterilization_order\.view/);
  assert.match(sterilization, /sterilize\.manage/);

  const warehouse = navTag("warehouse");
  assert.match(warehouse, /warehouse\.inventory\.view/);
  assert.match(warehouse, /warehouse\.item\.view/);
  assert.match(warehouse, /warehouse\.view/);

  const reports = navTag("data-reports");
  assert.match(reports, /report\.finance\.view/);
  assert.match(reports, /report\.operations\.view/);
  assert.match(reports, /report\.view/);

  const config = navTag("config");
  for (const key of [
    "clinic_settings.manage", "staff.view", "account.view", "master_data.view",
    "audit.view", "recycle.view", "settings.manage", "staff.manage", "user.manage",
  ]) assert.ok(config.includes(key), key);
});

function configKeys({perms, accessV3, systemAdmin = false, role = "doctor"}) {
  const sandbox = {
    hasPerm: key => systemAdmin || perms.has(key),
    __accessV3: accessV3,
    __isSystemAdmin: systemAdmin,
    __userRole: role,
    __capabilities: {sync: true},
  };
  sandbox.window = sandbox;
  vm.createContext(sandbox);
  const fn = configSrc.slice(
    configSrc.indexOf("function _cfgTabDefs"),
    configSrc.indexOf("function loadConfigModule"),
  );
  vm.runInContext(fn + "\nthis._cfgTabDefs = _cfgTabDefs;", sandbox);
  return Array.from(sandbox._cfgTabDefs(), item => item.key);
}

function makeConfigContainer() {
  let html = "";
  const container = {
    buttons: [],
    regions: new Map(),
    get innerHTML() { return html; },
    set innerHTML(value) {
      html = String(value);
      container.regions = new Map(Array.from(
        html.matchAll(/data-rp-region="([^"]+)"/g),
        match => [match[1], {innerHTML: "", textContent: ""}],
      ));
      container.buttons = Array.from(html.matchAll(/<button\b([^>]*)>/g), match => {
        const attrs = match[1];
        const cfgTab = /data-cfg-tab="([^"]+)"/.exec(attrs)?.[1];
        const accessTab = /data-access-center-tab="([^"]+)"/.exec(attrs)?.[1];
        const classes = new Set((/class="([^"]*)"/.exec(attrs)?.[1] || "").split(/\s+/).filter(Boolean));
        const listeners = new Map();
        return {
          dataset: {
            ...(cfgTab ? {cfgTab} : {}),
            ...(accessTab ? {accessCenterTab: accessTab} : {}),
          },
          classList: {
            contains: name => classes.has(name),
            toggle(name, force) {
              if (force) classes.add(name);
              else classes.delete(name);
            },
          },
          addEventListener(type, handler) { listeners.set(type, handler); },
          click() { return listeners.get("click")?.(); },
        };
      });
    },
    querySelector(selector) {
      const region = /^\[data-rp-region="([^"]+)"\]$/.exec(selector)?.[1];
      return region ? container.regions.get(region) || null : null;
    },
    querySelectorAll(selector) {
      if (selector === "[data-cfg-tab]") return container.buttons.filter(b => b.dataset.cfgTab);
      if (selector === "[data-access-center-tab]") {
        return container.buttons.filter(b => b.dataset.accessCenterTab);
      }
      return [];
    },
  };
  return container;
}

function makeRoleElement(initial = {}) {
  const listeners = new Map();
  return {
    hidden: false,
    value: "",
    textContent: "",
    style: {},
    removed: false,
    focus() {},
    select() {},
    addEventListener(type, handler) { listeners.set(type, handler); },
    click() { return listeners.get("click")?.(); },
    remove() { this.removed = true; },
    ...initial,
  };
}

function makeWorkspaceNavButton(view, active = false) {
  const classes = new Set(active ? ["active"] : []);
  return {
    hidden: false,
    dataset: {workspaceView: view},
    classList: {
      contains: name => classes.has(name),
      toggle(name, force) {
        if (force) classes.add(name);
        else classes.delete(name);
      },
    },
  };
}

function configUiSandbox({
  perms,
  accessV3 = true,
  systemAdmin = false,
  role = "doctor",
  includeRolePermissions = true,
  leaveRolePermissions,
  withWorkspaceNavigation = false,
}) {
  const navButtons = [
    makeWorkspaceNavButton("config", true),
    makeWorkspaceNavButton("warehouse"),
    makeWorkspaceNavButton("sterilization"),
    makeWorkspaceNavButton("today"),
  ];
  const elements = {
    modulePanel: makeConfigContainer(),
    cfgBody: makeConfigContainer(),
    accessCenterBody: makeConfigContainer(),
    cfgRolePerms: makeConfigContainer(),
    rpMsg: makeRoleElement(),
    rpV3Confirm: makeRoleElement({hidden: true}),
    rpPasswordStep: makeRoleElement(),
    rpCurrentPassword: makeRoleElement(),
    rpReasonStep: makeRoleElement({hidden: true}),
    rpReason: makeRoleElement(),
    aiDraftsPanel: makeRoleElement({hidden: true}),
    patientWorkspacePage: makeRoleElement({hidden: true}),
    patientWorkspace: makeRoleElement({hidden: true}),
    todayWorkPanel: makeRoleElement({hidden: true}),
    workspaceTitle: makeRoleElement({textContent: "配置管理"}),
    workspaceSubtitle: makeRoleElement({textContent: "配置副标题"}),
  };
  let sandbox;
  let doctorCheckedFallback = true;
  const doctorCheckbox = {dataset: {role: "doctor", perm: "staff.view"}};
  Object.defineProperty(doctorCheckbox, "checked", {
    get() {
      if (typeof sandbox?.rolePermissionDiff !== "function") return doctorCheckedFallback;
      return !Array.from(sandbox.rolePermissionDiff().removed).includes("staff.view");
    },
    set(value) {
      doctorCheckedFallback = Boolean(value);
      if (typeof sandbox?.togglePermission === "function") {
        sandbox.togglePermission("staff.view", Boolean(value));
      }
    },
  });
  const roleCheckboxes = [
    doctorCheckbox,
    {dataset: {role: "nurse", perm: "staff.view"}, checked: false},
  ];
  const overlays = [];
  const keydownListeners = new Set();
  const calls = {
    staff: 0,
    newStaffForm: 0,
    roles: 0,
    staffRequests: 0,
    roleRequests: 0,
    warehouse: 0,
    sterilization: 0,
    hidePatientWorkspace: 0,
    showPatientWorkspace: 0,
    shownPatients: [],
  };
  const roleMatrix = {
    roles: [
      {role_key: "doctor", name: "医生", is_system: true, assigned_staff_count: 2},
      {role_key: "nurse", name: "护士", is_system: true, assigned_staff_count: 1},
    ],
    perms: [{
      key: "staff.view",
      module: "人员管理",
      object: "人员档案",
      label: "查看",
      description: "查看人员档案",
      risk: "normal",
      dependencies: [],
    }],
    matrix: {doctor: {"staff.view": true}, nurse: {"staff.view": false}},
    system_admin_capabilities: [
      {key: "system_admin.role_permissions.manage", label: "修改角色权限", assignable: false},
      {key: "system_admin.grant", label: "授予/撤销系统管理员", assignable: false},
      {key: "system_admin.backup.download", label: "下载数据库备份", assignable: false},
      {key: "system_admin.sync.run", label: "执行全量同步", assignable: false},
      {key: "system_admin.migration.run", label: "执行数据迁移", assignable: false},
    ],
  };
  const createOverlay = () => {
    const cancel = makeRoleElement();
    const confirm = makeRoleElement();
    const overlay = makeRoleElement({
      querySelector(selector) {
        if (selector === '[data-rp-leave="cancel"]') return cancel;
        if (selector === '[data-rp-leave="confirm"]') return confirm;
        return null;
      },
    });
    overlay.remove = () => {
      overlay.removed = true;
      const index = overlays.indexOf(overlay);
      if (index >= 0) overlays.splice(index, 1);
    };
    return overlay;
  };
  sandbox = {
    document: {
      getElementById: id => elements[id] || null,
      querySelector(selector) {
        if (selector === ".nav-item.active[data-workspace-view]") {
          return navButtons.find(button => button.classList.contains("active")) || null;
        }
        return null;
      },
      querySelectorAll: selector => (
        selector === ".rp-table input[type=checkbox][data-role]"
          ? roleCheckboxes
          : (selector === ".nav-item[data-workspace-view]" ? navButtons : [])
      ),
      createElement: () => createOverlay(),
      body: {appendChild(overlay) { overlays.push(overlay); }},
      addEventListener(type, handler) {
        if (type === "keydown") keydownListeners.add(handler);
      },
      removeEventListener(type, handler) {
        if (type === "keydown") keydownListeners.delete(handler);
      },
    },
    escapeHtml: value => String(value ?? "")
      .replaceAll("&", "&amp;")
      .replaceAll('"', "&quot;")
      .replaceAll("<", "&lt;")
      .replaceAll(">", "&gt;"),
    hasPerm: key => systemAdmin || perms.has(key),
    hasAnyPerm: (...keys) => keys.some(key => systemAdmin || perms.has(key)),
    canAccessAccounts: () => [
      "account.view", "account.open", "account.security", "user.manage",
    ].some(key => systemAdmin || perms.has(key)),
    renderStaffInto: async () => {
      calls.staff += 1;
      await sandbox.fetch("/api/staff-admin/list");
    },
    openNewStaffForm: () => {
      calls.newStaffForm += 1;
      return true;
    },
    renderRolePermsInto: async () => { calls.roles += 1; },
    fetch: async url => {
      if (url === "/api/staff-admin/list") calls.staffRequests += 1;
      if (url === "/api/role-permissions") {
        calls.roleRequests += 1;
        return {ok: true, status: 200, json: async () => roleMatrix};
      }
      return {ok: true, status: 200, json: async () => ({})};
    },
    __accessV3: accessV3,
    __isSystemAdmin: systemAdmin,
    __userRole: role,
    __userPerms: perms,
    __capabilities: {sync: true},
    q: {value: ""},
    todayWorkPanel: elements.todayWorkPanel,
    modulePanel: elements.modulePanel,
    patientWorkspace: elements.patientWorkspace,
    workspaceTitle: elements.workspaceTitle,
    workspaceSubtitle: elements.workspaceSubtitle,
    location: {hash: ""},
    history: {length: 1, back() {}},
    hidePatientWorkspacePage: () => {
      calls.hidePatientWorkspace += 1;
      elements.patientWorkspacePage.hidden = true;
    },
    showPatientWorkspacePage(identity, tab) {
      calls.showPatientWorkspace += 1;
      calls.shownPatients.push([identity, tab]);
      elements.patientWorkspacePage.hidden = false;
    },
    syncTodayActions() {},
    loadWarehouseModule: () => {
      calls.warehouse += 1;
      elements.modulePanel.innerHTML = "<div>warehouse-loaded</div>";
    },
    loadSterilizationModule: () => {
      calls.sterilization += 1;
      elements.modulePanel.innerHTML = "<div>sterilization-loaded</div>";
    },
  };
  if (leaveRolePermissions !== undefined) {
    sandbox.requestLeaveRolePermissions = leaveRolePermissions;
  }
  sandbox.window = sandbox;
  vm.createContext(sandbox);
  if (includeRolePermissions) {
    vm.runInContext(rolePermissionsSrc, sandbox);
    vm.runInContext("this.__setRoleSaveInFlight = _setRolePermissionSaveInFlight;", sandbox);
    const renderRolePermsInto = sandbox.renderRolePermsInto;
    sandbox.renderRolePermsInto = async (...args) => {
      calls.roles += 1;
      return renderRolePermsInto(...args);
    };
  }
  vm.runInContext(configSrc + `
    this.__getConfigState = () => ({
      cfgTab: _cfgTab,
      accessCenterTab: typeof _accessCenterTab === "undefined" ? null : _accessCenterTab,
    });
    this.__setConfigState = (cfgTab, accessCenterTab) => {
      _cfgTab = cfgTab;
      if (typeof _accessCenterTab !== "undefined") _accessCenterTab = accessCenterTab;
    };
    this.__accessCenterTabs = typeof _accessCenterTabs === "function" ? _accessCenterTabs : null;
    this.__renderAccessCenter = typeof renderAccessCenter === "function" ? renderAccessCenter : null;
  `, sandbox);
  if (withWorkspaceNavigation) {
    vm.runInContext(workspaceNavigationSrc + `
      _authenticatedBusinessReady = true;
      this.__getWorkspaceState = () => ({activeModuleView, modulePage, moduleSearchSeed});
      this.__setWorkspaceView = view => { activeModuleView = view; };
    `, sandbox);
    sandbox.__setWorkspaceView("config");
  }
  return {
    sandbox,
    elements,
    calls,
    roleCheckboxes,
    overlays,
    keydownListeners,
    navButtons,
  };
}

async function flushConfigRender() {
  for (let index = 0; index < 8; index += 1) await Promise.resolve();
}

function buttonFor(container, datasetKey, value) {
  return container.buttons.find(button => button.dataset[datasetKey] === value);
}

function assertRolesAccessCenter(target) {
  const roleButton = buttonFor(target.elements.cfgBody, "accessCenterTab", "roles");
  const staffButton = buttonFor(target.elements.cfgBody, "accessCenterTab", "staff");
  assert.ok(roleButton, "roles button should exist");
  assert.ok(staffButton, "staff button should exist");
  assert.ok(roleButton.classList.contains("active"), "roles button should be active");
  assert.ok(!staffButton.classList.contains("active"), "staff button should be inactive");
  assert.match(target.elements.accessCenterBody.innerHTML, /<span>角色与权限<\/span>/);
  assert.doesNotMatch(target.elements.accessCenterBody.innerHTML, /<span>人员<\/span>/);
  return {roleButton, staffButton};
}

test("V3 普通用户按新配置权限显示，角色/同步/备份不向普通用户开放", () => {
  const keys = configKeys({
    accessV3: true,
    perms: new Set([
      "clinic_settings.manage", "staff.view", "account.view", "master_data.view",
      "audit.view", "recycle.view", "role.manage", "sync.manage",
    ]),
  });
  for (const key of ["shop", "access", "charge-items", "audit", "recycle"]) {
    assert.ok(keys.includes(key), key);
  }
  for (const key of ["staff", "roles", "sync", "images", "backup"]) {
    assert.ok(!keys.includes(key), key);
  }
  assert.strictEqual(keys.filter(key => key === "access").length, 1);
});

test("V3 system admin 拥有角色、同步和备份入口", () => {
  const keys = configKeys({accessV3: true, systemAdmin: true, perms: new Set()});
  for (const key of ["access", "sync", "images", "backup"]) {
    assert.ok(keys.includes(key), key);
  }
  assert.ok(!keys.includes("staff"));
  assert.ok(!keys.includes("roles"));
});

test("旧配置权限语义继续保留", () => {
  const keys = configKeys({
    accessV3: false,
    role: "reception",
    perms: new Set([
      "settings.manage", "staff.manage", "user.manage", "role.manage",
      "master_data.manage", "sync.manage", "audit.view", "recycle.manage",
    ]),
  });
  for (const key of [
    "shop", "access", "charge-items", "sync", "images", "audit", "recycle",
  ]) assert.ok(keys.includes(key), key);
  assert.ok(!keys.includes("staff"));
  assert.ok(!keys.includes("roles"));
  assert.ok(!keys.includes("backup"));
});

test("人员与权限中心按四种权限组合生成内部页签并正确隐藏单页签导航", async () => {
  const cases = [
    {
      label: "V3 普通人员",
      options: {perms: new Set(["staff.view"])},
      tabs: ["staff"],
      rendered: "staff",
      hidden: true,
    },
    {
      label: "V3 仅 staff.manage 的普通人员",
      options: {perms: new Set(["staff.manage"])},
      tabs: ["staff"],
      rendered: "staff",
      hidden: true,
    },
    {
      label: "V3 系统管理员无直接 staff 权限也放行(超级用户/空库首启死锁解)",
      options: {perms: new Set(), systemAdmin: true},
      tabs: ["staff", "roles"],
      rendered: "staff",
      hidden: false,
    },
    {
      label: "V3 系统管理员同时有直接 staff 权限",
      options: {perms: new Set(["staff.view"]), systemAdmin: true},
      tabs: ["staff", "roles"],
      rendered: "staff",
      hidden: false,
    },
    {
      label: "V3 仅账号权限",
      options: {perms: new Set(["account.view"])},
      tabs: [],
      rendered: null,
      hidden: true,
    },
  ];

  for (const {label, options, tabs, rendered, hidden} of cases) {
    const {sandbox, elements, calls} = configUiSandbox(options);
    assert.strictEqual(typeof sandbox.__accessCenterTabs, "function", label);
    assert.deepStrictEqual(
      Array.from(sandbox.__accessCenterTabs(), tab => tab.key),
      tabs,
      label,
    );
    sandbox.__setConfigState("access", "staff");
    assert.strictEqual(typeof sandbox.__renderAccessCenter, "function", label);
    await sandbox.__renderAccessCenter();

    assert.match(elements.cfgBody.innerHTML, /人员与权限中心/, label);
    assert.match(elements.cfgBody.innerHTML, /id="accessCenterBody"/, label);
    const tabsHidden = /class="module-subtabs access-center-tabs" hidden/.test(
      elements.cfgBody.innerHTML,
    );
    assert.strictEqual(tabsHidden, hidden, label);
    assert.strictEqual(calls.staff, rendered === "staff" ? 1 : 0, label);
    assert.strictEqual(calls.roles, rendered === "roles" ? 1 : 0, label);
  }
});

test("仅账号权限显示最小账号管理入口且不加载人员档案", async () => {
  const cases = [
    ["account.view", true],
    ["account.open", true],
    ["account.security", true],
    ["user.manage", false],
  ];
  for (const [permission, accessV3] of cases) {
    assert.ok(configKeys({perms: new Set([permission]), accessV3}).includes("access"), permission);
    const {sandbox, elements, calls} = configUiSandbox({
      perms: new Set([permission]),
      accessV3,
    });
    sandbox.__setConfigState("access", "staff");
    await sandbox.__renderAccessCenter();
    assert.match(elements.accessCenterBody.innerHTML, /仅有账号管理权限/, permission);
    assert.match(elements.accessCenterBody.innerHTML, /openAccountManager\(\)/, permission);
    assert.strictEqual(calls.staff, 0, permission);
    assert.strictEqual(calls.staffRequests, 0, permission);
  }
});

test("系统管理员无直接 staff 授权仍见人员页且保留账号管理入口", async () => {
  const {sandbox, elements, calls} = configUiSandbox({
    perms: new Set(),
    systemAdmin: true,
  });
  assert.deepStrictEqual(
    {...sandbox.__getConfigState()},
    {cfgTab: "access", accessCenterTab: "staff"},
  );
  await sandbox.__renderAccessCenter();
  assert.deepStrictEqual(Array.from(sandbox.__accessCenterTabs(), tab => tab.key), ["staff", "roles"]);
  assert.match(elements.accessCenterBody.innerHTML, /openAccountManager\(\)/);
  assert.strictEqual(calls.staff, 1);
});

test("系统管理员从账号入口切到人员页并打开现有新建表单", async () => {
  const {sandbox, calls} = configUiSandbox({
    perms: new Set(),
    systemAdmin: true,
  });
  sandbox.__setConfigState("access", "roles");

  const result = await sandbox.openNewStaffFromConfig();

  assert.strictEqual(result, true);
  assert.deepStrictEqual(
    {...sandbox.__getConfigState()},
    {cfgTab: "access", accessCenterTab: "staff"},
  );
  assert.strictEqual(calls.staff, 1);
  assert.strictEqual(calls.staffRequests, 1);
  assert.strictEqual(calls.newStaffForm, 1);
});

test("系统管理员取消离开角色权限时不切人员页也不打开新建表单", async () => {
  const {sandbox, calls} = configUiSandbox({
    perms: new Set(),
    systemAdmin: true,
    includeRolePermissions: false,
    leaveRolePermissions: async () => false,
  });
  sandbox.__setConfigState("access", "roles");

  const result = await sandbox.openNewStaffFromConfig();

  assert.strictEqual(result, null);
  assert.deepStrictEqual(
    {...sandbox.__getConfigState()},
    {cfgTab: "access", accessCenterTab: "roles"},
  );
  assert.strictEqual(calls.staff, 0);
  assert.strictEqual(calls.staffRequests, 0);
  assert.strictEqual(calls.newStaffForm, 0);
});

test("无 staff.edit 的普通用户不能程序化打开新建人员表单", async () => {
  let guardCalls = 0;
  const {sandbox, elements, calls} = configUiSandbox({
    perms: new Set(["staff.view"]),
    systemAdmin: false,
    includeRolePermissions: false,
    leaveRolePermissions: async () => {
      guardCalls += 1;
      return true;
    },
  });
  sandbox.__setConfigState("access", "roles");
  const beforeState = {...sandbox.__getConfigState()};
  const beforeHtml = elements.accessCenterBody.innerHTML;

  const result = await sandbox.openNewStaffFromConfig();

  assert.strictEqual(result, false);
  assert.deepStrictEqual({...sandbox.__getConfigState()}, beforeState);
  assert.strictEqual(calls.staff, 0);
  assert.strictEqual(calls.staffRequests, 0);
  assert.strictEqual(calls.newStaffForm, 0);
  assert.strictEqual(guardCalls, 0);
  assert.strictEqual(elements.accessCenterBody.innerHTML, beforeHtml);
});

test("离开配置工作区后账号入口不能使用持久化配置状态打开新增人员", async () => {
  let guardCalls = 0;
  const target = configUiSandbox({
    perms: new Set(),
    systemAdmin: true,
    includeRolePermissions: false,
    leaveRolePermissions: async () => {
      guardCalls += 1;
      return true;
    },
  });
  target.sandbox.__setConfigState("access", "roles");
  for (const button of target.navButtons) {
    button.classList.toggle("active", button.dataset.workspaceView === "today");
  }
  const beforeState = {...target.sandbox.__getConfigState()};
  const beforeHtml = target.elements.accessCenterBody.innerHTML;

  const result = await target.sandbox.openNewStaffFromConfig();

  assert.strictEqual(result, false);
  assert.deepStrictEqual({...target.sandbox.__getConfigState()}, beforeState);
  assert.strictEqual(target.calls.staff, 0);
  assert.strictEqual(target.calls.staffRequests, 0);
  assert.strictEqual(target.calls.newStaffForm, 0);
  assert.strictEqual(guardCalls, 0);
  assert.strictEqual(target.elements.accessCenterBody.innerHTML, beforeHtml);
});

test("人员组件渲染抛错时新增人员入口返回 false 并恢复内页状态", async () => {
  const target = configUiSandbox({
    perms: new Set(),
    systemAdmin: true,
    includeRolePermissions: false,
    leaveRolePermissions: async () => true,
  });
  target.sandbox.__setConfigState("access", "roles");
  await target.sandbox.__renderAccessCenter();
  assertRolesAccessCenter(target);
  target.sandbox.renderStaffInto = async () => {
    target.calls.staff += 1;
    throw new Error("staff render failed");
  };

  const result = await target.sandbox.openNewStaffFromConfig();

  assert.strictEqual(result, false);
  assert.deepStrictEqual(
    {...target.sandbox.__getConfigState()},
    {cfgTab: "access", accessCenterTab: "roles"},
  );
  const {roleButton} = assertRolesAccessCenter(target);
  assert.strictEqual(target.calls.staff, 1);
  assert.strictEqual(target.calls.staffRequests, 0);
  assert.strictEqual(target.calls.newStaffForm, 0);
  assert.strictEqual(target.calls.roles, 2);

  await roleButton.click();
  assertRolesAccessCenter(target);
});

test("新增人员表单抛错时入口返回 false 并恢复内页状态", async () => {
  const target = configUiSandbox({
    perms: new Set(),
    systemAdmin: true,
    includeRolePermissions: false,
    leaveRolePermissions: async () => true,
  });
  target.sandbox.__setConfigState("access", "roles");
  await target.sandbox.__renderAccessCenter();
  assertRolesAccessCenter(target);
  target.sandbox.openNewStaffForm = () => {
    target.calls.newStaffForm += 1;
    throw new Error("new staff form failed");
  };

  const result = await target.sandbox.openNewStaffFromConfig();

  assert.strictEqual(result, false);
  assert.deepStrictEqual(
    {...target.sandbox.__getConfigState()},
    {cfgTab: "access", accessCenterTab: "roles"},
  );
  const {roleButton} = assertRolesAccessCenter(target);
  assert.strictEqual(target.calls.staff, 1);
  assert.strictEqual(target.calls.staffRequests, 1);
  assert.strictEqual(target.calls.newStaffForm, 1);
  assert.strictEqual(target.calls.roles, 2);

  await roleButton.click();
  assertRolesAccessCenter(target);
});

test("真实角色权限守卫取消时保留外层、内层和 DOM，确认后恢复基线再离开", async () => {
  const guarded = configUiSandbox({
    perms: new Set(["staff.view"]),
    systemAdmin: true,
  });
  guarded.sandbox.__setConfigState("access", "roles");
  guarded.sandbox.loadConfigModule();
  await flushConfigRender();
  assert.strictEqual(typeof guarded.sandbox.requestLeaveRolePermissions, "function");
  assert.strictEqual(guarded.calls.roleRequests, 1);

  const roleButton = buttonFor(guarded.elements.cfgBody, "accessCenterTab", "roles");
  const staffButton = buttonFor(guarded.elements.cfgBody, "accessCenterTab", "staff");
  const accessButton = buttonFor(guarded.elements.modulePanel, "cfgTab", "access");
  const chargeItemsButton = buttonFor(guarded.elements.modulePanel, "cfgTab", "charge-items");
  assert.ok(roleButton?.classList.contains("active"));
  assert.ok(accessButton?.classList.contains("active"));

  guarded.roleCheckboxes[0].checked = false;
  const innerLeave = staffButton.click();
  await Promise.resolve();
  assert.strictEqual(guarded.overlays.length, 1);
  guarded.overlays[0].querySelector('[data-rp-leave="cancel"]').click();
  await innerLeave;
  assert.deepStrictEqual(
    {...guarded.sandbox.__getConfigState()},
    {cfgTab: "access", accessCenterTab: "roles"},
  );
  assert.ok(roleButton.classList.contains("active"));
  assert.ok(!staffButton.classList.contains("active"));
  assert.strictEqual(guarded.roleCheckboxes[0].checked, false);
  assert.strictEqual(guarded.overlays.length, 0);
  assert.strictEqual(guarded.keydownListeners.size, 0);

  const outerLeave = chargeItemsButton.click();
  await Promise.resolve();
  assert.strictEqual(guarded.overlays.length, 1);
  guarded.overlays[0].querySelector('[data-rp-leave="cancel"]').click();
  await outerLeave;
  assert.deepStrictEqual(
    {...guarded.sandbox.__getConfigState()},
    {cfgTab: "access", accessCenterTab: "roles"},
  );
  assert.ok(accessButton.classList.contains("active"));
  assert.ok(!chargeItemsButton.classList.contains("active"));
  assert.strictEqual(guarded.roleCheckboxes[0].checked, false);

  const confirmedLeave = chargeItemsButton.click();
  await Promise.resolve();
  assert.strictEqual(guarded.overlays.length, 1);
  guarded.overlays[0].querySelector('[data-rp-leave="confirm"]').click();
  await confirmedLeave;
  assert.deepStrictEqual(
    {...guarded.sandbox.__getConfigState()},
    {cfgTab: "charge-items", accessCenterTab: "roles"},
  );
  assert.strictEqual(guarded.roleCheckboxes[0].checked, true);
  assert.ok(chargeItemsButton.classList.contains("active"));
  assert.ok(!accessButton.classList.contains("active"));
  assert.strictEqual(guarded.overlays.length, 0);
  assert.strictEqual(guarded.keydownListeners.size, 0);
});

test("角色离开守卫缺失、抛错或非 true 时配置中心均 fail-closed", async () => {
  const cases = [
    ["缺失", undefined],
    ["抛错", async () => { throw new Error("guard failed"); }],
    ["返回 undefined", async () => undefined],
    ["返回 false", async () => false],
  ];
  for (const [label, leaveRolePermissions] of cases) {
    const unguarded = configUiSandbox({
      perms: new Set(["staff.view"]),
      systemAdmin: true,
      includeRolePermissions: false,
      leaveRolePermissions,
    });
    unguarded.sandbox.__setConfigState("access", "roles");
    unguarded.sandbox.loadConfigModule();
    await flushConfigRender();
    await buttonFor(unguarded.elements.cfgBody, "accessCenterTab", "staff").click();
    assert.deepStrictEqual(
      {...unguarded.sandbox.__getConfigState()},
      {cfgTab: "access", accessCenterTab: "roles"},
      label,
    );
  }
});

async function workspaceRoleSandbox() {
  const guarded = configUiSandbox({
    perms: new Set(["staff.view"]),
    systemAdmin: true,
    withWorkspaceNavigation: true,
  });
  guarded.sandbox.__setConfigState("access", "roles");
  guarded.sandbox.loadConfigModule();
  await flushConfigRender();
  assert.strictEqual(guarded.calls.roleRequests, 1);
  guarded.elements.workspaceTitle.textContent = "配置管理";
  guarded.elements.workspaceSubtitle.textContent = "配置副标题";
  return guarded;
}

function workspaceSnapshot(guarded) {
  return {
    state: {...guarded.sandbox.__getWorkspaceState()},
    activeNav: guarded.navButtons
      .filter(button => button.classList.contains("active"))
      .map(button => button.dataset.workspaceView),
    title: guarded.elements.workspaceTitle.textContent,
    subtitle: guarded.elements.workspaceSubtitle.textContent,
    panelHtml: guarded.elements.modulePanel.innerHTML,
    panelHidden: Boolean(guarded.elements.modulePanel.hidden),
    patientPageHidden: Boolean(guarded.elements.patientWorkspacePage.hidden),
    patientListHidden: Boolean(guarded.elements.patientWorkspace.hidden),
    todayHidden: Boolean(guarded.elements.todayWorkPanel.hidden),
    checkboxChecked: guarded.roleCheckboxes[0].checked,
    hash: guarded.sandbox.location.hash,
  };
}

test("患者 hash 进入复用真实角色守卫，取消零提交、确认后才写 hash 和显示患者页", async () => {
  const guarded = await workspaceRoleSandbox();
  guarded.roleCheckboxes[0].checked = false;
  const before = workspaceSnapshot(guarded);

  const cancelled = guarded.sandbox.openPatientWorkspace("synthetic-patient", "profile");
  await Promise.resolve();
  assert.strictEqual(guarded.overlays.length, 1);
  assert.deepStrictEqual(workspaceSnapshot(guarded), before);
  assert.strictEqual(guarded.calls.showPatientWorkspace, 0);
  guarded.overlays[0].querySelector('[data-rp-leave="cancel"]').click();

  assert.strictEqual(await cancelled, false);
  assert.deepStrictEqual(workspaceSnapshot(guarded), before);
  assert.strictEqual(guarded.calls.showPatientWorkspace, 0);

  const confirmed = guarded.sandbox.openPatientWorkspace("synthetic-patient", "profile");
  await Promise.resolve();
  assert.strictEqual(guarded.overlays.length, 1);
  assert.deepStrictEqual(workspaceSnapshot(guarded), before);
  guarded.overlays[0].querySelector('[data-rp-leave="confirm"]').click();

  assert.strictEqual(await confirmed, true);
  assert.strictEqual(guarded.sandbox.location.hash, "#patient/synthetic-patient/profile");
  assert.strictEqual(guarded.elements.patientWorkspacePage.hidden, false);
  assert.strictEqual(guarded.elements.modulePanel.hidden, true);
  assert.strictEqual(guarded.elements.patientWorkspace.hidden, true);
  assert.strictEqual(guarded.elements.todayWorkPanel.hidden, true);
  assert.deepStrictEqual(guarded.calls.shownPatients, [["synthetic-patient", "profile"]]);
  assert.strictEqual(guarded.roleCheckboxes[0].checked, true);
  assert.deepStrictEqual(
    guarded.navButtons
      .filter(button => button.classList.contains("active"))
      .map(button => button.dataset.workspaceView),
    ["config"],
  );
});

test("角色权限保存中患者 hash 进入 fail-closed 且不改 hash 或 DOM", async () => {
  const guarded = await workspaceRoleSandbox();
  const before = workspaceSnapshot(guarded);
  const messageBefore = guarded.elements.rpMsg.textContent;
  guarded.sandbox.__setRoleSaveInFlight(true);

  assert.strictEqual(
    await guarded.sandbox.openPatientWorkspace("synthetic-patient", "profile"),
    false,
  );
  assert.deepStrictEqual(workspaceSnapshot(guarded), before);
  assert.strictEqual(guarded.calls.showPatientWorkspace, 0);
  assert.strictEqual(guarded.overlays.length, 0);
  assert.strictEqual(guarded.elements.rpMsg.textContent, messageBefore);
  guarded.sandbox.__setRoleSaveInFlight(false);
});

test("患者 hash 快速重复进入共用确认且只提交最新目标", async () => {
  const guarded = await workspaceRoleSandbox();
  guarded.roleCheckboxes[0].checked = false;

  const first = guarded.sandbox.openPatientWorkspace("synthetic-first", "profile");
  await Promise.resolve();
  assert.strictEqual(guarded.overlays.length, 1);
  const second = guarded.sandbox.openPatientWorkspace("synthetic-latest", "billing");
  await Promise.resolve();
  assert.strictEqual(guarded.overlays.length, 1);
  guarded.overlays[0].querySelector('[data-rp-leave="confirm"]').click();

  assert.strictEqual(await first, false);
  assert.strictEqual(await second, true);
  assert.strictEqual(guarded.sandbox.location.hash, "#patient/synthetic-latest/billing");
  assert.deepStrictEqual(guarded.calls.shownPatients, [["synthetic-latest", "billing"]]);
});

test("患者 hash 返回在守卫决议前不隐藏，取消后恢复已显示患者页", async () => {
  const guarded = await workspaceRoleSandbox();
  assert.strictEqual(
    await guarded.sandbox.openPatientWorkspace("synthetic-patient", "profile"),
    true,
  );
  const patientHash = guarded.sandbox.location.hash;
  guarded.roleCheckboxes[0].checked = false;
  const patientDom = workspaceSnapshot(guarded);

  guarded.sandbox.location.hash = "";
  const returning = guarded.sandbox.handlePatientWorkspaceHash();
  await Promise.resolve();
  assert.strictEqual(guarded.overlays.length, 1);
  assert.strictEqual(guarded.elements.patientWorkspacePage.hidden, false);
  assert.strictEqual(guarded.elements.modulePanel.hidden, true);
  assert.strictEqual(guarded.calls.hidePatientWorkspace, 0);
  guarded.overlays[0].querySelector('[data-rp-leave="cancel"]').click();

  assert.strictEqual(await returning, false);
  assert.strictEqual(guarded.sandbox.location.hash, patientHash);
  assert.deepStrictEqual(workspaceSnapshot(guarded), patientDom);
  assert.strictEqual(guarded.calls.hidePatientWorkspace, 0);
});

test("直接患者 hashchange 在真实守卫决议期间恢复 URL 且取消零 DOM 提交", async () => {
  const guarded = await workspaceRoleSandbox();
  guarded.roleCheckboxes[0].checked = false;
  const before = workspaceSnapshot(guarded);
  const target = "#patient/synthetic-direct/profile";

  guarded.sandbox.location.hash = target;
  const handling = guarded.sandbox.handlePatientWorkspaceHash({
    oldURL: "http://local.test/",
    newURL: `http://local.test/${target}`,
  });
  await Promise.resolve();
  assert.strictEqual(guarded.overlays.length, 1);
  assert.deepStrictEqual(workspaceSnapshot(guarded), before);
  assert.strictEqual(guarded.calls.showPatientWorkspace, 0);
  guarded.overlays[0].querySelector('[data-rp-leave="cancel"]').click();

  assert.strictEqual(await handling, false);
  assert.deepStrictEqual(workspaceSnapshot(guarded), before);
  assert.strictEqual(guarded.calls.showPatientWorkspace, 0);
});

test("迟到 hashchange 的 newURL 不等于 live hash 时零守卫、零显隐、零提交", async () => {
  const guarded = await workspaceRoleSandbox();
  guarded.roleCheckboxes[0].checked = false;
  guarded.sandbox.location.hash = "#patient/synthetic-live/profile";
  const before = workspaceSnapshot(guarded);

  assert.strictEqual(await guarded.sandbox.handlePatientWorkspaceHash({
    oldURL: "http://local.test/",
    newURL: "http://local.test/#patient/synthetic-stale/profile",
  }), false);
  assert.deepStrictEqual(workspaceSnapshot(guarded), before);
  assert.strictEqual(guarded.overlays.length, 0);
  assert.strictEqual(guarded.calls.showPatientWorkspace, 0);
  assert.strictEqual(guarded.calls.hidePatientWorkspace, 0);
});

test("连续外部患者 hash 取消时恢复最后已提交路由而非未提交 oldURL", async () => {
  const guarded = await workspaceRoleSandbox();
  assert.strictEqual(
    await guarded.sandbox.openPatientWorkspace("synthetic-committed", "profile"),
    true,
  );
  const committedHash = guarded.sandbox.location.hash;
  guarded.roleCheckboxes[0].checked = false;
  const committed = workspaceSnapshot(guarded);

  const intermediate = "#patient/synthetic-intermediate/profile";
  const latest = "#patient/synthetic-latest/billing";
  guarded.sandbox.location.hash = intermediate;
  guarded.sandbox.location.hash = latest;
  const handling = guarded.sandbox.handlePatientWorkspaceHash({
    oldURL: `http://local.test/${intermediate}`,
    newURL: `http://local.test/${latest}`,
  });
  await Promise.resolve();

  assert.strictEqual(guarded.overlays.length, 1);
  assert.strictEqual(guarded.sandbox.location.hash, committedHash);
  assert.strictEqual(guarded.elements.patientWorkspacePage.hidden, false);
  assert.deepStrictEqual(guarded.calls.shownPatients, [["synthetic-committed", "profile"]]);
  guarded.overlays[0].querySelector('[data-rp-leave="cancel"]').click();

  assert.strictEqual(await handling, false);
  assert.deepStrictEqual(workspaceSnapshot(guarded), committed);
  assert.strictEqual(guarded.roleCheckboxes[0].checked, false);
});

test("配置离开接口缺失、抛错或非 true 时患者入口均 fail-closed", async () => {
  const cases = [
    ["缺失", undefined],
    ["抛错", async () => { throw new Error("config leave failed"); }],
    ["返回 undefined", async () => undefined],
    ["返回 false", async () => false],
  ];
  for (const [label, guard] of cases) {
    const current = configUiSandbox({
      perms: new Set(["staff.view"]),
      systemAdmin: true,
      withWorkspaceNavigation: true,
    });
    current.sandbox.requestLeaveConfigModule = guard;
    const before = workspaceSnapshot(current);

    assert.strictEqual(
      await current.sandbox.openPatientWorkspace("synthetic-patient", "profile"),
      false,
      label,
    );
    assert.deepStrictEqual(workspaceSnapshot(current), before, label);
    assert.strictEqual(current.calls.showPatientWorkspace, 0, label);
  }
});

test("患者工作区同一患者 ABA 乱序加载只允许最新 render revision 提交 tab", async () => {
  const page = makeRoleElement({hidden: true});
  const pendingLoads = [];
  const switchedTabs = [];
  const sandbox = {
    document: {getElementById: id => (id === "patientWorkspacePage" ? page : null)},
    activePatientId: "",
    resetWorkspacePanels() {},
    loadWorkspacePatient: () => new Promise(resolve => pendingLoads.push(resolve)),
    switchWorkspaceTab: tab => { switchedTabs.push(tab); },
  };
  sandbox.window = sandbox;
  vm.createContext(sandbox);
  vm.runInContext(patientWorkspaceLifecycleSrc, sandbox);

  const first = sandbox.showPatientWorkspacePage("synthetic-same", "medical");
  await Promise.resolve();
  assert.strictEqual(pendingLoads.length, 1);
  sandbox.hidePatientWorkspacePage();
  const latest = sandbox.showPatientWorkspacePage("synthetic-same", "billing");
  await Promise.resolve();
  assert.strictEqual(pendingLoads.length, 2);

  pendingLoads[1]();
  await latest;
  assert.deepStrictEqual(switchedTabs, ["billing"]);
  pendingLoads[0]();
  await first;
  assert.deepStrictEqual(switchedTabs, ["billing"]);
  assert.strictEqual(page.hidden, false);
});

test("患者资料旧保存 continuation 在同患者 ABA 后不得重载或切换最新 tab", async () => {
  let finishSave;
  const calls = {loads: [], tabs: [], audits: 0};
  const sandbox = {
    document: {
      getElementById: id => (id === "workspaceProfileSaveStatus" ? {textContent: ""} : null),
      querySelectorAll: selector => (selector === ".workspace-profile-input" ? [] : []),
    },
    fetch: () => new Promise(resolve => { finishSave = resolve; }),
    loadWorkspacePatient: async (...args) => { calls.loads.push(args); },
    switchWorkspaceTab: tab => { calls.tabs.push(tab); },
    loadAuditLogs: async () => { calls.audits += 1; },
  };
  sandbox.window = sandbox;
  vm.createContext(sandbox);
  vm.runInContext(`
    let workspacePatientId = "synthetic-patient";
    ${workspaceContinuationSrc}
    _workspaceRenderRevision = 1;
    let workspaceProfileEditing = true;
    const workspaceLoadedTabs = new Set(["profile"]);
    ${patientWorkspaceProfileSaveSrc}
    this.__workspaceABA = identity => {
      _workspaceRenderRevision += 1;
      workspacePatientId = "";
      _workspaceRenderRevision += 1;
      workspacePatientId = identity;
    };
  `, sandbox);

  const staleSave = sandbox.saveWorkspaceProfile("synthetic-patient");
  await Promise.resolve();
  sandbox.__workspaceABA("synthetic-patient");
  finishSave({ok: true});
  await staleSave;

  assert.deepStrictEqual(calls.loads, []);
  assert.deepStrictEqual(calls.tabs, []);
  assert.strictEqual(calls.audits, 0);
});

test("患者资料保存挂起后切到 billing，旧 continuation 不得强切回 profile", async () => {
  let finishSave;
  const calls = {loads: 0, audits: 0};
  const makeTab = (tab, active = false) => {
    const classes = new Set(active ? ["active"] : []);
    return {
      dataset: {workspaceTab: tab},
      classList: {
        contains: name => classes.has(name),
        toggle(name, force) {
          if (force) classes.add(name);
          else classes.delete(name);
        },
      },
    };
  };
  const makePanel = (tab, active = false) => {
    const classes = new Set(active ? ["active"] : []);
    return {
      dataset: {workspacePanel: tab},
      hidden: !active,
      classList: {
        toggle(name, force) {
          if (force) classes.add(name);
          else classes.delete(name);
        },
      },
    };
  };
  const tabs = [makeTab("profile", true), makeTab("billing")];
  const panels = [makePanel("profile", true), makePanel("billing")];
  const page = {
    querySelector(selector) {
      const tab = /^\[data-workspace-tab="([^"]+)"\]$/.exec(selector)?.[1];
      return tab ? tabs.find(button => button.dataset.workspaceTab === tab) || null : null;
    },
    querySelectorAll(selector) {
      if (selector === "[data-workspace-tab]") return tabs;
      if (selector === "[data-workspace-panel]") return panels;
      return [];
    },
  };
  const status = {textContent: ""};
  const sandbox = {
    document: {
      getElementById: id => (id === "workspaceProfileSaveStatus" ? status : null),
      querySelectorAll: selector => (selector === ".workspace-profile-input" ? [] : []),
    },
    fetch: () => new Promise(resolve => { finishSave = resolve; }),
    activePatientId: "",
    cssEscape: value => String(value),
  };
  sandbox.window = sandbox;
  vm.createContext(sandbox);
  vm.runInContext(patientWorkspaceSrc + `
    this.__prepareProfileTabSave = host => {
      _wsHost = host;
      workspacePatientId = "synthetic-patient";
      workspaceData = {};
      workspaceProfileEditing = true;
      workspaceLoadedTabs.clear();
      workspaceLoadedTabs.add("profile");
      workspaceLoadedTabs.add("billing");
      loadWorkspacePatient = async () => { this.__profileLoadCalls += 1; };
      loadAuditLogs = async () => { this.__profileAuditCalls += 1; };
    };
  `, sandbox);
  sandbox.__profileLoadCalls = 0;
  sandbox.__profileAuditCalls = 0;
  sandbox.__prepareProfileTabSave(page);

  const staleSave = sandbox.saveWorkspaceProfile("synthetic-patient");
  await Promise.resolve();
  sandbox.switchWorkspaceTab("billing");
  assert.strictEqual(tabs[1].classList.contains("active"), true);
  finishSave({ok: true});
  await staleSave;
  calls.loads = sandbox.__profileLoadCalls;
  calls.audits = sandbox.__profileAuditCalls;

  assert.strictEqual(tabs[1].classList.contains("active"), true);
  assert.strictEqual(tabs[0].classList.contains("active"), false);
  assert.strictEqual(calls.loads, 0);
  assert.strictEqual(calls.audits, 0);
});

test("患者资料保存挂起时重复点击当前 profile 不得让 continuation 失效", async () => {
  let finishSave;
  const status = {textContent: ""};
  const {page, tabs} = makeWorkspaceTabPage(["profile"], "profile");
  const sandbox = {
    document: {
      getElementById: id => (id === "workspaceProfileSaveStatus" ? status : null),
      querySelectorAll: selector => (selector === ".workspace-profile-input" ? [] : []),
    },
    fetch: () => new Promise(resolve => { finishSave = resolve; }),
    activePatientId: "",
    cssEscape: value => String(value),
  };
  sandbox.window = sandbox;
  vm.createContext(sandbox);
  vm.runInContext(patientWorkspaceSrc + `
    this.__prepareRepeatedProfileClick = host => {
      _wsHost = host;
      workspacePatientId = "synthetic-patient";
      workspaceData = {};
      workspaceProfileEditing = true;
      workspaceLoadedTabs.clear();
      workspaceLoadedTabs.add("profile");
      loadWorkspacePatient = async () => { this.__profileLoadCalls += 1; };
      loadWorkspaceTabPanel = tab => { this.__profileRenderTabs.push(tab); };
      loadAuditLogs = async () => { this.__profileAuditCalls += 1; };
    };
  `, sandbox);
  sandbox.__profileLoadCalls = 0;
  sandbox.__profileRenderTabs = [];
  sandbox.__profileAuditCalls = 0;
  sandbox.__prepareRepeatedProfileClick(page);

  const save = sandbox.saveWorkspaceProfile("synthetic-patient");
  await Promise.resolve();
  assert.strictEqual(status.textContent, "保存中...");
  sandbox.switchWorkspaceTab("profile");
  assert.strictEqual(tabs[0].classList.contains("active"), true);
  finishSave({ok: true});
  await save;

  assert.strictEqual(status.textContent, "已保存");
  assert.strictEqual(sandbox.__profileLoadCalls, 1);
  assert.deepStrictEqual(sandbox.__profileRenderTabs, ["profile"]);
  assert.strictEqual(sandbox.__profileAuditCalls, 1);
});

test("患者备注保存响应后刷新挂起，同患者 ABA 仍不得提交旧 continuation", async () => {
  let finishSave;
  let finishLoad;
  const calls = {loads: [], renders: 0, audits: 0};
  const noteText = {value: "合成备注"};
  const noteStatus = {textContent: ""};
  const notePanel = {};
  const sandbox = {
    document: {
      getElementById(id) {
        if (id === "noteText") return noteText;
        if (id === "noteSaveStatus") return noteStatus;
        return null;
      },
    },
    fetch: () => new Promise(resolve => { finishSave = resolve; }),
    loadWorkspacePatient: (...args) => {
      calls.loads.push(args);
      return new Promise(resolve => { finishLoad = resolve; });
    },
    workspacePageEl: () => ({
      querySelector: selector => (
        selector === '[data-workspace-panel="notes"]' ? notePanel : null
      ),
    }),
    renderWorkspaceNotesTab: panel => {
      assert.strictEqual(panel, notePanel);
      calls.renders += 1;
    },
    loadAuditLogs: async () => { calls.audits += 1; },
  };
  sandbox.window = sandbox;
  vm.createContext(sandbox);
  vm.runInContext(`
    let workspacePatientId = "synthetic-patient";
    ${workspaceContinuationSrc}
    _workspaceRenderRevision = 1;
    ${workspaceNoteSaveSrc}
    this.__workspaceABA = identity => {
      _workspaceRenderRevision += 1;
      workspacePatientId = "";
      _workspaceRenderRevision += 1;
      workspacePatientId = identity;
    };
  `, sandbox);

  const staleSave = sandbox.saveWorkspaceNote("synthetic-patient");
  await Promise.resolve();
  finishSave({ok: true});
  for (let index = 0; index < 4 && !finishLoad; index += 1) await Promise.resolve();
  assert.strictEqual(calls.loads.length, 1);
  assert.strictEqual(calls.loads[0][1], 1);
  sandbox.__workspaceABA("synthetic-patient");
  finishLoad();
  await staleSave;

  assert.strictEqual(calls.renders, 0);
  assert.strictEqual(calls.audits, 0);
});

test("患者备注保存挂起时重复点击当前 notes 不得让 continuation 失效", async () => {
  let finishSave;
  const calls = {loads: 0, renders: 0, audits: 0};
  const noteText = {value: "合成备注"};
  const noteStatus = {textContent: ""};
  const {page, tabs} = makeWorkspaceTabPage(["notes"], "notes");
  const sandbox = {
    document: {
      getElementById(id) {
        if (id === "noteText") return noteText;
        if (id === "noteSaveStatus") return noteStatus;
        return null;
      },
    },
    fetch: () => new Promise(resolve => { finishSave = resolve; }),
    activePatientId: "",
    cssEscape: value => String(value),
    renderWorkspaceNotesTab: panel => {
      assert.strictEqual(panel.dataset.workspacePanel, "notes");
      calls.renders += 1;
    },
    loadAuditLogs: async () => { calls.audits += 1; },
  };
  sandbox.window = sandbox;
  vm.createContext(sandbox);
  vm.runInContext(patientWorkspaceSrc + workspaceNoteSaveSrc + `
    this.__prepareRepeatedNoteClick = host => {
      _wsHost = host;
      workspacePatientId = "synthetic-patient";
      workspaceData = {};
      workspaceLoadedTabs.clear();
      workspaceLoadedTabs.add("notes");
      loadWorkspacePatient = async () => { this.__noteLoadCalls += 1; };
    };
  `, sandbox);
  sandbox.__noteLoadCalls = 0;
  sandbox.__prepareRepeatedNoteClick(page);

  const save = sandbox.saveWorkspaceNote("synthetic-patient");
  await Promise.resolve();
  assert.strictEqual(noteStatus.textContent, "保存中...");
  sandbox.switchWorkspaceTab("notes");
  assert.strictEqual(tabs[0].classList.contains("active"), true);
  finishSave({ok: true});
  await save;
  calls.loads = sandbox.__noteLoadCalls;

  assert.deepStrictEqual(calls, {loads: 1, renders: 1, audits: 1});
});

test("顶层 workspace 通过真实配置和角色守卫，取消零提交、确认后一次切换", async () => {
  const guarded = await workspaceRoleSandbox();
  guarded.roleCheckboxes[0].checked = false;
  guarded.sandbox.location.hash = "#patient/synthetic/profile";
  const before = workspaceSnapshot(guarded);

  const cancelled = guarded.sandbox.switchWorkspaceView("warehouse");
  await Promise.resolve();
  assert.strictEqual(guarded.overlays.length, 1);
  assert.deepStrictEqual(workspaceSnapshot(guarded), before);
  guarded.overlays[0].querySelector('[data-rp-leave="cancel"]').click();
  assert.strictEqual(await cancelled, false);
  assert.deepStrictEqual(workspaceSnapshot(guarded), before);
  assert.strictEqual(guarded.roleCheckboxes[0].checked, false);

  const confirmed = guarded.sandbox.switchWorkspaceView("warehouse");
  await Promise.resolve();
  assert.strictEqual(guarded.overlays.length, 1);
  guarded.overlays[0].querySelector('[data-rp-leave="confirm"]').click();
  assert.strictEqual(await confirmed, true);
  assert.strictEqual(guarded.sandbox.__getWorkspaceState().activeModuleView, "warehouse");
  assert.deepStrictEqual(
    guarded.navButtons
      .filter(button => button.classList.contains("active"))
      .map(button => button.dataset.workspaceView),
    ["warehouse"],
  );
  assert.strictEqual(guarded.elements.workspaceTitle.textContent, "库存管理");
  assert.strictEqual(guarded.elements.modulePanel.innerHTML, "<div>warehouse-loaded</div>");
  assert.strictEqual(guarded.sandbox.location.hash, "");
  assert.strictEqual(guarded.roleCheckboxes[0].checked, true);
  assert.strictEqual(guarded.calls.warehouse, 1);
});

test("真实 switchWorkspaceView 把 pending_due、患者搜索和工作日期传入已确认 V2 入口", async () => {
  const current = customerHubNavigationSandbox();
  assert.strictEqual(await current.sandbox.switchWorkspaceView(
    "return-visits",
    "  patient-synthetic  ",
    {context: {returnFilter: "pending_due"}},
  ), true);
  await Promise.resolve();

  assert.strictEqual(current.calls.mounted.length, 1);
  const context = current.calls.mounted[0];
  assert.deepStrictEqual({
    returnFilter: context.returnFilter,
    searchText: context.searchText,
    selectedDate: context.selectedDate,
  }, {
    returnFilter: "pending_due",
    searchText: "patient-synthetic",
    selectedDate: "2026-07-21",
  });
});

test("客户通入口 context 或工作日期读取异常时明确失败且不挂载 V2", async t => {
  const cases = [
    ["moduleContext descriptors", {}, new Proxy({}, {
      ownKeys() { throw new Error("context_descriptors_failed"); },
    })],
    ["workDate.value", {
      workDate: Object.defineProperty({}, "value", {
        get() { throw new Error("work_date_failed"); },
      }),
    }, {}],
    ["localDateValue", {
      workDate: {value: ""},
      localDateValue() { throw new Error("local_date_failed"); },
    }, {}],
  ];

  for (const [label, sandboxOptions, context] of cases) await t.test(label, async () => {
    const current = customerHubNavigationSandbox(sandboxOptions);
    assert.strictEqual(await current.sandbox.switchWorkspaceView(
      "return-visits",
      "",
      {context},
    ), true);
    await Promise.resolve();

    assert.strictEqual(current.calls.mounted.length, 0);
    assert.match(current.elements.modulePanel.innerHTML, /客户通载入失败/);
  });
});

test("顶层离开客户通必须经真实守卫 fail closed，拒绝不切换、允许才进预约", async () => {
  let allowed = false;
  let leaveCalls = 0;
  const current = customerHubNavigationSandbox({
    leave: async () => {
      leaveCalls += 1;
      return allowed;
    },
  });
  current.sandbox.__setWorkspaceView("return-visits");

  assert.strictEqual(await current.sandbox.switchWorkspaceView("appointments"), false);
  assert.strictEqual(current.sandbox.__getWorkspaceView(), "return-visits");
  assert.strictEqual(current.calls.appointments, 0);
  allowed = true;
  assert.strictEqual(await current.sandbox.switchWorkspaceView("appointments"), true);
  assert.strictEqual(current.sandbox.__getWorkspaceView(), "appointments");
  assert.strictEqual(current.calls.appointments, 1);
  assert.strictEqual(leaveCalls, 2);

  const missing = customerHubNavigationSandbox();
  missing.sandbox.__setWorkspaceView("return-visits");
  missing.sandbox.requestLeaveReturnVisitModule = undefined;
  assert.strictEqual(await missing.sandbox.switchWorkspaceView("appointments"), false);
  assert.strictEqual(missing.calls.appointments, 0);
});

test("角色页脏状态点击已激活配置入口仍走真实守卫且取消不重载 DOM", async () => {
  const guarded = await workspaceRoleSandbox();
  guarded.roleCheckboxes[0].checked = false;
  const before = workspaceSnapshot(guarded);
  const roleRequestsBefore = guarded.calls.roleRequests;

  const reenter = guarded.sandbox.switchWorkspaceView("config");
  await Promise.resolve();
  assert.strictEqual(guarded.overlays.length, 1);
  assert.deepStrictEqual(workspaceSnapshot(guarded), before);
  guarded.overlays[0].querySelector('[data-rp-leave="cancel"]').click();

  assert.strictEqual(await reenter, false);
  assert.deepStrictEqual(workspaceSnapshot(guarded), before);
  assert.strictEqual(guarded.roleCheckboxes[0].checked, false);
  assert.strictEqual(guarded.calls.roleRequests, roleRequestsBefore);
});

test("顶层 workspace 快速重复导航共用确认且只提交最新目标", async () => {
  const guarded = await workspaceRoleSandbox();
  guarded.roleCheckboxes[0].checked = false;

  const first = guarded.sandbox.switchWorkspaceView("warehouse");
  await Promise.resolve();
  assert.strictEqual(guarded.overlays.length, 1);
  const second = guarded.sandbox.switchWorkspaceView("sterilization");
  await Promise.resolve();
  assert.strictEqual(guarded.overlays.length, 1);
  guarded.overlays[0].querySelector('[data-rp-leave="confirm"]').click();

  assert.strictEqual(await first, false);
  assert.strictEqual(await second, true);
  assert.strictEqual(guarded.sandbox.__getWorkspaceState().activeModuleView, "sterilization");
  assert.strictEqual(guarded.calls.warehouse, 0);
  assert.strictEqual(guarded.calls.sterilization, 1);
  assert.deepStrictEqual(
    guarded.navButtons
      .filter(button => button.classList.contains("active"))
      .map(button => button.dataset.workspaceView),
    ["sterilization"],
  );
});

test("角色权限保存中顶层 workspace 切换被真实守卫阻断", async () => {
  const guarded = await workspaceRoleSandbox();
  const before = workspaceSnapshot(guarded);
  const messageBefore = guarded.elements.rpMsg.textContent;
  guarded.sandbox.__setRoleSaveInFlight(true);

  assert.strictEqual(await guarded.sandbox.switchWorkspaceView("warehouse"), false);
  assert.deepStrictEqual(workspaceSnapshot(guarded), before);
  assert.strictEqual(guarded.calls.warehouse, 0);
  assert.strictEqual(guarded.overlays.length, 0);
  assert.strictEqual(guarded.elements.rpMsg.textContent, messageBefore);
  guarded.sandbox.__setRoleSaveInFlight(false);
});

test("配置离开接口缺失、抛错或非 true 时顶层 workspace 均零提交", async () => {
  const cases = [
    ["缺失", undefined],
    ["抛错", async () => { throw new Error("config leave failed"); }],
    ["返回 undefined", async () => undefined],
    ["返回 false", async () => false],
  ];
  for (const [label, guard] of cases) {
    const current = configUiSandbox({
      perms: new Set(["staff.view"]),
      systemAdmin: true,
      withWorkspaceNavigation: true,
    });
    current.sandbox.requestLeaveConfigModule = guard;
    current.sandbox.location.hash = "#patient/synthetic/profile";
    const before = workspaceSnapshot(current);

    assert.strictEqual(await current.sandbox.switchWorkspaceView("warehouse"), false, label);
    assert.deepStrictEqual(workspaceSnapshot(current), before, label);
    assert.strictEqual(current.calls.warehouse, 0, label);
  }
});

test("旧 staff/user/role 权限通过唯一 access 入口合并承载", () => {
  const {sandbox} = configUiSandbox({
    accessV3: false,
    role: "reception",
    perms: new Set(["staff.manage", "user.manage", "role.manage"]),
  });
  const outer = configKeys({
    accessV3: false,
    role: "reception",
    perms: new Set(["staff.manage", "user.manage", "role.manage"]),
  });
  assert.deepStrictEqual(outer, ["access"]);
  assert.deepStrictEqual(
    Array.from(sandbox.__accessCenterTabs(), tab => tab.key),
    ["staff", "roles"],
  );
});

test("配置中心缓存串锁定人员与权限合并版本", () => {
  assert.match(indexSrc, /\/config_center\.js\?v=2026-08-04-r3/);
  assert.match(indexSrc, /\/role_permissions\.js\?v=2026-07-19-access-tree/);
  assert.match(indexSrc, /\/workspace_tabs\.js\?v=2026-08-04-r3/);
  assert.match(indexSrc, /\/patient_workspace\.js\?v=2026-08-04-r3/);
  assert.match(indexSrc, /\/app\.js\?v=2026-08-04-r3/);
});

test("付款方式齿轮统一使用新旧兼容能力判断", () => {
  assert.match(billingSrc, /canManagePaymentMethods\(\)/);
  assert.doesNotMatch(billingSrc, /!hasPerm\("billing\.pay"\)/);
});
