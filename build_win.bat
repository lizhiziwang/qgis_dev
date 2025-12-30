@echo off
setlocal
echo Starting PyQGIS application build... THE BATTLE-TESTED FINAL VERSION ...

rem --- qgis安装位置 ---
set "QGIS_INSTALL_PATH=D:\pf\qgis"

rem --- [脚本核心配置，通常无需修改] ---
set "QGIS_PYTHON_LAUNCHER=%QGIS_INSTALL_PATH%\bin\python-qgis-ltr.bat"
set "PROJECT_ROOT=%~dp0"
set "MAIN_SCRIPT=%PROJECT_ROOT%main.py"
set "DIST_PATH=%PROJECT_ROOT%dist\app"
set "QGIS_PYTHON_PACKAGES_PATH=%QGIS_INSTALL_PATH%\apps\qgis-ltr\python"
set "QGIS_APP_BIN_PATH=%QGIS_INSTALL_PATH%\apps\qgis-ltr\bin"
set "QT5_BIN_PATH=%QGIS_INSTALL_PATH%\apps\Qt5\bin"
set "OSGEO_DLL_PATH=%QGIS_INSTALL_PATH%\bin"
set "QGIS_MAIN_BIN_PATH=%QGIS_INSTALL_PATH%\bin"

rem --- STAGE 1: 清理旧的打包文件 ---
echo.
echo STAGE 1: Cleaning up old build files...
if exist "%PROJECT_ROOT%dist" ( rmdir /s /q "%PROJECT_ROOT%dist" )
if exist "%PROJECT_ROOT%build" ( rmdir /s /q "%PROJECT_ROOT%build" )
if exist "%PROJECT_ROOT%app.spec" ( del "%PROJECT_ROOT%app.spec" )

rem --- STAGE 2: 使用 QGIS 环境运行 PyInstaller ---
echo.
echo STAGE 2: Running PyInstaller via QGIS Environment...
call "%QGIS_PYTHON_LAUNCHER%" -m PyInstaller ^
    --noconfirm ^
    --name app ^
    --onedir ^
    --windowed ^
    --hidden-import=rasterio._features ^
    --hidden-import=PyQt5.pylupdate ^
    --hidden-import=PyQt5.pyrcc ^
    --hidden-import=PyQt5.QAxContainer ^
    --hidden-import=PyQt5.Qsci ^
    --hidden-import=PyQt5.Qt ^
    --hidden-import=PyQt5.QtBluetooth ^
    --hidden-import=PyQt5.QtCore ^
    --hidden-import=PyQt5.QtDBus ^
    --hidden-import=PyQt5.QtDesigner ^
    --hidden-import=PyQt5.QtGui ^
    --hidden-import=PyQt5.QtHelp ^
    --hidden-import=PyQt5.QtLocation ^
    --hidden-import=PyQt5.QtMultimedia ^
    --hidden-import=PyQt5.QtMultimediaWidgets ^
    --hidden-import=PyQt5.QtNetwork ^
    --hidden-import=PyQt5.QtOpenGL ^
    --hidden-import=PyQt5.QtPositioning ^
    --hidden-import=PyQt5.QtPrintSupport ^
    --hidden-import=PyQt5.QtQml ^
    --hidden-import=PyQt5.QtQuick ^
    --hidden-import=PyQt5.QtQuick3D ^
    --hidden-import=PyQt5.QtQuickWidgets ^
    --hidden-import=PyQt5.QtRemoteObjects ^
    --hidden-import=PyQt5.QtSensors ^
    --hidden-import=PyQt5.QtSerialPort ^
    --hidden-import=PyQt5.QtSql ^
    --hidden-import=PyQt5.QtSvg ^
    --hidden-import=PyQt5.QtTest ^
    --hidden-import=PyQt5.QtTextToSpeech ^
    --hidden-import=PyQt5.QtWebChannel ^
    --hidden-import=PyQt5.QtWebKit ^
    --hidden-import=PyQt5.QtWebKitWidgets ^
    --hidden-import=PyQt5.QtWebSockets ^
    --hidden-import=PyQt5.QtWidgets ^
    --hidden-import=PyQt5.QtWinExtras ^
    --hidden-import=PyQt5.QtXml ^
    --hidden-import=PyQt5.QtXmlPatterns ^
    --hidden-import=PyQt5.sip ^
    --hidden-import=PyQt5._QOpenGLFunctions_2_0 ^
    --hidden-import=PyQt5._QOpenGLFunctions_2_1 ^
    --hidden-import=PyQt5._QOpenGLFunctions_4_1_Core ^
    --collect-all rasterio ^
    --collect-all setuptools ^
    --add-data "%QGIS_PYTHON_PACKAGES_PATH%\qgis;qgis" ^
    --add-data "%PROJECT_ROOT%resource;resource" ^
    --add-data "%QGIS_INSTALL_PATH%\apps\qgis-ltr\resources\srs.db;resources" ^
    --add-data "%QGIS_INSTALL_PATH%\share\proj\proj.db;." ^
    --add-binary "%QGIS_APP_BIN_PATH%\qgis_core.dll;." ^
    --add-binary "%QGIS_APP_BIN_PATH%\qgis_gui.dll;." ^
    --add-binary "%OSGEO_DLL_PATH%\gdal311.dll;." ^
    --add-binary "%QGIS_MAIN_BIN_PATH%\geos_c.dll;." ^
    --add-binary "%QGIS_MAIN_BIN_PATH%\pdalcpp.dll;." ^
    --add-binary "%QGIS_MAIN_BIN_PATH%\proj_9.dll;proj" ^
    --add-binary "%QT5_BIN_PATH%\Qt5Core.dll;." ^
    --add-binary "%QT5_BIN_PATH%\Qt5Gui.dll;." ^
    --add-binary "%QT5_BIN_PATH%\Qt5Widgets.dll;." ^
    --add-binary "%QT5_BIN_PATH%\Qt5Network.dll;." ^
    --add-binary "%QT5_BIN_PATH%\Qt5Xml.dll;." ^
    "%MAIN_SCRIPT%"

rem --- STAGE 3: 复制 QGIS 运行环境，实现绿色便携 ---
echo.
echo STAGE 3: Copying QGIS environment files for portability...
if exist "%DIST_PATH%" (
    echo   - Copying 'etc' folder...
    xcopy /E /I /Y "%QGIS_INSTALL_PATH%\etc" "%DIST_PATH%\etc\"
    echo   - Copying environment launcher 'o4w_env.bat'...
    xcopy /Y "%QGIS_INSTALL_PATH%\bin\o4w_env.bat" "%DIST_PATH%\"

    echo   - Copying 'plugins' folder for data browser support
    xcopy /E /I /Y "%QGIS_INSTALL_PATH%\apps\qgis-ltr\plugins" "%DIST_PATH%\plugins\"

    echo   - Copying '翻译插件'
    xcopy /E /I /Y "%QGIS_INSTALL_PATH%\apps\qgis-ltr\i18n" "%DIST_PATH%\i18n\"

    echo   - Copying 'providers' folder (optional, if exists)
    if exist "%QGIS_INSTALL_PATH%\apps\qgis-ltr\providers" (
        xcopy /E /I /Y "%QGIS_INSTALL_PATH%\apps\qgis-ltr\providers" "%DIST_PATH%\providers\"
    )
    echo.
    echo =================================
    echo   B U I L D   C O M P L E T E !
    echo =================================
    echo.
    echo The portable application is in:
    echo %DIST_PATH%
    echo.
) else (
    echo.
    echo ERROR: PyInstaller build failed. Could not find dist folder.
    echo Please check the error messages above.
)

endlocal
exit /b