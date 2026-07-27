// 双月日历区间选择器（对标原系统 外部系统 起止日期）。原生 JS 无依赖，可复用到各报表。
// 用法：mountRangePicker(hostEl, {from, to, onApply})
//   host 内渲染：触发字段「📅 起 至 止」+ 点开弹出左右两个月日历；
//   点第一天=开始，再点一天=结束(早于开始则当新开始)，选满区间即回调 onApply(from,to) 并收起。
(function () {
  const WEEK = ["日", "一", "二", "三", "四", "五", "六"];
  const pad = n => String(n).padStart(2, "0");
  const ymd = d => d.getFullYear() + "-" + pad(d.getMonth() + 1) + "-" + pad(d.getDate());
  function parse(s) {
    const m = /^(\d{4})-(\d{2})-(\d{2})/.exec(String(s || ""));
    return m ? new Date(+m[1], +m[2] - 1, +m[3]) : null;
  }
  // 模块级单一外部点击监听(只装一次)：收起所有打开的区间弹窗。避免每次 mount 都加监听导致泄漏
  let _outsideBound = false;
  function bindOutsideOnce() {
    if (_outsideBound) return;
    _outsideBound = true;
    document.addEventListener("click", e => {
      document.querySelectorAll(".range-host").forEach(h => {
        const pop = h.querySelector(".range-popup");
        if (pop && !pop.hidden && !h.contains(e.target)) pop.hidden = true;
      });
    });
  }
  function monthCells(year, month) {
    const start = new Date(year, month, 1).getDay();           // 当月1号星期几(0=日)
    const days = new Date(year, month + 1, 0).getDate();        // 当月天数
    const cells = [];
    for (let i = 0; i < start; i++) cells.push(null);
    for (let d = 1; d <= days; d++) cells.push(new Date(year, month, d));
    return cells;
  }

  function mountRangePicker(host, opts) {
    opts = opts || {};
    const state = {from: opts.from || "", to: opts.to || "", view: null};
    const base = parse(state.from) || bjToday();
    state.view = new Date(base.getFullYear(), base.getMonth(), 1);   // 左侧月份

    host.classList.add("range-host");
    host.innerHTML = `<button type="button" class="range-field">📅 <span class="rf-text"></span></button>`
      + `<div class="range-popup" hidden></div>`;
    const field = host.querySelector(".range-field");
    const pop = host.querySelector(".range-popup");
    const text = host.querySelector(".rf-text");

    const refreshText = () => { text.textContent = (state.from || "开始") + " 至 " + (state.to || "结束"); };
    const inRange = d => state.from && state.to && ymd(d) >= state.from && ymd(d) <= state.to;

    function renderMonth(view) {
      const y = view.getFullYear(), m = view.getMonth();
      const grid = monthCells(y, m).map(d => {
        if (!d) return `<span class="rp-cell rp-empty"></span>`;
        const s = ymd(d), cls = ["rp-cell"];
        if (s === state.from || s === state.to) cls.push("rp-end");
        else if (inRange(d)) cls.push("rp-in");
        return `<button type="button" class="${cls.join(" ")}" data-day="${s}">${d.getDate()}</button>`;
      }).join("");
      return `<div class="rp-mhead">${y} 年 ${m + 1} 月</div>`
        + `<div class="rp-week">${WEEK.map(w => `<span>${w}</span>`).join("")}</div>`
        + `<div class="rp-grid">${grid}</div>`;
    }
    function render() {
      const left = state.view;
      const right = new Date(left.getFullYear(), left.getMonth() + 1, 1);
      const hint = (state.from && !state.to) ? `<span class="rp-hint">已选开始 ${state.from}，再点一天选结束</span>` : "";
      pop.innerHTML = `<div class="rp-presets">`
        + `<button type="button" class="rp-preset" data-preset="today">今天</button>`
        + `<button type="button" class="rp-preset" data-preset="yday">昨天</button>`
        + `<button type="button" class="rp-preset" data-preset="bday">前天</button>`
        + `<button type="button" class="rp-preset" data-preset="week">本周</button>`
        + `<button type="button" class="rp-preset" data-preset="month">本月</button></div>`
        + `<div class="rp-nav"><button type="button" class="rp-prev">‹</button>`
        + `<button type="button" class="rp-next">›</button></div>`
        + `<div class="rp-months"><div class="rp-month">${renderMonth(left)}</div>`
        + `<div class="rp-month">${renderMonth(right)}</div></div>`
        + `<div class="rp-foot">${hint}<button type="button" class="rp-clear">清空</button></div>`;
    }
    function ymdOffset(days) { const t = bjToday(); return ymd(new Date(t.getFullYear(), t.getMonth(), t.getDate() + days)); }
    function applyPreset(key) {
      const t = bjToday();
      let from, to;
      if (key === "today") { from = to = ymd(t); }
      else if (key === "yday") { from = to = ymdOffset(-1); }
      else if (key === "bday") { from = to = ymdOffset(-2); }
      else if (key === "week") { const wd = (t.getDay() + 6) % 7; from = ymd(new Date(t.getFullYear(), t.getMonth(), t.getDate() - wd)); to = ymd(t); }
      else { from = ymd(new Date(t.getFullYear(), t.getMonth(), 1)); to = ymd(t); }   // month
      state.from = from; state.to = to; refreshText(); pop.hidden = true;
      if (opts.onApply) opts.onApply(from, to);
    }
    function pickDay(s) {
      if (!state.from || (state.from && state.to)) { state.from = s; state.to = ""; }
      else if (s < state.from) { state.from = s; }
      else { state.to = s; }
      refreshText();
      if (state.from && state.to) {
        pop.hidden = true;
        if (opts.onApply) opts.onApply(state.from, state.to);
      } else { render(); }
    }
    function shift(delta) { state.view = new Date(state.view.getFullYear(), state.view.getMonth() + delta, 1); render(); }
    function clearRange() { state.from = ""; state.to = ""; refreshText(); pop.hidden = true; if (opts.onApply) opts.onApply("", ""); }

    field.addEventListener("click", () => { pop.hidden = !pop.hidden; if (!pop.hidden) render(); });
    pop.addEventListener("click", e => {
      // 点弹窗内部一律不冒泡到「点空白处关闭」的全局监听——否则 shift() 重建 DOM 后
      // 被点的 ‹/› 按钮已从 DOM 移除,全局监听 h.contains(旧节点)=false 会误判点到外面而关窗。
      e.stopPropagation();
      const day = e.target.closest("[data-day]");
      if (day) { pickDay(day.dataset.day); return; }
      const pre = e.target.closest("[data-preset]");
      if (pre) { applyPreset(pre.dataset.preset); return; }
      if (e.target.closest(".rp-clear")) { clearRange(); return; }
      if (e.target.closest(".rp-prev")) shift(-1);
      else if (e.target.closest(".rp-next")) shift(1);
    });
    bindOutsideOnce();   // 单一全局监听收起弹窗(不再每次 mount 加监听)

    refreshText();
    return {get from() { return state.from; }, get to() { return state.to; }};
  }

  window.mountRangePicker = mountRangePicker;
})();
