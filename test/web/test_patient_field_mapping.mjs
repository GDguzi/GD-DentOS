// 患者基本资料「其他字段」不该堆未翻译英文:导入数据带来的键必须 已映射(SOURCE_FIELD_GROUPS)
// 或 已隐藏(SOURCE_HIDDEN_KEYS)。守源码不变量,防同步带新键时又退化成英文堆。
// 跑：node --test test/web/test_patient_field_mapping.mjs
import { test } from "node:test";
import assert from "node:assert";
import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import { dirname, join } from "node:path";

const here = dirname(fileURLToPath(import.meta.url));
const src = readFileSync(join(here, "..", "..", "local_app", "static", "patient_workspace.js"), "utf8");

// 实测从导入数据取到、曾落「其他字段」的英文键
const IMPORTED_INFO_KEYS = [
  "agebirthday", "birthday1", "chainpat", "clinicname", "disconut", "equitycardtype",
  "name", "patgroupid", "picture", "registeridentity", "vipover", "vipoverdate", "ybreadinfo",
];

test("导入数据英文键全部已映射或隐藏(不再堆英文)", () => {
  const missing = IMPORTED_INFO_KEYS.filter(k => !src.includes(`"${k}"`));
  assert.deepEqual(missing, [], "这些键既没映射中文也没隐藏,会以英文落「其他字段」: " + missing.join(","));
});

test("常见来源/营销键已给中文标签", () => {
  for (const k of ["disconut", "equitycardtype", "vipover", "comefrom3", "counselor"]) {
    assert.ok(src.includes(`"${k}": {group:`), `${k} 应在 SOURCE_FIELD_GROUPS 给中文标签`);
  }
});
