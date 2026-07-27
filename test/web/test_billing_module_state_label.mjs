// 收费管理列表状态列原来直接输出 row.state 英文码(pending/partial/...),实机显示
// 与中文界面、患者收费 tab 口径不一致。改为复用 workspace_tabs.js 的 billStateLabel()。
// 跑：node --test test/web/test_billing_module_state_label.mjs
import { test } from "node:test";
import assert from "node:assert";
import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import { dirname, join } from "node:path";
import vm from "node:vm";

const here = dirname(fileURLToPath(import.meta.url));
const mvSrc = readFileSync(join(here, "..", "..", "local_app", "static", "billing_module.js"), "utf8");   // 收费模块已拆出
const wtSrc = readFileSync(join(here, "..", "..", "local_app", "static", "workspace_billing.js"), "utf8");

function extract(src, fnHeader) {
  const start = src.indexOf(fnHeader);
  assert.ok(start > 0, `应能定位 ${fnHeader}`);
  const end = src.indexOf("\n}", start) + 2;
  return src.slice(start, end);
}

// billStateLabel 依赖的两个常量 + 函数(与 test_bill_state_label.mjs 同抽取口径)
const labelDecl = wtSrc.slice(wtSrc.indexOf("const BILL_STATE_LABEL"), wtSrc.indexOf("\nfunction billStateClass"));
const rowCode = extract(mvSrc, "function renderBillingModuleRow");

function render(row) {
  const sandbox = {
    escapeHtml: s => String(s), escapeAttr: s => String(s),
    formatMoney: v => String(v == null ? "" : v), canRefund: () => false,
    moduleRowAction: () => "",
  };
  vm.createContext(sandbox);
  vm.runInContext(labelDecl + "\n" + rowCode + "\nthis.__f = renderBillingModuleRow;", sandbox);
  return sandbox.__f(row);
}

test("英文码渲染成中文标签,不再裸露 state", () => {
  const cases = [
    ["pending", "待收费"], ["partial", "部分收款"], ["paid", "已收清"],
    ["refunded", "已退费"], ["voided", "已作废"],
  ];
  for (const [state, label] of cases) {
    const html = render({bill_id: "local-bill-1", state, total_fee: 100, paid_fee: 0, unpaid_fee: 100});
    assert.ok(html.includes(`<span>${label}</span>`), `${state} 应显示「${label}」`);
    assert.ok(!html.includes(`<span>${state}</span>`), `${state} 不应裸露英文码`);
  }
});

test("外部系统 数字码按金额派生(billStateLabel 既有口径)", () => {
  const html = render({bill_id: "b1", state: "100", total_fee: 100, paid_fee: 40, unpaid_fee: 60});
  assert.ok(html.includes("<span>部分收款</span>"), "数字码+部分付款应派生「部分收款」");
});

test("收款/退费按钮判定仍用原始 state,不受显示映射影响", () => {
  const html = render({bill_id: "local-bill-1", state: "pending", total_fee: 100, paid_fee: 0, unpaid_fee: 100});
  assert.ok(html.includes("收款"), "pending 单仍有收款按钮");
});
