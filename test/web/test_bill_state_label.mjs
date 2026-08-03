// 扫荡#392:退费单(state=refunded)在收费 tab 状态列必须显示「已退费」,不能落到「—」。
// 沙箱 eval billStateLabel + 两个常量,验证 refunded 映射。
import { test } from "node:test";
import assert from "node:assert";
import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import { dirname, join } from "node:path";
import vm from "node:vm";

const here = dirname(fileURLToPath(import.meta.url));
const src = readFileSync(join(here, "..", "..", "local_app", "static", "workspace_billing.js"), "utf8");

// 抽出两个常量 + billStateLabel 函数
const labelDecl = src.slice(src.indexOf("const BILL_STATE_LABEL"), src.indexOf("\nfunction billStateClass"));

const sandbox = {};
vm.createContext(sandbox);
vm.runInContext(labelDecl + "\nthis.billStateLabel = billStateLabel;", sandbox);

test("refunded → 已退费(不再落到 —)", () => {
  assert.strictEqual(sandbox.billStateLabel("refunded", 0, 0), "已退费");
});

test("已有状态仍正确", () => {
  assert.strictEqual(sandbox.billStateLabel("pending", 100, 0), "待收费");
  assert.strictEqual(sandbox.billStateLabel("paid", 100, 100), "已收清");
  assert.strictEqual(sandbox.billStateLabel("voided", 0, 0), "已作废");
});
