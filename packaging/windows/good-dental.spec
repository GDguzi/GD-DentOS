# 开源诊所系统 · Windows 一键 exe 打包（PyInstaller onefile）
# 本地构建（Windows 上）：
#   pip install -r requirements.txt pyinstaller
#   pyinstaller packaging/windows/good-dental.spec --distpath dist --workpath build --clean --noconfirm
# CI 自动构建 + 冒烟验证见 .github/workflows/build-release.yml
import os

from PyInstaller.utils.hooks import collect_data_files, collect_submodules

# spec 里的相对路径按 spec 所在目录解析,不是仓库根 —— 一律用 SPECPATH 锚定仓库根。
ROOT = os.path.abspath(os.path.join(SPECPATH, "..", ".."))

# 运行时按模块 __file__ 定位的资源，必须随包携带：
# - local_app/static            前端页面（api.py 挂载 StaticFiles）
# - schema.sql / personnel_access_schema.sql  建库与 v3 首启引导（db/v3_bootstrap 读取）
datas = [
    (os.path.join(ROOT, "local_app", "static"), "local_app/static"),
    (os.path.join(ROOT, "local_app", "schema.sql"), "local_app"),
    (os.path.join(ROOT, "local_app", "personnel_access_schema.sql"), "local_app"),
    (os.path.join(ROOT, "local_app", "consent_templates_seed.json"), "local_app"),
]

# Windows 无系统时区库:随包携带 tzdata 的 IANA 数据,否则 ZoneInfo("Asia/Shanghai") 启动即崩
datas += collect_data_files("tzdata")

hiddenimports = collect_submodules("uvicorn") + collect_submodules("fastapi") + ["tzdata"]

a = Analysis(
    [os.path.join(ROOT, "local_app", "desktop_launcher.py")],
    pathex=[ROOT],
    binaries=[],
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    runtime_hooks=[],
    excludes=["tkinter", "matplotlib", "numpy", "pandas"],
    noarchive=False,
)
pyz = PYZ(a.pure, a.zipped_data)
exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name="good-dental-clinic",
    console=True,  # 保留控制台：首启账号密码/网址/端口提示都打印在这里
    icon=None,
)
