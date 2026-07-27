// 客户通 V2 与独立回访卡片的源码守卫。
import { test } from "node:test";
import assert from "node:assert";
import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import { dirname, join } from "node:path";

const here = dirname(fileURLToPath(import.meta.url));
const S = f => readFileSync(join(here, "..", "..", "local_app", "static", f), "utf8");
const hub = S("customer_hub_v2.js"), card = S("return_visit_card.js"), html = S("index.html");

test("V2 回访行点击打开处理弹窗，批删逐行携带 revision", () => {
  assert.match(hub, /data-chv-open=/, "回访行应保留 V2 打开标记");
  assert.match(
    hub,
    /const row = e\.target\.closest\("\[data-chv-open\]"\);[\s\S]{0,160}_chvOpenRvModal\(row\.dataset\.chvOpen, row\.dataset\.chvPid\)/,
    "行点击应交给 _chvOpenRvModal",
  );
  assert.match(hub, /class="chv-ck"[\s\S]{0,160}data-chv-revision=/,
    "批删复选框应绑定当前 revision");
  assert.match(hub, /data-chv-batchdel=/, "回访列表应保留批量删除入口");
  assert.match(hub, /action:\s*"batchDelete"[\s\S]{0,120}ids,[\s\S]{0,120}revisions/,
    "批删请求应同时传 ids 和逐行 revisions");
});

test("V2 回访日期区间由 data-chv-range 打开，保留三个快捷预设", () => {
  assert.match(hub, /data-chv-range="1"/, "列表工具栏应有日期区间入口");
  assert.match(hub, /function _chvOpenRange\(\)/, "区间入口应由 V2 独立实现");
  assert.match(hub, /closest\("\[data-chv-range\]"\)[\s\S]{0,80}_chvOpenRange\(\)/,
    "全局委托应从 data-chv-range 调用 _chvOpenRange");
  const rangeFn = hub.slice(hub.indexOf("function _chvOpenRange"), hub.indexOf("// ============ 记沟通弹窗"));
  assert.match(rangeFn, /data-chv-preset=/, "区间弹窗应保留快捷预设");
  const compact = rangeFn.replace(/\s+/g, "");
  for (const label of ["今天", "近7天", "近30天"]) {
    assert.ok(compact.includes(label), `区间弹窗应有 ${label}`);
  }
});

test("回访卡片:编辑/标记已回访/电话录音/微信截图 + 失败不静默", () => {
  assert.ok(html.includes('id="rvCardOverlay"'), "index 应有卡片弹窗容器");
  assert.ok(html.includes("/return_visit_card.js?v="), "index 应挂 return_visit_card.js");
  for (const fn of ["openReturnVisitCard", "saveRvCard", "markRvCardDone", "uploadRvCardAudio", "uploadRvCardImages", "deleteRvCardImage", "deleteRvCardCall"]) {
    assert.ok(card.includes(fn), `卡片应有 ${fn}`);
  }
  assert.ok(card.includes("linked_return_visit_id"), "电话录音应弱关联本回访");
  assert.ok(card.includes("/return-visits/") && card.includes("/images"), "应对接回访详情+截图接口");
  assert.match(card, /uploadRvCardImages[\s\S]*?response\.ok[\s\S]*?fail/, "截图上传应逐个检查响应");
  assert.match(card, /deleteRvCardImage[\s\S]*?res\.ok[\s\S]*?alert/, "删截图失败应告警");
  assert.match(card, /Object\.assign\(window,[\s\S]*?openReturnVisitCard/, "卡片函数应暴露 window");
});

test("修2 状态下拉保留 导入原始状态值,不静默改回待回访", () => {
  assert.ok(card.includes("stdVals") && card.includes("原值"),
    "非标准状态(done/完成/4)应补当前原值选项并选中,避免保存时被覆盖为待回访");
});

test("独立回访卡片保留快捷录音，不再绑定退役 study 视图", () => {
  assert.ok(card.includes("toggleRvCardMicRecord") && card.includes("快捷录音"), "卡片应有快捷录音按钮");
  assert.ok(card.includes("return_visit_mobile") && card.includes("_rvCardGen"), "快捷录音应标 source 且用卡片代数锁会话防挂错");
});

test("快捷录音防麦克风泄漏:关卡片停录 + 开麦异步防二次点击", () => {
  // 关卡片时若在录音必须 stop(释放麦克风),否则后台继续录环境声
  assert.match(card, /function closeReturnVisitCard[\s\S]*?_rvMicRec[\s\S]*?state === "recording"[\s\S]*?\.stop\(\)/,
    "closeReturnVisitCard 应在录音中停止录音器释放麦克风");
  // 开麦异步窗口防并发申请两条流
  assert.ok(card.includes("_rvMicStarting"), "应有开麦 in-flight 标志防二次点击");
  assert.match(card, /if \(gen !== _rvCardGen\)[\s\S]*?getTracks\(\)\.forEach[\s\S]*?stop\(\)/,
    "开麦期间卡片代数变了(关/切/重开)应弃流(stop tracks),不起录音机");
});
