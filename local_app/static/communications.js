// 沟通咨询子tab（客户沟通→沟通咨询）：电话/微信联系记录 + 微信截图 + 电话录音。
// 作为「客户沟通」tab 的子tab 渲染(renderCommunicationsSub(body))，由 workspace_tabs.loadCsSubTab 调用。
// 对接 GET|POST /api/patients/{id}/communications、PATCH|DELETE /api/communications/{id}、
// POST|GET|DELETE .../images；电话录音走 calls 的 linked_communication_id 弱关联。
// 复用 consult_surgery.js 的 field()/area()/csRow() 与 cs-* 样式。

const COMM_DEAL_OPTIONS = ["", "未成交", "已成交", "意向", "无效"];

function commChannelBadge(ch) {
  return ch === "phone"
    ? '<span class="plan-view-tag">电话</span>'
    : '<span class="plan-view-tag">微信</span>';
}

// 子tab 入口：拉沟通记录 + 该患者录音(按 linked_communication_id 归组),竞态守卫同 renderCsReturnVisitsSub
async function renderCommunicationsSub(panel) {
  const pid = workspacePatientId;
  panel.textContent = "加载中...";
  let data, callsData;
  try {
    const [cRes, kRes] = await Promise.all([
      fetch(`/api/patients/${encodeURIComponent(pid)}/communications`),
      fetch(`/api/patients/${encodeURIComponent(pid)}/calls`),
    ]);
    if (pid !== workspacePatientId) return;
    if (!cRes.ok) { panel.textContent = "沟通记录载入失败"; return; }
    data = await cRes.json();
    callsData = kRes.ok ? await kRes.json() : { calls: [] };
    if (pid !== workspacePatientId) return;
  } catch {
    if (pid !== workspacePatientId) return;
    panel.textContent = "沟通记录载入失败"; return;
  }
  if (panel.dataset.csCur && panel.dataset.csCur !== "沟通咨询") return;   // 期间切走子tab → 不覆盖共享 body
  renderCommunications(panel, data, callsData);
}

function renderCommunications(panel, data, callsData) {
  const callsByComm = {};
  (callsData.calls || []).forEach(c => {
    if (c.linked_communication_id) (callsByComm[c.linked_communication_id] ||= []).push(c);
  });
  const list = (data.communications || []).map(c => {
    const opts = COMM_DEAL_OPTIONS.map(o =>
      `<option value="${escapeAttr(o)}"${o === (c.deal_status || "") ? " selected" : ""}>${escapeHtml(o || "成交状态")}</option>`).join("");
    const shots = (c.images || []).map(im =>
      `<span><a href="/api/communications/${encodeURIComponent(c.communication_id)}/images/${encodeURIComponent(im.image_id)}/file" target="_blank" rel="noopener">
         <img src="/api/communications/${encodeURIComponent(c.communication_id)}/images/${encodeURIComponent(im.image_id)}/file"
              alt="微信截图" style="width:84px;height:84px;object-fit:cover;border-radius:6px;margin:4px 4px 0 0">
       </a><button type="button" class="link-danger" onclick="deleteCommImage('${escapeAttr(c.communication_id)}', '${escapeAttr(im.image_id)}', ${Number(c.revision)})">删</button></span>`).join("");
    const recs = (callsByComm[c.communication_id] || []).map(k =>
      `<audio controls preload="none" src="/api/calls/${encodeURIComponent(k.call_id)}/recording" style="display:block;margin-top:6px;max-width:320px"></audio>`).join("");
    const cid = escapeAttr(c.communication_id);
    // #722 补传入口:新增时附件上传失败(或事后想补)可在此卡片补传,不必重建记录
    const reattach = c.channel === "wechat"
      ? `<div class="cs-reattach" style="margin-top:6px"><input type="file" accept="image/*" multiple data-reshot="${cid}">
           <button type="button" onclick="reattachCommImages('${cid}', ${Number(c.revision)})">补传截图</button></div>`
      : `<div class="cs-reattach" style="margin-top:6px"><input type="file" accept="audio/*" data-rerec="${cid}">
           <button type="button" onclick="reattachCommAudio('${cid}', '${escapeAttr(c.direction)}', ${Number(c.revision)})">补传录音</button></div>`;
    return `
      <div class="cs-card">
        <div class="cs-card-head">${commChannelBadge(c.channel)}
          ${c.operator ? `<strong>${escapeHtml(c.operator)}</strong>` : ""}
          <span class="muted">${escapeHtml(c.contacted_at || "")}</span>
          <select class="cs-input" onchange="setCommDeal('${cid}', ${Number(c.revision)}, this.value)">${opts}</select>
          <button type="button" class="link-danger" onclick="deleteCommunication('${cid}', ${Number(c.revision)})">删除</button>
        </div>
        ${c.content ? csRow("内容", c.content) : ""}
        ${c.note ? csRow("备注", c.note) : ""}
        ${shots ? `<div>${shots}</div>` : ""}
        ${recs || ""}
        ${reattach}
      </div>`;
  }).join("");

  panel.innerHTML = `
    <section class="panel"><div class="panel-head"><span>新增沟通</span></div>
      <div class="panel-body cs-form" data-comm-form>
        <div class="cs-grid">
          <label><span>方式</span><select class="cs-input" data-field="channel" onchange="commChannelChanged()">
            <option value="phone">电话</option><option value="wechat">微信</option></select></label>
          <label data-comm-direction><span>方向</span><select class="cs-input" data-field="direction">
            <option value="outbound">电话呼出</option><option value="inbound">电话呼入</option></select></label>
          <label><span>成交状态</span><select class="cs-input" data-field="deal_status">
            ${COMM_DEAL_OPTIONS.map(o => `<option value="${escapeAttr(o)}">${escapeHtml(o || "未选")}</option>`).join("")}</select></label>
        </div>
        ${area("沟通内容", "content")}
        <label class="cs-wide" data-comm-rec><span>录音文件（电话，可选）</span>
          <input class="cs-input" type="file" accept="audio/*" data-comm-audio></label>
        <label class="cs-wide" data-comm-shot hidden><span>微信截图（可多张）</span>
          <input class="cs-input" type="file" accept="image/*" multiple data-comm-images></label>
        <div class="cs-actions"><button type="button" onclick="saveCommunication()">保存沟通</button>
          <span data-comm-status></span></div>
      </div></section>
    <section class="panel"><div class="panel-head"><span>沟通记录</span></div>
      <div class="panel-body">${list || '<span class="empty">暂无沟通记录</span>'}</div></section>`;
}

function commFormEl() {
  const page = workspacePageEl();
  return page && page.querySelector("[data-comm-form]");
}

// 切方式:电话显录音输入、微信显截图输入
function commChannelChanged() {
  const form = commFormEl();
  if (!form) return;
  const ch = form.querySelector('[data-field="channel"]').value;
  const rec = form.querySelector("[data-comm-rec]");
  const shot = form.querySelector("[data-comm-shot]");
  const direction = form.querySelector("[data-comm-direction]");
  if (rec) rec.hidden = ch !== "phone";
  if (shot) shot.hidden = ch !== "wechat";
  if (direction) direction.hidden = ch !== "phone";
}

async function saveCommunication() {
  const form = commFormEl();
  if (!form) return;
  const status = form.querySelector("[data-comm-status]");
  // #735 表单字段一律 data-field(field()/area() 与内联控件同口径),读取只认这一个属性
  const get = k => (form.querySelector(`[data-field="${k}"]`)?.value || "").trim();
  const channel = get("channel");
  const payload = {
    channel,
    direction: channel === "phone" ? get("direction") : "",
    deal_status: get("deal_status"),
    content: get("content"),
  };
  if (status) status.textContent = "保存中...";
  const pid = workspacePatientId;
  let res;
  try {
    res = await fetch(`/api/patients/${encodeURIComponent(pid)}/communications`, {
      method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(payload),
    });
  } catch { if (status) status.textContent = "保存失败（网络异常）"; return; }
  if (!res.ok) {
    const m = await res.json().catch(() => ({}));
    if (status) status.textContent = "保存失败：" + (m.detail || res.status); return;
  }
  const created = await res.json();
  const commId = created.communication_id;
  let contextRevision = Number(created.revision);
  // 附件:微信传截图 / 电话传录音(弱关联到本条沟通)。#720 逐个检查响应,失败必须明确提示,
  // 不能主记录存了、截图/录音(核心沟通证据)静默丢了还按成功重载。
  let attachFail = 0;
  try {
    if (channel === "wechat") {
      const files = form.querySelector("[data-comm-images]")?.files || [];
      for (const f of files) {
        const fd = new FormData();
        fd.append("file", f, f.name);
        fd.append("expected_revision", String(contextRevision));
        const r = await fetch(`/api/communications/${encodeURIComponent(commId)}/images`, { method: "POST", body: fd });
        if (r.status === 409) {
          alert("数据已被其他操作更新，请确认最新内容后重试");
          attachFail = -1;
          break;
        }
        if (!r.ok) { attachFail++; break; }
        const attached = await r.json().catch(() => ({}));
        const nextRevision = Number(attached.revision);
        if (!Number.isInteger(nextRevision) || nextRevision <= contextRevision) { attachFail++; break; }
        contextRevision = nextRevision;
      }
    } else if (channel === "phone") {
      const audio = form.querySelector("[data-comm-audio]")?.files?.[0];
      if (audio) {
        const fd = new FormData();
        fd.append("file", audio, audio.name);
        fd.append("direction", payload.direction);
        fd.append("linked_communication_id", commId);
        fd.append("expected_revision", String(contextRevision));
        const r = await fetch(`/api/patients/${encodeURIComponent(pid)}/calls`, { method: "POST", body: fd });
        if (r.status === 409) {
          alert("数据已被其他操作更新，请确认最新内容后重试");
          attachFail = -1;
        } else if (!r.ok) attachFail++;
      }
    }
  } catch { attachFail++; }
  if (attachFail > 0) {
    // #722 附件失败:主记录已存,重载后到该记录卡片用「补传截图/补传录音」重试(不必重建重复记录)
    alert("沟通记录已保存，但有 " + attachFail + " 个附件（截图/录音）上传失败。\n请在下方该记录卡片用「补传」重新上传，不用重建记录。");
  }
  if (pid !== workspacePatientId) return;
  reloadCommunicationsSub();
  if (typeof loadAuditLogs === "function") await loadAuditLogs();
}

// 就地重载沟通咨询子页(不 switchWorkspaceTab,录音/截图无独立顶层tab)
function reloadCommunicationsSub() {
  const page = workspacePageEl();
  const body = page && page.querySelector("[data-cs-sub-body]");
  if (body) { body.dataset.csCur = "沟通咨询"; renderCommunicationsSub(body); }
}

async function setCommDeal(commId, revision, value) {
  // #702 禁止静默假保存:失败必须告警并重载(把下拉回滚到服务端真值)
  if (!Number.isInteger(revision) || revision < 1) {
    alert("数据版本已失效，请刷新后重试");
    reloadCommunicationsSub();
    return;
  }
  let res;
  try {
    res = await fetch(`/api/communications/${encodeURIComponent(commId)}`, {
      method: "PATCH", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ expected_revision: revision, deal_status: value }),
    });
  } catch { alert("成交状态保存失败（网络异常），请重试"); reloadCommunicationsSub(); return; }
  if (res.status === 409) {
    alert("数据已被其他操作更新，请确认最新内容后重试");
    reloadCommunicationsSub();
    return;
  }
  if (!res.ok) { alert("成交状态保存失败（" + res.status + "），未生效"); reloadCommunicationsSub(); return; }
  reloadCommunicationsSub();
}

async function deleteCommunication(commId, revision) {
  if (!confirm("确定删除这条沟通记录？（含微信截图）")) return;
  if (!Number.isInteger(revision) || revision < 1) {
    alert("数据版本已失效，请刷新后重试");
    reloadCommunicationsSub();
    return;
  }
  let res;
  try {
    res = await fetch(`/api/communications/${encodeURIComponent(commId)}`, {
      method: "DELETE", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ expected_revision: revision }),
    });
  } catch { alert("删除失败（网络异常），请重试"); return; }   // #702 不吞错
  if (res.status === 409) {
    alert("数据已被其他操作更新，请确认最新内容后重试");
    reloadCommunicationsSub();
    return;
  }
  if (!res.ok) { alert("删除失败（" + res.status + "）"); return; }
  // #721 后端删盘失败会返回 files_failed>0(截图还在磁盘,已记审计待清理),明确告警不当成功掩盖
  const body = await res.json().catch(() => ({}));
  if (body && body.files_failed > 0) {
    alert("记录已删除，但有 " + body.files_failed + " 张微信截图未能从磁盘删除（已记审计待清理），请联系管理员。");
  }
  reloadCommunicationsSub();
  if (typeof loadAuditLogs === "function") await loadAuditLogs();
}

// #722 卡片补传:附件上传失败(或事后补)时对已存在的沟通记录补传截图/录音,不必重建记录
async function reattachCommImages(commId, revision) {
  const page = workspacePageEl();
  const input = page && page.querySelector(`[data-reshot="${commId}"]`);
  const files = (input && input.files) || [];
  if (!files.length) { alert("请先选择要补传的截图"); return; }
  const pid = workspacePatientId;
  let currentRevision = Number(revision);
  if (!Number.isInteger(currentRevision) || currentRevision < 1) {
    alert("数据版本已失效，请刷新后重试");
    reloadCommunicationsSub();
    return;
  }
  let fail = 0;
  for (const f of files) {
    const fd = new FormData();
    fd.append("file", f, f.name);
    fd.append("expected_revision", String(currentRevision));
    try {
      const r = await fetch(`/api/communications/${encodeURIComponent(commId)}/images`, { method: "POST", body: fd });
      if (r.status === 409) {
        alert("数据已被其他操作更新，请确认最新内容后重试");
        reloadCommunicationsSub();
        return;
      }
      if (!r.ok) { fail++; break; }
      const result = await r.json().catch(() => ({}));
      const nextRevision = Number(result.revision);
      if (!Number.isInteger(nextRevision) || nextRevision <= currentRevision) { fail++; break; }
      currentRevision = nextRevision;
    } catch { fail++; break; }
  }
  if (fail > 0) alert("有 " + fail + " 张截图补传失败，请重试");
  if (pid !== workspacePatientId) return;
  reloadCommunicationsSub();
}

async function reattachCommAudio(commId, direction, revision) {
  const page = workspacePageEl();
  const input = page && page.querySelector(`[data-rerec="${commId}"]`);
  const audio = input && input.files && input.files[0];
  if (!audio) { alert("请先选择要补传的录音"); return; }
  if (!Number.isInteger(Number(revision)) || Number(revision) < 1) {
    alert("数据版本已失效，请刷新后重试");
    reloadCommunicationsSub();
    return;
  }
  const pid = workspacePatientId;
  const fd = new FormData();
  fd.append("file", audio, audio.name);
  fd.append("direction", direction);
  fd.append("linked_communication_id", commId);
  fd.append("expected_revision", String(revision));
  let response;
  try {
    response = await fetch(`/api/patients/${encodeURIComponent(pid)}/calls`, { method: "POST", body: fd });
  } catch { response = null; }
  if (response && response.status === 409) {
    alert("数据已被其他操作更新，请确认最新内容后重试");
  } else if (!response || !response.ok) alert("录音补传失败，请重试");
  if (pid !== workspacePatientId) return;
  reloadCommunicationsSub();
}

async function deleteCommImage(commId, imageId, revision) {
  if (!confirm("删除这张截图？")) return;
  if (!Number.isInteger(Number(revision)) || Number(revision) < 1) {
    alert("数据版本已失效，请刷新后重试");
    reloadCommunicationsSub();
    return;
  }
  let response;
  try {
    response = await fetch(`/api/communications/${encodeURIComponent(commId)}/images/${encodeURIComponent(imageId)}`, {
      method: "DELETE", headers: {"Content-Type": "application/json"},
      body: JSON.stringify({expected_revision: Number(revision)}),
    });
  } catch { alert("删除失败（网络异常）"); return; }
  if (response.status === 409) {
    alert("数据已被其他操作更新，请确认最新内容后重试");
    reloadCommunicationsSub();
    return;
  }
  if (!response.ok) { alert("删除失败（" + response.status + "）"); return; }
  reloadCommunicationsSub();
}

Object.assign(window, {
  renderCommunicationsSub, commChannelChanged, saveCommunication,
  setCommDeal, deleteCommunication, reattachCommImages, reattachCommAudio,
  deleteCommImage,
});
