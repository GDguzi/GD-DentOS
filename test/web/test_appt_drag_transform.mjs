// 回归守卫(node:test)：重叠并排块的内联 translateX 在拖拽/点击后必须还原，不得被清空。
// 无 DOM 测试栈，改守源码不变量——startApptBlockDrag 的 onUp 必须还原 origTransform。
// 跑：node --test test/web/test_appt_drag_transform.mjs
import { test } from "node:test";
import assert from "node:assert";
import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import { dirname, join } from "node:path";

const here = dirname(fileURLToPath(import.meta.url));
const src = readFileSync(join(here, "..", "..", "local_app", "static", "appointment_grid.js"), "utf8");

// 截出 startApptBlockDrag 函数体
const fn = src.slice(src.indexOf("function startApptBlockDrag"));
const body = fn.slice(0, fn.indexOf("\n}\n") + 2);

test("拖拽开始时快照 origTransform", () => {
  assert.match(body, /const\s+origTransform\s*=\s*blk\.style\.transform/,
    "应在 startApptBlockDrag 入口快照 blk.style.transform");
});

test("onUp 还原 origTransform 而非无条件清空", () => {
  assert.match(body, /blk\.style\.transform\s*=\s*origTransform/,
    "松手应把 transform 还原成 origTransform");
  assert.ok(!/blk\.style\.transform\s*=\s*["']{2}\s*;/.test(body),
    "不得保留 blk.style.transform = \"\" 这种无条件清空(会抹掉并排 translateX)");
});
