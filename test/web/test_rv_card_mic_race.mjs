// 快捷录音竞态动态回归(VM 全文件加载+可控异步 mock):
//   ① 录音中关卡片后立即重开同一回访 → onstop 必须永久取消,不得 POST 录音
//   ② getUserMedia 权限框待定时切卡片 → resolve 后不得启动 recorder,必须弃流
// 跑：node --test test/web/test_rv_card_mic_race.mjs
import { test } from "node:test";
import assert from "node:assert";
import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import { dirname, join } from "node:path";
import vm from "node:vm";

const here = dirname(fileURLToPath(import.meta.url));
const src = readFileSync(join(here, "..", "..", "local_app", "static", "return_visit_card.js"), "utf8");

// 造一个可控的沙箱:整文件跑进去,DOM/媒体 API 全 mock
function makeSandbox() {
  const fetches = [];
  const node = { value: "", textContent: "", disabled: false, files: [] };
  const ov = {
    hidden: true, innerHTML: "", onclick: null,
    querySelector: selector => selector === "[data-rv-card-form]"
      ? {dataset: {rvCardRevision: "1"}}
      : node,
    querySelectorAll: () => [],
  };
  let resolveStream = null;
  const streamTracks = [{ stopped: 0, stop() { this.stopped++; } }];
  const stream = { getTracks: () => streamTracks };
  const recorders = [];
  class FakeRecorder {
    constructor() { this.state = "inactive"; recorders.push(this); }
    static isTypeSupported() { return true; }
    start() { this.state = "recording"; }
    stop() {
      this.state = "inactive";
      // 模拟浏览器:onstop 异步触发(微任务),制造"stop 之后还能重开卡片"的窗口
      queueMicrotask(() => { this.onstop && this.onstop(); });
    }
  }
  const sandbox = {
    console,
    document: { getElementById: () => ov, addEventListener() {}, removeEventListener() {} },
    navigator: { mediaDevices: { getUserMedia: () => new Promise(r => { resolveStream = () => r(stream); }) } },
    MediaRecorder: FakeRecorder,
    Blob: class { constructor(parts, opts) { this.parts = parts; this.opts = opts; } },
    FormData: class { append() {} },
    fetch: async (url, opts) => {
      fetches.push({ url, method: (opts && opts.method) || "GET" });
      return { ok: true, json: async () => ({}) };
    },
    alert() {}, confirm: () => true,
    escapeHtml: s => String(s ?? ""), escapeAttr: s => String(s ?? ""),
    window: {},
  };
  vm.createContext(sandbox);
  vm.runInContext(src, sandbox);
  return { sandbox, fetches, recorders, streamTracks, resolve: () => resolveStream && resolveStream(), node };
}

const settle = () => new Promise(r => setTimeout(r, 0));

test("① 录音中关卡片→立即重开同一回访:onstop 必须取消,不得 POST 录音", async () => {
  const { sandbox, fetches, recorders, resolve, node } = makeSandbox();
  sandbox.openReturnVisitCard("rv1", "p1");
  const t = sandbox.toggleRvCardMicRecord();
  resolve(); await t;                       // 开麦完成,录音中
  assert.strictEqual(recorders.length, 1);
  assert.strictEqual(recorders[0].state, "recording");
  sandbox.closeReturnVisitCard();           // 关卡片(触发 stop,onstop 排队在微任务)
  sandbox.openReturnVisitCard("rv1", "p1"); // onstop 跑之前立刻重开同一回访(ID 又相等了)
  await settle();                           // 让 onstop 跑完
  const posts = fetches.filter(f => f.method === "POST" && f.url.includes("/calls"));
  assert.strictEqual(posts.length, 0, "关卡片后的录音不得因重开同一回访而被误上传");
  assert.match(node.textContent, /取消/, "状态栏应提示已取消");
});

test("② 开麦权限框待定时切卡片:resolve 后不得启动 recorder,必须弃流", async () => {
  const { sandbox, recorders, streamTracks, resolve } = makeSandbox();
  sandbox.openReturnVisitCard("rv1", "p1");
  const t = sandbox.toggleRvCardMicRecord();   // 挂在 getUserMedia 上
  sandbox.openReturnVisitCard("rv2", "p2");    // 权限框还没回来就切了卡片
  resolve(); await t;                          // 权限框回来了
  assert.strictEqual(recorders.length, 0, "切卡片后 resolve 不得启动录音机");
  assert.ok(streamTracks[0].stopped >= 1, "已到手的媒体流必须弃掉(stop tracks)");
});

test("③ 正常路径不回归:同一代内开录→停录,照常 POST 上传", async () => {
  const { sandbox, fetches, recorders, resolve } = makeSandbox();
  sandbox.openReturnVisitCard("rv1", "p1");
  const t = sandbox.toggleRvCardMicRecord();
  resolve(); await t;
  recorders[0].stop();                      // 用户自己点停止
  await settle();
  const posts = fetches.filter(f => f.method === "POST" && f.url.includes("/api/patients/p1/calls"));
  assert.strictEqual(posts.length, 1, "正常停止必须照常上传到锁定患者");
});
