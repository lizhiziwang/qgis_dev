@echo off
REM Desktop launcher: keep this file in the same directory as app.exe (dist\app\).
REM It sets the QGIS env vars and starts app.exe.
REM app.exe also auto-detects the portable bundle; this is an explicit fallback.
REM
REM Layout it expects next to app.exe:
REM   runtime\        QGIS 运行时（proj.db / etc / plugins / i18n / providers / platforms）
REM   config\        应用配置
REM   resource\      应用资源
REM   _internal\     Python 运行时与依赖库

setlocal
set "DIST_DIR=%~dp0"

if not exist "%DIST_DIR%app.exe" (
    echo ERROR: %DIST_DIR%app.exe not found. Put this script next to app.exe.
    pause
    exit /b 1
)

REM Portable packaging: QGIS runtime data lives under runtime\.
set "QGIS_PREFIX=%DIST_DIR%runtime"

REM config / resource live at the top level; point the app at them.
set "QGIS_DEV_CONFIG=%DIST_DIR%config\app.yaml"
set "QGIS_DEV_PATHS_PROJECT_ROOT=%DIST_DIR%"
set "QGIS_DEV_PATHS_RESOURCE=%DIST_DIR%resource"
set "QGIS_DEV_PATHS_LOG=%DIST_DIR%logs"
set "QGIS_DEV_PATHS_TMP=%DIST_DIR%.tmp"
set "QGIS_DEV_PATHS_CONFIG=%DIST_DIR%config"

"%DIST_DIR%app.exe" %*
