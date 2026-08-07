#!/usr/bin/env bash
# 统一 Linux 打包脚本
#
# 用法（在项目根目录）：
#   bash tools/build_linux.sh

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PROJECT_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
APP_NAME="app"
DIST_PATH="$PROJECT_ROOT/dist/$APP_NAME"
SPEC_FILE="$PROJECT_ROOT/tools/pyinstaller.spec"

# 探测 QGIS
QGIS_PYTHON_PKG=$(python3 -c "import qgis, os; print(os.path.dirname(os.path.dirname(qgis.__file__)))" 2>/dev/null || true)
QGIS_LIB_DIR=$(python3 -c "import qgis.core, os; print(os.path.dirname(qgis.core.__file__))" 2>/dev/null || true)
SYSTEM_LIB_DIR="/usr/lib/x86_64-linux-gnu"

if [ -z "$QGIS_PYTHON_PKG" ] || [ -z "$QGIS_LIB_DIR" ]; then
    echo "ERROR: 未找到 QGIS Python 包，请确保已安装: apt install qgis python3-qgis"
    exit 1
fi

# STAGE 1: 清理
echo "STAGE 1: Cleaning..."
rm -rf "$PROJECT_ROOT/dist" "$PROJECT_ROOT/build"

# STAGE 2: PyInstaller
echo "STAGE 2: Running PyInstaller..."
python3 -m PyInstaller --noconfirm "$SPEC_FILE"

# STAGE 3: 复制 QGIS runtime
echo "STAGE 3: Copying QGIS runtime..."
if [ ! -d "$DIST_PATH" ]; then
    echo "ERROR: PyInstaller 失败，未找到 $DIST_PATH"
    exit 1
fi

QGIS_PLUGIN_DIR="$QGIS_PYTHON_PKG/qgis/plugins"
[ -d "$QGIS_PLUGIN_DIR" ] && cp -r "$QGIS_PLUGIN_DIR" "$DIST_PATH/plugins"

# Python 插件 processing：C++ plugins 目录里没有它（Ubuntu 等安装在
# /usr/share/qgis/python/plugins/processing）。缺了它 `processing.run` 是 stub，
# 瓦片/缓冲工具会报 "Processing plugin has not been loaded"。
mkdir -p "$DIST_PATH/plugins"
PROCESSING_PLUGIN=$(find /usr /opt -type d -path "*/python/plugins/processing" 2>/dev/null | head -1 || true)
if [ -n "$PROCESSING_PLUGIN" ] && [ -d "$PROCESSING_PLUGIN" ]; then
    cp -r "$PROCESSING_PLUGIN" "$DIST_PATH/plugins/processing"
else
    echo "WARNING: 未找到 processing Python 插件，打包后的瓦片/缓冲工具将不可用"
fi

QGIS_I18N_DIR="$QGIS_PYTHON_PKG/qgis/i18n"
[ -d "$QGIS_I18N_DIR" ] && cp -r "$QGIS_I18N_DIR" "$DIST_PATH/i18n"

# srs.db / proj.db
SRS_DB=$(find /usr -name "srs.db" -path "*/qgis/*" 2>/dev/null | head -1 || true)
[ -n "$SRS_DB" ] && cp "$SRS_DB" "$DIST_PATH/resources/srs.db" 2>/dev/null || true

PROJ_DB=$(find /usr -name "proj.db" 2>/dev/null | head -1 || true)
[ -n "$PROJ_DB" ] && cp "$PROJ_DB" "$DIST_PATH/proj.db" 2>/dev/null || true

# 把启动器复制进产物，和目标机器一起分发
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
cp "$SCRIPT_DIR/runtime/launch_qgis.sh" "$DIST_PATH/launch_qgis.sh" 2>/dev/null || true

echo ""
echo "================================="
echo "  B U I L D   C O M P L E T E ! "
echo "================================="
echo "Portable app: $DIST_PATH"
echo "Run: $DIST_PATH/$APP_NAME"
