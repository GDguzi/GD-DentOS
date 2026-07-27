// 2D.3 前端密码校验：只拦空/纯空白，不再管长度；发送原值不 trim。
// 抽出 account_manager.js / staff_manager.js 里 4 个入口函数，沙箱 eval 真跑交互路径，
// 断言"有没有发请求 + 请求体里的密码原文"。
// 跑：node --test test/web/test_account_password_validation.mjs
import { test } from "node:test";
import assert from "node:assert";
import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import { dirname, join } from "node:path";
import vm from "node:vm";

const here = dirname(fileURLToPath(import.meta.url));
const staticDir = join(here, "..", "..", "local_app", "static");
const accountSrc = readFileSync(join(staticDir, "account_manager.js"), "utf8");
const staffSrc = readFileSync(join(staticDir, "staff_manager.js"), "utf8");

function extract(src, fnHeader) {
  const start = src.indexOf(fnHeader);
  assert.ok(start > 0, `应能定位 ${fnHeader}`);
  const end = src.indexOf("\n}", start) + 2;
  return src.slice(start, end);
}

const code = [
  extract(accountSrc, "async function amCreate()"),
  extract(accountSrc, "async function amResetPw("),
  extract(accountSrc, "async function amChangeOwnPassword()"),
  extract(staffSrc, "async function openStaffAccount("),
].join("\n");

// 沙箱：记录 fetch 调用与提示文案；appPrompt 按队列吐值；表单字段可注入。
function makeSandbox({ fields = {}, prompts = [] } = {}) {
  const calls = [];
  const alerts = [];
  const msgs = [];
  const promptCalls = [];
  const queue = prompts.slice();
  const sandbox = {
    ROLE_TO_ACCT: { "医生": "doctor" },
    JSON,
    encodeURIComponent,
    console,
    document: {
      getElementById: id => (id in fields ? { value: fields[id] } : null),
    },
    window: { alert: t => alerts.push(t) },
    appPrompt: async (...args) => {
      promptCalls.push(args);
      return queue.length ? queue.shift() : null;
    },
    amSetMsg: t => msgs.push(t),
    amCanManageUserId: () => true,
    amRender: async () => {},
    ensureStaffAccts: async () => {},
    renderStaffManager: () => {},
    fetch: async (url, opts) => {
      calls.push({ url, body: JSON.parse(opts.body) });
      return { ok: true, status: 200, json: async () => ({}) };
    },
    __calls: calls,
    __alerts: alerts,
    __msgs: msgs,
    __promptCalls: promptCalls,
  };
  sandbox.globalThis = sandbox;
  vm.createContext(sandbox);
  vm.runInContext(code, sandbox);
  return sandbox;
}

const BLANKS = ["", " ", "   ", "\t", "\n", " \t\n "];

function assertPasswordPrompt(call, autocomplete) {
  assert.match(call[0], /密码/);
  assert.strictEqual(call[1], "");
  assert.strictEqual(call[2].type, "password");
  assert.strictEqual(call[2].autocomplete, autocomplete);
  assert.deepStrictEqual(Object.keys(call[2]).sort(), ["autocomplete", "type"]);
}

// ---------- 新增账号 ----------
test("新增账号：单字符密码会发请求", async () => {
  const s = makeSandbox({ fields: { amNewUser: "u", amNewName: "U", amNewPw: "短", amNewRole: "reception" } });
  await s.amCreate();
  assert.strictEqual(s.__calls.length, 1);
  assert.strictEqual(s.__calls[0].body.password, "短");
});

test("新增账号：空/纯空白不发请求且提示不能为空", async () => {
  for (const pw of BLANKS) {
    const s = makeSandbox({ fields: { amNewUser: "u", amNewName: "U", amNewPw: pw, amNewRole: "reception" } });
    await s.amCreate();
    assert.strictEqual(s.__calls.length, 0, `${JSON.stringify(pw)} 不该发请求`);
    assert.match(s.__msgs.join(""), /密码不能为空/);
  }
});

test("新增账号：密码原值不 trim", async () => {
  const s = makeSandbox({ fields: { amNewUser: " u ", amNewName: "U", amNewPw: " x ", amNewRole: "reception" } });
  await s.amCreate();
  assert.strictEqual(s.__calls[0].body.password, " x ");
  assert.strictEqual(s.__calls[0].body.username, "u", "用户名仍旧 trim");
});

// ---------- 管理员重置 ----------
test("重置密码：单字符会发请求", async () => {
  const s = makeSandbox({ prompts: ["短"] });
  await s.amResetPw("id1", "u");
  assert.strictEqual(s.__calls.length, 1);
  assert.strictEqual(s.__calls[0].body.password, "短");
});

test("重置密码：空/纯空白不发请求", async () => {
  for (const pw of BLANKS) {
    const s = makeSandbox({ prompts: [pw] });
    await s.amResetPw("id1", "u");
    assert.strictEqual(s.__calls.length, 0, `${JSON.stringify(pw)} 不该发请求`);
    assert.match(s.__alerts.join(""), /密码不能为空/);
  }
});

test("重置密码：原值不 trim", async () => {
  const s = makeSandbox({ prompts: [" x "] });
  await s.amResetPw("id1", "u");
  assert.strictEqual(s.__calls[0].body.password, " x ");
});

test("重置密码：使用 password/new-password 安全弹窗", async () => {
  const s = makeSandbox({ prompts: ["短"] });
  await s.amResetPw("id1", "u");
  assert.strictEqual(s.__promptCalls.length, 1);
  assertPasswordPrompt(s.__promptCalls[0], "new-password");
});

// ---------- 自助改密 ----------
test("自助改密：单字符会发请求", async () => {
  const s = makeSandbox({ prompts: ["old", "短"] });
  await s.amChangeOwnPassword();
  assert.strictEqual(s.__calls.length, 1);
  assert.strictEqual(s.__calls[0].body.new_password, "短");
});

test("自助改密：空/纯空白不发请求", async () => {
  for (const pw of BLANKS) {
    const s = makeSandbox({ prompts: ["old", pw] });
    await s.amChangeOwnPassword();
    assert.strictEqual(s.__calls.length, 0, `${JSON.stringify(pw)} 不该发请求`);
    assert.match(s.__alerts.join(""), /新密码不能为空/);
  }
});

test("自助改密：新旧密码原值不 trim", async () => {
  const s = makeSandbox({ prompts: [" o ", " x "] });
  await s.amChangeOwnPassword();
  assert.strictEqual(s.__calls[0].body.old_password, " o ");
  assert.strictEqual(s.__calls[0].body.new_password, " x ");
});

test("自助改密：旧密码用 current-password，新密码用 new-password", async () => {
  const s = makeSandbox({ prompts: ["old", "new"] });
  await s.amChangeOwnPassword();
  assert.strictEqual(s.__promptCalls.length, 2);
  assertPasswordPrompt(s.__promptCalls[0], "current-password");
  assertPasswordPrompt(s.__promptCalls[1], "new-password");
});

// ---------- 员工开账号 ----------
test("员工开账号：单字符会发请求", async () => {
  const s = makeSandbox({ prompts: ["J001", "短"] });
  await s.openStaffAccount("s1", "J001", "医生");
  assert.strictEqual(s.__calls.length, 1);
  assert.strictEqual(s.__calls[0].body.password, "短");
});

test("员工开账号：空/纯空白不发请求", async () => {
  for (const pw of BLANKS) {
    const s = makeSandbox({ prompts: ["J001", pw] });
    await s.openStaffAccount("s1", "J001", "医生");
    assert.strictEqual(s.__calls.length, 0, `${JSON.stringify(pw)} 不该发请求`);
    assert.match(s.__alerts.join(""), /密码不能为空/);
  }
});

test("员工开账号：原值不 trim", async () => {
  const s = makeSandbox({ prompts: ["J001", " x "] });
  await s.openStaffAccount("s1", "J001", "医生");
  assert.strictEqual(s.__calls[0].body.password, " x ");
});

test("员工开账号：初始密码使用 password/new-password 安全弹窗", async () => {
  const s = makeSandbox({ prompts: ["J001", "短"] });
  await s.openStaffAccount("s1", "J001", "医生");
  assert.strictEqual(s.__promptCalls.length, 2);
  assertPasswordPrompt(s.__promptCalls[1], "new-password");
});

// ---------- 文案残留扫描（辅助） ----------
test("授权前端文件不再出现长度策略文案/校验", () => {
  for (const src of [accountSrc, staffSrc]) {
    assert.doesNotMatch(src, /≥\s*6|至少\s*6/);
    assert.doesNotMatch(src, /(password|pw|newpw)\.length\s*<\s*6/);
  }
});
