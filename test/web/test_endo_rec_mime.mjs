// 内窥镜录像容器格式选择:优先 mp4(患者手机直接放),不支持退 webm,都不支持返回空串。
// 沙箱 eval endoRecMimePick 纯函数。
import { test } from "node:test";
import assert from "node:assert";
import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import { dirname, join } from "node:path";
import vm from "node:vm";

const here = dirname(fileURLToPath(import.meta.url));
const src = readFileSync(join(here, "..", "..", "local_app", "static", "endoscope_capture.js"), "utf8");

const start = src.indexOf("function endoRecMimePick");
const decl = src.slice(start, src.indexOf("\n}", start) + 2);

const sandbox = {};
vm.createContext(sandbox);
vm.runInContext(decl + "\nthis.endoRecMimePick = endoRecMimePick;", sandbox);

test("mp4 支持时优先选 mp4", () => {
  assert.strictEqual(sandbox.endoRecMimePick(() => true), "video/mp4");
});

test("mp4 不支持时退 webm(vp9 优先)", () => {
  assert.strictEqual(sandbox.endoRecMimePick(m => m.startsWith("video/webm")), "video/webm;codecs=vp9");
  assert.strictEqual(sandbox.endoRecMimePick(m => m === "video/webm"), "video/webm");
});

test("全都不支持返回空串(调用方提示不支持录像)", () => {
  assert.strictEqual(sandbox.endoRecMimePick(() => false), "");
});
