// 回归:window.prompt() 在嵌入式浏览器/自动化环境可能不支持("prompt() is not
// supported")。appPrompt(message, defaultValue) 是应用内modal替代品，接口对齐
// window.prompt——resolve 输入字符串，取消/Esc resolve null。
// 抽出 helpers.js 的 appPrompt，stub 最小 document，沙箱 eval 真测交互路径。
// 跑：node --test test/web/test_app_prompt.mjs
import { test } from "node:test";
import assert from "node:assert";
import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import { dirname, join } from "node:path";
import vm from "node:vm";

const here = dirname(fileURLToPath(import.meta.url));
const src = readFileSync(join(here, "..", "..", "local_app", "static", "helpers.js"), "utf8");

function extract(fnHeader) {
  const start = src.indexOf(fnHeader);
  assert.ok(start > 0, `应能定位 ${fnHeader}`);
  const end = src.indexOf("\n}", start) + 2;
  return src.slice(start, end);
}

const code = extract("function escapeHtml") + "\n" + extract("function escapeAttr") + "\n" + extract("function appPrompt");

// 最小 document/element stub:够 appPrompt 用(createElement/appendChild/querySelector/
// addEventListener/remove/focus/select)，事件用最简单的手动分发(不依赖真实 DOM 事件循环)。
function makeSandbox() {
  const listeners = {docKeydown: []};
  function makeInput(value) {
    return {
      value,
      type: "text",
      autocomplete: "",
      focus() {},
      select() {},
    };
  }
  function makeEl(tag) {
    const el = {
      tag,
      _html: "",
      children: {},
      hidden: false,
      style: {},
      classList: {list: [], add(c) { this.list.push(c); }},
      set innerHTML(html) {
        this._html = html;
        const inputTag = (/<input\b[^>]*class="ord-input app-prompt-input"[^>]*>/.exec(html) || [""])[0];
        const attr = name => (new RegExp(`\\b${name}="([^"]*)"`).exec(inputTag) || [])[1];
        el.children.inputTag = inputTag;
        el.children.input = makeInput(attr("value") || "");
        el.children.input.type = attr("type") || "text";
        el.children.input.autocomplete = attr("autocomplete") || "";
        el.children.cancelBtn = {_clickHandlers: [], addEventListener(t, fn) { this._clickHandlers.push(fn); }, click() { this._clickHandlers.forEach(f => f()); }};
        el.children.okBtn = {_clickHandlers: [], addEventListener(t, fn) { this._clickHandlers.push(fn); }, click() { this._clickHandlers.forEach(f => f()); }};
      },
      get innerHTML() { return this._html; },
      querySelector(sel) {
        if (sel === ".app-prompt-input") return el.children.input;
        if (sel === '[data-ap="cancel"]') return el.children.cancelBtn;
        if (sel === '[data-ap="ok"]') return el.children.okBtn;
        return null;
      },
      removed: false,
      remove() { el.removed = true; },
    };
    return el;
  }
  const sandbox = {
    document: {
      createElement: () => makeEl("div"),
      body: {appendChild(el) { sandbox.__lastOverlay = el; }},
      addEventListener: (type, fn) => { if (type === "keydown") listeners.docKeydown.push(fn); },
      removeEventListener: (type, fn) => {
        if (type === "keydown") listeners.docKeydown = listeners.docKeydown.filter(f => f !== fn);
      },
    },
    __fireKeydown: key => listeners.docKeydown.slice().forEach(fn => fn({key})),
  };
  vm.createContext(sandbox);
  vm.runInContext(code + "\nthis.__appPrompt = appPrompt;", sandbox);
  return sandbox;
}

test("点确定 → resolve 输入框当前值", async () => {
  const sandbox = makeSandbox();
  const p = sandbox.__appPrompt("取消原因（必填）：", "");
  sandbox.__lastOverlay.children.input.value = "患者要求退款";
  sandbox.__lastOverlay.children.okBtn.click();
  const result = await p;
  assert.strictEqual(result, "患者要求退款");
  assert.strictEqual(sandbox.__lastOverlay.removed, true, "确认后应移除overlay");
});

test("点取消 → resolve null", async () => {
  const sandbox = makeSandbox();
  const p = sandbox.__appPrompt("撤销理由：", "");
  sandbox.__lastOverlay.children.cancelBtn.click();
  const result = await p;
  assert.strictEqual(result, null);
});

test("按 Escape → resolve null(对齐原生prompt取消语义)", async () => {
  const sandbox = makeSandbox();
  const p = sandbox.__appPrompt("请输入：", "");
  sandbox.__fireKeydown("Escape");
  const result = await p;
  assert.strictEqual(result, null);
});

test("按 Enter → resolve 输入框当前值(对齐原生prompt回车确认)", async () => {
  const sandbox = makeSandbox();
  const p = sandbox.__appPrompt("请输入：", "默认值");
  sandbox.__lastOverlay.children.input.value = "改过的值";
  sandbox.__fireKeydown("Enter");
  const result = await p;
  assert.strictEqual(result, "改过的值");
});

test("defaultValue 预填进输入框", async () => {
  const sandbox = makeSandbox();
  sandbox.__appPrompt("返回日期", "2026-07-05");
  assert.strictEqual(sandbox.__lastOverlay.children.input.value, "2026-07-05");
  assert.strictEqual(sandbox.__lastOverlay.children.input.type, "text");
  assert.strictEqual(sandbox.__lastOverlay.children.input.autocomplete, "off");
});

test("appPrompt overlay 可见且高于已知最高业务浮层", () => {
  const sandbox = makeSandbox();
  sandbox.__appPrompt("请输入：", "");
  const overlay = sandbox.__lastOverlay;
  assert.strictEqual(overlay.hidden, false);
  assert.strictEqual(overlay.removed, false);
  assert.ok(
    Number(overlay.style.zIndex) > 9000,
    `appPrompt z-index ${overlay.style.zIndex || "(未设置)"} 应高于已知最高浮层 9000`,
  );
});

test("password 模式隐藏输入并不在 HTML 预填 value", async () => {
  const sandbox = makeSandbox();
  const p = sandbox.__appPrompt("请输入当前密码：", "", {type: "password"});
  const {input, inputTag} = sandbox.__lastOverlay.children;
  assert.strictEqual(input.type, "password");
  assert.strictEqual(input.autocomplete, "current-password");
  assert.strictEqual(input.value, "");
  assert.doesNotMatch(inputTag, /\svalue=/i, "密码框不应生成可序列化 value 属性");

  input.value = "机密密码";
  sandbox.__lastOverlay.children.okBtn.click();
  assert.strictEqual(await p, "机密密码");
  assert.doesNotMatch(sandbox.__lastOverlay.innerHTML, /机密密码/);
});

test("password 的非空 defaultValue 只写 DOM property，Enter/确定都返回原值", async () => {
  const sentinel = "非空密码哨兵";
  for (const action of ["Enter", "确定"]) {
    const sandbox = makeSandbox();
    const p = sandbox.__appPrompt("请输入密码：", sentinel, {type: "password"});
    const {input, inputTag} = sandbox.__lastOverlay.children;
    assert.strictEqual(input.value, sentinel, `${action} 路径应保留 defaultValue`);
    assert.doesNotMatch(inputTag, /\svalue=/i);
    assert.doesNotMatch(sandbox.__lastOverlay.innerHTML, new RegExp(sentinel));

    if (action === "Enter") sandbox.__fireKeydown("Enter");
    else sandbox.__lastOverlay.children.okBtn.click();
    assert.strictEqual(await p, sentinel);
  }
});

test("显式 autocomplete 经属性转义，恶意 type 降级为 text", () => {
  const sandbox = makeSandbox();
  sandbox.__appPrompt("请输入：", "默认值", {
    type: 'password" autofocus onfocus="steal',
    autocomplete: 'off" data-injected="yes',
  });
  const {input, inputTag} = sandbox.__lastOverlay.children;
  assert.strictEqual(input.type, "text");
  assert.strictEqual(input.value, "默认值");
  assert.strictEqual(input.autocomplete, "off&quot; data-injected=&quot;yes");
  assert.match(inputTag, /autocomplete="off&quot; data-injected=&quot;yes"/);
  assert.doesNotMatch(inputTag, /autocomplete="off"\s+data-injected=/);
  assert.doesNotMatch(inputTag, /\sdata-injected="/);
});
