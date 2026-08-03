import { test } from "node:test";
import assert from "node:assert";
import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import { dirname, join } from "node:path";
import vm from "node:vm";

const here = dirname(fileURLToPath(import.meta.url));
const src = readFileSync(join(here, "..", "..", "local_app", "static", "today_work.js"), "utf8");

function deferred() {
  let resolve;
  let reject;
  const promise = new Promise((res, rej) => { resolve = res; reject = rej; });
  return {promise, resolve, reject};
}

function response(body) {
  const res = {ok: true, status: 200, json: async () => body};
  res.clone = () => response(body);
  return res;
}

// 后端 409 现在带机器可读 code；生产代码要先 clone 再读，桩必须提供 clone。
function conflict(code) {
  const body = {detail: {code, message: code}};
  const res = {ok: false, status: 409, json: async () => body};
  res.clone = () => conflict(code);
  return res;
}

function extractFunction(source, header) {
  const start = source.indexOf(header);
  assert.ok(start >= 0, `应能定位 ${header}`);
  const brace = source.indexOf("{", start);
  assert.ok(brace >= 0, `${header} 应有函数体`);
  let depth = 0;
  let quote = "";
  let escaped = false;
  let lineComment = false;
  let blockComment = false;
  for (let index = brace; index < source.length; index += 1) {
    const char = source[index];
    const next = source[index + 1];
    if (lineComment) { if (char === "\n") lineComment = false; continue; }
    if (blockComment) {
      if (char === "*" && next === "/") { blockComment = false; index += 1; }
      continue;
    }
    if (quote) {
      if (escaped) escaped = false;
      else if (char === "\\") escaped = true;
      else if (char === quote) quote = "";
      continue;
    }
    if (char === "/" && next === "/") { lineComment = true; index += 1; continue; }
    if (char === "/" && next === "*") { blockComment = true; index += 1; continue; }
    if (char === '"' || char === "'" || char === "`") { quote = char; continue; }
    if (char === "{") depth += 1;
    if (char === "}" && --depth === 0) return source.slice(start, index + 1);
  }
  assert.fail(`${header} 函数体不完整`);
}

function makeLoadTodayWorkHarness({panel = {textContent: ""}, todayResponse = response({summary: {}})} = {}) {
  const renders = [];
  const requests = [];
  const sandbox = {
    todayWorkPanel: panel,
    queueRooms: [],
    latestTodayWorkData: null,
    workDate: {value: "2026-08-02"},
    encodeURIComponent,
    renderTodayWork(data) { renders.push(data); },
    fetch: async url => {
      requests.push(url);
      if (url === "/api/settings/rooms") return response({list: []});
      return todayResponse;
    },
  };
  vm.createContext(sandbox);
  const code = extractFunction(src, "async function loadTodayWork");
  vm.runInContext(`${code}\nthis.__loadTodayWork = loadTodayWork;`, sandbox);
  return {sandbox, renders, requests};
}

// 建档幂等号存在 localStorage，跨"关页面重开"必须还在，所以 storage 可由调用方传入复用。
function fakeStorage() {
  const map = new Map();
  return {
    getItem: key => (map.has(key) ? map.get(key) : null),
    setItem: (key, value) => { map.set(key, String(value)); },
    removeItem: key => { map.delete(key); },
    size: () => map.size,
  };
}

function makeHarness({storage = fakeStorage()} = {}) {
  const nodes = new Map();
  const node = id => {
    if (!nodes.has(id)) {
      nodes.set(id, {
        id,
        value: "",
        textContent: "",
        innerHTML: "",
        hidden: false,
        disabled: false,
        style: {},
        focus() {},
        classList: {add() {}, remove() {}, toggle() {}},
      });
    }
    return nodes.get(id);
  };
  node("npName").value = "合成患者";
  node("npPhone").value = "x";
  node("newPatientModal").hidden = true;
  const submitButtons = [node("npSavePatientBtn"), node("npRegisterPatientBtn"), node("npRegisterTriagePatientBtn")];
  const closeButtons = [node("npClosePatientBtn"), node("npCancelPatientBtn")];
  const patientPosts = [];
  const checkIns = [];
  let patientImpl = async () => response({patient_identity: "local-pat-synthetic"});
  let checkInImpl = async () => ({appointment: {appointment_id: "a1"}});
  let ocrImpl = async () => response({fields: {}, confident: [], lines: [], warnings: [], elapsed_ms: 1});
  let ocrFetches = 0;
  let todayLoads = 0;

  const sandbox = {
    console,
    AbortController,
    // LAN HTTP 不是安全上下文：只提供 getRandomValues，故意不提供 randomUUID。
    crypto: {
      getRandomValues(bytes) {
        bytes.forEach((_, index) => { bytes[index] = index + 1; });
        return bytes;
      },
    },
    document: {
      getElementById: id => node(id),
      querySelector: selector => selector.includes("npSex") ? {value: "男"} : null,
      querySelectorAll: selector => {
        const out = [];
        if (selector.includes("data-np-submit")) out.push(...submitButtons);
        if (selector.includes("data-np-close")) out.push(...closeButtons);
        return out;
      },
      createElement: () => node("created"),
      body: {appendChild() {}},
    },
    fetch: async (url, opts = {}) => {
      if (url === "/api/patients") {
        patientPosts.push(JSON.parse(opts.body));
        return patientImpl();
      }
      if (url === "/api/ocr/patient-card") {
        ocrFetches += 1;
        return ocrImpl();
      }
      if (url === "/api/ocr/status") return response({available: true});
      return response({});
    },
    checkInPatient: async options => {
      checkIns.push(options);
      return checkInImpl(options);
    },
    escapeHtml: value => String(value ?? ""),
    escapeAttr: value => String(value ?? ""),
    referralSlotHtml: () => "",
    bjToday: () => new Date("2026-08-02T00:00:00Z"),
    bindDictInputs() {},
    loadTodayWork: async () => { todayLoads += 1; },
    encodeURIComponent,
    FormData: class { append() {} },
    URL: {createObjectURL: () => "blob:test"},
    window: {alert() {}},
    localStorage: storage,
    setTimeout,
    clearTimeout,
  };
  vm.createContext(sandbox);
  vm.runInContext(src, sandbox);
  // today_work.js 自身声明了 loadTodayWork，加载后替换为可观测桩。
  sandbox.loadTodayWork = async () => { todayLoads += 1; };
  return {
    sandbox,
    node,
    submitButtons,
    closeButtons,
    patientPosts,
    checkIns,
    storage,
    // 默认 getRandomValues 桩是定值，会让两次生成的号撞在一起；需要区分新旧号的用例用它。
    // 跨 harness（模拟关页面重开）必须给不同 startSeed，否则两边生成同一个号，测试会假绿。
    varyUuid(startSeed = 0) {
      let seed = startSeed;
      sandbox.crypto.getRandomValues = bytes => {
        seed += 1;
        bytes.forEach((_, index) => { bytes[index] = (index + seed) & 0xff; });
        return bytes;
      };
    },
    setPatientImpl(fn) { patientImpl = fn; },
    setCheckInImpl(fn) { checkInImpl = fn; },
    setOcrImpl(fn) { ocrImpl = fn; },
    ocrFetches: () => ocrFetches,
    todayLoads: () => todayLoads,
  };
}

function open(h) {
  h.sandbox.window.openNewPatient();
  return Promise.resolve();
}

test("loadTodayWork 没有面板时返回 false", async () => {
  const h = makeLoadTodayWorkHarness({panel: null});
  assert.strictEqual(await h.sandbox.__loadTodayWork(), false);
  assert.strictEqual(h.requests.length, 0);
});

test("loadTodayWork 遇到 today-work HTTP 500 时返回 false", async () => {
  const panel = {textContent: ""};
  const h = makeLoadTodayWorkHarness({
    panel,
    todayResponse: {ok: false, status: 500, json: async () => ({})},
  });
  assert.strictEqual(await h.sandbox.__loadTodayWork(), false);
  assert.strictEqual(panel.textContent, "今日工作载入失败");
  assert.strictEqual(h.renders.length, 0);
});

test("loadTodayWork 成功渲染后返回 true", async () => {
  const data = {summary: {appointments_today: 2}, appointments: []};
  const h = makeLoadTodayWorkHarness({todayResponse: response(data)});
  assert.strictEqual(await h.sandbox.__loadTodayWork(), true);
  assert.deepStrictEqual(JSON.parse(JSON.stringify(h.renders)), [data]);
});

test("新建患者三个动作使用各自模式", () => {
  assert.match(src, /submitNewPatient\(["']save["']\)/);
  assert.match(src, /submitNewPatient\(["']register["']\)/);
  assert.match(src, /submitNewPatient\(["']register-triage["']\)/);
});

test("LAN HTTP 只有 getRandomValues 时仍能生成合法 v4 request_id", async () => {
  const h = makeHarness();
  await open(h);
  await h.sandbox.window.submitNewPatient("save");
  assert.match(
    h.patientPosts[0].request_id,
    /^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/,
  );
});

test("仅保存不挂号，保存并挂号与挂号并分诊传入精确选项", async () => {
  const save = makeHarness();
  await open(save);
  await save.sandbox.window.submitNewPatient("save");
  assert.strictEqual(save.patientPosts.length, 1);
  assert.strictEqual(save.checkIns.length, 0);
  assert.strictEqual(save.todayLoads(), 1);
  assert.strictEqual(save.node("newPatientModal").hidden, true);

  const register = makeHarness();
  await open(register);
  await register.sandbox.window.submitNewPatient("register");
  assert.deepStrictEqual(JSON.parse(JSON.stringify(register.checkIns)), [{
    patientIdentity: "local-pat-synthetic",
    displayName: "合成患者",
    openTriageAfter: false,
    switchToToday: true,
    showSuccess: true,
  }]);

  const triage = makeHarness();
  await open(triage);
  await triage.sandbox.window.submitNewPatient("register-triage");
  assert.strictEqual(triage.checkIns.length, 1);
  assert.strictEqual(triage.checkIns[0].openTriageAfter, true);
  assert.strictEqual(triage.checkIns[0].switchToToday, true);
});

test("提交中双击只 POST 一次，三个动作和关闭统一禁用", async () => {
  const h = makeHarness();
  const gate = deferred();
  h.setPatientImpl(() => gate.promise);
  await open(h);
  const first = h.sandbox.window.submitNewPatient("register");
  const second = h.sandbox.window.submitNewPatient("register");
  await Promise.resolve();
  assert.strictEqual(h.patientPosts.length, 1);
  assert.ok(h.submitButtons.every(button => button.disabled));
  assert.ok(h.closeButtons.every(button => button.disabled));
  gate.resolve(response({patient_identity: "local-pat-synthetic"}));
  await Promise.all([first, second]);
  assert.ok(h.submitButtons.every(button => !button.disabled));
  assert.ok(h.closeButtons.every(button => !button.disabled));
});

test("患者 POST 挂起时 OCR fail-closed，重试载荷保持不变", async () => {
  const h = makeHarness();
  const firstResponse = deferred();
  let attempt = 0;
  h.setPatientImpl(() => {
    attempt += 1;
    if (attempt === 1) return firstResponse.promise;
    return response({patient_identity: "local-pat-synthetic", replayed: true});
  });
  h.setOcrImpl(async () => response({
    fields: {display_name: "OCR 漂移姓名"},
    confident: ["display_name"],
    lines: [],
    warnings: [],
    elapsed_ms: 1,
  }));
  await open(h);

  const firstSubmit = h.sandbox.window.submitNewPatient("register");
  await Promise.resolve();
  const ocrDisabledWhileSubmitting = h.node("npOcrInput").disabled;
  await h.sandbox.window.onNewPatientOcrPick({
    value: "queued.jpg",
    files: [{type: "image/jpeg", name: "queued.jpg"}],
  });
  firstResponse.reject(new Error("response lost"));
  await firstSubmit;
  await h.sandbox.window.submitNewPatient("register");

  assert.strictEqual(ocrDisabledWhileSubmitting, true, "患者提交期间 OCR input 必须禁用");
  assert.strictEqual(h.ocrFetches(), 0, "程序化或已排队 change 不得绕过提交锁启动 OCR");
  assert.strictEqual(h.node("npName").value, "合成患者", "提交中的 OCR 不得改建档字段");
  assert.strictEqual(h.patientPosts.length, 2);
  assert.strictEqual(h.patientPosts[0].request_id, h.patientPosts[1].request_id);
  assert.deepStrictEqual(h.patientPosts[1], h.patientPosts[0], "同 request_id 重试载荷不得漂移");
});

test("建档响应丢失后重试复用同一 request_id", async () => {
  const h = makeHarness();
  let attempt = 0;
  h.setPatientImpl(async () => {
    attempt += 1;
    if (attempt === 1) throw new Error("response lost");
    return response({patient_identity: "local-pat-synthetic", replayed: true});
  });
  await open(h);
  await h.sandbox.window.submitNewPatient("register");
  await h.sandbox.window.submitNewPatient("register");
  assert.strictEqual(h.patientPosts.length, 2);
  assert.strictEqual(h.patientPosts[0].request_id, h.patientPosts[1].request_id);
  assert.strictEqual(h.patientPosts[0].request_id, "01020304-0506-4708-890a-0b0c0d0e0f10");
  assert.strictEqual(h.checkIns.length, 1);
});

test("患者已保存但挂号失败时，重试只调用挂号", async () => {
  const h = makeHarness();
  let attempt = 0;
  h.setCheckInImpl(async () => {
    attempt += 1;
    return attempt === 1 ? null : {appointment: {appointment_id: "a1"}};
  });
  await open(h);
  await h.sandbox.window.submitNewPatient("register");
  assert.strictEqual(h.patientPosts.length, 1);
  assert.strictEqual(h.node("newPatientModal").hidden, false);
  assert.match(h.node("npStatus").textContent, /患者已保存，后续操作未完成/);
  await h.sandbox.window.submitNewPatient("register");
  assert.strictEqual(h.patientPosts.length, 1, "下游重试不得再 POST 患者");
  assert.strictEqual(h.checkIns.length, 2);
  assert.strictEqual(h.node("newPatientModal").hidden, true);
});

test("OCR 在途时三个动作和关闭统一禁用", async () => {
  const h = makeHarness();
  const gate = deferred();
  h.setOcrImpl(() => gate.promise);
  await open(h);
  const pending = h.sandbox.window.onNewPatientOcrPick({value: "", files: [{type: "image/jpeg"}]});
  await Promise.resolve();
  assert.ok(h.submitButtons.every(button => button.disabled));
  assert.ok(h.closeButtons.every(button => button.disabled));
  gate.resolve(response({fields: {}, confident: [], lines: [], warnings: [], elapsed_ms: 1}));
  await pending;
  assert.ok(h.submitButtons.every(button => !button.disabled));
  assert.ok(h.closeButtons.every(button => !button.disabled));
});

test("建档响应丢失后关页面重开，复用同一个幂等号（不重复建患者）", async () => {
  const storage = fakeStorage();
  const first = makeHarness({storage});
  first.varyUuid();
  first.setPatientImpl(async () => { throw new Error("响应丢在路上"); });
  await open(first);
  await first.sandbox.window.submitNewPatient("save");
  assert.strictEqual(first.patientPosts.length, 1);

  // 关掉标签页重开：同一个 localStorage，重新填一遍
  const second = makeHarness({storage});
  second.varyUuid(100);
  await open(second);
  await second.sandbox.window.submitNewPatient("save");
  assert.strictEqual(
    second.patientPosts[0].request_id,
    first.patientPosts[0].request_id,
    "重开后必须复用上次的号，让后端重放命中同一位患者",
  );
});

test("建档成功后作废幂等号，下一位患者拿到新号", async () => {
  const storage = fakeStorage();
  const first = makeHarness({storage});
  first.varyUuid();
  await open(first);
  await first.sandbox.window.submitNewPatient("save");
  assert.strictEqual(storage.size(), 0, "落库后号必须清掉");

  const second = makeHarness({storage});
  second.varyUuid(100);
  await open(second);
  await second.sandbox.window.submitNewPatient("save");
  assert.notStrictEqual(second.patientPosts[0].request_id, first.patientPosts[0].request_id);
});

test("同号不同内容收到 409 时换新号重试，不卡死", async () => {
  const h = makeHarness();
  h.varyUuid();
  let calls = 0;
  h.setPatientImpl(async () => {
    calls += 1;
    return calls === 1
      ? conflict("request_id_payload_conflict")
      : response({patient_identity: "local-pat-retry"});
  });
  await open(h);
  await h.sandbox.window.submitNewPatient("save");
  assert.strictEqual(h.patientPosts.length, 2, "409 后应自动换号重试一次");
  assert.notStrictEqual(h.patientPosts[1].request_id, h.patientPosts[0].request_id);
  assert.strictEqual(h.node("newPatientModal").hidden, true, "重试成功后弹窗应关闭");
});

test("非载荷冲突的 409 必须停下报错，绝不换号重发（换号会建出第二份档案）", async () => {
  for (const code of ["request_id_snapshot_broken", "chart_no_conflict"]) {
    const h = makeHarness();
    h.varyUuid();
    h.setPatientImpl(async () => conflict(code));
    await open(h);
    await h.sandbox.window.submitNewPatient("save");
    assert.strictEqual(h.patientPosts.length, 1, `${code} 不该重发第二次 POST`);
    assert.match(h.node("npStatus").textContent, /保存失败/, `${code} 应明确报错`);
    assert.strictEqual(h.node("newPatientModal").hidden, false, `${code} 不该关掉弹窗`);
  }
});

test("浏览器禁用本地存储时明确告警，不静默降级", async () => {
  const blocked = {
    getItem() { throw new Error("SecurityError: localStorage 被禁用"); },
    setItem() { throw new Error("SecurityError: localStorage 被禁用"); },
    removeItem() { throw new Error("SecurityError: localStorage 被禁用"); },
    size: () => 0,
  };
  const h = makeHarness({storage: blocked});
  await open(h);
  assert.match(
    h.node("npStatus").textContent,
    /禁用了本地存储|防重复建档保护不可用/,
    "存储不可用必须让操作员看见，不能悄悄退化",
  );
  // 仍可建档：直接封死会让隐私模式下前台完全没法录患者
  await h.sandbox.window.submitNewPatient("save");
  assert.strictEqual(h.patientPosts.length, 1);
});
