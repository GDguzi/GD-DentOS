// 相机小助手手柄键手势:still pin 只报"按了一次"(无按下/松开对)。
// 手柄键=拍照即按即拍;录像用屏幕按钮——真机实测两次按键最小间隔1.9秒,
// 相机拍高清still期间第二按被硬件吞掉,"0.6秒内双击"物理不成立,双击手势已判死。
// 残留过滤:小助手启动会自发一次触发(编号恒为1),只有 n===1 且紧跟 hello 才丢——
// 重开模态 hello 会刷新,若只看时间窗会把头2秒的真按键(编号>1)全吞掉(真机实测踩过)。
import { test } from "node:test";
import assert from "node:assert";
import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import { dirname, join } from "node:path";
import vm from "node:vm";

const here = dirname(fileURLToPath(import.meta.url));
const src = readFileSync(join(here, "..", "..", "local_app", "static", "endoscope_capture.js"), "utf8");

function extract(name) {
  const start = src.indexOf(`function ${name}`);
  return src.slice(start, src.indexOf("\n}", start) + 2);
}

const sandbox = {};
vm.createContext(sandbox);
vm.runInContext(extract("endoHelperBtnDecision") + "\nthis.btn = endoHelperBtnDecision;", sandbox);

test("小助手启动残留(编号1)紧跟 hello=丢弃", () => {
  assert.strictEqual(sandbox.btn(10000, 10002, 1), "ignore");
  assert.strictEqual(sandbox.btn(10000, 11999, 1), "ignore");
});

test("重开模态后立刻按键(编号>1,虽在 hello 2秒内)=照拍,不丢", () => {
  assert.strictEqual(sandbox.btn(10000, 10500, 12), "shoot");
  assert.strictEqual(sandbox.btn(10000, 11999, 2), "shoot");
});

test("编号1但离 hello 超过2秒=真按键(小助手先开很久,页面后连,第一次按)", () => {
  assert.strictEqual(sandbox.btn(10000, 12000, 1), "shoot");
  assert.strictEqual(sandbox.btn(10000, 20000, 1), "shoot");
});

test("普通按键一律即按即拍,连按也每次都拍(硬件已保证最小间隔)", () => {
  assert.strictEqual(sandbox.btn(10000, 20000, 5), "shoot");
  assert.strictEqual(sandbox.btn(10000, 20300, 6), "shoot");
  assert.strictEqual(sandbox.btn(10000, 22200, 7), "shoot");
});
