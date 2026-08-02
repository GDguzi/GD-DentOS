# 开源诊所系统 · macOS 一键包（PyInstaller onefile）
# 本地构建（Mac 上）：
#   pip install -r requirements.txt pyinstaller
#   pyinstaller packaging/macos/good-dental-mac.spec --distpath dist --workpath build --clean --noconfirm
# 产物 dist/good-dental-clinic 改名为 .command 后双击即在终端启动（打包脚本已代劳）。
# CI 自动构建 + 冒烟验证见 .github/workflows/build-macos.yml
import os

from PyInstaller.utils.hooks import collect_data_files, collect_submodules

# spec 里的相对路径按 spec 所在目录解析,不是仓库根 —— 一律用 SPECPATH 锚定仓库根。
ROOT = os.path.abspath(os.path.join(SPECPATH, "..", ".."))

# 运行时按模块 __file__ 定位的资源，必须随包携带（同 Windows spec）：
datas = [
    (os.path.join(ROOT, "local_app", "static"), "local_app/static"),
    (os.path.join(ROOT, "local_app", "schema.sql"), "local_app"),
    (os.path.join(ROOT, "local_app", "personnel_access_schema.sql"), "local_app"),
]
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
    console=True,  # 首启账号密码/网址都打印在终端窗口
    icon=None,
)
