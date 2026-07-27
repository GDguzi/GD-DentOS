// 回归:诊所业务"今天/现在"必须按北京时间(UTC+8),不受机器/浏览器系统时区影响。
// 病根:宿主机时区漂到 America/Los_Angeles 时,前端 new Date() 的本地日期比北京慢一天,
// 工作日期(07-03)与后端落库/账单号(北京 07-04)拆成两天。
// 沙箱加载 helpers.js,把 Date 钉在固定时刻(北京 07-04 10:30 = LA 07-03 19:30),
// 断言 localDateValue()/bjNowStr() 输出北京口径。
// 跑：node --test test/web/test_bj_date.mjs
import { test } from "node:test";
import assert from "node:assert";
import { readFileSync, readdirSync } from "node:fs";
import { fileURLToPath } from "node:url";
import { dirname, join } from "node:path";
import vm from "node:vm";

const here = dirname(fileURLToPath(import.meta.url));
const staticDir = join(here, "..", "..", "local_app", "static");
const helpersSrc = readFileSync(join(staticDir, "helpers.js"), "utf8");

// 北京 2026-07-04 10:30:00 = UTC 2026-07-04 02:30:00 = 洛杉矶 2026-07-03 19:30:00(PDT)
const FIXED_EPOCH = Date.UTC(2026, 6, 4, 2, 30, 0);

class FixedDate extends Date {
  constructor(...args) {
    if (args.length === 0) super(FIXED_EPOCH);
    else super(...args);
  }
  static now() { return FIXED_EPOCH; }
}

function loadHelpers() {
  const ctx = { window: {}, Date: FixedDate };
  vm.createContext(ctx);
  vm.runInContext(helpersSrc, ctx);
  return ctx.window;
}

test("localDateValue() 无参默认 = 北京今天,与机器时区无关", () => {
  const w = loadHelpers();
  assert.strictEqual(w.localDateValue(), "2026-07-04");
});

test("bjToday() 本地字段 = 北京墙上日期", () => {
  const w = loadHelpers();
  const d = w.bjToday();
  assert.strictEqual(d.getFullYear(), 2026);
  assert.strictEqual(d.getMonth() + 1, 7);
  assert.strictEqual(d.getDate(), 4);
});

test("bjNowStr() = 北京 YYYY-MM-DD HH:MM:SS", () => {
  const w = loadHelpers();
  assert.strictEqual(w.bjNowStr(), "2026-07-04 10:30:00");
});

test("dateFromWorkValue 空值回退北京今天", () => {
  const w = loadHelpers();
  const d = w.dateFromWorkValue("");
  assert.strictEqual(d.getDate(), 4);
});

test("显式传参的 localDateValue 不受影响(格式化墙上日期)", () => {
  const w = loadHelpers();
  assert.strictEqual(w.localDateValue(new FixedDate(2026, 0, 15)), "2026-01-15");
});

// 源码守卫:业务代码不得再用浏览器本地/UTC 口径当"今天/现在"。
// (patient_module.js 的 new Date().getTime() 是缓存戳,时区无关,不在禁用面。)
test("static/*.js 不得出现浏览器时区口径的业务日期取法", () => {
  const banned = [
    "new Date().toISOString",      // UTC 日期当今天
    "localISO(new Date())",        // 机器本地日期当今天
    "new Date().toLocaleString",   // 机器本地时间当"现在"
  ];
  for (const f of readdirSync(staticDir).filter(x => x.endsWith(".js"))) {
    const src = readFileSync(join(staticDir, f), "utf8");
    for (const pat of banned) {
      assert.ok(!src.includes(pat), `${f} 含被禁用法 ${pat}(业务日期须走 localDateValue()/bjToday()/bjNowStr())`);
    }
  }
});
