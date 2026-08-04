// GD-03 人员候选数据契约:全站统一读 /api/staff-members 的 members 字段。
// 覆盖:预约列表/排班网格医生筛选契约;ensureStaff 失败不毒化缓存;
// 人员变更强刷已存在 datalist;bindStaffInputs 异步返回后自动填充;自由输入机制保留。
import { test } from "node:test";
import assert from "node:assert";
import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import { dirname, join } from "node:path";
import vm from "node:vm";

const here = dirname(fileURLToPath(import.meta.url));
const source = name => readFileSync(
  join(here, "..", "..", "local_app", "static", name),
  "utf8",
);

function section(text, startMarker, endMarker) {
  const start = text.indexOf(startMarker);
  assert.ok(start >= 0, `missing ${startMarker}`);
  const end = text.indexOf(endMarker, start + startMarker.length);
  assert.ok(end > start, `missing ${endMarker}`);
  return text.slice(start, end);
}

// ---------- 契约:staff-members 响应只认 members ----------

test("appointment list doctor filter reads members, never .list", () => {
  const t = source("appointment_views.js");
  assert.ok(
    t.includes("doctors = rD.ok ? (await rD.json()).members || [] : []"),
    "预约列表医生筛选必须读 members",
  );
  assert.ok(
    !t.includes("rD.json()).list"),
    "禁止对 staff-members 响应猜测式读取 .list",
  );
});

test("appointment grid doctor columns read members, never .list", () => {
  const t = source("appointment_grid.js");
  assert.ok(
    t.includes("doctors = rD.ok ? (await rD.json()).members || [] : []"),
    "排班网格医生列必须读 members",
  );
  assert.ok(!t.includes("rD.json()).list"), "禁止读取 .list");
});

test("free-input datalist mechanism is preserved (no closed select)", () => {
  const sm = source("staff_manager.js");
  assert.ok(sm.includes('input.setAttribute("list"'), "选人仍用 input+datalist");
  assert.ok(sm.includes('document.createElement("datalist")'), "datalist 机制保留");
  assert.ok(
    source("index.html").includes('id="apptDoctor" class="ord-input" data-staff-role="医生"'),
    "预约医生保留自由输入选人框",
  );
  assert.ok(
    source("appointment_editor.js").includes("bindStaffInputs(m)"),
    "预约编辑器渲染后绑定选人 datalist",
  );
});

// ---------- 运行时:共享缓存 + datalist ----------

const staffCore = section(
  source("staff_manager.js"),
  "let staffCache = null;",
  "// 记住上次配台",
);

function deferred() {
  let resolve, reject;
  const promise = new Promise((res, rej) => { resolve = res; reject = rej; });
  return { promise, resolve, reject };
}

function makeSandbox() {
  const lists = [];
  const doc = {
    body: { appendChild: el => lists.push(el) },
    getElementById: id => lists.find(l => l.id === id) || null,
    createElement: tag => ({ tag, id: "", innerHTML: "" }),
    querySelectorAll: sel =>
      sel === 'datalist[id^="staff-"]' ? [...lists] : [],
  };
  const fetches = [];
  const sandbox = {
    document: doc,
    escapeAttr: v => String(v ?? ""),
    escapeHtml: v => String(v ?? ""),
    fetch: url => {
      const d = deferred();
      fetches.push({ url, ...d });
      return d.promise;
    },
  };
  vm.createContext(sandbox);
  vm.runInContext(
    staffCore +
      "\nthis.__ensureStaff = ensureStaff;" +
      "\nthis.__staffByRole = staffByRole;" +
      "\nthis.__bindStaffInputs = bindStaffInputs;" +
      "\nthis.__refreshStaffDatalists = refreshStaffDatalists;",
    sandbox,
  );
  return { sandbox, lists, fetches };
}

const roster = members => ({ json: async () => ({ members, totalcount: members.length }) });
const DOC = { staff_id: "s1", name: "合成医生", role: "医生", roles: ["医生"] };
const NURSE = { staff_id: "s2", name: "合成护士", role: "护士", roles: ["护士"] };

test("ensureStaff caches members and staffByRole filters; '*' returns all", async () => {
  const { sandbox, fetches } = makeSandbox();
  const p = sandbox.__ensureStaff();
  fetches[0].resolve(roster([DOC, NURSE]));
  await p;
  assert.deepStrictEqual(sandbox.__staffByRole("医生").map(m => m.name), ["合成医生"]);
  assert.deepStrictEqual(sandbox.__staffByRole("*").map(m => m.name), ["合成医生", "合成护士"]);
});

test("failed staff fetch does not poison cache; next call retries", async () => {
  const { sandbox, fetches } = makeSandbox();
  const p1 = sandbox.__ensureStaff();
  fetches[0].reject(new Error("network down"));
  assert.strictEqual((await p1).length, 0, "失败时返回空列表");
  const p2 = sandbox.__ensureStaff();
  assert.strictEqual(fetches.length, 2, "失败后再次调用必须重新请求,不得吃缓存空数组");
  fetches[1].resolve(roster([DOC]));
  const got = await p2;
  assert.deepStrictEqual(got.map(m => m.name), ["合成医生"]);
});

test("bindStaffInputs fills datalist after async staff response", async () => {
  const { sandbox, lists, fetches } = makeSandbox();
  const input = {
    dataset: { staffRole: "医生" },
    attrs: {},
    setAttribute(k, v) { this.attrs[k] = v; },
  };
  const root = {
    querySelectorAll: sel =>
      sel === "input[data-staff-role]:not([list])" ? [input] : [],
  };
  sandbox.__bindStaffInputs(root);
  assert.strictEqual(input.attrs.list, "staff-医生");
  fetches[0].resolve(roster([DOC]));
  await new Promise(r => setTimeout(r, 0));
  const dl = lists.find(l => l.id === "staff-医生");
  assert.ok(dl, "datalist 已创建");
  assert.ok(dl.innerHTML.includes("合成医生"), "异步返回后 datalist 自动填充");
});

test("force refresh after roster change updates existing datalists", async () => {
  const { sandbox, lists, fetches } = makeSandbox();
  const input = {
    dataset: { staffRole: "医生" },
    attrs: {},
    setAttribute(k, v) { this.attrs[k] = v; },
  };
  const root = {
    querySelectorAll: sel =>
      sel === "input[data-staff-role]:not([list])" ? [input] : [],
  };
  sandbox.__bindStaffInputs(root);
  fetches[0].resolve(roster([DOC]));
  await new Promise(r => setTimeout(r, 0));

  const p = sandbox.__ensureStaff(true);   // 人员管理新增医生后强刷
  fetches[fetches.length - 1].resolve(roster([
    DOC,
    { staff_id: "s3", name: "新医生", role: "医生", roles: ["医生"] },
  ]));
  await p;
  const dl = lists.find(l => l.id === "staff-医生");
  assert.ok(dl.innerHTML.includes("新医生"), "已存在 datalist 必须拿到新候选,无需重开弹窗");
});
