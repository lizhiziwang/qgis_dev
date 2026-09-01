# -*- mode: python ; coding: utf-8 -*-
"""统一 PyInstaller spec。

- 入口 ``app/main.py``（不再用 root ``main.py``）
- QGIS 路径从 ``config/app.yaml`` 读，不在 spec 写死
- 单一 spec 给 Windows + Linux 共用；DLL 路径根据平台不同

用法::

    pyinstaller tools/pyinstaller.spec
"""

import os
import sys
from pathlib import Path

# PyInstaller 执行 spec 时命名空间里没有 __file__，改用 SPECPATH（spec 所在目录 tools/）
SPEC_DIR = Path(SPECPATH).resolve()
PROJECT_ROOT = SPEC_DIR.parent
sys.path.insert(0, str(PROJECT_ROOT))

# 读 yaml 配置
try:
    import yaml
    cfg = yaml.safe_load((PROJECT_ROOT / "config" / "app.yaml").read_text(encoding="utf-8"))
except Exception:
    cfg = {}

IS_WIN = sys.platform == "win32"

if IS_WIN:
    # ------------------------------------------------------------------ Windows
    # OSGeo4W 布局：config/app.yaml 的 qgis.install_path 指向安装根（D:/zsh/app/qgis4.2）
    QGIS_INSTALL = cfg.get("qgis", {}).get("install_path", "D:/zsh/app/qgis4.2")
    QGIS_PYTHON = Path(QGIS_INSTALL) / "apps" / "qgis" / "python"
    QGIS_BIN = Path(QGIS_INSTALL) / "apps" / "qgis" / "bin"
    MAIN_BIN = Path(QGIS_INSTALL) / "bin"
    QT6_BIN = Path(QGIS_INSTALL) / "apps" / "qt6" / "bin"

    datas = [
        (str(QGIS_PYTHON / "qgis"), "qgis"),
        (str(PROJECT_ROOT / "resource"), "resource"),
        (str(PROJECT_ROOT / "config"), "config"),
        (str(Path(QGIS_INSTALL) / "apps" / "qgis" / "resources" / "srs.db"), "resources"),
        (str(Path(QGIS_INSTALL) / "share" / "proj" / "proj.db"), "."),
    ]

    binaries = [
        (str(QGIS_BIN / "qgis_core.dll"), "."),
        (str(QGIS_BIN / "qgis_gui.dll"), "."),
        (str(QGIS_BIN / "qgis_analysis.dll"), "."),  # processing 插件 / qgis._analysis 需要
        (str(QGIS_BIN / "qgis_native.dll"), "."),    # qgis_core 依赖
        (str(QGIS_BIN / "qgispython.dll"), "."),     # python 集成
        (str(MAIN_BIN / "gdal313.dll"), "."),
        (str(MAIN_BIN / "geos_c.dll"), "."),
        (str(MAIN_BIN / "pdalcpp.dll"), "."),
        (str(MAIN_BIN / "proj_9.dll"), "proj"),
        (str(QT6_BIN / "Qt6Core.dll"), "."),
        (str(QT6_BIN / "Qt6Gui.dll"), "."),
        (str(QT6_BIN / "Qt6Widgets.dll"), "."),
        (str(QT6_BIN / "Qt6Network.dll"), "."),
        (str(QT6_BIN / "Qt6Xml.dll"), "."),
        (str(QT6_BIN / "Qt6Svg.dll"), "."),
        (str(QT6_BIN / "Qt6Concurrent.dll"), "."),
        # Qt 平台插件（GUI 显示必需）：apps/qt6/plugins/platforms/qwindows.dll → dist/app/platforms/
        (str(QT6_BIN.parent / "plugins" / "platforms"), "platforms"),
    ]
else:
    # ------------------------------------------------------------------ Linux
    # QGIS 由构建环境提供（conda env / apt）。路径不写死：
    # - qgis python 包取当前 Python 能 import 到的 qgis 包目录
    # - 动态库（.so）、Qt 插件、GDAL 数据等由 tools/build_linux.sh 收集进 dist/app/lib 等目录，
    #   不在这里列（Linux 下 dlopen 加载，PyInstaller 静态分析不到，硬列还会因路径不存在而跳过）
    try:
        import qgis  # 构建环境必须有 QGIS（conda activate 后跑）
        _qgis_pkg = Path(qgis.__file__).resolve().parent  # .../site-packages/qgis
    except ImportError:
        raise SystemExit(
            "Linux 打包需要先激活含 QGIS 的 Python 环境（conda activate qgis_build 或系统已装 python3-qgis）"
        )

    datas = [
        (str(_qgis_pkg), "qgis"),
        (str(PROJECT_ROOT / "resource"), "resource"),
        (str(PROJECT_ROOT / "config"), "config"),
    ]
    binaries = []

# 隐藏导入：Win 用 PyQt6（OSGeo4W 版 QGIS 4.2），Linux 用 PyQt5（conda-forge / 麒麟源 QGIS）
_PYQT_PREFIX = "PyQt6" if IS_WIN else "PyQt5"
_PYQT_MODULES = [
    "pylupdate",
    "pyrcc",
    "Qt",
    "QtBluetooth",
    "QtCore",
    "QtDBus",
    "QtDesigner",
    "QtGui",
    "QtHelp",
    "QtMultimedia",
    "QtMultimediaWidgets",
    "QtNetwork",
    "QtOpenGL",
    "QtPositioning",
    "QtPrintSupport",
    "QtQml",
    "QtQuick",
    "QtQuickWidgets",
    "QtRemoteObjects",
    "QtSensors",
    "QtSerialPort",
    "QtSql",
    "QtSvg",
    "QtTest",
    "QtTextToSpeech",
    "QtWebChannel",
    "QtWebSockets",
    "QtWidgets",
    "QtXml",
    "sip",
]
# QtOpenGLWidgets/QtSvgWidgets 仅在 PyQt6 有独立模块（PyQt5 的 QOpenGLWidget 在
# QtWidgets 里，无此绑定模块，conda pyqt 下会报 Hidden import not found）
if IS_WIN:
    _PYQT_MODULES += ["QtOpenGLWidgets", "QtSvgWidgets"]

hiddenimports = [
    "qgis.core",
    "qgis.gui",
    "qgis.utils",
    "qgis.PyQt",
    *(f"{_PYQT_PREFIX}.{m}" for m in _PYQT_MODULES),
    # processing 插件（运行时加载，PyInstaller 分析不到它的 import）需要：
    f"{_PYQT_PREFIX}.uic",  # processing/gui 用 qgis.PyQt.uic 加载 .ui
    "psycopg2",             # processing/algs/gdal/GdalUtils 顶层 import
]

try:
    from PyInstaller.utils.hooks import collect_all, collect_submodules

    # app 包全量打进 PYZ：入口只 import 了 app.bootstrap，但运行时 app.* 大量
    # 动态导入（算法库、工具注册、流程模板等），纯依赖分析易漏——麒麟真机已见
    # 打包后启动报 ``No module named 'app.bootstrap'``。collect_submodules 兜底。
    hiddenimports += collect_submodules("app")

    for pkg in ("setuptools",):
        try:
            tmp = collect_all(pkg)
            datas += tmp[0]
            binaries += tmp[1]
            hiddenimports += tmp[2]
        except Exception:
            pass
except Exception:
    pass

block_cipher = None

a = Analysis(
    [str(PROJECT_ROOT / "scripts" / "entry.py")],
    pathex=[str(PROJECT_ROOT)],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=["processing"],  # 项目根的 processing/ 是 QGIS 临时目录，namespace 包会遮蔽 plugins/processing
    win_no_prefer_redirects=False,
    win_private_assemblies=False,
    cipher=block_cipher,
    noarchive=False,
)

pyz = PYZ(a.pure, a.zipped_data, cipher=block_cipher)

# EXE 名：Windows 用 app（app.exe）；Linux 用 qgis_dev。
# Linux 不能叫 app：PyInstaller 6.x 的 PyiFrozenFinder 对包路径做 isfile 检查
# （#8767 防 runpy），而 Python 包名也是 app → 包 __path__ = dist/app/app 恰好是
# 可执行文件 → 子包（app.bootstrap 等）导入被拒 → No module named 'app.bootstrap'。
_EXE_NAME = "app" if IS_WIN else "qgis_dev"

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name=_EXE_NAME,
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    # PyInstaller 6.x 默认把 bundled 文件放进 `_internal/`；这里关掉 contents
    # 目录，恢复旧 onedir 布局（datas/binaries 落在 dist/app 根下），否则
    # build_windows.ps1 的 STAGE 4 校验与运行时便携探测（app.exe 旁找
    # `qgis/`+`proj.db`）都会因路径变化而失败。
    contents_directory=".",
)

coll = COLLECT(
    exe,
    a.binaries,
    a.zipfiles,
    a.datas,
    strip=False,
    upx=True,
    upx_exclude=[],
    name="app",
)
