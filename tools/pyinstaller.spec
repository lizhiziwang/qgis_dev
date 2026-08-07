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

QGIS_INSTALL = cfg.get("qgis", {}).get("install_path", "D:/zsh/app/qgis_3.40")
QGIS_PYTHON = Path(QGIS_INSTALL) / "apps" / "qgis-ltr" / "python"
QGIS_BIN = Path(QGIS_INSTALL) / "apps" / "qgis-ltr" / "bin"
MAIN_BIN = Path(QGIS_INSTALL) / "bin"
QT5_BIN = Path(QGIS_INSTALL) / "apps" / "Qt5" / "bin"

datas = [
    (str(QGIS_PYTHON / "qgis"), "qgis"),
    (str(PROJECT_ROOT / "resource"), "resource"),
    (str(PROJECT_ROOT / "config"), "config"),
    (str(Path(QGIS_INSTALL) / "apps" / "qgis-ltr" / "resources" / "srs.db"), "resources"),
    (str(Path(QGIS_INSTALL) / "share" / "proj" / "proj.db"), "."),
]

binaries = [
    (str(QGIS_BIN / "qgis_core.dll"), "."),
    (str(QGIS_BIN / "qgis_gui.dll"), "."),
    (str(QGIS_BIN / "qgis_analysis.dll"), "."),  # processing 插件 / qgis._analysis 需要
    (str(MAIN_BIN / "gdal311.dll"), "."),
    (str(MAIN_BIN / "geos_c.dll"), "."),
    (str(MAIN_BIN / "pdalcpp.dll"), "."),
    (str(MAIN_BIN / "proj_9.dll"), "proj"),
    (str(QT5_BIN / "Qt5Core.dll"), "."),
    (str(QT5_BIN / "Qt5Gui.dll"), "."),
    (str(QT5_BIN / "Qt5Widgets.dll"), "."),
    (str(QT5_BIN / "Qt5Network.dll"), "."),
    (str(QT5_BIN / "Qt5Xml.dll"), "."),
    # Qt 平台插件（GUI 显示必需）：apps/Qt5/plugins/platforms/qwindows.dll → dist/app/platforms/
    (str(QT5_BIN.parent / "plugins" / "platforms"), "platforms"),
]

hiddenimports = [
    "qgis.core",
    "qgis.gui",
    "qgis.utils",
    "PyQt5.pylupdate",
    "PyQt5.pyrcc",
    "PyQt5.QAxContainer",
    "PyQt5.Qsci",
    "PyQt5.Qt",
    "PyQt5.QtBluetooth",
    "PyQt5.QtCore",
    "PyQt5.QtDBus",
    "PyQt5.QtDesigner",
    "PyQt5.QtGui",
    "PyQt5.QtHelp",
    "PyQt5.QtLocation",
    "PyQt5.QtMultimedia",
    "PyQt5.QtMultimediaWidgets",
    "PyQt5.QtNetwork",
    "PyQt5.QtOpenGL",
    "PyQt5.QtPositioning",
    "PyQt5.QtPrintSupport",
    "PyQt5.QtQml",
    "PyQt5.QtQuick",
    "PyQt5.QtQuick3D",
    "PyQt5.QtQuickWidgets",
    "PyQt5.QtRemoteObjects",
    "PyQt5.QtSensors",
    "PyQt5.QtSerialPort",
    "PyQt5.QtSql",
    "PyQt5.QtSvg",
    "PyQt5.QtTest",
    "PyQt5.QtTextToSpeech",
    "PyQt5.QtWebChannel",
    "PyQt5.QtWebKit",
    "PyQt5.QtWebKitWidgets",
    "PyQt5.QtWebSockets",
    "PyQt5.QtWidgets",
    "PyQt5.QtWinExtras",
    "PyQt5.QtXml",
    "PyQt5.QtXmlPatterns",
    "PyQt5.sip",
    "PyQt5._QOpenGLFunctions_2_0",
    "PyQt5._QOpenGLFunctions_2_1",
    "PyQt5._QOpenGLFunctions_4_1_Core",
    # processing 插件（运行时加载，PyInstaller 分析不到它的 import）需要：
    "PyQt5.uic",      # processing/gui 用 qgis.PyQt.uic 加载 .ui
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
