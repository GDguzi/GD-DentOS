// 会员储值 tab：余额 + 充值/消费/退储值 + 流水。对接后端 /api/patients/{pid}/member-account。
// 关闭会员开关时只显示提示、不出操作按钮（后端也会 403 兜底）。data-* + 事件委托，不拼内联JS。

const MEMBER_TXN_LABEL = {topup: "充值", consume: "消费", refund: "退储值"};

async function renderWorkspaceMemberTab(panel) {
  const pid = workspacePatientId;
  panel.innerHTML = `<div class="module-loading">会员储值载入中...</div>`;
  let enabled = true, acc = null;
  try {
    const [fRes, aRes] = await Promise.all([
      fetch("/api/settings/features"),
      fetch(`/api/patients/${encodeURIComponent(pid)}/member-account`),
    ]);
    if (pid !== workspacePatientId) return;   // 竞态守卫：已换患者
    enabled = fRes.ok ? (await fRes.json()).membership_enabled !== false : true;
    acc = aRes.ok ? await aRes.json() : null;
    if (pid !== workspacePatientId) return;
  } catch {
    if (pid !== workspacePatientId) return;
    panel.innerHTML = `<div class="module-loading">会员储值载入失败（网络异常）</div>`;
    return;
  }
  if (!acc) { panel.innerHTML = `<div class="module-loading">会员储值载入失败</div>`; return; }

  const balance = acc.balance != null ? acc.balance : 0;
  const actions = enabled ? `
    <div class="member-actions">
      <button type="button" class="tooth-confirm-btn" data-member-act="topup">充值</button>
      <button type="button" class="plain-button" data-member-act="consume">消费扣减</button>
      <button type="button" class="plain-button" data-member-act="refund">退储值（退现）</button>
      <span class="member-status" data-member-status></span>
    </div>` : `<div class="member-disabled">会员储值未启用（可在设置里开启）。下方为历史记录。</div>`;

  const txns = acc.transactions || [];
  const rows = txns.map(t => {
    const sign = t.txn_type === "topup" ? "+" : "−";
    return `<tr>
      <td>${escapeHtml(String(t.created_at || "").slice(0, 16))}</td>
      <td>${escapeHtml(MEMBER_TXN_LABEL[t.txn_type] || t.txn_type || "")}</td>
      <td style="text-align:right">${sign}${escapeHtml(String(t.amount))}</td>
      <td style="text-align:right">${escapeHtml(String(t.balance_after))}</td>
      <td>${escapeHtml(t.note || "")}</td>
    </tr>`;
  }).join("");

  panel.innerHTML = `
    <div class="member-card">
      <span class="member-balance-label">当前储值余额</span>
      <span class="member-balance">¥${escapeHtml(String(balance))}</span>
    </div>
    ${actions}
    <div class="member-ledger">
      <div class="module-head"><strong>储值流水</strong><span>${txns.length} 笔</span></div>
      <table class="wh-table">
        <thead><tr><th>时间</th><th>类型</th><th>金额</th><th>后余额</th><th>备注</th></tr></thead>
        <tbody>${rows || '<tr><td colspan="5" class="module-empty">暂无流水</td></tr>'}</tbody>
      </table>
    </div>`;

  panel.querySelectorAll("[data-member-act]").forEach(btn =>
    btn.addEventListener("click", () => memberAction(btn.dataset.memberAct, pid, panel)));
}

// 网络异常(结果未知)的请求号暂存:同患者同动作同载荷(金额+备注,与后端整体哈希口径一致)重试时
// 复用同号(对齐 #800 弹窗内重试复用),后端同号重放不落第二笔——否则"已入账但响应丢了"的
// 超时场景,重试就是新号照样重复扣。载荷不同=新意图新号。
const _memberPendingReq = {};
// 进行中请求闸:同患者同动作的请求未返回前,拒绝再次提交——否则第二次是新号,后端视为两笔合法交易
const _memberInFlight = new Set();

async function memberAction(act, pid, panel) {
  const status = panel.querySelector("[data-member-status]");
  // 闸门在首个弹窗前占位:两次操作并行停在弹窗阶段时后者也必须被拒——
  // 若等弹窗结束才占位,前者完成释放后,后者仍会拿新号再入一笔。整段流程 finally 释放。
  const reqKey = `${pid}|${act}`;
  if (_memberInFlight.has(reqKey)) {
    if (status) status.textContent = "上一笔正在处理中，请稍候";
    return;
  }
  _memberInFlight.add(reqKey);
  try {
    const label = {topup: "充值", consume: "消费扣减", refund: "退储值"}[act];
    const amountStr = await appPrompt(`${label}金额（元）：`, "");
    if (amountStr === null) return;
    const amount = parseFloat(amountStr);
    if (!(amount > 0)) { window.alert("请输入大于 0 的金额"); return; }
    let note = "";
    if (act !== "consume") {
      const rawNote = await appPrompt("备注（可空）：", "");
      if (rawNote === null) return;   // 取消备注=放弃整笔(改钱不能误提交)
      note = rawNote;
    }
    if (status) status.textContent = "处理中...";
    // 审计R4:改钱端点强制幂等键,复用 #800 的 newPaymentRequestId(workspace_billing.js,先于本文件加载)。
    const pending = _memberPendingReq[reqKey];
    const requestId = (pending && pending.amount === amount && pending.note === note)
      ? pending.requestId : newPaymentRequestId();
    let res;
    try {
      res = await fetch(`/api/patients/${encodeURIComponent(pid)}/member-account/${act}`, {
        method: "POST", headers: {"Content-Type": "application/json"},
        body: JSON.stringify({amount, note, request_id: requestId}),
      });
    } catch {
      _memberPendingReq[reqKey] = {amount, note, requestId};   // 结果未知:同载荷重试复用同号
      if (status) status.textContent = "失败（网络异常）";
      return;
    }
    delete _memberPendingReq[reqKey];   // 有明确响应(成功或报错)即翻篇,下次是新意图新号
    if (!res.ok) { const m = await res.json().catch(() => ({})); if (status) status.textContent = "失败：" + (m.detail || res.status); return; }
    if (pid === workspacePatientId) renderWorkspaceMemberTab(panel);   // 刷新余额/流水
    if (typeof loadAuditLogs === "function") loadAuditLogs();
  } finally {
    _memberInFlight.delete(reqKey);
  }
}

Object.assign(window, { renderWorkspaceMemberTab, memberAction });
