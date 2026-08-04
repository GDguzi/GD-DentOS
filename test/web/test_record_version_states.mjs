// GD-10:版本区状态规则——空ID/new sentinel 不发请求显示"暂无";200 空列表="暂无";
// HTTP/网络失败="失败+重试"(带状态码,不伪装暂无);重试复用原参数;旧请求不覆盖新结果。
import { test } from "node:test";
import assert from "node:assert";
import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import { dirname, join } from "node:path";
import vm from "node:vm";

const here = dirname(fileURLToPath(import.meta.url));
const src = readFileSync(join(here, "..", "..", "local_app", "static", "workspace_tabs.js"), "utf8");

function section(text, startMarker, endMarker) {
  const start = text.indexOf(startMarker);
  assert.ok(start >= 0, `missing ${startMarker}`);
  const end = text.indexOf(endMarker, start + startMarker.length);
  assert.ok(end > start, `missing ${endMarker}`);
  return text.slice(start, end);
}

const core = section(src, "async function loadRecordVersions", "\nasync function");

function deferred() {
  let resolve, reject;
  const promise = new Promise((res, rej) => { resolve = res; reject = rej; });
  return { promise, resolve, reject };
}

function makeContainer() {
  return {
    dataset: {},
    textContent: "",
    _html: "",
    _btn: null,
    set innerHTML(v) {
      this._html = v;
      this._btn = v.includes("data-version-retry")
        ? { listeners: {}, addEventListener(ev, fn) { this.listeners[ev] = fn; } }
        : null;
    },
    get innerHTML() { return this._html; },
    querySelector(sel) { return sel === "[data-version-retry]" ? this._btn : null; },
  };
}

function makeSandbox() {
  const fetches = [];
  const sandbox = {
    encodeURIComponent,
    escapeHtml: v => String(v ?? ""),
    renderVersionRow: row => `<row>${row.version_id}</row>`,
    fetch: url => {
      const d = deferred();
      fetches.push({ url, ...d });
      return d.promise;
    },
  };
  vm.createContext(sandbox);
  vm.runInContext(core + "\nthis.__load = loadRecordVersions;", sandbox);
  return { sandbox, fetches };
}

const ok = body => ({ ok: true, status: 200, json: async () => body });

test("GD-10:空ID 与 new sentinel 不发请求,显示暂无", async () => {
  for (const id of ["", "new"]) {
    const { sandbox, fetches } = makeSandbox();
    const c = makeContainer();
    await sandbox.__load("medical_record", id, c);
    assert.strictEqual(fetches.length, 0, `entityId=${JSON.stringify(id)} 不得发版本请求`);
    assert.ok(c._html.includes("暂无本地版本记录"), `entityId=${JSON.stringify(id)} 应显示暂无`);
    assert.ok(!c._html.includes("失败") && !c.textContent.includes("失败"));
  }
});

test("GD-10:正式ID 200 空列表显示暂无", async () => {
  const { sandbox, fetches } = makeSandbox();
  const c = makeContainer();
  const p = sandbox.__load("medical_record", "rec-1", c);
  fetches[0].resolve(ok({ list: [] }));
  await p;
  assert.ok(c._html.includes("暂无本地版本记录"));
});

test("GD-10:HTTP 失败显示状态码+重试,重试成功替换为版本行", async () => {
  const { sandbox, fetches } = makeSandbox();
  const c = makeContainer();
  const p = sandbox.__load("medical_record", "rec-1", c);
  fetches[0].resolve({ ok: false, status: 500, json: async () => ({}) });
  await p;
  assert.ok(c._html.includes("版本记录载入失败"), "真实 HTTP 错误不得伪装成暂无");
  assert.ok(c._html.includes("500"), "失败文案保留状态码");
  assert.ok(c._btn, "必须提供重试按钮");

  const retryDone = c._btn.listeners.click();   // 点击重试
  assert.strictEqual(fetches.length, 2, "重试必须重新请求");
  assert.strictEqual(fetches[1].url, fetches[0].url, "重试复用原 entity 参数");
  fetches[1].resolve(ok({ list: [{ version_id: "v1" }] }));
  await retryDone;
  await new Promise(r => setTimeout(r, 0));
  assert.ok(c._html.includes("<row>v1</row>"), "重试成功后替换为版本行");
});

test("GD-10:网络异常显示失败+重试", async () => {
  const { sandbox, fetches } = makeSandbox();
  const c = makeContainer();
  const p = sandbox.__load("appointment", "a-1", c);
  fetches[0].reject(new Error("offline"));
  await p;
  assert.ok(c._html.includes("版本记录载入失败"));
  assert.ok(c._btn, "网络失败也要能重试");
});

test("GD-10:旧请求晚到不得覆盖新请求结果", async () => {
  const { sandbox, fetches } = makeSandbox();
  const c = makeContainer();
  const p1 = sandbox.__load("medical_record", "rec-1", c);
  const p2 = sandbox.__load("medical_record", "rec-1", c);
  assert.strictEqual(fetches.length, 2);
  fetches[1].resolve(ok({ list: [{ version_id: "v-new" }] }));
  await p2;
  fetches[0].resolve(ok({ list: [{ version_id: "v-old" }] }));
  await p1;
  assert.ok(c._html.includes("v-new"), "保留新请求结果");
  assert.ok(!c._html.includes("v-old"), "旧结果不得晚到覆盖");
});
