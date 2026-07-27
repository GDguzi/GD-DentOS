import { test } from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";
import vm from "node:vm";

const here = dirname(fileURLToPath(import.meta.url));
const staticDir = join(here, "..", "..", "local_app", "static");
const contractSource = readFileSync(join(staticDir, "customer_hub_v2_contract.js"), "utf8");
const viewSource = readFileSync(join(staticDir, "customer_hub_v2.js"), "utf8");

class FakeButton {
  constructor(document = null) {
    this.ownerDocument = document;
    this.tagName = "BUTTON";
    this.disabled = false;
    this.hidden = false;
    this.isConnected = true;
    this.attributes = new Map();
    this.listeners = new Map();
    this.dataset = {};
  }

  addEventListener(type, listener) {
    this.listeners.set(type, listener);
  }

  setAttribute(name, value) {
    this.attributes.set(name, String(value));
    if (name === "disabled") this.disabled = true;
    if (name === "hidden") this.hidden = true;
  }
  getAttribute(name) { return this.attributes.get(name) ?? null; }
  focus() {
    if (!this.ownerDocument || this.isConnected === false || this.disabled || this.hidden
        || this.getAttribute("aria-hidden") === "true") return;
    this.ownerDocument.activeElement = this;
  }
}

class FakeDialog {
  constructor(document = null) {
    this.ownerDocument = document;
    this.tagName = "DIV";
    this.disabled = false;
    this.hidden = false;
    this.isConnected = true;
    this.attributes = new Map();
    this.heading = {tagName: "H3", id: "", setAttribute(name, value) { if (name === "id") this.id = String(value); }};
  }

  setAttribute(name, value) {
    this.attributes.set(name, String(value));
    if (name === "disabled") this.disabled = true;
    if (name === "hidden") this.hidden = true;
  }
  getAttribute(name) { return this.attributes.get(name) ?? null; }
  querySelector(selector) { return selector.includes("h3") ? this.heading : null; }
  focus() {
    if (!this.ownerDocument || this.isConnected === false || this.disabled || this.hidden
        || this.getAttribute("aria-hidden") === "true"
        || this.getAttribute("tabindex") === null) return;
    this.ownerDocument.activeElement = this;
  }
}

class FakeOverlay {
  constructor(document = null) {
    this.ownerDocument = document;
    this.id = "";
    this.className = "";
    this.removed = false;
    this.listeners = new Map();
    this.buttons = new Map();
    this.dayButtons = [];
    this.dialog = null;
    this.html = "";
  }

  set innerHTML(value) {
    this.html = String(value);
    this.dialog = new FakeDialog(this.ownerDocument);
    for (const name of ["close", "mrprev", "mrnext", "mprev", "mnext", "mtoday"]) {
      if (this.html.includes(`data-chv-${name}`)) {
        this.buttons.set(`[data-chv-${name}]`, new FakeButton(this.ownerDocument));
      }
    }
    this.dayButtons = [...this.html.matchAll(/data-chv-day="([^"]+)"/g)].map(match => {
      const button = new FakeButton(this.ownerDocument);
      button.dataset.chvDay = match[1];
      return button;
    });
  }

  get innerHTML() {
    return this.html;
  }

  addEventListener(type, listener) {
    this.listeners.set(type, listener);
  }

  querySelector(selector) {
    if (selector === ".chv-pop") return this.dialog;
    return this.buttons.get(selector) || null;
  }

  querySelectorAll(selector) {
    if (selector === "button, a[href], input, select, textarea, [tabindex]") {
      return [...this.buttons.values()];
    }
    if (selector === "[data-chv-day]") return this.dayButtons;
    const button = this.buttons.get(selector);
    return button ? [button] : [];
  }

  remove() {
    this.removed = true;
    this.isConnected = false;
  }
}

function validSummary(overrides = {}) {
  return {
    total: 7,
    completed: 3,
    pending: 4,
    overdue: 1,
    completion_rate: 42.9,
    channel_counts: [
      {channel: "phone", count: 3},
      {channel: "wechat", count: 2},
      {channel: "other", count: 1},
      {channel: "unknown", count: 1},
    ],
    problem_counts: {needs_attention: 2, overdue: 1, low_satisfaction: 1},
    top_results: [
      {label: "已预约复诊", count: 3},
      {label: "恢复良好", count: 2},
    ],
    narrative: {status: "disabled", text: ""},
    ...overrides,
  };
}

function zeroSummary() {
  return {
    total: 0,
    completed: 0,
    pending: 0,
    overdue: 0,
    completion_rate: 0,
    channel_counts: [
      {channel: "phone", count: 0},
      {channel: "wechat", count: 0},
      {channel: "other", count: 0},
      {channel: "unknown", count: 0},
    ],
    problem_counts: {needs_attention: 0, overdue: 0, low_satisfaction: 0},
    top_results: [],
    narrative: {status: "disabled", text: ""},
  };
}

function makeHarness({response, fetchError = false, workDateValue = "2026-07-21"} = {}) {
  const requests = [];
  const alerts = [];
  const overlays = [];
  const document = {
    activeElement: null,
    addEventListener() {},
    createElement() { return new FakeOverlay(document); },
    getElementById(id) {
      return overlays.find(overlay => overlay.id === id && !overlay.removed) || null;
    },
    body: {
      appendChild(overlay) { overlays.push(overlay); },
    },
  };
  const sandbox = {
    console,
    document,
    moduleViewToken: 0,
    workDate: {value: workDateValue},
    localDateValue: () => workDateValue,
    hasPerm: () => true,
    escapeHtml: value => String(value),
    escapeAttr: value => String(value),
    fetch: async (url, init) => {
      requests.push({url, init});
      if (fetchError) {
        return {ok: false, status: 500, async json() { return {}; }};
      }
      const responseBody = typeof response === "function" ? response(url, init) : response;
      return {
        ok: true,
        status: 200,
        async json() { return responseBody; },
      };
    },
    alert: message => alerts.push(message),
    confirm: () => true,
  };
  sandbox.window = sandbox;
  vm.createContext(sandbox);
  vm.runInContext(contractSource, sandbox);
  vm.runInContext(
    `${viewSource}\n`
      + "globalThis.__openMonthlyReview = _chvOpenMonthlyReview;\n"
      + "globalThis.__openCalendar = _chvOpenCalendar;\n"
      + "globalThis.__setMonthlyFilters = value => { _chvF = value; };",
    sandbox,
  );
  return {sandbox, requests, alerts, overlays};
}

test("月历弹卡只渲染合同层验证后的 done 日格", async () => {
  const harness = makeHarness({response: {
    month: "2026-07",
    days: [{
      date: "2026-07-08", count: 3, done: 2, pending: 1, overdue: 0, completed: 99,
    }],
    total: 3,
    summary: validSummary({total: 3, completed: 2, pending: 1, overdue: 0,
      completion_rate: 66.7, channel_counts: [
        {channel: "phone", count: 3},
        {channel: "wechat", count: 0},
        {channel: "other", count: 0},
        {channel: "unknown", count: 0},
      ]}),
  }});

  await harness.sandbox.__openCalendar();

  assert.deepEqual(harness.alerts, []);
  assert.equal(harness.requests.length, 1);
  assert.equal(harness.requests[0].url, "/api/return-visits/calendar?month=2026-07");
  assert.equal(harness.overlays.length, 1);
  assert.match(harness.overlays[0].innerHTML, /title="待1·已2·逾0">3<\/span>/);
  assert.doesNotMatch(harness.overlays[0].innerHTML, /99/);
});

test("月历弹卡接受合法空 days，不把真实空月份当成载入失败", async () => {
  const harness = makeHarness({response: {month: "2026-07", days: []}});

  await harness.sandbox.__openCalendar();

  assert.deepEqual(harness.alerts, []);
  assert.equal(harness.requests.length, 1);
  assert.equal(harness.overlays.length, 1);
  assert.doesNotMatch(harness.overlays[0].innerHTML, /class="chv-cnt"/);
});

test("月历弹卡对 0001 年使用真实 Gregorian 星期并保持同世纪翻月", async () => {
  const harness = makeHarness({
    workDateValue: "0001-01-15",
    response: url => ({month: /month=([^&]+)/.exec(url)[1], days: []}),
  });

  await harness.sandbox.__openCalendar();

  assert.equal(
    (harness.overlays[0].innerHTML.match(/class="chv-day empty"/g) || []).length,
    0,
    "0001-01-01 是星期一，前面不应补空格",
  );
  harness.overlays[0].buttons.get("[data-chv-mnext]").listeners.get("click")();
  await new Promise(resolve => setTimeout(resolve, 0));
  assert.equal(harness.requests[1].url, "/api/return-visits/calendar?month=0001-02");
});

test("月历弹卡遇畸形 days 明确失败且不渲染半套日格", async t => {
  for (const [label, response] of [
    ["缺少 days", {month: "2026-07"}],
    ["合法与坏日格混合", {month: "2026-07", days: [
      {date: "2026-07-07", count: 1, done: 1, pending: 0, overdue: 0},
      {date: "2026-07-08", count: "3", done: 2, pending: 1, overdue: 0},
    ]}],
    ["日期越界", {month: "2026-07", days: [
      {date: "2026-07-32", count: 3, done: 2, pending: 1, overdue: 0},
    ]}],
  ]) await t.test(label, async () => {
    const harness = makeHarness({response});

    await harness.sandbox.__openCalendar();

    assert.deepEqual(harness.alerts, ["月历载入失败"]);
    assert.equal(harness.requests.length, 1);
    assert.equal(harness.overlays.length, 0);
  });
});

test("月度回顾只发一次 GET calendar 并继承当前可用筛选", async () => {
  const harness = makeHarness({response: {summary: validSummary()}});
  harness.sandbox.__setMonthlyFilters({
    q: "合成 关键字",
    status: "已回访",
    doctor: "合成医生",
    visitor: "合成回访人",
    channel: "phone",
    pageno: 9,
    pagesize: 200,
  });

  await harness.sandbox.__openMonthlyReview("2026-07");

  assert.equal(harness.requests.length, 1);
  assert.equal(
    harness.requests[0].url,
    "/api/return-visits/calendar?month=2026-07&q=%E5%90%88%E6%88%90%20%E5%85%B3%E9%94%AE%E5%AD%97&status=%E5%B7%B2%E5%9B%9E%E8%AE%BF&doctor=%E5%90%88%E6%88%90%E5%8C%BB%E7%94%9F&visitor=%E5%90%88%E6%88%90%E5%9B%9E%E8%AE%BF%E4%BA%BA&channel=phone",
  );
  assert.equal(harness.requests[0].init.method, "GET");
  assert.doesNotMatch(harness.requests[0].url, /pageno|pagesize|\/api\/return-visits\?/);
});

test("月度查询对特殊字符稳定编码，不串改参数边界", async () => {
  const harness = makeHarness({response: {summary: validSummary()}});
  harness.sandbox.__setMonthlyFilters({
    q: " a&b = c ",
    status: "x/y?z",
    doctor: "合成&医生",
    visitor: "A+B",
    channel: "phone",
  });

  await harness.sandbox.__openMonthlyReview("2026-07");

  assert.equal(
    harness.requests[0].url,
    "/api/return-visits/calendar?month=2026-07&q=a%26b%20%3D%20c&status=x%2Fy%3Fz&doctor=%E5%90%88%E6%88%90%26%E5%8C%BB%E7%94%9F&visitor=A%2BB&channel=phone",
  );
});

test("月度查询将孤立代理项转换为替换字符，合法代理对保持不变", async t => {
  for (const [label, value, encoded] of [
    ["lone high surrogate", "\uD800", "%EF%BF%BD"],
    ["lone low surrogate", "\uDC00", "%EF%BF%BD"],
    ["valid surrogate pair", "\uD83D\uDE00", "%F0%9F%98%80"],
  ]) await t.test(label, async () => {
    const harness = makeHarness({response: {summary: validSummary()}});
    harness.sandbox.__setMonthlyFilters({
      q: "",
      status: "",
      doctor: value,
      visitor: "",
      channel: "",
    });

    await assert.doesNotReject(() => harness.sandbox.__openMonthlyReview("2026-07"));

    assert.equal(
      harness.requests[0].url,
      `/api/return-visits/calendar?month=2026-07&doctor=${encoded}`,
    );
  });
});

test("合法 summary 原样展示 3/7、42.9%、方式、问题和高频结果", async () => {
  const harness = makeHarness({response: {
    days: [{date: "2026-07-01", count: 999}],
    list: [{display_name: "不应渲染的合成明细"}],
    summary: validSummary(),
  }});

  await harness.sandbox.__openMonthlyReview("2026-07");

  assert.equal(harness.alerts.length, 0);
  assert.equal(harness.overlays.length, 1);
  const html = harness.overlays[0].innerHTML;
  assert.match(html, /已完成 3 \/ 7/);
  assert.match(html, /42\.9%/);
  assert.match(html, /电话[\s\S]*?3/);
  assert.match(html, /微信[\s\S]*?2/);
  assert.match(html, /需关注[\s\S]*?2/);
  assert.match(html, /低满意度[\s\S]*?1/);
  assert.match(html, /已预约复诊[\s\S]*?3/);
  assert.match(html, /恢复良好[\s\S]*?2/);
  assert.doesNotMatch(html, /不应渲染的合成明细/);
});

test("缺失 summary 明确失败且不渲染假 0", async () => {
  const harness = makeHarness({response: {days: [], total: 0}});

  await harness.sandbox.__openMonthlyReview("2026-07");

  assert.deepEqual(harness.alerts, ["客户通载入失败"]);
  assert.equal(harness.overlays.length, 0);
  assert.equal(harness.requests.length, 1);
});

test("月度响应的继承属性和 accessor 一律不读取", async t => {
  let getterReads = 0;
  const accessorPayload = {};
  Object.defineProperty(accessorPayload, "summary", {
    configurable: true,
    enumerable: true,
    get() {
      getterReads += 1;
      return validSummary();
    },
  });
  const accessorSummary = validSummary();
  Object.defineProperty(accessorSummary, "completed", {
    configurable: true,
    enumerable: true,
    get() {
      getterReads += 1;
      return 3;
    },
  });
  const accessorChannel = {};
  Object.defineProperties(accessorChannel, {
    channel: {
      configurable: true,
      enumerable: true,
      get() {
        getterReads += 1;
        return "phone";
      },
    },
    count: {configurable: true, enumerable: true, value: 3},
  });
  const nestedAccessor = validSummary({
    channel_counts: [accessorChannel, ...validSummary().channel_counts.slice(1)],
  });
  const cases = [
    ["继承 summary", Object.create({summary: validSummary()})],
    ["summary accessor", accessorPayload],
    ["必填字段 accessor", {summary: accessorSummary}],
    ["嵌套字段 accessor", {summary: nestedAccessor}],
  ];

  for (const [label, response] of cases) {
    await t.test(label, async () => {
      const harness = makeHarness({response});
      await harness.sandbox.__openMonthlyReview("2026-07");
      assert.deepEqual(harness.alerts, ["客户通载入失败"]);
      assert.equal(harness.overlays.length, 0);
      assert.equal(harness.requests.length, 1);
    });
  }
  assert.equal(getterReads, 0);
});

test("合法全零月份是真实空结果，不按缺失 summary 处理", async () => {
  const harness = makeHarness({response: {summary: zeroSummary()}});

  await harness.sandbox.__openMonthlyReview("2026-07");

  assert.deepEqual(harness.alerts, []);
  assert.equal(harness.requests.length, 1);
  assert.equal(harness.overlays.length, 1);
  assert.match(harness.overlays[0].innerHTML, /本月回访[\s\S]*?<b>0<\/b>/);
  assert.match(harness.overlays[0].innerHTML, /已完成 0 \/ 0/);
  assert.match(harness.overlays[0].innerHTML, /0\.0%/);
});

test("summary 畸形类型与嵌套合同一律 fail closed", async t => {
  const malformed = [
    ["总量不是安全整数", validSummary({total: "7"})],
    ["完成率不是有限数", validSummary({completion_rate: Number.POSITIVE_INFINITY})],
    ["方式桶缺项", validSummary({channel_counts: validSummary().channel_counts.slice(0, 3)})],
    ["方式计数类型错误", validSummary({channel_counts: [
      {channel: "phone", count: "3"},
      {channel: "wechat", count: 2},
      {channel: "other", count: 1},
      {channel: "unknown", count: 1},
    ]})],
    ["问题三项缺失", validSummary({problem_counts: {needs_attention: 2, overdue: 1}})],
    ["高频结果计数不是正整数", validSummary({top_results: [{label: "合成结果", count: 0}]})],
    ["摘要状态非法", validSummary({narrative: {status: "unknown", text: ""}})],
    ["摘要文本不是字符串", validSummary({narrative: {status: "ready", text: null}})],
  ];

  for (const [label, summary] of malformed) {
    await t.test(label, async () => {
      const harness = makeHarness({response: {summary}});
      await harness.sandbox.__openMonthlyReview("2026-07");
      assert.deepEqual(harness.alerts, ["客户通载入失败"]);
      assert.equal(harness.overlays.length, 0);
      assert.equal(harness.requests.length, 1);
    });
  }
});

test("summary 计数不变量失败时不渲染", async t => {
  const malformed = [
    ["total 不等于 completed 加 pending", validSummary({total: 8})],
    ["overdue 超过 pending", validSummary({pending: 0, total: 3, overdue: 1})],
    ["方式桶重复", validSummary({channel_counts: [
      {channel: "phone", count: 3},
      {channel: "phone", count: 2},
      {channel: "other", count: 1},
      {channel: "unknown", count: 1},
    ]})],
    ["方式计数和不等于 total", validSummary({channel_counts: [
      {channel: "phone", count: 2},
      {channel: "wechat", count: 2},
      {channel: "other", count: 1},
      {channel: "unknown", count: 1},
    ]})],
  ];

  for (const [label, summary] of malformed) {
    await t.test(label, async () => {
      const harness = makeHarness({response: {summary}});
      await harness.sandbox.__openMonthlyReview("2026-07");
      assert.deepEqual(harness.alerts, ["客户通载入失败"]);
      assert.equal(harness.overlays.length, 0);
      assert.equal(harness.requests.length, 1);
    });
  }
});

test("calendar 请求失败明确显示客户通载入失败且不重试", async () => {
  const harness = makeHarness({fetchError: true});

  await harness.sandbox.__openMonthlyReview("2026-07");

  assert.deepEqual(harness.alerts, ["客户通载入失败"]);
  assert.equal(harness.overlays.length, 0);
  assert.equal(harness.requests.length, 1);
});
