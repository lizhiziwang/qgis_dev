# -*- mode: python ; coding: utf-8 -*-
from PyInstaller.utils.hooks import collect_all

datas = [('D:\\zsh\\app\\qgis_3.40\\apps\\qgis-ltr\\python\\qgis', 'qgis'), ('D:\\zsh\\code\\py\\qgis_dev\\resource', 'resource'), ('D:\\zsh\\app\\qgis_3.40\\apps\\qgis-ltr\\resources\\srs.db', 'resources'), ('D:\\zsh\\app\\qgis_3.40\\share\\proj\\proj.db', '.')]
binaries = [('D:\\zsh\\app\\qgis_3.40\\apps\\qgis-ltr\\bin\\qgis_core.dll', '.'), ('D:\\zsh\\app\\qgis_3.40\\apps\\qgis-ltr\\bin\\qgis_gui.dll', '.'), ('D:\\zsh\\app\\qgis_3.40\\bin\\gdal312.dll', '.'), ('D:\\zsh\\app\\qgis_3.40\\bin\\geos_c.dll', '.'), ('D:\\zsh\\app\\qgis_3.40\\bin\\pdalcpp.dll', '.'), ('D:\\zsh\\app\\qgis_3.40\\bin\\proj_9.dll', 'proj'), ('D:\\zsh\\app\\qgis_3.40\\apps\\Qt5\\bin\\Qt5Core.dll', '.'), ('D:\\zsh\\app\\qgis_3.40\\apps\\Qt5\\bin\\Qt5Gui.dll', '.'), ('D:\\zsh\\app\\qgis_3.40\\apps\\Qt5\\bin\\Qt5Widgets.dll', '.'), ('D:\\zsh\\app\\qgis_3.40\\apps\\Qt5\\bin\\Qt5Network.dll', '.'), ('D:\\zsh\\app\\qgis_3.40\\apps\\Qt5\\bin\\Qt5Xml.dll', '.')]
hiddenimports = ['rasterio._features', 'PyQt5.pylupdate', 'PyQt5.pyrcc', 'PyQt5.QAxContainer', 'PyQt5.Qsci', 'PyQt5.Qt', 'PyQt5.QtBluetooth', 'PyQt5.QtCore', 'PyQt5.QtDBus', 'PyQt5.QtDesigner', 'PyQt5.QtGui', 'PyQt5.QtHelp', 'PyQt5.QtLocation', 'PyQt5.QtMultimedia', 'PyQt5.QtMultimediaWidgets', 'PyQt5.QtNetwork', 'PyQt5.QtOpenGL', 'PyQt5.QtPositioning', 'PyQt5.QtPrintSupport', 'PyQt5.QtQml', 'PyQt5.QtQuick', 'PyQt5.QtQuick3D', 'PyQt5.QtQuickWidgets', 'PyQt5.QtRemoteObjects', 'PyQt5.QtSensors', 'PyQt5.QtSerialPort', 'PyQt5.QtSql', 'PyQt5.QtSvg', 'PyQt5.QtTest', 'PyQt5.QtTextToSpeech', 'PyQt5.QtWebChannel', 'PyQt5.QtWebKit', 'PyQt5.QtWebKitWidgets', 'PyQt5.QtWebSockets', 'PyQt5.QtWidgets', 'PyQt5.QtWinExtras', 'PyQt5.QtXml', 'PyQt5.QtXmlPatterns', 'PyQt5.sip', 'PyQt5._QOpenGLFunctions_2_0', 'PyQt5._QOpenGLFunctions_2_1', 'PyQt5._QOpenGLFunctions_4_1_Core']
tmp_ret = collect_all('rasterio')
datas += tmp_ret[0]; binaries += tmp_ret[1]; hiddenimports += tmp_ret[2]
tmp_ret = collect_all('setuptools')
datas += tmp_ret[0]; binaries += tmp_ret[1]; hiddenimports += tmp_ret[2]


block_cipher = None


a = Analysis(
    ['D:\\zsh\\code\\py\\qgis_dev\\main.py'],
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
