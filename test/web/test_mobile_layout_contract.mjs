// GD-01/GD-02 手机布局契约(<=600px 单列模式):
// 预约弹窗全屏单滚动、主框架单列+顶部横向导航、患者 rail 摘要卡可展开、
// 展开状态不跨患者继承、弹窗重开滚动回顶。平板(820)/桌面既有规则不动。
import { test } from "node:test";
import assert from "node:assert";
import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import { dirname, join } from "node:path";

const here = dirname(fileURLToPath(import.meta.url));
const source = name => readFileSync(
  join(here, "..", "..", "local_app", "static", name),
  "utf8",
);
const styles = source("styles.css");

function mobileBlock() {
  // 取 <=600px 的手机断点块(允许多个,拼起来断言)
  const blocks = [];
  const re = /@media \(max-width: 600px\) \{/g;
  let m;
  while ((m = re.exec(styles)) !== null) {
    let depth = 1;
    let i = m.index + m[0].length;
    while (depth > 0 && i < styles.length) {
      if (styles[i] === "{") depth += 1;
      if (styles[i] === "}") depth -= 1;
      i += 1;
    }
    blocks.push(styles.slice(m.index, i));
  }
  return blocks.join("\n");
}

test("GD-02:<=600px 主框架单列,侧导航改顶部横向滚动", () => {
  const mb = mobileBlock();
  assert.ok(mb, "缺 @media (max-width: 600px) 手机断点");
  assert.ok(/\.app-shell[^{]*\{[^}]*grid-template-columns:\s*minmax\(0, 1fr\)/.test(mb),
    "app-shell 必须单列且列宽可收缩(min-width 0),否则宽内容会把整页撑开");
  assert.ok(mb.includes("theme-pearl .app-shell"), "pearl 主题 72px 侧栏也要覆盖为单列");
  assert.ok(/\.side-nav nav[^{]*\{[^}]*overflow-x:\s*auto/.test(mb), "导航横向滚动");
});

test("GD-01:<=600px 预约弹窗全屏,单一滚动容器", () => {
  const mb = mobileBlock();
  assert.ok(mb.includes("100dvh"), "全屏高度用 dvh");
  assert.ok(mb.includes("100vh"), "不支持 dvh 的环境保留 vh 回退");
  assert.ok(/\.appt-modal-wide[^{]*\{[^}]*width:\s*100vw/.test(mb), "弹窗宽度铺满");
  assert.ok(/\.appt-dialog[^{]*\{[^}]*flex-direction:\s*column/.test(mb), "三栏改纵向");
  assert.ok(/\.appt-dialog[^{]*\{[^}]*overflow-y:\s*auto/.test(mb), "appt-dialog 是唯一滚动容器");
  assert.ok(/\.appt-col[^{]*\{[^}]*overflow:\s*visible/.test(mb), "各栏不得再独立滚动");
  assert.ok(mb.includes("env(safe-area-inset-bottom)"), "底部操作区适配安全区");
});

test("GD-01:弹窗打开/重开滚动位置回顶", () => {
  const editor = source("appointment_editor.js");
  assert.ok(/scrollTop = 0/.test(editor), "打开预约弹窗必须重置滚动位置");
});

test("GD-02:<=600px 患者 rail 摘要卡,详细字段默认收起可展开", () => {
  const mb = mobileBlock();
  assert.ok(/\.workspace-rail:not\(\.rail-expanded\) \.workspace-rail-fields[^{]*\{[^}]*display:\s*none/.test(mb),
    "手机端详细字段默认收起");
  assert.ok(/\.rail-fields-toggle/.test(mb), "手机端显示展开开关");
  assert.ok(styles.includes(".rail-fields-toggle { display: none; }"), "桌面端不出现开关");
});

test("GD-02:rail 展开状态只属于当前患者,切换患者不继承", () => {
  const pw = source("patient_workspace.js");
  assert.ok(pw.includes("railExpandedFor"), "展开状态必须按患者记账");
  assert.ok(/railExpandedFor !== pid/.test(pw), "换患者必须重置收起");
});

test("GD-02:主内容与搜索框防整页横向溢出", () => {
  const mb = mobileBlock();
  assert.ok(/\.main-shell[^{]*\{[^}]*min-width:\s*0/.test(mb), "main-shell 需 min-width:0");
  assert.ok(/#q[^{]*\{[^}]*max-width:\s*100%/.test(mb) || /\.topbar[^{]*\{[^}]*min-width:\s*0/.test(mb),
    "顶栏/搜索框不得撑宽页面");
});

test("平板 820px 与桌面既有规则保留,不因手机修复退化", () => {
  assert.ok(styles.includes("@media (max-width: 820px)"), "820 平板断点保留");
  assert.ok(styles.includes(".appt-dialog { display: grid; grid-template-columns: 286px 1fr 1fr;"),
    "桌面三栏布局保留");
  assert.ok(styles.includes("@media (max-width: 840px)"), "840 断点保留");
});
