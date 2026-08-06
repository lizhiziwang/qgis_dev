@echo off
REM Desktop launcher: keep this file in the same directory as app.exe (dist\app\).
REM It sets the QGIS env vars and starts app.exe.
REM app.exe also auto-detects the portable bundle; this is an explicit fallback.

setlocal
set "DIST_DIR=%~dp0"

if not exist "%DIST_DIR%app.exe" (
    echo ERROR: %DIST_DIR%app.exe not found. Put this script next to app.exe.
    pause
    exit /b 1
)

REM Portable packaging: QGIS runtime (proj.db / qgis / etc / plugins / i18n / DLLs)
REM are all bundled into this directory.
set "QGIS_PREFIX=%DIST_DIR%"

REM In the frozen build __file__ points to _internal; config / resource live at the
REM top level. Point them back via env vars.
set "QGIS_DEV_CONFIG=%DIST_DIR%config\app.yaml"
set "QGIS_DEV_PATHS_PROJECT_ROOT=%DIST_DIR%"
set "QGIS_DEV_PATHS_RESOURCE=%DIST_DIR%resource"
set "QGIS_DEV_PATHS_LOG=%DIST_DIR%logs"
set "QGIS_DEV_PATHS_TMP=%DIST_DIR%.tmp"
set "QGIS_DEV_PATHS_CONFIG=%DIST_DIR%config"

"%DIST_DIR%app.exe" %*
