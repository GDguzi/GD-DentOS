// 角色权限：旧模式保留横向矩阵整表保存；Access V3 使用单岗位草稿树。
// V3 高风险修改仍须先 POST /api/auth/reauth，再填写原因并提交单岗位权限全集。

let _rpPendingRole = null;
let _rpReauthReady = false;
let _rpBaselineByRole = null;
let _rpLeavePromise = null;
let _rpSaveInFlight = false;
let _rpFlowRevision = 0;
let _rpReauthInFlight = false;
let _rpRenderRevision = 0;
let _rpMessageRecord = {text: "", kind: "active", roleKey: ""};
let _rpSaveRevision = 0;
let _rpAutoAddedDependencyGeneration = 0;
let _rpRolePermissionModalSuspended = false;

const _RP_SAVE_ERROR_MESSAGES = Object.freeze({
  permission_dependency_missing: "缺少前置查看权限，请检查依赖项。",
  permission_unknown: "权限目录已变化，请刷新后重试。",
  permission_not_assignable: "此能力只属于系统管理员，不能分配给岗位。",
  role_not_configurable: "该岗位不能在这里配置。",
  recent_reauthentication_required: "当前密码确认已过期，请重新确认。",
  access_denied: "你没有修改角色权限的权限。",
  role_permissions_stale: "权限已被其他管理员修改，请刷新后重试，当前草稿已保留",
});

const _rpV3State = {
  roles: [],
  permissions: [],
  permissionByKey: new Map(),
  systemAdminCapabilities: [],
  selectedRoleKey: "",
  baselineByRole: new Map(),
  draftByRole: new Map(),
  search: "",
  view: "all",
  expanded: new Set(),
  autoAddedDependencies: new Map(),
  message: "",
  containerId: "",
  container: null,
  loaded: false,
};

function _rpEscape(value) {
  return String(value ?? "")
    .replaceAll("&", "&amp;")
    .replaceAll('"', "&quot;")
    .replaceAll("'", "&#39;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;");
}

function _isCurrentRolePermissionRender(revision, containerId, box) {
  return revision === _rpRenderRevision
    && document.getElementById(containerId) === box;
}

function _isRecord(value) {
  return value !== null && typeof value === "object" && !Array.isArray(value);
}

function _hasOwn(value, key) {
  return Object.prototype.hasOwnProperty.call(value, key);
}

function _isNonEmptyString(value) {
  return typeof value === "string" && value.trim().length > 0;
}

function _assertPayload(condition) {
  if (!condition) throw new Error("invalid role permission payload");
}

function _assertDependencyGraphAcyclic(permissions) {
  const byKey = new Map(permissions.map(item => [item.key, item]));
  const visiting = new Set();
  const visited = new Set();
  const visit = key => {
    if (visited.has(key)) return;
    _assertPayload(!visiting.has(key));
    visiting.add(key);
    byKey.get(key).dependencies.forEach(visit);
    visiting.delete(key);
    visited.add(key);
  };
  permissions.forEach(item => visit(item.key));
}

function normalizeRolePermissionPayload(data) {
  _assertPayload(_isRecord(data));
  for (const key of ["roles", "perms", "matrix", "system_admin_capabilities"]) {
    _assertPayload(_hasOwn(data, key));
  }
  _assertPayload(Array.isArray(data.roles) && data.roles.length > 0);
  _assertPayload(Array.isArray(data.perms) && data.perms.length > 0);
  _assertPayload(_isRecord(data.matrix));
  _assertPayload(
    Array.isArray(data.system_admin_capabilities)
      && data.system_admin_capabilities.length === 5,
  );

  const roleKeys = new Set();
  const roles = data.roles.map(role => {
    _assertPayload(_isRecord(role));
    _assertPayload(_isNonEmptyString(role.role_key));
    _assertPayload(_isNonEmptyString(role.name));
    _assertPayload(typeof role.is_system === "boolean");
    _assertPayload(
      Number.isInteger(role.assigned_staff_count)
        && role.assigned_staff_count >= 0,
    );
    _assertPayload(!roleKeys.has(role.role_key));
    roleKeys.add(role.role_key);
    return {
      role_key: role.role_key,
      name: role.name,
      is_system: role.is_system,
      assigned_staff_count: role.assigned_staff_count,
    };
  });

  const permissionKeys = new Set();
  const permissions = data.perms.map(permission => {
    _assertPayload(_isRecord(permission));
    for (const key of [
      "key", "module", "object", "label", "description", "risk", "dependencies",
    ]) _assertPayload(_hasOwn(permission, key));
    _assertPayload(_isNonEmptyString(permission.key));
    _assertPayload(_isNonEmptyString(permission.module));
    _assertPayload(_isNonEmptyString(permission.object));
    _assertPayload(_isNonEmptyString(permission.label));
    _assertPayload(_isNonEmptyString(permission.description));
    _assertPayload(["normal", "sensitive", "high"].includes(permission.risk));
    _assertPayload(Array.isArray(permission.dependencies));
    _assertPayload(!permissionKeys.has(permission.key));
    permissionKeys.add(permission.key);
    const dependencies = permission.dependencies.map(dependency => {
      _assertPayload(_isNonEmptyString(dependency));
      return dependency;
    });
    _assertPayload(new Set(dependencies).size === dependencies.length);
    _assertPayload(!dependencies.includes(permission.key));
    return {
      key: permission.key,
      module: permission.module,
      object: permission.object,
      label: permission.label,
      description: permission.description,
      risk: permission.risk,
      dependencies,
    };
  });
  permissions.forEach(permission => {
    permission.dependencies.forEach(dependency => {
      _assertPayload(permissionKeys.has(dependency));
    });
  });
  _assertDependencyGraphAcyclic(permissions);

  _assertPayload(Object.keys(data.matrix).length === roles.length);
  const baselineByRole = new Map();
  roles.forEach(role => {
    _assertPayload(_hasOwn(data.matrix, role.role_key));
    const row = data.matrix[role.role_key];
    _assertPayload(_isRecord(row));
    _assertPayload(Object.keys(row).length === permissions.length);
    const enabled = new Set();
    permissions.forEach(permission => {
      _assertPayload(_hasOwn(row, permission.key));
      _assertPayload(typeof row[permission.key] === "boolean");
      if (row[permission.key]) enabled.add(permission.key);
    });
    permissions.forEach(permission => {
      if (!enabled.has(permission.key)) return;
      permission.dependencies.forEach(dependency => {
        _assertPayload(enabled.has(dependency));
      });
    });
    baselineByRole.set(role.role_key, enabled);
  });
  Object.keys(data.matrix).forEach(roleKey => _assertPayload(roleKeys.has(roleKey)));

  const capabilityKeys = new Set();
  const systemAdminCapabilities = data.system_admin_capabilities.map(capability => {
    _assertPayload(_isRecord(capability));
    _assertPayload(_isNonEmptyString(capability.key));
    _assertPayload(_isNonEmptyString(capability.label));
    _assertPayload(capability.assignable === false);
    _assertPayload(!capabilityKeys.has(capability.key));
    _assertPayload(!permissionKeys.has(capability.key));
    capabilityKeys.add(capability.key);
    return {
      key: capability.key,
      label: capability.label,
      assignable: false,
    };
  });

  return {roles, permissions, baselineByRole, systemAdminCapabilities};
}

function _cloneRoleSets(source) {
  const result = new Map();
  source.forEach((permissions, roleKey) => {
    result.set(roleKey, new Set(permissions));
  });
  return result;
}

function _resetRolePermissionV3State() {
  _rpV3State.roles = [];
  _rpV3State.permissions = [];
  _rpV3State.permissionByKey = new Map();
  _rpV3State.systemAdminCapabilities = [];
  _rpV3State.selectedRoleKey = "";
  _rpV3State.baselineByRole = new Map();
  _rpV3State.draftByRole = new Map();
  _rpV3State.search = "";
  _rpV3State.view = "all";
  _rpV3State.expanded = new Set();
  _rpV3State.autoAddedDependencies = new Map();
  _rpAutoAddedDependencyGeneration = 0;
  _rpRolePermissionModalSuspended = false;
  _rpV3State.message = "";
  _rpV3State.containerId = "";
  _rpV3State.container = null;
  _rpV3State.loaded = false;
}

function _installRolePermissionV3State(normalized, containerId, container) {
  _rpV3State.roles = normalized.roles;
  _rpV3State.permissions = normalized.permissions;
  _rpV3State.permissionByKey = new Map(
    normalized.permissions.map(permission => [permission.key, permission]),
  );
  _rpV3State.systemAdminCapabilities = normalized.systemAdminCapabilities;
  _rpV3State.selectedRoleKey = normalized.roles[0].role_key;
  _rpV3State.baselineByRole = _cloneRoleSets(normalized.baselineByRole);
  _rpV3State.draftByRole = _cloneRoleSets(normalized.baselineByRole);
  _rpV3State.search = "";
  _rpV3State.view = "all";
  _rpV3State.expanded = new Set([
    roleTreeExpandedKey("module", normalized.permissions[0].module),
  ]);
  _rpV3State.autoAddedDependencies = new Map();
  _rpV3State.message = "";
  _rpV3State.containerId = containerId;
  _rpV3State.container = container;
  _rpV3State.loaded = true;
}

function _readRolePermissionSnapshot() {
  const snapshot = new Map();
  document.querySelectorAll(".rp-table input[type=checkbox][data-role]").forEach(cb => {
    const role = String(cb.dataset.role || "");
    const permission = String(cb.dataset.perm || "");
    if (!role || !permission) return;
    if (!snapshot.has(role)) snapshot.set(role, new Map());
    snapshot.get(role).set(permission, Boolean(cb.checked));
  });
  return snapshot;
}

function _captureRolePermissionBaseline() {
  _rpBaselineByRole = _readRolePermissionSnapshot();
}

function _replaceRolePermissionBaseline(snapshot) {
  _rpBaselineByRole = new Map();
  snapshot.forEach((permissionsForRole, role) => {
    _rpBaselineByRole.set(role, new Map(permissionsForRole));
  });
}

function _setRolePermissionSaveInFlight(inFlight) {
  _rpSaveInFlight = inFlight === true;
  document.querySelectorAll(
    ".rp-save, [data-rp-save-selected], [data-rp-cancel-save]",
  ).forEach(button => {
    const isPreviewButton = button.dataset
      && Object.prototype.hasOwnProperty.call(button.dataset, "rpSaveSelected");
    const diff = isPreviewButton && window.__accessV3 === true
      ? rolePermissionDiff()
      : null;
    const hasChanges = diff
      ? diff.added.length > 0 || diff.removed.length > 0
      : true;
    button.disabled = _rpSaveInFlight || !hasChanges;
  });
}

function _rolePermissionModalElements() {
  const container = _rpV3State.container;
  return {
    panel: document.getElementById("rpV3Confirm"),
    dialog: document.getElementById("rpSaveDialog"),
    background: container
      ? container.querySelector("[data-rp-modal-background]")
      : null,
  };
}

function _rolePermissionModalFocusableElements() {
  const {panel} = _rolePermissionModalElements();
  if (!panel) return [];
  return [
    document.getElementById("rpCurrentPassword"),
    panel.querySelector("[data-rp-confirm-password]"),
    document.getElementById("rpReason"),
    panel.querySelector("[data-rp-confirm-save]"),
    panel.querySelector("[data-rp-cancel-save]"),
  ].filter(element => (
    element
    && element.isConnected !== false
    && !element.disabled
    && !element.hidden
    && !element.closest("[hidden]")
  ));
}

function _rolePermissionModalOpen() {
  const {panel} = _rolePermissionModalElements();
  return Boolean(panel && !panel.hidden && _rpPendingRole !== null);
}

function _rolePermissionModalActive() {
  return !_rpRolePermissionModalSuspended && _rolePermissionModalOpen();
}

function _focusRolePermissionModal() {
  const first = _rolePermissionModalFocusableElements()[0];
  if (!first) return;
  first.focus({preventScroll: true});
}

function _onRolePermissionModalKeydown(event) {
  if (!_rolePermissionModalActive()) return;
  if (event.key === "Escape") {
    if (_rpSaveInFlight) return;
    event.preventDefault();
    cancelRolePermSave();
    return;
  }
  if (event.key !== "Tab") return;
  const focusable = _rolePermissionModalFocusableElements();
  if (!focusable.length) {
    event.preventDefault();
    return;
  }
  const {panel} = _rolePermissionModalElements();
  const first = focusable[0];
  const last = focusable[focusable.length - 1];
  const active = document.activeElement;
  if (event.shiftKey && (active === first || !panel.contains(active))) {
    event.preventDefault();
    last.focus({preventScroll: true});
  } else if (!event.shiftKey && (active === last || !panel.contains(active))) {
    event.preventDefault();
    first.focus({preventScroll: true});
  }
}

function _onRolePermissionModalFocusIn(event) {
  if (!_rolePermissionModalActive()) return;
  const {panel} = _rolePermissionModalElements();
  if (panel.contains(event.target)) return;
  _focusRolePermissionModal();
}

function _syncRolePermissionModal({focus = false} = {}) {
  const {panel, dialog, background} = _rolePermissionModalElements();
  if (!panel || !background) return;
  const open = _rpPendingRole !== null && _rpV3State.view !== "scope";
  const active = open && !_rpRolePermissionModalSuspended;
  const suspended = open && _rpRolePermissionModalSuspended;
  panel.hidden = !open;
  if (dialog) {
    dialog.hidden = !open;
    dialog.inert = suspended;
    if (suspended) dialog.setAttribute("inert", "");
    else dialog.removeAttribute("inert");
    dialog.setAttribute("aria-hidden", active ? "false" : "true");
  }
  background.inert = open;
  if (open) {
    background.setAttribute("inert", "");
    background.setAttribute("aria-hidden", "true");
  } else {
    background.removeAttribute("inert");
    background.removeAttribute("aria-hidden");
  }
  if (active) {
    document.addEventListener("keydown", _onRolePermissionModalKeydown);
    document.addEventListener("focusin", _onRolePermissionModalFocusIn);
    if (focus) _focusRolePermissionModal();
  } else {
    document.removeEventListener("keydown", _onRolePermissionModalKeydown);
    document.removeEventListener("focusin", _onRolePermissionModalFocusIn);
  }
}

function _restoreRolePermissionPreviewFocus() {
  const preview = _rpV3State.container
    ? _rpV3State.container.querySelector("[data-rp-save-selected]")
    : null;
  if (preview && !preview.disabled) preview.focus({preventScroll: true});
}

function _restoreRolePermissionSaveSuccessFocus() {
  const container = _rpV3State.container;
  if (!container) return;
  const preview = container.querySelector("[data-rp-save-selected]");
  if (preview && !preview.disabled) {
    preview.focus({preventScroll: true});
    return;
  }
  const selectedRole = Array.from(
    container.querySelectorAll("[data-rp-select-role]"),
  ).find(button => (
    button.dataset.rpSelectRole === _rpV3State.selectedRoleKey
  ));
  if (selectedRole) selectedRole.focus({preventScroll: true});
}

function _syncRolePermissionFlowMessage() {
  const message = document.getElementById("rpMsg");
  if (!message) return;
  const belongsToSelectedRole = _rpMessageRecord.kind !== "terminal"
    || _rpMessageRecord.roleKey === _rpV3State.selectedRoleKey;
  message.textContent = belongsToSelectedRole ? _rpMessageRecord.text : "";
  message.hidden = window.__accessV3 === true && _rpV3State.view === "scope";
}

function _setRolePermissionFlowMessage(value, {
  kind = "active",
  roleKey = _rpPendingRole || _rpV3State.selectedRoleKey,
} = {}) {
  const text = String(value || "");
  _rpMessageRecord = {
    text,
    kind: kind === "terminal" ? "terminal" : "active",
    roleKey: text ? String(roleKey || "") : "",
  };
  _syncRolePermissionFlowMessage();
}

function _clearRolePermissionTerminalMessage() {
  if (_rpMessageRecord.kind === "terminal") {
    _setRolePermissionFlowMessage("");
  }
}

function _captureRolePermissionFlow(roleKey) {
  return {
    revision: _rpFlowRevision,
    roleKey,
    containerId: _rpV3State.containerId,
    container: _rpV3State.container,
  };
}

function _isCurrentRolePermissionFlow(flow) {
  return window.__accessV3 === true
    && flow.revision === _rpFlowRevision
    && flow.roleKey === _rpPendingRole
    && _rpV3State.loaded
    && _rpV3State.selectedRoleKey === flow.roleKey
    && _rpV3State.containerId === flow.containerId
    && _rpV3State.container === flow.container
    && document.getElementById(flow.containerId) === flow.container;
}

function _legacyRolePermissionSnapshotsEqual(left, right) {
  if (left.size !== right.size) return false;
  for (const [role, permissionsForRole] of left) {
    const otherPermissions = right.get(role);
    if (!otherPermissions || otherPermissions.size !== permissionsForRole.size) return false;
    for (const [permission, enabled] of permissionsForRole) {
      if (!otherPermissions.has(permission) || otherPermissions.get(permission) !== enabled) {
        return false;
      }
    }
  }
  return true;
}

function _setsEqual(left, right) {
  if (!left || !right || left.size !== right.size) return false;
  for (const value of left) if (!right.has(value)) return false;
  return true;
}

function rolePermissionDiff(roleKey = _rpV3State.selectedRoleKey) {
  const role = String(roleKey || "");
  const baseline = _rpV3State.baselineByRole.get(role) || new Set();
  const draft = _rpV3State.draftByRole.get(role) || new Set();
  const added = [];
  const removed = [];
  const autoAddedDependencies = [];
  _rpV3State.permissions.forEach(permission => {
    const inBaseline = baseline.has(permission.key);
    const inDraft = draft.has(permission.key);
    if (inDraft && !inBaseline) added.push(permission.key);
    if (inBaseline && !inDraft) removed.push(permission.key);
    if (
      role === _rpV3State.selectedRoleKey
      && inDraft
      && !inBaseline
      && _rpV3State.autoAddedDependencies.has(permission.key)
    ) autoAddedDependencies.push(permission.key);
  });
  return {added, removed, autoAddedDependencies};
}

function _rolePermissionsDirty() {
  if (_rpPendingRole !== null) return true;
  if (window.__accessV3 === true) {
    if (!_rpV3State.loaded) return false;
    const role = _rpV3State.selectedRoleKey;
    return !_setsEqual(
      _rpV3State.baselineByRole.get(role),
      _rpV3State.draftByRole.get(role),
    );
  }
  if (_rpBaselineByRole === null) return false;
  return !_legacyRolePermissionSnapshotsEqual(
    _rpBaselineByRole,
    _readRolePermissionSnapshot(),
  );
}

function _restoreLegacyRolePermissionBaseline() {
  if (_rpBaselineByRole === null) return;
  document.querySelectorAll(".rp-table input[type=checkbox][data-role]").forEach(cb => {
    const role = String(cb.dataset.role || "");
    const permission = String(cb.dataset.perm || "");
    const roleBaseline = _rpBaselineByRole.get(role);
    if (roleBaseline && roleBaseline.has(permission)) {
      cb.checked = roleBaseline.get(permission);
    }
  });
}

function restoreRoleBaseline() {
  if (window.__accessV3 !== true) {
    _restoreLegacyRolePermissionBaseline();
    return;
  }
  if (!_rpV3State.loaded) return;
  const roleKey = _rpV3State.selectedRoleKey;
  const baseline = _rpV3State.baselineByRole.get(roleKey);
  const current = _rpV3State.draftByRole.get(roleKey);
  if (baseline) {
    _rpV3State.draftByRole.set(roleKey, new Set(baseline));
    if (!_setsEqual(current, baseline)) _clearRolePermissionTerminalMessage();
  }
  _rpV3State.autoAddedDependencies = new Map();
  _rpV3State.message = "";
  renderRolePermissionCenter();
}

function _showRolePermissionLeaveDialog() {
  return new Promise(resolve => {
    let overlay = null;
    let onKeydown = null;
    let onFocusIn = null;
    let suspendedRolePermissionModal = false;
    let settled = false;
    const cleanup = () => {
      if (onKeydown) document.removeEventListener("keydown", onKeydown);
      if (onFocusIn) document.removeEventListener("focusin", onFocusIn);
      if (overlay) overlay.remove();
    };
    const resumeRolePermissionModal = focus => {
      if (!suspendedRolePermissionModal) return;
      suspendedRolePermissionModal = false;
      _rpRolePermissionModalSuspended = false;
      _syncRolePermissionModal({focus});
    };
    const settle = allowed => {
      if (settled) return;
      settled = true;
      let result = allowed === true;
      try {
        if (result) {
          _rpRenderRevision += 1;
          restoreRoleBaseline();
          cancelRolePermSave();
        }
      } catch {
        result = false;
      }
      cleanup();
      resumeRolePermissionModal(!result);
      resolve(result);
    };
    try {
      if (_rolePermissionModalOpen()) {
        suspendedRolePermissionModal = true;
        _rpRolePermissionModalSuspended = true;
        _syncRolePermissionModal();
      }
      overlay = document.createElement("div");
      overlay.className = "modal-backdrop";
      overlay.innerHTML = `
        <section class="appt-modal" role="dialog" aria-modal="true" aria-labelledby="rpLeaveTitle">
          <div class="modal-head"><strong id="rpLeaveTitle">离开角色与权限？</strong></div>
          <div class="appt-body">当前有未保存的权限修改或未完成的确认流程。确认离开将放弃这些更改。</div>
          <div class="modal-actions">
            <button type="button" class="plain-button" data-rp-leave="cancel">继续编辑</button>
            <button type="button" class="tooth-confirm-btn" data-rp-leave="confirm">放弃更改并离开</button>
          </div>
        </section>`;
      const cancelButton = overlay.querySelector('[data-rp-leave="cancel"]');
      const confirmButton = overlay.querySelector('[data-rp-leave="confirm"]');
      if (!cancelButton || !confirmButton || !document.body) {
        throw new Error("leave dialog unavailable");
      }
      cancelButton.addEventListener("click", () => settle(false));
      confirmButton.addEventListener("click", () => settle(true));
      onKeydown = event => {
        if (event.key === "Escape") {
          event.preventDefault();
          settle(false);
          return;
        }
        if (event.key !== "Tab") return;
        const active = document.activeElement;
        if (event.shiftKey && (active === cancelButton || !overlay.contains(active))) {
          event.preventDefault();
          confirmButton.focus({preventScroll: true});
        } else if (!event.shiftKey && (
          active === confirmButton || !overlay.contains(active)
        )) {
          event.preventDefault();
          cancelButton.focus({preventScroll: true});
        }
      };
      onFocusIn = event => {
        if (overlay.contains(event.target)) return;
        cancelButton.focus({preventScroll: true});
      };
      document.addEventListener("keydown", onKeydown);
      document.addEventListener("focusin", onFocusIn);
      document.body.appendChild(overlay);
      cancelButton.focus({preventScroll: true});
    } catch {
      cleanup();
      resumeRolePermissionModal(true);
      settled = true;
      resolve(false);
    }
  });
}

function requestLeaveRolePermissions() {
  if (_rpLeavePromise) return _rpLeavePromise;
  if (_rpSaveInFlight) return Promise.resolve(false);
  if (!_rolePermissionsDirty()) {
    _rpRenderRevision += 1;
    return Promise.resolve(true);
  }
  _rpLeavePromise = _showRolePermissionLeaveDialog()
    .then(result => result === true, () => false)
    .finally(() => { _rpLeavePromise = null; });
  return _rpLeavePromise;
}

function roleTreeExpandedKey(level, moduleName, objectName = "") {
  const modulePart = String(moduleName || "");
  if (level === "module") return `module:${modulePart}`;
  if (level === "object") return `object:${modulePart}:${String(objectName || "")}`;
  throw new Error("invalid role tree level");
}

function toggleRoleTreeSection(level, moduleName, objectName = "") {
  const key = roleTreeExpandedKey(level, moduleName, objectName);
  const expanded = !_rpV3State.expanded.has(key);
  if (expanded) _rpV3State.expanded.add(key);
  else _rpV3State.expanded.delete(key);
  renderRolePermissionCenter();
  return expanded;
}

function _currentDraft() {
  return _rpV3State.draftByRole.get(_rpV3State.selectedRoleKey) || null;
}

function _permissionDisplayName(permission) {
  return `${permission.module} / ${permission.object} / ${permission.label}`;
}

function _permissionDisplayNames(keys) {
  return keys.map(key => _rpV3State.permissionByKey.get(key))
    .filter(Boolean)
    .map(_permissionDisplayName);
}

function _rolePermissionImpactList(label, keys) {
  const names = _permissionDisplayNames(keys);
  const content = names.length
    ? `<ul>${names.map(name => `<li>${_rpEscape(name)}</li>`).join("")}</ul>`
    : '<span class="cfg-hint">无</span>';
  return `<div><strong>${_rpEscape(label)}：</strong>${content}</div>`;
}

function _rolePermissionImpactMarkup(diff = rolePermissionDiff()) {
  const selectedRole = _rpV3State.roles.find(
    role => role.role_key === _rpV3State.selectedRoleKey,
  );
  const affectedStaffCount = selectedRole ? selectedRole.assigned_staff_count : 0;
  return `
    <p>影响人数：${affectedStaffCount} 人</p>
    <p>新增：${diff.added.length} 项</p>
    <p>移除：${diff.removed.length} 项</p>
    ${_rolePermissionImpactList("新增权限", diff.added)}
    ${_rolePermissionImpactList("移除权限", diff.removed)}
    ${_rolePermissionImpactList("自动补齐依赖", diff.autoAddedDependencies)}`;
}

function _syncRolePermissionSavePreview(markup = _rolePermissionImpactMarkup()) {
  const preview = document.getElementById("rpSavePreview");
  if (preview) preview.innerHTML = markup;
}

function _addPermissionAndDependencies(key, draft, autoAdded, rootKey) {
  const permission = _rpV3State.permissionByKey.get(key);
  if (!permission) return false;
  permission.dependencies.forEach(dependency => {
    if (!draft.has(dependency)) {
      _addPermissionAndDependencies(dependency, draft, autoAdded, rootKey);
      _rpAutoAddedDependencyGeneration += 1;
      autoAdded.set(dependency, _rpAutoAddedDependencyGeneration);
    }
  });
  draft.add(key);
  if (key === rootKey) autoAdded.delete(key);
  return true;
}

function _enabledExternalDependents(draft, removing) {
  return _rpV3State.permissions.filter(permission => (
    draft.has(permission.key)
    && !removing.has(permission.key)
    && permission.dependencies.some(dependency => removing.has(dependency))
  ));
}

function togglePermissionSubtree(keys, checked) {
  if (!_rpV3State.loaded || !Array.isArray(keys)) return false;
  const uniqueKeys = [...new Set(keys.map(key => String(key || "")))];
  if (
    uniqueKeys.length === 0
    || uniqueKeys.some(key => !_rpV3State.permissionByKey.has(key))
  ) return false;
  const current = _currentDraft();
  if (!current) return false;
  const next = new Set(current);
  const nextAutoAdded = new Map(_rpV3State.autoAddedDependencies);
  let autoAddedThisAction = [];

  if (checked === true) {
    uniqueKeys.forEach(key => {
      _addPermissionAndDependencies(key, next, nextAutoAdded, key);
    });
    const directKeys = new Set(uniqueKeys);
    autoAddedThisAction = _rpV3State.permissions.filter(permission => (
      !directKeys.has(permission.key)
      && !current.has(permission.key)
      && next.has(permission.key)
    ));
  } else {
    const removing = new Set(uniqueKeys.filter(key => next.has(key)));
    const blockers = _enabledExternalDependents(next, removing);
    if (blockers.length) {
      _rpV3State.message = `无法取消：${blockers.map(_permissionDisplayName).join("、")} 仍依赖所选权限`;
      renderRolePermissionCenter();
      return false;
    }
    removing.forEach(key => {
      next.delete(key);
      nextAutoAdded.delete(key);
    });
  }

  const changed = !_setsEqual(current, next);
  _rpV3State.draftByRole.set(_rpV3State.selectedRoleKey, next);
  _rpV3State.autoAddedDependencies = nextAutoAdded;
  _rpV3State.message = autoAddedThisAction.length
    ? `已自动补齐：${autoAddedThisAction.map(_permissionDisplayName).join("、")}`
    : "";
  if (changed) _clearRolePermissionTerminalMessage();
  renderRolePermissionCenter();
  return true;
}

function togglePermission(permissionKey, checked) {
  return togglePermissionSubtree([permissionKey], checked);
}

function setRolePermissionSearch(search) {
  _rpV3State.search = String(search || "");
  renderRolePermissionCenter();
}

function _setRolePermissionSearchFromInput(input, event) {
  const value = String(input.value || "");
  _rpV3State.search = value;
  if (event && event.isComposing === true) return;
  const selectionStart = Number.isInteger(input.selectionStart)
    ? input.selectionStart
    : value.length;
  const selectionEnd = Number.isInteger(input.selectionEnd)
    ? input.selectionEnd
    : selectionStart;
  renderRolePermissionCenter();
  const replacement = _rpV3State.container
    ? _rpV3State.container.querySelector("[data-rp-search]")
    : null;
  if (!replacement) return;
  replacement.focus({preventScroll: true});
  if (typeof replacement.setSelectionRange === "function") {
    replacement.setSelectionRange(selectionStart, selectionEnd);
  }
}

function setRolePermissionView(view) {
  const nextView = String(view || "");
  if (!new Set(["all", "sensitive", "scope"]).has(nextView)) return false;
  _rpV3State.view = nextView;
  _rpV3State.message = "";
  renderRolePermissionCenter();
  return true;
}

async function selectRole(roleKey) {
  const nextRoleKey = String(roleKey || "");
  if (!_rpV3State.roles.some(role => role.role_key === nextRoleKey)) return false;
  if (nextRoleKey === _rpV3State.selectedRoleKey) return true;
  if (_rpSaveInFlight) return false;
  if (_rolePermissionsDirty()) {
    const allowed = await requestLeaveRolePermissions();
    if (!allowed) return false;
  }
  _clearRolePermissionTerminalMessage();
  _rpV3State.selectedRoleKey = nextRoleKey;
  _rpV3State.autoAddedDependencies = new Map();
  _rpV3State.message = "";
  renderRolePermissionCenter();
  return true;
}

function _visiblePermissions() {
  let items = _rpV3State.permissions;
  if (_rpV3State.view === "sensitive") {
    items = items.filter(permission => permission.risk !== "normal");
  }
  const query = _rpV3State.search.trim().toLocaleLowerCase();
  if (!query) return items;
  return items.filter(permission => [
    permission.key,
    permission.module,
    permission.object,
    permission.label,
    permission.description,
  ].some(value => value.toLocaleLowerCase().includes(query)));
}

function _groupVisiblePermissions(items) {
  const modules = [];
  const moduleByName = new Map();
  items.forEach(permission => {
    if (!moduleByName.has(permission.module)) {
      const moduleGroup = {name: permission.module, objects: [], objectByName: new Map()};
      moduleByName.set(permission.module, moduleGroup);
      modules.push(moduleGroup);
    }
    const moduleGroup = moduleByName.get(permission.module);
    if (!moduleGroup.objectByName.has(permission.object)) {
      const objectGroup = {name: permission.object, permissions: []};
      moduleGroup.objectByName.set(permission.object, objectGroup);
      moduleGroup.objects.push(objectGroup);
    }
    moduleGroup.objectByName.get(permission.object).permissions.push(permission);
  });
  return modules;
}

function _subtreeCheckboxAttributes(keys, draft) {
  const checkedCount = keys.filter(key => draft.has(key)).length;
  return {
    checked: checkedCount === keys.length,
    indeterminate: checkedCount > 0 && checkedCount < keys.length,
  };
}

function _rolePermissionTreeFiltering() {
  return _rpV3State.view === "sensitive" || _rpV3State.search.trim().length > 0;
}

function _renderPermissionLeaf(permission, draft) {
  const riskLabel = permission.risk === "high"
    ? '<span class="rp-risk-tag rp-risk-high access-role-risk-tag access-role-risk-high">高风险</span>'
    : (permission.risk === "sensitive"
      ? '<span class="rp-risk-tag rp-risk-sensitive access-role-risk-tag access-role-risk-sensitive">敏感</span>'
      : "");
  return `
    <div class="rp-tnode rp-tnode-leaf access-role-tnode access-role-tnode-leaf">
      <label class="rp-trow rp-trow-leaf access-role-trow access-role-trow-leaf">
        <input type="checkbox" data-rp-permission-key="${_rpEscape(permission.key)}"${draft.has(permission.key) ? " checked" : ""}>
        <span class="rp-tleaf-label access-role-tleaf-label">${_rpEscape(permission.label)}</span>
        ${riskLabel}
        <span class="rp-tleaf-desc cfg-hint access-role-tleaf-desc">${_rpEscape(permission.description)}</span>
      </label>
    </div>`;
}

function _renderRoleTreeParentRow({level, name, expandedKey, moduleName, objectName, keys, draft}) {
  const expanded = _rolePermissionTreeFiltering() || _rpV3State.expanded.has(expandedKey);
  const check = _subtreeCheckboxAttributes(keys, draft);
  const checkedCount = keys.filter(key => draft.has(key)).length;
  return {
    expanded,
    row: `
      <div class="rp-trow rp-trow-${level} access-role-trow access-role-trow-${level}">
        <button type="button" class="plain-button rp-tarrow access-role-tarrow" data-rp-section-toggle
          data-rp-level="${level}" data-rp-module="${_rpEscape(moduleName)}"
          data-rp-object="${_rpEscape(objectName)}" data-rp-expanded-key="${_rpEscape(expandedKey)}" aria-expanded="${expanded}"
          aria-label="${expanded ? "折叠" : "展开"}${_rpEscape(name)}">${expanded ? "▾" : "▸"}</button>
        <label class="rp-tcheck access-role-tcheck"><input type="checkbox" data-rp-subtree-level="${level}"
          data-rp-module="${_rpEscape(moduleName)}" data-rp-object="${_rpEscape(objectName)}"${check.checked ? " checked" : ""}></label>
        <span class="rp-tname access-role-tname">${_rpEscape(name)}</span>
        <span class="rp-tcount cfg-hint access-role-tcount">${checkedCount}/${keys.length}</span>
      </div>`,
  };
}

function _renderRolePermissionTree() {
  const draft = _currentDraft() || new Set();
  const visible = _visiblePermissions();
  if (!visible.length) return '<div class="cfg-hint">没有匹配的权限</div>';

  const modules = _groupVisiblePermissions(visible).map(moduleGroup => {
    const moduleKey = roleTreeExpandedKey("module", moduleGroup.name);
    const moduleKeys = moduleGroup.objects.flatMap(group => (
      group.permissions.map(permission => permission.key)
    ));
    const moduleRow = _renderRoleTreeParentRow({
      level: "module",
      name: moduleGroup.name,
      expandedKey: moduleKey,
      moduleName: moduleGroup.name,
      objectName: "",
      keys: moduleKeys,
      draft,
    });
    const objects = moduleGroup.objects.map(objectGroup => {
      const objectKey = roleTreeExpandedKey("object", moduleGroup.name, objectGroup.name);
      const objectKeys = objectGroup.permissions.map(permission => permission.key);
      const objectRow = _renderRoleTreeParentRow({
        level: "object",
        name: objectGroup.name,
        expandedKey: objectKey,
        moduleName: moduleGroup.name,
        objectName: objectGroup.name,
        keys: objectKeys,
        draft,
      });
      const leaves = objectGroup.permissions.map(permission => (
        _renderPermissionLeaf(permission, draft)
      )).join("");
      return `
        <div class="rp-tnode rp-tnode-object access-role-tnode access-role-tnode-object"
          data-rp-object-group="${_rpEscape(objectKey)}">
          ${objectRow.row}
          <div class="rp-tkids access-role-tkids" data-rp-kids="${_rpEscape(objectKey)}"${objectRow.expanded ? "" : " hidden"}>${leaves}</div>
        </div>`;
    }).join("");
    return `
      <div class="rp-tnode rp-tnode-module access-role-tnode access-role-tnode-module"
        data-rp-module-group="${_rpEscape(moduleKey)}">
        ${moduleRow.row}
        <div class="rp-tkids access-role-tkids" data-rp-kids="${_rpEscape(moduleKey)}"${moduleRow.expanded ? "" : " hidden"}>${objects}</div>
      </div>`;
  }).join("");
  return `<div class="rp-tree access-role-tree-root">${modules}</div>`;
}

function _renderRolePermissionSaveControls(dirty) {
  if (_rpV3State.view === "scope") return "";
  return `
    <div class="rp-v3-actions access-role-action-buttons">
      <button type="button" class="rp-save access-role-save" data-rp-save-selected data-access-save${dirty ? "" : " disabled"}>预览影响并保存</button>
      <button type="button" class="plain-button" data-rp-restore${dirty ? "" : " disabled"}>撤销</button>
    </div>`;
}

function _ensureRolePermissionV3Shell(box) {
  if (box.querySelector('[data-rp-region="roles"]')) return true;
  box.innerHTML = `
    <div class="rp-v3-center access-role-center">
      <div data-rp-modal-background>
        <div class="rp-v3-layout access-role-layout">
          <aside class="rp-v3-roles access-role-roles">
            <strong>岗位</strong>
            <div data-rp-region="roles"></div>
          </aside>
          <section class="rp-v3-main access-role-main" data-rp-main>
            <div class="rp-v3-toolbar access-role-toolbar">
              <div data-rp-region="views"></div>
              <label>搜索 <span data-rp-region="search"></span></label>
            </div>
            <div class="rp-v3-role-status access-role-status">
              <strong data-rp-region="selected-role"></strong>
              <span data-rp-region="authorized-count"></span>
              <span data-rp-region="save-status"></span>
              <span data-rp-region="feedback" class="cfg-hint"></span>
              <span id="rpMsg" class="cfg-hint"></span>
            </div>
            <div class="rp-v3-tree access-role-tree" data-rp-region="tree"></div>
          </section>
          <aside class="rp-v3-impact access-role-impact" data-rp-impact-panel>
            <strong>变更影响</strong>
            <div data-rp-region="impact"></div>
            <div class="access-role-actions" data-rp-region="actions"></div>
          </aside>
        </div>
        <section class="rp-v3-system-capabilities access-role-system-capabilities" data-rp-system-capabilities>
          <strong>系统管理员能力</strong>
          <ul data-rp-region="capabilities"></ul>
        </section>
      </div>
      <div id="rpV3Confirm" class="modal-backdrop" hidden>
        <section id="rpSaveDialog" class="appt-modal" role="dialog" aria-modal="true"
          aria-labelledby="rpSaveTitle">
          <div class="modal-head"><strong id="rpSaveTitle">预览权限变更影响</strong></div>
          <div class="appt-body">
            <div id="rpSavePreview"></div>
            <div id="rpPasswordStep">
              <label>当前密码 <input id="rpCurrentPassword" name="current-password" type="password"
                autocomplete="current-password" class="ord-input"></label>
              <button type="button" class="rp-save" data-rp-confirm-password>确认密码</button>
            </div>
            <div id="rpReasonStep" hidden>
              <label>修改原因 <textarea id="rpReason" class="ord-input" rows="2"></textarea></label>
              <button type="button" class="rp-save" data-rp-confirm-save>确认保存</button>
            </div>
            <div class="modal-actions">
              <button type="button" class="plain-button" data-rp-cancel-save>取消</button>
            </div>
          </div>
        </section>
      </div>
    </div>`;
  _bindRolePermissionStableEvents(box);
  return Boolean(box.querySelector('[data-rp-region="roles"]'));
}

function _rolePermissionRegion(box, name) {
  return box.querySelector(`[data-rp-region="${name}"]`);
}

function _bindRolePermissionStableEvents(box) {
  box.querySelectorAll("[data-rp-confirm-password]").forEach(button => {
    button.addEventListener("click", confirmRolePermPassword);
  });
  box.querySelectorAll("[data-rp-confirm-save]").forEach(button => {
    button.addEventListener("click", saveRolePerms);
  });
  box.querySelectorAll("[data-rp-cancel-save]").forEach(button => {
    button.addEventListener("click", cancelRolePermSave);
  });
}

function _rolePermissionSectionKeys(level, moduleName, objectName) {
  return _visiblePermissions()
    .filter(permission => (
      permission.module === moduleName
      && (level === "module" || permission.object === objectName)
    ))
    .map(permission => permission.key);
}

function _bindRolePermissionV3Events(box) {
  box.querySelectorAll("[data-rp-select-role]").forEach(button => {
    button.addEventListener("click", () => { selectRole(button.dataset.rpSelectRole); });
  });
  box.querySelectorAll("[data-rp-view]").forEach(button => {
    button.addEventListener("click", () => { setRolePermissionView(button.dataset.rpView); });
  });
  box.querySelectorAll("[data-rp-search]").forEach(input => {
    input.addEventListener("input", event => {
      _setRolePermissionSearchFromInput(input, event);
    });
  });
  box.querySelectorAll("[data-rp-section-toggle]").forEach(button => {
    button.addEventListener("click", () => {
      toggleRoleTreeSection(
        button.dataset.rpLevel,
        button.dataset.rpModule,
        button.dataset.rpObject || "",
      );
    });
  });
  box.querySelectorAll("input[data-rp-permission-key]").forEach(input => {
    input.addEventListener("change", () => {
      togglePermission(input.dataset.rpPermissionKey, input.checked);
    });
  });
  box.querySelectorAll("input[data-rp-subtree-level]").forEach(input => {
    const keys = _rolePermissionSectionKeys(
      input.dataset.rpSubtreeLevel,
      input.dataset.rpModule,
      input.dataset.rpObject || "",
    );
    const check = _subtreeCheckboxAttributes(keys, _currentDraft() || new Set());
    input.indeterminate = check.indeterminate;
    input.addEventListener("change", () => {
      togglePermissionSubtree(keys, input.checked);
    });
  });
  box.querySelectorAll("[data-rp-save-selected]").forEach(button => {
    button.addEventListener("click", () => beginRoleSavePreview(
      _rpV3State.selectedRoleKey,
    ));
  });
  box.querySelectorAll("[data-rp-restore]").forEach(button => {
    button.addEventListener("click", restoreRoleBaseline);
  });
}

function renderRolePermissionCenter() {
  if (!_rpV3State.loaded) return;
  const box = _rpV3State.container;
  if (!box || document.getElementById(_rpV3State.containerId) !== box) return;
  const selectedRole = _rpV3State.roles.find(
    role => role.role_key === _rpV3State.selectedRoleKey,
  );
  if (!selectedRole) return;
  const diff = rolePermissionDiff();
  const dirty = diff.added.length > 0 || diff.removed.length > 0;
  const impactMarkup = _rolePermissionImpactMarkup(diff);
  const rolesHtml = _rpV3State.roles.map(role => `
    <button type="button" class="plain-button rp-v3-role"
      data-rp-select-role="${_rpEscape(role.role_key)}"
      aria-pressed="${role.role_key === _rpV3State.selectedRoleKey}">
      <span>${_rpEscape(role.name)}</span>
      <span class="cfg-hint">已分配 ${role.assigned_staff_count} 人</span>
    </button>`).join("");
  const views = [
    ["all", "全部权限"],
    ["sensitive", "敏感操作"],
    ["scope", "数据范围"],
  ].map(([key, label]) => `
    <button type="button" class="plain-button" data-rp-view="${key}"
      aria-pressed="${_rpV3State.view === key}">${label}</button>`).join("");
  const mainContent = _rpV3State.view === "scope"
    ? '<div class="cfg-hint" data-rp-data-scope>底层执行引擎尚未启用</div>'
    : _renderRolePermissionTree();
  const capabilities = _rpV3State.systemAdminCapabilities.map(capability => `
    <li><span>${_rpEscape(capability.label)}</span>
      <span class="cfg-hint">系统管理员专属、不能分配给岗位</span></li>`).join("");
  if (!_ensureRolePermissionV3Shell(box)) return;
  const regions = {
    roles: _rolePermissionRegion(box, "roles"),
    views: _rolePermissionRegion(box, "views"),
    search: _rolePermissionRegion(box, "search"),
    selectedRole: _rolePermissionRegion(box, "selected-role"),
    authorizedCount: _rolePermissionRegion(box, "authorized-count"),
    saveStatus: _rolePermissionRegion(box, "save-status"),
    feedback: _rolePermissionRegion(box, "feedback"),
    actions: _rolePermissionRegion(box, "actions"),
    impact: _rolePermissionRegion(box, "impact"),
    tree: _rolePermissionRegion(box, "tree"),
    capabilities: _rolePermissionRegion(box, "capabilities"),
  };
  if (Object.values(regions).some(region => !region)) return;
  regions.roles.innerHTML = rolesHtml;
  regions.views.innerHTML = views;
  regions.search.innerHTML = `<input type="search" class="ord-input" data-rp-search
    value="${_rpEscape(_rpV3State.search)}" placeholder="搜索中文名称或权限键">`;
  regions.selectedRole.textContent = selectedRole.name;
  const baselineCount = _rpV3State.baselineByRole.get(selectedRole.role_key).size;
  const draftCount = _currentDraft().size;
  regions.authorizedCount.textContent = `已授权 ${baselineCount} 项${
    dirty ? `，修改后 ${draftCount} 项` : ""
  }`;
  regions.saveStatus.textContent = dirty ? "未保存" : "当前已保存";
  regions.feedback.textContent = _rpV3State.message;
  regions.actions.innerHTML = _renderRolePermissionSaveControls(dirty);
  regions.impact.innerHTML = impactMarkup;
  regions.tree.innerHTML = mainContent;
  regions.capabilities.innerHTML = capabilities;
  _syncRolePermissionModal();
  if (_rpPendingRole !== null) _syncRolePermissionSavePreview(impactMarkup);
  _syncRolePermissionFlowMessage();
  _bindRolePermissionV3Events(box);
  _setRolePermissionSaveInFlight(_rpSaveInFlight);
}

function _renderLegacyRolePermissions(box, data) {
  const {roles, perms, matrix} = data;
  const groups = [];
  const seen = new Map();
  perms.forEach(permission => {
    if (!seen.has(permission.module)) {
      seen.set(permission.module, groups.length);
      groups.push({module: permission.module, items: []});
    }
    groups[seen.get(permission.module)].items.push(permission);
  });

  let html = '<div class="rp-toolbar"><button type="button" class="rp-save" onclick="saveRolePerms()">保存</button>';
  html += '<span id="rpMsg" class="cfg-hint"></span></div>';
  html += '<div class="rp-wrap"><table class="rp-table"><thead><tr><th>权限点</th>';
  roles.forEach(role => { html += `<th>${_rpEscape(role.name)}</th>`; });
  html += '</tr></thead><tbody>';
  groups.forEach(group => {
    html += `<tr class="rp-group"><td colspan="${roles.length + 1}">${_rpEscape(group.module)}</td></tr>`;
    group.items.forEach(permission => {
      html += `<tr><td class="rp-perm" title="${_rpEscape(permission.key)}">${_rpEscape(permission.label)}</td>`;
      roles.forEach(role => {
        const isAdmin = role.role_key === "admin";
        const enabled = matrix[role.role_key] && matrix[role.role_key][permission.key];
        html += `<td><input type="checkbox" data-role="${_rpEscape(role.role_key)}" `
          + `data-perm="${_rpEscape(permission.key)}"${enabled ? " checked" : ""}${isAdmin ? " disabled" : ""}></td>`;
      });
      html += '</tr>';
    });
  });
  html += '</tbody></table></div>';
  box.innerHTML = html;
  _captureRolePermissionBaseline();
}

async function renderRolePermsInto(containerId) {
  const renderRevision = ++_rpRenderRevision;
  const box = document.getElementById(containerId);
  if (!box) return;
  _rpSaveRevision += 1;
  _rpSaveInFlight = false;
  cancelRolePermSave();
  _rpBaselineByRole = null;
  _resetRolePermissionV3State();
  if (!_isCurrentRolePermissionRender(renderRevision, containerId, box)) return;
  box.innerHTML = '<div class="module-loading">载入中...</div>';
  let data;
  try {
    const response = await fetch("/api/role-permissions");
    if (!_isCurrentRolePermissionRender(renderRevision, containerId, box)) return;
    if (!response.ok) {
      box.innerHTML = `<div class="cfg-hint">无权查看或加载失败（${response.status}）</div>`;
      return;
    }
    data = await response.json();
    if (!_isCurrentRolePermissionRender(renderRevision, containerId, box)) return;
  } catch {
    if (!_isCurrentRolePermissionRender(renderRevision, containerId, box)) return;
    box.innerHTML = '<div class="cfg-hint">加载失败（网络错误）</div>';
    return;
  }

  if (window.__accessV3 !== true) {
    _renderLegacyRolePermissions(box, data);
    return;
  }

  try {
    const normalized = normalizeRolePermissionPayload(data);
    if (!_isCurrentRolePermissionRender(renderRevision, containerId, box)) return;
    _installRolePermissionV3State(normalized, containerId, box);
    renderRolePermissionCenter();
  } catch {
    if (!_isCurrentRolePermissionRender(renderRevision, containerId, box)) return;
    _resetRolePermissionV3State();
    box.innerHTML = '<div class="cfg-hint">角色权限加载失败，请刷新后重试</div>';
  }
}

function beginRoleSavePreview(roleKey) {
  if (window.__accessV3 !== true) return false;
  if (_rpSaveInFlight) {
    _setRolePermissionFlowMessage("角色权限正在保存，请稍候");
    return false;
  }
  const role = String(roleKey || "");
  if (
    !_rpV3State.draftByRole.has(role)
    || role !== _rpV3State.selectedRoleKey
    || _rpV3State.view === "scope"
  ) return false;
  const diff = rolePermissionDiff(role);
  if (diff.added.length === 0 && diff.removed.length === 0) return false;
  _rpFlowRevision += 1;
  _rpPendingRole = role;
  _rpReauthReady = false;
  _rpReauthInFlight = false;
  const passwordStep = document.getElementById("rpPasswordStep");
  const reasonStep = document.getElementById("rpReasonStep");
  const password = document.getElementById("rpCurrentPassword");
  const reason = document.getElementById("rpReason");
  _syncRolePermissionSavePreview(_rolePermissionImpactMarkup(diff));
  if (passwordStep) passwordStep.hidden = false;
  if (reasonStep) reasonStep.hidden = true;
  if (password) password.value = "";
  if (reason) reason.value = "";
  _syncRolePermissionModal({focus: true});
  _setRolePermissionFlowMessage("请先确认当前登录密码");
  return true;
}

async function confirmRolePermPassword() {
  const password = document.getElementById("rpCurrentPassword");
  if (window.__accessV3 !== true || !_rpPendingRole || !password) return;
  if (_rpReauthInFlight) return;
  if (!password.value || !password.value.trim()) {
    _setRolePermissionFlowMessage("请输入当前密码");
    return;
  }
  const roleKey = _rpPendingRole;
  const flow = _captureRolePermissionFlow(roleKey);
  _rpReauthInFlight = true;
  _rpReauthReady = false;
  _setRolePermissionFlowMessage("正在确认密码...");
  try {
    const request = typeof _origFetch === "function" ? _origFetch : fetch;
    const response = await request("/api/auth/reauth", {
      method: "POST",
      headers: {"Content-Type": "application/json"},
      body: JSON.stringify({password: password.value}),
    });
    if (!_isCurrentRolePermissionFlow(flow)) return;
    if (!response.ok) {
      _setRolePermissionFlowMessage(`密码确认失败：${response.status}`);
      return;
    }
    _rpReauthReady = true;
    const currentPassword = document.getElementById("rpCurrentPassword");
    const passwordStep = document.getElementById("rpPasswordStep");
    const reasonStep = document.getElementById("rpReasonStep");
    const reason = document.getElementById("rpReason");
    if (currentPassword) currentPassword.value = "";
    if (passwordStep) passwordStep.hidden = true;
    if (reasonStep) reasonStep.hidden = false;
    if (reason) reason.focus();
    _setRolePermissionFlowMessage("密码已确认，请填写修改原因");
  } catch {
    if (_isCurrentRolePermissionFlow(flow)) {
      _setRolePermissionFlowMessage("密码确认失败（网络错误）");
    }
  } finally {
    if (_isCurrentRolePermissionFlow(flow)) {
      _rpReauthInFlight = false;
    }
  }
}

function cancelRolePermSave() {
  _rpFlowRevision += 1;
  _rpPendingRole = null;
  _rpReauthReady = false;
  _rpReauthInFlight = false;
  _rpRolePermissionModalSuspended = false;
  const passwordStep = document.getElementById("rpPasswordStep");
  const reasonStep = document.getElementById("rpReasonStep");
  const password = document.getElementById("rpCurrentPassword");
  const reason = document.getElementById("rpReason");
  if (passwordStep) passwordStep.hidden = false;
  if (reasonStep) reasonStep.hidden = true;
  if (password) password.value = "";
  if (reason) reason.value = "";
  _syncRolePermissionModal();
  _setRolePermissionFlowMessage("");
  _restoreRolePermissionPreviewFocus();
}

async function _rolePermissionResponseDetail(response) {
  try {
    const body = await response.json();
    return _isRecord(body) && typeof body.detail === "string"
      ? body.detail
      : "";
  } catch {
    return "";
  }
}

function _showRolePermissionPasswordStep() {
  _rpReauthReady = false;
  const password = document.getElementById("rpCurrentPassword");
  const passwordStep = document.getElementById("rpPasswordStep");
  const reasonStep = document.getElementById("rpReasonStep");
  if (passwordStep) passwordStep.hidden = false;
  if (reasonStep) reasonStep.hidden = true;
  if (password) {
    password.value = "";
    password.focus();
  }
}

function _validRolePermissionResultKeys(value) {
  return Array.isArray(value)
    && new Set(value).size === value.length
    && value.every(key => (
      typeof key === "string" && _rpV3State.permissionByKey.has(key)
    ));
}

function _normalizeRolePermissionSaveResult(data, roleKey, submittedSnapshot) {
  if (
    !_isRecord(data)
    || data.ok !== true
    || data.role_key !== roleKey
    || !_validRolePermissionResultKeys(data.old)
    || !_validRolePermissionResultKeys(data.new)
    || !_validRolePermissionResultKeys(data.added)
    || !_validRolePermissionResultKeys(data.removed)
    || !Number.isInteger(data.affected_staff_count)
    || data.affected_staff_count < 0
    || typeof data.changed !== "boolean"
  ) return null;
  const oldSet = new Set(data.old);
  const newSet = new Set(data.new);
  const addedSet = new Set(data.added);
  const removedSet = new Set(data.removed);
  const actualAdded = new Set([...newSet].filter(key => !oldSet.has(key)));
  const actualRemoved = new Set([...oldSet].filter(key => !newSet.has(key)));
  if (
    !_setsEqual(newSet, submittedSnapshot)
    || !_setsEqual(addedSet, actualAdded)
    || !_setsEqual(removedSet, actualRemoved)
    || data.changed !== (data.added.length > 0 || data.removed.length > 0)
  ) return null;
  return {
    added: [...data.added],
    removed: [...data.removed],
    affectedStaffCount: data.affected_staff_count,
  };
}

function _rolePermissionSaveResultText(result) {
  const addedNames = _permissionDisplayNames(result.added);
  const removedNames = _permissionDisplayNames(result.removed);
  return `影响人数：${result.affectedStaffCount} 人；`
    + `实际新增：${result.added.length} 项（${addedNames.join("、") || "无"}）；`
    + `实际移除：${result.removed.length} 项（${removedNames.join("、") || "无"}）`;
}

async function _saveTargetRolePerms() {
  if (_rpSaveInFlight) {
    _setRolePermissionFlowMessage("角色权限正在保存，请稍候");
    return;
  }
  if (!_rpPendingRole || !_rpReauthReady) {
    _setRolePermissionFlowMessage("请先确认当前密码");
    return;
  }
  const reasonInput = document.getElementById("rpReason");
  const reason = reasonInput && typeof reasonInput.value === "string"
    ? reasonInput.value.trim()
    : "";
  if (!reason) {
    _setRolePermissionFlowMessage("请填写修改原因");
    return;
  }
  const roleKey = _rpPendingRole;
  const flow = _captureRolePermissionFlow(roleKey);
  const currentDraft = _rpV3State.draftByRole.get(roleKey);
  if (!currentDraft) {
    _setRolePermissionFlowMessage("保存失败：岗位草稿不可用");
    return;
  }
  if (_setsEqual(currentDraft, _rpV3State.baselineByRole.get(roleKey))) {
    _setRolePermissionFlowMessage("没有需要保存的权限变更");
    return;
  }
  const submittedSnapshot = new Set(currentDraft);
  const submittedAutoAddedDependencies = new Map(
    [..._rpV3State.autoAddedDependencies].filter(
      ([permissionKey]) => submittedSnapshot.has(permissionKey),
    ),
  );
  const submittedPermissions = _rpV3State.permissions
    .filter(permission => submittedSnapshot.has(permission.key))
    .map(permission => permission.key);
  const submittedExpectedPermissions = [
    ..._rpV3State.baselineByRole.get(roleKey),
  ].sort();
  const saveRevision = ++_rpSaveRevision;
  let renderSavedState = false;
  _setRolePermissionSaveInFlight(true);
  _setRolePermissionFlowMessage("保存中...");
  try {
    const request = typeof _origFetch === "function" ? _origFetch : fetch;
    const response = await request("/api/role-permissions", {
      method: "PUT",
      headers: {"Content-Type": "application/json"},
      body: JSON.stringify({
        role_key: roleKey,
        permissions: submittedPermissions,
        expected_permissions: submittedExpectedPermissions,
        reason,
      }),
    });
    if (!_isCurrentRolePermissionFlow(flow)) return;
    if (!response.ok) {
      const detail = await _rolePermissionResponseDetail(response);
      if (!_isCurrentRolePermissionFlow(flow)) return;
      if (detail === "recent_reauthentication_required") {
        _showRolePermissionPasswordStep();
      }
      _setRolePermissionFlowMessage(
        _hasOwn(_RP_SAVE_ERROR_MESSAGES, detail)
          ? _RP_SAVE_ERROR_MESSAGES[detail]
          : `保存失败：${response.status}`,
      );
      return;
    }
    let responseBody;
    try {
      responseBody = await response.json();
    } catch {
      if (_isCurrentRolePermissionFlow(flow)) {
        _setRolePermissionFlowMessage("保存失败：服务端响应无效，请刷新后重试");
      }
      return;
    }
    if (!_isCurrentRolePermissionFlow(flow)) return;
    const result = _normalizeRolePermissionSaveResult(
      responseBody,
      roleKey,
      submittedSnapshot,
    );
    if (!result) {
      _setRolePermissionFlowMessage("保存失败：服务端响应无效，请刷新后重试");
      return;
    }
    const hasUnsubmittedChanges = !_setsEqual(
      submittedSnapshot,
      _rpV3State.draftByRole.get(roleKey),
    );
    _rpV3State.baselineByRole.set(roleKey, new Set(submittedSnapshot));
    const remainingAutoAddedDependencies = new Map(
      _rpV3State.autoAddedDependencies,
    );
    submittedAutoAddedDependencies.forEach((generation, permissionKey) => {
      if (remainingAutoAddedDependencies.get(permissionKey) === generation) {
        remainingAutoAddedDependencies.delete(permissionKey);
      }
    });
    _rpV3State.autoAddedDependencies = remainingAutoAddedDependencies;
    cancelRolePermSave();
    const savedState = hasUnsubmittedChanges
      ? "本次提交已保存，另有未保存修改（其他用户下次刷新生效）"
      : "已保存 ✓（其他用户下次刷新生效）";
    _setRolePermissionFlowMessage(
      `${savedState}；${_rolePermissionSaveResultText(result)}`,
      {
        kind: "terminal",
        roleKey,
      },
    );
    renderSavedState = true;
  } catch {
    if (_isCurrentRolePermissionFlow(flow)) {
      _setRolePermissionFlowMessage("保存失败（网络错误）");
    }
  } finally {
    if (saveRevision !== _rpSaveRevision) return;
    if (renderSavedState) {
      _rpSaveInFlight = false;
      renderRolePermissionCenter();
      _restoreRolePermissionSaveSuccessFocus();
    } else if (_isCurrentRolePermissionFlow(flow)) {
      _setRolePermissionSaveInFlight(false);
    }
  }
}

async function saveRolePerms() {
  if (window.__accessV3 === true) return _saveTargetRolePerms();
  const message = document.getElementById("rpMsg");
  if (_rpSaveInFlight) {
    if (message) message.textContent = "角色权限正在保存，请稍候";
    return;
  }
  const submittedSnapshot = _readRolePermissionSnapshot();
  const matrix = {};
  submittedSnapshot.forEach((permissionsForRole, role) => {
    if (role === "admin") return;
    matrix[role] = {};
    permissionsForRole.forEach((enabled, permission) => {
      matrix[role][permission] = enabled;
    });
  });
  const saveRevision = ++_rpSaveRevision;
  const renderRevision = _rpRenderRevision;
  const isCurrentSave = () => (
    window.__accessV3 !== true
    && saveRevision === _rpSaveRevision
    && renderRevision === _rpRenderRevision
    && document.getElementById("rpMsg") === message
  );
  _setRolePermissionSaveInFlight(true);
  if (message) message.textContent = "保存中...";
  try {
    const response = await fetch("/api/role-permissions", {
      method: "PUT",
      headers: {"Content-Type": "application/json"},
      body: JSON.stringify({matrix}),
    });
    if (!isCurrentSave()) return;
    const hasUnsubmittedChanges = response.ok && !_legacyRolePermissionSnapshotsEqual(
      submittedSnapshot,
      _readRolePermissionSnapshot(),
    );
    if (response.ok) _replaceRolePermissionBaseline(submittedSnapshot);
    if (message) {
      message.textContent = !response.ok
        ? `保存失败：${response.status}`
        : (hasUnsubmittedChanges
          ? "本次提交已保存，另有未保存修改（其他用户下次刷新生效）"
          : "已保存 ✓（其他用户下次刷新生效）");
    }
  } catch {
    if (isCurrentSave() && message) message.textContent = "保存失败（网络错误）";
  } finally {
    if (isCurrentSave()) _setRolePermissionSaveInFlight(false);
  }
}

Object.assign(window, {
  normalizeRolePermissionPayload,
  roleTreeExpandedKey,
  toggleRoleTreeSection,
  selectRole,
  togglePermission,
  togglePermissionSubtree,
  rolePermissionDiff,
  restoreRoleBaseline,
  setRolePermissionSearch,
  setRolePermissionView,
  renderRolePermsInto,
  renderRolePermissionCenter,
  beginRoleSavePreview,
  confirmRolePermPassword,
  cancelRolePermSave,
  saveRolePerms,
  requestLeaveRolePermissions,
});
