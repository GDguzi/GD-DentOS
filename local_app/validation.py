"""日期/时间输入校验。

库内所有按日期过滤的查询都用 substr(time, 1, 10) 与 'YYYY-MM-DD' 字符串比较，
所以读写两侧都必须收紧到横杠格式：
- Python 3.11+ 的 date.fromisoformat 会放行 '20260613'、'2026-W24-5' 等紧凑格式，
  校验通过但与 substr 永远不匹配，接口静默返回全 0；
- 写入侧若存入 '2026/06/13'、'明天' 等值，记录从所有日历/列表/统计里消失。
"""

import datetime as dt
import re

from fastapi import HTTPException

_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
# 秒后允许小数（部分来源的时间形如 09:43:00.000000）
_DATETIME_PREFIX_RE = re.compile(r"^\d{4}-\d{2}-\d{2}([ T]\d{2}:\d{2}(:\d{2}(\.\d+)?)?)?$")


def valid_date_param(value, field="date"):
    """查询参数：必须是 YYYY-MM-DD。返回解析后的 date。"""
    value = str(value or "").strip()
    if not _DATE_RE.match(value):
        raise HTTPException(status_code=400, detail=f"{field} must be YYYY-MM-DD")
    try:
        return dt.date.fromisoformat(value)
    except ValueError as exc:
        raise HTTPException(
            status_code=400, detail=f"{field} must be YYYY-MM-DD"
        ) from exc


def valid_time_field(value, field):
    """写入侧时间字段：允许空串；非空时须为 YYYY-MM-DD[ HH:MM[:SS]]。返回原值。"""
    value = str(value or "").strip()
    if not value:
        return value
    if not _DATETIME_PREFIX_RE.match(value):
        raise HTTPException(
            status_code=400,
            detail=f"{field} 格式须为 YYYY-MM-DD 或 YYYY-MM-DD HH:MM[:SS]",
        )
    try:
        dt.date.fromisoformat(value[:10])
    except ValueError as exc:
        raise HTTPException(
            status_code=400, detail=f"{field} 日期不合法"
        ) from exc
    return value


def valid_int_qty(v, default=1):
    """数量必须是整数：缺省用 default;传了但非整数值(如 "2.5"/2.9)→400,不静默截断/兜底。
    整数值的 "2"/"2.0"/2/2.0 都接受。"""
    if v is None or v == "":
        return default
    if isinstance(v, bool):   # True/False 不算合法数量
        raise HTTPException(status_code=400, detail="数量必须是整数")
    if isinstance(v, int):
        return v
    if isinstance(v, float):
        if v.is_integer():
            return int(v)
        raise HTTPException(status_code=400, detail="数量必须是整数")
    # 字符串:只认普通十进制整数(或 "2.0"/"2.00" 这类整数值小数);
    # 走正则+int() 而非 float(),避免大数精度丢失(复核🟡)和科学计数法"1e3"被误纳
    s = str(v).strip()
    if re.fullmatch(r"-?\d+", s):
        return int(s)
    m = re.fullmatch(r"(-?\d+)\.0+", s)
    if m:
        return int(m.group(1))
    raise HTTPException(status_code=400, detail="数量必须是整数")
