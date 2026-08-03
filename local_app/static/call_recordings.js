// 通话录音：录音上传 / 手机麦克风录音 / 列表回放 / 改成交状态 / 删除。
// 作为「回访」tab 里的一段渲染(loadCallsTab(container))，回访与录音一起管理，不单独占标签页。
// 对接后端 GET|POST /api/patients/{id}/calls、GET /api/calls/{id}/recording、PATCH|DELETE /api/calls/{id}。
// Phase 1 纯本地：不含点击拨号/总机对接(属 Phase 2/3 telephony 层)。

const CALL_DEAL_OPTIONS = ["", "未成交", "已成交", "意向", "无效"];

function callDurationText(sec) {
  const s = Math.max(0, parseInt(sec, 10) || 0);
  if (!s) return "";
  const m = Math.floor(s / 60), r = s % 60;
  return m ? `${m}分${r}秒` : `${r}秒`;
}

function callDirBadge(dir) {
  return dir === "inbound"
    ? '<span class="plan-view-tag">电话呼入</span>'
    : '<span class="plan-view-tag">电话呼出</span>';
}

async function loadCallsTab(panel) {
  const identity = workspacePatientId;
  panel.textContent = "加载中...";
  let data;
  try {
    const res = await fetch(`/api/patients/${encodeURIComponent(identity)}/calls`);
    if (identity !== workspacePatientId) return;
    if (!res.ok) { panel.textContent = "通话录音载入失败"; return; }
    data = await res.json();
    if (identity !== workspacePatientId) return;
  } catch {
    if (identity !== workspacePatientId) return;
    panel.textContent = "通话录音载入失败"; return;
  }
  const contexts = (typeof workspaceData !== "undefined" && workspaceData)
    ? (workspaceData.return_visits || []) : [];
  renderCallsTab(panel, data, contexts);
}

function renderCallsTab(panel, data, contexts = []) {
  const list = (data.calls || []).map(c => {
    const opts = CALL_DEAL_OPTIONS.map(o =>
      `<option value="${escapeAttr(o)}"${o === (c.deal_status || "") ? " selected" : ""}>${escapeHtml(o || "成交状态")}</option>`).join("");
    const dur = callDurationText(c.duration_sec);
    return `
      <div class="cs-card">
        <div class="cs-card-head">${callDirBadge(c.direction)}
          ${c.peer_norm ? `<strong>${escapeHtml(c.peer_norm)}</strong>` : ""}
          ${c.region ? `<span class="muted">${escapeHtml(c.region)}</span>` : ""}
          <span class="muted">${escapeHtml(c.started_at || "")}</span>
          ${dur ? `<span class="muted">${dur}</span>` : ""}
          <select class="cs-input" onchange="setCallDeal('${escapeAttr(c.call_id)}', ${Number(c.revision)}, this.value)">${opts}</select>
          <button type="button" class="link-danger" onclick="deleteCall('${escapeAttr(c.call_id)}', ${Number(c.revision)})">删除</button>
        </div>
        <audio controls preload="none" src="/api/calls/${encodeURIComponent(c.call_id)}/recording"></audio>
        ${c.note ? csRow("备注", c.note) : ""}
      </div>`;
  }).join("");
  const contextOptions = contexts.map(row =>
    `<option value="${escapeAttr(row.return_visit_id)}" data-revision="${escapeAttr(row.revision)}">${escapeHtml(String(row.due_time || "").slice(0, 16))} ${escapeHtml(row.item_name || "回访")}</option>`
  ).join("");

  panel.innerHTML = `
    <section class="panel"><div class="panel-head"><span>新增通话录音</span></div>
      <div class="panel-body cs-form" data-call-form>
        <div class="cs-grid">
          <label><span>关联回访</span><select class="cs-input" data-call-context>
            <option value="">请选择回访记录</option>${contextOptions}</select></label>
          <label><span>方向</span><select class="cs-input" data-field="direction">
            <option value="outbound">电话呼出</option><option value="inbound">电话呼入</option></select></label>
          ${field("对方号码", "peer_number", "")}
          ${field("通话时间", "started_at", "")}
          ${field("时长(秒)", "duration_sec", "")}
        </div>
        <div class="cs-grid">
          <label><span>成交状态</span><select class="cs-input" data-field="deal_status">
            ${CALL_DEAL_OPTIONS.map(o => `<option value="${escapeAttr(o)}">${escapeHtml(o || "未选")}</option>`).join("")}</select></label>
          <label class="cs-wide"><span>录音文件</span><input class="cs-input" type="file" accept="audio/*" data-call-file></label>
        </div>
        ${field("备注", "note", "", true)}
        <div class="cs-actions">
          <button type="button" onclick="uploadCallRecording()">上传录音</button>
          <button type="button" data-call-rec onclick="toggleCallMicRecord()">手机录音</button>
          <span data-call-status></span>
        </div>
        <div class="muted" style="font-size:12px">手机录音只录本机麦克风环境声；iPhone 打真电话时录不到对方，电话回访请用第二台设备免提录，或走诊室软电话。</div>
      </div></section>
    <section class="panel"><div class="panel-head"><span>通话记录</span></div>
      <div class="panel-body">${list || '<span class="empty">暂无通话录音</span>'}</div></section>`;
}

function callFormEl() {
  const page = workspacePageEl();
  return page && page.querySelector("[data-call-form]");
}

function collectCallFields(form) {
  const payload = {};
  form.querySelectorAll("[data-field]").forEach(i => { payload[i.dataset.field] = (i.value || "").trim(); });
  return payload;
}

async function postCallRecording(blob, filename, source, statusEl) {
  const form = callFormEl();
  if (!form) return;
  const fields = collectCallFields(form);
  const context = form.querySelector("[data-call-context]");
  const contextId = (context && context.value || "").trim();
  const contextRevision = Number(context && context.selectedOptions
    && context.selectedOptions[0] && context.selectedOptions[0].dataset.revision);
  if (!contextId || !Number.isInteger(contextRevision) || contextRevision < 1) {
    if (statusEl) statusEl.textContent = "请先选择要关联的回访记录";
    return;
  }
  const fd = new FormData();
  fd.append("file", blob, filename);
  fd.append("direction", fields.direction || "outbound");
  fd.append("peer_number", fields.peer_number || "");
  fd.append("started_at", fields.started_at || "");
  fd.append("duration_sec", fields.duration_sec || "0");
  fd.append("deal_status", fields.deal_status || "");
  fd.append("note", fields.note || "");
  fd.append("source", source);
  fd.append("linked_return_visit_id", contextId);
  fd.append("expected_revision", String(contextRevision));
  if (statusEl) statusEl.textContent = "上传中...";
  let res;
  try {
    res = await fetch(`/api/patients/${encodeURIComponent(workspacePatientId)}/calls`, { method: "POST", body: fd });
  } catch { if (statusEl) statusEl.textContent = "上传失败（网络异常）"; return; }
  if (res.status === 409) {
    if (statusEl) statusEl.textContent = "数据已被其他操作更新，请确认最新内容后重试";
    reloadCallsInPlace();
    return;
  }
  if (!res.ok) {
    const m = await res.json().catch(() => ({}));
    if (statusEl) statusEl.textContent = "上传失败：" + (m.detail || res.status); return;
  }
  workspaceLoadedTabs.delete("return-visits");
  switchWorkspaceTab("return-visits");
  if (typeof loadAuditLogs === "function") await loadAuditLogs();
}

async function uploadCallRecording() {
  const form = callFormEl();
  if (!form) return;
  const status = form.querySelector("[data-call-status]");
  const fileInput = form.querySelector("[data-call-file]");
  const file = fileInput && fileInput.files && fileInput.files[0];
  if (!file) { if (status) status.textContent = "请先选择录音文件"; return; }
  await postCallRecording(file, file.name, "manual_import", status);
}

// ---------- 手机麦克风录音(回访手机录音) ----------
let _callMicRec = null, _callMicChunks = [], _callMicStart = 0, _callMicPid = "";

async function toggleCallMicRecord() {
  const form = callFormEl();
  if (!form) return;
  const status = form.querySelector("[data-call-status]");
  const btn = form.querySelector("[data-call-rec]");
  if (_callMicRec && _callMicRec.state === "recording") {
    _callMicRec.stop();
    return;
  }
  let stream;
  try {
    stream = await navigator.mediaDevices.getUserMedia({ audio: true });
  } catch { if (status) status.textContent = "无法访问麦克风（请允许权限）"; return; }
  _callMicChunks = [];
  _callMicStart = Date.now();
  _callMicPid = workspacePatientId;   // #700 锁定开始录音时的患者,停止上传只认这个,防录音中切患者挂错病历
  const mime = MediaRecorder.isTypeSupported("audio/webm") ? "audio/webm" : "";
  _callMicRec = mime ? new MediaRecorder(stream, { mimeType: mime }) : new MediaRecorder(stream);
  _callMicRec.ondataavailable = e => { if (e.data && e.data.size) _callMicChunks.push(e.data); };
  _callMicRec.onstop = async () => {
    stream.getTracks().forEach(t => t.stop());
    if (btn) btn.textContent = "手机录音";
    const sec = Math.round((Date.now() - _callMicStart) / 1000);
    const durField = form.querySelector('[data-field="duration_sec"]');
    if (durField && !durField.value.trim()) durField.value = String(sec);
    if (_callMicPid !== workspacePatientId) {   // #700 录音中切了患者/离开:不能挂到当前患者,取消上传并提示
      if (status) status.textContent = "录音中切换了患者，已取消上传（请回到原患者重录）";
      return;
    }
    const blob = new Blob(_callMicChunks, { type: "audio/webm" });
    await postCallRecording(blob, "mic-record.webm", "return_visit_mobile", status);
  };
  _callMicRec.start();
  if (btn) btn.textContent = "停止录音";
  if (status) status.textContent = "录音中…（免提说话，讲完点停止）";
}

// 就地重载通话录音段(回访子tab 里的 [data-consult-calls] 容器),用于失败回滚 UI 到服务端真值
function reloadCallsInPlace() {
  const page = workspacePageEl();
  const cc = page && page.querySelector("[data-consult-calls]");
  if (cc) loadCallsTab(cc);
}

async function setCallDeal(callId, revision, value) {
  // #702 禁止静默假保存:PATCH 失败必须明确告警并重载列表(把下拉回滚到服务端真值)
  if (!Number.isInteger(revision) || revision < 1) {
    alert("数据版本已失效，请刷新后重试");
    reloadCallsInPlace();
    return;
  }
  let res;
  try {
    res = await fetch(`/api/calls/${encodeURIComponent(callId)}`, {
      method: "PATCH", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ expected_revision: revision, deal_status: value }),
    });
  } catch { alert("成交状态保存失败（网络异常），请重试"); reloadCallsInPlace(); return; }
  if (res.status === 409) {
    alert("数据已被其他操作更新，请确认最新内容后重试");
    reloadCallsInPlace();
    return;
  }
  if (!res.ok) { alert("成交状态保存失败（" + res.status + "），未生效"); reloadCallsInPlace(); return; }
  reloadCallsInPlace();
}

async function deleteCall(callId, revision) {
  if (!confirm("确定删除这条通话录音？")) return;
  if (!Number.isInteger(revision) || revision < 1) {
    alert("数据版本已失效，请刷新后重试");
    reloadCallsInPlace();
    return;
  }
  let res;
  try {
    res = await fetch(`/api/calls/${encodeURIComponent(callId)}`, {
      method: "DELETE", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ expected_revision: revision }),
    });
  } catch { alert("删除失败（网络异常），请重试"); return; }   // #702 不吞错
  if (res.status === 409) {
    alert("数据已被其他操作更新，请确认最新内容后重试");
    reloadCallsInPlace();
    return;
  }
  if (!res.ok) { alert("删除失败（" + res.status + "）"); return; }
  // #716 后端删盘失败会返回 file_removed:false(录音文件还在磁盘,已记审计待清理)→ 明确告警,不当成功掩盖
  const body = await res.json().catch(() => ({}));
  if (body && body.file_removed === false) {
    alert("记录已删除，但录音文件未能从磁盘删除（已记审计待清理），请联系管理员处理。");
  }
  // 录音并入「客户沟通→回访」子tab,无独立 calls 顶层tab:就地重载 return-visits 刷新录音列表(对齐上传路径)
  workspaceLoadedTabs.delete("return-visits");
  switchWorkspaceTab("return-visits");
  if (typeof loadAuditLogs === "function") await loadAuditLogs();
}

Object.assign(window, {
  loadCallsTab, uploadCallRecording, toggleCallMicRecord, setCallDeal, deleteCall,
});
