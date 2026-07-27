// 内窥镜口内照拍摄模态：左=实时画面，右=牙弓示意图(高亮该机位要拍的牙)，拍完自动跳下一张。
// 安全上下文(HTTPS/localhost)外不可用,降级提示去设 Chrome 标志。患者影像绝不出本机。
// slot→category/牙位 的权威映射在后端;此处机位表(含牙位)仅用于引导示意图+主诉牙手选。
// 硬件按键:手柄拍照键=HID键盘键,学习式映射(⚙按钮记键码,存localStorage endoShootKey,本机生效)。
// 相机小助手模式(,诊室1):本机 EndoHelper.exe 独占相机,页面吃它的 MJPEG 流+WS按键事件,
// 不走 getUserMedia(也就不需要安全上下文)。手柄键只报"按了一次"→单击拍照/双击录像开停。

// 顺序引导机位(拍完自动前进);teeth=该机位要拍的牙(主诉牙特写为空,拍时手选)
const ENDO_SLOTS = [
  {slot: "正面咬合", teeth: "13,12,11,21,22,23,43,42,41,31,32,33"},
  {slot: "右侧咬合", teeth: "18,17,16,15,14,48,47,46,45,44"},
  {slot: "左侧咬合", teeth: "24,25,26,27,28,34,35,36,37,38"},
  {slot: "上颌右·组1", teeth: "11,12,13,14"},
  {slot: "上颌右·组2", teeth: "15,16,17,18"},
  {slot: "上颌左·组1", teeth: "21,22,23,24"},
  {slot: "上颌左·组2", teeth: "25,26,27,28"},
  {slot: "下颌右·组1", teeth: "41,42,43,44"},
  {slot: "下颌右·组2", teeth: "45,46,47,48"},
  {slot: "下颌左·组1", teeth: "31,32,33,34"},
  {slot: "下颌左·组2", teeth: "35,36,37,38"},
  {slot: "主诉牙特写①", teeth: "", manual: true},
  {slot: "主诉牙特写②", teeth: "", manual: true},
];
const ENDO_UPPER = [18,17,16,15,14,13,12,11,21,22,23,24,25,26,27,28];
const ENDO_LOWER = [48,47,46,45,44,43,42,41,31,32,33,34,35,36,37,38];

let _endoPatientId = null;
let _endoPanel = null;
let _endoStream = null;
let _endoDeviceId = null;
let _endoIdx = 0;             // 当前机位下标
let _endoShots = [];          // [{slot, blob, url, tooth, status}]
let _endoTooth = {};          // slot -> 主诉牙手选牙位
let _endoShootKey = null;     // 手柄拍照键的 KeyboardEvent.code(如 "Enter"/"F13")
let _endoKeyLearning = false; // true=下一个按键被记为手柄键
let _endoLastKeyMs = 0;       // 上次手柄键触发时刻(防连拍)
let _endoRec = null;          // 录像中的 MediaRecorder(null=没在录)
let _endoRecTimer = null;     // 录像计时器(按钮上跳秒)
let _endoRecT0 = 0;           // 录像开始时刻

// 相机小助手(EndoHelper.exe,本机127.0.0.1:8710)
const ENDO_HELPER_BASE = "http://127.0.0.1:8710";
let _endoHelper = null;              // /health 返回体(非null=小助手模式)
let _endoHelperWs = null;            // 按键事件 WebSocket
let _endoHelperHelloTs = 0;          // 收到 hello 时刻(启动残留触发过滤窗)
let _endoHelperCanvas = null;        // 录像用画布(captureStream 喂 MediaRecorder)
let _endoHelperDrawTimer = null;     // 录像时把 MJPEG img 描到画布的循环
let _endoHelperWsDropped = false;    // WS 断过线(重连成功后视频流也要重拉,MJPEG img 不会自己恢复)

// 选录像容器格式:优先 mp4(下载给患者手机直接放),不支持再退 webm(纯函数,test_endo_rec_mime.mjs)
function endoRecMimePick(isSupported) {
  const prefs = ["video/mp4", "video/webm;codecs=vp9", "video/webm"];
  for (const m of prefs) { if (isSupported(m)) return m; }
  return "";
}

// 手柄键手势:短按=拍照,长按0.6秒=开始录像,录像中短按=停止(手柄前后两个椭圆键都发同一键码)
const ENDO_LONGPRESS_MS = 600;
let _endoKeyHeld = false;    // 手柄键正被按住
let _endoLongFired = false;  // 本次按住已触发长按开录,keyup 不再当短按
let _endoLongTimer = null;   // 长按计时器

// keydown 决策(纯函数,test_endo_shoot_key.mjs):学习态记键;命中已映射键且非系统连发→开始跟踪本次按住
function endoKeyDownDecision(learning, savedCode, code, repeat) {
  if (learning) return "learn";
  if (!savedCode || code !== savedCode) return null;
  if (repeat) return null;
  return "track";
}

// keyup 决策(纯函数):长按已开录→吞掉;录像中短按→停录;距上次拍照<400ms→防连拍;否则拍照
function endoKeyUpDecision(recording, longFired, nowMs, lastMs) {
  if (longFired) return "consume";
  if (recording) return "recStop";
  if (nowMs - lastMs < 400) return null;
  return "shoot";
}

// 小助手手柄键手势(纯函数,test_endo_helper_btn.mjs):still pin 只报"按了一次"(无按下/松开对)。
// 手柄键=拍照,即按即拍;录像用屏幕按钮——真机实测两次按键最小间隔1.9秒:
// 按键后相机拍高清still的几百毫秒里第二按被硬件吞掉,"0.6秒内双击"物理不成立,双击手势已判死。
// 残留过滤:小助手进程刚启动会自发一次触发(实测启动后2ms,编号恒为),只有 n===1 且紧跟 hello 才丢
// (重开模态 hello 会刷新,若不看编号会把头2秒的真按键全吞掉——真机实测踩过);v0.2 起服务端也过滤,双保险。
function endoHelperBtnDecision(helloTs, nowTs, n) {
  if (n === 1 && nowTs - helloTs < 2000) return "ignore";
  return "shoot";
}

function _endoKeyBtnLabel() {
  return _endoShootKey ? `🎮 手柄键:${_endoShootKey}(点击重映射)` : "⚙ 映射手柄拍照键";
}

function _endoOnKeydown(e) {
  const act = endoKeyDownDecision(_endoKeyLearning, _endoShootKey, e.code, e.repeat);
  if (!act) {
    // 诊断可视化:模态开着时收到的每个按键都亮在状态栏——排查手柄发什么键/信号有没有到达页面
    if (!document.getElementById("endoModal")) return;
    if (_endoShootKey && e.code === _endoShootKey) e.preventDefault();   // 系统连发,长按由计时器判
    else _endoStatus(`收到按键:${e.code}(未映射为拍照键)`);
    return;
  }
  e.preventDefault();
  if (act === "learn") {
    _endoKeyLearning = false;
    _endoShootKey = e.code;
    localStorage.setItem("endoShootKey", e.code);
    const b = document.getElementById("endoKeyBtn");
    if (b) b.textContent = _endoKeyBtnLabel();
    _endoStatus(`已映射 ${e.code}:短按拍照,长按录像`);
    return;
  }
  // track:开始跟踪本次按住;没在录像时按住0.6秒=开始录像
  _endoKeyHeld = true;
  _endoLongFired = false;
  if (!_endoRec) {
    clearTimeout(_endoLongTimer);
    _endoLongTimer = setTimeout(() => {
      if (!_endoKeyHeld) return;
      _endoLongFired = true;
      _endoToggleRec();
    }, ENDO_LONGPRESS_MS);
  }
}

function _endoOnKeyup(e) {
  if (!_endoKeyHeld || !_endoShootKey || e.code !== _endoShootKey) return;
  e.preventDefault();
  _endoKeyHeld = false;
  clearTimeout(_endoLongTimer);
  _endoLongTimer = null;
  const act = endoKeyUpDecision(!!_endoRec, _endoLongFired, Date.now(), _endoLastKeyMs);
  _endoLongFired = false;
  if (act === "consume") return;              // 长按已开录,这次松键不算短按
  if (act === "recStop") { _endoToggleRec(); return; }
  if (act !== "shoot") { _endoStatus(`收到 ${e.code}(连按防抖,已忽略)`); return; }
  _endoLastKeyMs = Date.now();
  _endoShoot();
}

async function openEndoscopeCapture(patientId, panel) {
  _endoPatientId = patientId;
  _endoPanel = panel || null;
  _endoIdx = 0;
  _endoShots = [];
  _endoTooth = {};
  _endoDeviceId = localStorage.getItem("endoCamId") || null;
  _endoShootKey = localStorage.getItem("endoShootKey") || null;
  _endoKeyLearning = false;
  document.removeEventListener("keydown", _endoOnKeydown);   // 防重复绑定
  document.removeEventListener("keyup", _endoOnKeyup);
  document.addEventListener("keydown", _endoOnKeydown);
  document.addEventListener("keyup", _endoOnKeyup);
  _endoKeyHeld = false; _endoLongFired = false;
  _renderEndoModal();
  _endoHelper = await _endoProbeHelper();
  if (_endoHelper) { _endoHelperMode(); return; }   // 小助手模式不走 getUserMedia,不需要安全上下文
  if (!window.isSecureContext || !navigator.mediaDevices || !navigator.mediaDevices.getUserMedia) {
    _endoShowInsecure();
    return;
  }
  await _endoStartCamera();
  await _endoFillDevices();
}

// ---- 相机小助手模式 ----

async function _endoProbeHelper() {
  try {
    const ctl = new AbortController();
    const t = setTimeout(() => ctl.abort(), 800);
    const res = await fetch(ENDO_HELPER_BASE + "/health", {signal: ctl.signal});
    clearTimeout(t);
    if (!res.ok) return null;
    const j = await res.json();
    return j && j.ok ? j : null;
  } catch { return null; }
}

function _endoHelperMode() {
  const cam = document.querySelector("#endoModal .endo-cam");
  if (cam) cam.innerHTML =
    `<img id="endoHelperImg" crossorigin="anonymous" src="${ENDO_HELPER_BASE}/stream" alt="内窥镜画面">`;
  const img = document.getElementById("endoHelperImg");
  if (img) img.onerror = () => {   // 流断了(如小助手重启)1.5秒后自动重拉,模态开着就一直试
    setTimeout(() => { if (_endoHelper && document.getElementById("endoHelperImg")) _endoHelperRefreshStream(); }, 1500);
  };
  const sel = document.getElementById("endoDevice");
  if (sel) {
    sel.innerHTML = `<option>🔌 ${escapeHtml(_endoHelper.device || "相机小助手")}</option>`;
    sel.disabled = true;
  }
  _endoStatus(`已连相机小助手 ${_endoHelper.width}x${_endoHelper.height}:手柄按键=拍照,录像用屏幕按钮`);
  _endoHelperConnectWs();
}

// 重拉 MJPEG 流(带时间戳防缓存);WS 断线重连成功后也调——img 不会像 WS 一样自己恢复
function _endoHelperRefreshStream() {
  const img = document.getElementById("endoHelperImg");
  if (img) img.src = `${ENDO_HELPER_BASE}/stream?ts=${Date.now()}`;
}

function _endoHelperConnectWs() {
  let ws;
  try { ws = new WebSocket(ENDO_HELPER_BASE.replace("http", "ws") + "/events"); }
  catch { _endoStatus("手柄通道打不开(WebSocket 异常)"); return; }
  _endoHelperWs = ws;
  ws.onopen = () => {
    _endoStatus("手柄通道已连:按手柄键=拍照,录像用屏幕按钮");
    if (_endoHelperWsDropped) { _endoHelperWsDropped = false; _endoHelperRefreshStream(); }
  };
  ws.onmessage = ev => {
    let msg = null;
    try { msg = JSON.parse(ev.data); } catch { return; }
    if (!msg) return;
    if (msg.type === "hello") { _endoHelperHelloTs = Date.now(); return; }
    if (msg.type === "button") _endoHelperOnButton(msg.n);
  };
  // 模态开着期间断线自动重连(小助手重启等);关模态时 _endoHelper 已清空,不再重连
  ws.onclose = () => {
    if (_endoHelper && document.getElementById("endoModal")) {
      _endoHelperWsDropped = true;   // 重连成功后连视频流一起重拉(img 不会自己恢复)
      _endoStatus("手柄通道断开,2秒后自动重连…");
      setTimeout(_endoHelperConnectWs, 2000);
    }
  };
}

function _endoHelperOnButton(n) {
  const act = endoHelperBtnDecision(_endoHelperHelloTs, Date.now(), n);
  if (act === "ignore") { _endoStatus("忽略小助手启动残留触发"); return; }
  // 录像中按手柄=停止录像(方案1:屏幕开录+手柄停录,讲解完不用腾手;开始录像走屏幕按钮)
  if (_endoRec) { _endoStatus("手柄键:停止录像"); _endoToggleRec(); return; }
  _endoStatus("收到手柄键…");
  // 即按即拍;照片取视频流帧(与still同为1080p零损耗)——still已降为最小格式只当按键信号,缩短按键停顿
  _endoHelperShoot("/snapshot");
}

// 小助手模式拍照:取 /snapshot 视频流当前帧(1080p,与still同源零损耗);拿回的就是 JPEG
async function _endoHelperShoot(path) {
  const cur = ENDO_SLOTS[_endoIdx];
  let tooth = "";
  if (cur.manual) {
    tooth = (_endoTooth[cur.slot] || "").trim();
    if (!tooth) _endoStatus("主诉牙特写:先拍了,牙位可拍后补填(上传前填上即生效)");
  }
  try {
    const res = await fetch(`${ENDO_HELPER_BASE}${path}?ts=${Date.now()}`);
    const blob = res.ok ? await res.blob() : null;
    if (!blob || !blob.size) { _endoStatus("拍照失败(小助手没给图)"); return; }
    _endoShots.push({slot: cur.slot, blob, url: URL.createObjectURL(blob), tooth, status: ""});
    _endoRenderStrip();
    if (_endoIdx < ENDO_SLOTS.length - 1) _endoGo(_endoIdx + 1);
    else _endoRenderGuide();
  } catch { _endoStatus("拍照失败(连不上小助手)"); }
}

// 录像流:把 MJPEG img 按帧描到画布,captureStream 喂 MediaRecorder(img 带 crossorigin+小助手放 CORS,画布不污染)
function _endoHelperMakeStream() {
  const img = document.getElementById("endoHelperImg");
  if (!img || !img.naturalWidth || !_endoHelperCanvasOk(img)) return null;
  return _endoHelperCanvas.captureStream(30);
}

function _endoHelperCanvasOk(img) {
  _endoHelperCanvas = document.createElement("canvas");
  _endoHelperCanvas.width = img.naturalWidth;
  _endoHelperCanvas.height = img.naturalHeight;
  const ctx = _endoHelperCanvas.getContext("2d");
  if (!ctx || typeof _endoHelperCanvas.captureStream !== "function") return false;
  clearInterval(_endoHelperDrawTimer);
  _endoHelperDrawTimer = setInterval(() => {
    const im = document.getElementById("endoHelperImg");
    if (im && im.naturalWidth) ctx.drawImage(im, 0, 0, _endoHelperCanvas.width, _endoHelperCanvas.height);
  }, 33);
  return true;
}

function _endoHelperRecCleanup() {
  clearInterval(_endoHelperDrawTimer); _endoHelperDrawTimer = null;
  _endoHelperCanvas = null;
  if (_endoStream) { _endoStream.getTracks().forEach(t => t.stop()); _endoStream = null; }
}

// 掐断 MJPEG 流连接:multipart 连接不清 src 浏览器可能一直拖着,反复开关模态会占满对小助手的连接额度
function _endoHelperStopStream() {
  const img = document.getElementById("endoHelperImg");
  if (img) { try { img.src = ""; img.removeAttribute("src"); } catch { /* 已断 */ } }
}

function _renderEndoModal() {
  let m = document.getElementById("endoModal");
  if (m) m.remove();
  m = document.createElement("div");
  m.id = "endoModal";
  m.className = "modal-backdrop";
  m.innerHTML = `
    <section class="appt-modal appt-modal-wide endo-modal" role="dialog" aria-modal="true" aria-label="内窥镜拍照">
      <div class="modal-head"><strong>📷 内窥镜拍照</strong>
        <button type="button" class="plain-button" id="endoClose">×</button></div>
      <div class="endo-body">
        <div class="endo-left">
          <div class="endo-cam"><video id="endoVideo" autoplay playsinline muted></video></div>
          <div class="endo-cam-bar"><select id="endoDevice" class="ord-input"></select>
            <button type="button" class="plain-button" id="endoKeyBtn"></button></div>
          <div id="endoManual" class="endo-manual" hidden>
            主诉牙位 <input id="endoTooth" class="ord-input" placeholder="如 36，多颗逗号隔" autocomplete="off">
          </div>
          <button type="button" class="tooth-confirm-btn endo-shoot" id="endoShoot">拍照</button>
          <button type="button" class="plain-button endo-rec" id="endoRecBtn">⏺ 录像</button>
        </div>
        <div class="endo-right">
          <div class="endo-guide-title"><span id="endoGuideName"></span>
            <span class="endo-guide-prog" id="endoGuideProg"></span></div>
          <div class="endo-guide-svg" id="endoGuideSvg"></div>
          <div class="endo-guide-tooth" id="endoGuideTooth"></div>
          <div class="endo-slot-grid" id="endoSlotGrid"></div>
          <div class="endo-nav">
            <button type="button" class="plain-button" id="endoPrev">‹ 上一张</button>
            <button type="button" class="plain-button" id="endoNext">跳过 / 下一张 ›</button>
          </div>
        </div>
      </div>
      <div class="endo-strip" id="endoStrip"></div>
      <div class="modal-actions">
        <span id="endoStatus"></span>
        <button type="button" class="tooth-confirm-btn" id="endoUpload">全部上传</button>
        <button type="button" class="plain-button" id="endoCancel">关闭</button>
      </div>
    </section>`;
  document.body.appendChild(m);
  document.getElementById("endoClose").addEventListener("click", closeEndoscopeCapture);
  document.getElementById("endoCancel").addEventListener("click", closeEndoscopeCapture);
  document.getElementById("endoShoot").addEventListener("click", _endoShoot);
  document.getElementById("endoRecBtn").addEventListener("click", _endoToggleRec);
  document.getElementById("endoUpload").addEventListener("click", _endoUploadAll);
  document.getElementById("endoPrev").addEventListener("click", () => _endoGo(_endoIdx - 1));
  document.getElementById("endoNext").addEventListener("click", () => _endoGo(_endoIdx + 1));
  document.getElementById("endoDevice").addEventListener("change", e => {
    _endoDeviceId = e.target.value;
    localStorage.setItem("endoCamId", _endoDeviceId);
    _endoStartCamera();
  });
  const kb = document.getElementById("endoKeyBtn");
  kb.textContent = _endoKeyBtnLabel();
  kb.addEventListener("click", () => {
    _endoKeyLearning = true;
    _endoStatus("请按一下手柄上的拍照键…");
  });
  const tip = document.getElementById("endoTooth");
  if (tip) tip.addEventListener("input", e => {
    _endoTooth[ENDO_SLOTS[_endoIdx].slot] = e.target.value.trim();
    _endoRenderGuide();
  });
  _endoRenderGuide();
  _endoRenderStrip();
}

function _endoShowInsecure() {
  const cam = document.querySelector("#endoModal .endo-cam");
  if (cam) cam.innerHTML = `<div class="endo-insecure">
    无法访问摄像头：当前页面不是"安全上下文"。<br>
    请在本机 Chrome 打开 <code>chrome://flags/#unsafely-treat-insecure-origin-as-secure</code>，
    把本系统地址（如 <code>http://本机IP:8765</code>）填入并设为 Enabled，重启浏览器后重试。<br>
    详见部署文档《内窥镜拍照-Chrome设置》。</div>`;
}

// 录像开关:开始→按钮跳秒;停止→片段进底部条带,随"全部上传"存入影像
function _endoToggleRec() {
  if (_endoRec) { _endoRec.stop(); return; }   // 收尾在 onstop 里
  if (!_endoStream && _endoHelper) _endoStream = _endoHelperMakeStream();
  if (!_endoStream) { _endoStatus("摄像头未就绪"); return; }
  const mime = endoRecMimePick(m => !!(window.MediaRecorder && MediaRecorder.isTypeSupported(m)));
  if (!mime) { _endoStatus("此浏览器不支持录像"); return; }
  const chunks = [];
  let rec;
  try {
    rec = new MediaRecorder(_endoStream, {mimeType: mime, videoBitsPerSecond: 2500000});
  } catch (err) { _endoStatus(`录像启动失败(${err.name || "错误"})`); return; }
  rec.ondataavailable = e => { if (e.data && e.data.size) chunks.push(e.data); };
  rec.onstop = () => {
    clearInterval(_endoRecTimer); _endoRecTimer = null; _endoRec = null;
    if (_endoHelper) _endoHelperRecCleanup();   // 小助手模式:停描帧循环,画布流下次录像重建
    const b = document.getElementById("endoRecBtn");
    if (b) { b.textContent = "⏺ 录像"; b.classList.remove("rec-on"); }
    const blob = new Blob(chunks, {type: mime});
    if (!blob.size) { _endoStatus("录像为空,未保存"); return; }
    if (blob.size > 200 * 1024 * 1024) { _endoStatus("录像超过200MB存不下,请分段录"); return; }
    _endoShots.push({slot: "录像", blob, url: URL.createObjectURL(blob), tooth: "", status: "", video: true});
    _endoRenderStrip();
    _endoStatus(`录像完成(${(blob.size / 1048576).toFixed(1)}MB),点"全部上传"存入影像`);
  };
  _endoRec = rec;
  rec.start(1000);   // 每秒吐一段,避免停止时一次性攒大块
  _endoRecT0 = Date.now();
  const btn = document.getElementById("endoRecBtn");
  if (btn) btn.classList.add("rec-on");
  _endoRecTimer = setInterval(() => {
    const s = Math.floor((Date.now() - _endoRecT0) / 1000);
    const b = document.getElementById("endoRecBtn");
    if (b) b.textContent = `⏹ 停止 ${Math.floor(s / 60)}:${String(s % 60).padStart(2, "0")}`;
  }, 500);
  _endoStatus("录像中…再点一下停止");
}

async function _endoStartCamera() {
  try {
    if (_endoRec) _endoRec.stop();   // 换摄像头会断当前轨道:先停录像保住已录片段
    if (_endoStream) { _endoStream.getTracks().forEach(t => t.stop()); _endoStream = null; }
    const constraints = {video: _endoDeviceId ? {deviceId: {exact: _endoDeviceId}} : true, audio: false};
    _endoStream = await navigator.mediaDevices.getUserMedia(constraints);
    const v = document.getElementById("endoVideo");
    if (v) v.srcObject = _endoStream;
  } catch (err) {
    const cam = document.querySelector("#endoModal .endo-cam");
    if (cam) cam.innerHTML = `<div class="endo-insecure">打不开摄像头（${escapeHtml(err.name || "错误")}）：
      请确认已插入内窥镜、并在浏览器允许摄像头权限，然后重开。</div>`;
  }
}

async function _endoFillDevices() {
  try {
    const devs = (await navigator.mediaDevices.enumerateDevices()).filter(d => d.kind === "videoinput");
    const sel = document.getElementById("endoDevice");
    if (!sel) return;
    sel.innerHTML = devs.map((d, i) =>
      `<option value="${escapeAttr(d.deviceId)}">${escapeHtml(d.label || ("摄像头" + (i + 1)))}</option>`).join("");
    if (_endoDeviceId && devs.some(d => d.deviceId === _endoDeviceId)) sel.value = _endoDeviceId;
    else if (devs[0]) { _endoDeviceId = devs[0].deviceId; sel.value = _endoDeviceId; }
  } catch { /* 忽略 */ }
}

// 当前机位高亮的牙位集合(主诉牙特写取手选)
function _endoCurTeeth() {
  const cur = ENDO_SLOTS[_endoIdx];
  const raw = cur.manual ? (_endoTooth[cur.slot] || "") : cur.teeth;
  return new Set(raw.split(",").map(s => s.trim()).filter(Boolean));
}

// 牙弓示意图:上下两排FDI牙,高亮当前机位要拍的牙
function _endoArchSvg(hi) {
  const W = 18, H = 22, GAP = 2, n = 16;
  const rowW = n * (W + GAP);
  const cell = (fdi, x, y) => {
    const on = hi.has(String(fdi));
    return `<rect x="${x}" y="${y}" width="${W}" height="${H}" rx="4"
      fill="${on ? '#2c8c6f' : '#eef4f2'}" stroke="${on ? '#1f6e57' : '#cfe0da'}"/>
      <text x="${x + W / 2}" y="${y + H / 2 + 4}" text-anchor="middle" font-size="9"
      fill="${on ? '#fff' : '#7a8a85'}">${fdi}</text>`;
  };
  let u = "", l = "";
  ENDO_UPPER.forEach((f, i) => { u += cell(f, i * (W + GAP), 0); });
  ENDO_LOWER.forEach((f, i) => { l += cell(f, i * (W + GAP), H + 10); });
  return `<svg viewBox="0 0 ${rowW} ${2 * H + 10}" width="100%" xmlns="http://www.w3.org/2000/svg">
    <text x="0" y="-2" font-size="9" fill="#7a8a85"></text>${u}${l}</svg>`;
}

// 机位宫格:13个机位全摆出来,点哪个拍哪个(不用一张张翻);当前机位高亮,拍过的打勾
function _endoRenderSlotGrid() {
  const g = document.getElementById("endoSlotGrid");
  if (!g) return;
  g.innerHTML = ENDO_SLOTS.map((s, i) => {
    const done = _endoShotsOf(s.slot).length > 0;
    const cls = "endo-slot-btn" + (i === _endoIdx ? " cur" : "") + (done ? " done" : "");
    return `<button type="button" class="${cls}" data-slot-i="${i}">${done ? "✓" : ""}${escapeHtml(s.slot)}</button>`;
  }).join("");
  g.querySelectorAll("[data-slot-i]").forEach(b =>
    b.addEventListener("click", () => _endoGo(Number(b.dataset.slotI))));
}

function _endoRenderGuide() {
  _endoRenderSlotGrid();
  const cur = ENDO_SLOTS[_endoIdx];
  const name = document.getElementById("endoGuideName");
  if (name) name.textContent = "该拍：" + cur.slot;
  const prog = document.getElementById("endoGuideProg");
  if (prog) prog.textContent = `${_endoIdx + 1}/${ENDO_SLOTS.length}　已拍 ${_endoShotsOf(cur.slot).length}`;
  const svg = document.getElementById("endoGuideSvg");
  if (svg) svg.innerHTML = _endoArchSvg(_endoCurTeeth());
  const tip = document.getElementById("endoGuideTooth");
  if (tip) {
    const t = [..._endoCurTeeth()].join("、");
    tip.textContent = cur.manual ? (t ? "牙位：" + t : "主诉牙特写：请在左侧填牙位") : "牙位：" + t;
  }
  const shootName = document.getElementById("endoShoot");
  if (shootName) shootName.textContent = "拍照（" + cur.slot + "）";
  const manual = document.getElementById("endoManual");
  if (manual) manual.hidden = !cur.manual;
  const tooth = document.getElementById("endoTooth");
  if (tooth) tooth.value = _endoTooth[cur.slot] || "";
}

function _endoGo(idx) {
  _endoIdx = Math.max(0, Math.min(idx, ENDO_SLOTS.length - 1));
  _endoRenderGuide();
}

function _endoShotsOf(slot) { return _endoShots.filter(s => s.slot === slot); }

function _endoRenderStrip() {
  const strip = document.getElementById("endoStrip");
  if (!strip) return;
  strip.innerHTML = _endoShots.map((sh, i) => `
    <div class="endo-thumb${sh.status === "ok" ? " ok" : sh.status === "fail" ? " fail" : ""}">
      ${sh.video ? `<video src="${sh.url}" muted></video>` : `<img src="${sh.url}" alt="">`}
      <div class="endo-thumb-cap">${sh.video ? "▶ " : ""}${escapeHtml(sh.slot)}${sh.tooth ? " " + escapeHtml(sh.tooth) : ""}</div>
      <button type="button" class="endo-thumb-del" data-i="${i}">×</button>
    </div>`).join("") || '<span class="endo-strip-empty">还没拍照</span>';
  strip.querySelectorAll("[data-i]").forEach(b => b.addEventListener("click", () => {
    const i = Number(b.dataset.i);
    if (_endoShots[i]) { URL.revokeObjectURL(_endoShots[i].url); _endoShots.splice(i, 1); _endoRenderStrip(); _endoRenderGuide(); }
  }));
}

function _endoShoot() {
  if (_endoHelper) { _endoHelperShoot("/snapshot"); return; }
  const v = document.getElementById("endoVideo");
  if (!v || !v.videoWidth) { _endoStatus("摄像头未就绪"); return; }
  const cur = ENDO_SLOTS[_endoIdx];
  let tooth = "";
  if (cur.manual) {
    tooth = (_endoTooth[cur.slot] || "").trim();
    if (!tooth) _endoStatus("主诉牙特写:先拍了,牙位可拍后补填(上传前填上即生效)");
  }
  const canvas = document.createElement("canvas");
  canvas.width = v.videoWidth; canvas.height = v.videoHeight;
  canvas.getContext("2d").drawImage(v, 0, 0);
  const slot = cur.slot;
  canvas.toBlob(blob => {
    if (!blob) { _endoStatus("拍照失败"); return; }
    _endoShots.push({slot, blob, url: URL.createObjectURL(blob), tooth, status: ""});
    _endoRenderStrip();
    if (_endoIdx < ENDO_SLOTS.length - 1) _endoGo(_endoIdx + 1);   // 拍完自动跳下一张
    else _endoRenderGuide();
  }, "image/jpeg", 0.9);
}

async function _endoUploadAll() {
  const pending = _endoShots.filter(s => s.status !== "ok");
  if (!pending.length) { _endoStatus("没有待上传的照片"); return; }
  _endoStatus("上传中...");
  let ok = 0, fail = 0;
  for (const sh of pending) {
    const fd = new FormData();
    fd.append("file", sh.blob, sh.video ? (sh.blob.type.includes("mp4") ? "clip.mp4" : "clip.webm") : "shot.jpg");
    fd.append("slot", sh.slot);
    // 主诉牙拍时没填牙位的,上传前补填输入框也算数
    const tooth = sh.tooth || (_endoTooth[sh.slot] || "").trim();
    if (tooth) fd.append("tooth_codes", tooth);
    try {
      const res = await fetch(`/api/patients/${encodeURIComponent(_endoPatientId)}/images`,
        {method: "POST", body: fd});
      if (res.ok) { sh.status = "ok"; ok++; } else { sh.status = "fail"; fail++; }
    } catch { sh.status = "fail"; fail++; }
  }
  _endoRenderStrip();
  _endoStatus(`完成：成功 ${ok}${fail ? "，失败 " + fail + "（可再点上传重试）" : ""}`);
  if (ok && typeof renderWorkspaceImagesTab === "function" && _endoPanel) {
    renderWorkspaceImagesTab(_endoPanel);
  }
  if (ok && !fail) setTimeout(closeEndoscopeCapture, 800);   // 全部传成功自动关窗(留0.8秒看到"完成")
}

function _endoStatus(t) { const s = document.getElementById("endoStatus"); if (s) s.textContent = t; }

function closeEndoscopeCapture() {
  document.removeEventListener("keydown", _endoOnKeydown);
  document.removeEventListener("keyup", _endoOnKeyup);
  clearTimeout(_endoLongTimer); _endoLongTimer = null;
  _endoKeyHeld = false; _endoLongFired = false;
  _endoKeyLearning = false;
  if (_endoRec) { try { _endoRec.onstop = null; _endoRec.stop(); } catch { /* 已停 */ } _endoRec = null; }
  if (_endoRecTimer) { clearInterval(_endoRecTimer); _endoRecTimer = null; }
  _endoHelper = null;   // 先清再关 ws,onclose 里就不会再重连
  _endoHelperStopStream();
  clearInterval(_endoHelperDrawTimer); _endoHelperDrawTimer = null;
  _endoHelperCanvas = null;
  _endoHelperHelloTs = 0;
  if (_endoHelperWs) { try { _endoHelperWs.close(); } catch { /* 已断 */ } _endoHelperWs = null; }
  if (_endoStream) { _endoStream.getTracks().forEach(t => t.stop()); _endoStream = null; }
  _endoShots.forEach(s => URL.revokeObjectURL(s.url));
  _endoShots = [];
  const m = document.getElementById("endoModal");
  if (m) m.remove();
}

Object.assign(window, {openEndoscopeCapture, closeEndoscopeCapture});
