import {test} from "node:test";
import assert from "node:assert";
import {readFileSync} from "node:fs";
import {fileURLToPath} from "node:url";
import {dirname, join} from "node:path";
import vm from "node:vm";

const here = dirname(fileURLToPath(import.meta.url));
const appSource = readFileSync(
  join(here, "..", "..", "local_app", "static", "app.js"),
  "utf8",
);
const guardSource = appSource.slice(
  appSource.indexOf("let _authReloading"),
  appSource.indexOf("// 按权限隐藏", appSource.indexOf("let _authReloading")),
);

function response(status, body = {}) {
  return {
    status,
    ok: status >= 200 && status < 300,
    json: async () => body,
    clone: () => response(status, body),
  };
}

function makeSandbox({accessV3 = true, replies = [], password = "pw"} = {}) {
  const calls = [];
  const requestBodies = [];
  const prompts = [];
  const nativePrompts = [];
  const alerts = [];
  let reloads = 0;
  const sandbox = {
    __accessV3: accessV3,
    _authenticatedBusinessReady: true,
    fetch: async (...args) => {
      calls.push(args);
      if (args[0] instanceof Request) {
        requestBodies.push(await args[0].text());
      }
      return replies.shift() || response(200, {});
    },
    Request,
    appPrompt: async (...args) => {
      prompts.push(args);
      return password;
    },
    prompt: message => {
      nativePrompts.push(message);
      return password;
    },
    alert: message => alerts.push(message),
    location: {reload: () => { reloads += 1; }},
  };
  sandbox.window = sandbox;
  vm.createContext(sandbox);
  vm.runInContext(guardSource, sandbox);
  return {
    sandbox,
    calls,
    prompts,
    nativePrompts,
    alerts,
    requestBodies,
    reloads: () => reloads,
  };
}

test("V3 高风险请求被要求确认时，输对密码后只重放一次原请求", async () => {
  const run = makeSandbox({
    replies: [
      response(403, {detail: "recent_reauthentication_required"}),
      response(200, {ok: true}),
      response(200, {ok: true}),
    ],
  });
  const options = {
    method: "DELETE",
    headers: {"Content-Type": "application/json"},
    body: JSON.stringify({reason: "误建"}),
  };

  const result = await run.sandbox.fetch("/api/appointments/a1", options);

  assert.strictEqual(result.status, 200);
  assert.strictEqual(run.prompts.length, 1);
  assert.strictEqual(run.nativePrompts.length, 0, "不应再调用 window.prompt");
  assert.match(run.prompts[0][0], /重要操作/);
  assert.strictEqual(run.prompts[0][1], "");
  assert.strictEqual(run.prompts[0][2].type, "password");
  assert.strictEqual(run.prompts[0][2].autocomplete, "current-password");
  assert.deepStrictEqual(Object.keys(run.prompts[0][2]).sort(), ["autocomplete", "type"]);
  assert.strictEqual(run.calls.length, 3);
  assert.strictEqual(run.calls[0][0], "/api/appointments/a1");
  assert.strictEqual(run.calls[2][0], "/api/appointments/a1");
  assert.strictEqual(run.calls[0][1], options);
  assert.strictEqual(run.calls[2][1], options);
  assert.strictEqual(
    run.calls.filter(call => call[0] === "/api/appointments/a1").length,
    2,
    "原业务请求应只有首次+唯一一次重放",
  );
  assert.strictEqual(run.calls[1][0], "/api/auth/reauth");
  assert.deepStrictEqual(JSON.parse(run.calls[1][1].body), {password: "pw"});
  assert.strictEqual(run.reloads(), 0);
});

test("密码错误不执行第二次高风险请求，也不把密码错误误当掉线", async () => {
  const run = makeSandbox({
    replies: [
      response(403, {detail: "recent_reauthentication_required"}),
      response(401, {detail: "密码错误"}),
    ],
    password: "wrong",
  });

  const result = await run.sandbox.fetch("/api/bills/b1/refund", {
    method: "POST",
    body: "{}",
  });

  assert.strictEqual(result.status, 403);
  assert.strictEqual(run.calls.length, 2);
  assert.strictEqual(run.nativePrompts.length, 0);
  assert.strictEqual(run.reloads(), 0);
  assert.match(run.alerts.join("\n"), /密码/);
});

test("Request 对象的一次性 body 会提前克隆，确认后仍只重放一次", async () => {
  const run = makeSandbox({
    replies: [
      response(403, {detail: "recent_reauthentication_required"}),
      response(200, {ok: true}),
      response(200, {ok: true}),
    ],
  });
  const input = new Request("http://clinic.local/api/staff-members/s1", {
    method: "DELETE",
    headers: {"Content-Type": "application/json"},
    body: JSON.stringify({reason: "离职"}),
  });

  const result = await run.sandbox.fetch(input);

  assert.strictEqual(result.status, 200);
  assert.strictEqual(run.calls.length, 3);
  assert.strictEqual(run.calls[0][0] instanceof Request, true);
  assert.strictEqual(run.calls[2][0] instanceof Request, true);
  assert.notStrictEqual(run.calls[0][0], run.calls[2][0]);
  assert.deepStrictEqual(run.requestBodies, [
    JSON.stringify({reason: "离职"}),
    JSON.stringify({reason: "离职"}),
  ]);
});

test("普通 403、旧模式和取消输入都不触发重放", async () => {
  for (const [label, options] of [
    ["普通 403", {replies: [response(403, {detail: "access_denied"})]}],
    ["旧模式", {
      accessV3: false,
      replies: [response(403, {detail: "recent_reauthentication_required"})],
    }],
    ["取消输入", {
      password: null,
      replies: [response(403, {detail: "recent_reauthentication_required"})],
    }],
  ]) {
    const run = makeSandbox(options);
    const result = await run.sandbox.fetch("/api/appointments/a1", {
      method: "DELETE",
    });
    assert.strictEqual(result.status, 403, label);
    assert.strictEqual(run.calls.length, 1, label);
    assert.strictEqual(run.nativePrompts.length, 0, `${label}不应调用 window.prompt`);
    assert.strictEqual(run.prompts.length, label === "取消输入" ? 1 : 0, label);
  }
});
