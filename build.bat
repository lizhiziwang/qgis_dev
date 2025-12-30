@echo off
setlocal EnableDelayedExpansion

echo Starting PyQGIS application build... THE BATTLE-TESTED FINAL VERSION ...
echo.

rem --- [请根据你的实际情况修改此部分] ---
rem --- 这是唯一需要你手动配置的地方 ---
set "QGIS_INSTALL_PATH=D:\pf\qgis"
set "MAIN_SCRIPT=main.py"
set "APP_NAME=MyQgisApp"

rem --- [脚本核心配置，无需修改] ---
set "PROJECT_ROOT=%~dp0"
set "DIST_PATH=%PROJECT_ROOT%dist\%APP_NAME%"
set "QGIS_PYTHON_LAUNCHER=%QGIS_INSTALL_PATH%\bin\python-qgis-ltr.bat"
set "QGIS_PYTHON_PACKAGES_PATH=%QGIS_INSTALL_PATH%\apps\qgis-ltr\python"
set "QGIS_APP_BIN_PATH=%QGIS_INSTALL_PATH%\apps\qgis-ltr\bin"
set "QT5_BIN_PATH=%QGIS_INSTALL_PATH%\apps\Qt5\bin"
set "QGIS_MAIN_BIN_PATH=%QGIS_INSTALL_PATH%\bin"

rem --- 自动检测 GDAL/OGR/OSR DLL 文件名 ---
for /f "delims=" %%f in ('dir /b "%QGIS_MAIN_BIN_PATH%\gdal*.dll" 2^>nul') do set "GDAL_DLL=%%f"

rem 如果 OGR/OSR 没找到，尝试从 GDAL 推断（常见于 QGIS 3.40）

if not defined GDAL_DLL (
    echo ERROR: 未在 "%QGIS_MAIN_BIN_PATH%" 找到 gdal*.dll！请检查 QGIS 安装路径。
    echo 可能的路径示例: D:\OSGeo4W64\bin 或 D:\pf\qgis\bin
    pause
    exit /b 1
)

echo 自动识别的 QGIS 依赖 DLL:
echo   GDAL: %GDAL_DLL%
echo.

rem --- 预构建 add-binary 参数（避免延迟扩展在 call 中失效）---
set "ADD_GDAL=--add-binary=%QGIS_MAIN_BIN_PATH%\%GDAL_DLL%;osgeo\gdal.dll"

rem --- STAGE 1: 清理旧构建 ---
echo STAGE 1: Cleaning up old build files...
if exist "%PROJECT_ROOT%dist" rmdir /s /q "%PROJECT_ROOT%dist"
if exist "%PROJECT_ROOT%build" rmdir /s /q "%PROJECT_ROOT%build"
if exist "%PROJECT_ROOT%\%APP_NAME%.spec" del "%PROJECT_ROOT%\%APP_NAME%.spec"

rem --- STAGE 2: 使用 QGIS 环境运行 PyInstaller ---
echo.
echo STAGE 2: Running PyInstaller via QGIS Environment...

call "%QGIS_PYTHON_LAUNCHER%" -m PyInstaller ^
    --noconfirm ^
    --name %APP_NAME% ^
    --onedir ^
    --windowed ^
    --hidden-import=rasterio._features ^
    --hidden-import=osgeo ^
    --hidden-import=osgeo.gdal ^
    --hidden-import=qgis.core ^
    --hidden-import=qgis.gui ^
    --hidden-import=qgis.utils ^
    --collect-all rasterio ^
    --collect-all setuptools ^
    --collect-all qgis ^
    --add-data "%QGIS_PYTHON_PACKAGES_PATH%\qgis;qgis" ^
    --add-data "%PROJECT_ROOT%resource;resource" ^
    --add-binary "%QGIS_APP_BIN_PATH%\qgis_core.dll;." ^
    --add-binary "%QGIS_APP_BIN_PATH%\qgis_gui.dll;." ^
    %ADD_GDAL% ^
    --add-binary "%QGIS_MAIN_BIN_PATH%\geos_c.dll;." ^
    --add-binary "%QGIS_MAIN_BIN_PATH%\pdalcpp.dll;." ^
    --add-binary "%QGIS_MAIN_BIN_PATH%\proj_9.dll;proj.dll" ^
    --add-binary "%QT5_BIN_PATH%\Qt5Core.dll;." ^
    --add-binary "%QT5_BIN_PATH%\Qt5Gui.dll;." ^
    --add-binary "%QT5_BIN_PATH%\Qt5Widgets.dll;." ^
    --add-binary "%QT5_BIN_PATH%\Qt5Network.dll;." ^
    --add-binary "%QT5_BIN_PATH%\Qt5Xml.dll;." ^
    "%PROJECT_ROOT%\%MAIN_SCRIPT%"

if errorlevel 1 (
    echo.
    echo ERROR: PyInstaller failed. Build aborted.
    pause
    exit /b 1
)

rem --- STAGE 3: 复制 QGIS 运行环境，实现绿色便携 ---
echo.
echo STAGE 3: Copying QGIS environment files for portability...

if not exist "%DIST_PATH%" (
    echo ERROR: dist folder not created. PyInstaller may have failed silently.
    pause
    exit /b 1
)

echo   - Copying 'etc' folder...
xcopy /E /I /Y "%QGIS_INSTALL_PATH%\etc" "%DIST_PATH%\etc\" >nul

echo   - Copying Python core files...
xcopy /Y "%QGIS_INSTALL_PATH%\bin\python312.dll" "%DIST_PATH%\" >nul
xcopy /Y "%QGIS_INSTALL_PATH%\bin\python3.dll" "%DIST_PATH%\" >nul


echo   - Copying 'apps\qgis\resources' folder...
xcopy /E /I /Y "%QGIS_INSTALL_PATH%\apps\qgis\resources" "%DIST_PATH%\qgis\resources\" >nul

echo   - Copying 'apps\qgis\translations' folder...
xcopy /E /I /Y "%QGIS_INSTALL_PATH%\apps\qgis\translations" "%DIST_PATH%\qgis\translations\" >nul

echo   - Copying environment launcher 'o4w_env.bat'...
xcopy /Y "%QGIS_INSTALL_PATH%\bin\o4w_env.bat" "%DIST_PATH%\" >nul

echo.
echo =================================
echo   B U I L D   C O M P L E T E !
echo =================================
echo.
echo Portable application ready at:
echo %DIST_PATH%
echo.
echo Tip: Run %APP_NAME%.exe from its folder (do not move it alone).
echo.

endlocal
pause