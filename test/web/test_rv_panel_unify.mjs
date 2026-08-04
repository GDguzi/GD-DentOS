// 回访体验批次守卫:①档案客户沟通复用客户通处理弹窗 ②新增回访边打字边搜+计划快捷
// ③回访计划弹窗内可维护 ④处理弹窗内可上传录音文件(自动挂本回访)
import { test } from "node:test";
import assert from "node:assert";
import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import { dirname, join } from "node:path";

const here = dirname(fileURLToPath(import.meta.url));
const chv = readFileSync(join(here, "..", "..", "local_app", "static", "customer_hub_v2.js"), "utf8");
const tabs = readFileSync(join(here, "..", "..", "local_app", "static", "workspace_tabs.js"), "utf8");

test("档案客户沟通的回访/编辑走客户通同一个处理弹窗", () => {
  assert.ok(tabs.includes("openReturnVisitPanelFromWorkspace"), "卡片按钮换弹窗入口");
  assert.ok(/openReturnVisitPanelFromWorkspace[\s\S]*_chvOpenRvModal/.test(tabs), "复用 _chvOpenRvModal");
  assert.ok(!/onclick="toggleReturnVisitEdit/.test(tabs), "旧内联编辑入口必须撤下");
});

test("新增回访患者搜索边打字边搜(input防抖),不再只认回车", () => {
  const fn = chv.slice(chv.indexOf("function _chvBindPatientPick"));
  assert.ok(fn.includes('addEventListener("input"'), "input 事件实时搜");
  assert.ok(/setTimeout|debounce/.test(fn.slice(0, 800)), "带防抖");
});

test("新增回访有计划快捷(自动填时间+事项),计划=天数+内容可弹窗内维护", () => {
  assert.ok(chv.includes("chvNrPlan"), "新增回访带计划下拉");
  assert.ok(chv.includes("_chvOpenPlanManager"), "计划维护入口");
  assert.ok(/describe:\s*typeof item\.describe/.test(chv), "计划要带回访内容字段");
  assert.ok(/action:\s*"create_plan"/.test(chv) && /action:\s*"stop_plan"/.test(chv),
    "计划维护必须走合同层字典写(呈现层禁止直连 fetch)");
  const contract = readFileSync(join(here, "..", "..", "local_app", "static", "customer_hub_v2_contract.js"), "utf8");
  assert.ok(contract.includes('"POST", "/api/dictionaries"'), "合同层持有计划新建请求");
});

test("处理弹窗内可上传录音文件并自动挂本回访", () => {
  assert.ok(chv.includes("_chvUploadCallButton"), "上传录音按钮");
  const fn = chv.slice(chv.indexOf("function _chvUploadCallButton"));
  assert.ok(fn.includes("linkedReturnVisitId: rvId"), "必须自动挂当条回访");
  assert.ok(fn.includes('action: "attachment"'), "与直录同一条保存链路");
});
