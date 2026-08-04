// 回归：预约相关代码或样式修复后，index 必须更新查询版本，避免浏览器继续运行旧资源。
// 跑：node --test test/web/test_appt_asset_versions.mjs
import { test } from "node:test";
import assert from "node:assert";
import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import { dirname, join } from "node:path";

const here = dirname(fileURLToPath(import.meta.url));
const index = readFileSync(join(here, "..", "..", "local_app", "static", "index.html"), "utf8");
const version = "2026-08-04-r3";

test("预约与挂号相关静态资源统一使用本轮缓存版本", () => {
  const assets = [
    "styles.css",
    "helpers.js",
    "check_in.js",
    "today_work.js",
    "patient_module.js",
    "treatment_order_editor.js",
    "appointment_editor.js",
    "appointment_grid.js",
    "patient_workspace.js",
    "app.js",
  ];
  for (const asset of assets) {
    assert.ok(index.includes(`/${asset}?v=${version}`), `${asset} 仍未更新到 ${version}`);
  }
  assert.ok(index.includes(`APP_ASSET_VERSION: ${version}`), "页面资源版本标记应与 app.js 可见版本一致");
  assert.ok(index.indexOf("/helpers.js?v=") < index.indexOf("/check_in.js?v="), "check_in.js 应在 helpers.js 之后加载");
  assert.ok(index.indexOf("/check_in.js?v=") < index.indexOf("/today_work.js?v="), "check_in.js 应在挂号入口调用方之前加载");
});
