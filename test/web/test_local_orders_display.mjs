// 产品决策口径守卫:①处置 tab 不标数值角标;②本地处置单列表不显示撤销/作废单
// (退费的显示);③编辑/划价缓存仍存全量。源码不变量守卫,防后续 UI 调整冲掉口径。
import { test } from "node:test";
import assert from "node:assert";
import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import { dirname, join } from "node:path";

const here = dirname(fileURLToPath(import.meta.url));
const editorSrc = readFileSync(join(here, "..", "..", "local_app", "static", "treatment_order_editor.js"), "utf8");
const workspaceSrc = readFileSync(join(here, "..", "..", "local_app", "static", "patient_workspace.js"), "utf8");

test("处置 tab 角标映射已移除(不标数值)", () => {
  assert.ok(!workspaceSrc.includes("treatments: counts.treatment_items"),
    "处置 tab 不应再映射数字角标");
});

test("本地处置单列表滤掉 voided,渲染用过滤后的列表", () => {
  const fn = editorSrc.slice(editorSrc.indexOf("async function loadLocalOrders"),
                             editorSrc.indexOf("function renderLocalOrderCard"));
  assert.ok(fn.includes('!== "voided"'), "撤销/作废单不显示");
  assert.ok(fn.includes("shownOrders.map(renderLocalOrderCard)"), "渲染应走过滤后的 shownOrders");
  // 缓存存全量在过滤之前——编辑/划价按 order_id 查缓存不受影响
  assert.ok(fn.indexOf("localOrdersCache = orders") < fn.indexOf("shownOrders"));
});
