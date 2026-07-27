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
const workspaceTabs = source("workspace_tabs.js");
const returnVisitCard = source("return_visit_card.js");

function section(text, startMarker, endMarker) {
  const start = text.indexOf(startMarker);
  assert.ok(start >= 0, `missing ${startMarker}`);
  const end = text.indexOf(endMarker, start + startMarker.length);
  assert.ok(end > start, `missing ${endMarker}`);
  return text.slice(start, end);
}

function response() {
  return {ok: true, status: 200, json: async () => ({})};
}

function plain(value) {
  return JSON.parse(JSON.stringify(value));
}

function assertJsonWrite(call, {url, method, revision}) {
  assert.strictEqual(call.url, url);
  assert.strictEqual(call.options.method, method);
  assert.deepStrictEqual(plain(call.options.headers), {
    "Content-Type": "application/json",
  });
  const body = JSON.parse(call.options.body);
  assert.strictEqual(body.expected_revision, revision);
  assert.ok(!Object.hasOwn(body, "actual_date"));
  return body;
}

test("patient workspace delete executes DELETE with rendered revision", async () => {
  const calls = [];
  const container = {dataset: {returnVisitRevision: "11"}};
  const sandbox = {
    cssEscape: value => value,
    document: {querySelector: () => container},
    window: {confirm: () => true, alert: () => assert.fail("unexpected alert")},
    fetch: async (url, options) => {
      calls.push({url, options});
      return response();
    },
    refreshWorkspaceDetail: async () => {},
    workspaceLoadedTabs: new Set(["return-visits"]),
    evictVisitsCache: () => {},
    switchWorkspaceTab: () => {},
    loadAuditLogs: async () => {},
    encodeURIComponent,
  };
  vm.createContext(sandbox);
  const refreshCode = section(
    workspaceTabs,
    "async function refreshReturnVisitWorkspaceFromServer",
    "// \u60a3\u8005\u6863\u6848\u91cc\u5220\u9664\u4e00\u6761\u56de\u8bbf",
  );
  const code = section(
    workspaceTabs,
    "async function deleteReturnVisitRow",
    "function markReturnDone",
  );
  vm.runInContext(
    refreshCode + "\n" + code + "\nthis.__delete = deleteReturnVisitRow;",
    sandbox,
  );

  await sandbox.__delete("rv/workspace 1");

  assert.strictEqual(calls.length, 1);
  assertJsonWrite(calls[0], {
    url: "/api/return-visits/rv%2Fworkspace%201",
    method: "DELETE",
    revision: 11,
  });
});

test("patient workspace delete reloads latest revision after 409", async () => {
  const calls = [];
  const alerts = [];
  const refreshed = {detail: 0, switched: 0, audits: 0};
  const loadedTabs = new Set(["return-visits"]);
  const container = {dataset: {returnVisitRevision: "11"}};
  const sandbox = {
    cssEscape: value => value,
    document: {querySelector: () => container},
    window: {confirm: () => true, alert: message => alerts.push(message)},
    fetch: async (url, options) => {
      calls.push({url, options});
      return {
        ok: false,
        status: 409,
        json: async () => ({detail: "return_visit_stale"}),
      };
    },
    refreshWorkspaceDetail: async () => { refreshed.detail += 1; },
    workspaceLoadedTabs: loadedTabs,
    evictVisitsCache: () => {},
    switchWorkspaceTab: () => { refreshed.switched += 1; },
    loadAuditLogs: async () => { refreshed.audits += 1; },
    encodeURIComponent,
  };
  vm.createContext(sandbox);
  const refreshCode = section(
    workspaceTabs,
    "async function refreshReturnVisitWorkspaceFromServer",
    "// \u60a3\u8005\u6863\u6848\u91cc\u5220\u9664\u4e00\u6761\u56de\u8bbf",
  );
  const code = section(
    workspaceTabs,
    "async function deleteReturnVisitRow",
    "function markReturnDone",
  );
  vm.runInContext(
    refreshCode + "\n" + code + "\nthis.__delete = deleteReturnVisitRow;",
    sandbox,
  );

  await sandbox.__delete("rv/conflict");

  assert.strictEqual(calls.length, 1, "must not replay stale DELETE");
  assert.deepStrictEqual(alerts, [
    "数据已被其他操作更新，请确认最新内容后重试",
  ]);
  assert.deepStrictEqual(refreshed, {detail: 1, switched: 1, audits: 0});
  assert.ok(!loadedTabs.has("return-visits"));
});

function workspaceSaveHarness({
  revision = "13",
  statusValue = "待回访",
  writeResponse = response(),
} = {}) {
  const calls = [];
  const status = {textContent: ""};
  const statusInput = {
    dataset: {returnVisitField: "status"},
    value: statusValue,
  };
  const noteInput = {
    dataset: {returnVisitField: "note"},
    value: "  synthetic note  ",
  };
  const container = {
    dataset: {returnVisitRevision: revision},
    querySelectorAll: () => [statusInput, noteInput],
    querySelector: selector => (
      selector === '[data-return-visit-field="status"]' ? statusInput : null
    ),
  };
  const refreshed = {detail: 0, switched: 0, audits: 0};
  const sandbox = {
    cssEscape: value => value,
    document: {
      querySelector: () => container,
      getElementById: () => status,
    },
    fetch: async (url, options) => {
      calls.push({url, options});
      return writeResponse;
    },
    refreshWorkspaceDetail: async () => { refreshed.detail += 1; },
    workspaceLoadedTabs: new Set(["return-visits"]),
    evictVisitsCache: () => {},
    switchWorkspaceTab: () => { refreshed.switched += 1; },
    loadAuditLogs: async () => { refreshed.audits += 1; },
    encodeURIComponent,
  };
  vm.createContext(sandbox);
  const saveCode = section(
    workspaceTabs,
    "async function saveReturnVisit",
    "// ---------- \u5904\u7f6e tab",
  );
  const markCode = section(
    workspaceTabs,
    "function markReturnDone",
    "async function saveAppointment",
  );
  const refreshCode = section(
    workspaceTabs,
    "async function refreshReturnVisitWorkspaceFromServer",
    "// \u60a3\u8005\u6863\u6848\u91cc\u5220\u9664\u4e00\u6761\u56de\u8bbf",
  );
  vm.runInContext(
    refreshCode + "\n" + saveCode + "\n" + markCode
      + "\nthis.__save = saveReturnVisit; this.__mark = markReturnDone;",
    sandbox,
  );
  return {sandbox, calls, status, statusInput, refreshed};
}

test("patient workspace save executes PUT with current revision", async () => {
  const harness = workspaceSaveHarness();

  await harness.sandbox.__save("rv/save 1");

  assert.strictEqual(harness.calls.length, 1);
  const body = assertJsonWrite(harness.calls[0], {
    url: "/api/return-visits/rv%2Fsave%201",
    method: "PUT",
    revision: 13,
  });
  assert.deepStrictEqual(body, {
    expected_revision: 13,
    status: "\u5f85\u56de\u8bbf",
    note: "synthetic note",
  });
  assert.deepStrictEqual(
    harness.refreshed,
    {detail: 1, switched: 1, audits: 1},
  );
});

test("patient workspace mark-done executes save without client actual_date", async () => {
  const harness = workspaceSaveHarness({revision: "17"});

  harness.sandbox.__mark("rv/mark 1");
  await Promise.resolve();

  assert.strictEqual(harness.statusInput.value, "\u5df2\u56de\u8bbf");
  assert.strictEqual(harness.calls.length, 1);
  const body = assertJsonWrite(harness.calls[0], {
    url: "/api/return-visits/rv%2Fmark%201",
    method: "PUT",
    revision: 17,
  });
  assert.strictEqual(body.status, "\u5df2\u56de\u8bbf");
});

test("patient workspace save and mark-done reload after 409 without replay", async () => {
  const conflict = {
    ok: false,
    status: 409,
    json: async () => ({detail: "return_visit_stale"}),
  };
  for (const action of ["save", "mark"]) {
    const harness = workspaceSaveHarness({
      revision: action === "save" ? "23" : "29",
      writeResponse: conflict,
    });

    if (action === "save") {
      await harness.sandbox.__save("rv/conflict");
    } else {
      harness.sandbox.__mark("rv/conflict");
      await new Promise(resolve => setImmediate(resolve));
    }

    assert.strictEqual(
      harness.calls.length,
      1,
      `${action} must not replay stale PUT`,
    );
    assert.strictEqual(
      harness.status.textContent,
      "数据已被其他操作更新，请确认最新内容后重试",
    );
    assert.deepStrictEqual(
      harness.refreshed,
      {detail: 1, switched: 1, audits: 0},
    );
  }
});

test("return-visit popup save executes PUT with detail revision", async () => {
  const calls = [];
  const status = {textContent: ""};
  const fields = [
    {dataset: {rvf: "status"}, value: "\u5df2\u56de\u8bbf"},
    {dataset: {rvf: "note"}, value: "  popup note  "},
  ];
  const form = {
    dataset: {rvCardRevision: "19"},
    querySelectorAll: () => fields,
  };
  const refreshed = {list: 0, detail: 0};
  const sandbox = {
    rvCardForm: () => form,
    rvCardStatusEl: () => status,
    fetch: async (url, options) => {
      calls.push({url, options});
      return response();
    },
    loadReturnVisitModule: () => { refreshed.list += 1; },
    loadReturnVisitCard: async () => { refreshed.detail += 1; },
    encodeURIComponent,
  };
  vm.createContext(sandbox);
  const code = section(
    returnVisitCard,
    "async function saveRvCard",
    "function markRvCardDone",
  );
  vm.runInContext(
    "let _rvCardId = 'rv/popup 1';\n" + code
      + "\nthis.__save = saveRvCard;",
    sandbox,
  );

  await sandbox.__save();

  assert.strictEqual(calls.length, 1);
  const body = assertJsonWrite(calls[0], {
    url: "/api/return-visits/rv%2Fpopup%201",
    method: "PUT",
    revision: 19,
  });
  assert.deepStrictEqual(body, {
    expected_revision: 19,
    status: "\u5df2\u56de\u8bbf",
    note: "popup note",
  });
  assert.deepStrictEqual(refreshed, {list: 1, detail: 1});
});

test("return-visit popup reloads list and detail after 409 without replay", async () => {
  const calls = [];
  const status = {textContent: ""};
  const form = {
    dataset: {rvCardRevision: "31"},
    querySelectorAll: () => [
      {dataset: {rvf: "status"}, value: "\u5df2\u56de\u8bbf"},
    ],
  };
  const refreshed = {list: 0, detail: 0};
  const sandbox = {
    rvCardForm: () => form,
    rvCardStatusEl: () => status,
    fetch: async (url, options) => {
      calls.push({url, options});
      return {
        ok: false,
        status: 409,
        json: async () => ({detail: "return_visit_stale"}),
      };
    },
    loadReturnVisitModule: async () => { refreshed.list += 1; },
    loadReturnVisitCard: async () => { refreshed.detail += 1; },
    encodeURIComponent,
  };
  vm.createContext(sandbox);
  const code = section(
    returnVisitCard,
    "async function saveRvCard",
    "function markRvCardDone",
  );
  vm.runInContext(
    "let _rvCardId = 'rv/conflict';\n" + code
      + "\nthis.__save = saveRvCard;",
    sandbox,
  );

  await sandbox.__save();

  assert.strictEqual(calls.length, 1, "must not replay stale PUT");
  assert.strictEqual(
    status.textContent,
    "数据已被其他操作更新，请确认最新内容后重试",
  );
  assert.deepStrictEqual(refreshed, {list: 1, detail: 1});
});
