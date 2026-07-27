# GOOD 牙科管理软件

> 一个**单机/局域网**的口腔诊所管理系统：患者档案、预约、病历、处置划价、收费退费、
> 回访、库存、消毒、技工单、报表、角色权限。数据全部存在你自己的电脑上（SQLite 单文件），
> **不上云、不联网也能用**——数据主权在诊所自己手里。
>
> 由一线口腔门诊在日常诊疗中真实使用、打磨后开源。

## Windows 一键使用（免装 Python，推荐给诊所前台电脑）

到本仓库 **Releases** 下载 `good-dental-clinic-windows.zip`，解压后双击
`good-dental-clinic.exe`：

1. 黑窗口（控制台）打印本机网址和**首启账号与随机初始密码**（也存在 `data/.admin_initial_password`）；
2. 浏览器自动打开 `http://127.0.0.1:8765`，登录即用；
3. 全部数据在 exe 同级的 `data/` 文件夹——**整个文件夹拷走就是备份**；
4. 关闭黑窗口 = 停止系统。正式使用请登录后到「账号安全」修改密码。

默认只监听本机。要让同一 WiFi 的 iPad/手机访问（签字/查看），设环境变量
`DENTAL_LOCAL_ONLY=0` 再启动（非本机设备需访问口令，见下表）。

> exe 由 GitHub Actions 在干净的 Windows 机器上自动构建并做启动冒烟验证
> （见 `.github/workflows/build-windows.yml`）。想自己打包：
> `pip install -r requirements.txt pyinstaller`，再执行
> `pyinstaller packaging/windows/good-dental.spec --distpath dist --clean --noconfirm`。

## 快速开始（源码方式）

```bash
git clone <本仓库>
cd <目录>
python3 -m venv venv && source venv/bin/activate   # Windows: venv\Scripts\activate
pip install -r requirements.txt
python3 -m local_app.run_local
```

打开 `http://127.0.0.1:8765`：

1. 首次启动自动创建管理员账号，用户名/初始密码打印在终端（也存在 `local_app/data/.admin_initial_password`）。**登录后请立即改密。**
2. 登录后会引导你填写**诊所名称**——登录页、打印单头、病历卡从此显示你的诊所名。
3. 在「配置管理」里按自家情况调整：诊室列表、营业时间、收费项目、员工与角色权限、付款方式、患者来源字典。

### 试试演示数据（可选）

不想从空库开始？一键生成一个带合成假患者的演示库（纯合成数据，与任何真实患者无关）：

```bash
python3 -m local_app.demo.seed_demo
DENTAL_DB=local_app/data/demo/demo_clinic.sqlite3 DENTAL_PORT=8770 python3 -m local_app.run_local
```

## 部署形态

| 环境变量 | 作用 |
|---|---|
| （默认） | 监听 `0.0.0.0:8765`，同一 WiFi 的 iPad/手机可打开（签字/查看），非本机设备需访问口令 |
| `DENTAL_LOCAL_ONLY=1` | 只监听本机，局域网不可见 |
| `DENTAL_PORT=xxxx` | 换端口（多实例并行） |
| `DENTAL_DB=/path/x.sqlite3` | 指定数据库文件（默认 `local_app/data/clinic.sqlite3`） |
| `DENTAL_IMAGES=/path/dir` | 指定患者影像目录（默认 `local_app/data/images`） |
| `DENTAL_ACCESS_PASSWORD=xxx` | 自设局域网访问口令（默认首启随机生成并存 `data/.lan_password`） |
| `DENTAL_ADMIN_PASSWORD=xxx` | 预设首启 admin 初始密码（不设则随机生成并打印，含一键版） |

数据备份：系统内置「数据备份」入口（配置管理），也可直接拷走 `local_app/data/` 整个目录。

## AI 病历草稿（可选，不内置任何大模型）

系统留了一个开放接口：你用任何方式生成病历草稿 Markdown（自建语音管线、任何 LLM、手写脚本），
丢进约定目录并执行摄入命令，医生在「AI 草稿箱」里审核确认后才成为正式病历。
不用 AI 时，手写病历流程完全不受影响。格式与命令见 [`docs/开源发布_AI草稿接口.md`](docs/开源发布_AI草稿接口.md)。

## 拍照建档（可选，仅 macOS）

在「新增患者」弹窗里拍身份证 / 登记表 / 微信截图，字段自动预填，前台核对后保存。
识别用 macOS 系统自带的 Vision 框架：**全程本机运行，照片不落盘、不上传、不进备份**。
身份证号认出后，生日和性别是从号码本身**算**出来的（不是认出来的），校验位还能验出号码真伪。
识别不准的字段会标黄提示核对；识别到的每行原文都能一键点进对应字段，兜底任何没见过的版式。

macOS 系统 python3（`/usr/bin/python3`）已预装所需依赖，**开箱可用**；
用 Homebrew/pyenv 的 Python 则需 `pip install -r requirements-ocr.txt`。
非 macOS 或未安装时，该入口自动隐藏，其余功能不受影响。

## 文档

- [产品手册](docs/产品手册.md)：安装、首启、每日动线、备份迁移、局域网、功能地图
- [常见问题解答（FAQ）](docs/常见问题解答.md)：忘记密码、端口占用、换电脑迁移、iPad 连接、升级、拍照建档、AI 草稿箱
- [AI 病历草稿接口](docs/开源发布_AI草稿接口.md)：第三方喂草稿的格式与命令

## 技术形态

- 后端：Python + FastAPI + SQLite（标准库 sqlite3），无 ORM、无消息队列、无外部服务。
- 前端：原生 HTML/CSS/JS，**零构建**——clone 下来就能跑，改一行刷新即生效。
- 测试：1200+ 后端单测 + 前端源码级回归。先装开发依赖再跑：`pip install -r requirements-dev.txt && python3 -m unittest discover -s test -q`（`requirements.txt` 只含运行依赖，测试额外需要 `httpx`）。
- 隐私默认：患者数据只在 `local_app/data/`（已 gitignore），日志不含患者明细。

## 边界声明

- 本系统是**诊所内部管理工具**，不含对外挂号/商城/会员营销等互联网功能。
- 不与任何第三方 SaaS 交互；如需从原有系统迁移数据，请自行导出导入。
- 医疗文书的合规性（病历书写规范、知情同意书法律效力等）请按当地法规自查。

## 协议与商业授权

本项目以 **AGPL-3.0** 协议开源（见 [LICENSE](LICENSE)）。一句话说清谁能怎么用：

| 你是谁 | 能不能用 |
|---|---|
| **开诊所的同行**，拿去管自己的诊所 | ✅ **免费，随便用、随便改**，不用告诉任何人 |
| 个人学习、研究、二次开发自用 | ✅ 免费 |
| 公司拿去改一改、包装成产品**对外销售** | ⚠️ 必须**以 AGPL-3.0 公开你的全部修改源码** |
| 公司拿去部署成**网页版/SaaS 对外提供服务** | ⚠️ 同样必须**公开全部修改源码**（AGPL 的核心条款） |
| 想**闭源**商用、不愿公开自己的改动 | 💰 **不允许**，须另行向作者购买商业授权 |

说白了：**自己诊所用，随便；拿去卖钱，就得把代码交出来**——不想交，就来谈授权。

版权人保留**双授权**权利。需要闭源商用、定制开发或部署支持，请通过 GitHub Issues 联系作者。

---

made by GD古仔——一个自己写代码的基层牙医。
