import { test } from "node:test";
import assert from "node:assert";
import { readFileSync } from "node:fs";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";
import vm from "node:vm";

const here = dirname(fileURLToPath(import.meta.url));
const source = readFileSync(
  join(here, "..", "..", "local_app", "static", "check_in.js"),
  "utf8",
);

function deferred() {
  let resolve;
  let reject;
  const promise = new Promise((res, rej) => {
    resolve = res;
    reject = rej;
  });
  return { promise, resolve, reject };
}

function response(status, body) {
  return {
    ok: status >= 200 && status < 300,
    status,
    json: async () => body,
  };
}

function successData(appointmentId, overrides = {}) {
  return {
    appointment: { appointment_id: appointmentId, display_name: "测试患者" },
    created: true,
    already_arrived: false,
    ...overrides,
  };
}

function multipleResponse(candidates = [
  {
    appointment_id: "appt-1",
    start_time: "2026-08-02 08:30:00",
    doctor_name: "王医生",
    item_name: "洁牙",
    suspect_cancelled: false,
  },
  {
    appointment_id: "appt-2",
    start_time: "2026-08-02 10:15:00",
    doctor_name: "李医生",
    item_name: "补牙",
    suspect_cancelled: true,
  },
]) {
  return response(409, {
    detail: {
      code: "multiple_check_in_candidates",
      candidates,
    },
  });
}

function makeHarness({
  fetchImpl = async () => response(200, successData("appt-default")),
  promptAnswers = [],
  workDateValue = "2026-08-01",
  todayValue = "2026-08-02",
  loadTodayWorkImpl = async () => true,
  openTriageImpl = async () => {},
  switchWorkspaceViewImpl = async () => true,
} = {}) {
  const calls = {
    alerts: [],
    evictions: [],
    fetches: [],
    loads: 0,
    prompts: [],
    sequence: [],
    switches: [],
    triages: [],
  };
  const workDate = { value: workDateValue };
  let promptIndex = 0;
  const sandbox = {
    workDate,
    localDateValue: () => todayValue,
    window: {
      alert(message) {
        const text = String(message);
        calls.alerts.push(text);
        calls.sequence.push(`alert:${text}`);
      },
    },
    appPrompt: async (message, defaultValue) => {
      calls.prompts.push({ message: String(message), defaultValue });
      const answer = promptIndex < promptAnswers.length
        ? promptAnswers[promptIndex]
        : null;
      promptIndex += 1;
      return answer;
    },
    evictVisitsCache(patientIdentity) {
      calls.evictions.push(patientIdentity);
      calls.sequence.push(`evict:${patientIdentity}`);
    },
    async loadTodayWork() {
      calls.loads += 1;
      calls.sequence.push(`load:${workDate.value}`);
      return loadTodayWorkImpl({ calls, workDate });
    },
    async openTriage(appointmentId, appointment) {
      calls.triages.push({ appointmentId, appointment });
      calls.sequence.push(`triage:${appointmentId}`);
      return openTriageImpl({ appointmentId, appointment, calls });
    },
    async switchWorkspaceView(view) {
      calls.switches.push(view);
      calls.sequence.push(`switch:${view}`);
      return switchWorkspaceViewImpl({ view, calls });
    },
  };
  sandbox.fetch = async (url, options = {}) => {
    const call = {
      url: String(url),
      method: options.method,
      headers: { ...(options.headers || {}) },
      body: JSON.parse(options.body),
    };
    calls.fetches.push(call);
    return fetchImpl(call, calls);
  };
  vm.createContext(sandbox);
  vm.runInContext(`${source}\nthis.__checkInPatient = checkInPatient;`, sandbox);
  return { calls, checkInPatient: sandbox.__checkInPatient, sandbox, workDate };
}

test("同患者双击共享同一 Promise，且只发一次请求", async () => {
  const request = deferred();
  const data = successData("appt-one");
  const { calls, checkInPatient } = makeHarness({
    fetchImpl: () => request.promise,
  });

  const options = {
    patientIdentity: " patient /甲 ",
    displayName: "患者甲",
    showSuccess: false,
  };
  const first = checkInPatient(options);
  const second = checkInPatient(options);

  assert.strictEqual(first, second, "锁要返回首次调用的原 Promise");
  assert.strictEqual(calls.fetches.length, 1);
  assert.deepStrictEqual(calls.fetches[0], {
    url: "/api/patients/patient%20%2F%E7%94%B2/check-in",
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: {},
  });

  request.resolve(response(200, data));
  const [firstResult, secondResult] = await Promise.all([first, second]);
  assert.strictEqual(firstResult, data);
  assert.strictEqual(secondResult, data);
});

test("不同患者可并行挂号", async () => {
  const pendingByPatient = new Map();
  const { calls, checkInPatient } = makeHarness({
    fetchImpl(call) {
      const request = deferred();
      pendingByPatient.set(call.url, request);
      return request.promise;
    },
  });

  const first = checkInPatient({ patientIdentity: "p1", showSuccess: false });
  const second = checkInPatient({ patientIdentity: "p2", showSuccess: false });
  assert.notStrictEqual(first, second);
  assert.strictEqual(calls.fetches.length, 2, "不同患者不得被全局串行锁阻塞");

  pendingByPatient.get("/api/patients/p2/check-in")
    .resolve(response(200, successData("appt-p2")));
  pendingByPatient.get("/api/patients/p1/check-in")
    .resolve(response(200, successData("appt-p1")));
  const [result1, result2] = await Promise.all([first, second]);
  assert.strictEqual(result1.appointment.appointment_id, "appt-p1");
  assert.strictEqual(result2.appointment.appointment_id, "appt-p2");
});

test("409 多候选文案含时间、医生、项目和疑似取消，无效序号会重提", async () => {
  const responses = [
    multipleResponse(),
    response(200, successData("appt-2", { created: false })),
  ];
  const { calls, checkInPatient } = makeHarness({
    fetchImpl: async () => responses.shift(),
    promptAnswers: ["0", "2x", " 2 "],
  });

  const result = await checkInPatient({
    patientIdentity: "p-multi",
    displayName: "小明",
    showSuccess: false,
  });

  assert.strictEqual(result.appointment.appointment_id, "appt-2");
  assert.strictEqual(calls.prompts.length, 3, "只有完整的 1..N 整数才能结束选择");
  for (const { message } of calls.prompts) {
    assert.match(message, /小明/);
    assert.match(message, /1\..*2026-08-02 08:30:00.*王医生.*洁牙/s);
    assert.match(message, /2\..*2026-08-02 10:15:00.*李医生.*补牙.*疑似取消/s);
  }
  assert.strictEqual(calls.fetches.length, 2);
  assert.deepStrictEqual(calls.fetches[1].body, { appointment_id: "appt-2" });
});

test("多候选时取消或留空会明确提示，且不发第二次写请求", async t => {
  for (const [label, answer] of [["取消", null], ["留空", "   "]]) {
    await t.test(label, async () => {
      const { calls, checkInPatient } = makeHarness({
        fetchImpl: async () => multipleResponse(),
        promptAnswers: [answer],
      });
      const result = await checkInPatient({ patientIdentity: `p-${label}` });
      assert.strictEqual(result, null);
      assert.strictEqual(calls.fetches.length, 1);
      assert.strictEqual(calls.alerts.length, 1);
      assert.match(calls.alerts[0], /已取消挂号/);
    });
  }
});

test("显式候选竞态返回非 multiple 409 时提示并停止，不自动改选", async () => {
  const responses = [
    multipleResponse(),
    response(409, {
      detail: {
        code: "check_in_candidate_stale",
      },
    }),
  ];
  const { calls, checkInPatient } = makeHarness({
    fetchImpl: async () => responses.shift(),
    promptAnswers: ["1"],
  });

  const result = await checkInPatient({ patientIdentity: "p-race" });
  assert.strictEqual(result, null);
  assert.strictEqual(calls.fetches.length, 2, "竞态错误后不得自动试其他候选");
  assert.deepStrictEqual(calls.fetches[1].body, { appointment_id: "appt-1" });
  assert.strictEqual(calls.prompts.length, 1);
  assert.deepStrictEqual(calls.alerts, ["挂号失败：所选预约已失效，请重新挂号"]);
});

test("挂号成功严格先刷新队列，再传入后端预约打开分诊", async () => {
  const loadStarted = deferred();
  const releaseLoad = deferred();
  const data = successData("appt-triage", {
    appointment: { appointment_id: "appt-triage", doctor_name: "" },
  });
  const { calls, checkInPatient } = makeHarness({
    fetchImpl: async () => response(200, data),
    loadTodayWorkImpl: async ({ calls: innerCalls }) => {
      loadStarted.resolve();
      await releaseLoad.promise;
      innerCalls.sequence.push("load:done");
      return true;
    },
  });

  const pending = checkInPatient({
    patientIdentity: "p-triage",
    openTriageAfter: true,
  });
  await loadStarted.promise;
  assert.strictEqual(calls.triages.length, 0, "队列刷新未完成时不能提前打开分诊");
  releaseLoad.resolve();

  const result = await pending;
  assert.strictEqual(result, data);
  assert.deepStrictEqual(calls.sequence, [
    "evict:p-triage",
    "load:2026-08-01",
    "load:done",
    "triage:appt-triage",
  ]);
  assert.strictEqual(calls.triages[0].appointmentId, "appt-triage");
  assert.strictEqual(calls.triages[0].appointment, data.appointment);
  assert.deepStrictEqual(calls.alerts, [], "打开分诊时不额外弹成功提示");
});

test("切回今日时先改工作日期并刷新，等切页完成后再提示成功", async () => {
  const switchStarted = deferred();
  const releaseSwitch = deferred();
  const data = successData("appt-today");
  const { calls, checkInPatient, workDate } = makeHarness({
    fetchImpl: async () => response(200, data),
    switchWorkspaceViewImpl: async () => {
      switchStarted.resolve();
      await releaseSwitch.promise;
      return true;
    },
  });

  const pending = checkInPatient({
    patientIdentity: "p-today",
    switchToToday: true,
  });
  await switchStarted.promise;
  assert.strictEqual(workDate.value, "2026-08-02");
  assert.deepStrictEqual(calls.alerts, [], "切页 Promise 未完成前不得提前弹成功");
  releaseSwitch.resolve();

  const result = await pending;
  assert.strictEqual(result, data);
  assert.deepStrictEqual(calls.sequence, [
    "evict:p-today",
    "load:2026-08-02",
    "switch:today",
    "alert:挂号成功，已进入今日候诊",
  ]);
});

test("成功提示区分新进候诊与今天已挂号，且 showSuccess=false 可静默", async () => {
  const outcomes = [
    successData("appt-new"),
    successData("appt-existing", { created: false, already_arrived: true }),
    successData("appt-silent"),
  ];
  const { calls, checkInPatient } = makeHarness({
    fetchImpl: async () => response(200, outcomes.shift()),
  });

  await checkInPatient({ patientIdentity: "p-new" });
  await checkInPatient({ patientIdentity: "p-existing" });
  await checkInPatient({ patientIdentity: "p-silent", showSuccess: false });
  assert.deepStrictEqual(calls.alerts, [
    "挂号成功，已进入今日候诊",
    "患者今天已挂号",
  ]);
});

for (const scenario of [
  {
    label: "刷新今日候诊",
    options: {},
    harness: {
      loadTodayWorkImpl: async () => { throw new Error("refresh failed"); },
    },
  },
  {
    label: "打开分诊",
    options: { openTriageAfter: true },
    harness: {
      openTriageImpl: async () => { throw new Error("triage failed"); },
    },
  },
  {
    label: "切换今日工作台",
    options: { switchToToday: true },
    harness: {
      switchWorkspaceViewImpl: async () => { throw new Error("switch failed"); },
    },
  },
]) {
  test(`后端已挂号但${scenario.label}失败时明确提示并 resolve null`, async () => {
    const data = successData(`appt-${scenario.label}`);
    const { calls, checkInPatient } = makeHarness({
      fetchImpl: async () => response(200, data),
      ...scenario.harness,
    });

    const result = await checkInPatient({
      patientIdentity: `p-${scenario.label}`,
      ...scenario.options,
    });

    assert.strictEqual(result, null);
    assert.strictEqual(calls.fetches.length, 1, "后端已成功，不得因 UI 失败自动重复挂号");
    assert.strictEqual(calls.alerts.length, 1);
    assert.match(calls.alerts[0], /患者已挂号/);
    assert.match(calls.alerts[0], new RegExp(scenario.label));
  });
}

for (const scenario of [
  {
    label: "刷新今日候诊",
    options: {},
    harness: { loadTodayWorkImpl: async () => false },
  },
  {
    label: "切换今日工作台",
    options: { switchToToday: true },
    harness: { switchWorkspaceViewImpl: async () => false },
  },
]) {
  test(`后端已挂号但${scenario.label} resolve false 时不得误报成功`, async () => {
    const data = successData(`appt-false-${scenario.label}`);
    const { calls, checkInPatient } = makeHarness({
      fetchImpl: async () => response(200, data),
      ...scenario.harness,
    });

    const result = await checkInPatient({
      patientIdentity: `p-false-${scenario.label}`,
      ...scenario.options,
    });

    assert.strictEqual(result, null);
    assert.deepStrictEqual(calls.alerts, [
      `患者已挂号，但后续${scenario.label}失败，请手动继续处理`,
    ]);
    assert.doesNotMatch(calls.alerts[0], /挂号成功/);
  });
}

test("网络和 HTTP 错误都 resolve null 并在 finally 解锁", async () => {
  let attempt = 0;
  const recovered = successData("appt-recovered");
  const { calls, checkInPatient } = makeHarness({
    fetchImpl: async () => {
      attempt += 1;
      if (attempt === 1) throw new Error("offline");
      if (attempt === 2) return response(503, { detail: "服务暂时不可用" });
      return response(200, recovered);
    },
  });

  const networkResult = await checkInPatient({
    patientIdentity: "p-retry",
    showSuccess: false,
  });
  assert.strictEqual(networkResult, null);

  const httpResult = await checkInPatient({
    patientIdentity: "p-retry",
    showSuccess: false,
  });
  assert.strictEqual(httpResult, null);

  const finalResult = await checkInPatient({
    patientIdentity: "p-retry",
    showSuccess: false,
  });
  assert.strictEqual(finalResult, recovered);
  assert.strictEqual(calls.fetches.length, 3, "每次失败后都必须解锁才能重试");
  assert.deepStrictEqual(calls.alerts, [
    "挂号失败：网络异常",
    "挂号失败：服务暂时不可用",
  ]);
  assert.strictEqual(calls.loads, 1, "失败请求不得刷新队列");
});
