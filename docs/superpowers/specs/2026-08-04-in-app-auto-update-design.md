# 软件内一键自动升级（Windows 一键版）设计

日期：2026-08-04 · 状态：待审查 · 方案：A（下载新 exe + 引导脚本替换）

## 背景与目标

已安装诊所（Windows 一键 exe）目前升级要靠人工下载、拷包、覆盖，傻瓜用户做不了。
目标：软件检测到新版后挂横幅，用户点一次"立即升级"，软件自动下载、备份、替换、重启，
数据与账号零影响，全程无需任何技术操作。

## 范围

- **做**：Windows PyInstaller onefile 冻结版（诊所真实用户）。
- **不做**：macOS（Gatekeeper/签名问题，仅开发者自用，手动升级）；开发态 `run_local`（检测到非冻结环境直接返回"不适用"）。

## 已确认决策

| 决策点 | 结论 |
|---|---|
| 覆盖平台 | 仅 Windows 一键版 |
| 更新源 | Gitee 发行版（国内直连快） |
| 交互 | 启动时自动检查 + 横幅提醒 + 用户手动点"立即升级" |
| 替换机制 | 方案 A：引导 bat 等待旧进程退出后换 exe，失败回滚 |

## 架构

### 版本号

- 新增 `local_app/version.py`：`APP_VERSION = "0.0.0-dev"`（开发态默认值）。
- CI 构建时把 tag 写入该文件（workflow 加一步 sed，如 `0.1.5`），发版无需手动改代码。
- 冻结 exe 里读到的就是构建时的版本；比较用"语义化版本数字元组"（Gitee tag 如 `v0.1.6`，先去掉开头的 `v` 再逐段转 int 比较），不做字符串比较。

### 更新源

- 固定仓库 `gubaoye/GD-DentOS`，公开 API 无需令牌：
  `GET https://gitee.com/api/v5/repos/gubaoye/GD-DentOS/releases/latest`
  → 取 `tag_name`（最新版本）与附件中 `good-dental-clinic-windows.zip` 的下载地址。
- 网络失败 / 非 200 / 结构不符 → 一律静默视为"无更新"，绝不打断正常使用。

### 后端（新增 `local_app/routes/update.py`，FastAPI 路由，遵循现有 routes/ 模式）

| 接口 | 职责 |
|---|---|
| `GET /api/update/check` | 返回 `{current, latest, has_update, notes}`。结果进程内缓存（默认 6 小时），避免每次刷页面都打 Gitee |
| `POST /api/update/download` | 后台线程开始下载 zip 到 exe 同级 `update/new.pkg`（先写 `.part`，完成改名）。单机单任务：已有下载进行中时再次调用直接复用/返回当前状态，不并发下载 |
| `GET /api/update/status` | 返回 `{phase: idle/downloading/ready/error, percent, message}` 供前端进度条轮询 |
| `POST /api/update/apply` | 前置校验（下载完成+校验通过）→ 备份数据库 → 生成引导 bat → 启动 bat → 进程退出 |

权限：复用现有登录态；`apply` 限管理员角色（对齐现有角色权限矩阵的做法）。
非冻结环境（开发态/Mac）：`check` 直接返回 `has_update=false, reason="非 Windows 一键版"`。

### 升级执行序列（`apply` 触发后）

1. **备份**：`backup_util.make_backup(db_path, reason="pre-update")`（现有模块，含命名/去重）。
2. **校验**：zipfile 能打开、内含 `good-dental-clinic.exe`、解压到 `update/new.exe` 成功。
3. **生成引导脚本** `update/update_helper.bat`（内容见下），用 `subprocess.Popen` + `start /min` 启动。
4. **进程退出**：uvicorn 关闭、进程退出；bat 负责后续。

`update_helper.bat` 逻辑：

```
等待 旧exe 路径解锁（循环 try rename，超时 60s 放弃并提示）
旧exe → 重命名为 good-dental-clinic.old.exe（保险）
新exe → 移动到 旧exe 位置；失败 → 把 .old 换回来，弹 msg 提示"升级失败已恢复"
启动 新exe（start "" "新exe"）
删除自身
```

新 exe 启动成功后，启动序列里顺手删除残留的 `.old`（存在即删，失败不阻塞）。

### 前端（`local_app/static/`）

- 新增 `update_banner.js`（遵循现有静态 js 挂载惯例，`?v=` 缓存参数对齐 `test_appt_asset_versions.mjs` 的约束）。
- 页面加载后调 `check`；`has_update=true` 时顶部显示横幅：
  "发现新版本 vX.X.X（当前 vY.Y.Y）【立即升级】【查看更新说明】【暂不】"。
- 点【立即升级】→ 二次确认（提示"升级约需 1-2 分钟，期间系统会重启一次"）→
  `download` → 轮询 `status` 显示进度条 → ready 后自动 `apply` →
  页面显示"正在重启，约 10 秒后自动恢复"并轮询 `/api/health`，恢复后自动刷新页面。
- 【暂不】本次会话不再显示（sessionStorage），不持久打扰。

### 数据安全

- `data/`（数据库、图片、备份）全程只读不动；替换的只是 exe 文件本身。
- 升级前必有 `pre-update` 备份，升坏了可从备份恢复（现有恢复机制）。
- `.old` 保险 + bat 回滚，任何一步失败都能回到旧版。

## 组件职责

| 单元 | 职责 | 依赖 |
|---|---|---|
| `local_app/version.py` | 提供当前版本号 | 无 |
| `local_app/update_checker.py` | 问 Gitee 最新版本、版本比较、结果缓存 | version.py |
| `local_app/update_download.py` | 下载 zip（.part→改名）、zip 校验、解压 exe | update_checker |
| `local_app/update_apply.py` | 备份、生成 bat、启动 bat、退出进程 | backup_util、update_download |
| `local_app/routes/update.py` | 四个 HTTP 接口，薄壳，调上面三个单元 | 上述三者 |
| `static/update_banner.js` | 横幅、确认、进度、重启等待 | 四个接口 |

每个单元可独立测试；HTTP 层只做参数校验和转发。

## 错误处理

| 场景 | 行为 |
|---|---|
| 无网络 / Gitee 不可达 | check 静默返回无更新 |
| 下载中断 | `.part` 保留，重试时从头下载（不做断点续传，YAGNI）；3 次失败提示手动去 Gitee 下载 |
| 磁盘不足 / zip 损坏 | status 返回 error + 中文提示，不进入 apply |
| bat 替换失败 | bat 自动回滚 `.old`，用户看到失败提示，旧版可用 |
| 新版启动失败 | 用户手动把 `.old` 改名回来即可（产品手册补一句）；下次发版修复 |
| 升级中用户关浏览器 | 后端线程继续下载；重启阶段页面重连轮询兜底 |

## 测试

- 单元：版本元组比较、Gitee 响应解析（mock）、zip 校验、bat 文本生成、缓存过期。
- 集成：FastAPI TestClient 打四个接口（mock 下载与 Gitee）。
- 端到端（手工）：本机放旧版 exe + 数据 → Gitee 发测试 tag → 走完整升级流程，
  验证数据完好、`.old` 清理、浏览器自动打开。上线前必跑。

## CI / 发版配套

- `build-release.yml`：打包前把 tag 写进 `local_app/version.py`。
- Gitee 同步清单保持含 `good-dental-clinic-windows.zip`（自动升级唯一依赖的附件）。
- 发版流程不变：打 tag → 云端构建 → 本地 `scripts/sync-gitee-release.sh`。
