// #373 回归:牙位十字格须带完整 FDI 码(title/aria-label),不能只显示位号看不出象限。
// 抽出 helpers.js 的 toothCrossHtml,stub escapeHtml,沙箱 eval 真测输出。
// 跑：node --test test/web/test_tooth_fdi_label.mjs
import { test } from "node:test";
import assert from "node:assert";
import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import { dirname, join } from "node:path";
import vm from "node:vm";

const here = dirname(fileURLToPath(import.meta.url));
const src = readFileSync(join(here, "..", "..", "local_app", "static", "helpers.js"), "utf8");
const start = src.indexOf("function toothCrossHtml");
const end = src.indexOf("\n}", start) + 2;
const code = src.slice(start, end);

function run(teeth) {
  const sandbox = { escapeHtml: s => String(s) };
  vm.createContext(sandbox);
  vm.runInContext(code + "\nthis.__out = toothCrossHtml(" + JSON.stringify(teeth) + ");", sandbox);
  return sandbox.__out;
}

test("单颗 16 → title/aria-label 带完整 FDI 16(不只 6)", () => {
  const html = run(["16"]);
  assert.match(html, /title="16"/, "title 应是完整 FDI 16");
  assert.match(html, /aria-label="牙位 16"/);
});

test("多颗 16 26 36 → title 含全部 FDI", () => {
  const html = run(["16", "26", "36"]);
  assert.match(html, /title="16 26 36"/);
});

test("空选 → title 为空,不报错", () => {
  const html = run([]);
  assert.match(html, /title=""/);
});

test("#546 单颗 16 → 格子可见文本是完整 16(不只 6),不靠格子位置隐性表达象限", () => {
  const html = run(["16"]);
  assert.match(html, /class="tc tc-rt">16</);
});

test("#546 多颗 16 26 → 各象限格子可见文本都是完整 FDI 码", () => {
  const html = run(["16", "26"]);
  assert.match(html, /class="tc tc-rt">16</);
  assert.match(html, /class="tc tc-lt">26</);
});

test("#546 乳牙码沿用字母(通用命名法本身不含 FDI 象限位,不受本次改动影响)", () => {
  const html = run(["55"]);
  assert.match(html, /class="tc tc-rt">E</);
});
