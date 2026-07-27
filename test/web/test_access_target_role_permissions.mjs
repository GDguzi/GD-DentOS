import {test} from "node:test";
import assert from "node:assert";
import {readFileSync} from "node:fs";
import {fileURLToPath} from "node:url";
import {dirname, join} from "node:path";
import vm from "node:vm";

const here = dirname(fileURLToPath(import.meta.url));
const source = readFileSync(
  join(here, "..", "..", "local_app", "static", "role_permissions.js"),
  "utf8",
);

const permissions = [
  {
    key: "patient.profile.view",
    module: "患者档案与回收",
    object: "患者资料",
    label: "查看",
    description: "查看患者基本资料",
    risk: "normal",
    dependencies: [],
  },
  {
    key: "patient.profile.edit",
    module: "患者档案与回收",
    object: "患者资料",
    label: "编辑",
    description: "编辑患者基本资料",
    risk: "sensitive",
    dependencies: ["patient.profile.view"],
  },
  {
    key: "patient.image.view",
    module: "患者档案与回收",
    object: "影像资料",
    label: "查看",
    description: "查看患者影像资料",
    risk: "normal",
    dependencies: [],
  },
  {
    key: "consent.share",
    module: "知情同意",
    object: "知情同意书",
    label: "分享",
    description: "分享知情同意书",
    risk: "high",
    dependencies: ["patient.profile.view"],
  },
  {
    key: "appointment.view",
    module: "预约管理",
    object: "预约",
    label: "查看",
    description: "查看预约",
    risk: "normal",
    dependencies: [],
  },
  {
    key: "appointment.edit",
    module: "预约管理",
    object: "预约",
    label: "编辑",
    description: "编辑预约",
    risk: "sensitive",
    dependencies: ["appointment.view"],
  },
];

const systemAdminCapabilities = [
  {key: "system_admin.role_permissions.manage", label: "修改角色权限", assignable: false},
  {key: "system_admin.grant", label: "授予/撤销系统管理员", assignable: false},
  {key: "system_admin.backup.download", label: "下载数据库备份", assignable: false},
  {key: "system_admin.sync.run", label: "执行全量同步", assignable: false},
  {key: "system_admin.migration.run", label: "执行数据迁移", assignable: false},
];

function permissionPayload({
  doctor = [],
  nurse = ["appointment.view"],
  doctorName = "医生",
  nurseName = "护士",
} = {}) {
  const enabled = {doctor: new Set(doctor), nurse: new Set(nurse)};
  return {
    roles: [
      {role_key: "doctor", name: doctorName, is_system: true, assigned_staff_count: 3},
      {role_key: "nurse", name: nurseName, is_system: true, assigned_staff_count: 2},
    ],
    perms: permissions.map(item => ({...item, dependencies: [...item.dependencies]})),
    matrix: Object.fromEntries(
      Object.entries(enabled).map(([roleKey, keys]) => [
        roleKey,
        Object.fromEntries(permissions.map(item => [item.key, keys.has(item.key)])),
      ]),
    ),
    system_admin_capabilities: systemAdminCapabilities.map(item => ({...item})),
  };
}

function targetSaveResult({
  roleKey = "doctor",
  oldPermissions = [],
  newPermissions = ["appointment.view"],
  added = ["appointment.view"],
  removed = [],
  affectedStaffCount = 3,
} = {}) {
  return {
    ok: true,
    role_key: roleKey,
    old: [...oldPermissions],
    new: [...newPermissions],
    added: [...added],
    removed: [...removed],
    affected_staff_count: affectedStaffCount,
    changed: added.length > 0 || removed.length > 0,
  };
}

const legacyPayload = {
  roles: [
    {role_key: "doctor", name: "医生", is_system: true},
    {role_key: "nurse", name: "护士", is_system: true},
    {role_key: "admin", name: "管理员", is_system: true},
  ],
  perms: [
    {key: "patient.profile.view", module: "患者", label: "查看患者"},
    {key: "patient.profile.edit", module: "患者", label: "编辑患者"},
    {key: "appointment.view", module: "预约", label: "查看预约"},
  ],
  matrix: {
    doctor: {
      "patient.profile.view": true,
      "patient.profile.edit": false,
      "appointment.view": false,
    },
    nurse: {
      "patient.profile.view": false,
      "patient.profile.edit": false,
      "appointment.view": true,
    },
    admin: {
      "patient.profile.view": true,
      "patient.profile.edit": true,
      "appointment.view": true,
    },
  },
};

function clone(value) {
  return JSON.parse(JSON.stringify(value));
}

function makeElement(initial = {}) {
  const listeners = new Map();
  const listenerAddCounts = new Map();
  const element = {
    hidden: false,
    inert: false,
    value: "",
    textContent: "",
    disabled: false,
    focused: false,
    removed: false,
    isConnected: true,
    ownerDocument: null,
    parentElement: null,
    children: [],
    attributes: new Map(),
    selectionStart: 0,
    selectionEnd: 0,
    dataset: {},
    focus() {
      if (this.disabled || this.hidden || this.closest("[hidden]") || this.closest("[inert]")) {
        return;
      }
      if (this.ownerDocument && this.ownerDocument.activeElement) {
        this.ownerDocument.activeElement.focused = false;
      }
      this.focused = true;
      if (this.ownerDocument) {
        this.ownerDocument.activeElement = this;
        this.ownerDocument.dispatch("focusin", {target: this});
      }
    },
    contains(node) {
      for (let current = node; current; current = current.parentElement) {
        if (current === this) return true;
      }
      return false;
    },
    closest(selector) {
      for (let current = this; current; current = current.parentElement) {
        if (elementMatchesSelector(current, selector)) return current;
      }
      return null;
    },
    setAttribute(name, value) {
      this.attributes.set(String(name), String(value));
      if (name === "inert") this.inert = true;
    },
    getAttribute(name) {
      return this.attributes.has(String(name)) ? this.attributes.get(String(name)) : null;
    },
    hasAttribute(name) { return this.attributes.has(String(name)); },
    removeAttribute(name) {
      this.attributes.delete(String(name));
      if (name === "inert") this.inert = false;
    },
    querySelectorAll(selector) {
      const descendants = [];
      const visit = node => {
        node.children.forEach(child => {
          if (elementMatchesSelector(child, selector)) descendants.push(child);
          visit(child);
        });
      };
      visit(this);
      return [...new Set(descendants)];
    },
    querySelector(selector) { return this.querySelectorAll(selector)[0] || null; },
    setSelectionRange(start, end) {
      this.selectionStart = start;
      this.selectionEnd = end;
    },
    addEventListener(type, handler) {
      listenerAddCounts.set(type, (listenerAddCounts.get(type) || 0) + 1);
      const handlers = listeners.get(type) || [];
      if (!handlers.includes(handler)) handlers.push(handler);
      listeners.set(type, handlers);
    },
    removeEventListener(type, handler) {
      const handlers = (listeners.get(type) || []).filter(item => item !== handler);
      if (handlers.length) listeners.set(type, handlers);
      else listeners.delete(type);
    },
    listenerCount(type) { return (listeners.get(type) || []).length; },
    listenerAddCount(type) { return listenerAddCounts.get(type) || 0; },
    click() {
      if (this.disabled) return undefined;
      let result;
      for (const handler of listeners.get("click") || []) {
        result = handler({preventDefault() {}});
      }
      return result;
    },
    dispatch(type, event = {}) {
      let result;
      for (const handler of listeners.get(type) || []) {
        result = handler({
          target: this,
          isComposing: false,
          preventDefault() {},
          ...event,
        });
      }
      return result;
    },
    ...initial,
  };
  return element;
}

function dataNameToProperty(name) {
  return name.replace(/-([a-z])/g, (_, char) => char.toUpperCase());
}

function elementMatchesSelector(element, selector) {
  if (!element) return false;
  if (selector === "[hidden]") return element.hidden === true;
  if (selector === "[inert]") return element.inert === true || element.hasAttribute("inert");
  const dataMatch = /^\[data-([a-z0-9-]+)(?:="([^"]*)")?\]$/.exec(selector);
  if (dataMatch) {
    const property = dataNameToProperty(dataMatch[1]);
    return property in (element.dataset || {})
      && (dataMatch[2] === undefined || element.dataset[property] === dataMatch[2]);
  }
  return false;
}

function makeHtmlContainer() {
  let html = "";
  const container = makeElement({
    controls: [],
    idNodes: new Map(),
    regions: new Map(),
    allNodes: [],
  });
  const disconnect = () => {
    new Set(container.allNodes).forEach(node => {
      node.isConnected = false;
      node.focused = false;
    });
    container.regions.forEach(region => {
      region.isConnected = false;
      region.disconnect();
    });
  };
  container.disconnect = disconnect;
  Object.defineProperty(container, "innerHTML", {
    get() {
      let rendered = html;
      container.regions.forEach((region, name) => {
        const escapedName = name.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
        const tagName = region.tagName.toLowerCase();
        const pattern = new RegExp(
          `(<${tagName}\\b[^>]*data-rp-region="${escapedName}"[^>]*>)[\\s\\S]*?(<\\/${tagName}>)`,
        );
        rendered = rendered.replace(pattern, `$1${region.innerHTML}$2`);
      });
      return rendered;
    },
    set(value) {
      disconnect();
      html = String(value);
      container.controls = [];
      container.idNodes = new Map();
      container.regions = new Map();
      container.allNodes = [];
      container.children = [];
      const stack = [container];
      for (const match of html.matchAll(/<\/?([a-z0-9-]+)\b([^>]*)>/g)) {
        const tagName = match[1].toLowerCase();
        if (match[0].startsWith("</")) {
          while (stack.length > 1) {
            const closed = stack.pop();
            if (closed.tagName === tagName.toUpperCase()) break;
          }
          continue;
        }
        const attrs = match[2];
        const dataset = {};
        for (const dataMatch of attrs.matchAll(/data-([a-z0-9-]+)(?:="([^"]*)")?/g)) {
          dataset[dataNameToProperty(dataMatch[1])] = dataMatch[2] || "";
        }
        const id = /\sid="([^"]+)"/.exec(attrs)?.[1] || "";
        const node = makeElement({
          tagName: tagName.toUpperCase(),
          id,
          dataset,
          checked: /\schecked(?:\s|$)/.test(attrs),
          disabled: /\sdisabled(?:\s|$)/.test(attrs),
          hidden: /\shidden(?:\s|$)/.test(attrs),
          className: /\sclass="([^"]*)"/.exec(attrs)?.[1] || "",
          value: /\svalue="([^"]*)"/.exec(attrs)?.[1] || "",
          ownerDocument: container.ownerDocument,
          parentElement: stack.at(-1),
        });
        stack.at(-1).children.push(node);
        container.allNodes.push(node);
        if (id) {
          const escapedId = id.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
          const contents = new RegExp(
            `<${tagName}\\b[^>]*\\sid="${escapedId}"[^>]*>([\\s\\S]*?)<\\/${tagName}>`,
          ).exec(html)?.[1];
          if (contents !== undefined) node.textContent = visibleText(contents);
          container.idNodes.set(id, node);
        }
        if (["button", "input", "textarea"].includes(tagName)) {
          container.controls.push(node);
        }
        if (!["input", "br", "hr", "img", "meta", "link"].includes(tagName)
          && !match[0].endsWith("/>")) {
          stack.push(node);
        }
      }
      for (const match of html.matchAll(
        /<([a-z0-9-]+)\b([^>]*)\sdata-rp-region="([^"]+)"([^>]*)>/g,
      )) {
        const region = makeHtmlContainer();
        region.tagName = match[1].toUpperCase();
        region.dataset.rpRegion = match[3];
        const host = container.allNodes.find(
          node => node.dataset.rpRegion === match[3],
        );
        region.parentElement = host || container;
        if (host) host.children.push(region);
        region.setOwnerDocument(container.ownerDocument);
        container.regions.set(match[3], region);
      }
    },
  });
  Object.defineProperty(container, "textContent", {
    get() { return visibleText(container.innerHTML); },
    set(value) {
      container.innerHTML = String(value ?? "")
        .replaceAll("&", "&amp;")
        .replaceAll("<", "&lt;")
        .replaceAll(">", "&gt;");
    },
  });
  container.querySelectorAll = selector => {
    const regionMatch = /^\[data-rp-region="([^"]+)"\]$/.exec(selector);
    if (regionMatch) {
      const region = container.regions.get(regionMatch[1]);
      return region ? [region] : [];
    }
    const nested = Array.from(
      container.regions.values(),
      region => region.querySelectorAll(selector),
    ).flat();
    const dataMatch = /^(?:input|button)?\[data-([a-z0-9-]+)\]$/.exec(selector);
    if (dataMatch) {
      const property = dataNameToProperty(dataMatch[1]);
      return container.allNodes.filter(node => property in node.dataset).concat(nested);
    }
    if (selector === ".rp-save, [data-rp-save-selected], [data-rp-cancel-save]") {
      return container.controls.filter(control => (
        control.className.split(/\s+/).includes("rp-save")
        || "rpSaveSelected" in control.dataset
        || "rpCancelSave" in control.dataset
      )).concat(nested);
    }
    return nested;
  };
  container.querySelector = selector => container.querySelectorAll(selector)[0] || null;
  container.setOwnerDocument = ownerDocument => {
    container.ownerDocument = ownerDocument;
    container.allNodes.forEach(node => { node.ownerDocument = ownerDocument; });
    container.regions.forEach(region => region.setOwnerDocument(ownerDocument));
  };
  return container;
}

function makeSandbox({accessV3 = true, responses = [], payload = permissionPayload()} = {}) {
  let box = makeHtmlContainer();
  const elements = {};
  Object.defineProperty(elements, "box", {
    get() { return box; },
  });
  for (const id of [
    "rpMsg",
    "rpV3Confirm",
    "rpSaveDialog",
    "rpSavePreview",
    "rpPasswordStep",
    "rpCurrentPassword",
    "rpReasonStep",
    "rpReason",
  ]) {
    Object.defineProperty(elements, id, {
      get() { return elements.box.idNodes.get(id) || null; },
    });
  }
  const legacyCheckboxes = [
    {dataset: {role: "doctor", perm: "patient.profile.view"}, checked: true},
    {dataset: {role: "doctor", perm: "patient.profile.edit"}, checked: false},
    {dataset: {role: "nurse", perm: "appointment.view"}, checked: true},
    {dataset: {role: "admin", perm: "patient.profile.view"}, checked: true},
  ];
  const overlays = [];
  const keydownListeners = new Set();
  const documentListeners = new Map([
    ["keydown", keydownListeners],
    ["focusin", new Set()],
  ]);
  const createOverlay = () => {
    const cancel = makeElement({dataset: {rpLeave: "cancel"}});
    const confirm = makeElement({dataset: {rpLeave: "confirm"}});
    const overlay = makeElement({
      querySelector(selector) {
        if (selector === '[data-rp-leave="cancel"]') return cancel;
        if (selector === '[data-rp-leave="confirm"]') return confirm;
        return null;
      },
    });
    overlay.children = [cancel, confirm];
    cancel.parentElement = overlay;
    confirm.parentElement = overlay;
    overlay.ownerDocument = sandbox.document;
    cancel.ownerDocument = sandbox.document;
    confirm.ownerDocument = sandbox.document;
    overlay.remove = () => {
      overlay.removed = true;
      overlay.isConnected = false;
      cancel.isConnected = false;
      confirm.isConnected = false;
      const index = overlays.indexOf(overlay);
      if (index >= 0) overlays.splice(index, 1);
    };
    return overlay;
  };
  const calls = [];
  const queue = [...responses];
  const request = async (transport, url, options = {}) => {
    calls.push({transport, url, options});
    let response = queue.length
      ? queue.shift()
      : {ok: true, status: 200, body: payload};
    if (response && typeof response.then === "function") response = await response;
    return {
      ok: response.ok,
      status: response.status,
      json: async () => response.body,
    };
  };
  const sandbox = {
    __accessV3: accessV3,
    document: {
      activeElement: null,
      getElementById: id => (id === "box" ? elements.box : elements.box.idNodes.get(id) || null),
      querySelectorAll(selector) {
        if (selector === ".rp-table input[type=checkbox][data-role]") return legacyCheckboxes;
        return box.querySelectorAll(selector);
      },
      createElement: () => createOverlay(),
      body: {appendChild(overlay) { overlays.push(overlay); }},
      addEventListener(type, handler) {
        if (!documentListeners.has(type)) documentListeners.set(type, new Set());
        documentListeners.get(type).add(handler);
      },
      removeEventListener(type, handler) {
        documentListeners.get(type)?.delete(handler);
      },
      dispatch(type, event = {}) {
        let result;
        for (const handler of [...(documentListeners.get(type) || [])]) {
          result = handler({
            key: "",
            shiftKey: false,
            preventDefault() {},
            ...event,
          });
        }
        return result;
      },
    },
    escapeHtml: value => String(value ?? "")
      .replaceAll("&", "&amp;")
      .replaceAll('"', "&quot;")
      .replaceAll("<", "&lt;")
      .replaceAll(">", "&gt;"),
    fetch: (...args) => request("wrapped", ...args),
    _origFetch: (...args) => request("raw", ...args),
  };
  sandbox.window = sandbox;
  box.setOwnerDocument(sandbox.document);
  vm.createContext(sandbox);
  vm.runInContext(source, sandbox);
  return {
    sandbox,
    elements,
    legacyCheckboxes,
    calls,
    overlays,
    keydownListeners,
    replaceBox(innerHTML = "") {
      box.disconnect();
      box = makeHtmlContainer();
      box.innerHTML = innerHTML;
      box.setOwnerDocument(sandbox.document);
      return box;
    },
  };
}

function deferredResponse() {
  let resolve;
  let reject;
  const promise = new Promise((done, fail) => {
    resolve = done;
    reject = fail;
  });
  return {promise, resolve, reject};
}

function visibleText(html) {
  return String(html)
    .replace(/<[^>]*>/g, " ")
    .replaceAll("&amp;", "&")
    .replaceAll("&lt;", "<")
    .replaceAll("&gt;", ">")
    .replaceAll("&quot;", '"')
    .replace(/\s+/g, " ")
    .trim();
}

function renderedPermissionKeys(html) {
  return [...new Set(
    Array.from(
      String(html).matchAll(/data-rp-permission-key="([^"]+)"/g),
      match => match[1],
    ),
  )];
}

function findSubtreeInput(elements, module, object = "") {
  return elements.box.querySelectorAll("input[data-rp-subtree-level]")
    .find(input => (
      input.dataset.rpModule === module && input.dataset.rpObject === object
    )) || null;
}

function treeKidsHidden(elements) {
  return Object.fromEntries(
    elements.box.querySelectorAll("[data-rp-kids]")
      .map(node => [node.dataset.rpKids, node.hidden]),
  );
}

function plain(value) {
  return JSON.parse(JSON.stringify(value));
}

test("V3 只渲染单岗位树，岗位栏显示在岗人数且没有横向矩阵", async () => {
  const {sandbox, elements} = makeSandbox();
  await sandbox.renderRolePermsInto("box");

  assert.doesNotMatch(elements.box.innerHTML, /rp-table|<table\b|data-role=/);
  assert.match(elements.box.innerHTML, /data-rp-select-role="doctor"/);
  assert.match(elements.box.innerHTML, /aria-pressed="true"/);
  assert.strictEqual((
    elements.box.innerHTML.match(
      /data-rp-select-role="[^"]+"[^>]*aria-pressed="true"/g,
    ) || []
  ).length, 1);
  assert.match(visibleText(elements.box.innerHTML), /医生[^]*3 人/);
  assert.match(visibleText(elements.box.innerHTML), /护士[^]*2 人/);
  assert.match(visibleText(elements.box.innerHTML), /患者档案与回收[^]*患者资料[^]*查看/);
  assert.match(elements.box.innerHTML, /type="password"/);
  assert.match(elements.box.innerHTML, /autocomplete="current-password"/);
});

test("中栏显示服务器基线已授权数量，草稿变脏后同时显示修改后数量", async () => {
  const {sandbox, elements} = makeSandbox({
    payload: permissionPayload({doctor: ["patient.image.view"]}),
  });
  await sandbox.renderRolePermsInto("box");

  let count = elements.box.querySelector('[data-rp-region="authorized-count"]');
  assert.ok(count);
  assert.ok(count.closest("[data-rp-main]"));
  assert.strictEqual(visibleText(count.innerHTML), "已授权 1 项");

  sandbox.togglePermission("patient.profile.edit", true);
  count = elements.box.querySelector('[data-rp-region="authorized-count"]');
  assert.strictEqual(
    visibleText(count.innerHTML),
    "已授权 1 项，修改后 3 项",
  );
});

test("撤销和预览保存按钮归属右栏影响面板而不在中栏", async () => {
  const {sandbox, elements} = makeSandbox();
  await sandbox.renderRolePermsInto("box");

  for (const selector of ["[data-rp-restore]", "[data-rp-save-selected]"]) {
    const button = elements.box.querySelector(selector);
    assert.ok(button, selector);
    assert.ok(button.closest("[data-rp-impact-panel]"), selector);
    assert.strictEqual(button.closest("[data-rp-main]"), null, selector);
  }
});

test("模块和对象使用独立稳定键折叠，互不改写展开状态", async () => {
  const {sandbox, elements} = makeSandbox();
  await sandbox.renderRolePermsInto("box");

  assert.strictEqual(
    sandbox.roleTreeExpandedKey("module", "患者档案与回收"),
    "module:患者档案与回收",
  );
  assert.strictEqual(
    sandbox.roleTreeExpandedKey("object", "患者档案与回收", "患者资料"),
    "object:患者档案与回收:患者资料",
  );
  assert.strictEqual(sandbox.toggleRoleTreeSection("module", "患者档案与回收"), false);
  assert.strictEqual(
    sandbox.toggleRoleTreeSection("object", "患者档案与回收", "患者资料"),
    true,
  );
  assert.strictEqual(
    sandbox.toggleRoleTreeSection("object", "患者档案与回收", "患者资料"),
    false,
  );
  assert.strictEqual(sandbox.toggleRoleTreeSection("module", "患者档案与回收"), true);
  assert.match(elements.box.innerHTML, /data-rp-expanded-key="module:患者档案与回收"/);
  assert.match(elements.box.innerHTML, /data-rp-expanded-key="object:患者档案与回收:患者资料"/);
});

test("中文和技术键都能搜索，结果以模块/对象/操作三级树展示", async () => {
  const {sandbox, elements} = makeSandbox();
  await sandbox.renderRolePermsInto("box");

  sandbox.setRolePermissionSearch("编辑患者");
  assert.deepStrictEqual(renderedPermissionKeys(elements.box.innerHTML), ["patient.profile.edit"]);
  assert.match(elements.box.innerHTML, /data-rp-module-group="module:患者档案与回收"/);
  assert.match(elements.box.innerHTML, /data-rp-object-group="object:患者档案与回收:患者资料"/);
  assert.match(visibleText(elements.box.innerHTML), /患者档案与回收[^]*患者资料[^]*编辑/);
  assert.doesNotMatch(visibleText(elements.box.innerHTML), /patient\.profile\.edit/);

  sandbox.setRolePermissionSearch("patient.profile.edit");
  assert.deepStrictEqual(renderedPermissionKeys(elements.box.innerHTML), ["patient.profile.edit"]);
  assert.match(elements.box.innerHTML, /data-rp-module-group="module:患者档案与回收"/);
  assert.match(elements.box.innerHTML, /data-rp-object-group="object:患者档案与回收:患者资料"/);
  assert.match(visibleText(elements.box.innerHTML), /患者档案与回收[^]*患者资料[^]*编辑/);
  assert.doesNotMatch(visibleText(elements.box.innerHTML), /patient\.profile\.edit/);
});

test("搜索框真实 input 连续输入后保持当前节点焦点与光标并筛选中英文", async () => {
  const {sandbox, elements} = makeSandbox();
  await sandbox.renderRolePermsInto("box");

  const first = elements.box.querySelector("[data-rp-search]");
  first.focus();
  first.value = "编";
  first.setSelectionRange(1, 1);
  first.dispatch("input");

  const second = elements.box.querySelector("[data-rp-search]");
  assert.notStrictEqual(second, first);
  assert.strictEqual(first.isConnected, false);
  assert.strictEqual(second.isConnected, true);
  assert.strictEqual(second.focused, true);
  assert.deepStrictEqual([second.selectionStart, second.selectionEnd], [1, 1]);

  second.value = "编辑患者";
  second.setSelectionRange(4, 4);
  second.dispatch("input");
  const chineseResult = elements.box.querySelector("[data-rp-search]");
  assert.strictEqual(chineseResult.focused, true);
  assert.deepStrictEqual(
    [chineseResult.selectionStart, chineseResult.selectionEnd],
    [4, 4],
  );
  assert.deepStrictEqual(renderedPermissionKeys(elements.box.innerHTML), [
    "patient.profile.edit",
  ]);
  assert.match(elements.box.innerHTML, /data-rp-module-group="module:患者档案与回收"/);
  assert.match(elements.box.innerHTML, /data-rp-object-group="object:患者档案与回收:患者资料"/);

  chineseResult.value = "patient.profile.";
  chineseResult.setSelectionRange(16, 16);
  chineseResult.dispatch("input", {isComposing: true});
  assert.strictEqual(elements.box.querySelector("[data-rp-search]"), chineseResult);
  assert.strictEqual(chineseResult.focused, true);

  chineseResult.value = "patient.profile.edit";
  chineseResult.setSelectionRange(20, 20);
  chineseResult.dispatch("input");
  const technicalResult = elements.box.querySelector("[data-rp-search]");
  assert.strictEqual(technicalResult.focused, true);
  assert.deepStrictEqual(
    [technicalResult.selectionStart, technicalResult.selectionEnd],
    [20, 20],
  );
  assert.deepStrictEqual(renderedPermissionKeys(elements.box.innerHTML), [
    "patient.profile.edit",
  ]);
  assert.doesNotMatch(visibleText(elements.box.innerHTML), /patient\.profile\.edit/);
});

test("系统管理员五项能力完全取自响应并在树外只读展示", async () => {
  const payload = permissionPayload();
  payload.system_admin_capabilities[4].label = "响应中的迁移能力";
  const {sandbox, elements} = makeSandbox({payload});
  await sandbox.renderRolePermsInto("box");

  const section = /<section[^>]*data-rp-system-capabilities[^>]*>([\s\S]*?)<\/section>/.exec(
    elements.box.innerHTML,
  )?.[1] || "";
  for (const label of [
    "修改角色权限",
    "授予/撤销系统管理员",
    "下载数据库备份",
    "执行全量同步",
    "响应中的迁移能力",
  ]) assert.match(visibleText(section), new RegExp(label));
  assert.strictEqual((section.match(/系统管理员专属、不能分配给岗位/g) || []).length, 5);
  assert.doesNotMatch(section, /type="checkbox"/);
  assert.doesNotMatch(visibleText(section), /执行数据迁移/);
});

test("敏感操作仅按 API risk 过滤，数据范围只有未启用说明且不可保存", async () => {
  const payload = permissionPayload();
  payload.perms.find(item => item.key === "patient.image.view").risk = "high";
  const {sandbox, elements} = makeSandbox({payload});
  await sandbox.renderRolePermsInto("box");

  sandbox.setRolePermissionView("sensitive");
  assert.deepStrictEqual(renderedPermissionKeys(elements.box.innerHTML), [
    "patient.profile.edit",
    "patient.image.view",
    "consent.share",
    "appointment.edit",
  ]);

  sandbox.setRolePermissionView("scope");
  assert.match(visibleText(elements.box.innerHTML), /底层执行引擎尚未启用/);
  assert.deepStrictEqual(renderedPermissionKeys(elements.box.innerHTML), []);
  assert.doesNotMatch(elements.box.innerHTML, /data-rp-save-selected|type="checkbox"/);
});

test("权限树按模块/对象/操作三级渲染，默认仅第一模块展开且对象折叠", async () => {
  const {sandbox, elements} = makeSandbox();
  await sandbox.renderRolePermsInto("box");

  assert.deepStrictEqual(renderedPermissionKeys(elements.box.innerHTML), [
    "patient.profile.view",
    "patient.profile.edit",
    "patient.image.view",
    "consent.share",
    "appointment.view",
    "appointment.edit",
  ]);
  assert.match(elements.box.innerHTML, /data-rp-module-group="module:患者档案与回收"/);
  assert.match(elements.box.innerHTML, /data-rp-object-group="object:患者档案与回收:患者资料"/);
  const subtreeInputs = elements.box.querySelectorAll("input[data-rp-subtree-level]");
  assert.strictEqual(subtreeInputs.length, 7);
  subtreeInputs.forEach(input => {
    assert.ok(!("rpPermissionKey" in input.dataset));
  });
  assert.deepStrictEqual(treeKidsHidden(elements), {
    "module:患者档案与回收": false,
    "object:患者档案与回收:患者资料": true,
    "object:患者档案与回收:影像资料": true,
    "module:知情同意": true,
    "object:知情同意:知情同意书": true,
    "module:预约管理": true,
    "object:预约管理:预约": true,
  });
  assert.match(
    elements.box.innerHTML,
    /data-rp-expanded-key="module:患者档案与回收" aria-expanded="true"/,
  );
  assert.match(
    elements.box.innerHTML,
    /data-rp-expanded-key="module:预约管理" aria-expanded="false"/,
  );
  assert.match(
    elements.box.innerHTML,
    /data-rp-expanded-key="object:患者档案与回收:患者资料" aria-expanded="false"/,
  );
});

test("父级复选框按 draft 派生全选/半选/未选三态并显示勾选计数", async () => {
  const {sandbox, elements} = makeSandbox({
    payload: permissionPayload({doctor: ["patient.profile.view"]}),
  });
  await sandbox.renderRolePermsInto("box");

  let objectInput = findSubtreeInput(elements, "患者档案与回收", "患者资料");
  assert.strictEqual(objectInput.checked, false);
  assert.strictEqual(objectInput.indeterminate, true);
  let moduleInput = findSubtreeInput(elements, "患者档案与回收");
  assert.strictEqual(moduleInput.checked, false);
  assert.strictEqual(moduleInput.indeterminate, true);
  const emptyObject = findSubtreeInput(elements, "患者档案与回收", "影像资料");
  assert.strictEqual(emptyObject.checked, false);
  assert.strictEqual(emptyObject.indeterminate, false);
  assert.match(
    elements.box.innerHTML,
    /data-rp-expanded-key="object:患者档案与回收:患者资料"[^]*?>1\/2</,
  );
  assert.match(
    elements.box.innerHTML,
    /data-rp-expanded-key="module:患者档案与回收"[^]*?>1\/3</,
  );

  sandbox.togglePermission("patient.profile.edit", true);
  objectInput = findSubtreeInput(elements, "患者档案与回收", "患者资料");
  assert.strictEqual(objectInput.checked, true);
  assert.strictEqual(objectInput.indeterminate, false);
  moduleInput = findSubtreeInput(elements, "患者档案与回收");
  assert.strictEqual(moduleInput.checked, false);
  assert.strictEqual(moduleInput.indeterminate, true);
  assert.match(
    elements.box.innerHTML,
    /data-rp-expanded-key="module:患者档案与回收"[^]*?>2\/3</,
  );

  sandbox.togglePermission("patient.image.view", true);
  moduleInput = findSubtreeInput(elements, "患者档案与回收");
  assert.strictEqual(moduleInput.checked, true);
  assert.strictEqual(moduleInput.indeterminate, false);
  assert.match(
    elements.box.innerHTML,
    /data-rp-expanded-key="module:患者档案与回收"[^]*?>3\/3</,
  );
});

test("父级勾选级联整棵子树写入 draft，子树外缺失依赖照常补齐并提示", async () => {
  const {sandbox, elements} = makeSandbox();
  await sandbox.renderRolePermsInto("box");

  const consentModule = findSubtreeInput(elements, "知情同意");
  consentModule.checked = true;
  consentModule.dispatch("change");
  assert.deepStrictEqual(plain(sandbox.rolePermissionDiff()), {
    added: ["patient.profile.view", "consent.share"],
    removed: [],
    autoAddedDependencies: ["patient.profile.view"],
  });
  assert.match(
    visibleText(elements.box.innerHTML),
    /已自动补齐：患者档案与回收 \/ 患者资料 \/ 查看/,
  );
  const partialModule = findSubtreeInput(elements, "患者档案与回收");
  assert.strictEqual(partialModule.checked, false);
  assert.strictEqual(partialModule.indeterminate, true);

  const checkedConsent = findSubtreeInput(elements, "知情同意");
  assert.strictEqual(checkedConsent.checked, true);
  checkedConsent.checked = false;
  checkedConsent.dispatch("change");
  assert.deepStrictEqual(plain(sandbox.rolePermissionDiff()).added, [
    "patient.profile.view",
  ]);

  const appointmentObject = findSubtreeInput(elements, "预约管理", "预约");
  appointmentObject.checked = true;
  appointmentObject.dispatch("change");
  assert.deepStrictEqual(plain(sandbox.rolePermissionDiff()), {
    added: ["patient.profile.view", "appointment.view", "appointment.edit"],
    removed: [],
    autoAddedDependencies: ["patient.profile.view"],
  });

  const profileModule = findSubtreeInput(elements, "患者档案与回收");
  profileModule.checked = true;
  profileModule.dispatch("change");
  assert.deepStrictEqual(plain(sandbox.rolePermissionDiff()).added, [
    "patient.profile.view",
    "patient.profile.edit",
    "patient.image.view",
    "appointment.view",
    "appointment.edit",
  ]);
});

test("搜索命中时树只保留匹配叶子与祖先并强制展开，清空后恢复手动展开状态", async () => {
  const {sandbox, elements} = makeSandbox();
  await sandbox.renderRolePermsInto("box");

  sandbox.setRolePermissionSearch("编辑患者");
  assert.deepStrictEqual(renderedPermissionKeys(elements.box.innerHTML), [
    "patient.profile.edit",
  ]);
  assert.match(elements.box.innerHTML, /data-rp-module-group="module:患者档案与回收"/);
  assert.match(elements.box.innerHTML, /data-rp-object-group="object:患者档案与回收:患者资料"/);
  assert.doesNotMatch(elements.box.innerHTML, /data-rp-module-group="module:知情同意"/);
  assert.doesNotMatch(elements.box.innerHTML, /data-rp-object-group="object:患者档案与回收:影像资料"/);
  assert.deepStrictEqual(treeKidsHidden(elements), {
    "module:患者档案与回收": false,
    "object:患者档案与回收:患者资料": false,
  });
  assert.match(
    elements.box.innerHTML,
    /data-rp-expanded-key="object:患者档案与回收:患者资料" aria-expanded="true"/,
  );

  sandbox.setRolePermissionSearch("");
  assert.deepStrictEqual(treeKidsHidden(elements), {
    "module:患者档案与回收": false,
    "object:患者档案与回收:患者资料": true,
    "object:患者档案与回收:影像资料": true,
    "module:知情同意": true,
    "object:知情同意:知情同意书": true,
    "module:预约管理": true,
    "object:预约管理:预约": true,
  });
  assert.match(
    elements.box.innerHTML,
    /data-rp-expanded-key="module:预约管理" aria-expanded="false"/,
  );
});

test("敏感操作筛选在树中隐藏常规叶子、只留敏感分支并强制展开", async () => {
  const payload = permissionPayload();
  payload.perms.find(item => item.key === "patient.image.view").risk = "high";
  const {sandbox, elements} = makeSandbox({payload});
  await sandbox.renderRolePermsInto("box");

  sandbox.setRolePermissionView("sensitive");
  assert.deepStrictEqual(renderedPermissionKeys(elements.box.innerHTML), [
    "patient.profile.edit",
    "patient.image.view",
    "consent.share",
    "appointment.edit",
  ]);
  const kidsWhileFiltering = treeKidsHidden(elements);
  assert.ok(Object.keys(kidsWhileFiltering).length > 0);
  Object.values(kidsWhileFiltering).forEach(hidden => {
    assert.strictEqual(hidden, false);
  });

  sandbox.setRolePermissionView("all");
  assert.deepStrictEqual(treeKidsHidden(elements), {
    "module:患者档案与回收": false,
    "object:患者档案与回收:患者资料": true,
    "object:患者档案与回收:影像资料": true,
    "module:知情同意": true,
    "object:知情同意:知情同意书": true,
    "module:预约管理": true,
    "object:预约管理:预约": true,
  });
});

test("树上勾选驱动 draft，PUT 合同与逐键勾选完全一致", async () => {
  const payload = permissionPayload({doctor: ["patient.image.view"]});
  const {sandbox, elements, calls} = makeSandbox({
    payload,
    responses: [
      {ok: true, status: 200, body: payload},
      {ok: true, status: 200, body: {ok: true}},
      {
        ok: true,
        status: 200,
        body: targetSaveResult({
          oldPermissions: ["patient.image.view"],
          newPermissions: [
            "patient.profile.view",
            "patient.profile.edit",
            "patient.image.view",
            "appointment.view",
            "appointment.edit",
          ],
          added: [
            "patient.profile.view",
            "patient.profile.edit",
            "appointment.view",
            "appointment.edit",
          ],
        }),
      },
    ],
  });
  await sandbox.renderRolePermsInto("box");

  const leaf = elements.box.querySelectorAll("input[data-rp-permission-key]")
    .find(input => input.dataset.rpPermissionKey === "patient.profile.edit");
  leaf.checked = true;
  leaf.dispatch("change");
  const moduleInput = findSubtreeInput(elements, "预约管理");
  moduleInput.checked = true;
  moduleInput.dispatch("change");
  assert.deepStrictEqual(plain(sandbox.rolePermissionDiff()), {
    added: [
      "patient.profile.view",
      "patient.profile.edit",
      "appointment.view",
      "appointment.edit",
    ],
    removed: [],
    autoAddedDependencies: ["patient.profile.view"],
  });

  sandbox.beginRoleSavePreview("doctor");
  elements.rpCurrentPassword.value = "fixture-password";
  await sandbox.confirmRolePermPassword();
  elements.rpReason.value = "树形勾选保存";
  await sandbox.saveRolePerms();

  const putBody = JSON.parse(calls[2].options.body);
  assert.deepStrictEqual(putBody, {
    role_key: "doctor",
    permissions: [
      "patient.profile.view",
      "patient.profile.edit",
      "patient.image.view",
      "appointment.view",
      "appointment.edit",
    ],
    expected_permissions: ["patient.image.view"],
    reason: "树形勾选保存",
  });
  assert.match(elements.rpMsg.textContent, /^已保存/);
});

test("树上勾选保存遇 409 保留草稿、原因和预览 modal", async () => {
  const payload = permissionPayload();
  const {sandbox, elements} = makeSandbox({
    payload,
    responses: [
      {ok: true, status: 200, body: payload},
      {ok: true, status: 200, body: {ok: true}},
      {ok: false, status: 409, body: {detail: "role_permissions_stale"}},
    ],
  });
  await sandbox.renderRolePermsInto("box");

  const objectInput = findSubtreeInput(elements, "预约管理", "预约");
  objectInput.checked = true;
  objectInput.dispatch("change");
  sandbox.beginRoleSavePreview("doctor");
  elements.rpCurrentPassword.value = "fixture-password";
  await sandbox.confirmRolePermPassword();
  elements.rpReason.value = "树形勾选 409 保留";
  await sandbox.saveRolePerms();

  assert.strictEqual(elements.rpV3Confirm.hidden, false);
  assert.strictEqual(elements.rpReasonStep.hidden, false);
  assert.strictEqual(elements.rpReason.value, "树形勾选 409 保留");
  assert.match(
    elements.rpMsg.textContent,
    /权限已被其他管理员修改[^]*当前草稿已保留/,
  );
  assert.deepStrictEqual(plain(sandbox.rolePermissionDiff()), {
    added: ["appointment.view", "appointment.edit"],
    removed: [],
    autoAddedDependencies: [],
  });
});

test("勾选权限自动补齐 API 依赖，依赖仍在使用时阻止取消并显示中文名", async () => {
  const {sandbox, elements} = makeSandbox();
  await sandbox.renderRolePermsInto("box");

  assert.strictEqual(sandbox.togglePermission("patient.profile.edit", true), true);
  assert.deepStrictEqual(plain(sandbox.rolePermissionDiff()), {
    added: ["patient.profile.view", "patient.profile.edit"],
    removed: [],
    autoAddedDependencies: ["patient.profile.view"],
  });
  assert.match(
    visibleText(elements.box.innerHTML),
    /已自动补齐：患者档案与回收 \/ 患者资料 \/ 查看/,
  );
  assert.doesNotMatch(visibleText(elements.box.innerHTML), /patient\.profile\.view/);
  assert.strictEqual(sandbox.togglePermission("patient.profile.view", false), false);
  assert.match(visibleText(elements.box.innerHTML), /患者档案与回收 \/ 患者资料 \/ 编辑/);
  assert.deepStrictEqual(plain(sandbox.rolePermissionDiff()).added, [
    "patient.profile.view",
    "patient.profile.edit",
  ]);
});

test("右栏实时列出影响人数、新增、移除和自动依赖，预览 modal 重复同一份中文明细", async () => {
  const {sandbox, elements} = makeSandbox({
    payload: permissionPayload({doctor: ["patient.image.view"]}),
  });
  await sandbox.renderRolePermsInto("box");

  sandbox.togglePermission("patient.profile.edit", true);
  sandbox.togglePermission("patient.image.view", false);

  const impact = elements.box.querySelector('[data-rp-region="impact"]');
  assert.ok(impact);
  const liveText = visibleText(impact.innerHTML);
  assert.match(elements.box.innerHTML, /<aside\b[^>]*data-rp-impact-panel/);
  assert.match(liveText, /影响人数：3 人/);
  assert.match(liveText, /新增：2 项/);
  assert.match(liveText, /移除：1 项/);
  assert.match(liveText, /新增权限[^]*患者档案与回收 \/ 患者资料 \/ 查看/);
  assert.match(liveText, /新增权限[^]*患者档案与回收 \/ 患者资料 \/ 编辑/);
  assert.match(liveText, /移除权限[^]*患者档案与回收 \/ 影像资料 \/ 查看/);
  assert.match(liveText, /自动补齐依赖[^]*患者档案与回收 \/ 患者资料 \/ 查看/);
  assert.doesNotMatch(liveText, /patient\.|appointment\./);
  assert.match(visibleText(elements.box.innerHTML), /预览影响并保存/);

  elements.box.querySelector("[data-rp-save-selected]").click();
  assert.strictEqual(elements.rpV3Confirm.hidden, false);
  assert.match(elements.box.innerHTML, /id="rpV3Confirm"[^>]*class="modal-backdrop"/);
  assert.match(elements.box.innerHTML, /role="dialog"[^>]*aria-modal="true"/);
  assert.strictEqual(visibleText(elements.rpSavePreview.innerHTML), liveText);
  assert.strictEqual(elements.rpPasswordStep.hidden, false);
  assert.strictEqual(elements.rpReasonStep.hidden, true);
});

test("保存 modal 与 inert 背景同级且两阶段共用可见取消按钮", async () => {
  const {sandbox, elements} = makeSandbox();
  await sandbox.renderRolePermsInto("box");
  const background = elements.box.querySelector("[data-rp-modal-background]");
  const panel = elements.rpV3Confirm;
  const dialog = sandbox.document.getElementById("rpSaveDialog");
  const cancel = elements.box.querySelector("[data-rp-cancel-save]");

  assert.ok(background);
  assert.strictEqual(background.parentElement === panel.parentElement, true);
  assert.strictEqual(background.inert, false);
  assert.strictEqual(background.getAttribute("aria-hidden"), null);
  assert.strictEqual(dialog.hidden, true);
  assert.strictEqual(dialog.getAttribute("aria-hidden"), "true");

  sandbox.togglePermission("appointment.view", true);
  const preview = elements.box.querySelector("[data-rp-save-selected]");
  preview.focus();
  preview.click();
  assert.strictEqual(background.inert, true);
  assert.strictEqual(background.hasAttribute("inert"), true);
  assert.strictEqual(background.getAttribute("aria-hidden"), "true");
  assert.strictEqual(dialog.hidden, false);
  assert.strictEqual(dialog.getAttribute("aria-hidden"), "false");
  assert.strictEqual(cancel.closest("[hidden]"), null);

  elements.rpCurrentPassword.value = "fixture-password";
  await sandbox.confirmRolePermPassword();
  assert.strictEqual(elements.rpReasonStep.hidden, false);
  assert.strictEqual(cancel.closest("[hidden]"), null);
  cancel.click();
  assert.strictEqual(panel.hidden, true);
  assert.strictEqual(background.inert, false);
  assert.strictEqual(background.hasAttribute("inert"), false);
  assert.strictEqual(background.getAttribute("aria-hidden"), null);
  assert.strictEqual(dialog.hidden, true);
  assert.strictEqual(dialog.getAttribute("aria-hidden"), "true");
  assert.strictEqual(sandbox.document.activeElement === preview, true);
});

test("非保存期间 Esc 在密码和原因阶段关闭 modal 并恢复预览按钮焦点", async () => {
  const {sandbox, elements} = makeSandbox();
  await sandbox.renderRolePermsInto("box");
  sandbox.togglePermission("appointment.view", true);
  const preview = elements.box.querySelector("[data-rp-save-selected]");
  preview.focus();
  preview.click();
  assert.strictEqual(sandbox.document.activeElement === elements.rpCurrentPassword, true);

  sandbox.document.dispatch("keydown", {key: "Escape"});
  assert.strictEqual(elements.rpV3Confirm.hidden, true);
  assert.strictEqual(sandbox.document.activeElement === preview, true);

  preview.click();
  elements.rpCurrentPassword.value = "fixture-password";
  await sandbox.confirmRolePermPassword();
  assert.strictEqual(sandbox.document.activeElement === elements.rpReason, true);
  sandbox.document.dispatch("keydown", {key: "Escape"});
  assert.strictEqual(elements.rpV3Confirm.hidden, true);
  assert.strictEqual(sandbox.document.activeElement === preview, true);
});

test("Tab 与 Shift+Tab 在 modal 内循环且外部焦点会拉回当前阶段", async () => {
  const {sandbox, elements} = makeSandbox();
  await sandbox.renderRolePermsInto("box");
  sandbox.togglePermission("appointment.view", true);
  const preview = elements.box.querySelector("[data-rp-save-selected]");
  const cancel = elements.box.querySelector("[data-rp-cancel-save]");
  preview.focus();
  preview.click();

  cancel.focus();
  assert.strictEqual(sandbox.document.activeElement === cancel, true);
  sandbox.document.dispatch("keydown", {key: "Tab"});
  assert.strictEqual(sandbox.document.activeElement === elements.rpCurrentPassword, true);

  elements.rpCurrentPassword.value = "fixture-password";
  await sandbox.confirmRolePermPassword();
  elements.rpReason.focus();
  sandbox.document.dispatch("keydown", {key: "Tab", shiftKey: true});
  assert.strictEqual(sandbox.document.activeElement === cancel, true);
  sandbox.document.dispatch("keydown", {key: "Tab"});
  assert.strictEqual(sandbox.document.activeElement === elements.rpReason, true);

  sandbox.document.activeElement = preview;
  sandbox.document.dispatch("focusin", {target: preview});
  assert.strictEqual(sandbox.document.activeElement === elements.rpReason, true);
});

test("上层离开确认接管 Tab 循环并隐藏底层保存 dialog", async () => {
  const {sandbox, elements, overlays, keydownListeners} = makeSandbox();
  await sandbox.renderRolePermsInto("box");
  sandbox.togglePermission("appointment.view", true);
  sandbox.beginRoleSavePreview("doctor");

  const leaving = sandbox.requestLeaveRolePermissions();
  const overlay = overlays[0];
  const cancel = overlay.querySelector('[data-rp-leave="cancel"]');
  const confirm = overlay.querySelector('[data-rp-leave="confirm"]');
  assert.strictEqual(sandbox.document.activeElement === cancel, true);

  sandbox.document.dispatch("keydown", {key: "Tab", shiftKey: true});
  assert.strictEqual(sandbox.document.activeElement === confirm, true);
  sandbox.document.dispatch("keydown", {key: "Tab"});
  assert.strictEqual(sandbox.document.activeElement === cancel, true);
  assert.strictEqual(elements.rpSaveDialog.inert, true);
  assert.strictEqual(elements.rpSaveDialog.getAttribute("aria-hidden"), "true");
  assert.strictEqual(keydownListeners.size, 1);

  cancel.click();
  assert.strictEqual(await leaving, false);
});

test("上层 Esc 只取消离开确认并恢复密码阶段底层 trap", async () => {
  const {sandbox, elements, overlays, keydownListeners} = makeSandbox();
  await sandbox.renderRolePermsInto("box");
  sandbox.togglePermission("appointment.view", true);
  sandbox.beginRoleSavePreview("doctor");

  const leaving = sandbox.requestLeaveRolePermissions();
  assert.strictEqual(overlays.length, 1);
  sandbox.document.dispatch("keydown", {key: "Escape"});

  assert.strictEqual(await leaving, false);
  assert.strictEqual(overlays.length, 0);
  assert.strictEqual(elements.rpV3Confirm.hidden, false);
  assert.strictEqual(elements.rpSaveDialog.inert, false);
  assert.strictEqual(elements.rpSaveDialog.getAttribute("aria-hidden"), "false");
  assert.strictEqual(sandbox.document.activeElement === elements.rpCurrentPassword, true);
  assert.strictEqual(keydownListeners.size, 1);
});

test("取消上层离开确认恢复原因阶段焦点和底层 Tab 循环", async () => {
  const {sandbox, elements, overlays, keydownListeners} = makeSandbox();
  await sandbox.renderRolePermsInto("box");
  await enterRolePermissionReasonStep(sandbox, elements);

  const leaving = sandbox.requestLeaveRolePermissions();
  overlays[0].querySelector('[data-rp-leave="cancel"]').click();

  assert.strictEqual(await leaving, false);
  assert.strictEqual(elements.rpV3Confirm.hidden, false);
  assert.strictEqual(elements.rpReasonStep.hidden, false);
  assert.strictEqual(sandbox.document.activeElement === elements.rpReason, true);
  assert.strictEqual(keydownListeners.size, 1);
  const cancel = elements.box.querySelector("[data-rp-cancel-save]");
  cancel.focus();
  sandbox.document.dispatch("keydown", {key: "Tab"});
  assert.strictEqual(sandbox.document.activeElement === elements.rpReason, true);
});

test("确认上层离开统一清理两层且不恢复底层 trap", async () => {
  const {sandbox, elements, overlays, keydownListeners} = makeSandbox();
  await sandbox.renderRolePermsInto("box");
  await enterRolePermissionReasonStep(sandbox, elements);

  const leaving = sandbox.requestLeaveRolePermissions();
  const topConfirm = overlays[0].querySelector('[data-rp-leave="confirm"]');
  topConfirm.focus();
  topConfirm.click();

  assert.strictEqual(await leaving, true);
  assert.strictEqual(overlays.length, 0);
  assert.strictEqual(elements.rpV3Confirm.hidden, true);
  assert.strictEqual(elements.rpSaveDialog.hidden, true);
  assert.strictEqual(elements.rpSaveDialog.inert, false);
  assert.strictEqual(elements.rpSaveDialog.getAttribute("aria-hidden"), "true");
  assert.strictEqual(keydownListeners.size, 0);
  assert.strictEqual(elements.rpSaveDialog.contains(sandbox.document.activeElement), false);
  assert.deepStrictEqual(plain(sandbox.rolePermissionDiff()).added, []);
});

test("保存中取消按钮禁用且 Esc 不关闭 modal", async () => {
  const put = deferredResponse();
  const {sandbox, elements} = makeSandbox({
    responses: [
      {ok: true, status: 200, body: permissionPayload()},
      {ok: true, status: 200, body: {ok: true}},
      put.promise,
    ],
  });
  await sandbox.renderRolePermsInto("box");
  await enterRolePermissionReasonStep(sandbox, elements);
  const cancel = elements.box.querySelector("[data-rp-cancel-save]");
  const saving = sandbox.saveRolePerms();
  await Promise.resolve();

  assert.strictEqual(cancel.disabled, true);
  sandbox.document.dispatch("keydown", {key: "Escape"});
  cancel.click();
  assert.strictEqual(elements.rpV3Confirm.hidden, false);

  put.resolve({ok: true, status: 200, body: targetSaveResult()});
  await saving;
  assert.strictEqual(elements.rpV3Confirm.hidden, true);
});

test("保存成功重绘后有后续草稿时聚焦新的可用预览按钮", async () => {
  const put = deferredResponse();
  const {sandbox, elements} = makeSandbox({
    responses: [
      {ok: true, status: 200, body: permissionPayload()},
      {ok: true, status: 200, body: {ok: true}},
      put.promise,
    ],
  });
  await sandbox.renderRolePermsInto("box");
  await enterRolePermissionReasonStep(sandbox, elements);
  const saving = sandbox.saveRolePerms();
  await Promise.resolve();
  sandbox.togglePermission("patient.image.view", true);
  put.resolve({ok: true, status: 200, body: targetSaveResult()});
  await saving;

  const preview = elements.box.querySelector("[data-rp-save-selected]");
  assert.strictEqual(preview.disabled, false);
  assert.strictEqual(sandbox.document.activeElement === preview, true);
});

test("保存成功重绘后无后续草稿时聚焦当前岗位按钮", async () => {
  const {sandbox, elements} = makeSandbox({
    responses: [
      {ok: true, status: 200, body: permissionPayload()},
      {ok: true, status: 200, body: {ok: true}},
      {ok: true, status: 200, body: targetSaveResult()},
    ],
  });
  await sandbox.renderRolePermsInto("box");
  await enterRolePermissionReasonStep(sandbox, elements);
  await sandbox.saveRolePerms();

  const preview = elements.box.querySelector("[data-rp-save-selected]");
  const selectedRole = elements.box.querySelectorAll("[data-rp-select-role]")
    .find(button => button.dataset.rpSelectRole === "doctor");
  assert.strictEqual(preview.disabled, true);
  assert.strictEqual(sandbox.document.activeElement === selectedRole, true);
  assert.strictEqual(selectedRole.closest("[hidden]"), null);
});

test("重新验密回退先显示密码阶段再聚焦密码框", async () => {
  const {sandbox, elements} = makeSandbox({
    responses: [
      {ok: true, status: 200, body: permissionPayload()},
      {ok: true, status: 200, body: {ok: true}},
      {
        ok: false,
        status: 403,
        body: {detail: "recent_reauthentication_required"},
      },
    ],
  });
  await sandbox.renderRolePermsInto("box");
  await enterRolePermissionReasonStep(sandbox, elements);
  assert.strictEqual(sandbox.document.activeElement === elements.rpReason, true);

  await sandbox.saveRolePerms();

  assert.strictEqual(elements.rpPasswordStep.hidden, false);
  assert.strictEqual(elements.rpReasonStep.hidden, true);
  assert.strictEqual(sandbox.document.activeElement === elements.rpCurrentPassword, true);
});

test("无变更时不能进入预览和保存流程", async () => {
  const {sandbox, elements, calls} = makeSandbox();
  await sandbox.renderRolePermsInto("box");

  const saveButton = elements.box.querySelector("[data-rp-save-selected]");
  assert.strictEqual(saveButton.disabled, true);
  sandbox.beginRoleSavePreview("doctor");

  assert.strictEqual(elements.rpV3Confirm.hidden, true);
  assert.strictEqual(calls.length, 1);
  assert.deepStrictEqual(plain(sandbox.rolePermissionDiff()), {
    added: [],
    removed: [],
    autoAddedDependencies: [],
  });
});

test("父级全清按集合原子移除，只有子树外依赖才整体阻止", async () => {
  const {sandbox, elements} = makeSandbox();
  await sandbox.renderRolePermsInto("box");

  sandbox.togglePermission("patient.profile.edit", true);
  assert.strictEqual(sandbox.togglePermissionSubtree([
    "patient.profile.view",
    "patient.profile.edit",
  ], false), true);
  assert.deepStrictEqual(plain(sandbox.rolePermissionDiff()).added, []);

  sandbox.togglePermission("patient.profile.edit", true);
  sandbox.togglePermission("consent.share", true);
  const before = plain(sandbox.rolePermissionDiff());
  assert.strictEqual(sandbox.togglePermissionSubtree([
    "patient.profile.view",
    "patient.profile.edit",
  ], false), false);
  assert.deepStrictEqual(plain(sandbox.rolePermissionDiff()), before);
  assert.match(visibleText(elements.box.innerHTML), /知情同意 \/ 知情同意书 \/ 分享/);
});

test("baseline 与 draft 不同显示未保存，撤销恢复当前岗位 baseline", async () => {
  const {sandbox, elements} = makeSandbox();
  await sandbox.renderRolePermsInto("box");

  sandbox.togglePermission("appointment.view", true);
  assert.match(visibleText(elements.box.innerHTML), /未保存/);
  assert.deepStrictEqual(plain(sandbox.rolePermissionDiff()).added, ["appointment.view"]);
  sandbox.restoreRoleBaseline();
  assert.deepStrictEqual(plain(sandbox.rolePermissionDiff()), {
    added: [],
    removed: [],
    autoAddedDependencies: [],
  });
  assert.doesNotMatch(visibleText(elements.box.innerHTML), /未保存/);
});

test("切岗位先应用内确认：取消不动岗位和草稿，确认丢弃后切回保持 baseline", async () => {
  const {sandbox, elements, overlays} = makeSandbox();
  await sandbox.renderRolePermsInto("box");
  sandbox.togglePermission("appointment.view", true);

  const cancelled = sandbox.selectRole("nurse");
  assert.strictEqual(overlays.length, 1);
  overlays[0].querySelector('[data-rp-leave="cancel"]').click();
  assert.strictEqual(await cancelled, false);
  assert.match(elements.box.innerHTML, /data-rp-select-role="doctor"[^>]*aria-pressed="true"/);
  assert.deepStrictEqual(plain(sandbox.rolePermissionDiff()).added, ["appointment.view"]);

  const confirmed = sandbox.selectRole("nurse");
  assert.strictEqual(overlays.length, 1);
  overlays[0].querySelector('[data-rp-leave="confirm"]').click();
  assert.strictEqual(await confirmed, true);
  assert.match(elements.box.innerHTML, /data-rp-select-role="nurse"[^>]*aria-pressed="true"/);
  assert.strictEqual(await sandbox.selectRole("doctor"), true);
  assert.deepStrictEqual(plain(sandbox.rolePermissionDiff()).added, []);
});

test("迟到旧 GET 成功或失败都不能覆盖新 DOM 和新状态", async () => {
  const oldGet = deferredResponse();
  const latest = permissionPayload({doctorName: "最新医生", nurseName: "最新护士"});
  const {sandbox, elements, overlays} = makeSandbox({
    responses: [oldGet.promise, {ok: true, status: 200, body: latest}],
  });

  const oldRender = sandbox.renderRolePermsInto("box");
  await Promise.resolve();
  await sandbox.renderRolePermsInto("box");
  sandbox.togglePermission("appointment.view", true);
  oldGet.resolve({ok: true, status: 200, body: permissionPayload()});
  await oldRender;

  assert.match(visibleText(elements.box.innerHTML), /最新医生/);
  assert.doesNotMatch(visibleText(elements.box.innerHTML), /加载失败/);
  const leaving = sandbox.requestLeaveRolePermissions();
  assert.strictEqual(overlays.length, 1);
  overlays[0].querySelector('[data-rp-leave="cancel"]').click();
  assert.strictEqual(await leaving, false);

  const failedOldGet = deferredResponse();
  const second = makeSandbox({
    responses: [failedOldGet.promise, {ok: true, status: 200, body: latest}],
  });
  const failedRender = second.sandbox.renderRolePermsInto("box");
  await Promise.resolve();
  await second.sandbox.renderRolePermsInto("box");
  failedOldGet.reject(new Error("old network error"));
  await failedRender;
  assert.match(visibleText(second.elements.box.innerHTML), /最新医生/);
  assert.doesNotMatch(visibleText(second.elements.box.innerHTML), /加载失败/);
});

test("目标容器被替换后迟到 GET 完全 no-op", async () => {
  const get = deferredResponse();
  const {sandbox, elements, replaceBox} = makeSandbox({responses: [get.promise]});
  const oldBox = elements.box;
  const rendering = sandbox.renderRolePermsInto("box");
  await Promise.resolve();
  const replacement = replaceBox("<div>当前页面</div>");
  get.resolve({ok: true, status: 200, body: permissionPayload()});
  await rendering;

  assert.strictEqual(replacement.innerHTML, "<div>当前页面</div>");
  assert.strictEqual(oldBox.innerHTML, '<div class="module-loading">载入中...</div>');
});

test("损坏 V3 响应统一 fail-closed，不渲染半套权限树", async () => {
  const corruptions = [
    ["缺顶层字段", data => { delete data.system_admin_capabilities; }],
    ["重复权限 key", data => { data.perms[1].key = data.perms[0].key; }],
    ["错误 roles", data => { data.roles[0].assigned_staff_count = -1; }],
    ["错误 matrix", data => { delete data.matrix.doctor["patient.profile.view"]; }],
    ["matrix 违反依赖", data => {
      data.matrix.doctor["patient.profile.edit"] = true;
      data.matrix.doctor["patient.profile.view"] = false;
    }],
    ["错误 capabilities", data => { data.system_admin_capabilities[0].assignable = true; }],
    ["错误 dependencies", data => { data.perms[1].dependencies = ["missing.permission"]; }],
    ["错误 risk", data => { data.perms[1].risk = "unknown"; }],
  ];

  for (const [label, mutate] of corruptions) {
    const payload = clone(permissionPayload());
    mutate(payload);
    const {sandbox, elements} = makeSandbox({payload});
    await sandbox.renderRolePermsInto("box");
    assert.strictEqual(
      visibleText(elements.box.innerHTML),
      "角色权限加载失败，请刷新后重试",
      label,
    );
    assert.doesNotMatch(elements.box.innerHTML, /data-rp-permission-key|data-rp-select-role|rp-table/, label);
  }
});

test("保存确认中的密码节点在勾选、搜索、切视图和折叠后保持连接", async () => {
  const {sandbox, elements} = makeSandbox();
  await sandbox.renderRolePermsInto("box");
  sandbox.togglePermission("appointment.view", true);
  sandbox.beginRoleSavePreview("doctor");

  const password = elements.rpCurrentPassword;
  password.value = "fixture-password";
  const actions = [
    ["勾选", () => sandbox.togglePermission("patient.profile.edit", true)],
    ["搜索", () => sandbox.setRolePermissionSearch("患者")],
    ["切视图", () => sandbox.setRolePermissionView("sensitive")],
    ["折叠", () => sandbox.toggleRoleTreeSection("module", "患者档案与回收")],
  ];
  for (const [label, action] of actions) {
    action();
    assert.ok(elements.rpCurrentPassword === password, label);
    assert.strictEqual(password.isConnected, true, label);
    assert.ok(password.value.length > 0, label);
    assert.strictEqual(elements.rpV3Confirm.hidden, false, label);
    assert.strictEqual(elements.rpPasswordStep.hidden, false, label);
  }
});

test("密码阶段切到数据范围隐藏确认区，切回后保留节点、阶段和内容", async () => {
  const {sandbox, elements} = makeSandbox();
  await sandbox.renderRolePermsInto("box");
  sandbox.togglePermission("appointment.view", true);
  sandbox.beginRoleSavePreview("doctor");
  const password = elements.rpCurrentPassword;
  password.value = "fixture-password";
  const flowMessage = elements.rpMsg.textContent;
  assert.match(flowMessage, /请先确认当前登录密码/);

  sandbox.setRolePermissionView("scope");
  assert.strictEqual(elements.rpV3Confirm.hidden, true);
  assert.strictEqual(elements.rpMsg.hidden, true);
  assert.strictEqual(elements.rpMsg.textContent, flowMessage);
  assert.strictEqual(elements.box.querySelector("[data-rp-save-selected]"), null);
  assert.strictEqual(elements.rpCurrentPassword, password);
  assert.ok(password.value.length > 0);

  sandbox.setRolePermissionView("all");
  assert.strictEqual(elements.rpV3Confirm.hidden, false);
  assert.strictEqual(elements.rpMsg.hidden, false);
  assert.strictEqual(elements.rpMsg.textContent, flowMessage);
  assert.strictEqual(elements.rpPasswordStep.hidden, false);
  assert.strictEqual(elements.rpReasonStep.hidden, true);
  assert.strictEqual(elements.rpCurrentPassword, password);
  assert.ok(password.value.length > 0);
});

test("稳定确认控件跨局部重绘只绑定一次事件", async () => {
  const {sandbox, elements} = makeSandbox();
  await sandbox.renderRolePermsInto("box");
  const controls = [
    elements.box.querySelector("[data-rp-confirm-password]"),
    elements.box.querySelector("[data-rp-confirm-save]"),
    elements.box.querySelector("[data-rp-cancel-save]"),
  ];

  sandbox.togglePermission("appointment.view", true);
  sandbox.setRolePermissionSearch("患者");
  sandbox.setRolePermissionView("sensitive");
  sandbox.toggleRoleTreeSection("module", "患者档案与回收");

  for (const control of controls) {
    assert.strictEqual(control.isConnected, true);
    assert.strictEqual(control.listenerCount("click"), 1);
    assert.strictEqual(control.listenerAddCount("click"), 1);
  }
});

test("验密后的原因节点在勾选、搜索、切视图和折叠后保留阶段与内容", async () => {
  const {sandbox, elements} = makeSandbox({
    responses: [
      {ok: true, status: 200, body: permissionPayload()},
      {ok: true, status: 200, body: {ok: true}},
    ],
  });
  await sandbox.renderRolePermsInto("box");
  sandbox.togglePermission("appointment.view", true);
  sandbox.beginRoleSavePreview("doctor");
  elements.rpCurrentPassword.value = "fixture-password";
  await sandbox.confirmRolePermPassword();
  const reason = elements.rpReason;
  reason.value = "保留这次修改原因";

  const actions = [
    ["勾选", () => sandbox.togglePermission("patient.profile.edit", true)],
    ["搜索", () => sandbox.setRolePermissionSearch("患者")],
    ["切视图", () => sandbox.setRolePermissionView("sensitive")],
    ["折叠", () => sandbox.toggleRoleTreeSection("module", "患者档案与回收")],
  ];
  for (const [label, action] of actions) {
    action();
    assert.ok(elements.rpReason === reason, label);
    assert.strictEqual(reason.isConnected, true, label);
    assert.strictEqual(reason.value, "保留这次修改原因", label);
    assert.strictEqual(elements.rpReasonStep.hidden, false, label);
    assert.strictEqual(elements.rpPasswordStep.hidden, true, label);
  }
});

test("原因阶段切到数据范围隐藏确认区，切回后保留节点、阶段和内容", async () => {
  const {sandbox, elements} = makeSandbox({
    responses: [
      {ok: true, status: 200, body: permissionPayload()},
      {ok: true, status: 200, body: {ok: true}},
    ],
  });
  await sandbox.renderRolePermsInto("box");
  sandbox.togglePermission("appointment.view", true);
  sandbox.beginRoleSavePreview("doctor");
  elements.rpCurrentPassword.value = "fixture-password";
  await sandbox.confirmRolePermPassword();
  const reason = elements.rpReason;
  reason.value = "保留 scope 前的修改原因";
  const flowMessage = elements.rpMsg.textContent;
  assert.match(flowMessage, /密码已确认/);

  sandbox.setRolePermissionView("scope");
  assert.strictEqual(elements.rpV3Confirm.hidden, true);
  assert.strictEqual(elements.rpMsg.hidden, true);
  assert.strictEqual(elements.rpMsg.textContent, flowMessage);
  assert.strictEqual(elements.box.querySelector("[data-rp-save-selected]"), null);
  assert.strictEqual(elements.rpReason, reason);
  assert.strictEqual(reason.value, "保留 scope 前的修改原因");

  sandbox.setRolePermissionView("sensitive");
  assert.strictEqual(elements.rpV3Confirm.hidden, false);
  assert.strictEqual(elements.rpMsg.hidden, false);
  assert.strictEqual(elements.rpMsg.textContent, flowMessage);
  assert.strictEqual(elements.rpPasswordStep.hidden, true);
  assert.strictEqual(elements.rpReasonStep.hidden, false);
  assert.strictEqual(elements.rpReason, reason);
  assert.strictEqual(reason.value, "保留 scope 前的修改原因");
});

test("数据范围期间迟到验密只推进内部阶段，切回前不得显示确认区", async () => {
  const reauth = deferredResponse();
  const {sandbox, elements} = makeSandbox({
    responses: [
      {ok: true, status: 200, body: permissionPayload()},
      reauth.promise,
    ],
  });
  await sandbox.renderRolePermsInto("box");
  sandbox.togglePermission("appointment.view", true);
  sandbox.beginRoleSavePreview("doctor");
  elements.rpCurrentPassword.value = "fixture-password";
  const confirming = sandbox.confirmRolePermPassword();
  await Promise.resolve();
  sandbox.setRolePermissionView("scope");
  reauth.resolve({ok: true, status: 200, body: {ok: true}});
  await confirming;

  assert.strictEqual(elements.rpV3Confirm.hidden, true);
  assert.strictEqual(elements.rpMsg.hidden, true);
  assert.match(elements.rpMsg.textContent, /密码已确认/);
  assert.strictEqual(elements.rpPasswordStep.hidden, true);
  assert.strictEqual(elements.rpReasonStep.hidden, false);
  sandbox.setRolePermissionView("all");
  assert.strictEqual(elements.rpV3Confirm.hidden, false);
  assert.strictEqual(elements.rpMsg.hidden, false);
  assert.match(elements.rpMsg.textContent, /密码已确认/);
  assert.strictEqual(elements.rpPasswordStep.hidden, true);
  assert.strictEqual(elements.rpReasonStep.hidden, false);
});

test("确认流程消息与权限依赖提示独立，重绘不互相覆盖", async () => {
  const {sandbox, elements} = makeSandbox();
  await sandbox.renderRolePermsInto("box");
  sandbox.togglePermission("appointment.view", true);
  sandbox.beginRoleSavePreview("doctor");
  assert.match(elements.rpMsg.textContent, /请先确认当前登录密码/);

  sandbox.togglePermission("patient.profile.edit", true);

  assert.match(elements.rpMsg.textContent, /请先确认当前登录密码/);
  assert.match(
    visibleText(elements.box.innerHTML),
    /已自动补齐：患者档案与回收 \/ 患者资料 \/ 查看/,
  );
});

test("验密 4xx 在等待中重绘后保留当前错误且不进入原因阶段", async () => {
  const reauth = deferredResponse();
  const {sandbox, elements, calls} = makeSandbox({
    responses: [
      {ok: true, status: 200, body: permissionPayload()},
      reauth.promise,
    ],
  });
  await sandbox.renderRolePermsInto("box");
  sandbox.togglePermission("appointment.view", true);
  sandbox.beginRoleSavePreview("doctor");
  elements.rpCurrentPassword.value = "fixture-password";
  const confirming = sandbox.confirmRolePermPassword();
  await Promise.resolve();
  sandbox.togglePermission("patient.profile.edit", true);
  reauth.resolve({ok: false, status: 401, body: {}});
  await confirming;

  assert.match(elements.rpMsg.textContent, /密码确认失败：401/);
  assert.strictEqual(elements.rpPasswordStep.hidden, false);
  assert.strictEqual(elements.rpReasonStep.hidden, true);
  assert.strictEqual(calls.filter(call => call.options.method === "PUT").length, 0);
  sandbox.setRolePermissionSearch("编辑");
  assert.match(elements.rpMsg.textContent, /密码确认失败：401/);
});

test("验密网络失败在等待中重绘后保留当前错误且不假成功", async () => {
  const reauth = deferredResponse();
  const {sandbox, elements, calls} = makeSandbox({
    responses: [
      {ok: true, status: 200, body: permissionPayload()},
      reauth.promise,
    ],
  });
  await sandbox.renderRolePermsInto("box");
  sandbox.togglePermission("appointment.view", true);
  sandbox.beginRoleSavePreview("doctor");
  elements.rpCurrentPassword.value = "fixture-password";
  const confirming = sandbox.confirmRolePermPassword();
  await Promise.resolve();
  sandbox.setRolePermissionSearch("患者");
  reauth.reject(new Error("synthetic network failure"));
  await confirming;

  assert.match(elements.rpMsg.textContent, /密码确认失败（网络错误）/);
  assert.doesNotMatch(elements.rpMsg.textContent, /密码已确认/);
  assert.strictEqual(elements.rpReasonStep.hidden, true);
  assert.strictEqual(calls.filter(call => call.options.method === "PUT").length, 0);
  sandbox.setRolePermissionView("sensitive");
  assert.match(elements.rpMsg.textContent, /密码确认失败（网络错误）/);
});

test("验密响应到达前目标容器被替换时不得改写新容器", async () => {
  const reauth = deferredResponse();
  const {sandbox, elements, replaceBox} = makeSandbox({
    responses: [
      {ok: true, status: 200, body: permissionPayload()},
      reauth.promise,
    ],
  });
  await sandbox.renderRolePermsInto("box");
  sandbox.togglePermission("appointment.view", true);
  sandbox.beginRoleSavePreview("doctor");
  elements.rpCurrentPassword.value = "fixture-password";
  const confirming = sandbox.confirmRolePermPassword();
  await Promise.resolve();

  const replacement = replaceBox(`
    <span id="rpMsg">新容器原始消息</span>
    <div id="rpPasswordStep"></div>
    <input id="rpCurrentPassword" value="replacement-value">
    <div id="rpReasonStep" hidden></div>
    <textarea id="rpReason"></textarea>
  `);
  reauth.resolve({ok: true, status: 200, body: {ok: true}});
  await confirming;

  assert.strictEqual(replacement.idNodes.get("rpMsg").textContent, "新容器原始消息");
  assert.strictEqual(replacement.idNodes.get("rpCurrentPassword").value, "replacement-value");
  assert.strictEqual(replacement.idNodes.get("rpPasswordStep").hidden, false);
  assert.strictEqual(replacement.idNodes.get("rpReasonStep").hidden, true);
});

test("确认离开后迟到验密成功不得恢复确认流程", async () => {
  const reauth = deferredResponse();
  const {sandbox, elements, calls, overlays} = makeSandbox({
    responses: [
      {ok: true, status: 200, body: permissionPayload()},
      reauth.promise,
    ],
  });
  await sandbox.renderRolePermsInto("box");
  sandbox.togglePermission("appointment.view", true);
  sandbox.beginRoleSavePreview("doctor");
  elements.rpCurrentPassword.value = "fixture-password";
  const confirming = sandbox.confirmRolePermPassword();
  await Promise.resolve();

  const leaving = sandbox.requestLeaveRolePermissions();
  overlays[0].querySelector('[data-rp-leave="confirm"]').click();
  assert.strictEqual(await leaving, true);
  reauth.resolve({ok: true, status: 200, body: {ok: true}});
  await confirming;

  assert.strictEqual(elements.rpV3Confirm.hidden, true);
  assert.strictEqual(elements.rpPasswordStep.hidden, false);
  assert.strictEqual(elements.rpReasonStep.hidden, true);
  await sandbox.saveRolePerms();
  assert.strictEqual(calls.filter(call => call.options.method === "PUT").length, 0);
});

async function enterRolePermissionReasonStep(sandbox, elements) {
  sandbox.togglePermission("appointment.view", true);
  sandbox.beginRoleSavePreview("doctor");
  elements.rpCurrentPassword.value = "fixture-password";
  await sandbox.confirmRolePermPassword();
  elements.rpReason.value = "保留失败后的修改原因";
}

test("业务错误稳定翻译，失败始终保留 draft、原因和预览 modal", async () => {
  const cases = [
    [400, "permission_dependency_missing", "缺少前置查看权限，请检查依赖项。"],
    [400, "permission_unknown", "权限目录已变化，请刷新后重试。"],
    [400, "permission_not_assignable", "此能力只属于系统管理员，不能分配给岗位。"],
    [400, "role_not_configurable", "该岗位不能在这里配置。"],
    [403, "recent_reauthentication_required", "当前密码确认已过期，请重新确认。"],
    [403, "access_denied", "你没有修改角色权限的权限。"],
    [
      409,
      "role_permissions_stale",
      "权限已被其他管理员修改，请刷新后重试，当前草稿已保留",
    ],
  ];

  for (const [status, detail, translated] of cases) {
    const {sandbox, elements, calls} = makeSandbox({
      responses: [
        {ok: true, status: 200, body: permissionPayload()},
        {ok: true, status: 200, body: {ok: true}},
        {ok: false, status, body: {detail}},
      ],
    });
    await sandbox.renderRolePermsInto("box");
    sandbox.togglePermission("patient.profile.edit", true);
    sandbox.beginRoleSavePreview("doctor");
    const previewBefore = visibleText(elements.rpSavePreview.innerHTML);
    elements.rpCurrentPassword.value = "fixture-password";
    await sandbox.confirmRolePermPassword();
    elements.rpReason.value = "调整医生岗位权限";
    await sandbox.saveRolePerms();

    assert.strictEqual(elements.rpV3Confirm.hidden, false, detail);
    assert.strictEqual(elements.rpReason.value, "调整医生岗位权限", detail);
    assert.strictEqual(visibleText(elements.rpSavePreview.innerHTML), previewBefore, detail);
    assert.strictEqual(elements.rpMsg.textContent, translated, detail);
    assert.doesNotMatch(elements.rpMsg.textContent, new RegExp(detail), detail);
    assert.deepStrictEqual(plain(sandbox.rolePermissionDiff()), {
      added: ["patient.profile.view", "patient.profile.edit"],
      removed: [],
      autoAddedDependencies: ["patient.profile.view"],
    }, detail);
    assert.strictEqual(calls.filter(call => call.options.method === "PUT").length, 1, detail);
    if (detail === "recent_reauthentication_required") {
      assert.strictEqual(elements.rpPasswordStep.hidden, false, detail);
      assert.strictEqual(elements.rpReasonStep.hidden, true, detail);
    } else {
      assert.strictEqual(elements.rpPasswordStep.hidden, true, detail);
      assert.strictEqual(elements.rpReasonStep.hidden, false, detail);
    }
  }
});

test("PUT 4xx 在等待中继续编辑后保留草稿、原因和当前错误", async () => {
  const put = deferredResponse();
  const {sandbox, elements} = makeSandbox({
    responses: [
      {ok: true, status: 200, body: permissionPayload()},
      {ok: true, status: 200, body: {ok: true}},
      put.promise,
    ],
  });
  await sandbox.renderRolePermsInto("box");
  await enterRolePermissionReasonStep(sandbox, elements);
  const saving = sandbox.saveRolePerms();
  await Promise.resolve();
  sandbox.togglePermission("patient.profile.edit", true);
  put.resolve({ok: false, status: 409, body: {}});
  await saving;

  assert.deepStrictEqual(plain(sandbox.rolePermissionDiff()).added, [
    "patient.profile.view",
    "patient.profile.edit",
    "appointment.view",
  ]);
  assert.strictEqual(elements.rpReason.value, "保留失败后的修改原因");
  assert.strictEqual(elements.rpReasonStep.hidden, false);
  assert.match(elements.rpMsg.textContent, /保存失败：409/);
  sandbox.setRolePermissionSearch("患者");
  assert.match(elements.rpMsg.textContent, /保存失败：409/);
});

test("PUT 网络失败在等待中继续编辑后保留草稿、原因和当前错误", async () => {
  const put = deferredResponse();
  const {sandbox, elements} = makeSandbox({
    responses: [
      {ok: true, status: 200, body: permissionPayload()},
      {ok: true, status: 200, body: {ok: true}},
      put.promise,
    ],
  });
  await sandbox.renderRolePermsInto("box");
  await enterRolePermissionReasonStep(sandbox, elements);
  const saving = sandbox.saveRolePerms();
  await Promise.resolve();
  sandbox.togglePermission("patient.profile.edit", true);
  put.reject(new Error("synthetic network failure"));
  await saving;

  assert.strictEqual(elements.rpReason.value, "保留失败后的修改原因");
  assert.strictEqual(elements.rpReasonStep.hidden, false);
  assert.match(elements.rpMsg.textContent, /保存失败（网络错误）/);
  sandbox.setRolePermissionView("sensitive");
  assert.match(elements.rpMsg.textContent, /保存失败（网络错误）/);
});

test("PUT 响应到达前目标容器被替换时所有 continuation 均不得改写新容器", async () => {
  const put = deferredResponse();
  const {sandbox, elements, replaceBox} = makeSandbox({
    responses: [
      {ok: true, status: 200, body: permissionPayload()},
      {ok: true, status: 200, body: {ok: true}},
      put.promise,
    ],
  });
  await sandbox.renderRolePermsInto("box");
  await enterRolePermissionReasonStep(sandbox, elements);
  const saving = sandbox.saveRolePerms();
  await Promise.resolve();

  const replacement = replaceBox(`
    <span id="rpMsg">新容器原始消息</span>
    <div id="rpV3Confirm"></div>
    <div id="rpPasswordStep" hidden></div>
    <input id="rpCurrentPassword" value="replacement-value">
    <div id="rpReasonStep"></div>
    <textarea id="rpReason">replacement-reason</textarea>
    <button class="rp-save" data-rp-confirm-save disabled>新容器保存</button>
  `);
  put.resolve({ok: true, status: 200, body: targetSaveResult()});
  await saving;

  assert.strictEqual(replacement.idNodes.get("rpMsg").textContent, "新容器原始消息");
  assert.strictEqual(replacement.idNodes.get("rpV3Confirm").hidden, false);
  assert.strictEqual(replacement.idNodes.get("rpPasswordStep").hidden, true);
  assert.strictEqual(replacement.idNodes.get("rpReasonStep").hidden, false);
  assert.strictEqual(replacement.idNodes.get("rpCurrentPassword").value, "replacement-value");
  assert.strictEqual(replacement.querySelector("[data-rp-confirm-save]").disabled, true);
});

test("旧 PUT 成功响应不得污染重新加载后的 V3 baseline 和消息", async () => {
  const put = deferredResponse();
  const latest = permissionPayload({
    doctor: ["patient.image.view"],
    doctorName: "最新医生",
  });
  const {sandbox, elements} = makeSandbox({
    responses: [
      {ok: true, status: 200, body: permissionPayload()},
      {ok: true, status: 200, body: {ok: true}},
      put.promise,
      {ok: true, status: 200, body: latest},
    ],
  });
  await sandbox.renderRolePermsInto("box");
  await enterRolePermissionReasonStep(sandbox, elements);
  const saving = sandbox.saveRolePerms();
  await Promise.resolve();
  await sandbox.renderRolePermsInto("box");
  put.resolve({ok: true, status: 200, body: targetSaveResult()});
  await saving;

  assert.match(visibleText(elements.box.innerHTML), /最新医生/);
  assert.deepStrictEqual(plain(sandbox.rolePermissionDiff()), {
    added: [],
    removed: [],
    autoAddedDependencies: [],
  });
  assert.doesNotMatch(elements.rpMsg.textContent, /已保存/);
});

test("重新加载后新 PUT 不得被旧 PUT 的 finally 误解锁", async () => {
  const oldPut = deferredResponse();
  const currentPut = deferredResponse();
  const latest = permissionPayload({
    doctor: ["patient.image.view"],
    doctorName: "最新医生",
  });
  const {sandbox, elements, calls} = makeSandbox({
    responses: [
      {ok: true, status: 200, body: permissionPayload()},
      {ok: true, status: 200, body: {ok: true}},
      oldPut.promise,
      {ok: true, status: 200, body: latest},
      {ok: true, status: 200, body: {ok: true}},
      currentPut.promise,
    ],
  });
  await sandbox.renderRolePermsInto("box");
  await enterRolePermissionReasonStep(sandbox, elements);
  const oldSaving = sandbox.saveRolePerms();
  await Promise.resolve();

  await sandbox.renderRolePermsInto("box");
  sandbox.togglePermission("patient.profile.edit", true);
  sandbox.beginRoleSavePreview("doctor");
  assert.strictEqual(elements.rpV3Confirm.hidden, false);
  elements.rpCurrentPassword.value = "fixture-password";
  await sandbox.confirmRolePermPassword();
  elements.rpReason.value = "新流程仍在保存";
  const currentSaving = sandbox.saveRolePerms();
  await Promise.resolve();
  assert.strictEqual(elements.box.querySelector("[data-rp-confirm-save]").disabled, true);

  oldPut.resolve({ok: true, status: 200, body: targetSaveResult()});
  await oldSaving;
  assert.strictEqual(elements.box.querySelector("[data-rp-confirm-save]").disabled, true);
  await sandbox.saveRolePerms();
  assert.strictEqual(
    calls.filter(call => call.options.method === "PUT").length,
    2,
  );

  currentPut.resolve({
    ok: true,
    status: 200,
    body: targetSaveResult({
      oldPermissions: ["patient.image.view"],
      newPermissions: [
        "patient.profile.view",
        "patient.profile.edit",
        "patient.image.view",
      ],
      added: ["patient.profile.view", "patient.profile.edit"],
    }),
  });
  await currentSaving;
});

test("V3 验密后 PUT 岗位快照、服务器基线和原因，成功用实际差异提示", async () => {
  const put = deferredResponse();
  const {sandbox, elements, calls, overlays} = makeSandbox({
    responses: [
      {ok: true, status: 200, body: permissionPayload()},
      {ok: true, status: 200, body: {ok: true}},
      put.promise,
    ],
  });
  await sandbox.renderRolePermsInto("box");
  sandbox.togglePermission("patient.profile.edit", true);
  sandbox.beginRoleSavePreview("doctor");
  const currentPassword = elements.rpCurrentPassword;
  const secret = "current-secret-<never-render>";
  currentPassword.value = secret;
  assert.doesNotMatch(elements.box.innerHTML, new RegExp(secret));
  await sandbox.confirmRolePermPassword();
  assert.strictEqual(currentPassword.value, "");
  elements.rpReason.value = "调整医生岗位权限";
  const saving = sandbox.saveRolePerms();
  await Promise.resolve();

  assert.strictEqual(await sandbox.requestLeaveRolePermissions(), false);
  assert.strictEqual(overlays.length, 0);
  await sandbox.saveRolePerms();
  assert.strictEqual(calls.length, 3);
  assert.strictEqual(calls[1].url, "/api/auth/reauth");
  assert.strictEqual(calls[1].transport, "raw");
  const reauthBody = JSON.parse(calls[1].options.body);
  assert.deepStrictEqual(Object.keys(reauthBody), ["password"]);
  assert.strictEqual(reauthBody.password, secret);
  const activeSaveMessage = elements.rpMsg.textContent;
  assert.match(activeSaveMessage, /正在保存|保存中/);
  assert.doesNotMatch(activeSaveMessage, new RegExp(secret));
  sandbox.togglePermission("appointment.view", true);
  assert.strictEqual(elements.rpMsg.textContent, activeSaveMessage);
  put.resolve({
    ok: true,
    status: 200,
    body: targetSaveResult({
      oldPermissions: ["appointment.view"],
      newPermissions: ["patient.profile.view", "patient.profile.edit"],
      added: ["patient.profile.view", "patient.profile.edit"],
      removed: ["appointment.view"],
      affectedStaffCount: 4,
    }),
  });
  await saving;

  const putBody = JSON.parse(calls[2].options.body);
  assert.strictEqual(calls[2].transport, "raw");
  assert.deepStrictEqual(putBody, {
    role_key: "doctor",
    permissions: ["patient.profile.view", "patient.profile.edit"],
    expected_permissions: [],
    reason: "调整医生岗位权限",
  });
  assert.deepStrictEqual(
    Object.keys(putBody),
    ["role_key", "permissions", "expected_permissions", "reason"],
  );
  assert.doesNotMatch(calls[2].options.body, new RegExp(secret));
  assert.doesNotMatch(elements.box.innerHTML, new RegExp(secret));
  assert.deepStrictEqual(plain(sandbox.rolePermissionDiff()), {
    added: ["appointment.view"],
    removed: [],
    autoAddedDependencies: [],
  });
  assert.match(elements.rpMsg.textContent, /本次提交已保存，另有未保存修改/);
  assert.match(elements.rpMsg.textContent, /影响人数：4 人/);
  assert.match(elements.rpMsg.textContent, /实际新增[^]*患者档案与回收 \/ 患者资料 \/ 查看/);
  assert.match(elements.rpMsg.textContent, /实际新增[^]*患者档案与回收 \/ 患者资料 \/ 编辑/);
  assert.match(elements.rpMsg.textContent, /实际移除[^]*预约管理 \/ 预约 \/ 查看/);
  assert.doesNotMatch(elements.rpMsg.textContent, /patient\.|appointment\.|current-secret/);

  sandbox.togglePermission("patient.image.view", true);
  assert.strictEqual(elements.rpMsg.textContent, "");
  assert.match(visibleText(elements.box.innerHTML), /未保存/);
});

test("V3 PUT 稳定排序服务器基线，409 保留草稿、原因和 modal", async () => {
  const payload = permissionPayload({
    doctor: ["patient.image.view", "appointment.view"],
  });
  const {sandbox, elements, calls} = makeSandbox({
    payload,
    responses: [
      {ok: true, status: 200, body: payload},
      {ok: true, status: 200, body: {ok: true}},
      {ok: false, status: 409, body: {detail: "role_permissions_stale"}},
    ],
  });
  await sandbox.renderRolePermsInto("box");
  sandbox.togglePermission("patient.profile.edit", true);
  sandbox.beginRoleSavePreview("doctor");
  elements.rpCurrentPassword.value = "fixture-password";
  await sandbox.confirmRolePermPassword();
  elements.rpReason.value = "保留这份修改原因";
  const previewBefore = visibleText(elements.rpSavePreview.innerHTML);

  await sandbox.saveRolePerms();

  const putBody = JSON.parse(calls[2].options.body);
  assert.deepStrictEqual(putBody.expected_permissions, [
    "appointment.view",
    "patient.image.view",
  ]);
  assert.strictEqual(elements.rpV3Confirm.hidden, false);
  assert.strictEqual(elements.rpReasonStep.hidden, false);
  assert.strictEqual(elements.rpReason.value, "保留这份修改原因");
  assert.strictEqual(visibleText(elements.rpSavePreview.innerHTML), previewBefore);
  assert.match(
    elements.rpMsg.textContent,
    /权限已被其他管理员修改[^]*当前草稿已保留/,
  );
  assert.deepStrictEqual(plain(sandbox.rolePermissionDiff()), {
    added: ["patient.profile.view", "patient.profile.edit"],
    removed: [],
    autoAddedDependencies: ["patient.profile.view"],
  });
});

test("PUT 等待期间新增的自动依赖标记在本次提交成功后保留", async () => {
  const put = deferredResponse();
  const {sandbox, elements} = makeSandbox({
    responses: [
      {ok: true, status: 200, body: permissionPayload()},
      {ok: true, status: 200, body: {ok: true}},
      put.promise,
    ],
  });
  await sandbox.renderRolePermsInto("box");
  sandbox.togglePermission("patient.profile.edit", true);
  sandbox.beginRoleSavePreview("doctor");
  elements.rpCurrentPassword.value = "fixture-password";
  await sandbox.confirmRolePermPassword();
  elements.rpReason.value = "验证提交边界";

  const saving = sandbox.saveRolePerms();
  await Promise.resolve();
  sandbox.togglePermission("appointment.edit", true);
  put.resolve({
    ok: true,
    status: 200,
    body: targetSaveResult({
      newPermissions: ["patient.profile.view", "patient.profile.edit"],
      added: ["patient.profile.view", "patient.profile.edit"],
    }),
  });
  await saving;

  assert.deepStrictEqual(plain(sandbox.rolePermissionDiff()), {
    added: ["appointment.view", "appointment.edit"],
    removed: [],
    autoAddedDependencies: ["appointment.view"],
  });
});

test("PUT 等待期间同 key 自动依赖删除后重建不会被旧提交清除", async () => {
  const put = deferredResponse();
  const {sandbox, elements} = makeSandbox({
    responses: [
      {ok: true, status: 200, body: permissionPayload()},
      {ok: true, status: 200, body: {ok: true}},
      put.promise,
    ],
  });
  const markerKeys = () => plain(vm.runInContext(
    "Array.from(_rpV3State.autoAddedDependencies.keys())",
    sandbox,
  ));
  await sandbox.renderRolePermsInto("box");
  sandbox.togglePermission("patient.profile.edit", true);
  assert.deepStrictEqual(markerKeys(), ["patient.profile.view"]);
  sandbox.beginRoleSavePreview("doctor");
  elements.rpCurrentPassword.value = "fixture-password";
  await sandbox.confirmRolePermPassword();
  elements.rpReason.value = "验证自动依赖 ABA";

  const saving = sandbox.saveRolePerms();
  await Promise.resolve();
  sandbox.togglePermission("patient.profile.edit", false);
  sandbox.togglePermission("patient.profile.view", false);
  sandbox.togglePermission("consent.share", true);
  assert.deepStrictEqual(markerKeys(), ["patient.profile.view"]);

  put.resolve({
    ok: true,
    status: 200,
    body: targetSaveResult({
      newPermissions: ["patient.profile.view", "patient.profile.edit"],
      added: ["patient.profile.view", "patient.profile.edit"],
    }),
  });
  await saving;

  assert.deepStrictEqual(markerKeys(), ["patient.profile.view"]);
});

test("本次提交已保存后撤销另一份草稿也清除终态消息", async () => {
  const put = deferredResponse();
  const {sandbox, elements} = makeSandbox({
    responses: [
      {ok: true, status: 200, body: permissionPayload()},
      {ok: true, status: 200, body: {ok: true}},
      put.promise,
    ],
  });
  await sandbox.renderRolePermsInto("box");
  await enterRolePermissionReasonStep(sandbox, elements);
  const saving = sandbox.saveRolePerms();
  await Promise.resolve();
  sandbox.togglePermission("patient.profile.edit", true);
  put.resolve({ok: true, status: 200, body: targetSaveResult()});
  await saving;
  assert.match(elements.rpMsg.textContent, /本次提交已保存，另有未保存修改/);

  sandbox.restoreRoleBaseline();
  assert.strictEqual(elements.rpMsg.textContent, "");
  assert.deepStrictEqual(plain(sandbox.rolePermissionDiff()), {
    added: [],
    removed: [],
    autoAddedDependencies: [],
  });
});

test("V3 保存期间没有后续编辑时显示普通已保存且草稿干净", async () => {
  const {sandbox, elements} = makeSandbox({
    responses: [
      {ok: true, status: 200, body: permissionPayload()},
      {ok: true, status: 200, body: {ok: true}},
      {ok: true, status: 200, body: targetSaveResult()},
    ],
  });
  await sandbox.renderRolePermsInto("box");
  await enterRolePermissionReasonStep(sandbox, elements);
  await sandbox.saveRolePerms();

  assert.deepStrictEqual(plain(sandbox.rolePermissionDiff()), {
    added: [],
    removed: [],
    autoAddedDependencies: [],
  });
  assert.match(elements.rpMsg.textContent, /^已保存/);
  assert.doesNotMatch(elements.rpMsg.textContent, /另有未保存修改/);

  const successMessage = elements.rpMsg.textContent;
  sandbox.setRolePermissionSearch("患者");
  assert.strictEqual(elements.rpMsg.textContent, successMessage);
  sandbox.toggleRoleTreeSection("module", "患者档案与回收");
  assert.strictEqual(elements.rpMsg.textContent, successMessage);

  sandbox.togglePermission("patient.image.view", true);
  assert.strictEqual(elements.rpMsg.textContent, "");
  assert.match(visibleText(elements.box.innerHTML), /未保存/);
});

test("当前岗位保存成功后切到其他岗位不残留上一岗位终态消息", async () => {
  const {sandbox, elements} = makeSandbox({
    responses: [
      {ok: true, status: 200, body: permissionPayload()},
      {ok: true, status: 200, body: {ok: true}},
      {ok: true, status: 200, body: targetSaveResult()},
    ],
  });
  await sandbox.renderRolePermsInto("box");
  await enterRolePermissionReasonStep(sandbox, elements);
  await sandbox.saveRolePerms();
  assert.match(elements.rpMsg.textContent, /^已保存/);

  assert.strictEqual(await sandbox.selectRole("nurse"), true);
  assert.strictEqual(elements.rpMsg.textContent, "");
  assert.match(elements.box.innerHTML, /data-rp-select-role="nurse"[^>]*aria-pressed="true"/);
});

test("requestLeaveRolePermissions 始终返回 Promise，应用内确认清理 modal", async () => {
  const {sandbox, overlays, keydownListeners} = makeSandbox();
  await sandbox.renderRolePermsInto("box");
  assert.strictEqual(await sandbox.requestLeaveRolePermissions(), true);
  sandbox.togglePermission("appointment.view", true);

  const first = sandbox.requestLeaveRolePermissions();
  const second = sandbox.requestLeaveRolePermissions();
  assert.strictEqual(typeof first?.then, "function");
  assert.strictEqual(first, second);
  assert.strictEqual(overlays.length, 1);
  assert.strictEqual(keydownListeners.size, 1);
  overlays[0].querySelector('[data-rp-leave="confirm"]').click();
  assert.strictEqual(await first, true);
  assert.strictEqual(overlays.length, 0);
  assert.strictEqual(keydownListeners.size, 0);
  assert.deepStrictEqual(plain(sandbox.rolePermissionDiff()).added, []);
});

test("旧模式继续渲染横向矩阵、整表 PUT 并保留 baseline 离开守卫", async () => {
  const {sandbox, elements, legacyCheckboxes, calls, overlays} = makeSandbox({
    accessV3: false,
    responses: [
      {ok: true, status: 200, body: legacyPayload},
      {ok: true, status: 200, body: {ok: true}},
    ],
  });
  await sandbox.renderRolePermsInto("box");
  assert.match(elements.box.innerHTML, /class="rp-table"/);
  assert.match(elements.box.innerHTML, />医生<|>护士<|>管理员</);

  legacyCheckboxes[1].checked = true;
  const cancelled = sandbox.requestLeaveRolePermissions();
  assert.strictEqual(overlays.length, 1);
  overlays[0].querySelector('[data-rp-leave="cancel"]').click();
  assert.strictEqual(await cancelled, false);
  assert.strictEqual(legacyCheckboxes[1].checked, true);
  await sandbox.saveRolePerms();

  assert.strictEqual(calls[1].url, "/api/role-permissions");
  assert.deepStrictEqual(JSON.parse(calls[1].options.body), {
    matrix: {
      doctor: {
        "patient.profile.view": true,
        "patient.profile.edit": true,
      },
      nurse: {"appointment.view": true},
    },
  });
  assert.strictEqual(await sandbox.requestLeaveRolePermissions(), true);
});

test("旧模式 PUT 挂起期间继续编辑，成功后仍脏且显示双状态", async () => {
  const put = deferredResponse();
  const {sandbox, elements, legacyCheckboxes, overlays} = makeSandbox({
    accessV3: false,
    responses: [
      {ok: true, status: 200, body: legacyPayload},
      put.promise,
    ],
  });
  await sandbox.renderRolePermsInto("box");
  legacyCheckboxes[1].checked = true;
  const saving = sandbox.saveRolePerms();
  await Promise.resolve();
  legacyCheckboxes[2].checked = false;
  put.resolve({ok: true, status: 200, body: {ok: true}});
  await saving;

  assert.match(elements.rpMsg.textContent, /本次提交已保存，另有未保存修改/);
  const leaving = sandbox.requestLeaveRolePermissions();
  assert.strictEqual(overlays.length, 1);
  overlays[0].querySelector('[data-rp-leave="cancel"]').click();
  assert.strictEqual(await leaving, false);
});

test("旧模式重新加载后的新 PUT 不得被旧 PUT continuation 污染或误解锁", async () => {
  const oldPut = deferredResponse();
  const currentPut = deferredResponse();
  const {sandbox, elements, legacyCheckboxes, calls, overlays} = makeSandbox({
    accessV3: false,
    responses: [
      {ok: true, status: 200, body: legacyPayload},
      oldPut.promise,
      {ok: true, status: 200, body: legacyPayload},
      currentPut.promise,
    ],
  });
  await sandbox.renderRolePermsInto("box");
  legacyCheckboxes[1].checked = true;
  const oldSaving = sandbox.saveRolePerms();
  await Promise.resolve();

  legacyCheckboxes[1].checked = false;
  await sandbox.renderRolePermsInto("box");
  legacyCheckboxes[2].checked = false;
  const currentSaving = sandbox.saveRolePerms();
  await Promise.resolve();
  const currentSaveButton = elements.box.querySelectorAll(
    ".rp-save, [data-rp-save-selected], [data-rp-cancel-save]",
  )[0];
  assert.strictEqual(currentSaveButton.disabled, true);

  oldPut.resolve({ok: true, status: 200, body: {ok: true}});
  await oldSaving;
  assert.strictEqual(currentSaveButton.disabled, true);
  assert.match(elements.rpMsg.textContent, /保存中/);
  await sandbox.saveRolePerms();
  assert.strictEqual(calls.filter(call => call.options.method === "PUT").length, 2);

  currentPut.reject(new Error("synthetic current PUT failure"));
  await currentSaving;
  legacyCheckboxes[2].checked = true;
  const leaving = sandbox.requestLeaveRolePermissions();
  if (overlays.length) overlays[0].querySelector('[data-rp-leave="cancel"]').click();
  assert.strictEqual(await leaving, true);
});
