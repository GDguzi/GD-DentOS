import { test } from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import vm from "node:vm";

const source = readFileSync(new URL("../../local_app/static/endoscope_capture.js", import.meta.url), "utf8");
const start = source.indexOf("function _endoToggleRec()");
const toggle = source.slice(start, source.indexOf("\n}", start) + 2);

function record(size) {
  let recorder;
  const messages = [];
  const sandbox = {
    _endoRec: null, _endoStream: {}, _endoHelper: false, _endoRecTimer: null,
    _endoShots: [], _endoStatus: value => messages.push(value), _endoRenderStrip() {},
    endoRecMimePick: () => "video/webm", window: { MediaRecorder: {} },
    MediaRecorder: class {
      constructor() { recorder = this; }
      start() {}
    },
    // Report a large finished recording without allocating a large buffer.
    Blob: class { constructor() { this.size = size; } },
    URL: { createObjectURL: () => "blob:synthetic" },
    document: { getElementById: () => null },
    clearInterval() {}, setInterval: () => 1,
  };
  vm.createContext(sandbox);
  vm.runInContext(toggle + "\n_endoToggleRec();", sandbox);
  recorder.ondataavailable({ data: { size } });
  recorder.onstop();
  return { shots: sandbox._endoShots, messages };
}

test("recordings larger than the former 200MB cutoff stay available for upload", () => {
  const { shots } = record(200 * 1024 * 1024 + 1);
  assert.equal(shots.length, 1);
  assert.equal(shots[0].slot, "录像");
  assert.equal(shots[0].video, true);
});

test("empty recordings are still rejected", () => {
  const { shots, messages } = record(0);
  assert.equal(shots.length, 0);
  assert.ok(messages.includes("录像为空,未保存"));
});
