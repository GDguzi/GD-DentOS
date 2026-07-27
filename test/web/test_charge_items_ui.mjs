import { test } from "node:test";
import assert from "node:assert";
import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import { dirname, join } from "node:path";
import vm from "node:vm";

const here = dirname(fileURLToPath(import.meta.url));
const staticDir = join(here, "..", "..", "local_app", "static");
const read = name => readFileSync(join(staticDir, name), "utf8");
const charge = read("charge_items.js");
const plan = read("treatment_plan_editor.js");
const index = read("index.html");
const styles = read("styles.css");
const audit = read("audit_log.js");

function functionSource(source, name) {
  const start = source.indexOf(`function ${name}`);
  assert.notStrictEqual(start, -1, `缺少函数：${name}`);
  const open = source.indexOf("{", start);
  let depth = 0;
  for (let i = open; i < source.length; i += 1) {
    if (source[i] === "{") depth += 1;
    if (source[i] === "}") depth -= 1;
    if (depth === 0) return source.slice(start, i + 1);
  }
  assert.fail(`函数未闭合：${name}`);
}

function chargeSandbox({
  admin,
  fetchImpl = async () => ({ok: true, json: async () => ({})}),
  documentImpl = null,
}) {
  const sandbox = {
    __isSystemAdmin: admin,
    fetch: fetchImpl,
    document: documentImpl || {getElementById: () => null, querySelector: () => null},
    confirm: () => true,
    alert: () => {},
    escapeHtml(value) {
      return String(value ?? "")
        .replaceAll("&", "&amp;")
        .replaceAll("<", "&lt;")
        .replaceAll(">", "&gt;")
        .replaceAll('"', "&quot;");
    },
    escapeAttr(value) {
      return sandbox.escapeHtml(value).replaceAll("'", "&;");
    },
  };
  sandbox.window = sandbox;
  vm.createContext(sandbox);
  vm.runInContext(`${charge}
    this.__ciTest = {
      ciCanManage, ciFlattenGroups, ciFetchItems, ciFilterBar, ciRow, bindChargeItems,
      openChargeItemDialog, saveChargeItem, stopChargeItem, restoreChargeItem,
    };
    this.__setCiStatus = value => { _ciStatus = value; };
    this.__setCiCat = value => { _ciCat = value; };
    this.__setCiItems = items => {
      _ciItems = items;
      _ciById = Object.create(null);
      items.forEach(item => { _ciById[String(item.handle_id)] = item; });
    };
    this.__getCiState = () => ({status: _ciStatus, category: _ciCat, editingId: _ciEditingId, saving: Boolean(_ciSaving)});
    this.__getCiLifecycle = () => ({
      saving: Boolean(_ciSaving),
      active: Boolean(_ciActiveDialog),
      editingId: _ciEditingId,
      reloadPending: typeof _ciReloadPending === "undefined" ? undefined : _ciReloadPending,
    });
  `, sandbox);
  return sandbox;
}

function deferred() {
  let resolve;
  let reject;
  const promise = new Promise((res, rej) => { resolve = res; reject = rej; });
  return {promise, resolve, reject};
}

function fakeDialog(values) {
  const makeControl = (value = "") => ({
    value,
    disabled: false,
    listeners: {},
    addEventListener(type, listener) { this.listeners[type] = listener; },
  });
  const fields = Object.fromEntries(Object.entries(values).map(([key, value]) => [key, makeControl(value)]));
  const buttons = {
    close: makeControl(),
    cancel: makeControl(),
    save: makeControl(),
  };
  const status = {textContent: ""};
  const dialog = {
    fields,
    buttons,
    status,
    removed: false,
    owner: null,
    querySelector(selector) {
      const field = /^\[data-ci-f="([^"]+)"\]$/.exec(selector)?.[1];
      if (field) return fields[field] || null;
      if (selector === "[data-ci-close]") return buttons.close;
      if (selector === "[data-ci-cancel]") return buttons.cancel;
      if (selector === "[data-ci-save]") return buttons.save;
      if (selector === "[data-ci-dialog-status]") return status;
      return null;
    },
    querySelectorAll(selector) {
      return selector === "button, input" ? [...Object.values(buttons), ...Object.values(fields)] : [];
    },
    remove() {
      this.removed = true;
      if (this.owner?.current === this) this.owner.current = null;
    },
  };
  return dialog;
}

function fakeDialogDocument() {
  const owner = {current: null, queued: []};
  const container = {
    _innerHTML: "",
    get innerHTML() { return this._innerHTML; },
    set innerHTML(value) {
      this._innerHTML = value;
      owner.current?.remove();
    },
    insertAdjacentHTML() {
      const dialog = owner.queued.shift();
      assert.ok(dialog, "测试必须先排队一个 fake dialog");
      dialog.owner = owner;
      dialog.removed = false;
      owner.current = dialog;
    },
    querySelector(selector) {
      if (selector === "[data-ci-dialog]") return owner.current;
      return null;
    },
  };
  const documentImpl = {
    getElementById: id => id === "cfgBody" ? container : null,
    querySelector(selector) {
      if (selector === "[data-ci-dialog]") return owner.current;
      if (selector === "[data-ci-dialog-status]") return owner.current?.status || null;
      return null;
    },
  };
  return {
    container,
    documentImpl,
    enqueue(dialog) { owner.queued.push(dialog); },
    currentDialog() { return owner.current; },
  };
}

test("管理员和非管理员使用不同数据源与写操作语义", async () => {
  const adminUrls = [];
  const admin = chargeSandbox({
    admin: true,
    fetchImpl: async url => {
      adminUrls.push(url);
      return {
        ok: true,
        json: async () => ({
          items: [
            {handle_id: "imported-1", name: "历史项目", fee_type: "检查费", status: "active", origin: "imported"},
          ],
          counts: {active: 4, stopped: 2},
        }),
      };
    },
  });
  assert.strictEqual(admin.__ciTest.ciCanManage(), true);
  admin.__setCiStatus("stopped");
  assert.deepStrictEqual(
    JSON.parse(JSON.stringify(await admin.__ciTest.ciFetchItems())),
    {
      items: [{handle_id: "imported-1", name: "历史项目", fee_type: "检查费", status: "active", origin: "imported"}],
      counts: {active: 4, stopped: 2},
    },
  );
  assert.deepStrictEqual(adminUrls, ["/api/handle-items/manage?status=stopped"]);

  const importedActive = admin.__ciTest.ciRow({handle_id: "imported-1", name: "历史项目", status: "active", origin: "imported"});
  assert.match(importedActive, /data-ci-edit="imported-1"/);
  assert.match(importedActive, /data-ci-stop="imported-1"/);
  assert.doesNotMatch(importedActive, /data-ci-restore/);
  const stopped = admin.__ciTest.ciRow({handle_id: "stopped-1", name: "停用项目", status: "stopped", origin: "local"});
  assert.match(stopped, /data-ci-restore="stopped-1"/);
  assert.doesNotMatch(stopped, /data-ci-(?:edit|stop)=/);
  const dirtyStopped = admin.__ciTest.ciRow({
    handle_id: "dirty-1", name: "资料不全项目", status: "stopped", origin: "imported",
    restore_issue: "费用分类不能为空",
  });
  assert.match(dirtyStopped, /资料不完整/);
  assert.match(dirtyStopped, /data-ci-repair-restore="dirty-1"/);
  assert.doesNotMatch(dirtyStopped, /data-ci-restore=/);

  const regularUrls = [];
  const regular = chargeSandbox({
    admin: false,
    fetchImpl: async url => {
      regularUrls.push(url);
      return {
        ok: true,
        json: async () => ({groups: [{category: "治疗费", items: [{handle_id: "local-1", name: "洁治"}]}]}),
      };
    },
  });
  assert.strictEqual(regular.__ciTest.ciCanManage(), false);
  regular.__isSystemAdmin = 1;
  assert.strictEqual(regular.__ciTest.ciCanManage(), false, "truthy 值不得获得管理权限");
  regular.__isSystemAdmin = "true";
  assert.strictEqual(regular.__ciTest.ciCanManage(), false, "字符串 true 不得获得管理权限");
  const readonly = JSON.parse(JSON.stringify(await regular.__ciTest.ciFetchItems()));
  assert.deepStrictEqual(regularUrls, ["/api/handle-items"]);
  assert.deepStrictEqual(readonly, {
    items: [{handle_id: "local-1", name: "洁治", fee_type: "治疗费", status: "active", origin: "unknown"}],
    counts: {active: 1, stopped: 0},
  });
  const readonlyRow = regular.__ciTest.ciRow(readonly.items[0]);
  assert.match(readonlyRow, /ci-origin[^>]*>只读</);
  assert.match(regular.__ciTest.ciRow({...readonly.items[0], origin: "local"}), /ci-origin[^>]*>本地</);
  assert.match(regular.__ciTest.ciRow({...readonly.items[0], origin: "imported"}), /ci-origin[^>]*>历史导入</);
  assert.deepStrictEqual(
    Array.from(regular.__ciTest.ciFlattenGroups([{
      category: "治疗费",
      items: [{handle_id: "unknown"}, {handle_id: "local", origin: "local"}, {handle_id: "imported", origin: "imported"}],
    }]), item => item.origin),
    ["unknown", "local", "imported"],
  );
  assert.doesNotMatch(readonlyRow, /data-ci-(?:add|edit|stop|restore)/);
});

test("切换状态时同步清空旧费用分类过滤", async () => {
  let statusChange = null;
  const status = {
    value: "active",
    addEventListener(type, listener) { if (type === "change") statusChange = listener; },
  };
  const container = {
    querySelector(selector) { return selector === "[data-ci-status]" ? status : null; },
  };
  const sandbox = chargeSandbox({admin: true});
  sandbox.__setCiCat("治疗费");
  sandbox.__ciTest.bindChargeItems(container);
  assert.strictEqual(typeof statusChange, "function");

  status.value = "stopped";
  statusChange();
  await Promise.resolve();

  assert.deepStrictEqual(JSON.parse(JSON.stringify(sandbox.__getCiState())), {
    status: "stopped",
    category: "",
    editingId: null,
    saving: false,
  });
});

test("重载后当前费用分类消失时回到真实全部分类", () => {
  const sandbox = chargeSandbox({admin: true});
  sandbox.__setCiCat("旧分类");
  sandbox.__setCiItems([
    {handle_id: "new-1", name: "新项目", fee_type: "新分类", status: "active"},
  ]);

  const filterHtml = sandbox.__ciTest.ciFilterBar();

  assert.strictEqual(sandbox.__getCiState().category, "");
  assert.match(filterHtml, /<option value="">全部分类<\/option>/);
  assert.match(filterHtml, />新分类<\/option>/);
  assert.doesNotMatch(filterHtml, />旧分类<\/option>/);
});

test("页面合同包含管理员页头、状态筛选与专属六列表格", () => {
  assert.match(charge, /ciCanManage\(\)[\s\S]*data-ci-add/);
  assert.match(charge, /data-ci-status[\s\S]*value="active"[\s\S]*value="stopped"[\s\S]*value="all"/);
  assert.match(
    charge,
    /ci-table-head[\s\S]*<span>编码<\/span>[\s\S]*<span>名称<\/span>[\s\S]*<span>费用分类<\/span>[\s\S]*<span>单位<\/span>[\s\S]*<span>单价<\/span>[\s\S]*<span>状态\/操作<\/span>/,
  );
  assert.doesNotMatch(functionSource(charge, "ciRow"), /module-row/);

  const rowRule = styles.match(/\.ci-table-row\s*\{([^}]*)\}/)?.[1] || "";
  const tableRule = styles.match(/\.ci-table\s*\{([^}]*)\}/)?.[1] || "";
  assert.match(rowRule, /display:\s*grid/);
  assert.match(rowRule, /grid-template-columns:/);
  assert.strictEqual((rowRule.match(/minmax\(/g) || []).length, 6, "收费项目表格必须固定六列");
  assert.match(rowRule, /min-width:\s*820px/, "761–908px 中等宽度也必须保持完整列宽");
  assert.match(rowRule, /box-sizing:\s*border-box/);
  assert.match(tableRule, /overflow-x:\s*auto/, "横向滚动不能只在窄屏 media 内开启");
  assert.doesNotMatch(tableRule, /overflow:\s*hidden/);
  assert.doesNotMatch(styles, /\.ci-table-row\s*>\s*span\s*\{[^}]*overflow:\s*hidden/, "操作列不能被通用单元格规则裁掉");
  assert.match(styles, /\.ci-actions\s*\{[^}]*overflow:\s*visible/);
  assert.match(styles, /@media\s*\(max-width:\s*760px\)\s*\{[\s\S]*?\.ci-form-grid\s*\{[^}]*grid-template-columns:\s*1fr;[^}]*\}/);
});

test("新增编辑弹窗、重复提交保护和失败反馈保持完整", () => {
  assert.match(charge, /modal-backdrop/);
  assert.match(charge, /appt-modal/);
  assert.match(charge, /modal-head/);
  assert.match(charge, /modal-actions/);
  for (const field of ["名称", "编码", "费用分类", "单位", "单价"]) {
    assert.match(charge, new RegExp(`<label[^>]*>[\\s\\S]*?${field}[\\s\\S]*?<input`), `缺少弹窗字段标签：${field}`);
  }

  const save = functionSource(charge, "saveChargeItem");
  assert.match(save, /_ciSaving/);
  assert.match(save, /!payload\.name[\s\S]*!payload\.fee_type[\s\S]*payload\.price\s*===\s*""/);
  assert.match(save, /const editingId\s*=/);
  assert.match(save, /const saveState\s*=/);
  assert.match(save, /ciReadForm\(dialog/);
  assert.match(save, /ciSetDialogPending\(dialog[\s\S]*true\)/);
  assert.match(save, /finally[\s\S]*ciSetDialogPending\(dialog[\s\S]*false\)/);
  assert.match(save, /保存失败：\$\{error\.message\s*\|\|\s*"网络异常"\}/);
  assert.match(functionSource(charge, "stopChargeItem"), /window\.alert\(`停用失败：\$\{error\.message\s*\|\|\s*"网络异常"\}`\)/);
  assert.match(functionSource(charge, "restoreChargeItem"), /window\.alert\(`恢复失败：\$\{error\.message\s*\|\|\s*"网络异常"\}`\)/);
});

test("资料不完整的停用项目可补全并原子恢复", async () => {
  const dom = fakeDialogDocument();
  const fetchCalls = [];
  const sandbox = chargeSandbox({
    admin: true,
    documentImpl: dom.documentImpl,
    fetchImpl(url, options = {}) {
      fetchCalls.push({url, options});
      if (url.startsWith("/api/handle-items/manage")) {
        return Promise.resolve({ok: true, json: async () => ({items: [], counts: {active: 1, stopped: 0}})});
      }
      return Promise.resolve({ok: true, json: async () => ({handle_id: "dirty-1", stopped: 0})});
    },
  });
  sandbox.__setCiItems([{
    handle_id: "dirty-1", name: "旧项目", code: "OLD", unit: "", price: null,
    fee_type: "", status: "stopped", restore_issue: "费用分类不能为空",
  }]);
  const dialog = fakeDialog({name: "补全项目", code: "NEW", fee_type: "检查费", unit: "次", price: "88"});
  dom.enqueue(dialog);
  sandbox.__ciTest.openChargeItemDialog("dirty-1", "restore");

  await sandbox.__ciTest.saveChargeItem();

  assert.strictEqual(fetchCalls[0].url, "/api/handle-items/dirty-1/restore");
  assert.strictEqual(fetchCalls[0].options.method, "POST");
  assert.deepStrictEqual(JSON.parse(fetchCalls[0].options.body), {
    name: "补全项目", code: "NEW", fee_type: "检查费", unit: "次", price: "88",
  });
  assert.strictEqual(dialog.removed, true);
});

test("旧保存成功延迟刷新，关闭新弹窗后恰好重载一次", async () => {
  const dom = fakeDialogDocument();
  const saveRequests = [];
  const fetchCalls = [];
  const sandbox = chargeSandbox({
    admin: true,
    documentImpl: dom.documentImpl,
    fetchImpl(url, options = {}) {
      fetchCalls.push({url, options});
      if (url.startsWith("/api/handle-items/manage")) {
        return Promise.resolve({ok: true, json: async () => ({items: [], counts: {active: 0, stopped: 0}})});
      }
      const request = deferred();
      saveRequests.push(request);
      return request.promise;
    },
  });
  sandbox.__setCiItems([
    {handle_id: "item-old", name: "旧项目", fee_type: "治疗费", price: "10", status: "active"},
    {handle_id: "item-new", name: "新项目", fee_type: "检查费", price: "20", status: "active"},
  ]);

  const oldDialog = fakeDialog({name: "旧项目", code: "A", fee_type: "治疗费", unit: "次", price: "10"});
  dom.enqueue(oldDialog);
  sandbox.__ciTest.openChargeItemDialog("item-old");
  const oldSave = sandbox.__ciTest.saveChargeItem();
  assert.strictEqual(fetchCalls[0].url, "/api/handle-items/item-old");
  assert.strictEqual(fetchCalls[0].options.method, "PUT");

  const newDialog = fakeDialog({name: "新项目", code: "B", fee_type: "检查费", unit: "次", price: "20"});
  dom.enqueue(newDialog);
  sandbox.__ciTest.openChargeItemDialog("item-new");
  saveRequests[0].resolve({ok: true, json: async () => ({})});
  await oldSave;

  assert.strictEqual(dom.currentDialog(), newDialog, "旧成功回调不得关闭后来打开的弹窗");
  assert.strictEqual(newDialog.removed, false);
  assert.strictEqual(fetchCalls.length, 1, "存在新弹窗时旧成功回调不得重载列表");

  await newDialog.buttons.cancel.listeners.click();
  assert.strictEqual(dom.currentDialog(), null);
  assert.strictEqual(
    fetchCalls.filter(call => call.url.startsWith("/api/handle-items/manage")).length,
    1,
    "关闭当前弹窗后必须补做一次列表重载",
  );

  await newDialog.buttons.cancel.listeners.click();
  assert.strictEqual(
    fetchCalls.filter(call => call.url.startsWith("/api/handle-items/manage")).length,
    1,
    "同一个 pending reload 只能 flush 一次",
  );
});

test("延迟刷新存在时当前弹窗保存成功只重载一次", async () => {
  const dom = fakeDialogDocument();
  const saveRequests = [];
  const fetchCalls = [];
  const sandbox = chargeSandbox({
    admin: true,
    documentImpl: dom.documentImpl,
    fetchImpl(url, options = {}) {
      fetchCalls.push({url, options});
      if (url.startsWith("/api/handle-items/manage")) {
        return Promise.resolve({ok: true, json: async () => ({items: [], counts: {active: 0, stopped: 0}})});
      }
      const request = deferred();
      saveRequests.push(request);
      return request.promise;
    },
  });
  sandbox.__setCiItems([
    {handle_id: "item-old", name: "旧项目", fee_type: "治疗费", price: "10", status: "active"},
    {handle_id: "item-new", name: "新项目", fee_type: "检查费", price: "20", status: "active"},
  ]);

  const oldDialog = fakeDialog({name: "旧项目", code: "A", fee_type: "治疗费", unit: "次", price: "10"});
  dom.enqueue(oldDialog);
  sandbox.__ciTest.openChargeItemDialog("item-old");
  const oldSave = sandbox.__ciTest.saveChargeItem();

  const newDialog = fakeDialog({name: "新项目", code: "B", fee_type: "检查费", unit: "次", price: "20"});
  dom.enqueue(newDialog);
  sandbox.__ciTest.openChargeItemDialog("item-new");
  saveRequests[0].resolve({ok: true, json: async () => ({})});
  await oldSave;
  assert.strictEqual(sandbox.__getCiLifecycle().reloadPending, true);
  assert.strictEqual(fetchCalls.filter(call => call.url.startsWith("/api/handle-items/manage")).length, 0);

  const newSave = sandbox.__ciTest.saveChargeItem();
  assert.strictEqual(fetchCalls[1].url, "/api/handle-items/item-new");
  saveRequests[1].resolve({ok: true, json: async () => ({})});
  await newSave;

  assert.strictEqual(dom.currentDialog(), null);
  assert.strictEqual(fetchCalls.filter(call => call.url.startsWith("/api/handle-items/manage")).length, 1);
  assert.strictEqual(sandbox.__getCiLifecycle().reloadPending, false);
});

test("旧保存失败和 finally 不得解除新弹窗 pending 或重复提交保护", async () => {
  const dom = fakeDialogDocument();
  const requests = [];
  const fetchCalls = [];
  const sandbox = chargeSandbox({
    admin: true,
    documentImpl: dom.documentImpl,
    fetchImpl(url, options = {}) {
      fetchCalls.push({url, options});
      const request = deferred();
      requests.push(request);
      return request.promise;
    },
  });
  sandbox.__setCiItems([
    {handle_id: "item-old", name: "旧项目", fee_type: "治疗费", price: "10", status: "active"},
    {handle_id: "item-new", name: "新项目", fee_type: "检查费", price: "20", status: "active"},
  ]);

  const oldDialog = fakeDialog({name: "旧项目", code: "A", fee_type: "治疗费", unit: "次", price: "10"});
  dom.enqueue(oldDialog);
  sandbox.__ciTest.openChargeItemDialog("item-old");
  const oldSave = sandbox.__ciTest.saveChargeItem();
  await sandbox.__ciTest.saveChargeItem();
  assert.strictEqual(requests.length, 1, "同一弹窗保存中不得重复发请求");

  const newDialog = fakeDialog({name: "新项目", code: "B", fee_type: "检查费", unit: "次", price: "20"});
  dom.enqueue(newDialog);
  sandbox.__ciTest.openChargeItemDialog("item-new");
  const newSave = sandbox.__ciTest.saveChargeItem();
  assert.strictEqual(requests.length, 2);
  assert.ok(newDialog.querySelectorAll("button, input").every(control => control.disabled));

  requests[0].reject(new Error("旧请求失败"));
  await oldSave;
  assert.strictEqual(dom.currentDialog(), newDialog);
  assert.strictEqual(newDialog.status.textContent, "", "旧错误不得写进新弹窗");
  assert.ok(newDialog.querySelectorAll("button, input").every(control => control.disabled), "旧 finally 不得解除新请求 pending");
  await sandbox.__ciTest.saveChargeItem();
  assert.strictEqual(requests.length, 2, "旧 finally 不得清除新请求的重复提交保护");

  requests[1].reject(new Error("新请求失败"));
  await newSave;
  assert.strictEqual(newDialog.status.textContent, "保存失败：新请求失败");
  assert.ok(newDialog.querySelectorAll("button, input").every(control => !control.disabled));
});

test("保存弹窗被外部移除后 finally 有界清理旧生命周期", async () => {
  const dom = fakeDialogDocument();
  const request = deferred();
  const sandbox = chargeSandbox({
    admin: true,
    documentImpl: dom.documentImpl,
    fetchImpl: () => request.promise,
  });
  sandbox.__setCiItems([
    {handle_id: "item-old", name: "旧项目", fee_type: "治疗费", price: "10", status: "active"},
  ]);

  const oldDialog = fakeDialog({name: "旧项目", code: "A", fee_type: "治疗费", unit: "次", price: "10"});
  dom.enqueue(oldDialog);
  sandbox.__ciTest.openChargeItemDialog("item-old");
  const save = sandbox.__ciTest.saveChargeItem();
  oldDialog.remove();
  request.reject(new Error("请求失败"));
  await save;

  assert.deepStrictEqual(JSON.parse(JSON.stringify(sandbox.__getCiLifecycle())), {
    saving: false,
    active: false,
    editingId: null,
    reloadPending: false,
  });

  const newDialog = fakeDialog({name: "新项目", code: "B", fee_type: "检查费", unit: "次", price: "20"});
  dom.enqueue(newDialog);
  sandbox.__ciTest.openChargeItemDialog();
  assert.strictEqual(sandbox.__getCiLifecycle().active, true, "旧 finally 清理后仍可正常打开新弹窗");
});

for (const mutation of [
  {label: "停用", method: "DELETE", url: "/api/handle-items/item-1", run: sandbox => sandbox.__ciTest.stopChargeItem("item-1")},
  {label: "恢复", method: "POST", url: "/api/handle-items/item-1/restore", run: sandbox => sandbox.__ciTest.restoreChargeItem("item-1")},
]) {
  test(`${mutation.label}慢成功不得覆盖期间打开的弹窗，关闭后恰好重载一次`, async () => {
    const dom = fakeDialogDocument();
    const mutationRequest = deferred();
    const fetchCalls = [];
    const sandbox = chargeSandbox({
      admin: true,
      documentImpl: dom.documentImpl,
      fetchImpl(url, options = {}) {
        fetchCalls.push({url, options});
        if (url.startsWith("/api/handle-items/manage")) {
          return Promise.resolve({ok: true, json: async () => ({items: [], counts: {active: 0, stopped: 0}})});
        }
        return mutationRequest.promise;
      },
    });

    const pendingMutation = mutation.run(sandbox);
    assert.strictEqual(fetchCalls[0].url, mutation.url);
    assert.strictEqual(fetchCalls[0].options.method, mutation.method);

    const dialog = fakeDialog({name: "新项目", code: "B", fee_type: "检查费", unit: "次", price: "20"});
    dom.enqueue(dialog);
    sandbox.__ciTest.openChargeItemDialog();
    mutationRequest.resolve({ok: true, json: async () => ({})});
    await pendingMutation;

    assert.strictEqual(dom.currentDialog(), dialog, `${mutation.label}成功回调不得移除当前弹窗`);
    assert.strictEqual(dialog.removed, false);
    assert.strictEqual(fetchCalls.filter(call => call.url.startsWith("/api/handle-items/manage")).length, 0);
    assert.strictEqual(sandbox.__getCiLifecycle().reloadPending, true);

    await dialog.buttons.cancel.listeners.click();
    assert.strictEqual(fetchCalls.filter(call => call.url.startsWith("/api/handle-items/manage")).length, 1);
    assert.strictEqual(sandbox.__getCiLifecycle().reloadPending, false);
  });

  test(`${mutation.label}失败只提示错误且不产生延迟刷新`, async () => {
    const dom = fakeDialogDocument();
    const mutationRequest = deferred();
    const fetchCalls = [];
    const alerts = [];
    const sandbox = chargeSandbox({
      admin: true,
      documentImpl: dom.documentImpl,
      fetchImpl(url, options = {}) {
        fetchCalls.push({url, options});
        return mutationRequest.promise;
      },
    });
    sandbox.alert = message => alerts.push(message);

    const pendingMutation = mutation.run(sandbox);
    const dialog = fakeDialog({name: "新项目", code: "B", fee_type: "检查费", unit: "次", price: "20"});
    dom.enqueue(dialog);
    sandbox.__ciTest.openChargeItemDialog();
    mutationRequest.resolve({ok: false, json: async () => ({detail: "服务拒绝"})});
    await pendingMutation;

    assert.strictEqual(dom.currentDialog(), dialog);
    assert.deepStrictEqual(alerts, [`${mutation.label}失败：服务拒绝`]);
    assert.strictEqual(sandbox.__getCiLifecycle().reloadPending, false);
    await dialog.buttons.cancel.listeners.click();
    assert.strictEqual(fetchCalls.length, 1, "失败后关闭弹窗不得触发列表重载");
  });
}

test("停用语义明确且不会写成删除项目", () => {
  const stop = functionSource(charge, "stopChargeItem");
  assert.match(stop, /停用后，这个项目将不再出现在以后新开的处置中；历史处置和收费记录不会改变，可稍后恢复。/);
  assert.doesNotMatch(charge, /删除该收费项目/);
  assert.match(stop, /encodeURIComponent\(id\)[\s\S]*method:\s*"DELETE"/);
  assert.match(functionSource(charge, "restoreChargeItem"), /encodeURIComponent\(id\)[\s\S]*\/restore[\s\S]*method:\s*"POST"/);
});

test("收费项目变更会失效处置选择器缓存并更新静态资源版本", () => {
  const invalidate = functionSource(plan, "invalidateHandleItemsCache");
  assert.match(plan, /function invalidateHandleItemsCache\(\)/);
  assert.match(invalidate, /planHandleItems\s*=\s*null/);
  assert.match(invalidate, /planHandleGroups\s*=\s*null/);

  const planExports = plan.match(/Object\.assign\(window,\s*\{([\s\S]*?)\}\);/)?.[1] || "";
  assert.match(planExports, /(?:^|,)\s*invalidateHandleItemsCache\s*(?:,|$)/);
  assert.match(charge, /invalidateHandleItemsCache\(\)/);

  const sandbox = {};
  vm.createContext(sandbox);
  vm.runInContext(`
    let planHandleItems = [{handle_id: "cached"}];
    let planHandleGroups = [{category: "cached"}];
    let planHandleItemsGeneration = 0;
    ${invalidate}
    invalidateHandleItemsCache();
    this.__cache = {items: planHandleItems, groups: planHandleGroups, generation: planHandleItemsGeneration};
  `, sandbox);
  assert.strictEqual(sandbox.__cache.items, null);
  assert.strictEqual(sandbox.__cache.groups, null);
  assert.strictEqual(sandbox.__cache.generation, 1);

  const versionedResources = [
    '<link rel="stylesheet" href="/styles.css?v=2026-07-27-charge-items">',
    '<script src="/audit_log.js?v=2026-07-27-charge-items"></script>',
    '<script src="/treatment_plan_editor.js?v=2026-07-27-charge-items"></script>',
    '<script src="/charge_items.js?v=2026-07-27-charge-items"></script>',
  ];
  versionedResources.forEach(resource => assert.ok(index.includes(resource), `缺少精确资源引用：${resource}`));
  assert.ok(
    index.indexOf(versionedResources[2]) < index.indexOf(versionedResources[3]),
    "treatment_plan_editor.js 必须先于 charge_items.js 加载",
  );
});

test("缓存失效后在途旧响应不得重新写回处置选择器", async () => {
  const oldResponse = deferred();
  let fetchCount = 0;
  const sandbox = {
    fetch() {
      fetchCount += 1;
      if (fetchCount === 1) return oldResponse.promise;
      return Promise.resolve({
        json: async () => ({groups: [{category: "新分类", items: [{handle_id: "new", name: "新项目", price: 20}]}]}),
      });
    },
  };
  vm.createContext(sandbox);
  const cacheSource = plan.slice(0, plan.indexOf("\nfunction newPlanRow"));
  vm.runInContext(`${cacheSource}
    this.ensure = ensureHandleItems;
    this.invalidate = invalidateHandleItemsCache;
    this.cache = () => ({items: planHandleItems, groups: planHandleGroups});
  `, sandbox);

  const pending = sandbox.ensure();
  sandbox.invalidate();
  oldResponse.resolve({
    json: async () => ({groups: [{category: "旧分类", items: [{handle_id: "old", name: "旧项目", price: 10}]}]}),
  });
  const result = await pending;

  assert.strictEqual(fetchCount, 2, "旧响应失效后必须重新请求当前代次");
  assert.deepStrictEqual(Array.from(result, item => item.handle_id), ["new"]);
  assert.deepStrictEqual(Array.from(sandbox.cache().items, item => item.handle_id), ["new"]);
  assert.deepStrictEqual(Array.from(sandbox.cache().groups, group => group.category), ["新分类"]);
});

test("加载竞态、写入成功重载和保存失败保留弹窗均有源码约束", () => {
  const load = functionSource(charge, "loadChargeItemsModule");
  assert.match(load, /const token\s*=\s*\+\+_ciToken/);
  assert.ok((load.match(/_ciStale\(token\)/g) || []).length >= 2, "异步返回前后都要检查陈旧 token");
  assert.match(load, /收费项目载入失败：\$\{error\.message\s*\|\|\s*"网络异常"\}/);

  const save = functionSource(charge, "saveChargeItem");
  assert.match(save, /fetch\(url,[\s\S]*body:\s*JSON\.stringify\(payload\)/);
  assert.match(save, /if\s*\(!response\.ok\)[\s\S]*throw new Error/);
  assert.match(save, /closeChargeItemDialog\(dialogState,\s*false\)[\s\S]*typeof invalidateHandleItemsCache === "function"[\s\S]*await ciReloadAfterMutation\(\)[\s\S]*catch\s*\(error\)[\s\S]*status\.textContent/);
  assert.doesNotMatch(save.match(/catch\s*\(error\)[\s\S]*?finally/)?.[0] || "", /closeChargeItemDialog/);

  const reload = functionSource(charge, "ciReloadAfterMutation");
  assert.match(reload, /document\.querySelector\("\[data-ci-dialog\]"\)[\s\S]*_ciReloadPending\s*=\s*true/);
  assert.match(reload, /_ciReloadPending\s*=\s*false[\s\S]*await loadChargeItemsModule\(\)/);
  const flush = functionSource(charge, "ciFlushPendingReload");
  assert.match(flush, /!_ciReloadPending[\s\S]*document\.querySelector\("\[data-ci-dialog\]"\)/);
  assert.match(flush, /_ciReloadPending\s*=\s*false[\s\S]*await loadChargeItemsModule\(\)/);
  assert.match(functionSource(charge, "closeChargeItemDialog"), /ciFlushPendingReload\(\)/);

  for (const name of ["stopChargeItem", "restoreChargeItem"]) {
    const source = functionSource(charge, name);
    assert.match(source, /if\s*\(!ciCanManage\(\)\)\s*return/);
    assert.match(source, /typeof invalidateHandleItemsCache === "function"[\s\S]*await ciReloadAfterMutation\(\)/);
  }
});

test("收费项目审计动作显示中文", () => {
  assert.match(audit, /create_handle_item:\s*"新增收费项目"/);
  assert.match(audit, /update_handle_item:\s*"修改收费项目"/);
  assert.match(audit, /stop_handle_item:\s*"停用收费项目"/);
  assert.match(audit, /restore_handle_item:\s*"恢复收费项目"/);
});

test("收费项目审计能够安全展开变更前后值", () => {
  const source = functionSource(audit, "auditHandleItemChangeHtml");
  const sandbox = {
    escapeHtml(value) {
      return String(value ?? "")
        .replaceAll("&", "&amp;")
        .replaceAll("<", "&lt;")
        .replaceAll(">", "&gt;")
        .replaceAll('"', "&quot;");
    },
  };
  vm.createContext(sandbox);
  vm.runInContext(`${source}; this.renderChange = auditHandleItemChangeHtml;`, sandbox);
  const html = sandbox.renderChange({
    entity_type: "handle_item",
    change: {
      before: {name: "旧<项目>", code: "OLD", fee_type: "治疗费", unit: "次", price: 100, stopped: 0},
      after: {name: "新项目", code: "NEW", fee_type: "检查费", unit: "颗", price: 120, stopped: 1},
    },
  });
  assert.match(html, /查看变更前后/);
  assert.match(html, /变更前/);
  assert.match(html, /变更后/);
  assert.match(html, /旧&lt;项目&gt;/);
  assert.match(html, /已停用/);
  assert.doesNotMatch(html, /source_json|last_batch_id/);
  assert.match(functionSource(audit, "renderAuditLogPage"), /auditHandleItemChangeHtml\(r\)/);
});
