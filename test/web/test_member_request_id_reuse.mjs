// 审计R4 复核遗留:储值改钱请求的幂等号,网络异常(结果未知)后同患者同动作同金额重试必须复用同号
// (对齐 #800 弹窗内重试复用)——否则"已入账但响应丢了"的超时场景,重试就是新号照样重复扣。
// 有明确响应(成功/报错)或换金额则用新号。
// 跑：node --test test/web/test_member_request_id_reuse.mjs
import { test } from "node:test";
import assert from "node:assert";
import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import { dirname, join } from "node:path";
import vm from "node:vm";

const here = dirname(fileURLToPath(import.meta.url));
const src = readFileSync(join(here, "..", "..", "local_app", "static", "member_account.js"), "utf8");

function makeSandbox() {
  const sandbox = {
    window: { alert: () => {} },
    fetchCalls: [],
    fetchFailNext: 0,                 // >0 时接下来 N 次 fetch 抛网络异常
    promptQueue: [],                  // appPrompt 依次弹出的答案
    reqSeq: 0,
    appPrompt: async () => sandbox.promptQueue.shift() ?? null,
    newPaymentRequestId: () => "req-" + (++sandbox.reqSeq),
    escapeHtml: s => String(s ?? ""),
    escapeAttr: s => String(s ?? ""),
    workspacePatientId: "p1",
    renderWorkspaceMemberTab: () => {},
    loadAuditLogs: () => {},
    document: { addEventListener: () => {} },
  };
  sandbox.fetch = async (url, opts) => {
    sandbox.fetchCalls.push({ url, body: JSON.parse(opts.body) });
    if (sandbox.fetchFailNext > 0) { sandbox.fetchFailNext--; throw new Error("network"); }
    return { ok: true, json: async () => ({}) };
  };
  vm.createContext(sandbox);
  vm.runInContext(src, sandbox);
  return sandbox;
}

const panel = { querySelector: () => null, querySelectorAll: () => [] };

test("网络异常后同额重试复用同一 request_id", async () => {
  const sb = makeSandbox();
  sb.fetchFailNext = 1;
  sb.promptQueue = ["100", "备注A"];
  await sb.memberAction("topup", "p1", panel);        // 第一次:网络异常,结果未知
  sb.promptQueue = ["100", "备注A"];
  await sb.memberAction("topup", "p1", panel);        // 同额重试
  assert.strictEqual(sb.fetchCalls.length, 2);
  assert.strictEqual(sb.fetchCalls[0].body.request_id, sb.fetchCalls[1].body.request_id,
    "结果未知的同额重试必须复用同号,否则超时场景重复入账");
});

test("网络异常后换金额用新号;明确响应后下一笔也用新号", async () => {
  const sb = makeSandbox();
  sb.fetchFailNext = 1;
  sb.promptQueue = ["100", ""];
  await sb.memberAction("topup", "p1", panel);        // 网络异常,挂起 req-1
  sb.promptQueue = ["200", ""];
  await sb.memberAction("topup", "p1", panel);        // 换金额=新意图,新号且成功
  sb.promptQueue = ["300", ""];
  await sb.memberAction("topup", "p1", panel);        // 上笔已有明确响应,再来也是新号
  const ids = sb.fetchCalls.map(c => c.body.request_id);
  assert.strictEqual(new Set(ids).size, 3, `三笔应是三个不同号: ${ids}`);
});

test("网络异常后同额但改备注=新意图新号(与后端整体载荷哈希口径一致)", async () => {
  const sb = makeSandbox();
  sb.fetchFailNext = 1;
  sb.promptQueue = ["100", "备注A"];
  await sb.memberAction("topup", "p1", panel);        // 网络异常,挂起 req-1(备注A)
  sb.promptQueue = ["100", "备注B"];
  await sb.memberAction("topup", "p1", panel);        // 同额改备注:载荷不同,必须新号(旧号+新备注会被后端409)
  const ids = sb.fetchCalls.map(c => c.body.request_id);
  assert.notStrictEqual(ids[0], ids[1], "载荷(备注)变了不得复用旧号");
});

test("请求进行中再次触发被拒,不生成第二个号", async () => {
  const sb = makeSandbox();
  const statusEl = { textContent: "" };
  const panelWithStatus = { querySelector: sel => sel === "[data-member-status]" ? statusEl : null,
                            querySelectorAll: () => [] };
  let releaseFetch;
  sb.fetch = async (url, opts) => {
    sb.fetchCalls.push({ url, body: JSON.parse(opts.body) });
    await new Promise(r => { releaseFetch = r; });     // 第一笔悬着不返回
    return { ok: true, json: async () => ({}) };
  };
  sb.promptQueue = ["100", ""];
  const first = sb.memberAction("topup", "p1", panelWithStatus);   // 不 await,悬在 fetch
  await new Promise(r => setTimeout(r, 10));
  sb.promptQueue = ["100", ""];
  await sb.memberAction("topup", "p1", panelWithStatus);           // 进行中再次触发
  assert.strictEqual(sb.fetchCalls.length, 1, "进行中不得发出第二笔请求");
  assert.match(statusEl.textContent, /正在处理中/);
  releaseFetch();
  await first;
});

test("弹窗阶段并行启动第二笔也被拒(闸门在首个弹窗前占位)", async () => {
  // 四轮P1:若闸门等弹窗结束才占位,两笔并行停在弹窗阶段时,前者完成释放后
  // 后者仍拿新号再入一笔。闸门必须在 appPrompt 之前占位。
  const sb = makeSandbox();
  const statusEl = { textContent: "" };
  const panelWithStatus = { querySelector: sel => sel === "[data-member-status]" ? statusEl : null,
                            querySelectorAll: () => [] };
  let releasePrompt;
  let promptCalls = 0;
  sb.appPrompt = async () => {
    promptCalls++;
    if (promptCalls === 1) {                       // 第一笔悬在金额弹窗
      await new Promise(r => { releasePrompt = r; });
      return "100";
    }
    return "";                                     // 第一笔的备注弹窗
  };
  const first = sb.memberAction("topup", "p1", panelWithStatus);   // 不 await,悬在弹窗
  await new Promise(r => setTimeout(r, 10));
  await sb.memberAction("topup", "p1", panelWithStatus);           // 弹窗阶段并行触发第二笔
  assert.strictEqual(promptCalls, 1, "第二笔连弹窗都不该弹");
  assert.match(statusEl.textContent, /正在处理中/);
  releasePrompt();
  await first;
  assert.strictEqual(sb.fetchCalls.length, 1, "全程只发出第一笔请求");
});

test("不同动作互不串号", async () => {
  const sb = makeSandbox();
  sb.fetchFailNext = 2;
  sb.promptQueue = ["100", ""];
  await sb.memberAction("topup", "p1", panel);        // topup 网络异常
  sb.promptQueue = ["100"];
  await sb.memberAction("consume", "p1", panel);      // consume(无备注) 网络异常
  sb.promptQueue = ["100", ""];
  await sb.memberAction("refund", "p1", panel);       // refund 成功
  const ids = sb.fetchCalls.map(c => c.body.request_id);
  assert.strictEqual(new Set(ids).size, 3, `不同动作各自持号: ${ids}`);
});
