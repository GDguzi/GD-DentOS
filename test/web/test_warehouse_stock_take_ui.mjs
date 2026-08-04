// GD-08:库存模块补齐"新建盘点单"入口——账面只读/实盘可编辑/差异实时,
// 经手人自由输入选人,同一物品禁重复,保存走 POST /api/stock-take(草稿)。
import { test } from "node:test";
import assert from "node:assert";
import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import { dirname, join } from "node:path";

const here = dirname(fileURLToPath(import.meta.url));
const src = readFileSync(join(here, "..", "..", "local_app", "static", "warehouse.js"), "utf8");

test("GD-08:工具栏有 + 新建盘点单 按钮并绑定表单", () => {
  assert.ok(src.includes("data-wh-new-take"), "缺新建盘点单按钮");
  assert.ok(src.includes("+ 新建盘点单"), "按钮文案");
  assert.ok(/data-wh-new-take.*?addEventListener|querySelector\("\[data-wh-new-take\]"\)\.addEventListener/s.test(src),
    "按钮必须绑定点击");
});

test("GD-08:盘点表单从 stock-balance 取账面数,实盘可编辑,差异实时显示", () => {
  assert.ok(src.includes("/api/stock-balance"), "物品与账面数来自 stock-balance");
  assert.ok(src.includes("data-tf=\"actual_qty\""), "实盘数量输入");
  assert.ok(src.includes("data-take-diff"), "差异实时显示位");
});

test("GD-08:同一物品禁止重复添加", () => {
  assert.ok(src.includes("已添加过"), "重复物品要有提示");
});

test("GD-08:保存只提交 stock_item_id+actual_qty,账面/差异由后端重算", () => {
  assert.ok(src.includes('"/api/stock-take"'), "保存走 POST /api/stock-take");
  assert.ok(!/payload[^;]*book_qty/.test(src), "不得提交浏览器算的 book_qty");
  assert.ok(!/payload[^;]*diff/.test(src), "不得提交浏览器算的 diff");
});

test("GD-08:经手人保留自由输入选人(datalist)", () => {
  assert.ok(/新建盘点单[\s\S]*?data-staff-role="\*"/.test(src), "盘点经手人挂选人 datalist");
});

test("GD-08:确认前二次提示且 409 已确认单不再变库存(既有机制不退化)", () => {
  assert.ok(src.includes("确认后库存即变动"), "确认前二次提示保留");
  assert.ok(src.includes("stock-take") && src.includes("/confirm"), "确认走 confirm 接口");
});
