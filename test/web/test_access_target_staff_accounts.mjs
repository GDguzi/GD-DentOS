import {test} from "node:test";
import assert from "node:assert";
import {readFileSync} from "node:fs";
import {fileURLToPath} from "node:url";
import {dirname, join} from "node:path";
import vm from "node:vm";

const here = dirname(fileURLToPath(import.meta.url));
const staticDir = join(here, "..", "..", "local_app", "static");
const staffSource = readFileSync(join(staticDir, "staff_manager.js"), "utf8");
const accountSource = readFileSync(join(staticDir, "account_manager.js"), "utf8");

function between(source, start, end) {
  const from = source.indexOf(start);
  const to = source.indexOf(end, from);
  assert.ok(from >= 0 && to > from, `应能抽取 ${start}`);
  return source.slice(from, to);
}

function staffSandbox({accessV3, prompts = []}) {
  const calls = [];
  const queue = prompts.slice();
  const sandbox = {
    ROLE_TO_ACCT: {"医生": "doctor"},
    _smEditId: null,
    JSON,
    encodeURIComponent,
    appPrompt: async () => queue.shift() ?? null,
    ensureStaff: async () => {},
    ensureStaffAccts: async () => {},
    renderStaffManager: () => {},
    refreshOrderTeamSelects: () => {},
    fetch: async (url, options = {}) => {
      calls.push({url, options});
      return {ok: true, status: 200, json: async () => ({})};
    },
    window: {
      __accessV3: accessV3,
      alert: () => {},
      confirm: () => true,
    },
  };
  sandbox.globalThis = sandbox;
  vm.createContext(sandbox);
  vm.runInContext(
    between(staffSource, "async function openStaffAccount(", "// 处置编辑器"),
    sandbox,
  );
  return {sandbox, calls};
}

test("V3 人员开户不再发送旧 role 字段，旧模式保持原合同", async () => {
  const target = staffSandbox({accessV3: true, prompts: ["desk", " pw "]});
  await target.sandbox.openStaffAccount("staff-1", "D001", "医生");
  assert.deepStrictEqual(JSON.parse(target.calls[0].options.body), {
    username: "desk",
    password: " pw ",
  });

  const legacy = staffSandbox({accessV3: false, prompts: ["desk", "pw"]});
  await legacy.sandbox.openStaffAccount("staff-1", "D001", "医生");
  assert.deepStrictEqual(JSON.parse(legacy.calls[0].options.body), {
    username: "desk",
    password: "pw",
    role: "doctor",
  });
});

test("旧模式 deleteStaff 仍保留无请求体合同", async () => {
  const legacy = staffSandbox({accessV3: false});
  await legacy.sandbox.deleteStaff("staff-1");
  assert.strictEqual(legacy.calls[0].options.method, "DELETE");
  assert.strictEqual(legacy.calls[0].options.body, undefined);
});

const syntheticUsers = [
  {
    id: "user-1", username: "doctor", display_name: "张医生", is_active: true,
    is_system_admin: false, account_kind: "staff", primary_role: "doctor",
    role_keys: ["doctor", "nurse"], phone: "13800009999",
    id_card: "secret-id-card", license_no: "secret-license", department: "secret-department",
  },
  {
    id: "admin-1", username: "ops", display_name: "运维管理员", is_active: true,
    is_system_admin: true, account_kind: "independent_admin", role_keys: [],
  },
];

function accountSandbox({
  accessV3 = true,
  permissions = ["account.open", "account.security"],
  directPermissions = permissions,
  exposeDirectPermissions = true,
  users = syntheticUsers,
  isSystemAdmin = false,
  prompts = [],
} = {}) {
  const calls = [];
  const messages = [];
  const alerts = [];
  const promptCalls = [];
  const queue = prompts.slice();
  const elements = {
    amBody: {innerHTML: ""},
    amMsg: {
      textContent: "",
      classList: {toggle() {}},
      style: {},
    },
    amNewUser: {value: "new-user"},
    amNewName: {value: "New User"},
    amNewPw: {value: "pw"},
    amNewRole: {value: "admin"},
  };
  const granted = new Set(permissions);
  const sandbox = {
    __accessV3: accessV3,
    __isSystemAdmin: isSystemAdmin,
    ...(exposeDirectPermissions ? {__userPerms: new Set(directPermissions)} : {}),
    _ROLE_LABELS: {doctor: "医生", nurse: "护士"},
    JSON,
    encodeURIComponent,
    escapeHtml: value => String(value ?? ""),
    escapeAttr: value => String(value ?? ""),
    hasPerm: permission => isSystemAdmin || granted.has(permission),
    appPrompt: async (...args) => {
      promptCalls.push(args);
      return queue.length ? queue.shift() : null;
    },
    alert: message => alerts.push(String(message)),
    amSetMsg: text => messages.push(text),
    document: {
      getElementById: id => elements[id] || null,
      createElement: () => ({id: "", innerHTML: "", className: "", hidden: true}),
      body: {appendChild(node) { if (node.id) elements[node.id] = node; }},
    },
    fetch: async (url, options = {}) => {
      calls.push({url, options});
      return {
        ok: true,
        status: 200,
        json: async () => (url === "/api/users" ? {users} : {}),
      };
    },
    window: {__accessV3: accessV3, __isSystemAdmin: isSystemAdmin},
  };
  Object.assign(sandbox.window, sandbox);
  sandbox.window = sandbox;
  vm.createContext(sandbox);
  vm.runInContext(accountSource, sandbox);
  return {sandbox, calls, messages, alerts, promptCalls, elements};
}

test("V3 账号页显示岗位只读信息，不再提供旧角色修改和脱离人员的新增表单", async () => {
  const {sandbox, elements} = accountSandbox();
  await sandbox.amRender();
  assert.match(elements.amBody.innerHTML, /医生/);
  assert.match(elements.amBody.innerHTML, /护士/);
  assert.doesNotMatch(elements.amBody.innerHTML, /class="cs-input am-role"/);
  assert.doesNotMatch(elements.amBody.innerHTML, /id="amNewRole"/);
  assert.match(elements.amBody.innerHTML, /诊所人员管理/);
});

test("accountManagerVisibleUsers 只在 V3 且可查看人员时过滤员工账号", () => {
  const canView = accountSandbox({permissions: ["staff.view"]});
  const visible = canView.sandbox.accountManagerVisibleUsers(syntheticUsers);
  assert.deepStrictEqual(Array.from(visible, user => user.id), ["admin-1"]);

  const accountOnly = accountSandbox({permissions: ["account.open"]});
  assert.strictEqual(accountOnly.sandbox.accountManagerVisibleUsers(syntheticUsers), syntheticUsers);

  const legacy = accountSandbox({accessV3: false, permissions: ["staff.view"]});
  assert.strictEqual(legacy.sandbox.accountManagerVisibleUsers(syntheticUsers), syntheticUsers);
});

test("V3 系统管理员只按直接 staff.view 分流员工账号", () => {
  const withoutDirectStaffView = accountSandbox({isSystemAdmin: true, permissions: []});
  assert.strictEqual(
    withoutDirectStaffView.sandbox.accountManagerVisibleUsers(syntheticUsers),
    syntheticUsers,
  );

  const withDirectStaffView = accountSandbox({
    isSystemAdmin: true,
    permissions: ["staff.view"],
  });
  assert.deepStrictEqual(
    Array.from(withDirectStaffView.sandbox.accountManagerVisibleUsers(syntheticUsers), user => user.id),
    ["admin-1"],
  );

  const missingDirectPermissionSet = accountSandbox({
    isSystemAdmin: true,
    permissions: [],
    exposeDirectPermissions: false,
  });
  assert.strictEqual(
    missingDirectPermissionSet.sandbox.accountManagerVisibleUsers(syntheticUsers),
    syntheticUsers,
  );
});

test("amRender 接通账号可见性：有 staff.view 隐藏员工，无该权限仍只显示安全账号字段", async () => {
  const personnelViewer = accountSandbox({permissions: ["staff.view", "account.open", "account.security"]});
  await personnelViewer.sandbox.amRender();
  assert.doesNotMatch(personnelViewer.elements.amBody.innerHTML, />doctor</);
  assert.match(personnelViewer.elements.amBody.innerHTML, />ops</);

  const accountOperator = accountSandbox({permissions: ["account.open", "account.security"]});
  await accountOperator.sandbox.amRender();
  const html = accountOperator.elements.amBody.innerHTML;
  for (const expected of ["doctor", "张医生", "启用", "停用", "重置密码"]) assert.match(html, new RegExp(expected));
  for (const secret of ["13800009999", "secret-id-card", "secret-license", "secret-department"]) {
    assert.doesNotMatch(html, new RegExp(secret));
  }
});

test("账号行动作不把 DTO id 或 username 拼进 inline JavaScript，索引 wrapper 仍定位正确用户", async () => {
  const malicious = {
    ...syntheticUsers[0],
    id: "id');globalThis.__dtoIdExecuted=true;//",
    username: "name');globalThis.__dtoNameExecuted=true;//",
  };
  const target = accountSandbox({users: [malicious], prompts: ["pw"]});
  await target.sandbox.amRender();
  const handlers = Array.from(
    target.elements.amBody.innerHTML.matchAll(/onclick="([^"]*)"/g),
    match => match[1],
  );
  assert.ok(handlers.some(code => /amResetPwAt\(0\)/.test(code)));
  assert.ok(handlers.some(code => /amToggleAt\(0, false\)/.test(code)));
  for (const code of handlers) {
    assert.doesNotMatch(code, /__dtoIdExecuted|__dtoNameExecuted/);
  }

  assert.strictEqual(typeof target.sandbox.amResetPwAt, "function");
  assert.strictEqual(typeof target.sandbox.amToggleAt, "function");
  await target.sandbox.amResetPwAt(0);
  await target.sandbox.amToggleAt(0, false);
  assert.ok(target.calls.some(call => call.url === `/api/users/${encodeURIComponent(malicious.id)}/reset-password`));
  const toggle = target.calls.find(call => call.url === `/api/users/${encodeURIComponent(malicious.id)}` && call.options.method === "PUT");
  assert.deepStrictEqual(JSON.parse(toggle.options.body), {is_active: false});
  assert.match(target.promptCalls[0][0], /__dtoNameExecuted/);
});

test("账号页操作按钮分别受 account.open 和 account.security 控制", async () => {
  const openOnly = accountSandbox({permissions: ["account.open"], users: [syntheticUsers[0]]});
  await openOnly.sandbox.amRender();
  assert.match(openOnly.elements.amBody.innerHTML, />停用</);
  assert.doesNotMatch(openOnly.elements.amBody.innerHTML, />重置密码</);

  const securityOnly = accountSandbox({permissions: ["account.security"], users: [syntheticUsers[0]]});
  await securityOnly.sandbox.amRender();
  assert.match(securityOnly.elements.amBody.innerHTML, />重置密码</);
  assert.doesNotMatch(securityOnly.elements.amBody.innerHTML, />停用</);

  const readOnly = accountSandbox({permissions: [], users: [syntheticUsers[0]]});
  await readOnly.sandbox.amRender();
  assert.doesNotMatch(readOnly.elements.amBody.innerHTML, />停用<\/button>|>启用<\/button>|>重置密码<\/button>/);
});

test("普通账号操作员不显示也不能触发系统管理员目标的启停或重置", async () => {
  const ordinary = accountSandbox({
    permissions: ["account.open", "account.security"],
    users: [syntheticUsers[1]],
    isSystemAdmin: false,
    prompts: ["pw"],
  });
  await ordinary.sandbox.amRender();
  assert.doesNotMatch(ordinary.elements.amBody.innerHTML, />停用<\/button>|>启用<\/button>|>重置密码<\/button>/);
  const before = ordinary.calls.length;
  await ordinary.sandbox.amResetPwAt(0);
  await ordinary.sandbox.amToggleAt(0, false);
  await ordinary.sandbox.amResetPw("admin-1", "ops");
  await ordinary.sandbox.amToggle("admin-1", false);
  assert.strictEqual(ordinary.promptCalls.length, 0);
  assert.strictEqual(ordinary.calls.length, before);

  const systemAdmin = accountSandbox({
    permissions: ["account.open", "account.security"],
    users: [syntheticUsers[1]],
    isSystemAdmin: true,
  });
  await systemAdmin.sandbox.amRender();
  assert.match(systemAdmin.elements.amBody.innerHTML, />停用<\/button>/);
  assert.match(systemAdmin.elements.amBody.innerHTML, />重置密码<\/button>/);
});

test("账号管理标题栏不再提供 openStaffManager 第二入口", () => {
  const {sandbox} = accountSandbox();
  const modal = sandbox.amEnsureModal();
  assert.doesNotMatch(modal.innerHTML, /openStaffManager|[👥]配台人员/);
});

test("账号页 400/403/409/500 和网络异常均给稳定中文，不渲染后端 detail", async () => {
  const cases = [
    {status: 400, expected: /请求内容/},
    {status: 403, expected: /权限不足/},
    {status: 409, expected: /当前状态/},
    {status: 500, expected: /服务异常/},
    {network: true, expected: /网络异常/},
  ];
  for (const entry of cases) {
    const target = accountSandbox();
    await target.sandbox.amRender();
    target.sandbox.fetch = async (url, options = {}) => {
      target.calls.push({url, options});
      if (entry.network) throw new Error("private token network");
      return {ok: false, status: entry.status, json: async () => ({detail: "SQL /tmp/private.db token=secret hidden"})};
    };
    await target.sandbox.amToggle("user-1", false);
    assert.match(target.alerts.at(-1), entry.expected);
    assert.doesNotMatch(target.alerts.at(-1), /SQL|private\.db|token|secret|hidden/);
  }
});

test("账号列表加载的 403、500 和网络失败不伪装成空列表", async () => {
  const cases = [
    {status: 403, expected: /权限不足/},
    {status: 500, expected: /服务异常/},
    {network: true, expected: /网络异常/},
  ];
  for (const entry of cases) {
    const target = accountSandbox();
    target.sandbox.fetch = async (url, options = {}) => {
      target.calls.push({url, options});
      if (entry.network) throw new Error("private token network");
      return {ok: false, status: entry.status, json: async () => ({detail: "SQL private token"})};
    };
    await target.sandbox.amRender();
    assert.match(target.elements.amBody.innerHTML, entry.expected);
    assert.doesNotMatch(target.elements.amBody.innerHTML, /暂无账号|SQL|private|token/);
  }
});

test("账号列表 200 响应必须是 users 数组且每项含有效 id、username、is_active", async () => {
  const invalidPayloads = [
    {},
    {users: {}},
    {users: [{...syntheticUsers[0], id: ""}]},
    {users: [{...syntheticUsers[0], username: null}]},
    {users: [{...syntheticUsers[0], is_active: 1}]},
  ];
  for (const payload of invalidPayloads) {
    const target = accountSandbox({prompts: ["pw"]});
    target.sandbox.fetch = async (url, options = {}) => {
      target.calls.push({url, options});
      return {ok: true, status: 200, json: async () => payload};
    };
    await target.sandbox.amRender();
    assert.match(target.elements.amBody.innerHTML, /账号数据加载失败/);
    assert.doesNotMatch(target.elements.amBody.innerHTML, /暂无账号/);
    const before = target.calls.length;
    await target.sandbox.amResetPwAt(0);
    await target.sandbox.amToggleAt(0, false);
    await target.sandbox.amResetPw("missing-user", "missing");
    await target.sandbox.amToggle("missing-user", false);
    assert.strictEqual(target.promptCalls.length, 0);
    assert.strictEqual(target.calls.length, before);
  }
});

test("账号重置与本人改密遇服务或网络异常不抛出、不泄露细节", async () => {
  const reset = accountSandbox({prompts: ["pw"]});
  await reset.sandbox.amRender();
  reset.sandbox.fetch = async (url, options = {}) => {
    reset.calls.push({url, options});
    return {ok: false, status: 500, json: async () => ({detail: "SQL private token"})};
  };
  await reset.sandbox.amResetPw("user-1", "doctor");
  assert.match(reset.alerts.at(-1), /服务异常/);
  assert.doesNotMatch(reset.alerts.at(-1), /SQL|private|token/);

  const own = accountSandbox({prompts: ["old", "new"]});
  own.sandbox.fetch = async (url, options = {}) => {
    own.calls.push({url, options});
    throw new Error("private token network");
  };
  await own.sandbox.amChangeOwnPassword();
  assert.match(own.alerts.at(-1), /网络异常/);
  assert.doesNotMatch(own.alerts.at(-1), /private|token/);
});

test("V3 前端拒绝旧角色修改和无人员关联的直接开户请求", async () => {
  const {sandbox, calls, elements} = accountSandbox();
  await sandbox.amSetRole({dataset: {uid: "user-1"}, value: "admin"});
  await sandbox.amCreate();
  assert.strictEqual(calls.length, 0);
  assert.match(elements.amMsg.textContent, /诊所人员管理/);
});

test("legacy 角色 PUT 的 409、500 和网络失败都重新加载服务端真实选择", async () => {
  const legacyUser = {...syntheticUsers[0], role: "reception", role_keys: undefined};
  const cases = [{status: 409}, {status: 500}, {network: true}];
  for (const entry of cases) {
    const target = accountSandbox({accessV3: false, users: [legacyUser]});
    await target.sandbox.amRender();
    const baseFetch = target.sandbox.fetch;
    target.sandbox.fetch = async (url, options = {}) => {
      if (options.method === "PUT") {
        target.calls.push({url, options});
        if (entry.network) throw new Error("private token network");
        return {ok: false, status: entry.status, json: async () => ({detail: "SQL private token"})};
      }
      return baseFetch(url, options);
    };
    await target.sandbox.amSetRole({dataset: {uid: legacyUser.id}, value: "admin"});
    assert.strictEqual(target.calls.filter(call => call.options.method === "PUT").length, 1);
    assert.strictEqual(target.calls.filter(call => call.url === "/api/users" && !call.options.method).length, 2);
    assert.match(target.elements.amBody.innerHTML, /option value="reception" selected/);
    assert.match(target.alerts.at(-1), entry.network ? /网络异常/ : (entry.status === 409 ? /当前状态/ : /服务异常/));
    assert.doesNotMatch(target.alerts.at(-1), /SQL|private|token/);
  }
});

const syntheticMembers = [
  {
    staff_id: "staff-zhang", name: "张明", note: "合成备注", job_no: "D100", phone: "13800001001",
    sex: "男", id_card: "110101199001011234", title: "主治医师", license_no: "LIC-D100-7788",
    department: "综合科", employment_status: "employed", left_at: null, left_reason: null,
    primary_role: "doctor", role_keys: ["doctor", "consultant"], role: "医生", roles: ["医生", "咨询师"],
    account: {user_id: "user-zhang", username: "zhang.demo", is_active: true, is_system_admin: true, account_kind: "staff"},
  },
  {
    staff_id: "staff-li", name: "李娜", note: "", job_no: "D200", phone: "13900002002",
    sex: "女", id_card: "220202199202022345", title: "医师", license_no: "LIC-D200-8899",
    department: "综合科", employment_status: "left", left_at: "2026-01-02", left_reason: "合成离职",
    primary_role: "doctor", role_keys: ["doctor"], role: "医生", roles: ["医生"], account: null,
  },
  {
    staff_id: "staff-wang", name: "王强", note: "", job_no: "N300", phone: "13700003003",
    sex: "男", id_card: "330303199303033456", title: "护师", license_no: "LIC-N300-9900",
    department: "护理组", employment_status: "employed", left_at: null, left_reason: null,
    primary_role: "nurse", role_keys: ["nurse"], role: "护士", roles: ["护士"],
    account: {user_id: "user-wang", username: "wang.demo", is_active: false, is_system_admin: false, account_kind: "staff"},
  },
  {
    staff_id: "staff-zhao", name: "赵雨", note: "", job_no: "F400", phone: "13600004004",
    sex: "女", id_card: "440404199404044567", title: "", license_no: "",
    department: "前台", employment_status: "employed", left_at: null, left_reason: null,
    primary_role: "reception", role_keys: ["reception"], role: "前台", roles: ["前台"],
    account: {user_id: "user-zhao", username: "zhao.demo", is_active: true, is_system_admin: false, account_kind: "staff"},
  },
];

function personnelSandbox({
  members = syntheticMembers,
  listOk = true,
  canEdit,
  saveOk = true,
  saveNetwork = false,
  accessV3 = true,
  permissions,
  isSystemAdmin = false,
  prompts = [],
} = {}) {
  const calls = [];
  const alerts = [];
  const promptCalls = [];
  const queue = prompts.slice();
  let teamRefreshes = 0;
  const listScroller = {scrollTop: 0, innerHTML: ""};
  const renderTarget = () => ({
    innerHTML: "",
    querySelector: selector => selector === ".access-staff-list-scroll" ? listScroller : null,
  });
  const elements = {
    personnelBody: renderTarget(),
    staffManagerBody: renderTarget(),
    staffManagerModal: {hidden: true},
  };
  const selectedRoles = [];
  const defaultPermissions = accessV3 ? ["staff.edit"] : ["staff.manage"];
  const granted = new Set(permissions || (canEdit === false ? [] : defaultPermissions));
  const sandbox = {
    __accessV3: accessV3,
    __isSystemAdmin: isSystemAdmin,
    JSON,
    Intl,
    encodeURIComponent,
    escapeHtml: value => String(value ?? "")
      .replaceAll("&", "&amp;").replaceAll("<", "&lt;").replaceAll(">", "&gt;")
      .replaceAll('"', "&quot;").replaceAll("'", "&;"),
    escapeAttr: value => String(value ?? "")
      .replaceAll("&", "&amp;").replaceAll('"', "&quot;").replaceAll("'", "&;"),
    hasPerm: permission => granted.has(permission),
    appPrompt: async (...args) => {
      promptCalls.push(args);
      return queue.length ? queue.shift() : null;
    },
    alert: message => alerts.push(String(message)),
    localStorage: {getItem: () => null, setItem() {}},
    renderTreatmentOrderEditor() { teamRefreshes += 1; },
    orderModel: {},
    document: {
      getElementById: id => elements[id] || null,
      querySelector: selector => selector === ".access-staff-list-scroll" ? listScroller : null,
      querySelectorAll: selector => selector === '.access-staff-secondary-role:checked' ? selectedRoles : [],
      createElement: tag => ({tagName: tag.toUpperCase(), id: "", innerHTML: "", hidden: false}),
      body: {appendChild(node) { if (node.id) elements[node.id] = node; }},
    },
    fetch: async (url, options = {}) => {
      calls.push({url, options});
      if (url === "/api/staff-admin/list") {
        return {
          ok: listOk,
          status: listOk ? 200 : 503,
          json: async () => listOk ? {members, totalcount: members.length} : {detail: "SQL /tmp/private.db token=secret"},
        };
      }
      if (url === "/api/staff-members") {
        if (options.method === "POST") {
          if (saveNetwork) throw new Error("synthetic network failure");
          return {ok: saveOk, status: saveOk ? 200 : 400, json: async () => ({detail: "SQL /tmp/private.db token=secret username=hidden"})};
        }
        return {ok: true, status: 200, json: async () => ({members: []})};
      }
      if (url.startsWith("/api/staff-members/")) {
        return {ok: saveOk, status: saveOk ? 200 : 400, json: async () => ({detail: "SQL /tmp/private.db token=secret username=hidden"})};
      }
      if (url.startsWith("/api/users/")) {
        return {ok: true, status: 200, json: async () => ({})};
      }
      throw new Error(`unexpected request ${url}`);
    },
  };
  sandbox.window = sandbox;
  sandbox.globalThis = sandbox;
  vm.createContext(sandbox);
  vm.runInContext(staffSource, sandbox);
  return {
    sandbox, calls, alerts, promptCalls, elements, listScroller, selectedRoles,
    teamRefreshes: () => teamRefreshes,
  };
}

function accessStaffState(sandbox) {
  return vm.runInContext("_accessStaffState", sandbox);
}

test("人员中心只从受控聚合接口加载，失败时不回退轻量选人缓存", async () => {
  const ready = personnelSandbox();
  await ready.sandbox.renderStaffInto("personnelBody");
  assert.deepStrictEqual(ready.calls.map(call => call.url), ["/api/staff-admin/list"]);
  assert.match(ready.elements.personnelBody.innerHTML, /人员总数/);
  assert.match(ready.elements.personnelBody.innerHTML, />4</);

  const failed = personnelSandbox({listOk: false});
  await failed.sandbox.renderStaffInto("personnelBody");
  assert.deepStrictEqual(failed.calls.map(call => call.url), ["/api/staff-admin/list"]);
  assert.match(failed.elements.personnelBody.innerHTML, /人员数据加载失败/);
  assert.doesNotMatch(failed.elements.personnelBody.innerHTML, /SQL|private\.db|token|secret/);
});

test("合成四人概览、搜索、筛选、组内排序与列表脱敏", async () => {
  const {sandbox, elements} = personnelSandbox();
  await sandbox.renderStaffInto("personnelBody");
  const html = () => elements.personnelBody.innerHTML;
  for (const expected of ["人员总数", "在职", "离职", "已开通账号", "系统管理员"]) assert.match(html(), new RegExp(expected));
  for (const count of ["4", "3", "1", "3", "1"]) assert.match(html(), new RegExp(`>${count}<`));

  const state = accessStaffState(sandbox);
  for (const query of ["张明", "zhang.demo", "D100", "13800001001", "zm"]) {
    state.query = query;
    sandbox.renderStaffManager();
    assert.match(html(), /张明/, `应支持搜索 ${query}`);
    assert.doesNotMatch(html(), /李娜/, `搜索 ${query} 不应混入其他人`);
  }

  state.query = "";
  state.roleFilter = "doctor";
  sandbox.renderStaffManager();
  assert.match(html(), /张明/);
  assert.match(html(), /李娜/);
  assert.doesNotMatch(html(), /王强/);
  state.roleFilter = "";
  state.employmentFilter = "left";
  sandbox.renderStaffManager();
  assert.match(html(), /李娜/);
  assert.doesNotMatch(html(), /张明/);
  state.employmentFilter = "";
  state.accountFilter = "none";
  sandbox.renderStaffManager();
  assert.match(html(), /李娜/);
  assert.doesNotMatch(html(), /王强/);

  state.accountFilter = "";
  sandbox.renderStaffManager();
  assert.ok(html().indexOf("张明") < html().indexOf("李娜"), "同岗位离职人员应排在在职人员后");
  assert.match(html(), /access-staff-row is-left/);
  for (const raw of ["13800001001", "110101199001011234", "LIC-D100-7788"]) assert.doesNotMatch(html(), new RegExp(raw));
  assert.match(html(), /138\*{4}1001/);
  assert.doesNotMatch(html(), /class="sm-add"|detail-drawer|drawer-panel/);
});

test("人员列表行明确区分主兼岗位并始终显示系统管理员是否", () => {
  const {sandbox} = personnelSandbox();
  const adminRow = sandbox._accessStaffRowHtml(syntheticMembers[0]);
  assert.match(adminRow, /主岗位：医生/);
  assert.match(adminRow, /兼任岗位：咨询师/);
  assert.match(adminRow, /系统管理员：是/);

  const ordinaryRow = sandbox._accessStaffRowHtml(syntheticMembers[3]);
  assert.match(ordinaryRow, /主岗位：前台/);
  assert.match(ordinaryRow, /兼任岗位：无/);
  assert.match(ordinaryRow, /系统管理员：否/);
});

test("搜索连续输入保留真实焦点、光标与最终查询值", async () => {
  const {sandbox, elements, listScroller} = personnelSandbox();
  await sandbox.renderStaffInto("personnelBody");
  let renderedHtml = elements.personnelBody.innerHTML;
  const body = {};
  let activeElement = body;
  let currentSearch;
  const makeSearch = value => ({
    value,
    selectionStart: value.length,
    selectionEnd: value.length,
    focus() { activeElement = this; },
    setSelectionRange(start, end) { this.selectionStart = start; this.selectionEnd = end; },
  });
  currentSearch = makeSearch("");
  Object.defineProperty(sandbox.document, "activeElement", {get: () => activeElement});
  const originalDocumentQuery = sandbox.document.querySelector;
  sandbox.document.querySelector = selector => selector === ".access-staff-search" ? currentSearch : originalDocumentQuery(selector);
  Object.defineProperty(elements.personnelBody, "innerHTML", {
    configurable: true,
    get: () => renderedHtml,
    set: value => {
      renderedHtml = value;
      if (!value.includes("access-staff-search")) return;
      if (activeElement === currentSearch) activeElement = body;
      const match = value.match(/class="[^"]*access-staff-search[^"]*" value="([^"]*)"/);
      currentSearch = makeSearch(match ? match[1] : "");
    },
  });
  elements.personnelBody.querySelector = selector => {
    if (selector === ".access-staff-list-scroll") return listScroller;
    if (selector === ".access-staff-search") return currentSearch;
    return null;
  };

  currentSearch.focus();
  currentSearch.value = "张";
  currentSearch.setSelectionRange(1, 1);
  sandbox.accessStaffSetFilter("query", currentSearch.value);
  assert.strictEqual(sandbox.document.activeElement, currentSearch);
  assert.strictEqual(currentSearch.selectionStart, 1);
  assert.strictEqual(currentSearch.selectionEnd, 1);

  currentSearch.value = "张明";
  currentSearch.setSelectionRange(2, 2);
  sandbox.accessStaffSetFilter("query", currentSearch.value);
  assert.strictEqual(accessStaffState(sandbox).query, "张明");
  assert.match(listScroller.innerHTML, /张明/);
  assert.doesNotMatch(listScroller.innerHTML, /李娜/);
  assert.strictEqual(sandbox.document.activeElement, currentSearch);
  assert.strictEqual(currentSearch.selectionStart, 2);
  assert.strictEqual(currentSearch.selectionEnd, 2);
});

test("拼音首字母算法稳定，不支持字符原样保留", () => {
  const {sandbox} = personnelSandbox();
  assert.strictEqual(sandbox.staffPinyinInitials("张明"), "zm");
  assert.strictEqual(sandbox.staffPinyinInitials("张-A1"), "z-a1");
});

test("详情右侧栏有四块只读结构，关闭后保留查询筛选和滚动位置", async () => {
  const {sandbox, elements, listScroller} = personnelSandbox();
  await sandbox.renderStaffInto("personnelBody");
  const state = accessStaffState(sandbox);
  state.query = "张";
  state.roleFilter = "doctor";
  state.employmentFilter = "employed";
  state.accountFilter = "enabled";
  listScroller.scrollTop = 187;
  sandbox.openStaffDetails("staff-zhang");
  for (const title of ["人员资料", "岗位与日常权限", "登录账号", "系统管理员"]) assert.match(elements.personnelBody.innerHTML, new RegExp(title));
  assert.match(elements.personnelBody.innerHTML, /功能权限取所有岗位权限的并集/);
  assert.match(elements.personnelBody.innerHTML, /access-sheet-/);
  sandbox.closeStaffDetails();
  assert.strictEqual(state.query, "张");
  assert.strictEqual(state.roleFilter, "doctor");
  assert.strictEqual(state.employmentFilter, "employed");
  assert.strictEqual(state.accountFilter, "enabled");
  assert.strictEqual(state.listScrollTop, 187);
  assert.strictEqual(listScroller.scrollTop, 187);
});

test("人员详情把人事、账号和系统管理员动作归入对应四块", async () => {
  const {sandbox, elements} = personnelSandbox({
    permissions: ["staff.edit", "staff.employment", "account.open", "account.security"],
    isSystemAdmin: true,
  });
  await sandbox.renderStaffInto("personnelBody");
  sandbox.openStaffDetails("staff-zhang");
  const html = elements.personnelBody.innerHTML;
  const section = title => {
    const match = html.match(new RegExp(
      `<section class="access-sheet-section[^"]*"><h3>${title}</h3>[\\s\\S]*?</section>`,
    ));
    assert.ok(match, `应存在 ${title} 区块`);
    return match[0];
  };
  const employment = section("岗位与日常权限");
  const account = section("登录账号");
  const systemAdmin = section("系统管理员");
  assert.match(employment, />离职<\/button>/);
  assert.match(account, />停用<\/button>/);
  assert.match(account, />重置密码<\/button>/);
  assert.match(systemAdmin, /总钥匙/);
  assert.match(systemAdmin, /高风险/);
  assert.match(systemAdmin, />撤销系统管理员<\/button>/);
  assert.doesNotMatch(html, /<section class="access-sheet-section access-sheet-actions">/);
});

function installFormElements(elements, overrides = {}) {
  Object.assign(elements, {
    accessStaffName: {value: "  孙芳  "},
    accessStaffPrimaryRole: {value: "doctor"},
    accessStaffSex: {value: "女"},
    accessStaffJobNo: {value: "D500"},
    accessStaffPhone: {value: "13500005005"},
    accessStaffTitle: {value: "医师"},
    accessStaffDepartment: {value: "修复科"},
    accessStaffLicenseNo: {value: "LIC-D500-1122"},
    accessStaffIdCard: {value: "550505199505055678"},
    accessStaffNote: {value: "合成新人"},
    accessStaffFormError: {textContent: ""},
    accessStaffFormClose: {disabled: false},
    accessStaffFormCancel: {disabled: false},
    accessStaffFormSave: {disabled: false},
  }, overrides);
}

async function openSyntheticForm(options) {
  const target = personnelSandbox(options);
  await target.sandbox.renderStaffInto("personnelBody");
  target.sandbox.openStaffForm(null);
  installFormElements(target.elements);
  return target;
}

function deferNewStaffSave(target) {
  const gate = deferredResponse();
  const baseFetch = target.sandbox.fetch;
  target.sandbox.fetch = async (url, options = {}) => {
    if (url !== "/api/staff-members" || options.method !== "POST") return baseFetch(url, options);
    target.calls.push({url, options});
    return gate.promise;
  };
  return gate;
}

test("添加编辑共用应用内 modal，主岗位与兼任岗位生成精确 payload", async () => {
  const target = personnelSandbox();
  await target.sandbox.renderStaffInto("personnelBody");
  target.sandbox.openStaffForm(null);
  assert.match(target.elements.personnelBody.innerHTML, /access-center-modal/);
  assert.doesNotMatch(target.elements.personnelBody.innerHTML, /window\.prompt|class="sm-add"/);
  installFormElements(target.elements);
  target.selectedRoles.push({value: "nurse"}, {value: "doctor"}, {value: "nurse"});
  await target.sandbox.saveStaffForm();
  const save = target.calls.find(call => call.url === "/api/staff-members" && call.options.method === "POST");
  assert.ok(save, "应发送新增请求");
  assert.deepStrictEqual(JSON.parse(save.options.body), {
    name: "孙芳", primary_role: "doctor", role_keys: ["doctor", "nurse"], sex: "女",
    job_no: "D500", phone: "13500005005", title: "医师", department: "修复科",
    license_no: "LIC-D500-1122", id_card: "550505199505055678", note: "合成新人",
  });
  assert.ok(target.calls.some(call => call.url === "/api/staff-admin/list"), "成功后应刷新聚合列表");
  assert.ok(target.calls.some(call => call.url === "/api/staff-members" && !call.options.method), "成功后应刷新轻量选人缓存");
});

test("V3 人员表单不把系统管理员当作主岗位或兼任岗位", () => {
  const {sandbox} = personnelSandbox();
  const formHtml = sandbox._accessStaffFormHtml();
  assert.match(formHtml, /value="doctor"/);
  assert.match(formHtml, /value="director"/);
  assert.doesNotMatch(formHtml, /value="admin"/);
  assert.doesNotMatch(formHtml, />管理员<\/option>|>管理员<\/label>/);
});

test("人员表单双击保存同步加锁，只发送一个 POST 并禁用三个操作按钮", async () => {
  const target = await openSyntheticForm();
  const formHtml = target.elements.personnelBody.innerHTML;
  const gate = deferNewStaffSave(target);

  const first = target.sandbox.saveStaffForm();
  const second = target.sandbox.saveStaffForm();
  const requestsWhileWaiting = target.calls.filter(call =>
    call.url === "/api/staff-members" && call.options.method === "POST").length;
  const pendingWhileWaiting = accessStaffState(target.sandbox).formPending;
  const disabledWhileWaiting = [
    target.elements.accessStaffFormSave.disabled,
    target.elements.accessStaffFormClose.disabled,
    target.elements.accessStaffFormCancel.disabled,
  ];
  gate.resolve({ok: true, status: 200, json: async () => ({})});
  await Promise.all([first, second]);

  assert.strictEqual(requestsWhileWaiting, 1, "双击只能发送一个新增请求");
  assert.strictEqual(pendingWhileWaiting, true, "首个请求发出前应同步占用表单锁");
  assert.deepStrictEqual(disabledWhileWaiting, [true, true, true], "等待响应时应禁用保存、关闭和取消");
  assert.match(formHtml, /id="accessStaffFormSave"/);
  assert.match(formHtml, /id="accessStaffFormClose"/);
  assert.match(formHtml, /id="accessStaffFormCancel"/);
});

test("旧保存成功或失败返回都不能改动后来重新打开的表单", async () => {
  for (const network of [false, true]) {
    const target = await openSyntheticForm();
    const originalRevision = accessStaffState(target.sandbox).formRevision;
    const gate = deferNewStaffSave(target);

    const oldSave = target.sandbox.saveStaffForm();
    target.sandbox.openStaffForm("staff-zhang");
    installFormElements(target.elements, {
      accessStaffName: {value: "后来表单保留值"},
      accessStaffFormError: {textContent: "后来表单保留提示"},
    });
    const state = accessStaffState(target.sandbox);
    state.formError = "后来表单保留提示";
    const currentRevision = state.formRevision;
    const callsBeforeOldResponse = target.calls.length;
    if (network) gate.reject(new Error("synthetic network failure"));
    else gate.resolve({ok: true, status: 200, json: async () => ({})});
    await oldSave;

    assert.ok(currentRevision > originalRevision, "重新打开表单应推进代际");
    assert.strictEqual(state.formRevision, currentRevision, "旧响应不得推进当前表单代际");
    assert.strictEqual(state.formOpen, true, "旧响应不得关闭后来打开的表单");
    assert.strictEqual(state.formStaffId, "staff-zhang");
    assert.strictEqual(state.formPending, false, "新表单应有独立的非 pending 状态");
    assert.strictEqual(state.formError, "后来表单保留提示", "旧响应不得覆盖新表单错误");
    assert.strictEqual(target.elements.accessStaffName.value, "后来表单保留值", "旧响应不得覆盖新输入");
    assert.strictEqual(target.elements.accessStaffFormError.textContent, "后来表单保留提示");
    assert.strictEqual(target.calls.length, callsBeforeOldResponse, "旧响应不得触发列表或轻量选人刷新");
  }
});

test("legacy staff.manage 新增和编辑发送中文岗位旧合同，保留档案与备注", async () => {
  const legacyMember = {
    ...syntheticMembers[0], primary_role: undefined, role_keys: undefined,
    role: "医生", roles: ["医生", "护士"], account: null,
  };
  const expected = {
    name: "孙芳", role: "医生", roles: ["医生", "护士"], sex: "女",
    job_no: "D500", phone: "13500005005", title: "医师", department: "修复科",
    license_no: "LIC-D500-1122", id_card: "550505199505055678", note: "合成新人",
  };

  const created = personnelSandbox({accessV3: false, members: [legacyMember]});
  await created.sandbox.renderStaffInto("personnelBody");
  assert.match(created.elements.personnelBody.innerHTML, />添加人员</);
  created.sandbox.openStaffForm(null);
  installFormElements(created.elements);
  created.selectedRoles.push({value: "nurse"}, {value: "doctor"}, {value: "nurse"});
  await created.sandbox.saveStaffForm();
  const post = created.calls.find(call => call.url === "/api/staff-members" && call.options.method === "POST");
  assert.deepStrictEqual(JSON.parse(post.options.body), expected);

  const edited = personnelSandbox({accessV3: false, members: [legacyMember]});
  await edited.sandbox.renderStaffInto("personnelBody");
  edited.sandbox.openStaffForm("staff-zhang");
  assert.match(edited.elements.personnelBody.innerHTML, /option value="doctor" selected/);
  installFormElements(edited.elements);
  edited.selectedRoles.push({value: "nurse"}, {value: "doctor"});
  await edited.sandbox.saveStaffForm();
  const put = edited.calls.find(call => call.url === "/api/staff-members/staff-zhang" && call.options.method === "PUT");
  assert.deepStrictEqual(JSON.parse(put.options.body), expected);
});

test("legacy account_role=admin 统一驱动概览、筛选、行摘要和详情", async () => {
  const legacyAdmin = {
    ...syntheticMembers[0], primary_role: undefined, role_keys: undefined,
    role: "医生", roles: ["医生"],
    account: {user_id: "legacy-admin", username: "legacy.demo", is_active: true, account_role: "admin"},
  };
  const target = personnelSandbox({accessV3: false, members: [legacyAdmin]});
  await target.sandbox.renderStaffInto("personnelBody");
  assert.match(target.elements.personnelBody.innerHTML, /<span>系统管理员<\/span><strong>1<\/strong>/);
  assert.match(target.elements.personnelBody.innerHTML, /账号正常[\s\S]*系统管理员：是/);

  const state = accessStaffState(target.sandbox);
  state.accountFilter = "admin";
  target.sandbox.renderStaffManager();
  assert.match(target.elements.personnelBody.innerHTML, /张明/);
  target.sandbox.openStaffDetails("staff-zhang");
  assert.match(target.elements.personnelBody.innerHTML, /<h3>系统管理员<\/h3><p>是<\/p>/);
});

test("双容器时滚动状态只读取当前 _smTarget 的列表", async () => {
  const target = personnelSandbox();
  const secondScroller = {scrollTop: 0, innerHTML: ""};
  target.elements.secondaryBody = {
    innerHTML: "",
    querySelector: selector => selector === ".access-staff-list-scroll" ? secondScroller : null,
  };
  await target.sandbox.renderStaffInto("personnelBody");
  await target.sandbox.renderStaffInto("secondaryBody");
  target.listScroller.scrollTop = 41;
  secondScroller.scrollTop = 226;
  target.sandbox.openStaffDetails("staff-zhang");
  assert.strictEqual(accessStaffState(target.sandbox).listScrollTop, 226);
  assert.strictEqual(secondScroller.scrollTop, 226);
});

test("ensureStaffAccts 完成刷新后不再重复渲染管理容器", async () => {
  const target = personnelSandbox();
  await target.sandbox.renderStaffInto("personnelBody");
  target.sandbox.appPrompt = async prompt => prompt.includes("用户名") ? "demo.user" : "pw";
  target.sandbox.alert = () => {};
  let html = target.elements.personnelBody.innerHTML;
  let renderCount = 0;
  Object.defineProperty(target.elements.personnelBody, "innerHTML", {
    configurable: true,
    get: () => html,
    set: value => { html = value; renderCount += 1; },
  });
  await target.sandbox.openStaffAccount("staff-zhang", "D100", "医生");
  assert.strictEqual(renderCount, 2, "加载态和完成态各渲染一次即可");
});

test("主岗位为空不发请求，保存失败保留 modal 和字段且不泄露后端细节", async () => {
  const target = personnelSandbox({saveOk: false});
  await target.sandbox.renderStaffInto("personnelBody");
  target.sandbox.openStaffForm(null);
  installFormElements(target.elements);
  target.elements.accessStaffPrimaryRole.value = "";
  const before = target.calls.length;
  await target.sandbox.saveStaffForm();
  assert.strictEqual(target.calls.length, before);
  assert.match(target.elements.accessStaffFormError.textContent, /主岗位/);
  target.elements.accessStaffPrimaryRole.value = "doctor";
  await target.sandbox.saveStaffForm();
  assert.strictEqual(accessStaffState(target.sandbox).formOpen, true);
  assert.strictEqual(accessStaffState(target.sandbox).formPending, false);
  assert.strictEqual(target.elements.accessStaffFormSave.disabled, false);
  assert.strictEqual(target.elements.accessStaffFormClose.disabled, false);
  assert.strictEqual(target.elements.accessStaffFormCancel.disabled, false);
  assert.strictEqual(target.elements.accessStaffName.value, "  孙芳  ");
  assert.match(target.elements.accessStaffFormError.textContent, /保存失败/);
  assert.doesNotMatch(target.elements.accessStaffFormError.textContent, /SQL|private\.db|token|secret|hidden/);

  const network = await openSyntheticForm({saveNetwork: true});
  await network.sandbox.saveStaffForm();
  assert.strictEqual(accessStaffState(network.sandbox).formOpen, true);
  assert.strictEqual(accessStaffState(network.sandbox).formPending, false);
  assert.strictEqual(network.elements.accessStaffFormSave.disabled, false);
  assert.strictEqual(network.elements.accessStaffFormClose.disabled, false);
  assert.strictEqual(network.elements.accessStaffFormCancel.disabled, false);
  assert.strictEqual(network.elements.accessStaffName.value, "  孙芳  ");
  assert.match(network.elements.accessStaffFormError.textContent, /保存失败/);
});

test("只有人员编辑权限才显示添加和编辑入口", async () => {
  const editable = personnelSandbox({canEdit: true});
  await editable.sandbox.renderStaffInto("personnelBody");
  assert.match(editable.elements.personnelBody.innerHTML, />添加人员</);
  assert.match(editable.elements.personnelBody.innerHTML, />编辑</);
  const readOnly = personnelSandbox({canEdit: false});
  await readOnly.sandbox.renderStaffInto("personnelBody");
  assert.doesNotMatch(readOnly.elements.personnelBody.innerHTML, />添加人员|>编辑</);
});

function installActionReason(elements, value) {
  elements.accessStaffActionReason = {value};
  elements.accessStaffActionError = {textContent: ""};
  elements.accessStaffActionConfirm = {disabled: false};
}

function deferredResponse() {
  let resolve;
  let reject;
  const promise = new Promise((done, fail) => { resolve = done; reject = fail; });
  return {promise, resolve, reject};
}

function staffRenderSnapshot(sandbox) {
  return vm.runInContext(`JSON.stringify({
    members: _accessStaffState.members,
    staffFull,
    staffAccts,
    loadState: _accessStaffState.loadState,
    error: _accessStaffState.error,
  })`, sandbox);
}

async function assertStaleStaffRenderNoOp(oldOutcome) {
  const target = personnelSandbox();
  const gates = [];
  target.sandbox.fetch = async (url, options = {}) => {
    target.calls.push({url, options});
    const gate = deferredResponse();
    gates.push(gate);
    return gate.promise;
  };

  const oldRender = target.sandbox.renderStaffInto("personnelBody");
  const currentRender = target.sandbox.renderStaffInto("personnelBody");
  assert.strictEqual(gates.length, 2);
  gates[1].resolve({
    ok: true,
    status: 200,
    json: async () => ({members: [syntheticMembers[2]], totalcount: 1}),
  });
  await currentRender;
  const stateAfterCurrent = staffRenderSnapshot(target.sandbox);
  const htmlAfterCurrent = target.elements.personnelBody.innerHTML;

  if (oldOutcome === "success") {
    gates[0].resolve({
      ok: true,
      status: 200,
      json: async () => ({members: [syntheticMembers[0]], totalcount: 1}),
    });
  } else {
    gates[0].reject(new Error("synthetic stale network failure"));
  }
  await oldRender;

  assert.strictEqual(staffRenderSnapshot(target.sandbox), stateAfterCurrent);
  assert.strictEqual(target.elements.personnelBody.innerHTML, htmlAfterCurrent);
}

test("同一人员目标的旧成功响应不能覆盖较新的加载结果", async () => {
  await assertStaleStaffRenderNoOp("success");
});

test("同一人员目标的旧失败响应不能清空较新的加载结果", async () => {
  await assertStaleStaffRenderNoOp("network-failure");
});

test("人员目标同 ID 节点在 JSON 等待期间被替换后，旧加载完全 no-op", async () => {
  const target = personnelSandbox();
  await target.sandbox.renderStaffInto("personnelBody");
  const originalNode = target.elements.personnelBody;
  const jsonGate = deferredResponse();
  const jsonStarted = deferredResponse();
  let jsonCalls = 0;
  target.sandbox.fetch = async (url, options = {}) => {
    target.calls.push({url, options});
    return {
      ok: true,
      status: 200,
      json: () => {
        jsonCalls += 1;
        jsonStarted.resolve();
        return jsonGate.promise;
      },
    };
  };

  const pendingRender = target.sandbox.renderStaffInto("personnelBody");
  await jsonStarted.promise;
  assert.strictEqual(jsonCalls, 1, "应已越过 fetch，停在 JSON await");
  const replacementNode = {
    innerHTML: "同 ID 新节点保留内容",
    querySelector: () => null,
  };
  target.elements.personnelBody = replacementNode;
  const stateBeforeOldJson = staffRenderSnapshot(target.sandbox);
  const originalHtmlBeforeOldJson = originalNode.innerHTML;
  jsonGate.resolve({members: [syntheticMembers[0]], totalcount: 1});
  await pendingRender;

  assert.strictEqual(staffRenderSnapshot(target.sandbox), stateBeforeOldJson);
  assert.strictEqual(replacementNode.innerHTML, "同 ID 新节点保留内容");
  assert.strictEqual(originalNode.innerHTML, originalHtmlBeforeOldJson);
});

test("离职人员在列表隐藏编辑入口，在职人员仍可编辑", async () => {
  const target = personnelSandbox({permissions: ["staff.edit"]});
  await target.sandbox.renderStaffInto("personnelBody");

  assert.match(target.sandbox._accessStaffRowHtml(syntheticMembers[0]), /access-staff-edit/);
  assert.doesNotMatch(target.sandbox._accessStaffRowHtml(syntheticMembers[1]), /access-staff-edit|>编辑<\/button>/);
});

test("离职人员在详情隐藏编辑入口，在职人员仍可编辑", async () => {
  const target = personnelSandbox({permissions: ["staff.edit"]});
  await target.sandbox.renderStaffInto("personnelBody");

  target.sandbox.openStaffDetails("staff-zhang");
  assert.match(target.sandbox._accessStaffDetailsHtml(), /access-sheet-edit/);
  target.sandbox.openStaffDetails("staff-li");
  assert.doesNotMatch(target.sandbox._accessStaffDetailsHtml(), /access-sheet-edit|>编辑<\/button>/);
});

test("直接请求编辑离职人员不改表单状态，新建人员仍可打开", async () => {
  const target = personnelSandbox({permissions: ["staff.edit"]});
  await target.sandbox.renderStaffInto("personnelBody");
  const state = accessStaffState(target.sandbox);
  const formStateBefore = JSON.stringify({
    open: state.formOpen,
    staffId: state.formStaffId,
    error: state.formError,
    pending: state.formPending,
    revision: state.formRevision,
  });
  const htmlBefore = target.elements.personnelBody.innerHTML;

  target.sandbox.openStaffForm("staff-li");

  assert.strictEqual(JSON.stringify({
    open: state.formOpen,
    staffId: state.formStaffId,
    error: state.formError,
    pending: state.formPending,
    revision: state.formRevision,
  }), formStateBefore);
  assert.strictEqual(target.elements.personnelBody.innerHTML, htmlBefore);
  assert.strictEqual(vm.runInContext("_smEditId", target.sandbox), null);

  target.sandbox.openStaffForm(null);
  assert.strictEqual(state.formOpen, true);
  assert.strictEqual(state.formStaffId, null);
  assert.match(target.elements.personnelBody.innerHTML, /id="accessStaffFormTitle">添加人员<\/h2>/);
});

test("人员详情按精确权限和目标状态显示人事、账号与系统管理员动作", async () => {
  const allActions = personnelSandbox({
    permissions: ["staff.edit", "staff.employment", "account.open", "account.security"],
    isSystemAdmin: true,
  });
  await allActions.sandbox.renderStaffInto("personnelBody");
  allActions.sandbox.openStaffDetails("staff-zhang");
  let html = allActions.elements.personnelBody.innerHTML;
  for (const label of ["编辑", "离职", "停用", "重置密码", "撤销系统管理员"]) {
    assert.match(html, new RegExp(`>${label}</button>`));
  }

  allActions.sandbox.openStaffDetails("staff-wang");
  html = allActions.elements.personnelBody.innerHTML;
  assert.match(html, />启用</);
  assert.doesNotMatch(html, />授予系统管理员</);
  assert.doesNotMatch(html, />停用</);
  const inactiveCalls = allActions.calls.length;
  allActions.sandbox.beginSystemAdminChange("staff-wang", true);
  assert.strictEqual(accessStaffState(allActions.sandbox).action, null);
  assert.doesNotMatch(allActions.elements.personnelBody.innerHTML, /id="accessStaffActionReason"/);
  assert.strictEqual(allActions.calls.length, inactiveCalls);

  const inactiveAdmin = {
    ...syntheticMembers[2],
    account: {...syntheticMembers[2].account, is_system_admin: true},
  };
  const revokeInactiveAdmin = personnelSandbox({members: [inactiveAdmin], isSystemAdmin: true});
  await revokeInactiveAdmin.sandbox.renderStaffInto("personnelBody");
  revokeInactiveAdmin.sandbox.openStaffDetails("staff-wang");
  assert.match(revokeInactiveAdmin.elements.personnelBody.innerHTML, />撤销系统管理员</);
  revokeInactiveAdmin.sandbox.beginSystemAdminChange("staff-wang", false);
  const revokeAction = accessStaffState(revokeInactiveAdmin.sandbox).action;
  assert.strictEqual(revokeAction.kind, "systemAdmin");
  assert.strictEqual(revokeAction.staffId, "staff-wang");
  assert.strictEqual(revokeAction.grant, false);
  assert.match(revokeInactiveAdmin.elements.personnelBody.innerHTML, /id="accessStaffActionReason"/);

  const leftWithAccount = {
    ...syntheticMembers[1],
    account: {user_id: "user-li", username: "li.demo", is_active: false, is_system_admin: false, account_kind: "staff"},
  };
  const departed = personnelSandbox({
    members: [leftWithAccount],
    permissions: ["staff.employment", "account.open", "account.security"],
    isSystemAdmin: true,
  });
  await departed.sandbox.renderStaffInto("personnelBody");
  departed.sandbox.openStaffDetails("staff-li");
  html = departed.elements.personnelBody.innerHTML;
  assert.match(html, />复职</);
  assert.doesNotMatch(html, />启用</);
  assert.doesNotMatch(html, />开户<\/button>/);

  const employedWithoutAccount = {
    ...syntheticMembers[1], staff_id: "staff-new", employment_status: "employed", left_at: null,
    left_reason: null, account: null,
  };
  const accountOpenOnly = personnelSandbox({members: [employedWithoutAccount], permissions: ["account.open"]});
  await accountOpenOnly.sandbox.renderStaffInto("personnelBody");
  accountOpenOnly.sandbox.openStaffDetails("staff-new");
  assert.match(accountOpenOnly.elements.personnelBody.innerHTML, />开户<\/button>/);

  const employmentOnly = personnelSandbox({permissions: ["staff.employment"]});
  await employmentOnly.sandbox.renderStaffInto("personnelBody");
  employmentOnly.sandbox.openStaffDetails("staff-zhang");
  html = employmentOnly.elements.personnelBody.innerHTML;
  assert.match(html, />离职</);
  assert.doesNotMatch(html, />编辑|>停用|>重置密码/);

  const nonAdminActor = personnelSandbox({
    permissions: ["staff.edit", "staff.employment", "account.open", "account.security"],
    isSystemAdmin: false,
  });
  await nonAdminActor.sandbox.renderStaffInto("personnelBody");
  nonAdminActor.sandbox.openStaffDetails("staff-zhang");
  assert.doesNotMatch(nonAdminActor.elements.personnelBody.innerHTML, /授予系统管理员|撤销系统管理员|>停用<\/button>|>启用<\/button>|>重置密码<\/button>/);
  const before = nonAdminActor.calls.length;
  const promptsBefore = nonAdminActor.promptCalls.length;
  await nonAdminActor.sandbox.beginStaffPasswordReset("staff-zhang");
  await nonAdminActor.sandbox.toggleStaffAccount("staff-zhang", false);
  assert.strictEqual(nonAdminActor.promptCalls.length, promptsBefore);
  assert.strictEqual(nonAdminActor.calls.length, before);
});

test("人员聚合 account 缺非空 user_id 时明示异常并对所有账号动作 fail-closed", async () => {
  for (const badUserId of [undefined, "", "   ", 42]) {
    const broken = {
      ...syntheticMembers[3],
      account: {...syntheticMembers[3].account, user_id: badUserId},
    };
    const variants = [
      {member: broken, employmentAction: />离职<\/button>/},
      {
        member: {
          ...broken, staff_id: "staff-broken-left", employment_status: "left",
          left_at: "2026-01-02", left_reason: "合成离职",
        },
        employmentAction: />复职<\/button>/,
      },
    ];
    for (const {member, employmentAction} of variants) {
      const target = personnelSandbox({
        members: [member],
        permissions: ["staff.employment", "account.open", "account.security"],
        isSystemAdmin: true,
        prompts: ["new.user", "pw", "reset"],
      });
      await target.sandbox.renderStaffInto("personnelBody");
      target.sandbox.openStaffDetails(member.staff_id);
      const html = target.elements.personnelBody.innerHTML;
      assert.match(html, /账号数据异常/);
      assert.match(html, employmentAction);
      assert.doesNotMatch(html, />开户<\/button>|>启用<\/button>|>停用<\/button>|>重置密码<\/button>|授予系统管理员|撤销系统管理员/);
      const before = target.calls.length;
      const promptsBefore = target.promptCalls.length;
      await target.sandbox.beginStaffAccountOpen(member.staff_id);
      await target.sandbox.beginStaffPasswordReset(member.staff_id);
      await target.sandbox.toggleStaffAccount(member.staff_id, false);
      target.sandbox.beginSystemAdminChange(member.staff_id, true);
      const state = accessStaffState(target.sandbox);
      state.action = {kind: "systemAdmin", staffId: member.staff_id, grant: true};
      installActionReason(target.elements, "异常数据不应提交");
      await target.sandbox.confirmSystemAdminChange();
      assert.strictEqual(target.promptCalls.length, promptsBefore);
      assert.strictEqual(target.calls.length, before);
      assert.ok(target.calls.every(call => !call.url.includes("undefined")));
    }
  }
});

test("离职应用内确认列明四项影响，原因非空后发 DELETE 并刷新两类人员数据", async () => {
  const target = personnelSandbox({permissions: ["staff.employment"]});
  await target.sandbox.renderStaffInto("personnelBody");
  target.sandbox.openStaffDetails("staff-zhang");
  assert.strictEqual(typeof target.sandbox.beginStaffDeparture, "function");
  assert.strictEqual(typeof target.sandbox.confirmStaffDeparture, "function");
  target.sandbox.beginStaffDeparture("staff-zhang");
  const modalHtml = target.elements.personnelBody.innerHTML;
  for (const phrase of ["业务选人中移除", "账号停用", "现有登录退出", "档案和历史记录保留"]) {
    assert.match(modalHtml, new RegExp(phrase));
  }
  installActionReason(target.elements, "  主动离职  ");
  await target.sandbox.confirmStaffDeparture();
  const departure = target.calls.find(call => call.url === "/api/staff-members/staff-zhang" && call.options.method === "DELETE");
  assert.ok(departure);
  assert.deepStrictEqual(JSON.parse(departure.options.body), {reason: "主动离职"});
  assert.strictEqual(target.calls.filter(call => call.url === "/api/staff-admin/list").length, 2);
  assert.ok(target.calls.some(call => call.url === "/api/staff-members" && !call.options.method));
  assert.strictEqual(target.teamRefreshes(), 1);
  assert.strictEqual(accessStaffState(target.sandbox).selectedStaffId, "staff-zhang");
  assert.doesNotMatch(target.elements.personnelBody.innerHTML, /id="accessStaffActionReason"/);
});

test("离职与复职原因不能为空，复职发正确 POST 并固定提示账号仍停用", async () => {
  const departure = personnelSandbox({permissions: ["staff.employment"]});
  await departure.sandbox.renderStaffInto("personnelBody");
  departure.sandbox.openStaffDetails("staff-zhang");
  departure.sandbox.beginStaffDeparture("staff-zhang");
  installActionReason(departure.elements, "   ");
  const before = departure.calls.length;
  await departure.sandbox.confirmStaffDeparture();
  assert.strictEqual(departure.calls.length, before);
  assert.match(departure.elements.accessStaffActionError.textContent, /原因不能为空/);
  assert.match(departure.elements.personnelBody.innerHTML, /id="accessStaffActionReason"/);

  const reinstate = personnelSandbox({permissions: ["staff.employment"]});
  await reinstate.sandbox.renderStaffInto("personnelBody");
  reinstate.sandbox.openStaffDetails("staff-li");
  assert.strictEqual(typeof reinstate.sandbox.beginStaffReinstate, "function");
  assert.strictEqual(typeof reinstate.sandbox.confirmStaffReinstate, "function");
  reinstate.sandbox.beginStaffReinstate("staff-li");
  installActionReason(reinstate.elements, " 返聘复职 ");
  await reinstate.sandbox.confirmStaffReinstate();
  const request = reinstate.calls.find(call => call.url === "/api/staff-members/staff-li/reinstate");
  assert.strictEqual(request.options.method, "POST");
  assert.deepStrictEqual(JSON.parse(request.options.body), {reason: "返聘复职"});
  assert.strictEqual(reinstate.alerts.at(-1), "人员已复职，账号仍停用");
  assert.strictEqual(reinstate.calls.filter(call => call.url === "/api/staff-admin/list").length, 2);
  assert.ok(reinstate.calls.some(call => call.url === "/api/staff-members" && !call.options.method));
  assert.strictEqual(reinstate.teamRefreshes(), 1);
});

test("人员账号开户、重置密码和启停接通现有接口，离职人员不能启用", async () => {
  const employedWithoutAccount = {
    ...syntheticMembers[1], staff_id: "staff-new", employment_status: "employed", left_at: null,
    left_reason: null, account: null,
  };
  const open = personnelSandbox({
    members: [employedWithoutAccount], permissions: ["account.open"], prompts: ["new.user", " p "],
  });
  await open.sandbox.renderStaffInto("personnelBody");
  open.sandbox.openStaffDetails("staff-new");
  assert.strictEqual(typeof open.sandbox.beginStaffAccountOpen, "function");
  await open.sandbox.beginStaffAccountOpen("staff-new");
  const opened = open.calls.find(call => call.url === "/api/staff-members/staff-new/account");
  assert.deepStrictEqual(JSON.parse(opened.options.body), {username: "new.user", password: " p "});
  assert.strictEqual(open.promptCalls[1][2].type, "password");
  assert.strictEqual(open.promptCalls[1][2].autocomplete, "new-password");
  assert.strictEqual(open.calls.filter(call => call.url === "/api/staff-admin/list").length, 2);

  const reset = personnelSandbox({permissions: ["account.security"], prompts: ["new-password"]});
  await reset.sandbox.renderStaffInto("personnelBody");
  reset.sandbox.openStaffDetails("staff-zhao");
  assert.strictEqual(typeof reset.sandbox.beginStaffPasswordReset, "function");
  await reset.sandbox.beginStaffPasswordReset("staff-zhao");
  const resetCall = reset.calls.find(call => call.url === "/api/users/user-zhao/reset-password");
  assert.deepStrictEqual(JSON.parse(resetCall.options.body), {password: "new-password"});
  assert.strictEqual(reset.promptCalls[0][2].type, "password");
  assert.strictEqual(reset.promptCalls[0][2].autocomplete, "new-password");

  const toggle = personnelSandbox({permissions: ["account.open"]});
  await toggle.sandbox.renderStaffInto("personnelBody");
  toggle.sandbox.openStaffDetails("staff-wang");
  assert.strictEqual(typeof toggle.sandbox.toggleStaffAccount, "function");
  await toggle.sandbox.toggleStaffAccount("staff-wang", true);
  const toggleCall = toggle.calls.find(call => call.url === "/api/users/user-wang" && call.options.method === "PUT");
  assert.deepStrictEqual(JSON.parse(toggleCall.options.body), {is_active: true});

  const leftWithAccount = {
    ...syntheticMembers[1],
    account: {user_id: "user-li", username: "li.demo", is_active: false, is_system_admin: false, account_kind: "staff"},
  };
  const left = personnelSandbox({members: [leftWithAccount], permissions: ["account.open"]});
  await left.sandbox.renderStaffInto("personnelBody");
  const leftCalls = left.calls.length;
  await left.sandbox.toggleStaffAccount("staff-li", true);
  assert.strictEqual(left.calls.length, leftCalls);
});

test("只有当前系统管理员能用应用内原因 modal 授予或撤销系统管理员", async () => {
  const grant = personnelSandbox({isSystemAdmin: true});
  await grant.sandbox.renderStaffInto("personnelBody");
  grant.sandbox.openStaffDetails("staff-zhao");
  assert.strictEqual(typeof grant.sandbox.beginSystemAdminChange, "function");
  assert.strictEqual(typeof grant.sandbox.confirmSystemAdminChange, "function");
  grant.sandbox.beginSystemAdminChange("staff-zhao", true);
  installActionReason(grant.elements, " 值班需要 ");
  await grant.sandbox.confirmSystemAdminChange();
  const grantCall = grant.calls.find(call => call.url === "/api/users/user-zhao/system-admin/grant");
  assert.strictEqual(grantCall.options.method, "POST");
  assert.deepStrictEqual(JSON.parse(grantCall.options.body), {reason: "值班需要"});
  assert.strictEqual(grant.calls.filter(call => call.url === "/api/staff-admin/list").length, 2);

  const revoke = personnelSandbox({isSystemAdmin: true});
  await revoke.sandbox.renderStaffInto("personnelBody");
  revoke.sandbox.openStaffDetails("staff-zhang");
  revoke.sandbox.beginSystemAdminChange("staff-zhang", false);
  installActionReason(revoke.elements, " 权限收回 ");
  await revoke.sandbox.confirmSystemAdminChange();
  assert.ok(revoke.calls.some(call => call.url === "/api/users/user-zhang/system-admin/revoke"));

  const ordinary = personnelSandbox({isSystemAdmin: false});
  await ordinary.sandbox.renderStaffInto("personnelBody");
  const before = ordinary.calls.length;
  ordinary.sandbox.beginSystemAdminChange("staff-zhao", true);
  assert.strictEqual(ordinary.calls.length, before);
  assert.doesNotMatch(ordinary.elements.personnelBody.innerHTML, /id="accessStaffActionReason"/);
});

test("离职、复职和系统管理员确认同步加锁，双击各只发送一次业务请求", async () => {
  const cases = [
    {
      name: "离职",
      target: personnelSandbox({permissions: ["staff.employment"]}),
      staffId: "staff-zhang",
      begin: "beginStaffDeparture",
      confirm: "confirmStaffDeparture",
      matches: (url, options) => url === "/api/staff-members/staff-zhang" && options.method === "DELETE",
    },
    {
      name: "复职",
      target: personnelSandbox({permissions: ["staff.employment"]}),
      staffId: "staff-li",
      begin: "beginStaffReinstate",
      confirm: "confirmStaffReinstate",
      matches: (url, options) => url === "/api/staff-members/staff-li/reinstate" && options.method === "POST",
    },
    {
      name: "系统管理员",
      target: personnelSandbox({isSystemAdmin: true}),
      staffId: "staff-zhao",
      begin: "beginSystemAdminChange",
      beginArgs: ["staff-zhao", true],
      confirm: "confirmSystemAdminChange",
      matches: (url, options) => url === "/api/users/user-zhao/system-admin/grant" && options.method === "POST",
    },
  ];

  for (const entry of cases) {
    const {target} = entry;
    await target.sandbox.renderStaffInto("personnelBody");
    target.sandbox.openStaffDetails(entry.staffId);
    target.sandbox[entry.begin](...(entry.beginArgs || [entry.staffId]));
    assert.match(target.elements.personnelBody.innerHTML, /id="accessStaffActionConfirm"/);
    installActionReason(target.elements, `${entry.name}原因`);
    const gate = deferredResponse();
    const baseFetch = target.sandbox.fetch;
    target.sandbox.fetch = async (url, options = {}) => {
      if (entry.matches(url, options)) {
        target.calls.push({url, options});
        return gate.promise;
      }
      return baseFetch(url, options);
    };

    const first = target.sandbox[entry.confirm]();
    const second = target.sandbox[entry.confirm]();
    const pendingWhileWaiting = accessStaffState(target.sandbox).actionPending;
    const disabledWhileWaiting = target.elements.accessStaffActionConfirm.disabled;
    const requestsWhileWaiting = target.calls.filter(call => entry.matches(call.url, call.options)).length;
    gate.resolve({ok: true, status: 200, json: async () => ({})});
    await Promise.all([first, second]);

    assert.strictEqual(pendingWhileWaiting, true, `${entry.name}首个请求发出前应同步加锁`);
    assert.strictEqual(disabledWhileWaiting, true, `${entry.name}等待响应时应禁用确认按钮`);
    assert.strictEqual(requestsWhileWaiting, 1, `${entry.name}双击只能发一个业务请求`);
    assert.strictEqual(accessStaffState(target.sandbox).actionPending, false);
    assert.strictEqual(accessStaffState(target.sandbox).action, null);
  }
});

test("三类确认请求失败后解除 pending，保留原因和动作 modal", async () => {
  const cases = [
    {
      name: "离职",
      target: personnelSandbox({permissions: ["staff.employment"]}),
      staffId: "staff-zhang",
      begin: "beginStaffDeparture",
      confirm: "confirmStaffDeparture",
      matches: (url, options) => url === "/api/staff-members/staff-zhang" && options.method === "DELETE",
      status: 403,
    },
    {
      name: "复职",
      target: personnelSandbox({permissions: ["staff.employment"]}),
      staffId: "staff-li",
      begin: "beginStaffReinstate",
      confirm: "confirmStaffReinstate",
      matches: (url, options) => url === "/api/staff-members/staff-li/reinstate" && options.method === "POST",
      network: true,
    },
    {
      name: "系统管理员",
      target: personnelSandbox({isSystemAdmin: true}),
      staffId: "staff-zhao",
      begin: "beginSystemAdminChange",
      beginArgs: ["staff-zhao", true],
      confirm: "confirmSystemAdminChange",
      matches: (url, options) => url === "/api/users/user-zhao/system-admin/grant" && options.method === "POST",
      status: 403,
    },
  ];

  for (const entry of cases) {
    const {target} = entry;
    await target.sandbox.renderStaffInto("personnelBody");
    target.sandbox.openStaffDetails(entry.staffId);
    target.sandbox[entry.begin](...(entry.beginArgs || [entry.staffId]));
    const reason = `${entry.name}失败仍保留`;
    installActionReason(target.elements, reason);
    const baseFetch = target.sandbox.fetch;
    target.sandbox.fetch = async (url, options = {}) => {
      if (!entry.matches(url, options)) return baseFetch(url, options);
      target.calls.push({url, options});
      if (entry.network) throw new Error("private network token");
      return {ok: false, status: entry.status, json: async () => ({detail: "private SQL token"})};
    };

    await target.sandbox[entry.confirm]();

    assert.strictEqual(accessStaffState(target.sandbox).actionPending, false, `${entry.name}失败后应解锁`);
    assert.strictEqual(target.elements.accessStaffActionConfirm.disabled, false, `${entry.name}失败后应重新启用确认按钮`);
    assert.strictEqual(target.elements.accessStaffActionReason.value, reason);
    assert.strictEqual(accessStaffState(target.sandbox).action.kind, entry.name === "系统管理员" ? "systemAdmin" : (entry.name === "离职" ? "departure" : "reinstate"));
    assert.match(target.elements.personnelBody.innerHTML, /id="accessStaffActionReason"/);
  }
});

test("400/403/409/500 和网络失败均显示稳定中文，保留侧栏、动作 modal 与原因", async () => {
  const cases = [
    {status: 400, expected: /请求内容/},
    {status: 403, expected: /权限不足/},
    {status: 409, expected: /当前状态/},
    {status: 500, expected: /服务异常/},
    {network: true, expected: /网络异常/},
  ];
  for (const entry of cases) {
    const target = personnelSandbox({permissions: ["staff.employment"]});
    await target.sandbox.renderStaffInto("personnelBody");
    target.sandbox.openStaffDetails("staff-zhang");
    target.sandbox.beginStaffDeparture("staff-zhang");
    installActionReason(target.elements, "保留的原因");
    const initialListLoads = target.calls.filter(call => call.url === "/api/staff-admin/list").length;
    target.sandbox.fetch = async (url, options = {}) => {
      target.calls.push({url, options});
      if (entry.network) throw new Error("private network token");
      return {ok: false, status: entry.status, json: async () => ({detail: "SQL /tmp/private.db token=secret hidden"})};
    };
    await target.sandbox.confirmStaffDeparture();
    assert.match(target.elements.accessStaffActionError.textContent, entry.expected);
    assert.doesNotMatch(target.elements.accessStaffActionError.textContent, /SQL|private\.db|token|secret|hidden/);
    assert.strictEqual(target.elements.accessStaffActionReason.value, "保留的原因");
    assert.strictEqual(accessStaffState(target.sandbox).selectedStaffId, "staff-zhang");
    assert.match(target.elements.personnelBody.innerHTML, /id="accessStaffActionReason"/);
    assert.strictEqual(target.calls.filter(call => call.url === "/api/staff-admin/list").length, initialListLoads);
    assert.strictEqual(target.teamRefreshes(), 0);
  }
});

test("开户、重置和启停失败不泄露后端 detail，不关闭人员侧栏也不刷新列表", async () => {
  const employedWithoutAccount = {
    ...syntheticMembers[1], staff_id: "staff-new", employment_status: "employed", left_at: null,
    left_reason: null, account: null,
  };
  const open = personnelSandbox({
    members: [employedWithoutAccount], permissions: ["account.open"], prompts: ["new.user", "pw"],
  });
  await open.sandbox.renderStaffInto("personnelBody");
  open.sandbox.openStaffDetails("staff-new");
  open.sandbox.fetch = async (url, options = {}) => {
    open.calls.push({url, options});
    return {ok: false, status: 403, json: async () => ({detail: "SQL private token"})};
  };
  await open.sandbox.beginStaffAccountOpen("staff-new");
  assert.match(open.alerts.at(-1), /权限不足/);
  assert.doesNotMatch(open.alerts.at(-1), /SQL|private|token/);
  assert.strictEqual(accessStaffState(open.sandbox).selectedStaffId, "staff-new");
  assert.strictEqual(open.calls.filter(call => call.url === "/api/staff-admin/list").length, 1);

  const reset = personnelSandbox({permissions: ["account.security"], prompts: ["pw"]});
  await reset.sandbox.renderStaffInto("personnelBody");
  reset.sandbox.openStaffDetails("staff-zhao");
  reset.sandbox.fetch = async (url, options = {}) => {
    reset.calls.push({url, options});
    return {ok: false, status: 500, json: async () => ({detail: "SQL private token"})};
  };
  await reset.sandbox.beginStaffPasswordReset("staff-zhao");
  assert.match(reset.alerts.at(-1), /服务异常/);
  assert.doesNotMatch(reset.alerts.at(-1), /SQL|private|token/);
  assert.strictEqual(accessStaffState(reset.sandbox).selectedStaffId, "staff-zhao");

  const toggle = personnelSandbox({permissions: ["account.open"]});
  await toggle.sandbox.renderStaffInto("personnelBody");
  toggle.sandbox.openStaffDetails("staff-wang");
  toggle.sandbox.fetch = async (url, options = {}) => {
    toggle.calls.push({url, options});
    throw new Error("private network token");
  };
  await toggle.sandbox.toggleStaffAccount("staff-wang", true);
  assert.match(toggle.alerts.at(-1), /网络异常/);
  assert.doesNotMatch(toggle.alerts.at(-1), /private|token/);
  assert.strictEqual(accessStaffState(toggle.sandbox).selectedStaffId, "staff-wang");
});
