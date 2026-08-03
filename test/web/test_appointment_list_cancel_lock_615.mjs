// #615:预约列表视图原来只把"已取消"/"3"视为不可取消,"完成治疗"等终态行仍渲染启用的
// 取消按钮——实机点击会弹出取消原因确认框,确认后会把已完成的预约清成"已取消"并清空
// 到达/完成时间戳。改成复用今日队列同一套 tqStage() 终态判定，不再各写一套口径。
// 跑：node --test test/web/test_appointment_list_cancel_lock_615.mjs
import { test } from "node:test";
import assert from "node:assert";
import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import { dirname, join } from "node:path";
import vm from "node:vm";

const here = dirname(fileURLToPath(import.meta.url));
const twSrc = readFileSync(join(here, "..", "..", "local_app", "static", "today_work.js"), "utf8");
const avSrc = readFileSync(join(here, "..", "..", "local_app", "static", "appointment_views.js"), "utf8");

function extract(src, fnHeader) {
  const start = src.indexOf(fnHeader);
  assert.ok(start > 0, `应能定位 ${fnHeader}`);
  const end = src.indexOf("\n}", start) + 2;
  return src.slice(start, end);
}

const tqStageCode = extract(twSrc, "function tqStage");
const rowCode = extract(avSrc, "function appointmentListRow");

function render(status) {
  const sandbox = {escapeAttr: s => String(s), escapeHtml: s => String(s), apptStatusLabel: s => String(s)};
  vm.createContext(sandbox);
  vm.runInContext(tqStageCode + "\n" + rowCode + "\nthis.__f = appointmentListRow;", sandbox);
  return sandbox.__f({appointment_id: "a1", status});
}

for (const status of ["完成治疗", "已完成", "完成", "已离开", "已取消", "3"]) {
  test(`#615 状态"${status}" → 不可取消(不渲染取消按钮)`, () => {
    assert.ok(!render(status).includes("appt-cancel"), `${status} 不应该有可点的取消按钮`);
  });
}

for (const status of ["已到诊", "已确认", "已预约", ""]) {
  test(`#615 状态"${status || "(空)"}" → 仍可取消(未终态,不误伤)`, () => {
    assert.ok(render(status).includes("appt-cancel"), `${status || "(空)"} 应该保留可取消`);
  });
}
