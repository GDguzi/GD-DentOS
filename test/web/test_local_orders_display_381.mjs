// #381 用户拍板口径守卫:①处置 tab 不标数值角标;②本地处置单列表不显示撤销/作废单
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
    "#381:处置 tab 不应再映射数字角标");
});

test("本地处置单列表滤掉 voided,渲染用过滤后的列表", () => {
  const fn = editorSrc.slice(editorSrc.indexOf("async function loadLocalOrders"),
                             editorSrc.indexOf("function renderLocalOrderCard"));
  assert.ok(fn.includes('!== "voided"'), "#381:撤销/作废单不显示");
  assert.ok(fn.includes("shownOrders.map(renderLocalOrderCard)"), "渲染应走过滤后的 shownOrders");
  // 缓存存全量在过滤之前——编辑/划价按 order_id 查缓存不受影响
  assert.ok(fn.indexOf("localOrdersCache = orders") < fn.indexOf("shownOrders"));
});

test("到达空单闸门只看今天的单——复诊老患者有历史单也要能出今日空单", () => {
  const fn = editorSrc.slice(editorSrc.indexOf("async function loadLocalOrders"),
                             editorSrc.indexOf("function renderLocalOrderCard"));
  // 旧 bug:orders.some 扫全部历史单,老患者(昨天有已收费单)今天到达永远不触发 ensure-arrival。
  // 正确口径与后端一致:仅"今日"未撤销单才算已有单。
  assert.ok(/order_date[\s\S]{0,80}localDateValue\(\)|localDateValue\(\)[\s\S]{0,120}order_date/.test(fn),
    "闸门必须按 order_date 限定今天");
  assert.ok(!/const hasActiveOrder = orders\.some\(o => o\.status !== "voided"\);/.test(fn),
    "禁止跨日扫全量单");
});
