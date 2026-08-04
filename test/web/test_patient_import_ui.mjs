// 患者批量导入入口:配置管理→数据备份页(数据进出集中一处)→ 预览确认 → commit。
// 患者管理工具栏不再放导入按钮。
import { test } from "node:test";
import assert from "node:assert";
import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import { dirname, join } from "node:path";

const here = dirname(fileURLToPath(import.meta.url));
const cfg = readFileSync(join(here, "..", "..", "local_app", "static", "config_center.js"), "utf8");
const pm = readFileSync(join(here, "..", "..", "local_app", "static", "patient_module.js"), "utf8");

test("导入入口在数据备份页,患者管理工具栏不再有", () => {
  assert.ok(cfg.includes("importPatientsFile"), "备份页必须有导入入口");
  assert.ok(/数据导入/.test(cfg), "备份页有数据导入区块");
  assert.ok(!pm.includes("importPatientsFile"), "患者管理不再放导入按钮");
});

test("导入流程:先 preview 确认再 commit", () => {
  const fn = cfg.slice(cfg.indexOf("function importPatientsFile"));
  assert.ok(fn.includes('append("mode", "preview")'), "先预览");
  assert.ok(fn.includes("window.confirm"), "预览后必须用户确认");
  assert.ok(fn.includes('append("mode", "commit")'), "确认后才 commit");
});
