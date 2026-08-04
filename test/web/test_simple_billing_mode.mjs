// 简易收费模式(配置管理开关):开启后处置编辑器多一个「处置并结算」——
// 新单走 price_now 原价出待收费单/编辑单补划价,保存后直接跳收费 tab。默认关,不打扰现有流程。
import { test } from "node:test";
import assert from "node:assert";
import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import { dirname, join } from "node:path";

const here = dirname(fileURLToPath(import.meta.url));
const source = name => readFileSync(
  join(here, "..", "..", "local_app", "static", name),
  "utf8",
);

test("配置管理有简易收费模式开关并随保存提交", () => {
  const cfg = source("config_center.js");
  assert.ok(cfg.includes('data-shop="simple_billing_enabled"'), "缺开关复选框");
  assert.ok(/simple_billing_enabled:\s*val\("simple_billing_enabled"\)\.checked/.test(cfg),
    "保存必须提交该开关");
});

test("处置弹窗有隐藏的「处置并结算」按钮,开关开才显示", () => {
  const index = source("index.html");
  assert.ok(/id="orderSettleBtn"[^>]*hidden/.test(index), "按钮默认隐藏");
  assert.ok(index.includes("处置并结算"), "按钮文案");
  const editor = source("treatment_order_editor.js");
  assert.ok(editor.includes("simple_billing_enabled"), "编辑器按开关控制按钮显隐");
});

test("结算路径:新单 price_now 原价出单/编辑单补划价,完成后跳收费 tab", () => {
  const editor = source("treatment_order_editor.js");
  assert.ok(/price_now:\s*settleNow/.test(editor), "新单结算走后端 price_now 一步出待收费单");
  assert.ok(editor.includes("/price"), "编辑既有单结算须补划价");
  assert.ok(/settleNow[\s\S]*switchWorkspaceTab\("billing"\)/.test(editor), "结算后直接跳收费 tab");
});
