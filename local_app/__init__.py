"""Local dental clinic data application."""

import os as _os
import time as _time

# 诊所业务时间一律北京时区：宿主机系统时区可能不是国内（如 America/Los_Angeles），
# 全项目 138+ 处 datetime.now()/date.today() 都取本地时间，在包入口统一钉死。
_os.environ["TZ"] = "Asia/Shanghai"
if hasattr(_time, "tzset"):
    _time.tzset()
# Windows 没有 tzset,进程级钉不住时区,本地时间跟随系统时区(境内诊所机=北京);
# 时间敏感路径请用 local_app/timeutil 的显式北京时区接口,不依赖本开关。
