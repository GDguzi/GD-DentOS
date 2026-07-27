import test from "node:test";
import assert from "node:assert/strict";
import {
  FakeElement,
  deferred,
  makeApprovedV2Harness,
  settle,
  writeResponse,
} from "./customer_hub_approved_v2_harness.mjs";

function openerFor(harness, id = "open-dialog") {
  harness.modulePanel.innerHTML = `<button type="button" id="${id}">打开</button>`;
  const opener = harness.modulePanel.querySelector(`#${id}`);
  opener.focus();
  return opener;
}

function dirtyOverlay(harness, id = "dirty-dialog") {
  const overlay = harness.sandbox.__overlay(id, `
    <div><h3>编辑</h3><button type="button" data-chv-close>关闭</button></div>
    <input id="dirty-input">`);
  harness.sandbox.__guardDirty(overlay);
  return overlay;
}

test("dialog 生成唯一标题关联、modal 语义，且初始焦点只落首个 enabled focusable", () => {
  const harness = makeApprovedV2Harness();
  openerFor(harness);
  const first = harness.sandbox.__overlay("dialog-a", `
    <h3>标题 A</h3>
    <button type="button" disabled>禁用</button>
    <button type="button" id="first-enabled">可用</button>`);
  const firstPop = first.querySelector(".chv-pop");
  const firstTitle = first.querySelector("h3");

  assert.equal(firstPop.getAttribute("role"), "dialog");
  assert.equal(firstPop.getAttribute("aria-modal"), "true");
  assert.equal(firstPop.getAttribute("tabindex"), "-1");
  assert.equal(firstPop.getAttribute("aria-labelledby"), firstTitle.id);
  assert.ok(firstTitle.id);
  assert.strictEqual(harness.document.activeElement, first.querySelector("#first-enabled"));

  const second = harness.sandbox.__overlay("dialog-b", "<h3>标题 B</h3><button type=button>确定</button>");
  assert.notEqual(second.querySelector("h3").id, firstTitle.id);
});

test("真实回访主弹窗用显式 span 标题生成唯一 aria-labelledby", async () => {
  const harness = makeApprovedV2Harness();
  openerFor(harness);
  const first = await harness.openRv();
  const firstPop = first.querySelector(".chv-pop");
  const firstTitle = first.querySelector("[data-chv-dialog-title]");

  assert.ok(firstTitle, "主弹窗的 span.chv-mtitle 应显式声明为 dialog 标题");
  assert.ok(firstTitle.classList.contains("chv-mtitle"));
  assert.ok(firstTitle.id);
  assert.equal(firstPop.getAttribute("aria-labelledby"), firstTitle.id);

  harness.sandbox.__dispose(first);
  const second = await harness.openRv();
  assert.notEqual(second.querySelector("[data-chv-dialog-title]").id, firstTitle.id);
});

test("0 个焦点项时聚焦 dialog，Tab/Shift+Tab 都留在 dialog", async () => {
  const harness = makeApprovedV2Harness();
  openerFor(harness);
  const overlay = harness.sandbox.__overlay("dialog-zero", "<h3>无操作</h3><div>正文</div>");
  const pop = overlay.querySelector(".chv-pop");
  assert.strictEqual(harness.document.activeElement, pop);

  for (const shiftKey of [false, true]) {
    const event = await pop.dispatch("keydown", {key: "Tab", shiftKey});
    assert.equal(event.defaultPrevented, true);
    assert.strictEqual(harness.document.activeElement, pop);
  }
});

test("1 个焦点项时正反 Tab 均锁定，disabled tabindex=0 与 hidden/aria-hidden 被排除", async () => {
  const harness = makeApprovedV2Harness();
  openerFor(harness);
  const overlay = harness.sandbox.__overlay("dialog-one", `
    <h3>单项</h3>
    <div tabindex="0" disabled id="disabled-tab"></div>
    <button type="button" hidden>隐藏</button>
    <a href="#" aria-hidden="true">隐藏链接</a>
    <input type="hidden">
    <button type="button" id="only">唯一可用</button>`);
  const only = overlay.querySelector("#only");
  assert.strictEqual(harness.document.activeElement, only);

  for (const shiftKey of [false, true]) {
    const event = await only.dispatch("keydown", {key: "Tab", shiftKey});
    assert.equal(event.defaultPrevented, true);
    assert.strictEqual(harness.document.activeElement, only);
  }
  assert.equal(overlay.querySelector("#disabled-tab").focusCount, 0);
});

test("3 个焦点项只在首尾循环，中间 Tab 交给浏览器自然移动", async () => {
  const harness = makeApprovedV2Harness();
  openerFor(harness);
  const overlay = harness.sandbox.__overlay("dialog-three", `
    <h3>三项</h3><button id="one">一</button><input id="two"><a id="three" href="#">三</a>`);
  const one = overlay.querySelector("#one");
  const two = overlay.querySelector("#two");
  const three = overlay.querySelector("#three");

  three.focus();
  let event = await three.dispatch("keydown", {key: "Tab", shiftKey: false});
  assert.equal(event.defaultPrevented, true);
  assert.strictEqual(harness.document.activeElement, one);

  event = await one.dispatch("keydown", {key: "Tab", shiftKey: true});
  assert.equal(event.defaultPrevented, true);
  assert.strictEqual(harness.document.activeElement, three);

  two.focus();
  event = await two.dispatch("keydown", {key: "Tab", shiftKey: false});
  assert.equal(event.defaultPrevented, false);
  assert.strictEqual(harness.document.activeElement, two, "Fake DOM 不伪造浏览器默认焦点移动");
});

test("焦点列表排除任一隐藏祖先、computed 隐藏和 disabled，并只在可见首尾循环", async () => {
  const harness = makeApprovedV2Harness();
  openerFor(harness);
  const overlay = harness.sandbox.__overlay("dialog-hidden-ancestors", `
    <h3>隐藏祖先</h3>
    <div style="display:none"><button id="inline-display">inline display</button></div>
    <div hidden><button id="hidden-ancestor">hidden</button></div>
    <div aria-hidden="true"><button id="aria-ancestor">aria</button></div>
    <div style="visibility:hidden"><button id="inline-visibility">visibility</button></div>
    <button id="disabled-control" disabled>disabled</button>
    <button id="visible-first">first</button>
    <input id="visible-middle">
    <button id="visible-last">last</button>
    <div id="computed-parent"><button id="computed-child">computed</button></div>`);
  overlay.querySelector("#computed-parent").computedDisplay = "none";
  const first = overlay.querySelector("#visible-first");
  const last = overlay.querySelector("#visible-last");

  assert.strictEqual(harness.document.activeElement, first);
  last.focus();
  let event = await last.dispatch("keydown", {key: "Tab"});
  assert.equal(event.defaultPrevented, true);
  assert.strictEqual(harness.document.activeElement, first);

  event = await first.dispatch("keydown", {key: "Tab", shiftKey: true});
  assert.equal(event.defaultPrevented, true);
  assert.strictEqual(harness.document.activeElement, last);
  for (const id of ["inline-display", "hidden-ancestor", "aria-ancestor", "inline-visibility", "disabled-control", "computed-child"]) {
    assert.equal(overlay.querySelector(`#${id}`).focusCount, 0, `${id} 不得获得焦点`);
  }
});

test("焦点列表遵守原生 disabled fieldset 与全部负 tabindex 语义", () => {
  const harness = makeApprovedV2Harness();
  openerFor(harness);
  const overlay = harness.sandbox.__overlay("dialog-native-disabled", `
    <h3>原生禁用语义</h3>
    <fieldset disabled>
      <button id="fieldset-button">fieldset button</button>
      <input id="fieldset-input">
    </fieldset>
    <button id="negative-native" tabindex="-2">negative native</button>
    <div id="negative-custom" tabindex="-3">negative custom</div>
    <button id="valid-focus">valid</button>`);

  assert.deepEqual(
    Array.from(harness.sandbox.__focusables(overlay), element => element.id),
    ["valid-focus"],
  );
  assert.strictEqual(harness.document.activeElement, overlay.querySelector("#valid-focus"));
});

test("真实电话回访弹窗不把 display:none 的 chvWx 后代纳入 Tab 循环", async () => {
  const harness = makeApprovedV2Harness();
  openerFor(harness);
  const overlay = await harness.openRv();
  const selector = "button, a[href], input, select, textarea, [tabindex]";
  const before = overlay.querySelectorAll(selector).filter(node => (
    !node.disabled && !node.hidden && node.getAttribute("aria-hidden") !== "true"
    && !(node.tagName === "INPUT" && node.type === "hidden")
    && node.getAttribute("tabindex") !== "-1"
  ));
  const first = before[0];
  const last = before.at(-1);
  const wx = overlay.querySelector("#chvWx");
  const hiddenAction = new FakeElement("button", harness.document);
  hiddenAction.id = "wx-hidden-action";
  wx.appendChild(hiddenAction);

  assert.equal(wx.style.display, "none");
  last.focus();
  const event = await last.dispatch("keydown", {key: "Tab"});
  assert.equal(event.defaultPrevented, true);
  assert.strictEqual(harness.document.activeElement, first);
  assert.equal(hiddenAction.focusCount, 0);
});

test("Escape、背景和 X 共用关闭守卫；Escape 必须阻止默认并停止传播", async () => {
  const harness = makeApprovedV2Harness({confirmImpl: () => false});
  openerFor(harness);
  const overlay = dirtyOverlay(harness);
  await overlay.querySelector("#dirty-input").dispatch("input");

  const escape = await harness.document.activeElement.dispatch("keydown", {key: "Escape"});
  assert.equal(escape.defaultPrevented, true);
  assert.equal(escape.propagationStopped, true);
  assert.equal(overlay.removed, false);
  await overlay.dispatch("click", {target: overlay});
  await overlay.querySelector("[data-chv-close]").dispatch("click");
  assert.equal(harness.confirms.length, 3);
  assert.equal(overlay.removed, false);
  assert.equal(typeof overlay._chvTryClose, "function");
});

test("pending 关闭直接拒绝且不 confirm；dirty 确认允许后只销毁并恢复 opener 焦点", async () => {
  let allow = false;
  const harness = makeApprovedV2Harness({confirmImpl: () => allow});
  const opener = openerFor(harness);
  const overlay = dirtyOverlay(harness);
  await overlay.querySelector("#dirty-input").dispatch("input");
  overlay._chvWriteState.writeInFlight = {};

  await overlay.querySelector("[data-chv-close]").dispatch("click");
  assert.equal(overlay.removed, false);
  assert.equal(harness.confirms.length, 0);

  delete overlay._chvWriteState.writeInFlight;
  allow = true;
  await overlay.querySelector("[data-chv-close]").dispatch("click");
  assert.equal(overlay.removed, true);
  assert.strictEqual(harness.document.activeElement, opener);
  assert.equal(opener.focusCount, 2);
});

test("程序化 dispose 幂等：abort、remove、恢复 opener 都只执行一次", () => {
  const harness = makeApprovedV2Harness();
  const opener = openerFor(harness);
  const overlay = harness.sandbox.__overlay("dispose-once", "<h3>销毁</h3><button>确定</button>");
  let aborts = 0;
  let removes = 0;
  overlay._chvAbort = () => { aborts += 1; };
  const realRemove = overlay.remove.bind(overlay);
  overlay.remove = () => { removes += 1; realRemove(); };

  harness.sandbox.__dispose(overlay);
  harness.sandbox.__dispose(overlay);

  assert.equal(aborts, 1);
  assert.equal(removes, 1);
  assert.equal(opener.focusCount, 2);
  assert.strictEqual(harness.document.activeElement, opener);
});

test("tabindex=-1 opener 不进 Tab 候选，但程序化 dispose 和正常关闭都恢复焦点", async t => {
  for (const path of ["dispose", "close"]) await t.test(path, async () => {
    const harness = makeApprovedV2Harness();
    harness.modulePanel.innerHTML = `<button type="button" id="negative-opener-${path}" tabindex="-1">打开</button>`;
    const opener = harness.modulePanel.querySelector(`#negative-opener-${path}`);
    opener.focus();
    const overlay = harness.sandbox.__overlay(`negative-dialog-${path}`, `
      <h3>负 tabindex 来源</h3><button type="button" data-chv-close>关闭</button>`);

    assert.deepEqual(
      Array.from(harness.sandbox.__focusables(overlay), element => element.id),
      [""],
    );
    if (path === "dispose") harness.sandbox.__dispose(overlay);
    else await overlay.querySelector("[data-chv-close]").dispatch("click");

    assert.equal(overlay.removed, true);
    assert.equal(opener.focusCount, 2);
    assert.strictEqual(harness.document.activeElement, opener);
  });
});

test("顶层 tab：pending 无 confirm 拒绝；dirty 拒绝不切，允许后 dispose 再切", async () => {
  let allow = false;
  const harness = makeApprovedV2Harness({confirmImpl: () => allow});
  harness.modulePanel.innerHTML = `
    <button data-chv-view="today">今日</button><button data-chv-view="list">列表</button>`;
  harness.sandbox.__setView("today");
  harness.sandbox.__bindTabs(harness.modulePanel);
  const list = harness.modulePanel.querySelector('[data-chv-view="list"]');
  const overlay = dirtyOverlay(harness, "tab-dirty");
  await overlay.querySelector("#dirty-input").dispatch("input");

  overlay._chvWriteState.writeInFlight = {};
  await list.dispatch("click");
  assert.equal(harness.sandbox.__getView(), "today");
  assert.equal(harness.confirms.length, 0);

  delete overlay._chvWriteState.writeInFlight;
  await list.dispatch("click");
  assert.equal(harness.sandbox.__getView(), "today");
  assert.equal(overlay.removed, false);

  allow = true;
  await list.dispatch("click");
  assert.equal(harness.sandbox.__getView(), "list");
  assert.equal(overlay.removed, true);
  assert.equal(harness.sandbox.__loadCount, 1);
});

async function openDictionary(options = {}) {
  const harness = makeApprovedV2Harness({
    dictionaries: {return_type: [], visit_content: ["原值", "待删"], visit_result: [], comm_reason: []},
    ...options,
  });
  openerFor(harness, "dict-opener");
  await harness.sandbox.__openDict("visit_content");
  const overlay = harness.overlays.find(item => item.id === "chvDictOvl" && !item.removed);
  return {harness, overlay};
}

test("字典 add/edit/delete 只有有效变更置 dirty，空值/取消/同值不算", async () => {
  const added = await openDictionary();
  assert.equal(added.overlay._chvGuarded, true);
  added.overlay.querySelector("#chvDictNew").value = "新增值";
  await added.overlay.querySelector("[data-chv-add]").dispatch("click");
  assert.equal(added.overlay._chvDirty, true);

  for (const promptValue of [null, "", "原值"]) {
    const unchanged = await openDictionary({promptImpl: () => promptValue});
    await unchanged.overlay.querySelector("[data-de]").dispatch("click");
    assert.equal(unchanged.overlay._chvDirty, false);
  }

  const edited = await openDictionary({promptImpl: () => "改后值"});
  await edited.overlay.querySelector("[data-de]").dispatch("click");
  assert.equal(edited.overlay._chvDirty, true);

  const deleted = await openDictionary();
  await deleted.overlay.querySelector("[data-dx]").dispatch("click");
  assert.equal(deleted.overlay._chvDirty, true);
});

test("字典新条目输入由通用 input 标 dirty；失败/异常保留 dirty 和 overlay", async () => {
  for (const writeImpl of [
    () => writeResponse(500, {detail: "synthetic"}),
    () => new Error("synthetic network"),
  ]) {
    const {harness, overlay} = await openDictionary({writeImpl});
    const input = overlay.querySelector("#chvDictNew");
    input.value = "草稿";
    await input.dispatch("input");
    assert.equal(overlay._chvDirty, true);

    await overlay.querySelector("[data-chv-save]").dispatch("click");
    assert.equal(overlay.removed, false);
    assert.equal(overlay._chvDirty, true);
    assert.equal(overlay._chvClean, undefined);
  }
});

test("字典保存成功先 clean、更新 cache，再统一 dispose 并恢复焦点", async () => {
  const saved = {return_type: [], visit_content: ["原值", "待删", "新增值"], visit_result: [], comm_reason: []};
  const {harness, overlay} = await openDictionary({writeImpl: () => writeResponse(200, saved)});
  const opener = harness.modulePanel.querySelector("#dict-opener");
  overlay.querySelector("#chvDictNew").value = "新增值";
  await overlay.querySelector("[data-chv-add]").dispatch("click");
  await overlay.querySelector("[data-chv-save]").dispatch("click");

  assert.equal(overlay._chvClean, true);
  assert.equal(overlay.removed, true);
  assert.deepEqual(Array.from(harness.sandbox.__getDicts().visit_content), saved.visit_content);
  assert.strictEqual(harness.document.activeElement, opener);
});

test("字典保存成功立即刷新父回访弹窗常用语；失败保留父选项与 dirty", async () => {
  const initial = {return_type: [], visit_content: ["原值"], visit_result: [], comm_reason: []};
  const saved = {return_type: [], visit_content: ["原值", "新增值"], visit_result: [], comm_reason: []};

  for (const [status, responseData, expectedRefresh] of [
    [200, saved, true],
    [500, {detail: "synthetic"}, false],
  ]) {
    const harness = makeApprovedV2Harness({
      dictionaries: initial,
      writeImpl: () => writeResponse(status, responseData),
    });
    openerFor(harness, `parent-opener-${status}`);
    const parent = await harness.openRv();
    const parentSelect = parent.querySelector('[data-chv-dict-insert="visit_content"]');
    assert.ok(parentSelect, "父回访弹窗必须显式标识可刷新的常用语 select");
    const dictButton = parent.querySelector('[data-chv-dict="visit_content"]');
    dictButton.focus();
    await harness.sandbox.__openDict("visit_content");
    const dictionary = harness.overlays.find(item => item.id === "chvDictOvl" && !item.removed);
    dictionary.querySelector("#chvDictNew").value = "新增值";
    await dictionary.querySelector("[data-chv-add]").dispatch("click");
    await dictionary.querySelector("[data-chv-save]").dispatch("click");

    assert.equal(parentSelect.innerHTML.includes("新增值"), expectedRefresh);
    assert.equal(dictionary.removed, expectedRefresh);
    assert.equal(dictionary._chvDirty, true);
    if (expectedRefresh) assert.strictEqual(harness.document.activeElement, dictButton);
  }
});

test("字典并发 GET 只允许最新请求提交缓存，迟到旧调用 no-op", async () => {
  const oldGate = deferred();
  const newGate = deferred();
  let dictionaryReads = 0;
  const harness = makeApprovedV2Harness({
    getImpl: url => {
      if (url !== "/api/settings/customer-hub-dicts") return undefined;
      dictionaryReads += 1;
      return dictionaryReads === 1 ? oldGate.promise : newGate.promise;
    },
  });
  openerFor(harness, "dictionary-race-module");
  const parent = harness.sandbox.__overlay("dictionary-race-parent", `
    <h3>父回访</h3><button id="dictionary-race-opener">打开字典</button>`);
  const opener = parent.querySelector("#dictionary-race-opener");
  opener.focus();

  const oldOpen = harness.sandbox.__openDict("visit_content");
  await settle();
  const newOpen = harness.sandbox.__openDict("visit_content");
  await settle();
  assert.equal(dictionaryReads, 2);

  const latest = {return_type: [], visit_content: ["NEW"], visit_result: [], comm_reason: []};
  newGate.resolve(writeResponse(200, latest));
  await newOpen;
  const latestOverlay = harness.overlays.find(item => item.id === "chvDictOvl" && !item.removed);
  assert.ok(latestOverlay.querySelector("#chvDictList").innerHTML.includes("NEW"));

  oldGate.resolve(writeResponse(200, {
    return_type: [], visit_content: ["OLD"], visit_result: [], comm_reason: [],
  }));
  assert.equal(await oldOpen, false);

  assert.deepEqual(Array.from(harness.sandbox.__getDicts().visit_content), latest.visit_content);
  assert.strictEqual(
    harness.overlays.find(item => item.id === "chvDictOvl" && !item.removed),
    latestOverlay,
  );
  assert.ok(latestOverlay.querySelector("#chvDictList").innerHTML.includes("NEW"));
  assert.equal(latestOverlay.querySelector("#chvDictList").innerHTML.includes("OLD"), false);
  assert.deepEqual(harness.alerts, []);
});

test("真实字典按钮并发时，迟到旧请求 reject 也必须 no-op", async t => {
  for (const timing of ["新请求先成功", "旧请求先拒绝且新请求仍 pending"]) {
    await t.test(timing, async () => {
      const oldGate = deferred();
      const newGate = deferred();
      let dictionaryReads = 0;
      const harness = makeApprovedV2Harness({
        dictionaries: {return_type: [], visit_content: ["初始值"], visit_result: [], comm_reason: []},
        getImpl: url => {
          if (url !== "/api/settings/customer-hub-dicts") return undefined;
          dictionaryReads += 1;
          if (dictionaryReads === 1) return undefined;
          return dictionaryReads === 2 ? oldGate.promise : newGate.promise;
        },
      });
      openerFor(harness, `dictionary-reject-${timing}`);
      const parent = await harness.openRv();
      harness.sandbox.__setDicts(null);
      const button = parent.querySelector('[data-chv-dict="visit_content"]');
      button.focus();

      const oldClick = button.dispatch("click");
      await settle();
      const newClick = button.dispatch("click");
      await settle();
      assert.equal(dictionaryReads, 3);

      const latest = {return_type: [], visit_content: ["NEW"], visit_result: [], comm_reason: []};
      if (timing === "新请求先成功") {
        newGate.resolve(writeResponse(200, latest));
        await newClick;
        oldGate.reject(new Error("synthetic stale dictionary rejection"));
        await oldClick;
      } else {
        oldGate.reject(new Error("synthetic stale dictionary rejection"));
        await oldClick;
        assert.deepEqual(harness.alerts, []);
        newGate.resolve(writeResponse(200, latest));
        await newClick;
      }

      assert.deepEqual(Array.from(harness.sandbox.__getDicts().visit_content), latest.visit_content);
      assert.equal(harness.overlays.filter(item => item.id === "chvDictOvl" && !item.removed).length, 1);
      assert.deepEqual(harness.alerts, []);
    });
  }
});

test("真实字典按钮读取失败被点击链接管：1 条提示、0 overlay、无未处理拒绝", async () => {
  let dictionaryReads = 0;
  const harness = makeApprovedV2Harness({
    dictionaries: {return_type: [], visit_content: ["原值"], visit_result: [], comm_reason: []},
    getImpl: url => {
      if (url !== "/api/settings/customer-hub-dicts") return undefined;
      dictionaryReads += 1;
      return dictionaryReads === 1
        ? undefined : new Error("synthetic dictionary click rejection");
    },
  });
  openerFor(harness, "dictionary-click-module");
  const parent = await harness.openRv();
  harness.sandbox.__setDicts(null);
  const button = parent.querySelector('[data-chv-dict="visit_content"]');
  button.focus();

  await button.dispatch("click");
  await settle();

  assert.equal(dictionaryReads, 2);
  assert.equal(harness.overlays.some(item => item.id === "chvDictOvl" && !item.removed), false);
  assert.deepEqual(harness.alerts, ["常用语字典载入失败(可能无权限或网络异常),请重试"]);
});

const validMonthlySummary = {
  total: 1,
  completed: 1,
  pending: 0,
  overdue: 0,
  completion_rate: 100,
  channel_counts: [
    {channel: "phone", count: 1},
    {channel: "wechat", count: 0},
    {channel: "other", count: 0},
    {channel: "unknown", count: 0},
  ],
  problem_counts: {needs_attention: 0, overdue: 0, low_satisfaction: 0},
  top_results: [],
  narrative: {status: "disabled", text: ""},
};

test("离开客户通会让所有迟到 GET 入口 no-op，不重挂弹窗或启动录音", async t => {
  const cases = [
    {
      name: "回访详情",
      match: url => url.startsWith("/api/return-visits/"),
      payload: {
        revision: 1,
        patient_identity: "patient/synthetic",
        display_name: "Synthetic",
        due_time: "2026-07-21 10:00",
        item_name: "Synthetic follow-up",
        channel: "phone",
        visitor: "Synthetic operator",
        status: "待回访",
        note: "Synthetic note",
        return_result: "",
        images: [],
      },
      invoke: harness => harness.sandbox.__openRv("rv/synthetic", "patient/synthetic"),
    },
    {
      name: "字典",
      match: url => url === "/api/settings/customer-hub-dicts",
      payload: {return_type: [], visit_content: [], visit_result: [], comm_reason: []},
      invoke: harness => harness.sandbox.__openDict("visit_content"),
    },
    {
      name: "月历",
      match: url => url.startsWith("/api/return-visits/calendar?month="),
      payload: {days: []},
      invoke: harness => harness.sandbox.__openCalendar(),
    },
    {
      name: "月度回顾",
      match: url => url.startsWith("/api/return-visits/calendar?month="),
      payload: {summary: validMonthlySummary},
      invoke: harness => harness.sandbox.__openMonthly("2026-07"),
    },
    {
      name: "新增沟通",
      match: url => url === "/api/settings/customer-hub-dicts",
      payload: {return_type: [], visit_content: [], visit_result: [], comm_reason: []},
      permissions: ["patient.profile.view", "communication.manage"],
      invoke: harness => harness.sandbox.__openAddComm("patient/synthetic"),
    },
    {
      name: "新增回访",
      match: url => url === "/api/settings/customer-hub-dicts",
      payload: {return_type: [], visit_content: [], visit_result: [], comm_reason: []},
      invoke: harness => harness.sandbox.__openNewRv("patient/synthetic"),
    },
    {
      name: "行内播放录音",
      match: url => url.endsWith("/calls"),
      payload: {calls: [{call_id: "call/synthetic", linked_return_visit_id: "rv/synthetic"}]},
      permissions: ["patient.profile.view", "call_record.view"],
      invoke: harness => harness.sandbox.__playRecording("rv/synthetic", "patient/synthetic"),
    },
  ];

  for (const item of cases) await t.test(item.name, async () => {
    const gate = deferred();
    const harness = makeApprovedV2Harness({
      permissions: item.permissions,
      getImpl: url => item.match(url) ? gate.promise : undefined,
    });
    openerFor(harness, `late-${item.name}`);
    const pending = item.invoke(harness);
    await settle();
    assert.ok(harness.gets.some(item.match), "测试必须先卡住所测 GET");

    assert.equal(await harness.sandbox.__leaveHub(), true);
    gate.resolve(writeResponse(200, item.payload));
    await pending;
    await settle();

    assert.equal(harness.document.querySelectorAll(".chv-ovl").length, 0);
    assert.equal(harness.audios.length, 0);
    assert.deepEqual(harness.alerts, []);
  });
});

test("字典 GET 返回前父 overlay 或 opener 断开时 no-op", async t => {
  for (const disconnect of ["parent", "opener"]) await t.test(disconnect, async () => {
    const gate = deferred();
    const dictionaries = {return_type: [], visit_content: [], visit_result: [], comm_reason: []};
    const harness = makeApprovedV2Harness({
      getImpl: url => url === "/api/settings/customer-hub-dicts" ? gate.promise : undefined,
    });
    const moduleOpener = openerFor(harness, `module-${disconnect}`);
    const parent = harness.sandbox.__overlay(`parent-${disconnect}`, `
      <h3>父回访</h3><button id="dict-${disconnect}">打开字典</button>`);
    const dictOpener = parent.querySelector(`#dict-${disconnect}`);
    dictOpener.focus();
    const pending = harness.sandbox.__openDict("visit_content");
    await settle();

    if (disconnect === "parent") harness.sandbox.__dispose(parent);
    else dictOpener.remove();
    gate.resolve(writeResponse(200, dictionaries));
    await pending;
    await settle();

    assert.equal(harness.overlays.some(item => item.id === "chvDictOvl" && !item.removed), false);
    assert.strictEqual(harness.document.activeElement, disconnect === "parent" ? moduleOpener : dictOpener);
  });
});

test("字典 GET 拒绝前父 overlay 或 opener 断开时也 no-op", async t => {
  for (const disconnect of ["parent", "opener"]) await t.test(disconnect, async () => {
    const gate = deferred();
    const harness = makeApprovedV2Harness({
      getImpl: url => url === "/api/settings/customer-hub-dicts" ? gate.promise : undefined,
    });
    openerFor(harness, `reject-module-${disconnect}`);
    const parent = harness.sandbox.__overlay(`reject-parent-${disconnect}`, `
      <h3>父回访</h3><button id="reject-dict-${disconnect}">打开字典</button>`);
    const dictOpener = parent.querySelector(`#reject-dict-${disconnect}`);
    dictOpener.focus();
    const pending = harness.sandbox.__openDict("visit_content");
    await settle();

    if (disconnect === "parent") harness.sandbox.__dispose(parent);
    else dictOpener.remove();
    gate.reject(new Error("synthetic dictionary read rejection"));

    assert.equal(await pending, false);
    assert.equal(harness.overlays.some(item => item.id === "chvDictOvl" && !item.removed), false);
    assert.deepEqual(harness.alerts, []);
  });
});

test("字典 GET 拒绝时若模块、父 overlay 和 opener 仍当前连接，保留原错误", async () => {
  const gate = deferred();
  const harness = makeApprovedV2Harness({
    getImpl: url => url === "/api/settings/customer-hub-dicts" ? gate.promise : undefined,
  });
  openerFor(harness, "reject-current-module");
  const parent = harness.sandbox.__overlay("reject-current-parent", `
    <h3>父回访</h3><button id="reject-current-opener">打开字典</button>`);
  parent.querySelector("#reject-current-opener").focus();
  const pending = harness.sandbox.__openDict("visit_content");
  await settle();
  gate.reject(new Error("synthetic current dictionary read rejection"));

  await assert.rejects(pending, /synthetic current dictionary read rejection/);
  assert.equal(harness.overlays.some(item => item.id === "chvDictOvl" && !item.removed), false);
  assert.deepEqual(harness.alerts, []);
});

test("字典 dirty 的 X、顶层切换、全局离开使用同一确认规则", async () => {
  let allow = false;
  const opened = await openDictionary({confirmImpl: () => allow});
  const {harness, overlay} = opened;
  overlay.querySelector("#chvDictNew").value = "草稿";
  await overlay.querySelector("#chvDictNew").dispatch("input");

  await overlay.querySelector("[data-chv-close]").dispatch("click");
  assert.equal(overlay.removed, false);
  await overlay.dispatch("click", {target: overlay});
  assert.equal(overlay.removed, false);
  const escape = await harness.document.activeElement.dispatch("keydown", {key: "Escape"});
  assert.equal(escape.defaultPrevented, true);
  assert.equal(overlay.removed, false);

  harness.modulePanel.innerHTML = `
    <button data-chv-view="today">今日</button><button data-chv-view="list">列表</button>`;
  harness.sandbox.__setView("today");
  harness.sandbox.__bindTabs(harness.modulePanel);
  await harness.modulePanel.querySelector('[data-chv-view="list"]').dispatch("click");
  assert.equal(harness.sandbox.__getView(), "today");
  assert.equal(overlay.removed, false);

  assert.equal(await harness.sandbox.__leaveHub(), false);
  assert.equal(overlay.removed, false);
  assert.equal(harness.confirms.length, 5);

  allow = true;
  assert.equal(await harness.sandbox.__leaveHub(), true);
  assert.equal(overlay.removed, true);
});
