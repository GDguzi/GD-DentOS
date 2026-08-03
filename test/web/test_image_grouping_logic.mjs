// 影像组图纯函数单测:vm 求值整个 image_grouping.js(其顶层只有常量与函数声明,无 DOM)。
import { test } from "node:test";
import assert from "node:assert";
import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import { dirname, join } from "node:path";
import vm from "node:vm";

const here = dirname(fileURLToPath(import.meta.url));
const src = readFileSync(join(here, "..", "..", "local_app", "static", "image_grouping.js"), "utf8");
const sandbox = {};
vm.createContext(sandbox);
vm.runInContext(src + `
this.groupImagesByDate = groupImagesByDate;
this.monthsBetween = monthsBetween;
this.buildPeriodModel = buildPeriodModel;
this.IG_FACE_SLOTS = IG_FACE_SLOTS;
this.IG_INTRA_SLOTS = IG_INTRA_SLOTS;
this.IG_XRAY_SLOTS = IG_XRAY_SLOTS;
this.buildTemplateModel = buildTemplateModel;
this.buildCompareModel = buildCompareModel;
this.splitOthersForTab = splitOthersForTab;
this.igIsDcm = igIsDcm;
this.igToggleCompare = igToggleCompare;
this.igCollagePlan = igCollagePlan;
this.igIsSetDate = igIsSetDate;
`, sandbox);

const im = (date, category, id) => ({ image_id: id || Math.random().toString(16).slice(2), image_date: date, category, file_name: id ? id + ".jpg" : "x.jpg" });
// vm 沙箱数组与宿主 Array.prototype 不同源,deepStrictEqual 会因原型不等而失败;JSON 归一化后再比
const j = x => JSON.parse(JSON.stringify(x));

test("按日分组:升序、日期取前10位、无日期归'未知日期'", () => {
  const g = sandbox.groupImagesByDate([im("2026-03-14 09:00", "正面像"), im("2024-06-29", "正面像"), im("", "其他")]);
  assert.deepStrictEqual(j(g.map(x => x.date)), ["2024-06-29", "2026-03-14", "未知日期"]);
  assert.strictEqual(g[0].images.length, 1);
});

test("月数:整年月差,不按日修正,负数钳为0", () => {
  assert.strictEqual(sandbox.monthsBetween("2024-06-29", "2025-08-18"), 14);
  assert.strictEqual(sandbox.monthsBetween("2024-06-29", "2024-06-30"), 0);
  assert.strictEqual(sandbox.monthsBetween("2024-06-29", "2024-05-01"), 0);
});

test("期模型:首期0个月,带张数", () => {
  const p = sandbox.buildPeriodModel([im("2024-06-29", "正面像"), im("2025-08-18", "正面像"), im("2025-08-18", "覆盖像")]);
  assert.deepStrictEqual(j(p.map(x => [x.date, x.months, x.count])),
    [["2024-06-29", 0, 1], ["2025-08-18", 14, 2]]);
});

test("映射表:口外10位/口内6位/X光4位,与库内标签逐字一致", () => {
  assert.deepStrictEqual(j(sandbox.IG_FACE_SLOTS),
    ["右侧面像", "右侧面微笑像", "右45度像", "右45度微笑像", "正面像", "正面微笑像", "左45度微笑像", "左45度像", "左侧面微笑像", "左侧面像"]);
  assert.deepStrictEqual(j(sandbox.IG_INTRA_SLOTS),
    ["上颌合面像", "覆盖像", "右侧咬合像", "正面咬合像", "左侧咬合像", "下颌合面像"]);
  assert.deepStrictEqual(j(sandbox.IG_XRAY_SLOTS), ["全景片", "正位片", "侧位片", "小牙片"]);
});

test("模板模型:标签入位,同位多张后者顶位、前者落散图,未映射标签落散图", () => {
  const a = im("2026-03-14", "正面像", "a"), b = im("2026-03-14", "正面像", "b");
  const m = sandbox.buildTemplateModel([a, b, im("2026-03-14", "SC", "c"), im("2026-03-14", "侧面像", "d")]);
  assert.strictEqual(m.slots["正面像"].image_id, "b");
  assert.deepStrictEqual(j(m.others.map(x => x.image_id)), ["a", "c", "d"]);
});

test("对比模型:按标准位序逐行,单边有也出行,双边全无不出行", () => {
  const A = sandbox.buildTemplateModel([im("2024-06-29", "正面像", "a1"), im("2024-06-29", "全景片", "a2")]);
  const B = sandbox.buildTemplateModel([im("2026-03-14", "正面像", "b1"), im("2026-03-14", "覆盖像", "b2")]);
  const rows = sandbox.buildCompareModel(A, B);
  assert.deepStrictEqual(j(rows.map(r => r.label)), ["正面像", "覆盖像", "全景片"]);
  assert.strictEqual(rows[0].left.image_id, "a1");
  assert.strictEqual(rows[0].right.image_id, "b1");
  assert.strictEqual(rows[1].left, null);
});

test("tab过滤散图:xray只留CR/DX/PX和真.dcm文件;category=dcm的jpg是口内照归others", () => {
  const others = [im("d", "SC", "s"), im("d", "dcm", "t"), im("d", "CR", "u"),
    { image_id: "v", image_date: "d", category: "其他", file_name: "raw.dcm" }];
  assert.deepStrictEqual(j(sandbox.splitOthersForTab(others, "xray").map(x => x.image_id)), ["u", "v"]);
  assert.deepStrictEqual(j(sandbox.splitOthersForTab(others, "others").map(x => x.image_id)), ["s", "t"]);
  assert.strictEqual(sandbox.splitOthersForTab(others, "all").length, 4);
});

test("对比勾选:最多2期,勾第3个顶掉最早勾的;重复勾=取消", () => {
  assert.deepStrictEqual(j(sandbox.igToggleCompare([], "a")), ["a"]);
  assert.deepStrictEqual(j(sandbox.igToggleCompare(["a"], "b")), ["a", "b"]);
  assert.deepStrictEqual(j(sandbox.igToggleCompare(["a", "b"], "c")), ["b", "c"]);
  assert.deepStrictEqual(j(sandbox.igToggleCompare(["a", "b"], "a")), ["b"]);
});

test("dcm判定:只看扩展名——category=dcm的.jpg是WADO渲染口内照,要正常显示", () => {
  assert.ok(!sandbox.igIsDcm({ category: "dcm", file_name: "x.jpg" }));
  assert.ok(sandbox.igIsDcm({ category: "其他", file_name: "y.DCM" }));
  assert.ok(!sandbox.igIsDcm({ category: "其他", file_name: "z.jpg" }));
});

test("拼图布局:三区带图入格,空位跳过,格子不越界", () => {
  const model = { slots: { "正面像": im("d", "正面像", "a"), "全景片": im("d", "全景片", "b") }, others: [im("d", "SC", "c"), im("d", "dcm", "e")] };
  model.others.push({ image_id: "f", image_date: "d", category: "其他", file_name: "raw.dcm" });
  const plan = sandbox.igCollagePlan(model);
  assert.ok(plan.width > 0 && plan.height > 0);
  assert.strictEqual(plan.cells.filter(c => c.image).length, 4);          // 真.dcm文件不进拼图,dcm.jpg口内照进
  assert.ok(plan.cells.every(c => c.x >= 0 && c.y >= 0 && c.x + c.w <= plan.width && c.y + c.h <= plan.height));
  assert.deepStrictEqual(j(plan.cells.map(c => c.section)), ["口外", "X光", "其他", "其他"]);
});

test("拼图布局:空模型无格子", () => {
  const plan = sandbox.igCollagePlan({ slots: {}, others: [] });
  assert.strictEqual(plan.cells.length, 0);
});

test("组图判定:只认落库标记,有标签也不自动算组图(用户拍板)", () => {
  assert.ok(sandbox.igIsSetDate(["2025-07-14"], "2025-07-14"));
  assert.ok(!sandbox.igIsSetDate(["2025-07-14"], "2026-07-03"));
  assert.ok(!sandbox.igIsSetDate([], "2026-07-03"));
  assert.ok(!sandbox.igIsSetDate(null, "2026-07-03"));
});
