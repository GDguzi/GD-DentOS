import test from "node:test";
import assert from "node:assert/strict";
import vm from "node:vm";
import {
  FakeElement,
  deferred,
  makeApprovedV2Harness,
  multipartFile,
  setValidEditDraft,
  settle,
  writeResponse,
} from "./customer_hub_approved_v2_harness.mjs";

const actions = [
  {
    name: "edit",
    method: "PUT",
    prepare(overlay) { setValidEditDraft(overlay); },
    button(overlay) { return overlay.querySelector("[data-chv-ok]"); },
  },
  {
    name: "delete",
    method: "DELETE",
    prepare() {},
    button(overlay) { return overlay.querySelector("[data-chv-del]"); },
  },
  {
    name: "screenshot",
    method: "POST",
    prepare(overlay, file) {
      setValidEditDraft(overlay);
      overlay.querySelector("#chvMWay").value = "wechat";
      const input = overlay.querySelector("#chvScFile");
      input.files = [file];
      return input.dispatch("change");
    },
    button(overlay) { return overlay.querySelector("[data-chv-ok]"); },
  },
];

function makeStream() {
  const tracks = [{stopCount: 0, stop() { this.stopCount += 1; }}];
  return {tracks, stream: {getTracks: () => tracks}};
}

function unchangedReturnVisitReceipt(overrides = {}) {
  return {return_visit: {
    return_visit_id: "rv/synthetic",
    patient_identity: "patient/synthetic",
    due_time: "2026-07-21 10:00",
    item_name: "Synthetic follow-up",
    status: "待回访",
    note: "Synthetic note",
    visitor: "Synthetic operator",
    return_doctor: "",
    return_type: "",
    channel: "phone",
    return_result: "",
    actual_date: "",
    intent_level: "",
    satisfaction: "",
    revision: 1,
    is_deleted: 0,
    source_json: "",
    created_at: "2026-07-21 09:00:00",
    updated_at: "2026-07-21 09:00:00",
    ...overrides,
  }};
}

function editedReturnVisitDetail(overrides = {}) {
  return unchangedReturnVisitReceipt({
    due_time: "2026-07-21 11:00",
    item_name: "Edited synthetic item",
    note: "Edited synthetic note",
    return_result: "Synthetic result",
    ...overrides,
  }).return_visit;
}

for (const action of actions) {
  test(`${action.name}: WRITE409,GET 后第二次显式点击才用新 revision 写`, async () => {
    const harness = makeApprovedV2Harness({
      writeImpl: (_request, attempt) => attempt === 1
        ? writeResponse(409, {detail: "synthetic conflict"})
        : writeResponse(200, action.name === "screenshot"
          ? {revision: 3}
          : {return_visit: {revision: 3}}),
      detailReadImpl: async () => ({revision: 2}),
    });
    const overlay = await harness.openRv();
    const file = {name: "same-file.png", marker: Symbol("same-file")};
    await action.prepare(overlay, file);
    const button = action.button(overlay);

    await button.dispatch("click");

    assert.equal(harness.writes.length, 1);
    assert.equal(harness.writes[0].method, action.method);
    assert.deepEqual(harness.detailReads, ["/api/return-visits/rv%2Fsynthetic"]);
    assert.equal(overlay.removed, false, "409 必须保留 overlay 和草稿");
    assert.equal(overlay.querySelector("#chvMC").value, action.name === "edit" || action.name === "screenshot"
      ? "Edited synthetic note" : "");
    if (action.name === "screenshot") assert.strictEqual(multipartFile(harness.writes[0]), file);

    await button.dispatch("click");

    assert.equal(harness.writes[1].method, action.method);
    if (action.name === "screenshot") assert.strictEqual(multipartFile(harness.writes[1]), file);
    const body = harness.writes[1].body;
    const revision = body instanceof Object && typeof body.get === "function"
      ? Number(body.get("expected_revision")) : body.expected_revision;
    assert.equal(revision, 2);
  });

  test(`${action.name}: WRITE409,GETfail; 第二点 GETok; 第三点才 WRITEnext`, async () => {
    const harness = makeApprovedV2Harness({
      writeImpl: (_request, attempt) => attempt === 1
        ? writeResponse(409)
        : writeResponse(200, action.name === "screenshot"
          ? {revision: 4}
          : {return_visit: {revision: 4}}),
      detailReadImpl: async (_url, attempt) => {
        if (attempt === 1) throw Object.assign(new Error("synthetic GET failure"), {status: 500});
        return {revision: 3};
      },
    });
    const overlay = await harness.openRv();
    const file = {name: "retry-same-file.png"};
    await action.prepare(overlay, file);
    const button = action.button(overlay);

    await button.dispatch("click");
    assert.equal(harness.writes.length, 1);
    assert.equal(harness.detailReads.length, 1);

    await button.dispatch("click");
    assert.equal(harness.writes.length, 1, "刷新成功的这次点击也不得写");
    assert.equal(harness.detailReads.length, 2);

    await button.dispatch("click");
    assert.equal(harness.writes.length >= 2, true);
    assert.equal(harness.writes[1].method, action.method);
    if (action.name === "screenshot") {
      assert.strictEqual(multipartFile(harness.writes[0]), file);
      assert.strictEqual(multipartFile(harness.writes[1]), file);
    }
  });
}

for (const [label, responseData] of [
  ["缺失", {}],
  ["同值", {revision: 1}],
  ["倒退", {revision: 0}],
  ["非安全整数", {revision: Number.MAX_SAFE_INTEGER + 1}],
]) {
  test(`screenshot 200 revision ${label}: 只读确认且不重传同一 File`, async () => {
    const harness = makeApprovedV2Harness({
      writeImpl: request => request.url.endsWith("/images")
        ? writeResponse(200, responseData)
        : writeResponse(200, {return_visit: {revision: 3}}),
      detailReadImpl: async () => ({revision: 2}),
    });
    const overlay = await harness.openRv();
    const file = {name: `ambiguous-${label}.png`};
    await actions[2].prepare(overlay, file);

    await actions[2].button(overlay).dispatch("click");

    const uploads = harness.writes.filter(request => request.url.endsWith("/images"));
    assert.equal(uploads.length, 1);
    assert.equal(harness.writes.some(request => request.method === "PUT"), false,
      "结果待确认的这次点击不得继续写 edit");
    assert.strictEqual(multipartFile(uploads[0]), file);
    assert.deepEqual(harness.detailReads, ["/api/return-visits/rv%2Fsynthetic"]);
    assert.equal(overlay.removed, false);
  });
}

test('screenshot 200 revision 数字字符串仍是待确认，只 POST+GET 不 PUT', async () => {
  const harness = makeApprovedV2Harness({
    writeImpl: request => request.url.endsWith("/images")
      ? writeResponse(200, {revision: "2"})
      : writeResponse(200, {return_visit: {revision: 4}}),
    detailReadImpl: async () => ({revision: 3}),
  });
  const overlay = await harness.openRv();
  const file = {name: "string-revision.png"};
  await actions[2].prepare(overlay, file);

  await actions[2].button(overlay).dispatch("click");

  assert.deepEqual(harness.writes.map(request => request.method), ["POST"]);
  assert.deepEqual(harness.detailReads, ["/api/return-visits/rv%2Fsynthetic"]);
  assert.equal(overlay.removed, false);
});

test("screenshot status 0 后隔离当前 File，重复点击也不得再写", async () => {
  const harness = makeApprovedV2Harness({
    writeImpl: request => request.url.endsWith("/images")
      ? writeResponse(0, {detail: "synthetic network uncertainty"})
      : writeResponse(200, {return_visit: {revision: 3}}),
  });
  const overlay = await harness.openRv();
  const file = {name: "quarantined-network-file.png"};
  await actions[2].prepare(overlay, file);
  const save = actions[2].button(overlay);

  await save.dispatch("click");
  assert.deepEqual(harness.writes.map(request => request.method), ["POST"]);
  assert.strictEqual(multipartFile(harness.writes[0]), file);
  assert.equal(overlay.removed, false);
  assert.match(harness.alerts.at(-1), /关闭.*重新打开.*确认/);

  await save.dispatch("click");

  assert.deepEqual(harness.writes.map(request => request.method), ["POST"],
    "网络结果不确定后不得重传同一 File");
  assert.strictEqual(multipartFile(harness.writes[0]), file);
});

test('edit 200 revision 数字字符串仍是待确认，只 PUT+GET 且不关窗', async () => {
  const harness = makeApprovedV2Harness({
    writeImpl: () => writeResponse(200, {return_visit: {revision: "2"}}),
    detailReadImpl: async () => ({revision: 3}),
  });
  const overlay = await harness.openRv();
  setValidEditDraft(overlay);

  await overlay.querySelector("[data-chv-ok]").dispatch("click");

  assert.deepEqual(harness.writes.map(request => request.method), ["PUT"]);
  assert.deepEqual(harness.detailReads, ["/api/return-visits/rv%2Fsynthetic"]);
  assert.equal(overlay.removed, false);
});

test("edit 200 同 revision 是合法 no-op，不 GET 确认并正常关窗", async () => {
  const harness = makeApprovedV2Harness({
    writeImpl: () => writeResponse(200, unchangedReturnVisitReceipt()),
  });
  const overlay = await harness.openRv();
  // FakeElement 不会把 textarea 的初始文本自动映射为 value；补回 GET 的原值，
  // 仍是未修改草稿，不触发 input/dirty。
  overlay.querySelector("#chvMC").value = "Synthetic note";

  await overlay.querySelector("[data-chv-ok]").dispatch("click");

  assert.deepEqual(harness.writes.map(request => request.method), ["PUT"]);
  assert.deepEqual(harness.detailReads, []);
  assert.equal(overlay.removed, true);
});

for (const [label, responseData] of [
  ["缺字段", {return_visit: {revision: 1}}],
  ["完整但字段不一致", unchangedReturnVisitReceipt()],
]) {
  test(`edit changed payload + 同 revision ${label}: 必须只读确认且不关窗`, async () => {
    const harness = makeApprovedV2Harness({
      writeImpl: (_request, attempt) => attempt === 1
        ? writeResponse(200, responseData)
        : writeResponse(200, {return_visit: {revision: 3}}),
      detailReadImpl: async () => ({revision: 2}),
    });
    const overlay = await harness.openRv();
    setValidEditDraft(overlay);

    await overlay.querySelector("[data-chv-ok]").dispatch("click");

    assert.deepEqual(harness.writes.map(request => request.method), ["PUT"]);
    assert.deepEqual(harness.detailReads, ["/api/return-visits/rv%2Fsynthetic"]);
    assert.equal(overlay.removed, false);
    assert.equal(overlay.querySelector("#chvMC").value, "Edited synthetic note");

    await overlay.querySelector("[data-chv-ok]").dispatch("click");

    assert.deepEqual(harness.writes.map(request => request.method), ["PUT", "PUT"],
      "确认 GET 未匹配草稿时第二次必须显式重写，不能静默关窗");
    assert.equal(harness.writes[1].body.expected_revision, 2);
    assert.equal(overlay.removed, true);
  });
}

for (const initial of [
  {label: "数字字符串", revision: "1"},
  {label: "缺失", revision: 1, omitInitialRevision: true},
  {label: "0", revision: 0},
  {label: "小数", revision: 1.5},
  {label: "unsafe", revision: Number.MAX_SAFE_INTEGER + 1},
]) {
  test(`初始 revision ${initial.label} 时 edit/delete/screenshot 均 fail closed 且 0 写`, async () => {
    for (const action of actions) {
      const harness = makeApprovedV2Harness(initial);
      const overlay = await harness.openRv();
      await action.prepare(overlay, {name: `invalid-${initial.label}.png`});

      await action.button(overlay).dispatch("click");

      assert.equal(harness.writes.length, 0, `${action.name} 不得写`);
      assert.equal(overlay.removed, false);
    }
  });

  test(`初始 revision ${initial.label} 时 mic 上传 fail closed 且 0 写`, async () => {
    const media = makeStream();
    const harness = makeApprovedV2Harness({
      ...initial,
      permissions: [
        "patient.profile.view",
        "return_visit.view",
        "return_visit.manage",
        "call_record.manage",
      ],
      getUserMedia: async () => media.stream,
    });
    const overlay = await harness.openRv();
    const mic = overlay.querySelector("[data-chv-mic]");

    await mic.dispatch("click");
    harness.recorders[0].emit({size: 5});
    await mic.dispatch("click");
    await settle();

    assert.equal(harness.writes.length, 0);
    harness.sandbox.__dispose(overlay);
  });
}

test("edit 结果确认后草稿未变，下次点击从 edit 后续跑计划 POST 且不重复 PUT", async () => {
  const harness = makeApprovedV2Harness({
    writeImpl: request => request.method === "PUT"
      ? writeResponse(200, {return_visit: {revision: "2"}})
      : writeResponse(200, {}),
    detailReadImpl: async () => editedReturnVisitDetail({revision: 2}),
  });
  const overlay = await harness.openRv();
  setValidEditDraft(overlay);
  overlay.querySelector("#chvMNext").value = "2026-07-30";
  const save = overlay.querySelector("[data-chv-ok]");

  await save.dispatch("click");
  assert.deepEqual(harness.writes.map(request => request.method), ["PUT"]);
  assert.equal(overlay.removed, false);

  await save.dispatch("click");

  assert.deepEqual(harness.writes.map(request => request.method), ["PUT", "POST"]);
  assert.match(harness.writes[1].url, /\/api\/patients\/patient%2Fsynthetic\/return-visits$/);
});

test("旧状态代码 3 不受已回访文字前缀影响，改其他字段仍原值保存", async () => {
  const harness = makeApprovedV2Harness({
    initialDetail: {
      status: "3",
      item_name: "已回访：合成旧系统事项",
      note: "已回访：合成旧系统内容",
    },
  });
  const overlay = await harness.openRv();
  const selected = overlay.querySelector("input[name=chvSt]:checked");
  const titleHtml = overlay.innerHTML.match(
    /<span class="chv-mtitle" data-chv-dialog-title>[\s\S]*?<\/span>\s*<button/,
  )?.[0] || "";

  assert.equal(selected.value, "3");
  assert.match(overlay.innerHTML, /旧系统状态（代码 3，未映射）/);
  assert.doesNotMatch(overlay.innerHTML, />3（原值）</);
  assert.match(titleHtml, /旧系统状态（代码 3，未映射）/);
  assert.doesNotMatch(titleHtml, />已回访<\/span>|>3<\/span>/);

  setValidEditDraft(overlay);
  await overlay.querySelector("[data-chv-ok]").dispatch("click");

  assert.equal(harness.writes[0].body.status, "3");
});

test("旧状态代码 3 只有主动选择标准状态后才更新", async () => {
  const harness = makeApprovedV2Harness({initialDetail: {status: "3"}});
  const overlay = await harness.openRv();
  const radios = overlay.querySelectorAll("input[name=chvSt]");
  for (const radio of radios) radio.checked = radio.value === "待跟进";
  setValidEditDraft(overlay);

  await overlay.querySelector("[data-chv-ok]").dispatch("click");

  assert.equal(harness.writes[0].body.status, "待跟进");
});

test("下次回访快捷项读取当前回访时间、写日期并置脏", async () => {
  const harness = makeApprovedV2Harness();
  const overlay = await harness.openRv();
  overlay.querySelector("#chvMT").value = "2026-01-31T10:00";
  assert.equal(Boolean(overlay._chvDirty), false);

  await overlay.querySelector('[data-chv-next="1m"]').dispatch("click");

  assert.equal(overlay.querySelector("#chvMNext").value, "2026-02-28");
  assert.equal(overlay._chvDirty, true);
});

test("回访时间非法时快捷项不改下次回访和原脏状态", async () => {
  const harness = makeApprovedV2Harness();
  const overlay = await harness.openRv();
  overlay.querySelector("#chvMT").value = "";
  overlay.querySelector("#chvMNext").value = "2026-09-01";
  overlay._chvDirty = false;

  await overlay.querySelector('[data-chv-next="15d"]').dispatch("click");

  assert.equal(overlay.querySelector("#chvMNext").value, "2026-09-01");
  assert.equal(overlay._chvDirty, false);
  assert.match(harness.alerts.at(-1), /回访时间无效，请先填写正确的回访时间/);
});

test("edit 结果确认后草稿变更，下次点击用新 revision 重新 PUT 而不直接关窗", async () => {
  const harness = makeApprovedV2Harness({
    writeImpl: (_request, attempt) => attempt === 1
      ? writeResponse(200, {return_visit: {revision: "2"}})
      : writeResponse(200, {return_visit: {revision: 3}}),
    detailReadImpl: async () => editedReturnVisitDetail({revision: 2}),
  });
  const overlay = await harness.openRv();
  setValidEditDraft(overlay);
  const save = overlay.querySelector("[data-chv-ok]");
  await save.dispatch("click");
  assert.equal(overlay.removed, false);

  overlay.querySelector("#chvMC").value = "Changed after confirmation";
  await overlay.querySelector("#chvMC").dispatch("input");
  await save.dispatch("click");

  assert.deepEqual(harness.writes.map(request => request.method), ["PUT", "PUT"]);
  assert.equal(harness.writes[1].body.expected_revision, 2);
  assert.equal(harness.writes[1].body.note, "Changed after confirmation");
});

for (const phase of ["starting", "recording", "upload"]) {
  test(`mic ${phase} 时父回访字典入口 fail closed，不打开子 overlay`, async () => {
    const media = makeStream();
    const permission = deferred();
    const upload = deferred();
    const harness = makeApprovedV2Harness({
      permissions: [
        "patient.profile.view",
        "return_visit.view",
        "return_visit.manage",
        "call_record.manage",
      ],
      getUserMedia: phase === "starting" ? () => permission.promise : async () => media.stream,
      writeImpl: phase === "upload" ? () => upload.promise : undefined,
    });
    const overlay = await harness.openRv();
    const mic = overlay.querySelector("[data-chv-mic]");
    const dict = overlay.querySelector("[data-chv-dict]");
    let starting;
    if (phase === "starting") {
      starting = mic.dispatch("click");
      await settle();
    } else {
      await mic.dispatch("click");
      if (phase === "upload") {
        harness.recorders[0].emit({size: 5});
        await mic.dispatch("click");
        await settle();
      }
    }

    await dict.dispatch("click");
    const childCount = harness.overlays.filter(item => item.id === "chvDictOvl" && !item.removed).length;

    harness.sandbox.__dispose(overlay);
    if (phase === "starting") { permission.resolve(media.stream); await starting; }
    if (phase === "upload") { upload.resolve(writeResponse(500)); await settle(); }
    assert.equal(childCount, 0);
  });
}

test("edit 200 revision 倒退时关闭前只读确认，确认失败保留草稿", async () => {
  const harness = makeApprovedV2Harness({
    revision: 2,
    writeImpl: () => writeResponse(200, {return_visit: {revision: 1}}),
    detailReadImpl: async () => { throw Object.assign(new Error("synthetic"), {status: 500}); },
  });
  const overlay = await harness.openRv();
  setValidEditDraft(overlay);

  await overlay.querySelector("[data-chv-ok]").dispatch("click");

  assert.equal(harness.writes.length, 1);
  assert.equal(harness.detailReads.length, 1);
  assert.equal(overlay.removed, false);
  assert.equal(overlay.querySelector("#chvMC").value, "Edited synthetic note");
});

test("普通写 pending 冻结全部控件、与 mic 互斥、禁止双提交和所有离开，settle 精确恢复", async () => {
  const pending = deferred();
  const harness = makeApprovedV2Harness({writeImpl: () => pending.promise});
  const overlay = await harness.openRv();
  setValidEditDraft(overlay);
  const originallyDisabled = overlay.querySelector("#chvMType");
  originallyDisabled.disabled = true;
  const ok = overlay.querySelector("[data-chv-ok]");
  const other = overlay.querySelector("[data-chv-okplan]");

  const first = ok.dispatch("click");
  await settle();

  assert.equal(harness.writes.length, 1);
  assert.ok(overlay.querySelectorAll("input, select, textarea, button").every(control => control.disabled));
  assert.equal(await harness.sandbox.__leaveHub(), false);
  assert.equal(harness.confirms.length, 0, "pending 离开不得弹 dirty confirm");
  await overlay.dispatch("click", {target: overlay});
  await overlay.querySelector("[data-chv-close]").dispatch("click");
  await overlay.querySelector("[data-chv-home]").dispatch("click");
  const infoTab = overlay.querySelector('[data-chv-ptab="info"]');
  const historyTab = overlay.querySelector('[data-chv-ptab="hist"]');
  await historyTab.dispatch("click");
  assert.equal(infoTab.classList.contains("on"), true);
  assert.equal(historyTab.classList.contains("on"), false);
  assert.deepEqual(harness.openedPatients, []);
  await other.dispatch("click");
  assert.equal(overlay.removed, false);
  assert.equal(harness.writes.length, 1);

  pending.resolve(writeResponse(500));
  await first;
  assert.equal(ok.disabled, false);
  assert.equal(other.disabled, false);
  assert.equal(originallyDisabled.disabled, true);
  assert.ok(ok.focusCount > 0, "失败后焦点回本次触发按钮");
});

test("普通写与已在 pending 的 mic 互斥", async () => {
  const harness = makeApprovedV2Harness();
  const overlay = await harness.openRv();
  setValidEditDraft(overlay);
  overlay._chvMicPending = true;

  await overlay.querySelector("[data-chv-ok]").dispatch("click");

  assert.equal(harness.writes.length, 0);
  assert.equal(overlay.removed, false);
});

test("批删 409 只发一次 POST，刷新列表并要求重新勾选；刷新失败明确提示", async () => {
  const harness = makeApprovedV2Harness({writeImpl: () => writeResponse(409)});
  assert.equal(typeof harness.sandbox.__bindBatchDelete, "function", "应提供页面级批删绑定");
  const body = new FakeElement("section", harness.document);
  body.innerHTML = `
    <button data-chv-batchdel="1">batch</button>
    <input class="chv-ck" type="checkbox" value="rv/1" data-chv-revision="1" checked>
    <button data-chv-search="1">search</button>`;
  harness.sandbox.refreshes = 0;
  vm.runInContext(`loadCustomerHubV2 = async () => { refreshes += 1; return false; };`, harness.sandbox);
  harness.sandbox.__bindBatchDelete(body);

  await body.querySelector("[data-chv-batchdel]").dispatch("click");

  assert.equal(harness.writes.filter(request => request.url.endsWith("/batch-delete")).length, 1);
  assert.equal(harness.sandbox.refreshes, 1);
  assert.match(harness.alerts.join("\n"), /刷新失败|重新勾选/);
});

test("批删 pending 使用页面级共享锁，冻结控件并禁止双提交与顶层离开", async () => {
  const pending = deferred();
  const harness = makeApprovedV2Harness({writeImpl: () => pending.promise});
  const body = new FakeElement("section", harness.document);
  body.innerHTML = `
    <button data-chv-batchdel="1">batch</button>
    <input class="chv-ck" type="checkbox" value="rv/1" data-chv-revision="1" checked>
    <button data-chv-search="1">search</button>`;
  harness.sandbox.batchBody = body;
  vm.runInContext("modulePanel = batchBody; loadCustomerHubV2 = async () => true;", harness.sandbox);
  harness.sandbox.__bindBatchDelete(body);
  const button = body.querySelector("[data-chv-batchdel]");

  const first = button.dispatch("click");
  await settle();

  assert.equal(harness.writes.length, 1);
  assert.ok(body.querySelectorAll("input, select, textarea, button").every(control => control.disabled));
  assert.equal(await harness.sandbox.__leaveHub(), false);
  await button.dispatch("click");
  assert.equal(harness.writes.length, 1);

  pending.resolve(writeResponse(500));
  await first;
  assert.ok(body.querySelectorAll("input, select, textarea, button").every(control => !control.disabled));
});
