import test from "node:test";
import assert from "node:assert/strict";
import {createRequire} from "node:module";
import {dirname, join} from "node:path";
import {fileURLToPath} from "node:url";

const require = createRequire(import.meta.url);
const here = dirname(fileURLToPath(import.meta.url));
const sourcePath = join(
  here, "..", "..", "local_app", "static", "customer_hub_v2_followup_date.js"
);

test("下次回访四个快捷项按表单当前回访时间起算", () => {
  const {computeNextFollowupDate: compute} = require(sourcePath);
  assert.equal(compute("2026-07-21T10:00", "15d"), "2026-08-05");
  assert.equal(compute("2026-07-21T10:00", "1m"), "2026-08-21");
  assert.equal(compute("2026-07-21T10:00", "6m"), "2027-01-21");
  assert.equal(compute("2026-07-21T10:00", "1y"), "2027-07-21");
});

test("自然月和自然年压到目标月最后一天", () => {
  const {computeNextFollowupDate: compute} = require(sourcePath);
  assert.equal(compute("2026-01-31T10:00", "1m"), "2026-02-28");
  assert.equal(compute("2026-08-31T10:00", "6m"), "2027-02-28");
  assert.equal(compute("2024-02-29T10:00", "1y"), "2025-02-28");
  assert.equal(compute("2026-12-20T10:00", "15d"), "2027-01-04");
});

test("非法回访时间和未知快捷项明确失败", () => {
  const {computeNextFollowupDate: compute} = require(sourcePath);
  for (const [value, preset] of [
    ["", "1m"],
    ["2026-02-30T10:00", "1m"],
    ["2026-07-21", "1m"],
    ["2026-07-21T24:00", "1m"],
    ["2026-07-21T10:00", "unknown"],
  ]) {
    assert.throws(
      () => compute(value, preset),
      error => error instanceof TypeError && /invalid_followup_base_date/.test(error.message),
    );
  }
});
