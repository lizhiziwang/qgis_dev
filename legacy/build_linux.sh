#!/usr/bin/env bash
set -euo pipefail

echo "Starting PyQGIS application build for Linux..."

# ==================== [配置区] 根据你的 Linux 环境修改 ====================
# 通过系统命令自动检测 QGIS Python 包路径
QGIS_PYTHON_PKG=$(python3 -c "import qgis; import os; print(os.path.dirname(os.path.dirname(qgis.__file__)))" 2>/dev/null || true)
# 如果上面没找到，可手动指定（例如 /usr/lib/python3/dist-packages）
if [ -z "$QGIS_PYTHON_PKG" ]; then
    echo "ERROR: 未找到 QGIS Python 包，请确保已安装: apt install qgis python3-qgis"
    exit 1
fi

# QGIS 库路径（.so 文件所在目录）
QGIS_LIB_DIR=$(python3 -c "import qgis.core; import os; print(os.path.dirname(qgis.core.__file__))" 2>/dev/null)

# 系统库路径
SYSTEM_LIB_DIR="/usr/lib/x86_64-linux-gnu"

# 项目根目录（脚本所在目录）
PROJECT_ROOT="$(cd "$(dirname "$0")" && pwd)"
MAIN_SCRIPT="$PROJECT_ROOT/main.py"
APP_NAME="app"
DIST_PATH="$PROJECT_ROOT/dist/$APP_NAME"

# ==================== 脚本逻辑（通常无需修改） ====================

# STAGE 1: 清理旧构建
echo ""
echo "STAGE 1: Cleaning up old build files..."
rm -rf "$PROJECT_ROOT/dist" "$PROJECT_ROOT/build" "$PROJECT_ROOT/$APP_NAME.spec"

# STAGE 2: 使用 PyInstaller 打包
echo ""
echo "STAGE 2: Running PyInstaller..."
# 注意：请先安装 pyinstaller: pip3 install pyinstaller
# 建议在系统 Python 或 venv 中安装
python3 -m PyInstaller \
    --noconfirm \
    --name "$APP_NAME" \
    --onedir \
    --windowed \
    --hidden-import=rasterio._features \
    --hidden-import=osgeo \
    --hidden-import=osgeo.gdal \
    --hidden-import=osgeo.ogr \
    --hidden-import=osgeo.osr \
    --hidden-import=qgis.core \
    --hidden-import=qgis.gui \
    --hidden-import=qgis.utils \
    --hidden-import=PyQt5 \
    --hidden-import=PyQt5.QtCore \
    --hidden-import=PyQt5.QtGui \
    --hidden-import=PyQt5.QtWidgets \
    --hidden-import=PyQt5.QtNetwork \
    --hidden-import=PyQt5.QtXml \
    --hidden-import=PyQt5.QtSvg \
    --hidden-import=PyQt5.QtSql \
    --collect-all rasterio \
    --collect-all setuptools \
    --collect-all qgis \
    --add-data "$QGIS_PYTHON_PKG/qgis:qgis" \
    --add-data "$PROJECT_ROOT/resource:resource" \
    --add-binary "$QGIS_LIB_DIR/qgis_core.so:." \
    --add-binary "$QGIS_LIB_DIR/qgis_gui.so:." \
    --add-binary "$SYSTEM_LIB_DIR/libgdal.so:." \
    --add-binary "$SYSTEM_LIB_DIR/libgeos_c.so:." \
    --add-binary "$SYSTEM_LIB_DIR/libproj.so:." \
    "$MAIN_SCRIPT"

# STAGE 3: 复制 QGIS 运行环境
echo ""
echo "STAGE 3: Copying QGIS runtime environment..."

if [ ! -d "$DIST_PATH" ]; then
    echo "ERROR: PyInstaller 打包失败，未找到 $DIST_PATH"
    exit 1
fi

# 复制 QGIS 插件
QGIS_PLUGIN_DIR=$(python3 -c "import qgis; import os; print(os.path.join(os.path.dirname(os.path.dirname(qgis.__file__)), 'qgis', 'plugins'))" 2>/dev/null || true)
if [ -d "$QGIS_PLUGIN_DIR" ]; then
    echo "  - Copying QGIS plugins..."
    cp -r "$QGIS_PLUGIN_DIR" "$DIST_PATH/plugins"
fi

# 复制 QGIS i18n 翻译文件
QGIS_I18N_DIR=$(python3 -c "import qgis; import os; print(os.path.join(os.path.dirname(os.path.dirname(qgis.__file__)), 'qgis', 'i18n'))" 2>/dev/null || true)
if [ -d "$QGIS_I18N_DIR" ]; then
    echo "  - Copying i18n translations..."
    cp -r "$QGIS_I18N_DIR" "$DIST_PATH/i18n"
fi

# 复制 srs.db
SRS_DB=$(find /usr -name "srs.db" -path "*/qgis/*" 2>/dev/null | head -1 || true)
if [ -n "$SRS_DB" ]; then
    mkdir -p "$DIST_PATH/resources"
    cp "$SRS_DB" "$DIST_PATH/resources/"
fi

# 复制 proj.db
PROJ_DB=$(find /usr -name "proj.db" 2>/dev/null | head -1 || true)
if [ -n "$PROJ_DB" ]; then
    cp "$PROJ_DB" "$DIST_PATH/"
fi

echo ""
echo "================================="
echo "  B U I L D   C O M P L E T E !"
echo "================================="
echo ""
echo "Portable application is in:"
echo "  $DIST_PATH"
echo ""
echo "Run: $DIST_PATH/$APP_NAME"
echo ""
