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

hiddenimports = [
    "qgis.core",
    "qgis.gui",
    "qgis.utils",
    "PyQt6.pylupdate",
    "PyQt6.pyrcc",
    "PyQt6.Qt",
    "PyQt6.QtBluetooth",
    "PyQt6.QtCore",
    "PyQt6.QtDBus",
    "PyQt6.QtDesigner",
    "PyQt6.QtGui",
    "PyQt6.QtHelp",
    "PyQt6.QtMultimedia",
    "PyQt6.QtMultimediaWidgets",
    "PyQt6.QtNetwork",
    "PyQt6.QtOpenGL",
    "PyQt6.QtOpenGLWidgets",
    "PyQt6.QtPositioning",
    "PyQt6.QtPrintSupport",
    "PyQt6.QtQml",
    "PyQt6.QtQuick",
    "PyQt6.QtQuickWidgets",
    "PyQt6.QtRemoteObjects",
    "PyQt6.QtSensors",
    "PyQt6.QtSerialPort",
    "PyQt6.QtSql",
    "PyQt6.QtSvg",
    "PyQt6.QtSvgWidgets",
    "PyQt6.QtTest",
    "PyQt6.QtTextToSpeech",
    "PyQt6.QtWebChannel",
    "PyQt6.QtWebSockets",
    "PyQt6.QtWidgets",
    "PyQt6.QtXml",
    "PyQt6.sip",
    # processing 插件（运行时加载，PyInstaller 分析不到它的 import）需要：
    "PyQt6.uic",      # processing/gui 用 qgis.PyQt.uic 加载 .ui
    "psycopg2",       # processing/algs/gdal/GdalUtils 顶层 import
]

try:
    from PyInstaller.utils.hooks import collect_all

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

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="app",
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
