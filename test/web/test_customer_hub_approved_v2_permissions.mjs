import test from "node:test";
import assert from "node:assert/strict";
import {readFileSync} from "node:fs";
import {dirname, join} from "node:path";
import {fileURLToPath} from "node:url";
import vm from "node:vm";
import {
  FakeElement,
  deferred,
  makeApprovedV2Harness,
  settle,
} from "./customer_hub_approved_v2_harness.mjs";

const here = dirname(fileURLToPath(import.meta.url));
const root = join(here, "..", "..");
const contractSource = readFileSync(join(root, "local_app", "static", "customer_hub_v2_contract.js"), "utf8");
const followupDateSource = readFileSync(join(root, "local_app", "static", "customer_hub_v2_followup_date.js"), "utf8");
const viewSource = readFileSync(join(root, "local_app", "static", "customer_hub_v2.js"), "utf8");
const indexHtml = readFileSync(join(root, "local_app", "static", "index.html"), "utf8");

function response(status, data = {}) {
  return {ok: status >= 200 && status < 300, status, async json() { return data; }};
}

function makeHarness({permissions = [], route} = {}) {
  const calls = [];
  const alerts = [];
  const overlays = [];
  const permissionSet = new Set(permissions);
  let modulePanel;
  const document = {
    body: {
      appendChild(element) { overlays.push(element); },
      contains(element) { return Boolean(element && !element.removed); },
    },
    createElement(tagName) { return new FakeElement(tagName, document); },
    getElementById(id) {
      if (modulePanel?.id === id) return modulePanel;
      const panelChild = modulePanel?.querySelector(`#${id}`);
      if (panelChild) return panelChild;
      for (const overlay of overlays.filter(item => !item.removed)) {
        if (overlay.id === id) return overlay;
        const child = overlay.querySelector(`#${id}`);
        if (child) return child;
      }
      return null;
    },
    querySelectorAll(selector) {
      if (selector === ".chv-ovl") return overlays.filter(item => !item.removed);
      return [];
    },
    addEventListener() {},
  };
  modulePanel = new FakeElement("main", document);
  modulePanel.id = "modulePanel";
  const fakeWindow = {
    alert(message) { alerts.push(String(message)); },
    confirm() { return true; },
    open() {},
  };
  const sandbox = {
    console,
    document,
    window: fakeWindow,
    navigator: {mediaDevices: null},
    modulePanel,
    moduleViewToken: 0,
    workDate: {value: "2026-07-21"},
    localDateValue: () => "2026-07-21",
    bjToday: () => new Date("2026-07-21T00:00:00"),
    escapeHtml: value => String(value ?? ""),
    escapeAttr: value => String(value ?? ""),
    hasPerm: permission => permissionSet.has(permission),
    openPatientWorkspace() {},
    openNewAppointment() {},
    MutationObserver: class { observe() {} },
    Audio: class { play() { return Promise.resolve(); } },
    FormData: class {},
    Blob: class {},
    File: class {},
    setTimeout,
    clearTimeout,
    setInterval,
    clearInterval,
  };
  fakeWindow.window = fakeWindow;
  fakeWindow.document = document;
  sandbox.fetch = async (url, init = {}) => {
    const request = {url: String(url), init: {...init}};
    calls.push(request);
    const routed = route ? await route(request, calls.length) : undefined;
    if (routed) return response(
      routed.status ?? 200,
      Object.prototype.hasOwnProperty.call(routed, "data") ? routed.data : {},
    );
    if (request.url === "/api/settings/customer-hub-dicts") {
      return response(200, {return_type: [], visit_content: [], visit_result: [], comm_reason: []});
    }
    if (request.url.startsWith("/api/return-visits/") && !request.url.includes("?")) {
      return response(200, {
        revision: 1,
        patient_identity: "patient/synthetic",
        display_name: "Synthetic Patient",
        due_time: "2026-07-21 10:00",
        item_name: "Synthetic follow-up",
        channel: "phone",
        visitor: "Synthetic Visitor",
        status: "待回访",
        note: "Synthetic note",
        return_result: "",
        images: [],
      });
    }
    if (request.url.startsWith("/api/patients/")) return response(200, {patient: {display_name: "Synthetic Patient"}});
    return response(200, {});
  };

  vm.createContext(sandbox);
  vm.runInContext(contractSource, sandbox);
  fakeWindow.CustomerHubV2Contract = sandbox.CustomerHubV2Contract;
  vm.runInContext(followupDateSource, sandbox);
  fakeWindow.CustomerHubV2FollowupDate = sandbox.CustomerHubV2FollowupDate;
  vm.runInContext(`${viewSource}\n    globalThis.__permissionsApi = {
      load: loadCustomerHubV2,
      tabs: _chvTabsHtml,
      name: _chvName,
      renderToday: _chvRenderToday,
      renderList: _chvRenderList,
      renderComm: _chvRenderComm,
      renderPane: _chvRenderPane,
      playRecording: _chvPlayRecording,
      openCalendar: _chvOpenCalendar,
      openMonthlyReview: _chvOpenMonthlyReview,
      openAddComm: _chvOpenAddComm,
      openNewRv: _chvOpenNewRv,
      loadPlans: _chvLoadReturnPlans,
      openRv: _chvOpenRvModal,
      maybeFillTranscript: _chvMaybeFillTranscript,
      openDict: _chvOpenDict,
      dictsGet: _chvDictsGet,
      bindBatchDelete: _chvBindBatchDelete,
      dispose: _chvDisposeOverlay,
      resetPlans() { _chvPlans = null; },
      setEntryState({filters = {}, page = 1, pending = false, view = "list", day = ""} = {}) {
        _chvF = {..._chvEmptyFilter(), ...filters};
        _chvListPage = page;
        _chvPendingChip = pending;
        _chvView = view;
        _chvDay = day;
      },
    };`, sandbox);

  return {
    api: sandbox.__permissionsApi,
    sandbox,
    modulePanel,
    calls,
    alerts,
    overlays,
    setPermissions(values) {
      permissionSet.clear();
      for (const value of values) permissionSet.add(value);
    },
    body() {
      modulePanel.innerHTML = '<div id="chvBody"></div>';
      return document.getElementById("chvBody");
    },
  };
}

test("index 只为客户通增加 patient.profile.view，不改今日工作入口", () => {
  const button = view => indexHtml.match(new RegExp(`<button[^>]*data-workspace-view="${view}"[^>]*>`))?.[0] || "";
  assert.doesNotMatch(button("today"), /data-perm="patient\.profile\.view"/);
  assert.match(button("return-visits"), /data-perm="patient\.profile\.view"/);
  assert.doesNotMatch(button("patients"), /data-perm="patient\.profile\.view"/);
  assert.doesNotMatch(button("appointments"), /data-perm="patient\.profile\.view"/);
});

test("loadCustomerHubV2 无 profile 时不渲染且 0 请求", async () => {
  const harness = makeHarness();
  harness.modulePanel.innerHTML = "sentinel";

  assert.equal(await harness.api.load(), false);
  assert.equal(harness.calls.length, 0);
  assert.equal(harness.modulePanel.innerHTML, "sentinel");
});

test("业务日期决定月历首次请求的月份", async () => {
  const harness = makeHarness({
    permissions: ["patient.profile.view", "return_visit.view"],
    route: ({url}) => url.startsWith("/api/return-visits/calendar?")
      ? {data: {days: []}}
      : undefined,
  });
  harness.sandbox.workDate.value = "2026-08-31";

  await harness.api.openCalendar();

  assert.deepEqual(
    harness.calls.filter(({url}) => url.startsWith("/api/return-visits/calendar?"))
      .map(({url}) => url),
    ["/api/return-visits/calendar?month=2026-08"],
  );
});

test("pending_due 首次入口先清旧状态，再带搜索和截至工作日期直达回访列表", async () => {
  const harness = makeHarness({
    permissions: ["patient.profile.view", "return_visit.view"],
    route: ({url}) => {
      if (url.startsWith("/api/return-visits?")) return {data: {list: [], totalcount: 0}};
      if (url.startsWith("/api/staff-members")) return {data: {members: []}};
      return undefined;
    },
  });

  const context = {
    returnFilter: "pending_due",
    searchText: "  patient-synthetic  ",
    selectedDate: "2026-07-18",
  };
  assert.equal(await harness.api.load(context), true);

  assert.equal(
    harness.calls[0].url,
    "/api/return-visits?date_to=2026-07-18&q=patient-synthetic&pageno=1&pagesize=50",
  );
  assert.equal(harness.calls.some(({url}) => url.startsWith("/api/customer-hub/today")), false);
  assert.match(harness.modulePanel.querySelector("#chvBody").innerHTML, /回访列表/);
  assert.match(harness.modulePanel.querySelector("#chvBody").innerHTML, /待回访（截至 2026-07-18）/);
  assert.equal(harness.modulePanel.querySelector('[data-chv-f="q"]').value, "patient-synthetic");
  assert.equal(harness.modulePanel.querySelector('[data-chv-view="list"]').classList.contains("on"), true);

  await harness.api.load();
  assert.deepEqual(
    harness.calls.filter(({url}) => url.startsWith("/api/return-visits?")).map(({url}) => url),
    [harness.calls[0].url, harness.calls[0].url],
  );
});

test("pending_due 提示与实际 date_to 同源，覆盖默认今日、历史和未来截止日期", async t => {
  for (const {label, context, expectedUrl, expectedChip} of [
    {
      label: "无显式截止日期",
      context: {returnFilter: "pending_due"},
      expectedUrl: "/api/return-visits?pageno=1&pagesize=50",
      expectedChip: "待回访（截至今日）",
    },
    {
      label: "历史工作日期",
      context: {returnFilter: "pending_due", selectedDate: "2026-07-18"},
      expectedUrl: "/api/return-visits?date_to=2026-07-18&pageno=1&pagesize=50",
      expectedChip: "待回访（截至 2026-07-18）",
    },
    {
      label: "未来工作日期",
      context: {returnFilter: "pending_due", selectedDate: "2026-07-25"},
      expectedUrl: "/api/return-visits?date_to=2026-07-25&pageno=1&pagesize=50",
      expectedChip: "待回访（截至 2026-07-25）",
    },
  ]) await t.test(label, async () => {
    const harness = makeHarness({
      permissions: ["patient.profile.view", "return_visit.view"],
      route: ({url}) => {
        if (url.startsWith("/api/return-visits?")) return {data: {list: [], totalcount: 0}};
        if (url.startsWith("/api/staff-members")) return {data: {members: []}};
        return undefined;
      },
    });

    assert.equal(await harness.api.load(context), true);
    assert.equal(harness.calls[0].url, expectedUrl);
    assert.match(harness.modulePanel.querySelector("#chvBody").innerHTML, new RegExp(expectedChip));
  });
});

test("纯外部 searchText 入口清空旧筛选、pending 状态和页码，只保留普通 q", async () => {
  const harness = makeHarness({
    permissions: ["patient.profile.view", "return_visit.view"],
    route: ({url}) => {
      if (url.startsWith("/api/return-visits?")) return {data: {list: [], totalcount: 0}};
      if (url.startsWith("/api/staff-members")) return {data: {members: []}};
      return undefined;
    },
  });
  harness.api.setEntryState({
    filters: {
      date_from: "2026-06-01",
      date_to: "2026-06-30",
      status: "已回访",
      doctor: "old-doctor",
      visitor: "old-visitor",
      channel: "phone",
      rtype: "old-type",
      att: "recording",
      item: "old-item",
      patient_q: "old-patient",
      content_q: "old-content",
      q: "old-q",
    },
    page: 8,
    pending: true,
  });

  assert.equal(await harness.api.load({searchText: "  patient-fresh  "}), true);

  const expectedUrl = "/api/return-visits?q=patient-fresh&pageno=1&pagesize=50";
  assert.equal(harness.calls[0].url, expectedUrl);
  assert.equal(harness.modulePanel.querySelector('[data-chv-f="q"]').value, "patient-fresh");
  assert.doesNotMatch(harness.modulePanel.querySelector("#chvBody").innerHTML, /待回访（截至今日）/);

  await harness.api.load();
  assert.deepEqual(
    harness.calls.filter(({url}) => url.startsWith("/api/return-visits?")).map(({url}) => url),
    [expectedUrl, expectedUrl],
  );
});

test("有效 selectedDate 同步总览日期，内部无 context 刷新保留已选日期", async () => {
  const harness = makeHarness({
    permissions: ["patient.profile.view"],
    route: ({url}) => url.startsWith("/api/customer-hub/today?") ? {data: {}} : undefined,
  });

  await harness.api.load({selectedDate: "2026-07-18"});
  await harness.api.load();

  assert.deepEqual(
    harness.calls.map(({url}) => url),
    [
      "/api/customer-hub/today?date=2026-07-18",
      "/api/customer-hub/today?date=2026-07-18",
    ],
  );
});

test("显式非空非法 selectedDate 明确载入失败且零请求", async t => {
  for (const selectedDate of ["0000-01-01", "2026-02-30", "2026-7-1", "not-a-date"]) {
    await t.test(selectedDate, async () => {
      const harness = makeHarness({
        permissions: ["patient.profile.view"],
        route: ({url}) => url.startsWith("/api/customer-hub/today?") ? {data: {}} : undefined,
      });

      assert.equal(await harness.api.load({selectedDate}), false);
      assert.equal(harness.calls.length, 0);
      assert.match(harness.modulePanel.innerHTML, /客户通载入失败/);
    });
  }
});

test("公开入口 context descriptor trap 必须整体 fail-closed，不沿用旧状态", async () => {
  const harness = makeHarness({
    permissions: ["patient.profile.view", "return_visit.view"],
    route: ({url}) => {
      if (url.startsWith("/api/return-visits?")) return {data: {list: [], totalcount: 0}};
      if (url.startsWith("/api/staff-members")) return {data: {members: []}};
      return undefined;
    },
  });
  harness.api.setEntryState({
    filters: {
      date_to: "2026-06-30",
      status: "已回访",
      doctor: "old-doctor",
      q: "old-q",
    },
    page: 9,
    pending: true,
    day: "2026-06-30",
  });
  const proxyContext = new Proxy({}, {
    getOwnPropertyDescriptor() { throw new Error("descriptor_trap_failed"); },
  });

  assert.equal(await harness.api.load(proxyContext), false);
  assert.equal(harness.calls.length, 0);
  assert.match(harness.modulePanel.innerHTML, /客户通载入失败/);
});

test("入口 context 忽略继承和访问器字段，不触发访问器或原型污染", async () => {
  const harness = makeHarness({
    permissions: ["patient.profile.view"],
    route: ({url}) => url.startsWith("/api/customer-hub/today?") ? {data: {}} : undefined,
  });
  const context = Object.create({searchText: "inherited", selectedDate: "2099-01-01"});
  let getterCalls = 0;
  Object.defineProperty(context, "returnFilter", {
    enumerable: true,
    get() {
      getterCalls += 1;
      throw new Error("accessor_must_not_run");
    },
  });
  Object.defineProperty(context, "__proto__", {
    enumerable: true,
    value: {polluted: true},
  });

  await assert.doesNotReject(() => harness.api.load(context));

  assert.equal(harness.calls[0].url, "/api/customer-hub/today?date=2026-07-21");
  assert.equal(getterCalls, 0);
  assert.equal({}.polluted, undefined);
});

test("回访与沟通主列表的畸形 JSON envelope 显示载入失败，不伪装空态", async () => {
  const listHarness = makeHarness({
    permissions: ["patient.profile.view", "return_visit.view"],
    route: ({url}) => {
      if (url.startsWith("/api/return-visits?")) return {data: {}};
      if (url.startsWith("/api/staff-members")) return {data: {members: []}};
      return undefined;
    },
  });
  const listBody = listHarness.body();
  assert.equal(await listHarness.api.renderList(0), false);
  assert.match(listBody.innerHTML, /回访列表载入失败/);
  assert.doesNotMatch(listBody.innerHTML, /无符合条件的回访/);

  const commHarness = makeHarness({
    permissions: ["patient.profile.view", "communication.view"],
    route: ({url}) => url.startsWith("/api/communications?") ? {data: {}} : undefined,
  });
  const commBody = commHarness.body();
  assert.equal(await commHarness.api.renderComm(0), false);
  assert.match(commBody.innerHTML, /沟通记录载入失败/);
  assert.doesNotMatch(commBody.innerHTML, /无沟通记录/);

  const emptyCommHarness = makeHarness({
    permissions: ["patient.profile.view", "communication.view"],
    route: ({url}) => url.startsWith("/api/communications?")
      ? {data: {list: [], totalcount: 0}} : undefined,
  });
  const emptyCommBody = emptyCommHarness.body();
  await emptyCommHarness.api.renderComm(0);
  assert.match(emptyCommBody.innerHTML, /无沟通记录/);
  assert.match(emptyCommBody.innerHTML, />0 条</);
});

test("请求型内部入口必须在函数内精确门禁，无权直调 0 请求 0 overlay", async t => {
  const cases = [
    ["renderToday 缺 profile", [], api => api.renderToday(0)],
    ["renderList 缺 return view", ["patient.profile.view"], api => api.renderList(0)],
    ["renderList 缺 profile", ["return_visit.view"], api => api.renderList(0)],
    ["renderComm 缺 communication view", ["patient.profile.view"], api => api.renderComm(0)],
    ["renderComm 缺 profile", ["communication.view"], api => api.renderComm(0)],
    ["playRecording 缺 call view", ["patient.profile.view"], api => api.playRecording("rv/synthetic", "patient/synthetic")],
    ["playRecording 缺 profile", ["call_record.view"], api => api.playRecording("rv/synthetic", "patient/synthetic")],
    ["openCalendar 缺 return view", ["patient.profile.view"], api => api.openCalendar()],
    ["openCalendar 缺 profile", ["return_visit.view"], api => api.openCalendar()],
    ["openMonthlyReview 缺 return view", ["patient.profile.view"], api => api.openMonthlyReview("2026-07")],
    ["openMonthlyReview 缺 profile", ["return_visit.view"], api => api.openMonthlyReview("2026-07")],
    ["openNewRv 缺 return manage", ["patient.profile.view"], api => api.openNewRv()],
    ["openNewRv 缺 profile", ["return_visit.manage"], api => api.openNewRv()],
    ["openRv 缺 return view", ["patient.profile.view"], api => api.openRv("rv/synthetic", "patient/synthetic")],
    ["openRv 缺 profile", ["return_visit.view"], api => api.openRv("rv/synthetic", "patient/synthetic")],
    ["transcript 缺 call view", ["patient.profile.view"], api => api.maybeFillTranscript(new FakeElement("div"), "rv/synthetic", "patient/synthetic")],
    ["transcript 缺 profile", ["call_record.view"], api => api.maybeFillTranscript(new FakeElement("div"), "rv/synthetic", "patient/synthetic")],
    ["comm dict 缺 communication manage", ["patient.profile.view"], api => api.openDict("comm_reason")],
    ["comm dict 缺 profile", ["communication.manage"], api => api.openDict("comm_reason")],
    ["return dict 缺 return manage", ["patient.profile.view"], api => api.openDict("visit_content")],
    ["return dict 缺 profile", ["return_visit.manage"], api => api.openDict("visit_content")],
    ["unknown dict name", ["patient.profile.view", "return_visit.manage", "communication.manage"], api => api.openDict("unknown")],
  ];
  for (const [label, permissions, invoke] of cases) {
    await t.test(label, async () => {
      const harness = makeHarness({permissions});
      assert.equal(await invoke(harness.api), false);
      assert.equal(harness.calls.length, 0);
      assert.equal(harness.overlays.length, 0);
    });
  }
});

test("_chvDictsGet 必须传明确 purpose 且同时具备 profile 和相应权限", async t => {
  for (const [label, permissions, purpose] of [
    ["缺 purpose", ["patient.profile.view", "return_visit.view"], undefined],
    ["缺 profile", ["communication.manage"], "communication.manage"],
    ["缺 communication manage", ["patient.profile.view"], "communication.manage"],
    ["不允许的 purpose", ["patient.profile.view", "master_data.view"], "master_data.view"],
  ]) {
    await t.test(label, async () => {
      const harness = makeHarness({permissions});
      await assert.rejects(() => harness.api.dictsGet(purpose));
      assert.equal(harness.calls.length, 0);
      assert.equal(harness.overlays.length, 0);
    });
  }
});

test("写按钮绑定后权限被收回也必须二次 fail closed", async t => {
  await t.test("批量删除不发写请求", async () => {
    const harness = makeHarness({permissions: [
      "patient.profile.view", "return_visit.view", "return_visit.delete",
    ]});
    const body = new FakeElement("div");
    body.innerHTML = `
      <input type="checkbox" class="chv-ck" value="rv/synthetic" data-chv-revision="1" checked>
      <button type="button" data-chv-batchdel="1">批量删除</button>`;
    harness.api.bindBatchDelete(body);
    harness.setPermissions(["patient.profile.view", "return_visit.view"]);

    await body.querySelector("[data-chv-batchdel]").dispatch("click");
    assert.equal(harness.calls.length, 0);
  });

  await t.test("麦克风按钮不启动 getUserMedia", async () => {
    const harness = makeApprovedV2Harness({
      permissions: [
        "patient.profile.view", "return_visit.view", "return_visit.manage",
        "call_record.manage", "master_data.view",
      ],
      getUserMedia: async () => ({getTracks: () => []}),
    });
    const overlay = await harness.openRv();
    const mic = overlay.querySelector("[data-chv-mic]");
    harness.setPermissions(["patient.profile.view", "return_visit.view", "return_visit.manage"]);

    try {
      await mic.dispatch("click");
      assert.equal(harness.mediaCalls(), 0);
    } finally {
      harness.sandbox.__dispose(overlay);
    }
  });
});

test("profile + communication.view 同时具备才显示沟通入口，姓名跳转也需 profile", () => {
  const neither = makeHarness();
  const communicationOnly = makeHarness({permissions: ["communication.view"]});
  const profileOnly = makeHarness({permissions: ["patient.profile.view"]});
  const both = makeHarness({permissions: ["patient.profile.view", "communication.view"]});

  assert.doesNotMatch(neither.api.tabs(), /data-chv-view="comm"/);
  assert.doesNotMatch(communicationOnly.api.tabs(), /data-chv-view="comm"/);
  assert.doesNotMatch(profileOnly.api.tabs(), /data-chv-view="comm"/);
  assert.match(both.api.tabs(), /data-chv-view="comm"/);
  assert.equal(communicationOnly.api.name("patient/synthetic", "Synthetic Patient"), "Synthetic Patient");
  assert.doesNotMatch(communicationOnly.api.name("patient/synthetic", "Synthetic Patient"), /data-chv-home/);
  assert.match(profileOnly.api.name("patient/synthetic", "Synthetic Patient"), /data-chv-home="patient\/synthetic"/);
});

test("记沟通按钮和直接调用都需 profile + communication.manage", async () => {
  const denied = makeHarness({
    permissions: ["communication.manage"],
    route: ({url}) => url.startsWith("/api/customer-hub/today")
      ? {data: {communications: []}} : undefined,
  });
  const deniedBody = denied.body();
  await denied.api.renderToday(0);
  assert.doesNotMatch(deniedBody.innerHTML, /data-chv-addcomm/);
  const before = denied.calls.length;
  assert.equal(await denied.api.openAddComm("patient/synthetic"), false);
  assert.equal(denied.calls.length, before, "直调不得读字典或患者数据");

  const allowed = makeHarness({
    permissions: ["patient.profile.view", "communication.manage"],
    route: ({url}) => url.startsWith("/api/customer-hub/today")
      ? {data: {communications: []}} : undefined,
  });
  const allowedBody = allowed.body();
  await allowed.api.renderToday(0);
  assert.match(allowedBody.innerHTML, /data-chv-addcomm/);
});

test("无 return_visit.view 不渲染月度回顾按钮", async () => {
  const harness = makeHarness({
    permissions: ["patient.profile.view"],
    route: ({url}) => url.startsWith("/api/customer-hub/today") ? {data: {}} : undefined,
  });
  const body = harness.body();
  await harness.api.renderToday(0);
  assert.doesNotMatch(body.innerHTML, /data-chv-mr/);
});

test("五个患者 pane 直接调用无权都明确不可用且 0 请求", async t => {
  const cases = ["info", "emr", "hist", "img", "comm"];
  for (const paneName of cases) {
    await t.test(paneName, async () => {
      const harness = makeHarness();
      const overlay = new FakeElement("div");
      overlay.innerHTML = '<div id="chvPane"></div>';
      const result = await harness.api.renderPane(overlay, paneName, "patient/synthetic", "rv/synthetic");
      assert.equal(result, false);
      assert.equal(harness.calls.length, 0);
      assert.match(overlay.querySelector("#chvPane").innerHTML, /无权限|不可用/);
    });
  }
});

test("病历 pane 区分真实空、有正式病历无条目、合同畸形和请求失败", async t => {
  const cases = [
    {
      name: "真实空",
      response: {data: {medical_records: [], records: {}, totalcount: 0}},
      expected: /无病历记录/,
      forbidden: /病历载入失败/,
    },
    {
      name: "正式病历无结构化条目",
      response: {data: {
        medical_records: [{
          record_id: "record-synthetic",
          record_type: "初诊",
          visit_time: "2026-07-21 09:00:00",
          doctor_name: "合成医生",
          created_at: null,
          updated_at: "2026-07-21 10:00:00",
        }],
        records: {"record-synthetic": []},
        totalcount: 0,
      }},
      expected: /2026-07-21[\s\S]*初诊[\s\S]*合成医生[\s\S]*暂无结构化明细/,
      forbidden: /无病历记录|病历载入失败/,
    },
    {
      name: "合同畸形",
      response: {data: {records: {}}},
      expected: /病历载入失败，请重试/,
      forbidden: /无病历记录/,
    },
    {
      name: "请求失败",
      response: {status: 500, data: {detail: "synthetic"}},
      expected: /病历载入失败，请重试/,
      forbidden: /无病历记录/,
    },
  ];

  for (const current of cases) {
    await t.test(current.name, async () => {
      const harness = makeHarness({
        permissions: ["medical_record.view"],
        route: ({url}) => url.endsWith("/record-items") ? current.response : undefined,
      });
      const overlay = new FakeElement("div");
      overlay.innerHTML = '<div id="chvPane"></div>';

      await harness.api.renderPane(
        overlay, "emr", "patient/synthetic", "rv/synthetic"
      );

      const html = overlay.querySelector("#chvPane").innerHTML;
      assert.match(html, current.expected);
      assert.doesNotMatch(html, current.forbidden);
    });
  }
});

test("pane 旧 tab 迟到 reject 不得覆盖新 tab", async () => {
  const oldRequest = deferred();
  const harness = makeHarness({
    permissions: ["patient.profile.view", "medical_record.view"],
    route: ({url}) => {
      if (url === "/api/patients/patient%2Fsynthetic") return oldRequest.promise;
      if (url.endsWith("/record-items")) {
        return {data: {medical_records: [], records: {}, totalcount: 0}};
      }
      return undefined;
    },
  });
  const overlay = new FakeElement("div");
  overlay.innerHTML = '<div id="chvPane"></div>';
  const oldRender = harness.api.renderPane(overlay, "info", "patient/synthetic", "rv/synthetic");
  await settle();
  await harness.api.renderPane(overlay, "emr", "patient/synthetic", "rv/synthetic");
  const pane = overlay.querySelector("#chvPane");
  assert.match(pane.innerHTML, /无病历记录/);

  oldRequest.reject(new Error("synthetic late rejection"));
  await oldRender;
  assert.match(pane.innerHTML, /无病历记录/);
  assert.doesNotMatch(pane.innerHTML, /载入失败/);
});

test("pane 旧 tab 迟到 resolve 不得覆盖新 tab", async () => {
  const oldRequest = deferred();
  const harness = makeHarness({
    permissions: ["patient.profile.view", "medical_record.view"],
    route: ({url}) => {
      if (url === "/api/patients/patient%2Fsynthetic") return oldRequest.promise;
      if (url.endsWith("/record-items")) {
        return {data: {medical_records: [], records: {}, totalcount: 0}};
      }
      return undefined;
    },
  });
  const overlay = new FakeElement("div");
  overlay.innerHTML = '<div id="chvPane"></div>';
  const oldRender = harness.api.renderPane(overlay, "info", "patient/synthetic", "rv/synthetic");
  await settle();
  await harness.api.renderPane(overlay, "emr", "patient/synthetic", "rv/synthetic");
  const pane = overlay.querySelector("#chvPane");

  oldRequest.resolve({data: {patient: {display_name: "Late Synthetic Patient"}}});
  await oldRender;
  assert.match(pane.innerHTML, /无病历记录/);
  assert.doesNotMatch(pane.innerHTML, /Late Synthetic Patient/);
});

test("pane overlay 关闭后迟到 reject 不得再写 DOM", async () => {
  const lateRequest = deferred();
  const harness = makeHarness({
    permissions: ["patient.profile.view"],
    route: ({url}) => url === "/api/patients/patient%2Fsynthetic"
      ? lateRequest.promise : undefined,
  });
  const overlay = new FakeElement("div");
  overlay.innerHTML = '<div id="chvPane"></div>';
  const rendering = harness.api.renderPane(overlay, "info", "patient/synthetic", "rv/synthetic");
  await settle();
  const pane = overlay.querySelector("#chvPane");
  overlay.remove();
  pane.innerHTML = "closed sentinel";

  lateRequest.reject(new Error("synthetic rejection after close"));
  await rendering;
  assert.equal(pane.innerHTML, "closed sentinel");
});

test("ReturnPlan 首次失败不缓存假空，关闭重开会第二次请求并恢复", async () => {
  let planAttempts = 0;
  const harness = makeHarness({
    permissions: [
      "patient.profile.view", "return_visit.view", "return_visit.manage", "master_data.view",
    ],
    route: ({url}) => {
      if (url === "/api/dictionaries?type=ReturnPlan") {
        planAttempts += 1;
        return planAttempts === 1
          ? {status: 500, data: {detail: "synthetic failure"}}
          : {data: {items: [{name: "Synthetic plan", value: "3"}]}};
      }
      return undefined;
    },
  });

  await harness.api.openRv("rv/synthetic", "patient/synthetic");
  await settle();
  const first = harness.overlays.find(item => item.id === "chvRvOvl" && !item.removed);
  assert.match(first.querySelector("[data-chv-plan]").innerHTML, /制度载入失败，请关闭后重试/);
  harness.api.dispose(first);

  await harness.api.openRv("rv/synthetic", "patient/synthetic");
  await settle();
  const second = [...harness.overlays].reverse().find(item => item.id === "chvRvOvl" && !item.removed);
  assert.equal(planAttempts, 2);
  assert.match(second.querySelector("[data-chv-plan]").innerHTML, /Synthetic plan/);
  assert.doesNotMatch(second.querySelector("[data-chv-plan]").innerHTML, /制度载入失败/);
});

test("ReturnPlan 200 响应合同畸形必须抛错且不缓存，第二次会重请求", async t => {
  for (const [label, malformed] of [
    ["payload null", null],
    ["缺失 items", {}],
    ["items 非数组", {items: {}}],
  ]) {
    await t.test(label, async () => {
      let attempts = 0;
      const harness = makeHarness({
        permissions: ["master_data.view"],
        route: ({url}) => {
          if (url !== "/api/dictionaries?type=ReturnPlan") return undefined;
          attempts += 1;
          return attempts === 1
            ? {data: malformed}
            : {data: {items: [{name: "Synthetic retry plan", value: "5"}]}};
        },
      });

      await assert.rejects(() => harness.api.loadPlans("Synthetic follow-up"));
      assert.equal(attempts, 1);
      const plans = await harness.api.loadPlans("Synthetic follow-up");
      assert.equal(attempts, 2);
      assert.equal(plans.length, 1);
      assert.equal(plans[0].name, "Synthetic retry plan");
    });
  }
});

test("ReturnPlan item 必须全部合法才能缓存，任一畸形都可下次重请求", async t => {
  const valid = {name: "Synthetic valid plan", value: "5"};
  for (const [label, malformedItems] of [
    ["空对象", [{}]],
    ["混合畸形", [valid, {name: "Broken plan", value: "bad"}]],
    ["parseInt 污染字符串", [{name: "Broken plan", value: "3x"}]],
    ["空名称", [{name: "   ", value: "3"}]],
    ["非安全整数", [{name: "Broken plan", value: String(Number.MAX_SAFE_INTEGER + 1)}]],
  ]) {
    await t.test(label, async () => {
      let attempts = 0;
      const harness = makeHarness({
        permissions: ["master_data.view"],
        route: ({url}) => {
          if (url !== "/api/dictionaries?type=ReturnPlan") return undefined;
          attempts += 1;
          return attempts === 1
            ? {data: {items: malformedItems}}
            : {data: {items: [valid]}};
        },
      });

      await assert.rejects(() => harness.api.loadPlans("Synthetic follow-up"));
      assert.equal(attempts, 1);
      const plans = await harness.api.loadPlans("Synthetic follow-up");
      assert.equal(attempts, 2);
      assert.equal(plans.length, 1);
      assert.equal(plans[0].days, 5);
    });
  }
});

test("ReturnPlan 合法 items 空数组是真实空结果并只请求一次", async () => {
  let attempts = 0;
  const harness = makeHarness({
    permissions: ["master_data.view"],
    route: ({url}) => {
      if (url !== "/api/dictionaries?type=ReturnPlan") return undefined;
      attempts += 1;
      return {data: {items: []}};
    },
  });

  assert.equal((await harness.api.loadPlans("Synthetic follow-up")).length, 0);
  assert.equal((await harness.api.loadPlans("Synthetic follow-up")).length, 0);
  assert.equal(attempts, 1);
});

test("无 master_data.view 时处理弹窗无 plan 选择且直调 0 ReturnPlan 请求", async () => {
  const harness = makeHarness({permissions: [
    "patient.profile.view", "return_visit.view", "return_visit.manage",
  ]});
  await harness.api.openRv("rv/synthetic", "patient/synthetic");
  await settle();
  const overlay = harness.overlays.find(item => item.id === "chvRvOvl" && !item.removed);
  assert.equal(overlay.querySelector("[data-chv-plan]"), null);
  assert.equal(overlay.querySelectorAll("[data-chv-next]").length, 4);
  const before = harness.calls.length;
  await assert.rejects(() => harness.api.loadPlans("Synthetic follow-up"), /无权限|不可用/);
  assert.equal(harness.calls.length, before);
  assert.equal(harness.calls.filter(call => call.url === "/api/dictionaries?type=ReturnPlan").length, 0);
});

test("只读回访弹窗不显示下次回访输入和快捷项", async () => {
  const harness = makeHarness({permissions: [
    "patient.profile.view", "return_visit.view",
  ]});

  await harness.api.openRv("rv/synthetic", "patient/synthetic");
  const overlay = harness.overlays.find(item => item.id === "chvRvOvl" && !item.removed);

  assert.equal(overlay.querySelector("#chvMNext"), null);
  assert.equal(overlay.querySelectorAll("[data-chv-next]").length, 0);
});

test("医生与回访人名录精确 URL、独立降级，且私密字段不泄露", async t => {
  const doctorUrl = "/api/staff-members?role=%E5%8C%BB%E7%94%9F";
  const visitorUrl = "/api/staff-members";
  for (const failedMode of ["doctor", "visitor"]) {
    await t.test(`${failedMode} 失败`, async () => {
      const harness = makeHarness({
        permissions: ["patient.profile.view", "return_visit.view"],
        route: ({url}) => {
          if (url.startsWith("/api/return-visits?")) return {data: {list: [], totalcount: 0}};
          if (url === doctorUrl) return failedMode === "doctor"
            ? {status: 500}
            : {data: {members: [{
              staff_id: "doctor-safe", name: "Synthetic Doctor", role: "医生", roles: ["医生"],
              phone: "DOCTOR_SECRET_PHONE", id_card: "DOCTOR_SECRET_ID", employment_status: "active",
            }]}};
          if (url === visitorUrl) return failedMode === "visitor"
            ? {status: 500}
            : {data: {members: [{
              staff_id: "visitor-safe", name: "Synthetic Visitor", role: "回访", roles: ["回访"],
              phone: "VISITOR_SECRET_PHONE", id_card: "VISITOR_SECRET_ID", employment_status: "active",
            }]}};
          return undefined;
        },
      });
      const body = harness.body();
      assert.equal(await harness.api.renderList(0), true);
      assert.equal(harness.calls.filter(call => call.url === doctorUrl).length, 1);
      assert.equal(harness.calls.filter(call => call.url === visitorUrl).length, 1);
      assert.match(body.innerHTML, /员工名录载入失败,手填/);
      assert.match(body.innerHTML, failedMode === "doctor" ? /Synthetic Visitor/ : /Synthetic Doctor/);
      assert.doesNotMatch(body.innerHTML, /SECRET_PHONE|SECRET_ID|employment_status/);
    });
  }
});

test("员工名录 200 畸形 envelope 也必须按 doctor/visitor 独立降级", async t => {
  const doctorUrl = "/api/staff-members?role=%E5%8C%BB%E7%94%9F";
  const visitorUrl = "/api/staff-members";
  for (const [failedMode, malformed] of [
    ["doctor", {}],
    ["visitor", {members: {}}],
  ]) {
    await t.test(`${failedMode} envelope 畸形`, async () => {
      const harness = makeHarness({
        permissions: ["patient.profile.view", "return_visit.view"],
        route: ({url}) => {
          if (url.startsWith("/api/return-visits?")) return {data: {list: [], totalcount: 0}};
          if (url === doctorUrl) return {data: failedMode === "doctor"
            ? malformed
            : {members: [{staff_id: "doctor", name: "Synthetic Doctor", role: "医生", roles: ["医生"]}]}};
          if (url === visitorUrl) return {data: failedMode === "visitor"
            ? malformed
            : {members: [{staff_id: "visitor", name: "Synthetic Visitor", role: "回访", roles: ["回访"]}]}};
          return undefined;
        },
      });
      const body = harness.body();
      assert.equal(await harness.api.renderList(0), true);
      assert.match(body.innerHTML, /员工名录载入失败,手填/);
      assert.match(body.innerHTML, failedMode === "doctor" ? /Synthetic Visitor/ : /Synthetic Doctor/);
    });
  }
});

test("员工名录 200 畸形成员必须整个筛选降级，不得使用部分列表", async t => {
  const doctorUrl = "/api/staff-members?role=%E5%8C%BB%E7%94%9F";
  const visitorUrl = "/api/staff-members";
  const validDoctor = {
    staff_id: "doctor",
    name: "Synthetic Doctor",
    role: "医生",
    roles: ["医生"],
  };
  const validVisitor = {
    staff_id: "visitor",
    name: "Synthetic Visitor",
    role: "回访",
    roles: ["回访"],
  };
  for (const [failedMode, malformed] of [
    ["doctor", {members: [{}]}],
    ["visitor", {members: [{
      staff_id: "partial-must-not-survive",
      name: "PARTIAL_MUST_NOT_SURVIVE",
      role: "回访",
      roles: ["回访"],
    }, {}]}],
  ]) {
    await t.test(`${failedMode} 成员畸形`, async () => {
      const harness = makeHarness({
        permissions: ["patient.profile.view", "return_visit.view"],
        route: ({url}) => {
          if (url.startsWith("/api/return-visits?")) return {data: {list: [], totalcount: 0}};
          if (url === doctorUrl) return {data: failedMode === "doctor"
            ? malformed
            : {members: [validDoctor]}};
          if (url === visitorUrl) return {data: failedMode === "visitor"
            ? malformed
            : {members: [validVisitor]}};
          return undefined;
        },
      });
      const body = harness.body();
      assert.equal(await harness.api.renderList(0), true);
      assert.match(body.innerHTML, /员工名录载入失败,手填/);
      assert.match(body.innerHTML, failedMode === "doctor" ? /Synthetic Visitor/ : /Synthetic Doctor/);
      assert.doesNotMatch(body.innerHTML, /PARTIAL_MUST_NOT_SURVIVE/);
    });
  }
});
