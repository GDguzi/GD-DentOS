import { test } from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";
import vm from "node:vm";

const here = dirname(fileURLToPath(import.meta.url));
const source = readFileSync(
  join(here, "..", "..", "local_app", "static", "customer_hub_v2.js"),
  "utf8",
);

function section(startMarker, endMarker) {
  const start = source.indexOf(startMarker);
  assert.ok(start >= 0, `missing ${startMarker}`);
  const end = source.indexOf(endMarker, start + startMarker.length);
  assert.ok(end > start, `missing ${endMarker}`);
  return source.slice(start, end);
}

const leaveSource = section(
  "async function requestLeaveCustomerHubV2",
  "function _chvTabsHtml",
);
const permissionHelperSource = section(
  "function _chvHasAllPermissions",
  "function _chvContractApi",
);
const encodeHelperSource = section(
  "function _chvEncodeComponent",
  "function _chvHasAllPermissions",
);
const overlayPendingSource = section(
  "function _chvOverlayPending",
  "function _chvOverlayControls",
);
const overlayWriteSource = section(
  "function _chvOverlayControls",
  "function _chvWriteError",
);
const micSource = section(
  "function _chvMicButton",
  "// ============ 弹层基建",
);
const overlaySource = section(
  "// ============ 弹层基建",
  "// ============ 月历弹卡",
);
const rvSuccessCloseSource = section(
  "// 删除必须携带当前 revision",
  "// 新增预约:复用预约新增弹窗",
);

const settle = () => new Promise(resolve => setTimeout(resolve, 0));

function deferred() {
  let resolve;
  let reject;
  const promise = new Promise((done, fail) => {
    resolve = done;
    reject = fail;
  });
  return {promise, resolve, reject};
}

class FakeElement {
  constructor(tagName = "div", ownerDocument = null) {
    this.tagName = tagName.toUpperCase();
    this.ownerDocument = ownerDocument;
    this.dataset = {};
    this.disabled = false;
    this.hidden = false;
    this.textContent = "";
    this.title = "";
    this.type = "";
    this.className = "";
    this.id = "";
    this.removed = false;
    this.isConnected = true;
    this.focusCount = 0;
    this.attributes = new Map();
    this._listeners = new Map();
    this._closeButtons = [];
    this._controls = [];
    this._dialog = null;
    this._html = "";
  }

  addEventListener(type, listener) {
    const listeners = this._listeners.get(type) || [];
    listeners.push(listener);
    this._listeners.set(type, listeners);
  }

  async dispatch(type, event = {}) {
    let result;
    for (const listener of this._listeners.get(type) || []) {
      result = await listener({target: this, preventDefault() {}, ...event});
    }
    return result;
  }

  set innerHTML(value) {
    this._html = String(value);
    this._dialog = this._html.includes("class=\"chv-pop")
      ? new FakeElement("div", this.ownerDocument) : null;
    if (this._dialog) this._dialog.className = "chv-pop";
    this._closeButtons = this._html.includes("data-chv-close")
      ? [new FakeElement("button", this.ownerDocument)]
      : [];
    for (const button of this._closeButtons) button.dataset.chvClose = "1";
    this._controls = [...this._closeButtons];
  }

  get innerHTML() { return this._html; }

  querySelectorAll(selector) {
    if (selector === "[data-chv-close]") return this._closeButtons;
    if (selector === "input, select, textarea, button") return this._controls;
    if (selector === "button, a[href], input, select, textarea, [tabindex]") {
      return this._controls.filter(control => !control.disabled);
    }
    return [];
  }

  querySelector(selector) {
    if (selector === ".chv-pop") return this._dialog;
    return this.querySelectorAll(selector)[0] || null;
  }

  setAttribute(name, value) {
    this.attributes.set(name, String(value));
    if (name === "disabled") this.disabled = true;
    if (name === "hidden") this.hidden = true;
  }
  getAttribute(name) { return this.attributes.get(name) ?? null; }
  focus() {
    if (this.isConnected === false || this.disabled || this.hidden
        || this.getAttribute("aria-hidden") === "true") return;
    const focusable = (this.tagName === "INPUT" && this.type !== "hidden")
      || ["BUTTON", "SELECT", "TEXTAREA"].includes(this.tagName)
      || (this.tagName === "A" && this.getAttribute("href") !== null)
      || this.getAttribute("tabindex") !== null;
    if (!focusable) return;
    this.focusCount += 1;
    if (this.ownerDocument) this.ownerDocument.activeElement = this;
  }

  remove() { this.removed = true; this.isConnected = false; }
}

function makeHarness({
  getUserMedia,
  writeImpl,
  readImpl,
  confirmImpl,
  recorderConstructorFailures = 0,
  recorderStartFailures = 0,
} = {}) {
  const writes = [];
  const reads = [];
  const writeErrors = [];
  const alerts = [];
  const openedPatients = [];
  const confirmations = [];
  const recorders = [];
  const overlays = [];
  let mediaCalls = 0;
  let intervalId = 0;
  let constructorFailuresLeft = recorderConstructorFailures;
  let startFailuresLeft = recorderStartFailures;

  class FakeBlob {
    constructor(parts, options = {}) {
      this.parts = parts;
      this.type = options.type || "";
      this.size = parts.reduce((total, part) => total + Number(part?.size || 0), 0);
    }
  }

  class FakeRecorder {
    constructor(stream) {
      if (constructorFailuresLeft > 0) {
        constructorFailuresLeft -= 1;
        throw new Error("synthetic MediaRecorder constructor failure");
      }
      this.stream = stream;
      this.state = "inactive";
      this.mimeType = "audio/webm";
      recorders.push(this);
    }

    start() {
      if (startFailuresLeft > 0) {
        startFailuresLeft -= 1;
        throw new Error("synthetic MediaRecorder start failure");
      }
      this.state = "recording";
    }

    stop() {
      if (this.state !== "recording") return;
      this.state = "inactive";
      queueMicrotask(() => this.onstop?.());
    }

    emit(part) { this.ondataavailable?.({data: part}); }
  }

  const document = {
    activeElement: null,
    body: {
      appendChild(element) { overlays.push(element); },
    },
    createElement(tagName) { return new FakeElement(tagName, document); },
    getElementById(id) {
      return overlays.find(element => element.id === id && !element.removed) || null;
    },
    querySelectorAll(selector) {
      if (selector === ".chv-ovl") return overlays.filter(element => !element.removed);
      return [];
    },
  };

  const sandbox = {
    console,
    document,
    navigator: {
      mediaDevices: {
        async getUserMedia(options) {
          mediaCalls += 1;
          if (getUserMedia) return getUserMedia(options);
          return makeStream().stream;
        },
      },
    },
    MediaRecorder: FakeRecorder,
    Blob: FakeBlob,
    setInterval() { intervalId += 1; return intervalId; },
    clearInterval() {},
    setTimeout() { return 1; },
    moduleViewToken: 0,
    hasPerm: () => true,
    _chvWrite: async (kind, options) => {
      const state = options && options.state;
      if (state && state.writeInFlight) return {ok: false, status: 0, busy: true};
      const token = {};
      if (state) state.writeInFlight = token;
      writes.push({kind, options});
      try {
        if (writeImpl) return await writeImpl(kind, options, writes.length);
        return {ok: true, status: 200, response: {data: {context_revision: 2}}};
      } finally {
        if (state && state.writeInFlight === token) delete state.writeInFlight;
      }
    },
    _chvFetchJson: async url => {
      reads.push(url);
      if (readImpl) return readImpl(url, reads.length);
      return {revision: 2};
    },
    _chvWriteError: (result, prefix) => writeErrors.push({result, prefix}),
    openPatientWorkspace: (patientId, tab) => openedPatients.push({patientId, tab}),
  };
  sandbox.window = {
    MediaRecorder: FakeRecorder,
    alert: message => alerts.push(message),
    confirm: message => {
      confirmations.push(message);
      return confirmImpl ? confirmImpl(message) : true;
    },
  };
  vm.createContext(sandbox);
  vm.runInContext(
    `const _chvPageWriteState = {};\n${encodeHelperSource}\n${permissionHelperSource}\n${overlayPendingSource}\n${overlayWriteSource}\n${leaveSource}\n${micSource}\n${overlaySource}\n`
      + "this.__mic = _chvMicButton;"
      + "this.__overlay = _chvOvl;"
      + "this.__leave = requestLeaveCustomerHubV2;"
      + "this.__dispose = typeof _chvDisposeOverlay === 'function' ? _chvDisposeOverlay : null;"
      + "this.__leavePatient = typeof _chvLeavePatientOverlay === 'function' ? _chvLeavePatientOverlay : null;"
      + "this.__writeOverlay = _chvWriteInOverlay;",
    sandbox,
  );

  return {
    sandbox,
    writes,
    reads,
    writeErrors,
    alerts,
    openedPatients,
    confirmations,
    recorders,
    overlays,
    mediaCalls: () => mediaCalls,
  };
}

function makeStream(trackCount = 2) {
  const tracks = Array.from({length: trackCount}, () => ({
    stopCount: 0,
    stop() { this.stopCount += 1; },
  }));
  return {tracks, stream: {getTracks: () => tracks}};
}

function makeMic(harness, {existingAbort, revision = 1} = {}) {
  const overlay = harness.sandbox.__overlay(
    `media-overlay-${harness.overlays.length}`,
    '<button type="button" data-chv-close>关闭</button>',
  );
  if (existingAbort) overlay._chvAbort = existingAbort;
  const revisions = [];
  let currentRevision = revision;
  const button = harness.sandbox.__mic(
    "patient/synthetic",
    "rv/synthetic",
    () => currentRevision,
    value => {
      currentRevision = value;
      revisions.push(value);
    },
    overlay,
  );
  return {overlay, button, revisions};
}

test("程序化销毁录音弹窗立即停全部 tracks，异步 onstop 不上传，并组合既有 abort", async () => {
  const media = makeStream();
  let existingAborts = 0;
  const harness = makeHarness({getUserMedia: async () => media.stream});
  const {overlay, button} = makeMic(harness, {
    existingAbort: () => { existingAborts += 1; },
  });

  await button.dispatch("click");
  harness.recorders[0].emit({size: 4, marker: "partial"});
  harness.sandbox.__dispose(overlay);

  assert.equal(overlay.removed, true);
  assert.deepEqual(media.tracks.map(track => track.stopCount), [1, 1]);
  assert.equal(existingAborts, 1, "mic abort 必须组合而非覆盖已有 abort");
  await settle();
  assert.equal(harness.writes.length, 0, "取消后的异步 onstop 不得上传部分录音");
});

test("表单已干净的程序化关闭也会立即停止录音，迟到 onstop 不上传", async () => {
  const media = makeStream();
  const harness = makeHarness({getUserMedia: async () => media.stream});
  const {overlay, button} = makeMic(harness);

  overlay._chvClean = true;
  await button.dispatch("click");
  harness.recorders[0].emit({size: 4, marker: "saved-form-partial"});

  assert.equal(typeof harness.sandbox.__dispose, "function");
  harness.sandbox.__dispose(overlay);

  assert.equal(overlay.removed, true);
  assert.deepEqual(media.tracks.map(track => track.stopCount), [1, 1]);
  await settle();
  assert.equal(harness.writes.length, 0, "程序化关闭后的 onstop 不得上传部分录音");
  assert.doesNotMatch(rvSuccessCloseSource, /ovl\.remove\(\)/);
  assert.match(rvSuccessCloseSource, /_chvDisposeOverlay\(ovl\)/);
});

test("getUserMedia 待定时程序化销毁，返回后弃流且不启动 recorder", async () => {
  const permission = deferred();
  const media = makeStream();
  const harness = makeHarness({getUserMedia: () => permission.promise});
  const {overlay, button} = makeMic(harness);

  const starting = button.dispatch("click");
  harness.sandbox.__dispose(overlay);
  permission.resolve(media.stream);
  await starting;

  assert.equal(overlay.removed, true);
  assert.equal(harness.recorders.length, 0);
  assert.deepEqual(media.tracks.map(track => track.stopCount), [1, 1]);
  assert.equal(harness.writes.length, 0);
});

test("用户主动停止空录音不上传，下一次点击重新开麦", async () => {
  const harness = makeHarness();
  const {button} = makeMic(harness);

  await button.dispatch("click");
  await button.dispatch("click");
  await settle();
  assert.equal(harness.writes.length, 0);

  await button.dispatch("click");
  assert.equal(harness.mediaCalls(), 2);
});

for (const [label, failureOption] of [
  ["MediaRecorder 构造", "recorderConstructorFailures"],
  ["MediaRecorder.start", "recorderStartFailures"],
]) {
  test(`${label} 同步异常立即停 tracks 并可重新开麦`, async () => {
    const media = [makeStream(), makeStream()];
    let nextStream = 0;
    const harness = makeHarness({
      getUserMedia: async () => media[nextStream++].stream,
      [failureOption]: 1,
    });
    const {overlay, button} = makeMic(harness);

    await button.dispatch("click");

    assert.deepEqual(media[0].tracks.map(track => track.stopCount), [1, 1]);
    assert.equal(button.disabled, false);
    assert.equal(button.textContent, "🎙 录音");
    assert.deepEqual(harness.alerts, ["录音启动失败，请重试"]);
    assert.equal(harness.writes.length, 0);

    await button.dispatch("click");
    assert.equal(harness.mediaCalls(), 2);
    assert.equal(harness.recorders.at(-1).state, "recording");
    assert.deepEqual(media[1].tracks.map(track => track.stopCount), [0, 0]);

    harness.sandbox.__dispose(overlay);
    assert.deepEqual(media[1].tracks.map(track => track.stopCount), [1, 1]);
    await settle();
    assert.equal(harness.writes.length, 0);
  });
}

for (const failure of [400, 403, 500]) {
  test(`${failure} 后保留同一个 Blob，显式点击才重传且成功推进 revision`, async () => {
    const harness = makeHarness({
      writeImpl: async (_kind, _options, attempt) => {
        if (attempt === 1) {
          return {ok: false, status: failure};
        }
        return {ok: true, status: 200, response: {data: {context_revision: 7}}};
      },
    });
    const {overlay, button, revisions} = makeMic(harness);

    await button.dispatch("click");
    harness.recorders[0].emit({size: 6, marker: `blob-${failure}`});
    await button.dispatch("click");
    await settle();

    assert.equal(harness.writes.length, 1, "失败不能自动重试");
    const firstBlob = harness.writes[0].options.media;
    assert.equal(firstBlob.size, 6);
    assert.equal(button.disabled, false);
    assert.equal(overlay.querySelector("[data-chv-close]").disabled, false);

    await settle();
    assert.equal(harness.writes.length, 1, "等待也不能触发自动重试");
    await button.dispatch("click");
    await settle();

    assert.equal(harness.mediaCalls(), 1, "显式重试不得重新 getUserMedia");
    assert.equal(harness.writes.length, 2);
    assert.strictEqual(harness.writes[1].options.media, firstBlob);
    assert.deepEqual(revisions, [7]);
    assert.ok(harness.writes.every(call => call.kind === "call"));
  });
}

test("status 0 后隔离当前录音 Blob，重复点击也不得再写", async () => {
  const harness = makeHarness({
    writeImpl: async () => ({ok: false, status: 0, message: "网络异常"}),
  });
  const {overlay, button} = makeMic(harness);

  await button.dispatch("click");
  harness.recorders[0].emit({size: 6, marker: "quarantined-network-blob"});
  await button.dispatch("click");
  await settle();

  assert.equal(harness.writes.length, 1);
  const firstBlob = harness.writes[0].options.media;
  assert.equal(firstBlob.size, 6);
  assert.equal(overlay._chvMediaDirty, true, "隔离不得清除待确认 Blob");
  assert.match(harness.alerts.at(-1), /关闭.*重新打开.*确认/);

  await button.dispatch("click");
  await settle();

  assert.equal(harness.writes.length, 1, "网络结果不确定后不得重传同一 Blob");
  assert.strictEqual(harness.writes[0].options.media, firstBlob);
  assert.equal(harness.mediaCalls(), 1);
});

test("409 后自动只读刷新 revision，用户显式再点才用新版本重传", async () => {
  const harness = makeHarness({
    writeImpl: async (_kind, _options, attempt) => attempt === 1
      ? {ok: false, status: 409, response: {data: {detail: "synthetic conflict"}}}
      : {ok: true, status: 200, response: {data: {context_revision: 7}}},
    readImpl: async () => ({revision: 6}),
  });
  const {button, revisions} = makeMic(harness);

  await button.dispatch("click");
  harness.recorders[0].emit({size: 6, marker: "blob-409"});
  await button.dispatch("click");
  await settle();

  assert.equal(harness.writes.length, 1, "409 后不得自动重放写请求");
  assert.equal(harness.writes[0].options.expectedRevision, 1);
  assert.deepEqual(harness.reads, ["/api/return-visits/rv%2Fsynthetic"]);
  assert.deepEqual(revisions, [6]);
  const firstBlob = harness.writes[0].options.media;

  await button.dispatch("click");
  await settle();

  assert.equal(harness.writes.length, 2);
  assert.equal(harness.writes[1].options.expectedRevision, 6);
  assert.strictEqual(harness.writes[1].options.media, firstBlob);
  assert.equal(harness.reads.length, 1);
  assert.deepEqual(revisions, [6, 7]);
  assert.equal(harness.mediaCalls(), 1);
});

test("409 后 revision 刷新失败只重试读，读成功后仍需再点才写", async () => {
  const harness = makeHarness({
    writeImpl: async (_kind, _options, attempt) => attempt === 1
      ? {ok: false, status: 409}
      : {ok: true, status: 200, response: {data: {context_revision: 9}}},
    readImpl: async (_url, attempt) => {
      if (attempt <= 2) throw new Error("synthetic refresh failure");
      return {revision: 8};
    },
  });
  const {button, revisions} = makeMic(harness);

  await button.dispatch("click");
  harness.recorders[0].emit({size: 7, marker: "blob-refresh-failure"});
  await button.dispatch("click");
  await settle();

  assert.equal(harness.writes.length, 1);
  assert.equal(harness.reads.length, 1, "409 后应自动尝试一次只读刷新");
  assert.deepEqual(revisions, []);

  await button.dispatch("click");
  await settle();
  assert.equal(harness.reads.length, 2);
  assert.equal(harness.writes.length, 1, "刷新失败不得带 stale revision 发写");

  await button.dispatch("click");
  await settle();
  assert.equal(harness.reads.length, 3);
  assert.equal(harness.writes.length, 1, "刷新成功的这次点击也只读不写");
  assert.deepEqual(revisions, [8]);

  await button.dispatch("click");
  await settle();
  assert.equal(harness.writes.length, 2);
  assert.equal(harness.writes[1].options.expectedRevision, 8);
  assert.deepEqual(revisions, [8, 9]);
  assert.equal(harness.mediaCalls(), 1);
});

for (const [label, responseData] of [
  ["缺失", {}],
  ["同值", {context_revision: 1}],
  ["倒退", {context_revision: 0}],
  ["非安全整数", {context_revision: Number.MAX_SAFE_INTEGER + 1}],
]) {
  test(`200 context_revision ${label}时只读确认，新 revision 可确认落库`, async () => {
    const harness = makeHarness({
      writeImpl: async () => ({ok: true, status: 200, response: {data: responseData}}),
      readImpl: async () => ({revision: 2}),
    });
    const {overlay, button, revisions} = makeMic(harness);

    await button.dispatch("click");
    harness.recorders[0].emit({size: 8, marker: `ambiguous-${label}`});
    await button.dispatch("click");
    await settle();

    assert.equal(harness.writes.length, 1);
    assert.deepEqual(harness.reads, ["/api/return-visits/rv%2Fsynthetic"]);
    assert.deepEqual(revisions, [2]);
    assert.equal(overlay._chvMediaDirty, false);
    assert.match(button.textContent, /已保存/);
  });
}

test("200 revision 待确认时只重试读，绝不重传 Blob", async () => {
  const harness = makeHarness({
    writeImpl: async () => ({ok: true, status: 200, response: {data: {}}}),
    readImpl: async (_url, attempt) => {
      if (attempt === 1) throw new Error("synthetic confirmation read failure");
      if (attempt === 2) return {revision: 1};
      return {revision: 3};
    },
  });
  const {overlay, button, revisions} = makeMic(harness);

  await button.dispatch("click");
  harness.recorders[0].emit({size: 9, marker: "ambiguous-pending"});
  await button.dispatch("click");
  await settle();

  assert.equal(harness.writes.length, 1);
  assert.equal(harness.reads.length, 1);
  assert.equal(overlay._chvMediaDirty, true);
  assert.doesNotMatch(button.textContent, /已保存/);

  await button.dispatch("click");
  await settle();
  assert.equal(harness.reads.length, 2);
  assert.equal(harness.writes.length, 1, "未读到新 revision 时不得重传");
  assert.equal(overlay._chvMediaDirty, true);

  await button.dispatch("click");
  await settle();
  assert.equal(harness.reads.length, 3);
  assert.equal(harness.writes.length, 1, "确认读成功也不得重传");
  assert.deepEqual(revisions, [3]);
  assert.equal(overlay._chvMediaDirty, false);
  assert.match(button.textContent, /已保存/);
  assert.equal(harness.mediaCalls(), 1);
});

test("上传 pending 时冻结全部控件并阻止离开和双提交，settle 后精确恢复 disabled", async () => {
  const upload = deferred();
  const harness = makeHarness({writeImpl: () => upload.promise});
  const {overlay, button} = makeMic(harness);
  const originallyDisabledClose = new FakeElement("button");
  originallyDisabledClose.disabled = true;
  overlay._closeButtons.push(originallyDisabledClose);
  overlay._controls.push(originallyDisabledClose);
  const parentSave = new FakeElement("button");
  parentSave.dataset.chvOk = "1";
  const originallyDisabledInput = new FakeElement("input");
  originallyDisabledInput.disabled = true;
  overlay._controls.push(parentSave, originallyDisabledInput);

  await button.dispatch("click");
  harness.recorders[0].emit({size: 5});
  await button.dispatch("click");
  await settle();

  assert.equal(harness.writes.length, 1);
  assert.equal(button.disabled, true);
  assert.deepEqual(overlay._closeButtons.map(control => control.disabled), [true, true]);
  assert.equal(parentSave.disabled, true, "父回访保存不得与录音上传并发");
  assert.equal(originallyDisabledInput.disabled, true);
  assert.equal(await harness.sandbox.__leave(), false, "顶层离开必须 fail closed");
  await button.dispatch("click");
  assert.equal(harness.writes.length, 1, "pending 双击不能发第二个请求");

  upload.resolve({ok: false, status: 409});
  await settle();
  assert.equal(button.disabled, false);
  assert.deepEqual(overlay._closeButtons.map(control => control.disabled), [false, true]);
  assert.equal(parentSave.disabled, false);
  assert.equal(originallyDisabledInput.disabled, true, "原本 disabled 的控件不得被错误启用");
});

test("上传 pending 时背景和关闭按钮不能移除 overlay", async () => {
  const upload = deferred();
  const harness = makeHarness({writeImpl: () => upload.promise});
  const {overlay, button} = makeMic(harness);

  await button.dispatch("click");
  harness.recorders[0].emit({size: 5});
  await button.dispatch("click");
  await settle();

  await overlay.dispatch("click", {target: overlay});
  await overlay.querySelector("[data-chv-close]").dispatch("click");
  assert.equal(overlay.removed, false);

  upload.resolve({ok: false, status: 500});
  await settle();
});

test("患者姓名离开在 pending 时 fail closed，settle 后才 abort、移除并打开主页", async () => {
  const upload = deferred();
  const harness = makeHarness({writeImpl: () => upload.promise});
  let aborts = 0;
  const {overlay, button} = makeMic(harness, {
    existingAbort: () => { aborts += 1; },
  });

  assert.equal(typeof harness.sandbox.__leavePatient, "function");
  await button.dispatch("click");
  harness.recorders[0].emit({size: 5});
  await button.dispatch("click");
  await settle();

  assert.equal(harness.sandbox.__leavePatient(overlay, "patient/synthetic"), false);
  assert.equal(overlay.removed, false);
  assert.deepEqual(harness.openedPatients, []);
  assert.equal(aborts, 0);

  upload.resolve({ok: false, status: 400});
  await settle();
  assert.equal(harness.sandbox.__leavePatient(overlay, "patient/synthetic"), true);
  assert.equal(overlay.removed, true);
  assert.deepEqual(harness.openedPatients, [
    {patientId: "patient/synthetic", tab: "return-visits"},
  ]);
  assert.equal(aborts, 1);
});

test("失败 Blob 是独立 media-dirty，关闭需确认；成功后清除且不伪造表单 clean", async () => {
  let allowDiscard = false;
  const harness = makeHarness({
    confirmImpl: () => allowDiscard,
    writeImpl: async (_kind, _options, attempt) => attempt === 1
      ? {ok: false, status: 409}
      : {ok: true, status: 200, response: {data: {context_revision: 8}}},
  });
  const {overlay, button} = makeMic(harness);

  await button.dispatch("click");
  harness.recorders[0].emit({size: 5});
  await button.dispatch("click");
  await settle();

  assert.equal(overlay._chvMediaDirty, true);
  assert.notEqual(overlay._chvClean, true, "media dirty 不能伪装成表单 clean 状态");
  await overlay.dispatch("click", {target: overlay});
  assert.equal(overlay.removed, false);
  assert.equal(harness.confirmations.length, 1);

  await button.dispatch("click");
  await settle();
  assert.equal(overlay._chvMediaDirty, false);
  assert.notEqual(overlay._chvClean, true);
  await overlay.dispatch("click", {target: overlay});
  assert.equal(overlay.removed, true);
  assert.equal(harness.confirmations.length, 1, "成功清除 media dirty 后不应再弹放弃确认");

  allowDiscard = true;
});

test("getUserMedia starting 全程与普通写互斥，且背景不得关窗", async () => {
  const permission = deferred();
  const media = makeStream();
  const harness = makeHarness({getUserMedia: () => permission.promise});
  const {overlay, button} = makeMic(harness);
  const save = new FakeElement("button");
  overlay._controls.push(save);

  const starting = button.dispatch("click");
  await settle();
  await harness.sandbox.__writeOverlay(overlay, save, "returnVisit", {action: "create"});
  await overlay.dispatch("click", {target: overlay});
  const writesWhileStarting = harness.writes.length;
  const removedWhileStarting = overlay.removed;
  permission.resolve(media.stream);
  await starting;
  if (!overlay.removed) harness.sandbox.__dispose(overlay);

  assert.equal(writesWhileStarting, 0);
  assert.equal(removedWhileStarting, false);
});

test("recorder recording 全程与普通写互斥，且背景不得关窗", async () => {
  const harness = makeHarness();
  const {overlay, button} = makeMic(harness);
  const save = new FakeElement("button");
  overlay._controls.push(save);

  await button.dispatch("click");
  assert.equal(harness.recorders[0].state, "recording");
  await harness.sandbox.__writeOverlay(overlay, save, "returnVisit", {action: "create"});
  await overlay.dispatch("click", {target: overlay});
  const writesWhileRecording = harness.writes.length;
  const removedWhileRecording = overlay.removed;
  if (!overlay.removed) harness.sandbox.__dispose(overlay);
  await settle();

  assert.equal(writesWhileRecording, 0);
  assert.equal(removedWhileRecording, false);
});

test("普通写 pending 时 mic 点击不得启动 getUserMedia", async () => {
  const write = deferred();
  const harness = makeHarness({writeImpl: () => write.promise});
  const {overlay, button} = makeMic(harness);
  const save = new FakeElement("button");
  overlay._controls.push(save);

  const writing = harness.sandbox.__writeOverlay(
    overlay,
    save,
    "returnVisit",
    {action: "create"},
  );
  await settle();
  await button.dispatch("click");
  const mediaCallsWhileWriting = harness.mediaCalls();
  write.resolve({ok: false, status: 500});
  await writing;
  harness.sandbox.__dispose(overlay);

  assert.equal(mediaCallsWhileWriting, 0);
});

test("确认版录音仍只走 _chvWrite call，不直接 fetch 或恢复 R2B 壳", () => {
  assert.doesNotMatch(micSource, /\bfetch\s*\(/);
  assert.match(micSource, /_chvWrite\("call"/);
  assert.doesNotMatch(source, /customer-hub-v2__views|customer-hub-v2__tabs|data-chv2-tab/);
});
