// 内窥镜手柄键手势:短按=拍照,长按=开始录像,录像中短按=停止。
// 沙箱 eval 两个纯函数:endoKeyDownDecision(keydown 过滤/学习/跟踪) + endoKeyUpDecision(松键定动作)。
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
vm.runInContext(extract("endoKeyDownDecision") + "\n" + extract("endoKeyUpDecision")
  + "\nthis.down = endoKeyDownDecision; this.up = endoKeyUpDecision;", sandbox);

test("keydown 学习态:任何按键都返回 learn(记这个键码)", () => {
  assert.strictEqual(sandbox.down(true, null, "Enter", false), "learn");
  assert.strictEqual(sandbox.down(true, "Enter", "F13", false), "learn");
});

test("keydown 已映射:命中键码→track(开始跟踪按住);未命中/未映射/系统连发→null", () => {
  assert.strictEqual(sandbox.down(false, "Enter", "Enter", false), "track");
  assert.strictEqual(sandbox.down(false, "Enter", "Space", false), null);
  assert.strictEqual(sandbox.down(false, null, "Enter", false), null);
  assert.strictEqual(sandbox.down(false, "", "Enter", false), null);
  assert.strictEqual(sandbox.down(false, "Enter", "Enter", true), null);   // e.repeat 由计时器判长按
});

test("keyup 短按:没在录像→shoot 拍照", () => {
  assert.strictEqual(sandbox.up(false, false, 1000, 0), "shoot");
});

test("keyup 长按已开录:consume(这次松键不算短按,不停录)", () => {
  assert.strictEqual(sandbox.up(true, true, 1000, 0), "consume");
});

test("keyup 录像中短按:recStop 停止录像", () => {
  assert.strictEqual(sandbox.up(true, false, 1000, 0), "recStop");
});

test("keyup 防连拍:距上次拍照<400ms 不重拍(录像停止不受防抖限制)", () => {
  assert.strictEqual(sandbox.up(false, false, 1300, 1000), null);
  assert.strictEqual(sandbox.up(false, false, 1400, 1000), "shoot");
  assert.strictEqual(sandbox.up(true, false, 1300, 1000), "recStop");
});
