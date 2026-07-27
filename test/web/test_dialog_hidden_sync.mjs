// 外层 backdrop hidden 时,内部 role=dialog 必须同步 hidden/aria-hidden,
// 否则读屏/自动化全局扫描会读到已关闭弹窗的旧内容。
// 沙箱 eval syncDialogHidden,用桩 host/dialog 验证镜像逻辑。
import { test } from "node:test";
import assert from "node:assert";
import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import { dirname, join } from "node:path";
import vm from "node:vm";

const here = dirname(fileURLToPath(import.meta.url));
const src = readFileSync(join(here, "..", "..", "local_app", "static", "app.js"), "utf8");

const decl = src.slice(src.indexOf("function syncDialogHidden"), src.indexOf("(function watchModalBackdrops"));

const sandbox = {};
vm.createContext(sandbox);
vm.runInContext(decl + "\nthis.syncDialogHidden = syncDialogHidden;", sandbox);

function stubDialog() {
  const attrs = {};
  return { hidden: false, attrs, setAttribute(k, v) { attrs[k] = v; } };
}

test("外层关闭 → dialog 补 hidden + aria-hidden=true", () => {
  const d = stubDialog();
  sandbox.syncDialogHidden({ hidden: true, querySelectorAll: () => [d] });
  assert.strictEqual(d.hidden, true);
  assert.strictEqual(d.attrs["aria-hidden"], "true");
});

test("外层打开 → dialog 去 hidden + aria-hidden=false", () => {
  const d = stubDialog();
  d.hidden = true;
  sandbox.syncDialogHidden({ hidden: false, querySelectorAll: () => [d] });
  assert.strictEqual(d.hidden, false);
  assert.strictEqual(d.attrs["aria-hidden"], "false");
});

test("观察器已挂在 app.js(源码不变量守卫)", () => {
  assert.ok(src.includes("watchModalBackdrops"));
  assert.ok(src.includes('attributeFilter: ["hidden"]'));
});
