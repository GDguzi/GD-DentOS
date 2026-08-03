const _checkInPending = new Map();

function checkInPatient({
  patientIdentity,
  displayName = "",
  openTriageAfter = false,
  switchToToday = false,
  showSuccess = true,
} = {}) {
  const pid = String(patientIdentity || "").trim();
  if (!pid) {
    window.alert("挂号失败：缺少患者标识");
    return Promise.resolve(null);
  }
  if (_checkInPending.has(pid)) return _checkInPending.get(pid);

  const pending = _runCheckIn({
    patientIdentity: pid,
    displayName: String(displayName || "").trim(),
    openTriageAfter,
    switchToToday,
    showSuccess,
  }).finally(() => _checkInPending.delete(pid));
  _checkInPending.set(pid, pending);
  return pending;
}

async function _postCheckIn(patientIdentity, appointmentId = "") {
  return fetch(`/api/patients/${encodeURIComponent(patientIdentity)}/check-in`, {
    method: "POST",
    headers: {"Content-Type": "application/json"},
    body: JSON.stringify(appointmentId ? {appointment_id: appointmentId} : {}),
  });
}

async function _checkInBody(response) {
  try {
    return await response.json();
  } catch {
    return {};
  }
}

function _checkInErrorMessage(data, status) {
  const detail = data && data.detail;
  if (typeof detail === "string" && detail.trim()) return detail.trim();
  if (detail && typeof detail.message === "string" && detail.message.trim()) {
    return detail.message.trim();
  }
  if (detail && detail.code === "check_in_candidate_stale") {
    return "所选预约已失效，请重新挂号";
  }
  if (data && typeof data.message === "string" && data.message.trim()) {
    return data.message.trim();
  }
  return `HTTP ${status}`;
}

function _checkInCandidatePrompt(detail, displayName) {
  const candidates = Array.isArray(detail.candidates) ? detail.candidates : [];
  const patientText = displayName ? `${displayName}今天有多条预约` : "患者今天有多条预约";
  const detailHeading = (typeof detail.message === "string" && detail.message.trim())
    ? detail.message.trim()
    : `${patientText}，请选择要挂号的一条：`;
  const heading = displayName ? `${displayName}：${detailHeading}` : detailHeading;
  const rows = candidates.map((candidate, index) => {
    const time = candidate.start_time || "时间未注明";
    const doctor = candidate.doctor_name || "未指定医生";
    const item = candidate.item_name || "未指定项目";
    const suspect = candidate.suspect_cancelled ? " · 疑似取消" : "";
    return `${index + 1}. 时间：${time} · 医生：${doctor} · 项目：${item}${suspect}`;
  });
  return `${heading}\n\n${rows.join("\n")}\n\n请输入 1-${candidates.length}；留空或取消则停止挂号。`;
}

async function _chooseCheckInCandidate(detail, displayName) {
  const candidates = Array.isArray(detail.candidates) ? detail.candidates : [];
  if (!candidates.length) {
    window.alert(`挂号失败：${_checkInErrorMessage({detail}, 409)}`);
    return null;
  }

  const message = _checkInCandidatePrompt(detail, displayName);
  while (true) {
    const answer = await appPrompt(message, "");
    if (answer === null || String(answer).trim() === "") {
      window.alert("已取消挂号：未选择今日预约");
      return null;
    }
    const value = String(answer).trim();
    if (!/^\d+$/.test(value)) continue;
    const index = Number(value) - 1;
    if (index < 0 || index >= candidates.length) continue;
    const appointmentId = candidates[index].appointment_id;
    if (!appointmentId) {
      window.alert("挂号失败：候选预约缺少标识");
      return null;
    }
    return String(appointmentId);
  }
}

async function _finishCheckIn(data, options) {
  let followUp = "刷新今日候诊";
  try {
    const appointment = data.appointment;
    if (typeof evictVisitsCache === "function") {
      evictVisitsCache(options.patientIdentity);
    }
    if (
      options.switchToToday &&
      typeof workDate !== "undefined" &&
      workDate &&
      typeof localDateValue === "function"
    ) {
      workDate.value = localDateValue();
    }

    if ((await loadTodayWork()) !== true) {
      throw new Error("today work refresh failed");
    }
    if (options.openTriageAfter) {
      followUp = "打开分诊";
      await openTriage(appointment.appointment_id, appointment);
    }
    if (options.switchToToday) {
      followUp = "切换今日工作台";
      if ((await switchWorkspaceView("today")) !== true) {
        throw new Error("today workspace switch failed");
      }
    }
  } catch {
    window.alert(`患者已挂号，但后续${followUp}失败，请手动继续处理`);
    return null;
  }
  if (options.showSuccess && !options.openTriageAfter) {
    window.alert(data.already_arrived
      ? "患者今天已挂号"
      : "挂号成功，已进入今日候诊");
  }
  return data;
}

async function _runCheckIn(options) {
  let response;
  try {
    response = await _postCheckIn(options.patientIdentity);
  } catch {
    window.alert("挂号失败：网络异常");
    return null;
  }

  while (true) {
    const data = await _checkInBody(response);
    if (response.ok) return _finishCheckIn(data, options);

    const detail = data && data.detail;
    if (
      response.status === 409 &&
      detail &&
      detail.code === "multiple_check_in_candidates"
    ) {
      const appointmentId = await _chooseCheckInCandidate(detail, options.displayName);
      if (!appointmentId) return null;
      try {
        response = await _postCheckIn(options.patientIdentity, appointmentId);
      } catch {
        window.alert("挂号失败：网络异常");
        return null;
      }
      continue;
    }

    window.alert(`挂号失败：${_checkInErrorMessage(data, response.status)}`);
    return null;
  }
}
