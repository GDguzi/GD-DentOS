"""诊所业务时间统一入口 —— 一律北京时区（Asia/Shanghai），显式带时区。

为什么单独一个模块：
- 宿主机系统时区不一定是国内（走代理时定位服务可能按出口 IP 把系统时区漂到别的时区）。
- `local_app/__init__.py` 用 `os.environ["TZ"]=... + time.tzset()` 在进程级钉死北京时间，
  但 `tzset()` 是 Unix-only，Windows 上失效；且依赖 import 顺序。
- 本模块用显式 `ZoneInfo("Asia/Shanghai")` 取时间，不依赖进程 TZ，跨平台一致。

历史上同一套逻辑被复制在多处（各路由私有 _bj_now/_bj_today、各文件 _now、
warehouse_common.now_text），一律收敛到这里，单一真相源。
"""

from datetime import datetime
from zoneinfo import ZoneInfo

BJ = ZoneInfo("Asia/Shanghai")


def bj_now() -> datetime:
    """当前北京时间（带时区的 aware datetime）。需要 datetime 对象时用它。"""
    return datetime.now(BJ)


def now_str() -> str:
    """北京时间时间戳字符串 'YYYY-MM-DD HH:MM:SS'。

    替代各处 `_bj_now()` / `_now()` / `warehouse_common.now_text()`。
    """
    return datetime.now(BJ).strftime("%Y-%m-%d %H:%M:%S")


def today_str() -> str:
    """北京日期字符串 'YYYY-MM-DD'。替代各处 `_bj_today()`。"""
    return datetime.now(BJ).strftime("%Y-%m-%d")
