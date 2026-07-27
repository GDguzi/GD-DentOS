// dict.js — 字典前端消费 (P0-4)
// 把建档/病历表单里原本自由文本/硬编码的字段，接上 GET /api/dictionaries?type= 的已同步字典，
// 用 <datalist> 给"建议值"但仍允许自由输入（字典外的值照样能填，不破坏旧行为）。
// 断 外部系统 后仍可用本地已同步的字典数据。

const _dictCache = {};     // type -> [{name, value, ...}]
const _dictPending = {};   // type -> Promise（并发去重）

// 拉取某字典类型的在用项（带缓存）；网络/解析失败回退空数组，不抛。
async function loadDict(type) {
  if (_dictCache[type]) return _dictCache[type];
  if (_dictPending[type]) return _dictPending[type];
  _dictPending[type] = (async () => {
    try {
      const res = await fetch(`/api/dictionaries?type=${encodeURIComponent(type)}`);
      if (!res.ok) return [];
      const data = await res.json();
      const items = Array.isArray(data.items) ? data.items : [];
      _dictCache[type] = items;
      return items;
    } catch {
      return [];
    } finally {
      delete _dictPending[type];
    }
  })();
  return _dictPending[type];
}

// 确保 <datalist id="dict-<type>"> 存在并填充字典项，返回其 id 供 input 的 list= 绑定。
async function ensureDictDatalist(type) {
  const id = `dict-${type}`;
  let dl = document.getElementById(id);
  if (!dl) {
    dl = document.createElement("datalist");
    dl.id = id;
    document.body.appendChild(dl);
  }
  if (dl.dataset.filled) return id;
  const items = await loadDict(type);
  dl.innerHTML = items
    .map(it => `<option value="${escapeAttr(it.name)}"></option>`)
    .join("");
  dl.dataset.filled = "1";
  return id;
}

// 给 root 范围内带 data-dict 的输入框批量挂上字典 datalist。面板渲染后调用。
function bindDictInputs(root) {
  const scope = root || document;
  scope.querySelectorAll("input[data-dict]:not([list])").forEach(input => {
    const type = input.dataset.dict;
    if (!type) return;
    input.setAttribute("list", `dict-${type}`);
    ensureDictDatalist(type);
  });
}
