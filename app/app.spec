# -*- mode: python ; coding: utf-8 -*-
from PyInstaller.utils.hooks import collect_all

datas = [('D:\\zsh\\app\\qgis4.2\\apps\\qgis\\python\\qgis', 'qgis'), ('D:\\zsh\\code\\py\\qgis_dev\\app\\resource', 'resource'), ('D:\\zsh\\app\\qgis4.2\\apps\\qgis\\resources\\srs.db', 'resources'), ('D:\\zsh\\app\\qgis4.2\\share\\proj\\proj.db', '.')]
binaries = [('D:\\zsh\\app\\qgis4.2\\apps\\qgis\\bin\\qgis_core.dll', '.'), ('D:\\zsh\\app\\qgis4.2\\apps\\qgis\\bin\\qgis_gui.dll', '.'), ('D:\\zsh\\app\\qgis4.2\\apps\\qgis\\bin\\qgis_analysis.dll', '.'), ('D:\\zsh\\app\\qgis4.2\\apps\\qgis\\bin\\qgis_native.dll', '.'), ('D:\\zsh\\app\\qgis4.2\\apps\\qgis\\bin\\qgispython.dll', '.'), ('D:\\zsh\\app\\qgis4.2\\bin\\gdal313.dll', '.'), ('D:\\zsh\\app\\qgis4.2\\bin\\geos_c.dll', '.'), ('D:\\zsh\\app\\qgis4.2\\bin\\pdalcpp.dll', '.'), ('D:\\zsh\\app\\qgis4.2\\bin\\proj_9.dll', 'proj'), ('D:\\zsh\\app\\qgis4.2\\apps\\qt6\\bin\\Qt6Core.dll', '.'), ('D:\\zsh\\app\\qgis4.2\\apps\\qt6\\bin\\Qt6Gui.dll', '.'), ('D:\\zsh\\app\\qgis4.2\\apps\\qt6\\bin\\Qt6Widgets.dll', '.'), ('D:\\zsh\\app\\qgis4.2\\apps\\qt6\\bin\\Qt6Network.dll', '.'), ('D:\\zsh\\app\\qgis4.2\\apps\\qt6\\bin\\Qt6Xml.dll', '.'), ('D:\\zsh\\app\\qgis4.2\\apps\\qt6\\bin\\Qt6Svg.dll', '.'), ('D:\\zsh\\app\\qgis4.2\\apps\\qt6\\bin\\Qt6Concurrent.dll', '.')]
hiddenimports = ['rasterio._features', 'PyQt6.pylupdate', 'PyQt6.pyrcc', 'PyQt6.Qt', 'PyQt6.QtBluetooth', 'PyQt6.QtCore', 'PyQt6.QtDBus', 'PyQt6.QtDesigner', 'PyQt6.QtGui', 'PyQt6.QtHelp', 'PyQt6.QtMultimedia', 'PyQt6.QtMultimediaWidgets', 'PyQt6.QtNetwork', 'PyQt6.QtOpenGL', 'PyQt6.QtOpenGLWidgets', 'PyQt6.QtPositioning', 'PyQt6.QtPrintSupport', 'PyQt6.QtQml', 'PyQt6.QtQuick', 'PyQt6.QtQuickWidgets', 'PyQt6.QtRemoteObjects', 'PyQt6.QtSensors', 'PyQt6.QtSerialPort', 'PyQt6.QtSql', 'PyQt6.QtSvg', 'PyQt6.QtSvgWidgets', 'PyQt6.QtTest', 'PyQt6.QtTextToSpeech', 'PyQt6.QtWebChannel', 'PyQt6.QtWebSockets', 'PyQt6.QtWidgets', 'PyQt6.QtXml', 'PyQt6.sip', 'PyQt6.uic']
tmp_ret = collect_all('rasterio')
datas += tmp_ret[0]; binaries += tmp_ret[1]; hiddenimports += tmp_ret[2]
tmp_ret = collect_all('setuptools')
datas += tmp_ret[0]; binaries += tmp_ret[1]; hiddenimports += tmp_ret[2]


block_cipher = None


a = Analysis(
    ['D:\\zsh\\code\\py\\qgis_dev\\app\\main.py'],
    pathex=[],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
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
    name='app',
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
    name='app',
)
