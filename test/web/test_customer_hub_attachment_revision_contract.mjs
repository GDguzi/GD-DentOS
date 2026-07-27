import {test} from "node:test";
import assert from "node:assert";
import {readFileSync} from "node:fs";
import {fileURLToPath} from "node:url";
import {dirname, join} from "node:path";
import vm from "node:vm";

const here = dirname(fileURLToPath(import.meta.url));
const source = name => readFileSync(
  join(here, "..", "..", "local_app", "static", name),
  "utf8",
);

class CapturingFormData {
  constructor() { this.values = []; }
  append(name, value, filename) { this.values.push([name, value, filename]); }
  get(name) {
    const found = this.values.find(([key]) => key === name);
    return found && found[1];
  }
}

function response(body = {}, status = 200) {
  return {
    ok: status >= 200 && status < 300,
    status,
    json: async () => body,
  };
}

function evaluate(name, sandbox, trailer = "") {
  sandbox.window ||= {};
  sandbox.encodeURIComponent ||= encodeURIComponent;
  sandbox.FormData ||= CapturingFormData;
  sandbox.alert ||= () => {};
  sandbox.confirm ||= () => true;
  vm.createContext(sandbox);
  vm.runInContext(source(name) + "\n" + trailer, sandbox);
  return sandbox;
}

test("return-visit image uploads chain server revisions and 409 stops without replay", async () => {
  const writes = [];
  const alerts = [];
  let reloads = 0;
  const fileInput = {files: [{name: "one.png"}, {name: "two.png"}, {name: "three.png"}]};
  const form = {dataset: {rvCardRevision: "7"}};
  const overlay = {
    querySelector: selector => selector === "[data-rv-shot]" ? fileInput : (
      selector === "[data-rv-card-form]" ? form : null
    ),
  };
  const replies = [response({revision: 8}), response({detail: "return_visit_stale"}, 409)];
  const sandbox = evaluate("return_visit_card.js", {
    document: {
      getElementById: () => overlay,
      addEventListener: () => {},
      removeEventListener: () => {},
    },
    fetch: async (url, options) => {
      writes.push({url, options});
      return replies.shift();
    },
    alert: message => alerts.push(message),
    onReload: async () => { reloads += 1; },
  }, "loadReturnVisitCard = async () => this.onReload(); this.__setRv = () => {_rvCardId = 'rv/one'; _rvCardPid = 'patient/one';};");
  sandbox.__setRv();

  await sandbox.window.uploadRvCardImages();

  assert.strictEqual(writes.length, 2, "409 must stop before the third file");
  assert.strictEqual(writes[0].options.body.get("expected_revision"), "7");
  assert.strictEqual(writes[1].options.body.get("expected_revision"), "8");
  assert.strictEqual(form.dataset.rvCardRevision, "8");
  assert.deepStrictEqual(alerts, [
    "数据已被其他操作更新，请确认最新内容后重试",
  ]);
  assert.strictEqual(reloads, 1);
});

test("return-visit audio upload sends its exact context revision", async () => {
  const writes = [];
  const form = {dataset: {rvCardRevision: "11"}};
  const overlay = {
    querySelector: selector => {
      if (selector === "[data-rv-audio]") return {files: [{name: "call.wav"}]};
      if (selector === "[data-rv-card-form]") return form;
      return null;
    },
  };
  const sandbox = evaluate("return_visit_card.js", {
    document: {
      getElementById: () => overlay,
      addEventListener: () => {},
      removeEventListener: () => {},
    },
    fetch: async (url, options) => {
      writes.push({url, options});
      return response({context_revision: 12, revision: 1});
    },
    onReload: async () => {},
  }, "loadReturnVisitCard = async () => this.onReload(); this.__setRv = () => {_rvCardId = 'rv/ctx'; _rvCardPid = 'patient/ctx';};");
  sandbox.__setRv();

  await sandbox.window.uploadRvCardAudio();

  assert.strictEqual(writes.length, 1);
  assert.strictEqual(writes[0].options.body.get("linked_return_visit_id"), "rv/ctx");
  assert.strictEqual(writes[0].options.body.get("expected_revision"), "11");
  assert.strictEqual(form.dataset.rvCardRevision, "12");
});

test("return-visit image delete sends revision JSON and 409 reloads once", async () => {
  const writes = [];
  const alerts = [];
  let reloads = 0;
  const form = {dataset: {rvCardRevision: "13"}};
  const overlay = {querySelector: selector => selector === "[data-rv-card-form]" ? form : null};
  const sandbox = evaluate("return_visit_card.js", {
    document: {
      getElementById: () => overlay,
      addEventListener: () => {},
      removeEventListener: () => {},
    },
    fetch: async (url, options) => {
      writes.push({url, options});
      return response({detail: "return_visit_stale"}, 409);
    },
    alert: message => alerts.push(message),
    onReload: async () => { reloads += 1; },
  }, "loadReturnVisitCard = async () => this.onReload(); this.__setRv = () => {_rvCardId = 'rv/delete';};");
  sandbox.__setRv();

  await sandbox.window.deleteRvCardImage("image/one");

  assert.strictEqual(writes.length, 1);
  assert.deepStrictEqual(JSON.parse(writes[0].options.body), {expected_revision: 13});
  assert.deepStrictEqual(alerts, [
    "数据已被其他操作更新，请确认最新内容后重试",
  ]);
  assert.strictEqual(reloads, 1);
});

test("communication reattachments send parent revision and refresh on conflict", async () => {
  const writes = [];
  const alerts = [];
  let reloads = 0;
  const page = {
    querySelector: selector => selector.startsWith("[data-reshot=")
      ? {files: [{name: "shot.png"}]}
      : null,
  };
  const sandbox = evaluate("communications.js", {
    workspacePatientId: "patient/comm",
    workspacePageEl: () => page,
    fetch: async (url, options) => {
      writes.push({url, options});
      return response({detail: "communication_stale"}, 409);
    },
    alert: message => alerts.push(message),
    onReload: () => { reloads += 1; },
  }, "reloadCommunicationsSub = () => this.onReload();");

  await sandbox.window.reattachCommImages("comm/one", 17);

  assert.strictEqual(writes.length, 1);
  assert.strictEqual(writes[0].options.body.get("expected_revision"), "17");
  assert.deepStrictEqual(alerts, [
    "数据已被其他操作更新，请确认最新内容后重试",
  ]);
  assert.strictEqual(reloads, 1);
});

test("communication image delete sends exact revision JSON", async () => {
  const writes = [];
  const sandbox = evaluate("communications.js", {
    fetch: async (url, options) => {
      writes.push({url, options});
      return response({revision: 20, file_removed: true});
    },
    onReload: () => {},
  }, "reloadCommunicationsSub = () => this.onReload();");

  await sandbox.window.deleteCommImage("comm/delete", "image/delete", 19);

  assert.strictEqual(writes.length, 1);
  assert.strictEqual(writes[0].options.method, "DELETE");
  assert.deepStrictEqual(JSON.parse(writes[0].options.body), {expected_revision: 19});
});

test("generic call upload uses the selected return-visit context revision", async () => {
  const writes = [];
  const status = {textContent: ""};
  const selected = {
    value: "rv/selected",
    selectedOptions: [{dataset: {revision: "23"}}],
  };
  const fields = {
    direction: {dataset: {field: "direction"}, value: "outbound"},
    peer_number: {dataset: {field: "peer_number"}, value: ""},
    started_at: {dataset: {field: "started_at"}, value: ""},
    duration_sec: {dataset: {field: "duration_sec"}, value: "0"},
    deal_status: {dataset: {field: "deal_status"}, value: ""},
    note: {dataset: {field: "note"}, value: ""},
  };
  const form = {
    querySelectorAll: selector => selector === "[data-field]" ? Object.values(fields) : [],
    querySelector: selector => {
      if (selector === "[data-call-context]") return selected;
      return null;
    },
  };
  const sandbox = evaluate("call_recordings.js", {
    workspacePatientId: "patient/call",
    workspacePageEl: () => ({querySelector: () => form}),
    fetch: async (url, options) => {
      writes.push({url, options});
      return response({revision: 1, context_revision: 24});
    },
    workspaceLoadedTabs: new Set(["return-visits"]),
    switchWorkspaceTab: () => {},
    loadAuditLogs: async () => {},
  }, "this.postCallRecordingForTest = postCallRecording;");

  await sandbox.postCallRecordingForTest(
    {name: "call.wav"},
    "call.wav",
    "manual_import",
    status,
  );

  assert.strictEqual(writes.length, 1);
  assert.strictEqual(writes[0].options.body.get("linked_return_visit_id"), "rv/selected");
  assert.strictEqual(writes[0].options.body.get("expected_revision"), "23");
});
