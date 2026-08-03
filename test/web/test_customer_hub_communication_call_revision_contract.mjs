import { test } from "node:test";
import assert from "node:assert";
import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import { dirname, join } from "node:path";
import vm from "node:vm";

const here = dirname(fileURLToPath(import.meta.url));
const source = name => readFileSync(
  join(here, "..", "..", "local_app", "static", name),
  "utf8",
);
const communications = source("communications.js");
const calls = source("call_recordings.js");
const returnVisitCard = source("return_visit_card.js");

function section(text, startMarker, endMarker) {
  const start = text.indexOf(startMarker);
  assert.ok(start >= 0, `missing ${startMarker}`);
  const end = text.indexOf(endMarker, start + startMarker.length);
  assert.ok(end > start, `missing ${endMarker}`);
  return text.slice(start, end);
}

function ok(body = {}) {
  return {ok: true, status: 200, json: async () => body};
}

function conflict() {
  return {
    ok: false,
    status: 409,
    json: async () => ({detail: "stale_write"}),
  };
}

function commHarness(writeResponse = ok()) {
  const requests = [];
  const alerts = [];
  let reloads = 0;
  const sandbox = {
    confirm: () => true,
    alert: message => alerts.push(message),
    fetch: async (url, options) => {
      requests.push({url, options});
      return writeResponse;
    },
    reloadCommunicationsSub: async () => { reloads += 1; },
    loadAuditLogs: async () => {},
    encodeURIComponent,
  };
  vm.createContext(sandbox);
  vm.runInContext(
    section(communications, "async function setCommDeal", "// #722 \u5361\u7247\u8865\u4f20")
      + "\nthis.__set = setCommDeal; this.__delete = deleteCommunication;",
    sandbox,
  );
  return {sandbox, requests, alerts, reloads: () => reloads};
}

function callHarness(writeResponse = ok()) {
  const requests = [];
  const alerts = [];
  let reloads = 0;
  const sandbox = {
    confirm: () => true,
    alert: message => alerts.push(message),
    fetch: async (url, options) => {
      requests.push({url, options});
      return writeResponse;
    },
    reloadCallsInPlace: async () => { reloads += 1; },
    workspaceLoadedTabs: new Set(["return-visits"]),
    switchWorkspaceTab: () => { reloads += 1; },
    loadAuditLogs: async () => {},
    encodeURIComponent,
  };
  vm.createContext(sandbox);
  vm.runInContext(
    section(calls, "async function setCallDeal", "Object.assign(window")
      + "\nthis.__set = setCallDeal; this.__delete = deleteCall;",
    sandbox,
  );
  return {sandbox, requests, alerts, reloads: () => reloads};
}

function inlineEvent(html, attribute, functionName) {
  const escapedName = functionName.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
  const match = html.match(
    new RegExp(`${attribute}="([^"]*${escapedName}\\([^"]*)"`),
  );
  assert.ok(match, `rendered ${attribute} must call ${functionName}`);
  return match[1];
}

function runInlineEvent(sandbox, html, attribute, functionName, value = "") {
  sandbox.value = value;
  return vm.runInContext(
    inlineEvent(html, attribute, functionName),
    sandbox,
  );
}

test("communication render wires current row revision into edit and delete handlers", () => {
  const events = [];
  const sandbox = {
    encodeURIComponent,
    escapeAttr: value => String(value ?? ""),
    escapeHtml: value => String(value ?? ""),
    csRow: () => "",
    area: () => "",
    setCommDeal: (...args) => events.push(["edit", ...args]),
    deleteCommunication: (...args) => events.push(["delete", ...args]),
  };
  vm.createContext(sandbox);
  vm.runInContext(
    section(communications, "const COMM_DEAL_OPTIONS", "function commFormEl")
      + "\nthis.__render = renderCommunications;",
    sandbox,
  );
  const panel = {innerHTML: ""};
  sandbox.__render(panel, {
    communications: [{
      communication_id: "comm-render",
      channel: "phone",
      direction: "outbound",
      revision: 17,
      images: [],
    }],
  }, {calls: []});
  runInlineEvent(sandbox, panel.innerHTML, "onchange", "setCommDeal", "意向");
  runInlineEvent(sandbox, panel.innerHTML, "onclick", "deleteCommunication");
  assert.deepStrictEqual(events, [
    ["edit", "comm-render", 17, "意向"],
    ["delete", "comm-render", 17],
  ]);
});

test("call list render wires current row revision into edit and delete handlers", () => {
  const events = [];
  const sandbox = {
    encodeURIComponent,
    escapeAttr: value => String(value ?? ""),
    escapeHtml: value => String(value ?? ""),
    csRow: () => "",
    field: () => "",
    setCallDeal: (...args) => events.push(["edit", ...args]),
    deleteCall: (...args) => events.push(["delete", ...args]),
  };
  vm.createContext(sandbox);
  vm.runInContext(
    section(calls, "const CALL_DEAL_OPTIONS", "function callFormEl")
      + "\nthis.__render = renderCallsTab;",
    sandbox,
  );
  const panel = {innerHTML: ""};
  sandbox.__render(panel, {calls: [{
    call_id: "call-render",
    direction: "outbound",
    revision: 23,
  }]});
  runInlineEvent(sandbox, panel.innerHTML, "onchange", "setCallDeal", "已成交");
  runInlineEvent(sandbox, panel.innerHTML, "onclick", "deleteCall");
  assert.deepStrictEqual(events, [
    ["edit", "call-render", 23, "已成交"],
    ["delete", "call-render", 23],
  ]);
});

test("return-visit card render wires current call revision into delete handler", () => {
  const events = [];
  const overlay = {innerHTML: ""};
  const sandbox = {
    document: {getElementById: () => overlay},
    encodeURIComponent,
    escapeAttr: value => String(value ?? ""),
    escapeHtml: value => String(value ?? ""),
    deleteRvCardCall: (...args) => events.push(args),
  };
  vm.createContext(sandbox);
  vm.runInContext(
    section(returnVisitCard, "let _rvCardId", "function rvCardForm")
      + "\n_rvCardId = 'rv-render'; this.__render = renderReturnVisitCard;",
    sandbox,
  );
  sandbox.__render({
    display_name: "Synthetic patient",
    item_name: "Synthetic visit",
    status: "待回访",
    revision: 1,
    images: [],
  }, [{call_id: "call-card-render", revision: 31}]);
  runInlineEvent(
    sandbox,
    overlay.innerHTML,
    "onclick",
    "deleteRvCardCall",
  );
  assert.deepStrictEqual(events, [["call-card-render", 31]]);
});

test("communication PATCH and DELETE send exact revision JSON", async () => {
  const patch = commHarness(ok({communication: {revision: 8}}));
  await patch.sandbox.__set("comm/one", 7, "\u5df2\u6210\u4ea4");
  assert.strictEqual(patch.requests.length, 1);
  assert.strictEqual(patch.requests[0].options.method, "PATCH");
  assert.deepStrictEqual(
    JSON.parse(patch.requests[0].options.body),
    {expected_revision: 7, deal_status: "\u5df2\u6210\u4ea4"},
  );

  const remove = commHarness(ok({files_failed: 0}));
  await remove.sandbox.__delete("comm/two", 11);
  assert.strictEqual(remove.requests.length, 1);
  assert.strictEqual(remove.requests[0].options.method, "DELETE");
  assert.deepStrictEqual(
    JSON.parse(remove.requests[0].options.body),
    {expected_revision: 11},
  );
});

test("call PATCH and both DELETE callers send exact revision JSON", async () => {
  const patch = callHarness(ok({call: {revision: 6}}));
  await patch.sandbox.__set("call/one", 5, "\u610f\u5411");
  assert.strictEqual(patch.requests.length, 1);
  assert.deepStrictEqual(
    JSON.parse(patch.requests[0].options.body),
    {expected_revision: 5, deal_status: "\u610f\u5411"},
  );

  const remove = callHarness(ok({file_removed: true}));
  await remove.sandbox.__delete("call/two", 9);
  assert.strictEqual(remove.requests.length, 1);
  assert.strictEqual(remove.requests[0].options.method, "DELETE");
  assert.deepStrictEqual(
    JSON.parse(remove.requests[0].options.body),
    {expected_revision: 9},
  );

  const requests = [];
  const sandbox = {
    confirm: () => true,
    alert: () => assert.fail("unexpected alert"),
    fetch: async (url, options) => {
      requests.push({url, options});
      return ok({file_removed: true});
    },
    loadReturnVisitCard: async () => {},
    encodeURIComponent,
  };
  vm.createContext(sandbox);
  vm.runInContext(
    section(returnVisitCard, "async function deleteRvCardCall", "Object.assign(window")
      + "\nthis.__delete = deleteRvCardCall;",
    sandbox,
  );
  await sandbox.__delete("call/card", 13);
  assert.strictEqual(requests.length, 1);
  assert.deepStrictEqual(
    JSON.parse(requests[0].options.body),
    {expected_revision: 13},
  );
});

test("communication conflicts are neutral, refresh once, and never replay", async () => {
  for (const action of ["set", "delete"]) {
    const harness = commHarness(conflict());
    if (action === "set") {
      await harness.sandbox.__set("comm/conflict", 3, "\u610f\u5411");
    } else {
      await harness.sandbox.__delete("comm/conflict", 3);
    }
    assert.strictEqual(harness.requests.length, 1, `${action} must not replay`);
    assert.deepStrictEqual(harness.alerts, [
      "\u6570\u636e\u5df2\u88ab\u5176\u4ed6\u64cd\u4f5c\u66f4\u65b0\uff0c\u8bf7\u786e\u8ba4\u6700\u65b0\u5185\u5bb9\u540e\u91cd\u8bd5",
    ]);
    assert.strictEqual(harness.reloads(), 1);
  }
});

test("call conflicts in both views are neutral, refresh once, and never replay", async () => {
  for (const action of ["set", "delete"]) {
    const harness = callHarness(conflict());
    if (action === "set") {
      await harness.sandbox.__set("call/conflict", 4, "\u610f\u5411");
    } else {
      await harness.sandbox.__delete("call/conflict", 4);
    }
    assert.strictEqual(harness.requests.length, 1, `${action} must not replay`);
    assert.deepStrictEqual(harness.alerts, [
      "\u6570\u636e\u5df2\u88ab\u5176\u4ed6\u64cd\u4f5c\u66f4\u65b0\uff0c\u8bf7\u786e\u8ba4\u6700\u65b0\u5185\u5bb9\u540e\u91cd\u8bd5",
    ]);
    assert.strictEqual(harness.reloads(), 1);
  }

  const requests = [];
  const alerts = [];
  let reloads = 0;
  const sandbox = {
    confirm: () => true,
    alert: message => alerts.push(message),
    fetch: async (url, options) => {
      requests.push({url, options});
      return conflict();
    },
    loadReturnVisitCard: async () => { reloads += 1; },
    encodeURIComponent,
  };
  vm.createContext(sandbox);
  vm.runInContext(
    section(returnVisitCard, "async function deleteRvCardCall", "Object.assign(window")
      + "\nthis.__delete = deleteRvCardCall;",
    sandbox,
  );
  await sandbox.__delete("call/card-conflict", 12);
  assert.strictEqual(requests.length, 1);
  assert.deepStrictEqual(alerts, [
    "\u6570\u636e\u5df2\u88ab\u5176\u4ed6\u64cd\u4f5c\u66f4\u65b0\uff0c\u8bf7\u786e\u8ba4\u6700\u65b0\u5185\u5bb9\u540e\u91cd\u8bd5",
  ]);
  assert.strictEqual(reloads, 1);
});
