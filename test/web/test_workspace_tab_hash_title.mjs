// 回归:患者工作区切tab时 URL hash 和顶部 H1 都不跟着变,始终停在
// 打开时的路由/"今日工作台"。抽出 switchWorkspaceTab 源码,stub document/location,
// 沙箱 eval 真测切tab后 hash 和 H1 有没有更新。
// 跑：node --test test/web/test_workspace_tab_hash_title.mjs
import { test } from "node:test";
import assert from "node:assert";
import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import { dirname, join } from "node:path";
import vm from "node:vm";

const here = dirname(fileURLToPath(import.meta.url));
const src = readFileSync(join(here, "..", "..", "local_app", "static", "patient_workspace.js"), "utf8");

function extract(fnHeader) {
  const start = src.indexOf(fnHeader);
  assert.ok(start > 0, `应能定位 ${fnHeader}`);
  const end = src.indexOf("\n}", start) + 2;
  return src.slice(start, end);
}

const labelCode = extract("function workspaceTabLabel");
const switchCode = extract("function switchWorkspaceTab");

// 模拟真实按钮:label + 角标span(如"收费"+"0")，clone 后 querySelectorAll(".workspace-tab-count")
// 能找到角标span并 remove()，跟浏览器真实 cloneNode(true) 行为一致(不是死数据)。
function makeButton(tab, label, countText) {
  return {
    dataset: {workspaceTab: tab},
    classList: {toggled: null, toggle(cls, v) { this.toggled = v; }},
    cloneNode() {
      const clone = {
        _label: label,
        _count: countText || "",
        get textContent() { return this._label + this._count; },
        querySelectorAll(sel) {
          if (sel === ".workspace-tab-count" && clone._count) {
            return [{remove: () => { clone._count = ""; }}];
          }
          return [];
        },
      };
      return clone;
    },
  };
}

function makeSandbox({initialHash, patientId, displayName, loadedTabs}) {
  const buttons = {
    profile: makeButton("profile", "基本资料"),
    billing: makeButton("billing", "收费", "0"),
  };
  const panels = {
    profile: {dataset: {workspacePanel: "profile"}, classList: {toggle() {}}, hidden: false},
    billing: {dataset: {workspacePanel: "billing"}, classList: {toggle() {}}, hidden: true},
  };
  const page = {
    querySelector: sel => {
      const m = /data-workspace-tab="([^"]+)"/.exec(sel);
      if (m) return buttons[m[1]] || null;
      return null;
    },
    querySelectorAll: sel => {
      if (sel === "[data-workspace-tab]") return Object.values(buttons);
      if (sel === "[data-workspace-panel]") return Object.values(panels);
      return [];
    },
  };
  const sandbox = {
    _wsHost: null,
    workspacePatientId: patientId,
    workspaceData: {patient: {display_name: displayName}},
    workspaceLoadedTabs: new Set(loadedTabs || []),
    workspaceProfileEditing: false,
    _workspaceTabIntentRevision: 0,
    workspaceTitle: {textContent: "今日工作台"},
    location: {hash: initialHash},
    window: {scrollTo: () => {}},
    __pushes: 0, __replaces: 0, __replacedState: null, __committedHashes: [],
    cssEscape: s => s,
    workspacePageEl: () => page,
    loadWorkspaceTabPanel: () => {},
    markPatientWorkspaceHashCommitted: hash => { sandbox.__committedHashes.push(hash); },
  };
  // 真浏览器里直接赋 location.hash=压历史;mock里对hash做setter计数,replaceState计另一个数
  const loc = {__h: initialHash};
  Object.defineProperty(loc, "hash", {
    get() { return loc.__h; },
    set(v) { loc.__h = v; sandbox.__pushes += 1; },
  });
  sandbox.location = loc;
  sandbox.history = {
    state: {__patientWorkspacePushed: true, __workspaceReturnHash: ""},
    replaceState(state, _t, url) {
      loc.__h = url;
      sandbox.__replaces += 1;
      sandbox.__replacedState = state;
      sandbox.history.state = state;
    },
  };
  vm.createContext(sandbox);
  vm.runInContext(labelCode + "\n" + switchCode + "\nthis.__switch = switchWorkspaceTab;", sandbox);
  return sandbox;
}

test("切到收费tab → hash 带上患者+tab，不停在打开时的路由", () => {
  const sandbox = makeSandbox({initialHash: "#patient/p1/profile", patientId: "p1", displayName: "张三", loadedTabs: ["profile", "billing"]});
  sandbox.__switch("billing");
  assert.strictEqual(sandbox.location.hash, "#patient/p1/billing", "hash 应该跟着切到的tab变，不能停在profile");
});

test("切到收费tab → 顶部H1不再是「今日工作台」，改显示患者名+当前tab", () => {
  const sandbox = makeSandbox({initialHash: "#patient/p1/profile", patientId: "p1", displayName: "张三", loadedTabs: ["profile", "billing"]});
  sandbox.__switch("billing");
  assert.strictEqual(sandbox.workspaceTitle.textContent, "张三 · 收费", "H1应显示患者名+当前tab标签，不能停留在今日工作台");
});

test("tab角标数字不该混进H1标题里", () => {
  const sandbox = makeSandbox({initialHash: "#patient/p1/profile", patientId: "p1", displayName: "张三", loadedTabs: ["profile", "billing"]});
  sandbox.__switch("billing");
  assert.ok(!sandbox.workspaceTitle.textContent.includes("0"), "角标countspan文本不该混进标题");
});

test("返回键修复:切tab用replaceState替换,不往浏览器历史压栈(否则history.back()在tab间打转)", () => {
  const sandbox = makeSandbox({initialHash: "#patient/p1/profile", patientId: "p1", displayName: "张三", loadedTabs: ["profile", "billing"]});
  sandbox.__switch("billing");
  assert.strictEqual(sandbox.location.hash, "#patient/p1/billing");
  assert.strictEqual(sandbox.__replaces, 1, "应走replaceState");
  assert.strictEqual(sandbox.__pushes, 0, "不得直接赋location.hash压历史");
  assert.strictEqual(sandbox._workspaceTabIntentRevision, 1, "切 tab 必须推进工作区意图代次");
  assert.strictEqual(sandbox.__replacedState.__patientWorkspacePushed, true, "tab 切换不得擦除患者历史来源标记");
  assert.strictEqual(sandbox.__replacedState.__workspaceReturnHash, "", "tab 切换不得擦除返回目标");
  assert.deepStrictEqual(
    sandbox.__committedHashes,
    ["#patient/p1/billing"],
    "app 路由事务必须同步最新已显示患者 hash",
  );
});
