// #451:折扣率口径——SaaS 单 total_fee=优惠前总额,折扣率=net_receivable/total_fee;
// 本地单(local-bill-*) total_fee 已是净额,折扣率=total/(total+优惠)。
// 沙箱 eval billDiscountRate 验证两种来源口径。
import { test } from "node:test";
import assert from "node:assert";
import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import { dirname, join } from "node:path";
import vm from "node:vm";

const here = dirname(fileURLToPath(import.meta.url));
const src = readFileSync(join(here, "..", "..", "local_app", "static", "workspace_billing.js"), "utf8");

const decl = src.slice(src.indexOf("function billDiscountRate"), src.indexOf("\nconst BILL_STATE_LABEL"));

const sandbox = {};
vm.createContext(sandbox);
vm.runInContext(decl + "\nthis.billDiscountRate = billDiscountRate;", sandbox);

test("SaaS 单:9400 优惠 1400 → 85%(net/total),不再 87%", () => {
  assert.strictEqual(
    sandbox.billDiscountRate({ bill_id: "12345", total_fee: 9400, discount_fee: "1400.00", net_receivable: 8000 }),
    "85%");
});

test("SaaS 单缺 net_receivable 回退 total-优惠", () => {
  assert.strictEqual(
    sandbox.billDiscountRate({ bill_id: "12345", total_fee: 9400, discount_fee: 1400 }),
    "85%");
});

test("本地单:净额 800 优惠 100 → 89%(旧公式仍适用)", () => {
  assert.strictEqual(
    sandbox.billDiscountRate({ bill_id: "local-bill-abc", total_fee: 800, discount_fee: 100 }),
    "89%");
});

test("无优惠不显示折扣率", () => {
  assert.strictEqual(sandbox.billDiscountRate({ bill_id: "12345", total_fee: 9400 }), "");
  assert.strictEqual(sandbox.billDiscountRate({ bill_id: "local-bill-abc", total_fee: 800, discount_fee: 0 }), "");
});
