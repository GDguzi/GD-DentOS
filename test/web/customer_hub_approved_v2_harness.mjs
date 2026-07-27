import {readFileSync} from "node:fs";
import {dirname, join} from "node:path";
import {fileURLToPath} from "node:url";
import vm from "node:vm";

const here = dirname(fileURLToPath(import.meta.url));
const root = join(here, "..", "..");
const contractSource = readFileSync(join(root, "local_app", "static", "customer_hub_v2_contract.js"), "utf8");
const followupDateSource = readFileSync(join(root, "local_app", "static", "customer_hub_v2_followup_date.js"), "utf8");
const viewSource = readFileSync(join(root, "local_app", "static", "customer_hub_v2.js"), "utf8");

export function deferred() {
  let resolve;
  let reject;
  const promise = new Promise((done, fail) => { resolve = done; reject = fail; });
  return {promise, resolve, reject};
}

export const settle = () => new Promise(resolve => setTimeout(resolve, 0));

function attrMap(source) {
  const attrs = new Map();
  const re = /([^\s=]+)(?:\s*=\s*(?:"([^"]*)"|'([^']*)'|([^\s>]+)))?/g;
  let match;
  while ((match = re.exec(source))) {
    attrs.set(match[1], match[2] ?? match[3] ?? match[4] ?? "");
  }
  return attrs;
}

function dataKey(name) {
  return name.slice(5).replace(/-([a-z])/g, (_all, letter) => letter.toUpperCase());
}

class FakeClassList {
  constructor(element) { this.element = element; }
  contains(name) { return this.element.className.split(/\s+/).includes(name); }
  toggle(name, force) {
    const names = new Set(this.element.className.split(/\s+/).filter(Boolean));
    const enabled = force === undefined ? !names.has(name) : Boolean(force);
    if (enabled) names.add(name); else names.delete(name);
    this.element.className = [...names].join(" ");
    return enabled;
  }
}

export class FakeElement {
  constructor(tagName = "div", document = null) {
    this.tagName = tagName.toUpperCase();
    this.ownerDocument = document;
    this.parentElement = null;
    this.dataset = {};
    this.disabled = false;
    this.readOnly = false;
    this.checked = false;
    this.hidden = false;
    this.value = "";
    this.files = [];
    this.name = "";
    this.type = "";
    this.id = "";
    this.className = "";
    this.textContent = "";
    this.style = {};
    this.isConnected = true;
    this.removed = false;
    this.focusCount = 0;
    this.attributes = new Map();
    this._listeners = new Map();
    this._nodes = [];
    this._root = this;
    this.classList = new FakeClassList(this);
  }

  addEventListener(type, listener) {
    const list = this._listeners.get(type) || [];
    list.push(listener);
    this._listeners.set(type, list);
  }

  async dispatch(type, extra = {}) {
    const event = {
      ...extra,
      target: extra.target || this,
      currentTarget: this,
      defaultPrevented: false,
      propagationStopped: false,
      preventDefault() { this.defaultPrevented = true; },
      stopPropagation() { this.propagationStopped = true; },
    };
    for (const listener of this._listeners.get(type) || []) await listener(event);
    if (!event.propagationStopped && this !== this._root) {
      for (const listener of this._root._listeners.get(type) || []) await listener(event);
    }
    return event;
  }

  setAttribute(name, value) {
    this.attributes.set(name, String(value));
    if (name === "disabled") this.disabled = true;
    else if (name === "hidden") this.hidden = true;
    else if (name === "id") this.id = String(value);
    else if (name === "class") this.className = String(value);
  }

  getAttribute(name) { return this.attributes.get(name) ?? null; }
  matches(selector) { return matches(this, selector); }
  focus() {
    if (!isFocusable(this)) return;
    this.focusCount += 1;
    if (this.ownerDocument) this.ownerDocument.activeElement = this;
  }

  set innerHTML(value) {
    this._html = String(value);
    if (this === this._root) {
      this._nodes = [];
    } else {
      const root = this._root;
      const descendants = new Set(root._nodes.filter(node => isDescendantOf(node, this)));
      for (const node of descendants) node.isConnected = false;
      root._nodes = root._nodes.filter(node => !descendants.has(node));
    }
    this._parseIntoRoot(this._html);
  }

  get innerHTML() { return this._html || ""; }

  _parseIntoRoot(html) {
    const root = this._root || this;
    const re = /<(\/)?(input|button|textarea|select|option|fieldset|legend|div|span|nav|label|a|h[1-6])\b([^>]*)>/gi;
    const stack = [this];
    const voidTags = new Set(["INPUT"]);
    let match;
    while ((match = re.exec(html))) {
      const tagName = match[2].toUpperCase();
      if (match[1]) {
        while (stack.length > 1) {
          const closed = stack.pop();
          if (closed.tagName === tagName) break;
        }
        continue;
      }
      const node = new FakeElement(match[2], this.ownerDocument);
      node._root = root;
      node.parentElement = stack[stack.length - 1] || null;
      const attrs = attrMap(match[3]);
      for (const [name, raw] of attrs) {
        const value = raw.replaceAll("&quot;", '"').replaceAll("&amp;", "&");
        node.attributes.set(name, value);
        if (name === "id") node.id = value;
        else if (name === "class") node.className = value;
        else if (name === "value") node.value = value;
        else if (name === "name") node.name = value;
        else if (name === "type") node.type = value;
        else if (name === "disabled") node.disabled = true;
        else if (name === "hidden") node.hidden = true;
        else if (name === "readonly") node.readOnly = true;
        else if (name === "checked") node.checked = true;
        else if (name === "style") {
          for (const declaration of value.split(";")) {
            const [property, ...rest] = declaration.split(":");
            if (property && rest.length) node.style[property.trim()] = rest.join(":").trim();
          }
        }
        else if (name.startsWith("data-")) node.dataset[dataKey(name)] = value;
      }
      node.classList = new FakeClassList(node);
      root._nodes.push(node);
      if (!voidTags.has(tagName) && !/\/\s*>$/.test(match[0])) stack.push(node);
    }
  }

  appendChild(node) {
    const root = this._root || this;
    node._root = root;
    node.ownerDocument = this.ownerDocument;
    node.parentElement = this;
    root._nodes.push(node);
    return node;
  }

  insertAdjacentHTML(_position, html) { this._parseIntoRoot(String(html)); }
  closest(selector) {
    for (let current = this; current; current = current.parentElement) {
      if (matches(current, selector)) return current;
    }
    return null;
  }
  after(node) { this._root.appendChild(node); }

  querySelectorAll(selector) {
    const nodes = this === this._root ? this._nodes : this._root._nodes;
    return nodes.filter(node => (this === this._root || isDescendantOf(node, this))
      && selector.split(",").some(part => matches(node, part.trim())));
  }

  querySelector(selector) { return this.querySelectorAll(selector)[0] || null; }

  remove() {
    this.removed = true;
    this.isConnected = false;
    for (const node of this._nodes) node.isConnected = false;
  }
}

function isDescendantOf(node, ancestor) {
  for (let current = node?.parentElement; current; current = current.parentElement) {
    if (current === ancestor) return true;
  }
  return false;
}

function isDisabled(node) {
  if (!node) return false;
  if (node.disabled) return true;
  for (let current = node.parentElement; current; current = current.parentElement) {
    if (current.tagName === "FIELDSET" && current.disabled) return true;
  }
  return false;
}

function matches(node, selector) {
  if (!selector) return false;
  if (selector === ":disabled") return isDisabled(node);
  if (selector === "input" || selector === "select" || selector === "textarea" || selector === "button") {
    return node.tagName === selector.toUpperCase();
  }
  if (selector.startsWith("#")) return node.id === selector.slice(1);
  if (selector.startsWith(".")) {
    const [className, pseudo] = selector.slice(1).split(":");
    return node.classList.contains(className) && (!pseudo || (pseudo === "checked" && node.checked));
  }
  const attributeMatches = [...selector.matchAll(/\[([^\]=]+)(?:=([^\]]+))?\]/g)];
  let tag = selector.match(/^[a-z][a-z0-9-]*/i)?.[0];
  if (tag && node.tagName !== tag.toUpperCase()) return false;
  for (const match of attributeMatches) {
    const name = match[1];
    const expected = match[2]?.replace(/^['"]|['"]$/g, "");
    const actual = name.startsWith("data-")
      ? node.dataset[dataKey(name)]
      : name === "name" ? node.name : node.attributes.get(name);
    if (actual === undefined || (expected !== undefined && actual !== expected)) return false;
  }
  if (selector.includes(":checked") && !node.checked) return false;
  if (selector.includes(":disabled") && !isDisabled(node)) return false;
  return Boolean(tag || attributeMatches.length);
}

function isFocusable(node) {
  if (!node || node.isConnected === false || isDisabled(node)) return false;
  for (let current = node; current; current = current.parentElement) {
    if (current.hidden || current.getAttribute("aria-hidden") === "true"
        || current.style?.display === "none" || current.style?.visibility === "hidden"
        || current.computedDisplay === "none" || current.computedVisibility === "hidden") return false;
  }
  if (node.tagName === "INPUT" && node.type === "hidden") return false;
  const tabindex = node.getAttribute("tabindex");
  if (["BUTTON", "INPUT", "SELECT", "TEXTAREA"].includes(node.tagName)) return true;
  if (node.tagName === "A" && node.getAttribute("href") !== null) return true;
  return tabindex !== null;
}

class FakeFormData {
  constructor() { this.entries = []; }
  append(name, value, fileName) { this.entries.push([name, value, fileName]); }
  get(name) { return this.entries.find(entry => entry[0] === name)?.[1]; }
}

function response(status, data = {}) {
  return {ok: status >= 200 && status < 300, status, data, async json() { return data; }};
}

export function writeResponse(status, data = {}) { return {status, data}; }

export function makeApprovedV2Harness({
  revision = 1,
  omitInitialRevision = false,
  initialDetail = {},
  writeImpl,
  getImpl,
  detailReadImpl,
  confirmImpl,
  promptImpl,
  getUserMedia,
  dictionaries = {return_type: [], visit_content: [], visit_result: [], comm_reason: []},
  permissions = [
    "patient.profile.view", "return_visit.view", "return_visit.manage", "return_visit.delete",
    "master_data.view",
  ],
} = {}) {
  const permissionSet = new Set(permissions);
  const overlays = [];
  const writes = [];
  const detailReads = [];
  const gets = [];
  const alerts = [];
  const confirms = [];
  const openedPatients = [];
  const recorders = [];
  const audios = [];
  let mediaCalls = 0;

  class FakeBlob {
    constructor(parts = [], options = {}) {
      this.parts = parts;
      this.type = options.type || "";
      this.size = parts.reduce((total, part) => total + Number(part?.size || 0), 0);
    }
  }

  class FakeRecorder {
    constructor(stream) {
      this.stream = stream;
      this.state = "inactive";
      this.mimeType = "audio/webm";
      recorders.push(this);
    }
    start() { this.state = "recording"; }
    stop() {
      if (this.state !== "recording") return;
      this.state = "inactive";
      queueMicrotask(() => this.onstop?.());
    }
    emit(part) { this.ondataavailable?.({data: part}); }
  }
  class FakeAudio {
    constructor(url) {
      this.url = url;
      this.playCount = 0;
      audios.push(this);
    }
    play() {
      this.playCount += 1;
      return Promise.resolve();
    }
  }
  const documentListeners = new Map();
  const document = {
    activeElement: null,
    body: {
      appendChild(element) { overlays.push(element); },
      contains(element) { return Boolean(element && !element.removed); },
    },
    createElement(tagName) { return new FakeElement(tagName, document); },
    getElementById(id) {
      for (const overlay of overlays.filter(item => !item.removed)) {
        if (overlay.id === id) return overlay;
        const child = overlay.querySelector(`#${id}`);
        if (child) return child;
      }
      return null;
    },
    querySelectorAll(selector) {
      if (selector === ".chv-ovl") return overlays.filter(item => !item.removed);
      return [];
    },
    addEventListener(type, listener) {
      const list = documentListeners.get(type) || [];
      list.push(listener);
      documentListeners.set(type, list);
    },
    removeEventListener(type, listener) {
      const list = documentListeners.get(type) || [];
      documentListeners.set(type, list.filter(item => item !== listener));
    },
    async dispatch(type, extra = {}) {
      const event = {
        ...extra,
        target: extra.target || document.activeElement || document,
        defaultPrevented: false,
        propagationStopped: false,
        preventDefault() { this.defaultPrevented = true; },
        stopPropagation() { this.propagationStopped = true; },
      };
      for (const listener of documentListeners.get(type) || []) await listener(event);
      return event;
    },
  };
  const modulePanel = new FakeElement("main", document);
  modulePanel.id = "modulePanel";
  const fakeWindow = {
    alert(message) { alerts.push(String(message)); },
    confirm(message) {
      confirms.push(String(message));
      return confirmImpl ? confirmImpl(message, confirms.length) : true;
    },
    prompt(message, value) {
      return promptImpl ? promptImpl(message, value) : value;
    },
    getComputedStyle(element) {
      return {
        display: element.computedDisplay ?? element.style?.display ?? "",
        visibility: element.computedVisibility ?? element.style?.visibility ?? "",
      };
    },
    open() {},
    MediaRecorder: getUserMedia ? FakeRecorder : undefined,
    Audio: FakeAudio,
  };
  const sandbox = {
    console,
    document,
    window: fakeWindow,
    navigator: {mediaDevices: getUserMedia ? {
      async getUserMedia(options) { mediaCalls += 1; return getUserMedia(options); },
    } : null},
    FormData: FakeFormData,
    Blob: FakeBlob,
    MediaRecorder: FakeRecorder,
    Audio: FakeAudio,
    File: class FakeFile {},
    MutationObserver: class { observe() {} },
    setTimeout,
    clearTimeout,
    setInterval,
    clearInterval,
    modulePanel,
    moduleViewToken: 0,
    workDate: {value: "2026-07-21"},
    localDateValue: () => "2026-07-21",
    bjToday: () => new Date("2026-07-21T00:00:00"),
    escapeHtml: value => String(value ?? ""),
    escapeAttr: value => String(value ?? ""),
    hasPerm: permission => permissionSet.has(permission),
    openPatientWorkspace(patientId, tab) { openedPatients.push({patientId, tab}); },
    openNewAppointment() {},
    async guardSubmit(button, callback) {
      if (button._guardPending) return false;
      button._guardPending = true;
      const wasDisabled = button.disabled;
      button.disabled = true;
      try { return await callback(); }
      finally { button.disabled = wasDisabled; button._guardPending = false; }
    },
  };
  fakeWindow.window = fakeWindow;
  fakeWindow.document = document;

  let detailReadCount = 0;
  sandbox.fetch = async (url, init = {}) => {
    const method = init.method || "GET";
    if (method === "GET") {
      if (getImpl) {
        gets.push(url);
        const custom = await getImpl(url, gets.length);
        if (custom !== undefined) {
          if (custom instanceof Error) throw custom;
          return response(custom?.status ?? 200, custom?.data ?? custom);
        }
      }
      if (url === "/api/settings/customer-hub-dicts") {
        return response(200, dictionaries);
      }
      if (url === "/api/dictionaries?type=ReturnPlan") return response(200, {items: []});
      if (url.startsWith("/api/patients/") && url.endsWith("/calls")) return response(200, {calls: []});
      if (url.startsWith("/api/patients/")) return response(200, {patient: {display_name: "Synthetic"}});
      if (url.startsWith("/api/return-visits/")) {
        detailReadCount += 1;
        if (detailReadCount === 1) {
          return response(200, {
            ...(omitInitialRevision ? {} : {revision}),
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
            ...initialDetail,
          });
        }
        detailReads.push(url);
        try {
          const result = detailReadImpl
            ? await detailReadImpl(url, detailReads.length)
            : {revision: revision + 1};
          return response(200, result);
        } catch (error) {
          return response(error?.status || 500, {detail: "synthetic read failure"});
        }
      }
      return response(200, {});
    }

    let body = init.body;
    if (typeof body === "string") body = JSON.parse(body);
    const request = {url, method, body};
    writes.push(request);
    const result = writeImpl
      ? await writeImpl(request, writes.length)
      : writeResponse(200, {return_visit: {revision: revision + writes.length}});
    if (result instanceof Error) throw result;
    return response(result?.status ?? 200, result?.data ?? {});
  };

  vm.createContext(sandbox);
  vm.runInContext(contractSource, sandbox);
  fakeWindow.CustomerHubV2Contract = sandbox.CustomerHubV2Contract;
  vm.runInContext(followupDateSource, sandbox);
  fakeWindow.CustomerHubV2FollowupDate = sandbox.CustomerHubV2FollowupDate;
  vm.runInContext(viewSource, sandbox);
  vm.runInContext(`
    _chvRenderPane = async () => {};
    _chvLoadReturnPlans = async () => [];
    loadCustomerHubV2 = async () => { this.__loadCount += 1; return true; };
    this.__openRv = _chvOpenRvModal;
    this.__overlay = _chvOvl;
    this.__guardDirty = _chvGuardDirty;
    this.__openDict = _chvOpenDict;
    this.__openCalendar = _chvOpenCalendar;
    this.__openMonthly = _chvOpenMonthlyReview;
    this.__openAddComm = _chvOpenAddComm;
    this.__openNewRv = _chvOpenNewRv;
    this.__playRecording = _chvPlayRecording;
    this.__focusables = _chvDialogFocusables;
    this.__setDicts = value => { _chvDicts = value; };
    this.__dispose = _chvDisposeOverlay;
    this.__leaveHub = requestLeaveCustomerHubV2;
    this.__bindTabs = _chvBindTabs;
    this.__setView = value => { _chvView = value; };
    this.__getView = () => _chvView;
    this.__getDicts = () => _chvDicts;
    this.__bindBatchDelete = typeof _chvBindBatchDelete === "function" ? _chvBindBatchDelete : undefined;
    this.__pagePending = () => typeof _chvPageWriteState === "object" && Boolean(_chvPageWriteState.writeInFlight);
  `, Object.assign(sandbox, {__loadCount: 0}));

  async function openRv(rvId = "rv/synthetic") {
    await sandbox.__openRv(rvId, "patient/synthetic");
    return overlays.find(item => item.id === "chvRvOvl" && !item.removed);
  }

  return {
    sandbox,
    document,
    modulePanel,
    overlays,
    writes,
    detailReads,
    gets,
    alerts,
    confirms,
    openedPatients,
    recorders,
    audios,
    mediaCalls: () => mediaCalls,
    setPermissions(values) {
      permissionSet.clear();
      for (const value of values) permissionSet.add(value);
    },
    openRv,
  };
}

export function setValidEditDraft(overlay) {
  overlay.querySelector("#chvMT").value = "2026-07-21T11:00";
  overlay.querySelector("#chvMI").value = "Edited synthetic item";
  overlay.querySelector("#chvMC").value = "Edited synthetic note";
  overlay.querySelector("#chvMV").value = "Synthetic operator";
  overlay.querySelector("#chvMR").value = "Synthetic result";
}

export function multipartFile(request) {
  return request.body?.get("file");
}
