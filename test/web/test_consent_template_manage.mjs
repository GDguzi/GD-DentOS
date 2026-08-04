// 同意书模板维护在签署弹窗内完成:空态可现场新建;保存走 POST 并自动选中;不再指去不存在的配置页。
import { test } from "node:test";
import assert from "node:assert";
import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import { dirname, join } from "node:path";

const here = dirname(fileURLToPath(import.meta.url));
const src = readFileSync(join(here, "..", "..", "local_app", "static", "consent_form.js"), "utf8");

test("空态不再指去不存在的配置页,给现场新建入口", () => {
  assert.ok(!src.includes("配置管理 / 同意书模板"), "旧文案指向不存在的页面,必须删");
  assert.ok(src.includes("showConsentTemplateForm"), "选择器里有新建模板入口");
  assert.ok(src.includes("＋ 新建模板"), "新建按钮文案");
});

test("新建模板走 POST 并保存后自动选中开签", () => {
  const fn = src.slice(src.indexOf("async function submitConsentTemplate"));
  assert.ok(fn.includes('fetch("/api/consent-templates"') && fn.includes('method: "POST"'), "保存走新建接口");
  assert.ok(fn.includes("pickConsentTemplate"), "存完自动选中");
  assert.ok(/名称、类别、正文都要填/.test(fn), "必填校验");
});

test("预约弹窗患者搜索网络失败给提示,不再静默装作搜不到", () => {
  const editor = readFileSync(join(here, "..", "..", "local_app", "static", "appointment_editor.js"), "utf8");
  assert.ok(editor.includes("搜索失败（网络异常）"), "失败必须有反馈");
});

test("已签列表可页内预览并标注电子签/纸质签", () => {
  assert.ok(src.includes("viewConsentDocument"), "已签行必须有查看入口");
  assert.ok(src.includes("cs-method-tag"), "已签行必须标签署方式");
  assert.ok(src.includes("纸质签署，签名在打印件上"), "纸质签预览要说明签名在纸面");
  assert.ok(/viewConsentDocument[\s\S]*hash_valid/.test(src), "预览必须带哈希校验结果");
});
