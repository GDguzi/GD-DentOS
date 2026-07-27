(function customerHubV2ContractModule(root, factory) {
  const api = factory();
  if (typeof module === "object" && module && module.exports) {
    module.exports = api;
  } else if (root && typeof root === "object") {
    root.CustomerHubV2Contract = api;
  }
})(typeof globalThis === "object" ? globalThis : this, function createCustomerHubV2Contract() {
  "use strict";

  const getOwnPropertyDescriptor = Object.getOwnPropertyDescriptor;

  const SERVER_MANAGED_WRITE_FIELDS = Object.freeze([
    "operator",
    "contacted_at",
    "actual_date",
  ]);

  function isRecord(value) {
    return value !== null && typeof value === "object" && !Array.isArray(value);
  }

  function ownDataValue(record, key) {
    if (!isRecord(record) && !Array.isArray(record)) return undefined;
    const descriptor = getOwnPropertyDescriptor(record, key);
    return descriptor && getOwnPropertyDescriptor(descriptor, "value")
      ? descriptor.value : undefined;
  }

  function ownArrayValues(value) {
    if (!Array.isArray(value)) return [];
    const items = [];
    for (let index = 0; index < value.length; index += 1) {
      const item = ownDataValue(value, String(index));
      if (item !== undefined) items.push(item);
    }
    return items;
  }

  function validRevision(value) {
    return typeof value === "number" && Number.isSafeInteger(value) && value > 0;
  }

  function safeIdentifier(value) {
    return typeof value === "string" && value.trim()
      ? encodeURIComponent(value.trim())
      : "";
  }

  function rawIdentifier(value) {
    return typeof value === "string" ? value.trim() : "";
  }

  function buildStaffDirectoryRequest(mode) {
    if (mode === "doctor") {
      return {method: "GET", url: "/api/staff-members?role=%E5%8C%BB%E7%94%9F"};
    }
    if (mode === "visitor") return {method: "GET", url: "/api/staff-members"};
    return null;
  }

  function normalizeStaffDirectory(payload) {
    const members = ownDataValue(payload, "members");
    if (!isRecord(payload) || !Array.isArray(members)) {
      throw new TypeError("invalid_staff_directory_envelope");
    }
    const validated = [];
    for (let index = 0; index < members.length; index += 1) {
      const member = ownDataValue(members, String(index));
      if (!isRecord(member)) throw new TypeError("invalid_staff_directory_member");
      const employmentStatus = ownDataValue(member, "employment_status");
      const staffId = ownDataValue(member, "staff_id");
      const name = ownDataValue(member, "name");
      const role = ownDataValue(member, "role");
      const rawRoles = ownDataValue(member, "roles");
      if (typeof staffId !== "string" || typeof name !== "string"
          || typeof role !== "string" || !Array.isArray(rawRoles)) {
        throw new TypeError("invalid_staff_directory_member");
      }
      const roles = [];
      for (let roleIndex = 0; roleIndex < rawRoles.length; roleIndex += 1) {
        const roleValue = ownDataValue(rawRoles, String(roleIndex));
        if (typeof roleValue !== "string") {
          throw new TypeError("invalid_staff_directory_member");
        }
        roles.push(roleValue.trim());
      }
      validated.push({
        staff_id: staffId,
        name,
        role,
        roles,
        departed: ["left", "inactive", "离职"].includes(employmentStatus)
          || Boolean(ownDataValue(member, "left_at")),
      });
    }
    return validated
      .filter(member => !member.departed)
      .map(({staff_id, name, role, roles}) => ({staff_id, name, role, roles}));
  }

  function normalizeListEnvelope(payload, errorCode) {
    const list = ownDataValue(payload, "list");
    const totalcount = ownDataValue(payload, "totalcount");
    if (!isRecord(payload) || !Array.isArray(list)
        || !Number.isSafeInteger(totalcount) || totalcount < 0) {
      throw new TypeError(errorCode);
    }
    const validated = [];
    for (let index = 0; index < list.length; index += 1) {
      const item = ownDataValue(list, String(index));
      if (!isRecord(item)) throw new TypeError(errorCode);
      validated.push(item);
    }
    return {list: validated, totalcount};
  }

  function normalizeReturnVisitList(payload) {
    return normalizeListEnvelope(payload, "invalid_return_visit_list_envelope");
  }

  function normalizeCommunicationList(payload) {
    return normalizeListEnvelope(payload, "invalid_communication_list_envelope");
  }

  function nullableString(value) {
    return value === null || typeof value === "string";
  }

  function strictOwnArray(value) {
    if (!Array.isArray(value)) throw new TypeError("invalid_array");
    const length = ownDataValue(value, "length");
    if (!Number.isSafeInteger(length) || length < 0) {
      throw new TypeError("invalid_array");
    }
    const items = [];
    for (let index = 0; index < length; index += 1) {
      const descriptor = getOwnPropertyDescriptor(value, String(index));
      if (!descriptor || !getOwnPropertyDescriptor(descriptor, "value")) {
        throw new TypeError("invalid_array");
      }
      items.push(descriptor.value);
    }
    return items;
  }

  function normalizeMedicalRecordItem(item, recordId) {
    const itemId = ownDataValue(item, "item_id");
    const parentId = ownDataValue(item, "record_id");
    const tooth = ownDataValue(item, "tooth");
    const itemType = ownDataValue(item, "item_type");
    const content = ownDataValue(item, "content");
    const sortOrder = ownDataValue(item, "sort_order");
    const createdAt = ownDataValue(item, "created_at");
    if (!isRecord(item)
        || ![itemId, parentId, tooth, itemType, content]
          .every(value => typeof value === "string")
        || parentId !== recordId
        || !Number.isSafeInteger(sortOrder)
        || !nullableString(createdAt)) {
      throw new TypeError("invalid_medical_record_item");
    }
    return {
      item_id: itemId,
      record_id: parentId,
      tooth,
      item_type: itemType,
      content,
      sort_order: sortOrder,
      created_at: createdAt,
    };
  }

  function normalizeMedicalRecordTimeline(payload) {
    try {
      const summaries = ownDataValue(payload, "medical_records");
      const groups = ownDataValue(payload, "records");
      const totalcount = ownDataValue(payload, "totalcount");
      if (!isRecord(payload) || !Array.isArray(summaries) || !isRecord(groups)
          || !Number.isSafeInteger(totalcount) || totalcount < 0) {
        throw new TypeError("invalid_medical_record_timeline");
      }

      const ids = new Set();
      let itemCount = 0;
      const medicalRecords = strictOwnArray(summaries).map(summary => {
        const recordId = ownDataValue(summary, "record_id");
        const recordType = ownDataValue(summary, "record_type");
        const visitTime = ownDataValue(summary, "visit_time");
        const doctorName = ownDataValue(summary, "doctor_name");
        const createdAt = ownDataValue(summary, "created_at");
        const updatedAt = ownDataValue(summary, "updated_at");
        if (!isRecord(summary) || typeof recordId !== "string" || !recordId.trim()
            || ids.has(recordId)
            || ![recordType, visitTime, doctorName, createdAt, updatedAt]
              .every(nullableString)) {
          throw new TypeError("invalid_medical_record_timeline");
        }
        ids.add(recordId);
        const rawItems = ownDataValue(groups, recordId);
        const items = strictOwnArray(rawItems)
          .map(item => normalizeMedicalRecordItem(item, recordId));
        itemCount += items.length;
        return {
          record_id: recordId,
          record_type: recordType,
          visit_time: visitTime,
          doctor_name: doctorName,
          created_at: createdAt,
          updated_at: updatedAt,
          items,
        };
      });

      const groupKeys = Object.keys(groups);
      if (groupKeys.length !== ids.size || groupKeys.some(key => !ids.has(key))
          || itemCount !== totalcount) {
        throw new TypeError("invalid_medical_record_timeline");
      }
      return {medical_records: medicalRecords, totalcount};
    } catch {
      throw new TypeError("invalid_medical_record_timeline");
    }
  }

  function validCalendarMonth(value) {
    const match = typeof value === "string" ? /^(\d{4})-(0[1-9]|1[0-2])$/.exec(value) : null;
    return Boolean(match && Number(match[1]) >= 1);
  }

  function validCalendarDate(value, month) {
    const match = typeof value === "string"
      ? /^(\d{4})-(\d{2})-(\d{2})$/.exec(value) : null;
    if (!match || value.slice(0, 7) !== month) return false;
    const year = Number(match[1]);
    const monthNumber = Number(match[2]);
    const day = Number(match[3]);
    const leap = year % 4 === 0 && (year % 100 !== 0 || year % 400 === 0);
    const monthDays = [31, leap ? 29 : 28, 31, 30, 31, 30, 31, 31, 30, 31, 30, 31];
    return year >= 1 && monthNumber >= 1 && monthNumber <= 12
      && day >= 1 && day <= monthDays[monthNumber - 1];
  }

  function validCalendarCount(value) {
    return typeof value === "number" && Number.isSafeInteger(value) && value >= 0;
  }

  function normalizeCalendarDays(payload, expectedMonth) {
    try {
      const month = ownDataValue(payload, "month");
      const days = ownDataValue(payload, "days");
      if (!isRecord(payload) || !validCalendarMonth(expectedMonth)
          || month !== expectedMonth || !Array.isArray(days)) {
        throw new TypeError("invalid_calendar_days");
      }
      const length = ownDataValue(days, "length");
      if (!Number.isSafeInteger(length) || length < 0 || length > 31) {
        throw new TypeError("invalid_calendar_days");
      }
      const normalized = [];
      const seenDates = new Set();
      for (let index = 0; index < length; index += 1) {
        const day = ownDataValue(days, String(index));
        const date = ownDataValue(day, "date");
        const count = ownDataValue(day, "count");
        const done = ownDataValue(day, "done");
        const pending = ownDataValue(day, "pending");
        const overdue = ownDataValue(day, "overdue");
        if (!isRecord(day) || !validCalendarDate(date, month)
            || ![count, done, pending, overdue].every(validCalendarCount)
            || count !== done + pending || overdue > pending || seenDates.has(date)) {
          throw new TypeError("invalid_calendar_days");
        }
        seenDates.add(date);
        normalized.push({date, count, done, pending, overdue});
      }
      return normalized;
    } catch {
      throw new TypeError("invalid_calendar_days");
    }
  }

  function copyWriteFields(fields) {
    const result = {};
    if (!isRecord(fields)) return result;
    for (const key of Object.keys(fields)) {
      const descriptor = getOwnPropertyDescriptor(fields, key);
      if (!descriptor || !getOwnPropertyDescriptor(descriptor, "value")) return null;
      if (SERVER_MANAGED_WRITE_FIELDS.includes(key) || key === "expected_revision") continue;
      result[key] = descriptor.value;
    }
    return result;
  }

  function returnVisitEditRecordMatches(record, source) {
    if (!isRecord(record)) return false;
    const returnVisitId = rawIdentifier(ownDataValue(source, "id"));
    const patientIdentity = rawIdentifier(ownDataValue(source, "patientIdentity"));
    if (!returnVisitId || !patientIdentity
        || ownDataValue(record, "return_visit_id") !== returnVisitId
        || ownDataValue(record, "patient_identity") !== patientIdentity) {
      return false;
    }
    const fields = copyWriteFields(ownDataValue(source, "fields"));
    if (!isRecord(fields)) return false;
    const keys = Object.keys(fields);
    if (!keys.length) return false;
    for (const key of keys) {
      if (ownDataValue(record, key) !== ownDataValue(fields, key)) return false;
    }
    return true;
  }

  function readReturnVisitRevision(record) {
    const revision = ownDataValue(record, "revision");
    return validRevision(revision) ? revision : null;
  }

  function validateReturnVisitEditReceipt(response, options) {
    const source = isRecord(options) ? options : {};
    const expectedRevision = ownDataValue(source, "expectedRevision");
    const data = ownDataValue(response, "data");
    const record = ownDataValue(data, "return_visit");
    const savedRevision = readReturnVisitRevision(record);
    if (!validRevision(expectedRevision) || savedRevision === null
        || savedRevision < expectedRevision) return null;
    // 正常更新必然升 revision；保留既有成功语义。只有同 revision 的 no-op
    // 需要完整回执逐项证明“提交值本来就在库里”，否则交给只读确认链路。
    if (savedRevision > expectedRevision) return savedRevision;
    if (!returnVisitEditRecordMatches(record, source)) return null;
    return savedRevision;
  }

  function validateReturnVisitEditConfirmation(record, options) {
    const source = isRecord(options) ? options : {};
    const expectedRevision = ownDataValue(source, "expectedRevision");
    const revision = readReturnVisitRevision(record);
    if (!validRevision(expectedRevision) || revision === null
        || revision <= expectedRevision
        || !returnVisitEditRecordMatches(record, source)) return null;
    return revision;
  }

  function copyOwnWriteFields(source) {
    const descriptor = isRecord(source) ? getOwnPropertyDescriptor(source, "fields") : null;
    if (!descriptor) return {};
    if (!getOwnPropertyDescriptor(descriptor, "value")) return null;
    return copyWriteFields(descriptor.value);
  }

  function jsonWrite(method, url, body) {
    return {method, url, bodyKind: "json", body};
  }

  function multipartWrite(method, url, options, extras = {}) {
    const source = isRecord(options) ? options : {};
    const expectedRevision = ownDataValue(source, "expectedRevision");
    const media = ownDataValue(source, "media");
    if (!validRevision(expectedRevision) || media == null) return null;
    return {
      method,
      url,
      bodyKind: "multipart",
      body: {
        file: media,
        file_name: typeof ownDataValue(source, "fileName") === "string"
          ? ownDataValue(source, "fileName") : "",
        expected_revision: expectedRevision,
        ...extras,
      },
    };
  }

  function buildReturnVisitWriteRequest(options) {
    const source = isRecord(options) ? options : {};
    const action = ownDataValue(source, "action");
    const id = safeIdentifier(ownDataValue(source, "id"));
    if (action === "create") {
      const patient = safeIdentifier(ownDataValue(source, "patientIdentity"));
      const fields = copyOwnWriteFields(source);
      return patient
        && fields !== null
        ? jsonWrite("POST", `/api/patients/${patient}/return-visits`, fields)
        : null;
    }
    const expectedRevision = ownDataValue(source, "expectedRevision");
    if (action === "edit" && id && validRevision(expectedRevision)) {
      const fields = copyOwnWriteFields(source);
      if (fields === null) return null;
      return jsonWrite("PUT", `/api/return-visits/${id}`, {
        ...fields,
        expected_revision: expectedRevision,
      });
    }
    if (action === "delete" && id && validRevision(expectedRevision)) {
      return jsonWrite("DELETE", `/api/return-visits/${id}`, {
        expected_revision: expectedRevision,
      });
    }
    if (action === "batchDelete") {
      const rawIds = ownDataValue(source, "ids");
      const ids = Array.isArray(rawIds) ? ownArrayValues(rawIds) : [];
      const rawRevisions = ownDataValue(source, "revisions");
      const revisions = isRecord(rawRevisions) ? rawRevisions : {};
      const items = ids.map(rawId => {
        const itemId = rawIdentifier(rawId);
        const revision = ownDataValue(revisions, itemId);
        return itemId && validRevision(revision)
          ? {id: itemId, expected_revision: revision}
          : null;
      }).filter(Boolean);
      return items.length > 0 && items.length === ids.length
        ? jsonWrite("POST", "/api/return-visits/batch-delete", {items})
        : null;
    }
    if (action === "attachment" && id) {
      return multipartWrite("POST", `/api/return-visits/${id}/images`, source);
    }
    if (action === "attachmentDelete" && id && validRevision(expectedRevision)) {
      const imageId = safeIdentifier(ownDataValue(source, "imageId"));
      return imageId
        ? jsonWrite("DELETE", `/api/return-visits/${id}/images/${imageId}`, {
          expected_revision: expectedRevision,
        })
        : null;
    }
    return null;
  }

  function buildCommunicationWriteRequest(options) {
    const source = isRecord(options) ? options : {};
    const action = ownDataValue(source, "action");
    const id = safeIdentifier(ownDataValue(source, "id"));
    if (action === "create") {
      const patient = safeIdentifier(ownDataValue(source, "patientIdentity"));
      const fields = copyOwnWriteFields(source);
      return patient
        && fields !== null
        ? jsonWrite("POST", `/api/patients/${patient}/communications`, fields)
        : null;
    }
    const expectedRevision = ownDataValue(source, "expectedRevision");
    if (action === "edit" && id && validRevision(expectedRevision)) {
      const fields = copyOwnWriteFields(source);
      if (fields === null) return null;
      return jsonWrite("PATCH", `/api/communications/${id}`, {
        ...fields,
        expected_revision: expectedRevision,
      });
    }
    if (action === "delete" && id && validRevision(expectedRevision)) {
      return jsonWrite("DELETE", `/api/communications/${id}`, {
        expected_revision: expectedRevision,
      });
    }
    if (action === "attachment" && id) {
      return multipartWrite("POST", `/api/communications/${id}/images`, source);
    }
    if (action === "attachmentDelete" && id && validRevision(expectedRevision)) {
      const imageId = safeIdentifier(ownDataValue(source, "imageId"));
      return imageId
        ? jsonWrite("DELETE", `/api/communications/${id}/images/${imageId}`, {
          expected_revision: expectedRevision,
        })
        : null;
    }
    return null;
  }

  function buildCallWriteRequest(options) {
    const source = isRecord(options) ? options : {};
    const action = ownDataValue(source, "action");
    const id = safeIdentifier(ownDataValue(source, "id"));
    const expectedRevision = ownDataValue(source, "expectedRevision");
    if (action === "edit" && id && validRevision(expectedRevision)) {
      const fields = copyOwnWriteFields(source);
      if (fields === null) return null;
      return jsonWrite("PATCH", `/api/calls/${id}`, {
        ...fields,
        expected_revision: expectedRevision,
      });
    }
    if (action === "delete" && id && validRevision(expectedRevision)) {
      return jsonWrite("DELETE", `/api/calls/${id}`, {
        expected_revision: expectedRevision,
      });
    }
    if (action === "attachment") {
      const patient = safeIdentifier(ownDataValue(source, "patientIdentity"));
      const returnVisitId = rawIdentifier(ownDataValue(source, "linkedReturnVisitId"));
      const communicationId = rawIdentifier(ownDataValue(source, "linkedCommunicationId"));
      if (!patient || Boolean(returnVisitId) === Boolean(communicationId)) return null;
      return multipartWrite("POST", `/api/patients/${patient}/calls`, source, {
        direction: ownDataValue(source, "direction") === "inbound" ? "inbound" : "outbound",
        source: ownDataValue(source, "source") === "desktop_mic" ? "desktop_mic" : "manual_import",
        ...(returnVisitId ? {linked_return_visit_id: returnVisitId} : {}),
        ...(communicationId ? {linked_communication_id: communicationId} : {}),
      });
    }
    return null;
  }

  function buildDictionaryWriteRequest(options) {
    const source = isRecord(options) ? options : {};
    if (ownDataValue(source, "action") !== "update") return null;
    const fields = copyOwnWriteFields(source);
    return fields === null ? null
      : jsonWrite("PUT", "/api/settings/customer-hub-dicts", fields);
  }

  function writeFailureMessage(status) {
    if (status === 400) return "保存失败：请检查填写内容后重试";
    if (status === 403) return "保存失败：权限不足，请联系管理员";
    if (status === 409) return "保存冲突：数据已更新，请刷新确认后显式重试";
    if (status >= 500) return "保存失败：服务异常，请稍后显式重试";
    return "保存失败：网络异常，请检查连接后显式重试";
  }

  function responseStatus(response) {
    const status = ownDataValue(response, "status");
    return typeof status === "number" && Number.isInteger(status) ? status : 0;
  }

  function clearSuccessfulWriteState(state) {
    if (!isRecord(state)) return;
    if (isRecord(ownDataValue(state, "fields"))) state.fields = {};
    if (Array.isArray(ownDataValue(state, "pendingMedia"))) state.pendingMedia = [];
    if (Array.isArray(ownDataValue(state, "selectedItems"))) state.selectedItems = [];
    if (getOwnPropertyDescriptor(state, "modalOpen")?.value !== undefined) state.modalOpen = false;
    if (getOwnPropertyDescriptor(state, "dirty")?.value !== undefined) state.dirty = false;
  }

  function freezeWriteControls(controls) {
    const snapshots = [];
    if (!controls || typeof controls[Symbol.iterator] !== "function") return snapshots;
    for (const control of controls) {
      if (!control || typeof control !== "object") continue;
      snapshots.push({control, disabled: control.disabled === true});
      control.disabled = true;
      if (typeof control.setAttribute === "function") {
        control.setAttribute("aria-busy", "true");
        control.setAttribute("aria-disabled", "true");
      }
    }
    return snapshots;
  }

  function restoreWriteControls(snapshots) {
    for (const snapshot of snapshots) {
      const control = snapshot.control;
      if (!control || typeof control !== "object") continue;
      control.disabled = snapshot.disabled;
      if (typeof control.setAttribute === "function") {
        control.setAttribute("aria-busy", "false");
        control.setAttribute("aria-disabled", snapshot.disabled ? "true" : "false");
      }
    }
  }

  function requestFetch(options) {
    if (isRecord(options) && typeof options.fetch === "function") return options.fetch;
    return typeof globalThis === "object" && typeof globalThis.fetch === "function"
      ? globalThis.fetch.bind(globalThis)
      : null;
  }

  async function parseJson(response) {
    if (!response || typeof response.json !== "function") return {};
    try {
      return await response.json();
    } catch {
      return {};
    }
  }

  async function parseRequiredJson(response) {
    if (!response || typeof response.json !== "function") {
      throw new TypeError("invalid_json_response");
    }
    try {
      return await response.json();
    } catch {
      throw new TypeError("invalid_json_response");
    }
  }

  async function fetchJson(url, options = {}) {
    const fetchImpl = requestFetch(options);
    if (!fetchImpl) throw new TypeError("fetch_required");
    const response = await fetchImpl(url, {method: options.method || "GET"});
    const data = await parseRequiredJson(response);
    if (!response || response.ok !== true) {
      const error = new Error(response && response.status === 409
        ? writeFailureMessage(409)
        : "客户通载入失败");
      error.status = response && Number.isInteger(response.status) ? response.status : 0;
      error.data = data;
      throw error;
    }
    return data;
  }

  async function sendCustomerHubRequest(request, options = {}) {
    const fetchImpl = requestFetch(options);
    if (!fetchImpl || !isRecord(request)) throw new TypeError("invalid_write_request");
    const init = {method: request.method};
    if (request.bodyKind === "json") {
      init.headers = {"Content-Type": "application/json"};
      init.body = JSON.stringify(isRecord(request.body) ? request.body : {});
    } else if (request.bodyKind === "multipart") {
      if (typeof FormData !== "function" || !isRecord(request.body)) {
        throw new TypeError("multipart_unavailable");
      }
      const form = new FormData();
      for (const [key, value] of Object.entries(request.body)) {
        if (key === "file_name" || value == null) continue;
        if (key === "file") {
          const name = request.body.file_name;
          if (typeof Blob === "function" && value instanceof Blob && name) form.append(key, value, name);
          else form.append(key, value);
        } else {
          form.append(key, String(value));
        }
      }
      init.body = form;
    }
    const response = await fetchImpl(request.url, init);
    return {
      ok: Boolean(response && response.ok === true),
      status: response && Number.isInteger(response.status) ? response.status : 0,
      data: await parseJson(response),
      response,
    };
  }

  async function runRetriableWrite(options) {
    const source = isRecord(options) ? options : {};
    const request = ownDataValue(source, "request");
    const sendRequest = ownDataValue(source, "sendRequest");
    const rawState = ownDataValue(source, "state");
    const state = isRecord(rawState) ? rawState : null;
    const focusTarget = ownDataValue(source, "focusTarget");
    const isCurrent = ownDataValue(source, "isCurrent");
    const current = () => typeof isCurrent !== "function" || isCurrent() === true;
    const writeToken = {};
    if (state && ownDataValue(state, "writeInFlight")) {
      return {ok: false, status: 0, busy: true, message: "保存处理中，请勿重复提交"};
    }
    if (state) state.writeInFlight = writeToken;
    const controlSnapshots = freezeWriteControls(ownDataValue(source, "pendingControls"));
    const pendingFocusTarget = ownDataValue(source, "pendingFocusTarget");
    if (controlSnapshots.length && pendingFocusTarget
        && typeof pendingFocusTarget.focus === "function") pendingFocusTarget.focus();
    let focusAfterRelease = false;
    try {
      if (!isRecord(request) || typeof sendRequest !== "function") {
        focusAfterRelease = true;
        return {ok: false, status: 400, message: writeFailureMessage(400)};
      }
      let response;
      try {
        response = await sendRequest(request);
      } catch {
        if (!current()) return {ok: false, status: 0, stale: true};
        focusAfterRelease = true;
        return {ok: false, status: 0, message: writeFailureMessage(0)};
      }
      if (!current()) return {ok: false, status: 0, stale: true};
      if (ownDataValue(response, "ok") !== true) {
        const status = responseStatus(response);
        focusAfterRelease = true;
        return {ok: false, status, message: writeFailureMessage(status), response};
      }
      clearSuccessfulWriteState(state);
      const onSuccess = ownDataValue(source, "onSuccess");
      if (typeof onSuccess === "function") await onSuccess(response);
      return {ok: true, status: responseStatus(response) || 200, response};
    } finally {
      restoreWriteControls(controlSnapshots);
      if (state && ownDataValue(state, "writeInFlight") === writeToken) delete state.writeInFlight;
      if (focusAfterRelease && current() && focusTarget && focusTarget.isConnected !== false
          && focusTarget.disabled !== true && typeof focusTarget.focus === "function") {
        focusTarget.focus();
      }
    }
  }

  function currentOptions(options) {
    const source = isRecord(options) ? options : {};
    const state = ownDataValue(source, "state");
    const stateRevision = ownDataValue(state, "expectedRevision");
    const stateRevisions = ownDataValue(state, "revisions");
    const current = {};
    for (const key of [
      "action", "id", "imageId", "patientIdentity", "fields", "ids", "revisions",
      "media", "fileName", "direction", "source", "expectedRevision", "state",
      "linkedReturnVisitId", "linkedCommunicationId", "sendRequest", "focusTarget",
      "onSuccess", "isCurrent", "pendingControls", "pendingFocusTarget",
    ]) {
      const value = ownDataValue(source, key);
      if (value !== undefined) current[key] = value;
    }
    if (validRevision(stateRevision)) current.expectedRevision = stateRevision;
    if (isRecord(stateRevisions)) current.revisions = stateRevisions;
    return current;
  }

  function handleWrite(builder, options) {
    const current = currentOptions(options);
    return runRetriableWrite({
      request: builder(current),
      sendRequest: ownDataValue(current, "sendRequest"),
      state: ownDataValue(current, "state"),
      focusTarget: ownDataValue(current, "focusTarget"),
      onSuccess: ownDataValue(current, "onSuccess"),
      isCurrent: ownDataValue(current, "isCurrent"),
      pendingControls: ownDataValue(current, "pendingControls"),
      pendingFocusTarget: ownDataValue(current, "pendingFocusTarget"),
    });
  }

  function handleReturnVisitWrite(options) {
    return handleWrite(buildReturnVisitWriteRequest, options);
  }

  function handleCommunicationWrite(options) {
    return handleWrite(buildCommunicationWriteRequest, options);
  }

  function handleCallWrite(options) {
    return handleWrite(buildCallWriteRequest, options);
  }

  function handleDictionaryWrite(options) {
    return handleWrite(buildDictionaryWriteRequest, options);
  }

  return Object.freeze({
    validRevision,
    buildStaffDirectoryRequest,
    normalizeStaffDirectory,
    normalizeReturnVisitList,
    normalizeCommunicationList,
    normalizeMedicalRecordTimeline,
    normalizeCalendarDays,
    readReturnVisitRevision,
    validateReturnVisitEditReceipt,
    validateReturnVisitEditConfirmation,
    fetchJson,
    sendCustomerHubRequest,
    buildReturnVisitWriteRequest,
    buildCommunicationWriteRequest,
    buildCallWriteRequest,
    buildDictionaryWriteRequest,
    writeFailureMessage,
    runRetriableWrite,
    handleReturnVisitWrite,
    handleCommunicationWrite,
    handleCallWrite,
    handleDictionaryWrite,
  });
});
