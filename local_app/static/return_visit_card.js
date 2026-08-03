// 回访卡片(客户通→回访列表→点行弹出的弹窗浮层)：回访详情编辑 + 标记已回访 +
// 电话回访录音(走 calls 的 linked_return_visit_id) + 微信回访截图(return-visit-images)。
// 只在客户通模块用;数据 GET/PUT /api/return-visits/{id}、.../images、患者 calls。

let _rvCardId = "", _rvCardPid = "";
// #734 卡片代数：开/关/切卡片都 +1。每次开麦会话在点击瞬间记住自己那一代，
// 之后任何环节(权限框返回/onstop)发现代数变了就永久取消——代数只增不回头，
// 杜绝"关后立刻重开同一回访,ID 又相等,旧守卫误放行"的竞态。
let _rvCardGen = 0;
const RV_CARD_STATUS = [["待回访", "待回访"], ["已回访", "已回访"]];

function rvCardOverlay() { return document.getElementById("rvCardOverlay"); }

function _rvCardEsc(e) { if (e.key === "Escape") closeReturnVisitCard(); }

function openReturnVisitCard(rvId, patientId) {
  if (!rvId) return;
  _rvCardGen++;   // 切卡片:旧麦克风会话立即失效
  // 换卡片时若上一张还在录音,必须停(onstop 因代数不匹配会取消上传),防麦克风悬空续录
  if (_rvMicRec && _rvMicRec.state === "recording") { try { _rvMicRec.stop(); } catch { /* 忽略 */ } }
  _rvCardId = rvId; _rvCardPid = patientId || "";
  const ov = rvCardOverlay();
  if (!ov) return;
  ov.hidden = false;
  ov.innerHTML = `<div class="rv-card"><div class="rv-card-body">加载中...</div></div>`;
  // 点遮罩空白处关闭(点卡片内部不关)+ Esc 关闭(与同仓其它浮层惯例一致)
  ov.onclick = (e) => { if (e.target === ov) closeReturnVisitCard(); };
  document.addEventListener("keydown", _rvCardEsc);
  loadReturnVisitCard();
}

function closeReturnVisitCard() {
  _rvCardGen++;     // #734 关卡片:本代作废,重开同一回访也是新代,旧 onstop 必取消
  _rvCardId = "";
  // 关卡片时若在录音,必须停(释放麦克风+灭红点);否则录音器/麦克风流泄漏,后台继续录环境声
  if (_rvMicRec && _rvMicRec.state === "recording") { try { _rvMicRec.stop(); } catch { /* 忽略 */ } }
  const ov = rvCardOverlay();
  if (ov) { ov.hidden = true; ov.innerHTML = ""; }
  document.removeEventListener("keydown", _rvCardEsc);
}

async function loadReturnVisitCard() {
  const ov = rvCardOverlay();
  if (!ov) return;
  const rvId = _rvCardId, pid = _rvCardPid;
  let detail, calls = [];
  try {
    const [dR, kR] = await Promise.all([
      fetch(`/api/return-visits/${encodeURIComponent(rvId)}`),
      pid ? fetch(`/api/patients/${encodeURIComponent(pid)}/calls`) : Promise.resolve(null),
    ]);
    if (rvId !== _rvCardId) return;   // 期间关了/换了卡片 → 丢弃
    if (!dR.ok) { ov.innerHTML = rvCardShell("回访详情载入失败", ""); return; }
    detail = await dR.json();
    if (kR && kR.ok) {
      const kd = await kR.json();
      calls = (kd.calls || []).filter(c => c.linked_return_visit_id === rvId);
    }
    if (rvId !== _rvCardId) return;
  } catch {
    if (rvId === _rvCardId) ov.innerHTML = rvCardShell("回访详情载入失败", "");
    return;
  }
  renderReturnVisitCard(detail, calls);
}

function rvCardShell(title, bodyHtml) {
  return `<div class="rv-card">
    <div class="rv-card-head"><strong>${escapeHtml(title)}</strong>
      <button type="button" class="plain-button" onclick="closeReturnVisitCard()">×</button></div>
    <div class="rv-card-body">${bodyHtml}</div></div>`;
}

function renderReturnVisitCard(d, calls) {
  const ov = rvCardOverlay();
  if (!ov) return;
  const title = (d.display_name || "患者") + " · " + (d.item_name || "回访");
  const cur = d.status || "";
  const stdVals = RV_CARD_STATUS.map(([v]) => v);
  let statusOpts = RV_CARD_STATUS.map(([v, l]) =>
    `<option value="${escapeAttr(v)}"${v === cur ? " selected" : ""}>${escapeHtml(l)}</option>`).join("");
  // SaaS 导入的状态可能是 done/完成/4 等,不在标准两项里。补一个当前原值选项并选中,否则浏览器
  // 默认落到「待回访」→ 保存时把已办结回访静默改回待回访(数据损坏)。保留原值即不覆盖。
  if (cur && !stdVals.includes(cur)) {
    const label = (typeof returnVisitStatusText === "function") ? returnVisitStatusText(cur) : cur;
    statusOpts = `<option value="${escapeAttr(cur)}" selected>${escapeHtml(label)}（原值）</option>` + statusOpts;
  }
  const shots = (d.images || []).map(im =>
    `<span class="rv-shot"><a href="/api/return-visits/${encodeURIComponent(_rvCardId)}/images/${encodeURIComponent(im.image_id)}/file" target="_blank" rel="noopener">
       <img src="/api/return-visits/${encodeURIComponent(_rvCardId)}/images/${encodeURIComponent(im.image_id)}/file" alt="回访截图"></a>
     <button type="button" class="link-danger" onclick="deleteRvCardImage('${escapeAttr(im.image_id)}')">删</button></span>`).join("");
  const recs = (calls || []).map(k =>
    `<div class="rv-rec"><audio controls preload="none" src="/api/calls/${encodeURIComponent(k.call_id)}/recording"></audio>
     <button type="button" class="link-danger" onclick="deleteRvCardCall('${escapeAttr(k.call_id)}', ${Number(k.revision)})">删</button></div>`).join("");

  const body = `
    <div class="rv-card-meta">
      <span class="muted">回访时间 ${escapeHtml(d.due_time || "")}</span>
      ${d.return_doctor ? `<span class="muted">医生 ${escapeHtml(d.return_doctor)}</span>` : ""}
      ${d.visitor ? `<span class="muted">回访人 ${escapeHtml(d.visitor)}</span>` : ""}
    </div>
    <div class="cs-form" data-rv-card-form data-rv-card-revision="${escapeAttr(d.revision)}">
      <label class="cs-wide"><span>回访内容</span><input class="cs-input" data-rvf="item_name" value="${escapeAttr(d.item_name || "")}"></label>
      <label class="cs-wide"><span>回访结果</span><textarea class="cs-input cs-area" data-rvf="return_result">${escapeHtml(d.return_result || "")}</textarea></label>
      <div class="cs-grid">
        <label><span>状态</span><select class="cs-input" data-rvf="status">${statusOpts}</select></label>
      </div>
      <label class="cs-wide"><span>备注</span><input class="cs-input" data-rvf="note" value="${escapeAttr(d.note || "")}"></label>
      <div class="cs-actions">
        <button type="button" onclick="saveRvCard()">保存</button>
        <button type="button" class="plain-button" onclick="markRvCardDone()">标记已回访</button>
        <span data-rv-card-status></span>
      </div>
    </div>
    <div class="rv-card-sec"><div class="rv-card-sec-head">电话回访录音</div>
      <div class="rv-recs">${recs || '<span class="empty">暂无录音</span>'}</div>
      <div class="rv-up">
        <button type="button" data-rv-mic onclick="toggleRvCardMicRecord()">🎙 快捷录音</button>
        <span data-rv-mic-status class="muted" style="font-size:12px"></span>
      </div>
      <label class="rv-up"><span>或上传文件</span><input type="file" accept="audio/*" data-rv-audio>
        <button type="button" onclick="uploadRvCardAudio()">上传</button></label>
      <div class="muted" style="font-size:12px">快捷录音录本机麦克风环境声；电话回访请第二台设备免提录，或用诊室软电话。</div></div>
    <div class="rv-card-sec"><div class="rv-card-sec-head">微信回访截图</div>
      <div class="rv-shots">${shots || '<span class="empty">暂无截图</span>'}</div>
      <label class="rv-up"><span>上传截图</span><input type="file" accept="image/*" multiple data-rv-shot>
        <button type="button" onclick="uploadRvCardImages()">上传</button></label></div>`;
  ov.innerHTML = rvCardShell(title, body);
}

function rvCardForm() { const ov = rvCardOverlay(); return ov && ov.querySelector("[data-rv-card-form]"); }
function rvCardStatusEl() { const f = rvCardForm(); return f && f.querySelector("[data-rv-card-status]"); }

function rvCardRevision() {
  const form = rvCardForm();
  const revision = Number(form && form.dataset.rvCardRevision);
  return Number.isInteger(revision) && revision > 0 ? revision : 0;
}

function setRvCardRevision(revision) {
  const form = rvCardForm();
  if (form && Number.isInteger(Number(revision)) && Number(revision) > 0) {
    form.dataset.rvCardRevision = String(revision);
  }
}

async function rvAttachmentConflict() {
  alert("数据已被其他操作更新，请确认最新内容后重试");
  await loadReturnVisitCard();
}

async function saveRvCard() {
  const f = rvCardForm();
  if (!f) return;
  const st = rvCardStatusEl();
  const revision = Number(f.dataset.rvCardRevision);
  if (!Number.isInteger(revision) || revision < 1) {
    if (st) st.textContent = "保存失败：回访记录版本已失效，请刷新后重试";
    return;
  }
  const payload = {expected_revision: revision};
  f.querySelectorAll("[data-rvf]").forEach(i => { payload[i.dataset.rvf] = (i.value || "").trim(); });
  if (st) st.textContent = "保存中...";
  const rvId = _rvCardId;
  let res;
  try {
    res = await fetch(`/api/return-visits/${encodeURIComponent(rvId)}`, {
      method: "PUT", headers: { "Content-Type": "application/json" }, body: JSON.stringify(payload),
    });
  } catch { if (st) st.textContent = "保存失败（网络异常）"; return; }
  if (res.status === 409) {
    if (st) st.textContent = "数据已被其他操作更新，请确认最新内容后重试";
    if (typeof loadReturnVisitModule === "function") await loadReturnVisitModule();
    await loadReturnVisitCard();
    return;
  }
  if (!res.ok) { const m = await res.json().catch(() => ({})); if (st) st.textContent = "保存失败：" + (m.detail || res.status); return; }
  if (typeof loadReturnVisitModule === "function") loadReturnVisitModule();   // 列表同步刷新
  await loadReturnVisitCard();
}

function markRvCardDone() {
  const f = rvCardForm();
  if (!f) return;
  const s = f.querySelector('[data-rvf="status"]');
  if (s) s.value = "已回访";
  saveRvCard();
}

// 快捷录音:点一下开始录本机麦克风,再点停止即上传并挂到本回访(linked_return_visit_id)。
// #734 会话锁定用卡片代数:点击瞬间记 gen,开麦返回/onstop 时代数变了(关卡片/切卡片/重开)
// 一律永久取消,不再比对可能"变回相等"的回访/患者 ID(同 #700 口径的升级版)。
let _rvMicRec = null, _rvMicChunks = [], _rvMicStarting = false;

async function toggleRvCardMicRecord() {
  const ov = rvCardOverlay();
  const btn = ov && ov.querySelector("[data-rv-mic]");
  const status = ov && ov.querySelector("[data-rv-mic-status]");
  if (_rvMicRec && _rvMicRec.state === "recording") { _rvMicRec.stop(); return; }
  if (_rvMicStarting) return;   // 开麦是异步(权限框),防二次点击并发申请两条流致孤儿泄漏
  if (!_rvCardPid) { alert("该回访没有关联患者，无法录音"); return; }
  const revision = rvCardRevision();
  if (!revision) { alert("数据版本已失效，请刷新后重试"); await loadReturnVisitCard(); return; }
  const gen = _rvCardGen;               // #734 本次开麦会话绑定当前卡片代数
  const pid = _rvCardPid, rvId = _rvCardId;   // 上传目标在点击瞬间锁定
  _rvMicStarting = true;
  if (btn) { btn.disabled = true; btn.textContent = "正在开麦…"; }
  let stream;
  try { stream = await navigator.mediaDevices.getUserMedia({ audio: true }); }
  catch {
    _rvMicStarting = false;
    if (btn) { btn.disabled = false; btn.textContent = "🎙 快捷录音"; }
    if (status) status.textContent = "无法访问麦克风（请允许权限）";
    return;
  }
  _rvMicStarting = false;
  if (btn) btn.disabled = false;
  if (gen !== _rvCardGen) {   // #734 开麦期间关了/切了卡片(含重开同一回访) → 弃流,不起录音机
    stream.getTracks().forEach(t => t.stop());
    if (btn) btn.textContent = "🎙 快捷录音";
    return;
  }
  _rvMicChunks = [];
  const mime = MediaRecorder.isTypeSupported("audio/webm") ? "audio/webm" : "";
  _rvMicRec = mime ? new MediaRecorder(stream, { mimeType: mime }) : new MediaRecorder(stream);
  _rvMicRec.ondataavailable = e => { if (e.data && e.data.size) _rvMicChunks.push(e.data); };
  _rvMicRec.onstop = async () => {
    stream.getTracks().forEach(t => t.stop());
    if (btn) btn.textContent = "🎙 快捷录音";
    if (gen !== _rvCardGen) {   // #734 录音期间卡片代数变了 → 永久取消上传
      if (status) status.textContent = "已切换回访，已取消上传";
      return;
    }
    const blob = new Blob(_rvMicChunks, { type: "audio/webm" });
    const fd = new FormData();
    fd.append("file", blob, "rv-mic.webm");
    fd.append("direction", "outbound");
    fd.append("source", "return_visit_mobile");
    fd.append("linked_return_visit_id", rvId);
    fd.append("expected_revision", String(revision));
    let response;
    try { response = await fetch(`/api/patients/${encodeURIComponent(pid)}/calls`, { method: "POST", body: fd }); }
    catch { if (status) status.textContent = "录音上传失败，请重试"; return; }
    if (response.status === 409) { await rvAttachmentConflict(); return; }
    if (!response.ok) { if (status) status.textContent = "录音上传失败，请重试"; return; }
    const result = await response.json().catch(() => ({}));
    setRvCardRevision(result.context_revision);
    if (status) status.textContent = "";
    await loadReturnVisitCard();
  };
  _rvMicRec.start();
  if (btn) btn.textContent = "⏹ 停止录音";
  if (status) status.textContent = "录音中…（讲完点停止）";
}

async function uploadRvCardAudio() {
  if (!_rvCardPid) { alert("该回访没有关联患者，无法上传录音"); return; }   // 防 POST 到畸形 /api/patients//calls
  const ov = rvCardOverlay();
  const input = ov && ov.querySelector("[data-rv-audio]");
  const file = input && input.files && input.files[0];
  if (!file) { alert("请先选择录音文件"); return; }
  const revision = rvCardRevision();
  if (!revision) { alert("数据版本已失效，请刷新后重试"); await loadReturnVisitCard(); return; }
  const fd = new FormData();
  fd.append("file", file, file.name);
  fd.append("direction", "outbound");
  fd.append("linked_return_visit_id", _rvCardId);
  fd.append("expected_revision", String(revision));
  let response;
  try { response = await fetch(`/api/patients/${encodeURIComponent(_rvCardPid)}/calls`, { method: "POST", body: fd }); }
  catch { alert("录音上传失败，请重试"); return; }
  if (response.status === 409) { await rvAttachmentConflict(); return; }
  if (!response.ok) { alert("录音上传失败，请重试"); return; }
  const result = await response.json().catch(() => ({}));
  setRvCardRevision(result.context_revision);
  await loadReturnVisitCard();
}

async function uploadRvCardImages() {
  const ov = rvCardOverlay();
  const input = ov && ov.querySelector("[data-rv-shot]");
  const files = (input && input.files) || [];
  if (!files.length) { alert("请先选择截图"); return; }
  let revision = rvCardRevision();
  if (!revision) { alert("数据版本已失效，请刷新后重试"); await loadReturnVisitCard(); return; }
  let fail = 0;
  for (const fchosen of files) {
    const fd = new FormData();
    fd.append("file", fchosen, fchosen.name);
    fd.append("expected_revision", String(revision));
    let response;
    try { response = await fetch(`/api/return-visits/${encodeURIComponent(_rvCardId)}/images`, { method: "POST", body: fd }); }
    catch { fail++; break; }
    if (response.status === 409) { await rvAttachmentConflict(); return; }
    if (!response.ok) { fail++; break; }
    const result = await response.json().catch(() => ({}));
    const nextRevision = Number(result.revision);
    if (!Number.isInteger(nextRevision) || nextRevision <= revision) { fail++; break; }
    revision = nextRevision;
    setRvCardRevision(revision);
  }
  if (fail > 0) alert("有 " + fail + " 张截图上传失败，请重试");
  await loadReturnVisitCard();
}

async function deleteRvCardImage(imageId) {
  if (!confirm("删除这张截图？")) return;
  const revision = rvCardRevision();
  if (!revision) { alert("数据版本已失效，请刷新后重试"); await loadReturnVisitCard(); return; }
  let res;
  try {
    res = await fetch(`/api/return-visits/${encodeURIComponent(_rvCardId)}/images/${encodeURIComponent(imageId)}`, {
      method: "DELETE", headers: {"Content-Type": "application/json"},
      body: JSON.stringify({expected_revision: revision}),
    });
  }
  catch { alert("删除失败（网络异常）"); return; }
  if (res.status === 409) { await rvAttachmentConflict(); return; }
  if (!res.ok) { alert("删除失败（" + res.status + "）"); return; }
  const body = await res.json().catch(() => ({}));
  setRvCardRevision(body.revision);
  if (body && body.file_removed === false) alert("记录已删，但截图文件未能从磁盘删除（已记审计待清理）。");
  await loadReturnVisitCard();
}

async function deleteRvCardCall(callId, revision) {
  if (!confirm("删除这条录音？")) return;
  if (!Number.isInteger(revision) || revision < 1) {
    alert("数据版本已失效，请刷新后重试");
    await loadReturnVisitCard();
    return;
  }
  let res;
  try {
    res = await fetch(`/api/calls/${encodeURIComponent(callId)}`, {
      method: "DELETE", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ expected_revision: revision }),
    });
  }
  catch { alert("删除失败（网络异常）"); return; }
  if (res.status === 409) {
    alert("数据已被其他操作更新，请确认最新内容后重试");
    await loadReturnVisitCard();
    return;
  }
  if (!res.ok) { alert("删除失败（" + res.status + "）"); return; }
  const body = await res.json().catch(() => ({}));
  if (body && body.file_removed === false) alert("记录已删，但录音文件未能从磁盘删除（已记审计待清理）。");
  await loadReturnVisitCard();
}

Object.assign(window, {
  openReturnVisitCard, closeReturnVisitCard, saveRvCard, markRvCardDone,
  toggleRvCardMicRecord, uploadRvCardAudio, uploadRvCardImages, deleteRvCardImage, deleteRvCardCall,
});
