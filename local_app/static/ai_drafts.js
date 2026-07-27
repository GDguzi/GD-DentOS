// AI 草稿箱视图：统计条 + 待审草稿列表 + 待认领区，审核界面复用 medical_editor.js 的 renderMedicalRecord。
// 由侧边栏 data-workspace-view="ai-drafts" 经 app.js 的 switchWorkspaceView → loadAiDrafts 进入。

const aiDraftsPanel = document.getElementById("aiDraftsPanel");

// 竞态守卫：每次加载自增 token，过期的 await 延续不得再渲染（快速切视图/连点动作时）。
let aiDraftsToken = 0;
// 缓存最近一次列表数据，审核界面据 record_id 取草稿行（避免再次 fetch）。
let aiDraftsCache = {drafts: [], unclaimed: []};

function aiDraftPcExcerpt(content, limit = 40) {
  const pc = content && typeof content === "object" ? content.PC || "" : "";
  const text = String(pc).replace(/\s+/g, " ").trim();
  return text.length > limit ? `${text.slice(0, limit)}…` : text;
}

async function loadAiDrafts() {
  if (!aiDraftsPanel) return;
  const token = ++aiDraftsToken;
  aiDraftsPanel.innerHTML = '<div class="ai-draft-loading">草稿箱载入中...</div>';
  let data;
  try {
    const res = await fetch("/api/ai-drafts");
    if (!res.ok) throw new Error(String(res.status));
    data = await res.json();
  } catch {
    if (token !== aiDraftsToken) return;
    aiDraftsPanel.innerHTML = '<div class="ai-draft-empty">草稿箱载入失败，请重试</div>';
    return;
  }
  if (token !== aiDraftsToken) return;
  aiDraftsCache = {drafts: data.drafts || [], unclaimed: data.unclaimed || []};
  renderAiDrafts(data);
}

function renderAiDrafts(data) {
  if (!aiDraftsPanel) return;
  const drafts = data.drafts || [];
  const unclaimed = data.unclaimed || [];
  aiDraftsPanel.innerHTML = `
    ${renderAiDraftStats(data.stats || {})}
    <section class="ai-draft-section">
      <h2 class="ai-draft-section-title">待审草稿（${escapeHtml(formatCount(drafts.length))}）</h2>
      <div class="ai-draft-list">
        ${drafts.length ? drafts.map(renderAiDraftCard).join("") : '<div class="ai-draft-empty">暂无待审草稿</div>'}
      </div>
    </section>
    <section class="ai-draft-section">
      <h2 class="ai-draft-section-title">待认领（${escapeHtml(formatCount(unclaimed.length))}）</h2>
      <div class="ai-unclaimed-list">
        ${unclaimed.length ? unclaimed.map(renderAiUnclaimedCard).join("") : '<div class="ai-draft-empty">暂无待认领草稿</div>'}
      </div>
    </section>
  `;
}

function renderAiDraftStats(stats) {
  const generated = Number(stats.generated || 0);
  const confirmed = Number(stats.confirmed || 0);
  const noEdit = Number(stats.no_edit || 0);
  const pending = Number(stats.pending || 0);
  const discarded = Number(stats.discarded || 0);
  // 确认率 = 已确认 ÷ 生成数；无改率 = 无改 ÷ 已确认（已确认才有改/不改之分，无确认时退回生成数兜底为 0）。
  const confirmRate = generated ? Math.round((confirmed / generated) * 100) : 0;
  const noEditRate = confirmed ? Math.round((noEdit / confirmed) * 100) : 0;
  const items = [
    ["生成数", formatCount(generated)],
    ["确认率", `${confirmRate}%`],
    ["无改率", `${noEditRate}%`],
    ["待审数", formatCount(pending)],
    ["废弃数", formatCount(discarded)],
  ];
  return `
    <div class="ai-draft-stats" aria-label="近 7 天 AI 草稿统计">
      <div class="ai-draft-stats-head">近 7 天统计</div>
      <div class="ai-draft-stats-grid">
        ${items.map(([label, value]) => `
          <div class="ai-draft-stat">
            <span class="ai-draft-stat-label">${escapeHtml(label)}</span>
            <strong class="ai-draft-stat-value">${escapeHtml(value)}</strong>
          </div>
        `).join("")}
      </div>
    </div>
  `;
}

function renderAiDraftCard(draft) {
  const meta = [
    draft.visit_time && `就诊 ${draft.visit_time}`,
    draft.room && `诊室 ${draft.room}`,
    draft.doctor_name && `医生 ${draft.doctor_name}`,
  ].filter(Boolean).join(" · ");
  return `
    <div class="ai-draft-card" data-ai-draft-id="${escapeAttr(draft.record_id)}">
      <div class="ai-draft-card-head">
        <strong class="ai-draft-card-name">${escapeHtml(draft.display_name || "(无名)")}</strong>
        <button type="button" class="plain-button ai-draft-review-btn" onclick="openAiDraftReview('${escapeAttr(draft.record_id)}')">审核</button>
      </div>
      <div class="ai-draft-card-meta">${escapeHtml(meta)}</div>
      <div class="ai-draft-card-pc">${escapeHtml(aiDraftPcExcerpt(draft.content_json) || "（无主诉）")}</div>
    </div>
  `;
}

function renderAiUnclaimedCard(item) {
  const meta = [
    item.visit_time && `就诊 ${item.visit_time}`,
    item.room && `诊室 ${item.room}`,
    item.doctor_name && `医生 ${item.doctor_name}`,
  ].filter(Boolean).join(" · ");
  return `
    <div class="ai-unclaimed-card" data-ai-unclaimed-id="${escapeAttr(item.unclaimed_id)}">
      <div class="ai-unclaimed-card-head">
        <strong class="ai-unclaimed-card-code">${escapeHtml(item.code_name || "（无代号）")}</strong>
        <span class="ai-unclaimed-card-tag">待认领</span>
      </div>
      <div class="ai-draft-card-meta">${escapeHtml(meta)}</div>
      <div class="ai-draft-card-pc">${escapeHtml(aiDraftPcExcerpt(item.content_json) || "（无主诉）")}</div>
      <div class="ai-unclaimed-claim" data-ai-claim-box="${escapeAttr(item.unclaimed_id)}">
        <button type="button" class="plain-button" onclick="openAiClaimSearch('${escapeAttr(item.unclaimed_id)}')">认领到患者</button>
        <button type="button" class="plain-button" onclick="discardAiUnclaimed('${escapeAttr(item.unclaimed_id)}')">废弃</button>
      </div>
    </div>
  `;
}

// ---------- 审核界面（草稿编辑）----------

function findAiDraft(recordId) {
  return aiDraftsCache.drafts.find(row => String(row.record_id) === String(recordId)) || null;
}

// 打开审核：复用 renderMedicalRecord 渲染 content_json + 牙位。
// renderMedicalRecord 期望 content_json/tooth_json 为字符串，API 返回 content_json 为对象、无 tooth_json，故归一化。
function openAiDraftReview(recordId) {
  const draft = findAiDraft(recordId);
  const card = aiDraftsPanel
    ? aiDraftsPanel.querySelector(`.ai-draft-card[data-ai-draft-id="${cssEscape(recordId)}"]`)
    : null;
  if (!draft || !card) return;
  const row = {
    record_id: draft.record_id,
    record_type: draft.record_type || "",
    doctor_name: draft.doctor_name || "",
    visit_time: draft.visit_time || "",
    content_json: JSON.stringify(draft.content_json || {}),
    tooth_json: draft.tooth_json || "{}",
  };
  card.innerHTML = `
    <div class="ai-draft-review" data-ai-review-id="${escapeAttr(recordId)}">
      ${renderMedicalRecord(row)}
      <div class="ai-draft-review-actions">
        <button type="button" onclick="guardSubmit(this, () => confirmAiDraft('${escapeAttr(recordId)}', false))">确认</button>
        <button type="button" onclick="guardSubmit(this, () => confirmAiDraft('${escapeAttr(recordId)}', true))">保存修改并确认</button>
        <button type="button" onclick="discardAiDraft('${escapeAttr(recordId)}')">废弃</button>
        <button type="button" class="plain-button" onclick="loadAiDrafts()">取消</button>
        <span class="ai-draft-review-status" id="aiDraftStatus-${escapeAttr(recordId)}"></span>
      </div>
    </div>
  `;
}

// 从编辑器 DOM 读取 content_json / tooth_json（与 medical_editor.js 的 saveMedicalRecord 同口径）。
function collectAiDraftEditor(recordId) {
  const container = aiDraftsPanel.querySelector(`.medical-record-editor[data-record-id="${cssEscape(recordId)}"]`);
  if (!container) return null;
  const contentJson = {};
  container.querySelectorAll("[data-medical-field]").forEach(input => {
    const value = input.value.trim();
    if (value) contentJson[input.dataset.medicalField] = value;
  });
  const rawTooth = container.querySelector('[data-record-field="tooth_json"]').value.trim();
  let toothJson = {};
  try {
    toothJson = rawTooth ? JSON.parse(rawTooth) : {};
  } catch {
    return {error: "牙位JSON格式错误"};
  }
  return {
    content_json: contentJson,
    tooth_json: toothJson,
    record_type: container.querySelector('[data-record-field="record_type"]').value.trim(),
    doctor_name: container.querySelector('[data-record-field="doctor_name"]').value.trim(),
    visit_time: container.querySelector('[data-record-field="visit_time"]').value.trim(),
  };
}

function aiDraftStatusEl(recordId) {
  return document.getElementById(`aiDraftStatus-${recordId}`);
}

// 确认：edited=false 用草稿原始 content_json 直接确认；edited=true 带改后的 content/tooth。
async function confirmAiDraft(recordId, edited) {
  const status = aiDraftStatusEl(recordId);
  const draft = findAiDraft(recordId);
  if (!draft) return;
  let payload;
  if (edited) {
    const collected = collectAiDraftEditor(recordId);
    if (!collected) return;
    if (collected.error) {
      if (status) status.textContent = collected.error;
      return;
    }
    payload = collected;
  } else {
    payload = {content_json: draft.content_json || {}};
  }
  if (status) status.textContent = "提交中...";
  let ok = false;
  try {
    const res = await fetch(`/api/ai-drafts/${encodeURIComponent(recordId)}/confirm`, {
      method: "POST",
      headers: {"Content-Type": "application/json"},
      body: JSON.stringify(payload),
    });
    ok = res.ok;
  } catch {
    ok = false;
  }
  if (!ok) {
    if (status) status.textContent = "确认失败，请重试";
    return;
  }
  await loadAiDrafts();
}

async function discardAiDraft(recordId) {
  if (!confirm("确认废弃这条草稿？废弃后不再出现在待审列表。")) return;
  const status = aiDraftStatusEl(recordId);
  if (status) status.textContent = "提交中...";
  let ok = false;
  try {
    const res = await fetch(`/api/ai-drafts/${encodeURIComponent(recordId)}/discard`, {method: "POST"});
    ok = res.ok;
  } catch {
    ok = false;
  }
  if (!ok) {
    if (status) status.textContent = "废弃失败，请重试";
    return;
  }
  await loadAiDrafts();
}

// ---------- 待认领：认领患者搜索 ----------

function openAiClaimSearch(unclaimedId) {
  const box = aiDraftsPanel
    ? aiDraftsPanel.querySelector(`[data-ai-claim-box="${cssEscape(unclaimedId)}"]`)
    : null;
  if (!box) return;
  box.innerHTML = `
    <div class="ai-claim-search">
      <input type="text" class="ai-claim-input" id="aiClaimInput-${escapeAttr(unclaimedId)}" placeholder="搜索患者姓名 / 电话 / ID">
      <button type="button" class="plain-button" onclick="searchAiClaimPatients('${escapeAttr(unclaimedId)}')">搜索</button>
      <button type="button" class="plain-button" onclick="loadAiDrafts()">取消</button>
      <span class="ai-claim-status" id="aiClaimStatus-${escapeAttr(unclaimedId)}"></span>
      <div class="ai-claim-results" id="aiClaimResults-${escapeAttr(unclaimedId)}"></div>
    </div>
  `;
  const input = document.getElementById(`aiClaimInput-${unclaimedId}`);
  if (input) {
    input.focus();
    input.addEventListener("keydown", event => {
      if (event.key === "Enter") searchAiClaimPatients(unclaimedId);
    });
  }
}

async function searchAiClaimPatients(unclaimedId) {
  const input = document.getElementById(`aiClaimInput-${unclaimedId}`);
  const results = document.getElementById(`aiClaimResults-${unclaimedId}`);
  const status = document.getElementById(`aiClaimStatus-${unclaimedId}`);
  if (!input || !results) return;
  const query = input.value.trim();
  if (!query) {
    if (status) status.textContent = "请输入搜索词";
    return;
  }
  if (status) status.textContent = "搜索中...";
  let data;
  try {
    const res = await fetch(`/api/patients?q=${encodeURIComponent(query)}&pagesize=20`);
    if (!res.ok) throw new Error(String(res.status));
    data = await res.json();
  } catch {
    if (status) status.textContent = "搜索失败，请重试";
    return;
  }
  const rows = data.list || [];
  if (status) status.textContent = `${rows.length} 条`;
  results.innerHTML = rows.length
    ? rows.map(p => {
        const sub = [p.phone, p.patient_identity].filter(Boolean).join(" | ");
        return `
          <button type="button" class="ai-claim-result" onclick="claimAiUnclaimed('${escapeAttr(unclaimedId)}', '${escapeAttr(p.patient_identity)}')">
            <span class="ai-claim-result-name">${escapeHtml(p.display_name || "(无名)")}</span>
            <span class="ai-claim-result-sub">${escapeHtml(sub)}</span>
          </button>
        `;
      }).join("")
    : '<div class="ai-draft-empty">没有匹配的患者</div>';
}

async function claimAiUnclaimed(unclaimedId, patientIdentity) {
  const status = document.getElementById(`aiClaimStatus-${unclaimedId}`);
  if (status) status.textContent = "认领中...";
  let ok = false;
  try {
    const res = await fetch(`/api/ai-unclaimed/${encodeURIComponent(unclaimedId)}/claim`, {
      method: "POST",
      headers: {"Content-Type": "application/json"},
      body: JSON.stringify({patient_identity: patientIdentity}),
    });
    ok = res.ok;
  } catch {
    ok = false;
  }
  if (!ok) {
    if (status) status.textContent = "认领失败，请重试";
    return;
  }
  await loadAiDrafts();
}

async function discardAiUnclaimed(unclaimedId) {
  if (!confirm("确认废弃这条无主草稿？")) return;
  let ok = false;
  try {
    const res = await fetch(`/api/ai-unclaimed/${encodeURIComponent(unclaimedId)}/discard`, {method: "POST"});
    ok = res.ok;
  } catch {
    ok = false;
  }
  if (!ok) {
    const box = aiDraftsPanel
      ? aiDraftsPanel.querySelector(`[data-ai-claim-box="${cssEscape(unclaimedId)}"]`)
      : null;
    if (box) box.insertAdjacentHTML("beforeend", '<span class="ai-claim-status">废弃失败，请重试</span>');
    return;
  }
  await loadAiDrafts();
}

Object.assign(window, {
  loadAiDrafts,
  renderAiDrafts,
  renderAiDraftStats,
  renderAiDraftCard,
  renderAiUnclaimedCard,
  openAiDraftReview,
  collectAiDraftEditor,
  confirmAiDraft,
  discardAiDraft,
  openAiClaimSearch,
  searchAiClaimPatients,
  claimAiUnclaimed,
  discardAiUnclaimed,
});
