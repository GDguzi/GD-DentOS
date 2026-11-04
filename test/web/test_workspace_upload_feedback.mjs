import { test } from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import vm from "node:vm";

const source = readFileSync(new URL("../../local_app/static/workspace_lightbox.js", import.meta.url), "utf8");
const start = source.indexOf("async function uploadWorkspaceLocalFiles(");
const upload = source.slice(start, source.indexOf("\n}", start) + 2);
const photo = name => ({ name, type: "image/png" });

function setup(responses, { switchDuringUpload = false, switchDuringRefresh = false } = {}) {
  let hint = { textContent: "Original hint" };
  let renders = 0;
  const requests = [];
  const panel = { querySelector: () => hint };
  const sandbox = {
    workspacePatientId: "p1",
    document: { querySelector: selector => selector.endsWith(".image-drop-hint") ? hint : panel },
    FormData: class { append() {} },
    fetch: async url => {
      requests.push(url);
      if (switchDuringUpload) {
        sandbox.workspacePatientId = "p2";
        hint = { textContent: "Other patient" };
      }
      const next = responses.shift();
      if (next instanceof Error) throw next;
      return { ok: next.status === 200, status: next.status, json: async () => ({ detail: next.detail }) };
    },
    renderWorkspaceImagesTab: async () => {
      renders++;
      // Model the asynchronous refresh replacing the old hint element.
      await Promise.resolve();
      if (switchDuringRefresh) sandbox.workspacePatientId = "p2";
      hint = { textContent: switchDuringRefresh ? "Other patient" : "Fresh hint" };
    },
  };
  vm.createContext(sandbox);
  vm.runInContext(upload, sandbox);
  return {
    run: files => sandbox.uploadWorkspaceLocalFiles(files),
    text: () => hint.textContent,
    renders: () => renders,
    requests,
  };
}

test("server failure names and details remain visible after a successful image refresh", async () => {
  const harness = setup([{ status: 200 }, { status: 400, detail: "只支持 jpg/png/webp 图片" }]);
  await harness.run([photo("good.png"), photo("<bad>.gif")]);
  assert.equal(harness.renders(), 1);
  assert.match(harness.text(), /成功 1/);
  assert.ok(harness.text().includes("<bad>.gif"));
  assert.ok(harness.text().includes("只支持 jpg/png/webp 图片"));
});

test("403 explains the missing upload permission", async () => {
  const harness = setup([{ status: 403, detail: "forbidden" }]);
  await harness.run([photo("permission.png")]);
  assert.match(harness.text(), /permission\.png/);
  assert.match(harness.text(), /权限/);
});

test("network errors name the file and explain the connection failure", async () => {
  const harness = setup([new Error("Failed to fetch")]);
  await harness.run([photo("network.png")]);
  assert.match(harness.text(), /network\.png/);
  assert.match(harness.text(), /网络/);
});

test("an entirely unsupported selection explains every skipped file without uploading", async () => {
  const harness = setup([]);
  await harness.run([{ name: "notes.txt", type: "text/plain" }, { name: "table.csv", type: "text/csv" }]);
  assert.equal(harness.requests.length, 0);
  assert.match(harness.text(), /notes\.txt/);
  assert.match(harness.text(), /table\.csv/);
  assert.match(harness.text(), /跳过/);
});

test("successful uploads refresh the images and retain their completion status", async () => {
  const harness = setup([{ status: 200 }]);
  await harness.run([photo("good.png")]);
  assert.equal(harness.renders(), 1);
  assert.match(harness.text(), /成功 1/);
});

test("switching patient during upload cannot overwrite the new patient's hint", async () => {
  const harness = setup([{ status: 400, detail: "Synthetic failure" }, { status: 200 }], { switchDuringUpload: true });
  await harness.run([photo("old-1.png"), photo("old-2.png")]);
  assert.equal(harness.renders(), 0);
  assert.equal(harness.text(), "Other patient");
  assert.ok(harness.requests.every(url => url.includes("/p1/")));
});

test("switching patient while the refresh awaits cannot receive the previous completion status", async () => {
  const harness = setup([{ status: 200 }], { switchDuringRefresh: true });
  await harness.run([photo("old.png")]);
  assert.equal(harness.text(), "Other patient");
});
