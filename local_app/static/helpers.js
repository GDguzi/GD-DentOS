function formatMoney(value) {
  if (value === null || value === undefined || value === "") return "";
  const numberValue = Number(value);
  if (Number.isNaN(numberValue)) return String(value);
  return numberValue.toFixed(2);
}

// 角色权限：登录后 app.js 把当前用户权限集存 window.__userPerms(Set)。hasPerm 是前端显隐的唯一入口——
// system admin 由 __isSystemAdmin 明确全通过；普通用户只精确命中权限键。
// 未加载/未登录返回 false(fail-closed，按钮不显示)。
// 前端隐藏只是 UX 与防误触，后端 require_perm 才是安全闸门(权限点 key 前后端共用一份)。
function hasPerm(key) {
  if (window.__isSystemAdmin === true) return true;
  const perms = window.__userPerms;
  if (!perms) return false;
  return perms.has(key);
}

function hasAnyPerm(...keys) {
  return keys.some(key => hasPerm(key));
}

// 新旧权限键只在具体按钮处兼容；hasPerm 本身始终精确匹配，避免普通用户权限被放大。
function canRefund() {
  return hasAnyPerm("payment.refund", "billing.refund");
}

function canExport() {
  return hasAnyPerm("patient.profile.export", "data.export");
}

function canManagePaymentMethods() {
  return hasAnyPerm("payment_method.manage", "billing.pay");
}

function canAccessAccounts() {
  return hasAnyPerm("account.view", "account.open", "account.security", "user.manage");
}

// 防双击重复提交：提交期间禁用按钮，结束后恢复；已在进行中则忽略第二次。
// 后端 begin_immediate 已兜底数据不翻倍，这层只为避免第二次点击多冒一个报错框。
// 用法：把提交按钮的 onclick="handler()" 改成 onclick="guardSubmit(this, handler)"
async function guardSubmit(btn, fn) {
  if (btn && btn.disabled) return;
  if (btn) btn.disabled = true;
  try { return await fn(); }
  finally { if (btn) btn.disabled = false; }
}

function formatCount(value) {
  if (value === null || value === undefined || value === "") return "0";
  if (typeof value === "number") return value.toLocaleString("zh-CN");
  return String(value);
}

function cssEscape(value) {
  if (window.CSS && CSS.escape) return CSS.escape(value);
  return String(value).replace(/["\\]/g, "\\$&");
}

function escapeHtml(value) {
  return String(value ?? "").replace(/[&<>"']/g, char => ({
    "&": "&amp;",
    "<": "&lt;",
    ">": "&gt;",
    "\"": "&quot;",
    "'": "&;",
  }[char]));
}

function escapeAttr(value) {
  return escapeHtml(value).replace(/`/g, "&;");
}

function field(label, value) {
  return `
    <div>
      <div class="field-label">${label}</div>
      <div class="field-value">${escapeHtml(value || "")}</div>
    </div>
  `;
}

// 诊所业务"今天/现在"一律北京时间(UTC+8,无夏令时)：宿主机/浏览器系统时区可能漂
// (如代理出口在国外),new Date() 的本地日期不能当工作日期;后端已在 local_app/__init__.py
// 钉死 TZ=Asia/Shanghai,前端在此对齐。
function bjNow() {
  // 返回"本地字段=北京墙上时间"的 Date：UTC 毫秒 +8h 后读 UTC 字段。
  const t = new Date(Date.now() + 8 * 3600 * 1000);
  return new Date(t.getUTCFullYear(), t.getUTCMonth(), t.getUTCDate(),
                  t.getUTCHours(), t.getUTCMinutes(), t.getUTCSeconds());
}

function bjToday() {
  const n = bjNow();
  return new Date(n.getFullYear(), n.getMonth(), n.getDate());
}

function bjNowStr() {
  const n = bjNow();
  const p = x => String(x).padStart(2, "0");
  return `${n.getFullYear()}-${p(n.getMonth() + 1)}-${p(n.getDate())}` +
         ` ${p(n.getHours())}:${p(n.getMinutes())}:${p(n.getSeconds())}`;
}

function localDateValue(date = bjToday()) {
  const year = date.getFullYear();
  const month = String(date.getMonth() + 1).padStart(2, "0");
  const day = String(date.getDate()).padStart(2, "0");
  return `${year}-${month}-${day}`;
}

function dateFromWorkValue(value) {
  if (!value) return bjToday();
  const [year, month, day] = value.split("-").map(Number);
  if (!year || !month || !day) return bjToday();
  return new Date(year, month - 1, day);
}

Object.assign(window, {
  toothCrossHtml,
  parseImportedSubject,
  importedSubjectToText,
  importedXmlToText,
  formatVisitTimeText,
  formatMoney,
  formatCount,
  cssEscape,
  escapeHtml,
  escapeAttr,
  field,
  bjNow,
  bjToday,
  bjNowStr,
  localDateValue,
  dateFromWorkValue,
});

// === 导入病历科目结构化值解析 ===
// 导入的历史病历把 检查/诊断/治疗/医嘱 等科目存成 {"item":[{"RT":"4","RT_new":[{"tooth":"4",...}],"Text":"..."}]}。
// 解析为 [{teeth:["14"], text:"..."}]；不是该形态返回 null；item 为空返回 []（调用方按"无内容"跳过）。
function parseImportedSubject(value) {
  let parsed = value;
  if (typeof value === "string") {
    const s = value.trim();
    if (!s.startsWith("{") && !s.startsWith("[")) return null;   // 新格式补的病历是数组 [..]
    try { parsed = JSON.parse(s); } catch { return null; }
  }
  // 新格式(补的病历)：直接是数组 [{"lb","lt","rb","rt","diagnose",...}](小写象限+diagnose)
  if (Array.isArray(parsed)) return parseImportedSubjectArray(parsed);
  if (!parsed || typeof parsed !== "object" || !Array.isArray(parsed.item)) {
    return null;
  }
  // ⚠️牙位左右铁律：外部系统 象限键按【屏幕格子位置(viewer)】命名,屏幕右=患者左,故须左右翻转成 FDI(患者解剖侧)：
  // RT(屏右上)=患者左上=2, LT(屏左上)=患者右上=1, RB(屏右下)=患者左下=3, LB(屏左下)=患者右下=4。
  const QUAD = {RT: "2", LT: "1", LB: "4", RB: "3"};
  return parsed.item.map(entry => {
    const teeth = [];
    for (const [key, digit] of Object.entries(QUAD)) {
      const v = entry && entry[key];
      if (typeof v === "string" && v.trim()) {
        // 象限值是数字串（"46" = 4号+6号牙），沿用 tooth_json 的既有约定
        for (const ch of v.trim()) {
          if (/[1-9]/.test(ch)) teeth.push(digit + ch);
        }
      }
    }
    return {teeth: teeth.sort(), text: String((entry && entry.Text) || "").trim()};
  }).filter(e => e.text || e.teeth.length);
}

// 新结构化病历格式：数组 [{lb,lt,rb,rt(小写象限牙位串), diagnose/text, specialtooth, mpr}]。
// 全空时返回 []（渲染为空,不再把原始 JSON 直接怼到界面)。
function parseImportedSubjectArray(arr) {
  // ⚠️同 parseImportedSubject：外部系统 小写象限键也是屏幕位(viewer),屏右=患者左,翻转成 FDI(患者解剖侧)。
  const QUAD = {rt: "2", lt: "1", lb: "4", rb: "3"};
  return arr.map(entry => {
    if (!entry || typeof entry !== "object") return {teeth: [], text: ""};
    const teeth = [];
    for (const [key, digit] of Object.entries(QUAD)) {
      const v = entry[key];
      if (typeof v === "string" && v.trim()) {
        for (const ch of v.trim()) if (/[1-9]/.test(ch)) teeth.push(digit + ch);
      }
    }
    const text = String(entry.diagnose || entry.Text || entry.text
      || entry.specialtooth || entry.mpr || "").trim();
    return {teeth: teeth.sort(), text};
  }).filter(e => e.text || e.teeth.length);
}

// 外部系统 结构化值转可读文本："14 缝线存，牙龈未见异常；…"；非该形态返回 null
function importedSubjectToText(value) {
  const entries = parseImportedSubject(value);
  if (entries === null) return null;
  return entries
    .map(e => (e.teeth.length ? e.teeth.join(" ") + " " : "") + e.text)
    .join("；");
}

// 导入的病历模板科目可能是 XML 富文本(<?xml..><Document><body><div><span>文本</span></div>..)。
// 转成可读纯文本：去声明/标签、div 边界换行、反转义实体。非 XML 字符串原样返回。
function importedXmlToText(value) {
  if (typeof value !== "string") return "";   // 非字符串(对象/null)→空，不输出 [object Object]
  let s = value.trim();
  if (!s.startsWith("<")) return value;                  // 非 XML，原样
  s = s.replace(/<\?xml[^>]*\?>/gi, "");                  // 去 XML 声明
  s = s.replace(/<\/(div|p|br)\s*\/?>/gi, "\n");          // 块边界换行
  s = s.replace(/<br\s*\/?>/gi, "\n");
  s = s.replace(/<[^>]+>/g, "");                          // 去剩余标签
  s = s.replace(/&lt;/g, "<").replace(/&gt;/g, ">")
       .replace(/&quot;/g, '"').replace(/&;/g, "'").replace(/&amp;/g, "&");  // 反转义(&amp;最后)
  return s.split("\n").map(l => l.trim()).filter(Boolean).join("\n");
}

// 就诊时间显示：去微秒/秒，保留 "YYYY-MM-DD HH:MM"
function formatVisitTimeText(value) {
  const s = String(value || "");
  const m = s.match(/^(\d{4}-\d{2}-\d{2})[ T](\d{2}:\d{2})/);
  return m ? `${m[1]} ${m[2]}` : s;
}

// 回访状态码 → 可读文案：原始 "0"/空 直接显示成裸「0」很费解,统一映射。
function returnVisitStatusText(s) {
  s = String(s == null ? "" : s).trim();
  if (s === "" || s === "0") return "待回访";
  if (["done", "已回访", "完成", "4"].includes(s)) return "已回访";
  return s;   // 其他状态原样
}

// 十字牙位格（官方病历卡/编辑器样式）：FDI 码集合 → 四象限十字布局
// 上排=患者右上|左上(viewer 左|右)，下排=右下|左下；乳牙码(5x-8x)显示为字母 A-E
function toothCrossHtml(teeth) {
  const quads = {RT: [], LT: [], RB: [], LB: []};
  const QUAD_BY_DIGIT = {1: "RT", 2: "LT", 3: "LB", 4: "RB", 5: "RT", 6: "LT", 7: "LB", 8: "RB"};
  const LETTERS = ["A", "B", "C", "D", "E"];
  for (const code of teeth || []) {
    const str = String(code);
    const quad = QUAD_BY_DIGIT[str[0]];
    if (!quad) continue;
    const num = str.slice(1);
    const isDeciduous = "5678".includes(str[0]);
    // 格子里只显示位号(如 16→"6")会丢象限信息(是右上还是左上),
    // 恒牙直接显示完整 FDI 码(如 "16");乳牙沿用字母(A-E 本身就是通用命名法，不含 FDI 象限位)。
    quads[quad].push(isDeciduous ? (LETTERS[Number(num) - 1] || num) : str);
  }
  const cell = arr => escapeHtml(arr.join(" "));
  // 补完整 FDI 码到 title/aria-label，便于无障碍/自动化(hover 即见 "16")。
  const fdi = (teeth || []).map(String).filter(Boolean).join(" ");
  return `<span class="tooth-cross" aria-label="牙位 ${escapeHtml(fdi)}" title="${escapeHtml(fdi)}">` +
    `<span class="tc tc-rt">${cell(quads.RT)}</span>` +
    `<span class="tc tc-lt">${cell(quads.LT)}</span>` +
    `<span class="tc tc-rb">${cell(quads.RB)}</span>` +
    `<span class="tc tc-lb">${cell(quads.LB)}</span>` +
    `</span>`;
}

// 应用内输入弹窗，接口对齐 window.prompt(message, defaultValue) —— resolve 输入的字符串，
// 取消/Esc resolve null。嵌入式浏览器/自动化环境对原生 prompt() 支持不稳定，改用页面内 modal。
function appPrompt(message, defaultValue = "", options = {}) {
  const isPassword = options && options.type === "password";
  const inputType = isPassword ? "password" : "text";
  const autocomplete = options && Object.prototype.hasOwnProperty.call(options, "autocomplete")
    ? options.autocomplete
    : (isPassword ? "current-password" : "off");
  const valueAttr = isPassword ? "" : ` value="${escapeAttr(defaultValue)}"`;
  return new Promise(resolve => {
    const ov = document.createElement("div");
    ov.className = "modal-backdrop";
    ov.style.zIndex = "10000";
    ov.innerHTML = `
      <section class="app-prompt-modal" role="dialog" aria-modal="true" aria-label="${escapeAttr(String(message).slice(0, 60))}">
        <div class="app-prompt-body">
          <p class="app-prompt-msg">${escapeHtml(message)}</p>
          <input class="ord-input app-prompt-input" type="${inputType}" autocomplete="${escapeAttr(autocomplete)}"${valueAttr}>
        </div>
        <div class="modal-actions">
          <button type="button" class="plain-button" data-ap="cancel">取消</button>
          <button type="button" class="tooth-confirm-btn" data-ap="ok">确定</button>
        </div>
      </section>`;
    document.body.appendChild(ov);
    const input = ov.querySelector(".app-prompt-input");
    if (isPassword) input.value = String(defaultValue ?? "");
    const finish = value => { ov.remove(); document.removeEventListener("keydown", onKeydown); resolve(value); };
    const onKeydown = e => {
      if (e.key === "Escape") finish(null);
      if (e.key === "Enter") finish(input.value);
    };
    document.addEventListener("keydown", onKeydown);
    ov.querySelector('[data-ap="cancel"]').addEventListener("click", () => finish(null));
    ov.querySelector('[data-ap="ok"]').addEventListener("click", () => finish(input.value));
    input.focus();
    input.select();
  });
}

// UI重构Phase0: echarts 等无法用 CSS var() 的场景，读 styles.css token 实值
function cssToken(name) {
  return getComputedStyle(document.documentElement).getPropertyValue(name).trim();
}
