// 回归:收费管理内嵌报表三个统计请求(收入趋势/配台统计/今日营收)必须检查 HTTP 状态,
// 接口 500 时显示"载入失败",不能把错误伪装成 0.00 / "当天暂无配台"。
// 抽出三个函数源码,stub echarts/DOM/fetch,vm 真执行断言错误态渲染。
// 跑：node --test test/web/test_billing_module_error.mjs
import { test } from "node:test";
import assert from "node:assert";
import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import { dirname, join } from "node:path";
import vm from "node:vm";

const here = dirname(fileURLToPath(import.meta.url));
const src = readFileSync(join(here, "..", "..", "local_app", "static", "billing_module.js"), "utf8");

function extract(fnHeader) {
  const start = src.indexOf(fnHeader);
  assert.ok(start > 0, `应能定位 ${fnHeader}`);
  const end = src.indexOf("\n}", start) + 2;
  return src.slice(start, end);
}

const trendCode = extract("async function loadIncomeTrendChart");
const workloadCode = extract("async function loadStaffWorkload");
const collectionCode = extract("async function loadTodayCollection");

function makeSandbox(fetchImpl, {chartBox, staffBox, collBox}) {
  const chartOpts = [];
  const payMethodOpts = [];
  const sandbox = {
    fetch: fetchImpl,
    workDate: {value: "2026-07-15"},
    localDateValue: () => "2026-07-15",
    dateFromWorkValue: () => new Date(2026, 6, 15),
    cssToken: () => "#000",
    escapeHtml: s => String(s == null ? "" : s),
    formatMoney: v => "¥" + Number(v || 0).toFixed(2),
    formatCount: v => String(v || 0),
    renderPayMethodsChart: () => {},
    _echartsInit: id => ({ setOption: o => { (id === "chartPayMethods" ? payMethodOpts : chartOpts).push(o); } }),
    modulePanel: { querySelector: sel => {
      if (sel.includes("staff-workload")) return staffBox;
      if (sel.includes("today-collection")) return collBox;
      return null;
    } },
    Date, Number,
    document: { getElementById: () => chartBox },
    console,
  };
  vm.createContext(sandbox);
  vm.runInContext(
    trendCode + "\n" + workloadCode + "\n" + collectionCode +
    "\nthis.__trend = loadIncomeTrendChart; this.__workload = loadStaffWorkload;" +
    " this.__collection = loadTodayCollection;",
    sandbox,
  );
  return {sandbox, chartOpts, payMethodOpts};
}

const fail500 = async () => ({ok: false, status: 500, json: async () => ({detail: "boom"})});

test("收入趋势 500 → 图表显示载入失败,不画零值线", async () => {
  const chartBox = {};
  const {sandbox, chartOpts} = makeSandbox(fail500, {chartBox});
  await sandbox.__trend();
  const last = chartOpts[chartOpts.length - 1];
  assert.ok(JSON.stringify(last).includes("载入失败"), "应渲染载入失败标题");
  assert.ok(!(last.series && last.series.length), "失败时不应画数据系列(零值线)");
});

test("配台统计 500 → 显示载入失败,不显示'暂无配台记录'", async () => {
  const staffBox = {innerHTML: "", textContent: ""};
  const {sandbox} = makeSandbox(fail500, {staffBox});
  await sandbox.__workload();
  assert.ok(staffBox.innerHTML.includes("载入失败"), "应显示载入失败");
  assert.ok(!staffBox.innerHTML.includes("暂无配台记录"), "不能把错误伪装成暂无记录");
});

test("今日营收 500 → 显示载入失败,不渲染 0.00,且清掉旧收款方式饼图", async () => {
  const collBox = {innerHTML: "", textContent: ""};
  const {sandbox, payMethodOpts} = makeSandbox(fail500, {collBox});
  await sandbox.__collection();
  assert.ok(collBox.innerHTML.includes("载入失败"), "应显示载入失败");
  assert.ok(!collBox.innerHTML.includes("0.00"), "不能把错误伪装成今日实收 0.00");
  // 失败必须清饼图,否则切换日期失败时旧日期饼图残留冒充当前
  assert.ok(payMethodOpts.length >= 1, "失败路径必须更新收款方式饼图(清旧数据)");
  const last = payMethodOpts[payMethodOpts.length - 1];
  assert.ok(JSON.stringify(last).includes("载入失败"), "饼图应显示载入失败而非旧数据");
  assert.ok(!(last.series && last.series.length), "失败时饼图不应保留数据系列");
});
