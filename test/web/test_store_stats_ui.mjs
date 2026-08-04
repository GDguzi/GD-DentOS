// 店内统计前端源码守卫:①报表 tab 含 医生/护士/助理/咨询师四统计;②分发到 loadRolePerfTab 各角色;
// ③store_stats.js 表头含 初诊/复诊×量/成交/实收/应收 + 未归类空态口径。防后续 UI 调整冲掉。
import { test } from "node:test";
import assert from "node:assert";
import { readFileSync, readdirSync } from "node:fs";
import { fileURLToPath } from "node:url";
import { dirname, join } from "node:path";

const here = dirname(fileURLToPath(import.meta.url));
const reportsSrc = readFileSync(join(here, "..", "..", "local_app", "static", "reports.js"), "utf8");
const storeSrc = readFileSync(join(here, "..", "..", "local_app", "static", "store_stats.js"), "utf8");

test("报表 tab 含四角色统计", () => {
  for (const key of ["docstat", "nursestat", "asststat", "consultstat"]) {
    assert.ok(reportsSrc.includes(`"${key}"`), `缺 tab ${key}`);
  }
});

test("四角色 tab 分发到对应 role", () => {
  for (const r of ["doctor", "nurse", "assistant", "consultant"]) {
    assert.ok(reportsSrc.includes(`loadRolePerfTab(b, "${r}")`), `缺 role 分发 ${r}`);
  }
});

test("store_stats 打 role-perf 接口 + 8列表头 + 未归类空态", () => {
  assert.ok(storeSrc.includes("/api/store-stats/role-perf"));
  assert.ok(storeSrc.includes("初诊") && storeSrc.includes("复诊"));
  assert.ok(storeSrc.includes("成交人数") && storeSrc.includes("实收金额") && storeSrc.includes("应收金额"));
  // 非医生角色空态提示如实、未归类仅有金额才显示
  assert.ok(storeSrc.includes("本地暂无该角色的配台数据"));
  assert.ok(storeSrc.includes("unclassified"));
});

test("收入统计/门诊日志 有清空+导出按钮(#680/#686)", () => {
  assert.ok(storeSrc.includes("data-inc-clear") && storeSrc.includes("data-inc-csv"));
  assert.ok(storeSrc.includes("/api/reports/income.csv"));
  assert.ok(storeSrc.includes("data-cl-clear"));
});

test("日期字符串不得对 bjToday() 直接 .slice(它返回 Date,不是字符串)——用 localDateValue()", () => {
  // #691 根因:bjToday().slice 在浏览器抛 'slice is not a function' → 报表卡"载入中"
  assert.ok(!/bjToday\(\)\s*\.\s*slice/.test(storeSrc),
    "bjToday() 返回 Date,不能 .slice;取北京日期字符串用 localDateValue()");
});

test("loadReportsModule 进入时重置跳转tab(billing)避免立刻跳走(#691顶层报表)", () => {
  assert.ok(/loadReportsModule[\s\S]*?_rptTab === "billing"\s*\)\s*_rptTab = "docstat"/.test(reportsSrc),
    "进报表模块应把停在 billing 跳转tab 的状态重置为默认");
});

test("store_stats 顶层 let/const/var 不与其他静态JS 撞名(全局作用域重复声明会致整文件加载失败)", () => {
  const staticDir = join(here, "..", "..", "local_app", "static");
  const files = readdirSync(staticDir).filter(f => f.endsWith(".js") && f !== "store_stats.js");
  const topDecl = src => {
    const names = new Set();
    for (const line of src.split("\n")) {
      // 只认顶格(0 缩进)声明=真·全局作用域；函数体内声明有缩进，不算
      const m = line.match(/^(?:let|const|var)\s+([A-Za-z_$][\w$]*)/);
      if (!m) continue;
      names.add(m[1]);
      // 逗号一行多声明 `let a = "", b = "";`——仅当本行无对象/数组字面量(避免把 {k:v} 的键当变量)
      if (!/[{[]/.test(line)) {
        for (const extra of line.matchAll(/,\s*([A-Za-z_$][\w$]*)\s*=/g)) names.add(extra[1]);
      }
    }
    return names;
  };
  const mine = topDecl(storeSrc);
  for (const f of files) {
    const other = topDecl(readFileSync(join(staticDir, f), "utf8"));
    for (const n of mine) {
      assert.ok(!other.has(n), `store_stats.js 顶层变量 ${n} 与 ${f} 撞名(会触发 SyntaxError: has already been declared)`);
    }
  }
});

// ---------- GD-07:未归类就诊量单列 + 空态区分 + 新建病历医生预填 ----------

test("GD-07:未归类行展示无法归类的就诊量,不只金额", () => {
  assert.ok(
    /u\.first\.visit|u\.revisit\.visit/.test(storeSrc),
    "未归类行必须带就诊量列",
  );
});

test("GD-07:空结果文案区分 确实无就诊 vs 存在无法归类就诊", () => {
  assert.ok(storeSrc.includes("该区间无就诊记录"), "保留纯空态文案");
  assert.ok(storeSrc.includes("无法唯一归类"), "有未归类数据时空态必须说明,不能装作没数据");
});

test("GD-07:新建病历预填当日唯一预约医生(可改),不唯一不猜", () => {
  const tabsSrc = readFileSync(join(here, "..", "..", "local_app", "static", "workspace_tabs.js"), "utf8");
  assert.ok(tabsSrc.includes("function defaultRecordDoctor"), "缺预填函数");
  assert.ok(/doctor_name:\s*defaultRecordDoctor\(\)/.test(tabsSrc), "新建病历必须走预填");
  assert.ok(/docs\.size === 1/.test(tabsSrc), "多候选必须放弃预填,不得取第一个");
});
