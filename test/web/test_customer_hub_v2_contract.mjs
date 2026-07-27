import { test } from "node:test";
import assert from "node:assert/strict";
import { createRequire } from "node:module";
import { readFileSync } from "node:fs";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";
import vm from "node:vm";

const require = createRequire(import.meta.url);
const here = dirname(fileURLToPath(import.meta.url));
const sourcePath = join(here, "..", "..", "local_app", "static", "customer_hub_v2_contract.js");

function deferred() {
  let resolve;
  let reject;
  const promise = new Promise((done, fail) => {
    resolve = done;
    reject = fail;
  });
  return {promise, resolve, reject};
}

function control({disabled = false} = {}) {
  const attributes = new Map();
  return {
    disabled,
    isConnected: true,
    focusCount: 0,
    focus() { this.focusCount += 1; },
    setAttribute(name, value) { attributes.set(name, String(value)); },
    getAttribute(name) { return attributes.get(name) ?? null; },
  };
}

test("合同层加载不读取 DOM、不发请求", () => {
  const source = readFileSync(sourcePath, "utf8");
  let domReads = 0;
  let fetchCalls = 0;
  const sandbox = {
    module: { exports: {} },
    document: new Proxy({}, { get() { domReads += 1; throw new Error("DOM read"); } }),
    fetch() { fetchCalls += 1; throw new Error("fetch"); },
  };
  sandbox.globalThis = sandbox;
  vm.createContext(sandbox);
  vm.runInContext(source, sandbox);
  assert.equal(domReads, 0);
  assert.equal(fetchCalls, 0);
  assert.equal(typeof sandbox.module.exports.buildReturnVisitWriteRequest, "function");
});

test("合同层浏览器模式只导出 CustomerHubV2Contract 且加载时无副作用", () => {
  const source = readFileSync(sourcePath, "utf8");
  let fetchCalls = 0;
  const sandbox = {
    fetch() { fetchCalls += 1; throw new Error("load must not fetch"); },
  };
  sandbox.globalThis = sandbox;
  vm.createContext(sandbox);
  vm.runInContext(source, sandbox);
  assert.equal(typeof sandbox.CustomerHubV2Contract, "object");
  assert.equal(typeof sandbox.CustomerHubV2Contract.handleReturnVisitWrite, "function");
  assert.equal(fetchCalls, 0);
});

test("员工目录按医生与全部在职人员分流，只保留四个白名单字段", () => {
  const contract = require(sourcePath);
  assert.deepEqual(contract.buildStaffDirectoryRequest("doctor"), {
    method: "GET",
    url: "/api/staff-members?role=%E5%8C%BB%E7%94%9F",
  });
  assert.deepEqual(contract.buildStaffDirectoryRequest("visitor"), {
    method: "GET",
    url: "/api/staff-members",
  });
  assert.equal(contract.buildStaffDirectoryRequest("unknown"), null);

  const model = contract.normalizeStaffDirectory({members: [
    {
      staff_id: "staff-doctor",
      name: "合成医生",
      role: "医生",
      roles: ["医生", "咨询师"],
      phone: "forbidden",
      id_card: "forbidden",
      username: "forbidden",
    },
    {
      staff_id: "staff-left",
      name: "已离职",
      role: "医生",
      roles: ["医生"],
      employment_status: "left",
    },
  ]});
  assert.deepEqual(model, [{
    staff_id: "staff-doctor",
    name: "合成医生",
    role: "医生",
    roles: ["医生", "咨询师"],
  }]);
  assert.deepEqual(Object.keys(model[0]), ["staff_id", "name", "role", "roles"]);
  assert.doesNotMatch(JSON.stringify(model), /phone|id_card|username|credential/);
});

test("员工目录任一成员畸形必须整体失败，不得返回假空或部分列表", () => {
  const contract = require(sourcePath);
  const valid = {
    staff_id: "staff-valid",
    name: "合成员工",
    role: "医生",
    roles: ["医生"],
  };
  for (const malformed of [
    {members: [{}]},
    {members: [valid, {}]},
    {members: [{staff_id: "staff-no-roles", name: "缺少岗位列表", role: "医生"}]},
    {members: [{...valid, roles: ["医生", 7]}]},
    {members: [{...valid, roles: "医生"}]},
    {members: [{employment_status: "left"}]},
  ]) {
    assert.throws(
      () => contract.normalizeStaffDirectory(malformed),
      error => error instanceof TypeError && /staff_directory/i.test(error.message),
    );
  }
});

test("员工目录 envelope 畸形必须抛错，合法空数组才是真实空名录", () => {
  const contract = require(sourcePath);
  for (const malformed of [null, {}, {members: {}}, {members: "not-array"}]) {
    assert.throws(() => contract.normalizeStaffDirectory(malformed), /staff_directory/i);
  }
  assert.deepEqual(contract.normalizeStaffDirectory({members: []}), []);
});

test("2xx 响应体不是合法 JSON 时必须失败关闭", async () => {
  const contract = require(sourcePath);
  await assert.rejects(
    contract.fetchJson("/api/return-visits", {
      fetch: async () => ({
        ok: true,
        status: 200,
        async json() { throw new SyntaxError("synthetic invalid json"); },
      }),
    }),
    error => error instanceof TypeError && /json/i.test(error.message),
  );
});

test("回访与沟通主列表拒绝畸形 envelope，仅合法空列表显示空态", () => {
  const contract = require(sourcePath);
  const normalizers = [
    contract.normalizeReturnVisitList,
    contract.normalizeCommunicationList,
  ];
  for (const normalize of normalizers) {
    for (const malformed of [
      null,
      {},
      {list: {}, totalcount: 0},
      {list: [], totalcount: "0"},
      {list: [null], totalcount: 1},
      Object.create({list: [], totalcount: 0}),
    ]) {
      assert.throws(
        () => normalize(malformed),
        error => error instanceof TypeError && /list_envelope/i.test(error.message),
      );
    }
    assert.deepEqual(normalize({list: [], totalcount: 0}), {list: [], totalcount: 0});
  }
});

test("病历时间线只输出摘要与条目白名单，并保留正式无条目病历", () => {
  const contract = require(sourcePath);
  const normalized = contract.normalizeMedicalRecordTimeline({
    medical_records: [{
      record_id: "record-synthetic",
      record_type: "初诊",
      visit_time: "2026-07-21 09:00:00",
      doctor_name: "合成医生",
      created_at: null,
      updated_at: "2026-07-21 10:00:00",
      content_json: "不得进入呈现层",
    }],
    records: {"record-synthetic": []},
    totalcount: 0,
    patient_phone: "不得进入呈现层",
  });

  assert.deepEqual(normalized, {
    medical_records: [{
      record_id: "record-synthetic",
      record_type: "初诊",
      visit_time: "2026-07-21 09:00:00",
      doctor_name: "合成医生",
      created_at: null,
      updated_at: "2026-07-21 10:00:00",
      items: [],
    }],
    totalcount: 0,
  });
  assert.doesNotMatch(JSON.stringify(normalized), /content_json|patient_phone/);
});

test("病历时间线任一摘要、分组、条目或计数畸形都整体失败", () => {
  const contract = require(sourcePath);
  const summary = {
    record_id: "record-synthetic",
    record_type: "初诊",
    visit_time: "2026-07-21 09:00:00",
    doctor_name: "合成医生",
    created_at: null,
    updated_at: "2026-07-21 10:00:00",
  };
  const item = {
    item_id: "item-synthetic",
    record_id: "record-synthetic",
    tooth: "36",
    item_type: "exam",
    content: "合成检查",
    sort_order: 0,
    created_at: null,
  };
  const malformed = [
    null,
    {},
    {medical_records: {}, records: {}, totalcount: 0},
    {medical_records: [summary], records: {}, totalcount: 0},
    {medical_records: [summary], records: {"record-synthetic": [], extra: []}, totalcount: 0},
    {medical_records: [{...summary, record_id: ""}], records: {"": []}, totalcount: 0},
    {medical_records: [{...summary, doctor_name: 7}], records: {"record-synthetic": []}, totalcount: 0},
    {medical_records: [summary, summary], records: {"record-synthetic": []}, totalcount: 0},
    {medical_records: [summary], records: {"record-synthetic": [{...item, record_id: "other"}]}, totalcount: 1},
    {medical_records: [summary], records: {"record-synthetic": [{...item, sort_order: "0"}]}, totalcount: 1},
    {medical_records: [summary], records: {"record-synthetic": [item]}, totalcount: 0},
    {medical_records: [summary], records: {"record-synthetic": [item]}, totalcount: "1"},
  ];

  for (const payload of malformed) {
    assert.throws(
      () => contract.normalizeMedicalRecordTimeline(payload),
      error => error instanceof TypeError && /medical_record_timeline/i.test(error.message),
    );
  }
});

test("月历日格合同只返回五个白名单字段并保留 done 口径", () => {
  const contract = require(sourcePath);
  assert.equal(typeof contract.normalizeCalendarDays, "function");
  assert.deepEqual(contract.normalizeCalendarDays({
    month: "2026-07",
    days: [{
      date: "2026-07-08",
      count: 3,
      done: 2,
      pending: 1,
      overdue: 0,
      completed: 99,
      patient_name: "不应进入呈现层",
    }],
    total: 3,
  }, "2026-07"), [{
    date: "2026-07-08",
    count: 3,
    done: 2,
    pending: 1,
    overdue: 0,
  }]);
});

test("月历日格 envelope、日期、计数与不变量畸形时整体失败", () => {
  const contract = require(sourcePath);
  const validDay = {
    date: "2026-07-08", count: 3, done: 2, pending: 1, overdue: 0,
  };
  const malformed = [
    null,
    {},
    {month: "2026-07", days: {}},
    {month: "2026-08", days: [validDay]},
    {month: "2026-07", days: [null]},
    {month: "2026-07", days: [{...validDay, date: "2026-7-8"}]},
    {month: "2026-07", days: [{...validDay, date: "2026-07-32"}]},
    {month: "2026-07", days: [{...validDay, date: "2026-08-08"}]},
    {month: "2026-07", days: [{...validDay, count: "3"}]},
    {month: "2026-07", days: [{...validDay, done: -1}]},
    {month: "2026-07", days: [{...validDay, pending: Number.NaN}]},
    {month: "2026-07", days: [{...validDay, overdue: 2}]},
    {month: "2026-07", days: [{...validDay, count: 4}]},
    {month: "2026-07", days: [validDay, {...validDay}]},
  ];
  for (const payload of malformed) {
    assert.throws(
      () => contract.normalizeCalendarDays(payload, "2026-07"),
      error => error instanceof TypeError && /calendar_days/i.test(error.message),
    );
  }
  assert.throws(
    () => contract.normalizeCalendarDays({month: "2026-07", days: []}, "2026-7"),
    /calendar_days/i,
  );
});

test("月历日格不读取继承属性、accessor 或稀疏数组", () => {
  const contract = require(sourcePath);
  let getterReads = 0;
  const accessorPayload = {month: "2026-07"};
  Object.defineProperty(accessorPayload, "days", {
    configurable: true,
    enumerable: true,
    get() {
      getterReads += 1;
      return [];
    },
  });
  const accessorDay = {date: "2026-07-08", count: 3, pending: 1, overdue: 0};
  Object.defineProperty(accessorDay, "done", {
    configurable: true,
    enumerable: true,
    get() {
      getterReads += 1;
      return 2;
    },
  });
  const sparseDays = [];
  sparseDays.length = 1;
  for (const payload of [
    Object.create({month: "2026-07", days: []}),
    accessorPayload,
    {month: "2026-07", days: [Object.create({
      date: "2026-07-08", count: 3, done: 2, pending: 1, overdue: 0,
    })]},
    {month: "2026-07", days: [accessorDay]},
    {month: "2026-07", days: sparseDays},
  ]) {
    assert.throws(() => contract.normalizeCalendarDays(payload, "2026-07"), /calendar_days/i);
  }
  assert.equal(getterReads, 0);
});

test("月历日格不用 Proxy get 读取数组长度，descriptor trap 统一失败关闭", () => {
  const contract = require(sourcePath);
  const validDay = {
    date: "2026-07-08", count: 1, done: 1, pending: 0, overdue: 0,
  };
  let lengthReads = 0;
  const proxiedDays = new Proxy([validDay], {
    get(target, key, receiver) {
      if (key === "length") {
        lengthReads += 1;
        return 0;
      }
      return Reflect.get(target, key, receiver);
    },
  });
  assert.deepEqual(
    contract.normalizeCalendarDays({month: "2026-07", days: proxiedDays}, "2026-07"),
    [validDay],
  );
  assert.equal(lengthReads, 0);

  const descriptorTrap = new Proxy([], {
    getOwnPropertyDescriptor() {
      throw new RangeError("synthetic descriptor trap");
    },
  });
  assert.throws(
    () => contract.normalizeCalendarDays(
      {month: "2026-07", days: descriptorTrap}, "2026-07",
    ),
    error => error instanceof TypeError && /calendar_days/i.test(error.message),
  );
});

test("回访编辑和删除必须携带精确 revision，且剥离服务端字段", () => {
  const contract = require(sourcePath);
  assert.deepEqual(contract.buildReturnVisitWriteRequest({
    action: "edit",
    id: "rv/one",
    expectedRevision: 7,
    fields: { note: "合成内容", operator: "伪造人员", actual_date: "2026-07-21" },
  }), {
    method: "PUT",
    url: "/api/return-visits/rv%2Fone",
    bodyKind: "json",
    body: { note: "合成内容", expected_revision: 7 },
  });
  assert.deepEqual(contract.buildReturnVisitWriteRequest({
    action: "delete",
    id: "rv/one",
    expectedRevision: 8,
  }).body, { expected_revision: 8 });
  assert.equal(contract.buildReturnVisitWriteRequest({action: "delete", id: "rv/one"}), null);
});

test("所有写请求 builder 拒绝继承 action，不从原型生成请求", () => {
  const contract = require(sourcePath);
  const cases = [
    [contract.buildReturnVisitWriteRequest, {action: "delete", id: "rv-1", expectedRevision: 2}],
    [contract.buildCommunicationWriteRequest, {action: "delete", id: "comm-1", expectedRevision: 2}],
    [contract.buildCallWriteRequest, {action: "delete", id: "call-1", expectedRevision: 2}],
    [contract.buildDictionaryWriteRequest, {action: "update", fields: {visit_content: []}}],
  ];
  for (const [builder, inherited] of cases) {
    assert.equal(builder(Object.create(inherited)), null);
  }
  assert.equal(contract.buildReturnVisitWriteRequest(Object.assign(
    Object.create({id: "rv-1"}),
    {action: "delete", expectedRevision: 2},
  )), null);
  assert.equal(contract.buildCommunicationWriteRequest(Object.assign(
    Object.create({expectedRevision: 2}),
    {action: "delete", id: "comm-1"},
  )), null);
});

test("builder 拒绝 id/revision/写字段 accessor，且 getter 执行 0 次", () => {
  const contract = require(sourcePath);
  let getterCalls = 0;
  const accessor = value => ({
    enumerable: true,
    get() { getterCalls += 1; return value; },
  });

  const accessorId = {action: "delete", expectedRevision: 2};
  Object.defineProperty(accessorId, "id", accessor("rv-1"));
  assert.equal(contract.buildReturnVisitWriteRequest(accessorId), null);

  const accessorAction = {id: "rv-1", expectedRevision: 2};
  Object.defineProperty(accessorAction, "action", accessor("delete"));
  assert.equal(contract.buildReturnVisitWriteRequest(accessorAction), null);

  const accessorRevision = {action: "delete", id: "comm-1"};
  Object.defineProperty(accessorRevision, "expectedRevision", accessor(2));
  assert.equal(contract.buildCommunicationWriteRequest(accessorRevision), null);

  const fields = {};
  Object.defineProperty(fields, "content", accessor("合成内容"));
  assert.equal(contract.buildCommunicationWriteRequest({
    action: "edit",
    id: "comm-1",
    expectedRevision: 2,
    fields,
  }), null);
  assert.equal(getterCalls, 0);
});

test("批量删除逐条绑定 revision，附件写入携带父 revision", () => {
  const contract = require(sourcePath);
  const batch = contract.buildReturnVisitWriteRequest({
    action: "batchDelete",
    ids: ["rv-1", "rv-2"],
    revisions: {"rv-1": 3, "rv-2": 5},
  });
  assert.deepEqual(batch.body, {items: [
    {id: "rv-1", expected_revision: 3},
    {id: "rv-2", expected_revision: 5},
  ]});
  const upload = contract.buildReturnVisitWriteRequest({
    action: "attachment",
    id: "rv-1",
    expectedRevision: 5,
    media: {name: "synthetic.png"},
    fileName: "synthetic.png",
  });
  assert.equal(upload.body.expected_revision, 5);
});

test("409 明确分类为冲突且不自动重放", async () => {
  const contract = require(sourcePath);
  let sends = 0;
  const state = {expectedRevision: 9, dirty: true, fields: {note: "合成内容"}};
  const result = await contract.handleReturnVisitWrite({
    action: "edit",
    id: "rv-9",
    fields: state.fields,
    state,
    sendRequest: async () => { sends += 1; return {ok: false, status: 409}; },
  });
  assert.equal(sends, 1);
  assert.equal(result.ok, false);
  assert.equal(result.status, 409);
  assert.match(result.message, /冲突/);
  assert.equal(state.dirty, true);
});

test("沟通和字典写入剥离服务端字段，字典请求保留动态键", () => {
  const contract = require(sourcePath);
  assert.deepEqual(contract.buildCommunicationWriteRequest({
    action: "create",
    patientIdentity: "patient/one",
    fields: {
      content: "合成沟通",
      operator: "伪造人员",
      contacted_at: "2026-07-21 09:00:00",
    },
  }), {
    method: "POST",
    url: "/api/patients/patient%2Fone/communications",
    bodyKind: "json",
    body: {content: "合成沟通"},
  });
  assert.deepEqual(contract.buildDictionaryWriteRequest({
    action: "update",
    fields: {visit_content: ["合成常用语"]},
  }).body, {visit_content: ["合成常用语"]});
});

test("录音上传同时携带父 revision 和且仅一个父记录链接", () => {
  const contract = require(sourcePath);
  const upload = contract.buildCallWriteRequest({
    action: "attachment",
    patientIdentity: "patient-1",
    linkedReturnVisitId: "rv-1",
    expectedRevision: 6,
    media: {name: "synthetic.webm"},
    fileName: "synthetic.webm",
    direction: "outbound",
    source: "desktop_mic",
  });
  assert.equal(upload.url, "/api/patients/patient-1/calls");
  assert.equal(upload.body.expected_revision, 6);
  assert.equal(upload.body.linked_return_visit_id, "rv-1");
  assert.equal(Object.hasOwn(upload.body, "linked_communication_id"), false);
  assert.equal(contract.buildCallWriteRequest({
    action: "attachment",
    patientIdentity: "patient-1",
    linkedReturnVisitId: "rv-1",
    linkedCommunicationId: "comm-1",
    expectedRevision: 6,
    media: {},
  }), null);
});

test("400/403/500 和网络失败都单次返回明确错误，写入中拒绝重放", async () => {
  const contract = require(sourcePath);
  for (const status of [400, 403, 500]) {
    let sends = 0;
    const result = await contract.handleReturnVisitWrite({
      action: "delete",
      id: "rv-1",
      expectedRevision: 2,
      sendRequest: async () => { sends += 1; return {ok: false, status}; },
    });
    assert.equal(sends, 1);
    assert.equal(result.status, status);
    assert.match(result.message, status === 403 ? /权限/ : /失败/);
  }
  let networkSends = 0;
  const network = await contract.handleReturnVisitWrite({
    action: "delete",
    id: "rv-1",
    expectedRevision: 2,
    sendRequest: async () => { networkSends += 1; throw new Error("synthetic"); },
  });
  assert.equal(networkSends, 1);
  assert.equal(network.status, 0);
  assert.match(network.message, /网络/);

  const state = {expectedRevision: 2, writeInFlight: true};
  let busySends = 0;
  const busy = await contract.handleReturnVisitWrite({
    action: "delete",
    id: "rv-1",
    state,
    sendRequest: async () => { busySends += 1; return {ok: true, status: 200}; },
  });
  assert.equal(busySends, 0);
  assert.equal(busy.busy, true);
  assert.equal(state.writeInFlight, true);
});

test("handle 写入拒绝继承 state/action 与 accessor 输入，getter 0 次且 0 发送", async () => {
  const contract = require(sourcePath);
  let sends = 0;
  let getterCalls = 0;
  const sendRequest = async () => {
    sends += 1;
    return {ok: true, status: 200};
  };

  const inheritedState = Object.assign(Object.create({state: {expectedRevision: 2}}), {
    action: "delete",
    id: "rv-inherited-state",
    sendRequest,
  });
  assert.equal((await contract.handleReturnVisitWrite(inheritedState)).status, 400);

  const inheritedAction = Object.assign(Object.create({action: "delete"}), {
    id: "rv-inherited-action",
    expectedRevision: 2,
    sendRequest,
  });
  assert.equal((await contract.handleReturnVisitWrite(inheritedAction)).status, 400);

  const accessorCases = [
    ["state", {action: "delete", id: "rv-accessor-state", sendRequest}, {expectedRevision: 2}],
    ["action", {id: "rv-accessor-action", expectedRevision: 2, sendRequest}, "delete"],
    ["sendRequest", {action: "delete", id: "rv-accessor-send", expectedRevision: 2}, sendRequest],
  ];
  for (const [key, options, value] of accessorCases) {
    Object.defineProperty(options, key, {
      enumerable: true,
      get() { getterCalls += 1; return value; },
    });
    assert.equal((await contract.handleReturnVisitWrite(options)).status, 400);
  }
  assert.equal(getterCalls, 0);
  assert.equal(sends, 0);
});

test("runRetriableWrite 不执行 response accessor，不误判成功", async () => {
  const contract = require(sourcePath);
  let getterCalls = 0;
  let successCalls = 0;
  const response = {};
  for (const [key, value] of [["ok", true], ["status", 200]]) {
    Object.defineProperty(response, key, {
      enumerable: true,
      get() { getterCalls += 1; return value; },
    });
  }
  const result = await contract.runRetriableWrite({
    request: {method: "DELETE", url: "/synthetic"},
    sendRequest: async () => response,
    onSuccess: () => { successCalls += 1; },
  });
  assert.equal(result.ok, false);
  assert.equal(result.status, 0);
  assert.equal(successCalls, 0);
  assert.equal(getterCalls, 0);
});

test("默认发送适配器读取原生 Response 原型状态并保留错误体", async () => {
  const contract = require(sourcePath);
  const nativeLikeResponse = Object.create({
    ok: false,
    status: 409,
    json: async () => ({detail: "revision_conflict", revision: 12}),
  });
  const result = await contract.sendCustomerHubRequest({
    method: "PATCH",
    url: "/api/communications/comm%2Fsynthetic",
    bodyKind: "json",
    body: {content: "合成草稿", expected_revision: 11},
  }, {fetch: async () => nativeLikeResponse});
  assert.equal(result.ok, false);
  assert.equal(result.status, 409);
  assert.deepEqual(result.data, {detail: "revision_conflict", revision: 12});
  assert.strictEqual(result.response, nativeLikeResponse);
});

test("回访 edit 回执：升 revision 可确认，同 revision 仅完整自有字段逐项一致才确认", () => {
  const contract = require(sourcePath);
  const fields = {
    due_time: "2026-07-21 10:00",
    item_name: "合成回访",
    note: "合成内容",
    channel: "phone",
    visitor: "合成操作员",
    status: "待回访",
    return_type: "",
    return_result: "",
  };
  const options = {
    id: "rv/synthetic",
    patientIdentity: "patient/synthetic",
    expectedRevision: 5,
    fields,
  };
  const matchingRecord = {
    return_visit_id: "rv/synthetic",
    patient_identity: "patient/synthetic",
    ...fields,
    revision: 5,
  };

  assert.equal(contract.validateReturnVisitEditReceipt(
    {data: {return_visit: matchingRecord}}, options,
  ), 5);
  assert.equal(contract.validateReturnVisitEditReceipt(
    {data: {return_visit: {revision: 6}}}, options,
  ), 6, "升 revision 保留现有成功语义");
  assert.equal(contract.validateReturnVisitEditReceipt(
    {data: {return_visit: {revision: 5}}}, options,
  ), null, "同 revision 缺字段不得确认为 no-op");
  assert.equal(contract.validateReturnVisitEditReceipt(
    {data: {return_visit: {...matchingRecord, note: "未保存的旧值"}}}, options,
  ), null, "同 revision 字段不一致不得确认为 no-op");
});

test("回访同 revision 回执拒绝继承值和 accessor，getter 执行 0 次", () => {
  const contract = require(sourcePath);
  const fields = {
    due_time: "2026-07-21 10:00",
    item_name: "合成回访",
    note: "合成内容",
    channel: "phone",
    visitor: "合成操作员",
    status: "待回访",
    return_type: "",
    return_result: "",
  };
  const options = {
    id: "rv/synthetic",
    patientIdentity: "patient/synthetic",
    expectedRevision: 5,
    fields,
  };
  let getterCalls = 0;
  const accessorRecord = {
    return_visit_id: "rv/synthetic",
    patient_identity: "patient/synthetic",
    ...fields,
  };
  Object.defineProperty(accessorRecord, "revision", {
    enumerable: true,
    get() { getterCalls += 1; return 5; },
  });
  const inheritedRecord = Object.assign(Object.create({revision: 5}), {
    return_visit_id: "rv/synthetic",
    patient_identity: "patient/synthetic",
    ...fields,
  });

  assert.equal(contract.validateReturnVisitEditReceipt(
    {data: {return_visit: accessorRecord}}, options,
  ), null);
  assert.equal(contract.validateReturnVisitEditReceipt(
    {data: {return_visit: inheritedRecord}}, options,
  ), null);
  assert.equal(getterCalls, 0);
});

test("回访 edit 确认 GET 必须升 revision 且完整匹配草稿", () => {
  const contract = require(sourcePath);
  const fields = {
    due_time: "2026-07-21 11:00",
    item_name: "合成回访",
    note: "合成内容",
    channel: "phone",
    visitor: "合成操作员",
    status: "待回访",
    return_type: "",
    return_result: "",
  };
  const options = {
    id: "rv/synthetic",
    patientIdentity: "patient/synthetic",
    expectedRevision: 5,
    fields,
  };
  const matching = {
    return_visit_id: "rv/synthetic",
    patient_identity: "patient/synthetic",
    ...fields,
    revision: 6,
  };

  assert.equal(contract.validateReturnVisitEditConfirmation(matching, options), 6);
  assert.equal(contract.validateReturnVisitEditConfirmation({...matching, revision: 5}, options), null);
  assert.equal(contract.validateReturnVisitEditConfirmation({revision: 6}, options), null);
  assert.equal(contract.validateReturnVisitEditConfirmation({...matching, note: "旧值"}, options), null);

  let getterCalls = 0;
  const accessorRecord = {...matching};
  Object.defineProperty(accessorRecord, "revision", {
    enumerable: true,
    get() { getterCalls += 1; return 6; },
  });
  assert.equal(contract.readReturnVisitRevision(accessorRecord), null);
  assert.equal(contract.validateReturnVisitEditConfirmation(accessorRecord, options), null);
  assert.equal(getterCalls, 0);
});

test("共享状态锁住并发写，pending 冻结全部控件且失败后精确恢复与聚焦", async () => {
  const contract = require(sourcePath);
  const state = {expectedRevision: 4, dirty: true};
  const field = control();
  const submit = control();
  const originallyDisabled = control({disabled: true});
  const dialog = control();
  const pending = deferred();
  let sends = 0;

  const firstPromise = contract.handleReturnVisitWrite({
    action: "edit",
    id: "rv-synthetic",
    fields: {note: "合成草稿"},
    state,
    pendingControls: [field, submit, originallyDisabled],
    pendingFocusTarget: dialog,
    focusTarget: field,
    sendRequest: async request => {
      sends += 1;
      assert.equal(request.body.expected_revision, 4);
      return pending.promise;
    },
  });
  await Promise.resolve();

  assert.equal(field.disabled, true);
  assert.equal(submit.disabled, true);
  assert.equal(originallyDisabled.disabled, true);
  assert.equal(field.getAttribute("aria-busy"), "true");
  assert.equal(dialog.focusCount, 1);

  const second = await contract.handleReturnVisitWrite({
    action: "delete",
    id: "rv-synthetic",
    state,
    sendRequest: async () => {
      sends += 1;
      return {ok: true, status: 200};
    },
  });
  assert.equal(second.busy, true);
  assert.equal(sends, 1);

  pending.resolve({ok: false, status: 409});
  const first = await firstPromise;
  assert.equal(first.status, 409);
  assert.equal(field.disabled, false);
  assert.equal(submit.disabled, false);
  assert.equal(originallyDisabled.disabled, true);
  assert.equal(field.getAttribute("aria-busy"), "false");
  assert.equal(originallyDisabled.getAttribute("aria-disabled"), "true");
  assert.equal(field.focusCount, 1);
  assert.equal(state.writeInFlight, undefined);
  assert.equal(state.dirty, true);
});

test("失败保留草稿媒体与选择，显式重试读取状态中最新 revision，成功才清理", async () => {
  const contract = require(sourcePath);
  const media = new Uint8Array([7, 2, 1]);
  const state = {
    expectedRevision: 21,
    fields: {note: "合成未保存内容"},
    pendingMedia: [media],
    selectedItems: ["rv-synthetic"],
    modalOpen: true,
    dirty: true,
  };
  const revisions = [];
  let attempt = 0;
  const sendRequest = async request => {
    revisions.push(request.body.expected_revision);
    attempt += 1;
    return attempt === 1 ? {ok: false, status: 409} : {ok: true, status: 200};
  };

  const first = await contract.handleReturnVisitWrite({
    action: "edit",
    id: "rv-synthetic",
    fields: state.fields,
    state,
    sendRequest,
  });
  assert.equal(first.status, 409);
  assert.deepEqual(revisions, [21]);
  assert.deepEqual(state.fields, {note: "合成未保存内容"});
  assert.strictEqual(state.pendingMedia[0], media);
  assert.deepEqual(state.selectedItems, ["rv-synthetic"]);
  assert.equal(state.modalOpen, true);
  assert.equal(state.dirty, true);

  state.expectedRevision = 22;
  const second = await contract.handleReturnVisitWrite({
    action: "edit",
    id: "rv-synthetic",
    fields: state.fields,
    state,
    sendRequest,
  });
  assert.equal(second.ok, true);
  assert.deepEqual(revisions, [21, 22]);
  assert.deepEqual(state.fields, {});
  assert.deepEqual(state.pendingMedia, []);
  assert.deepEqual(state.selectedItems, []);
  assert.equal(state.modalOpen, false);
  assert.equal(state.dirty, false);
});

test("批删每次读取共享状态内最新 revisions，迟到响应不得运行成功回调或抢焦点", async () => {
  const contract = require(sourcePath);
  const state = {revisions: {"rv-a": 31, "rv-b": 32}};
  let sentBody;
  const batch = await contract.handleReturnVisitWrite({
    action: "batchDelete",
    ids: ["rv-a", "rv-b"],
    revisions: {"rv-a": 1, "rv-b": 1},
    state,
    sendRequest: async request => {
      sentBody = request.body;
      return {ok: true, status: 200};
    },
  });
  assert.equal(batch.ok, true);
  assert.deepEqual(sentBody, {items: [
    {id: "rv-a", expected_revision: 31},
    {id: "rv-b", expected_revision: 32},
  ]});

  let current = true;
  let successCalls = 0;
  const field = control();
  const late = deferred();
  const request = contract.handleCommunicationWrite({
    action: "edit",
    id: "comm-synthetic",
    expectedRevision: 8,
    fields: {content: "合成内容"},
    state: {},
    focusTarget: field,
    isCurrent: () => current,
    onSuccess: () => { successCalls += 1; },
    sendRequest: async () => late.promise,
  });
  current = false;
  late.resolve({ok: true, status: 200});
  const result = await request;
  assert.equal(result.stale, true);
  assert.equal(successCalls, 0);
  assert.equal(field.focusCount, 0);
});

test("父 ID 仅 URL 路径编码，multipart 关联值保留 trim 后原值", () => {
  const contract = require(sourcePath);
  const request = contract.buildCallWriteRequest({
    action: "attachment",
    patientIdentity: " patient/一 ",
    linkedReturnVisitId: " rv/一 ",
    expectedRevision: 7,
    media: {name: "synthetic.wav"},
  });
  assert.equal(request.url, "/api/patients/patient%2F%E4%B8%80/calls");
  assert.equal(request.body.linked_return_visit_id, "rv/一");
  assert.equal(Object.hasOwn(request.body, "linked_communication_id"), false);
});

test("确认版所需写路径矩阵全部由独立合同 builder 生成", () => {
  const contract = require(sourcePath);
  const media = {name: "synthetic.bin"};
  const cases = [
    [contract.buildReturnVisitWriteRequest, {action: "edit", id: "rv", expectedRevision: 2, fields: {note: "x"}}, "PUT", "/api/return-visits/rv"],
    [contract.buildReturnVisitWriteRequest, {action: "delete", id: "rv", expectedRevision: 2}, "DELETE", "/api/return-visits/rv"],
    [contract.buildReturnVisitWriteRequest, {action: "batchDelete", ids: ["rv"], revisions: {rv: 2}}, "POST", "/api/return-visits/batch-delete"],
    [contract.buildReturnVisitWriteRequest, {action: "attachment", id: "rv", expectedRevision: 2, media}, "POST", "/api/return-visits/rv/images"],
    [contract.buildReturnVisitWriteRequest, {action: "attachmentDelete", id: "rv", imageId: "image", expectedRevision: 2}, "DELETE", "/api/return-visits/rv/images/image"],
    [contract.buildCommunicationWriteRequest, {action: "edit", id: "comm", expectedRevision: 3, fields: {content: "x"}}, "PATCH", "/api/communications/comm"],
    [contract.buildCommunicationWriteRequest, {action: "delete", id: "comm", expectedRevision: 3}, "DELETE", "/api/communications/comm"],
    [contract.buildCommunicationWriteRequest, {action: "attachment", id: "comm", expectedRevision: 3, media}, "POST", "/api/communications/comm/images"],
    [contract.buildCommunicationWriteRequest, {action: "attachmentDelete", id: "comm", imageId: "image", expectedRevision: 3}, "DELETE", "/api/communications/comm/images/image"],
    [contract.buildDictionaryWriteRequest, {action: "update", fields: {visit_content: ["x"]}}, "PUT", "/api/settings/customer-hub-dicts"],
    [contract.buildCallWriteRequest, {action: "edit", id: "call", expectedRevision: 4, fields: {deal_status: "x"}}, "PATCH", "/api/calls/call"],
    [contract.buildCallWriteRequest, {action: "delete", id: "call", expectedRevision: 4}, "DELETE", "/api/calls/call"],
    [contract.buildCallWriteRequest, {action: "attachment", patientIdentity: "patient", linkedReturnVisitId: "rv", expectedRevision: 4, media}, "POST", "/api/patients/patient/calls"],
  ];
  for (const [builder, input, method, url] of cases) {
    const request = builder(input);
    assert.ok(request, `${method} ${url}`);
    assert.equal(request.method, method);
    assert.equal(request.url, url);
  }
});
