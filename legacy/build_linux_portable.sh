#!/usr/bin/env bash
# ============================================================
# Linux 便携打包脚本（不依赖系统已安装 QGIS）
# 原理：用 conda 创建独立 QGIS 环境，在其中运行 PyInstaller
# 前置：安装 miniforge: https://github.com/conda-forge/miniforge
# ============================================================
set -euo pipefail

APP_NAME="app"
PROJECT_ROOT="$(cd "$(dirname "$0")" && pwd)"
MAIN_SCRIPT="$PROJECT_ROOT/main.py"
DIST_PATH="$PROJECT_ROOT/dist/$APP_NAME"
CONDA_ENV_NAME="qgis-pkg-env"

# ================== STAGE 0: 准备 Conda 环境 ==================
echo ""
echo "STAGE 0: Creating self-contained Conda environment..."
# 用 mamba 加速（需先安装 mamba: conda install mamba -c conda-forge）
CONDA_CMD="${CONDA_EXE:-conda}"
if command -v mamba &> /dev/null; then
    CONDA_CMD="mamba"
fi

# 创建独立的 QGIS 环境（不依赖系统任何 QGIS 包）
if ! $CONDA_CMD env list | grep -q "$CONDA_ENV_NAME"; then
    $CONDA_CMD create -n "$CONDA_ENV_NAME" -c conda-forge -y \
        python=3.10 \
        qgis \
        pyinstaller \
        pyqt=5 \
        rasterio \
        fastapi \
        uvicorn \
        networkx \
        matplotlib \
        pip
fi

# 激活环境并安装 pip 依赖
eval "$($CONDA_CMD shell.bash hook)"
$CONDA_CMD activate "$CONDA_ENV_NAME"
pip install --no-deps -q 2>/dev/null || true  # 如有额外 pip 包可在此安装

# 获取 Conda 环境内 QGIS 的关键路径
QGIS_ENV_DIR="$($CONDA_CMD info --base)/envs/$CONDA_ENV_NAME"
QGIS_PYTHON_PKG="$QGIS_ENV_DIR/lib/python3.10/site-packages"
QGIS_LIB_DIR="$QGIS_ENV_DIR/lib"
QGIS_BIN_DIR="$QGIS_ENV_DIR/bin"

echo "  Conda env path: $QGIS_ENV_DIR"
echo ""

# ================== STAGE 1: 清理 ==================
echo "STAGE 1: Cleaning old builds..."
rm -rf "$PROJECT_ROOT/dist" "$PROJECT_ROOT/build" "$PROJECT_ROOT/$APP_NAME.spec"

# ================== STAGE 2: PyInstaller 打包 ==================
echo ""
echo "STAGE 2: Running PyInstaller inside conda env..."
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
    --add-binary "$QGIS_LIB_DIR/libqgis_core.so:." \
    --add-binary "$QGIS_LIB_DIR/libqgis_gui.so:." \
    --add-binary "$QGIS_LIB_DIR/libgdal.so:." \
    --add-binary "$QGIS_LIB_DIR/libgeos_c.so:." \
    --add-binary "$QGIS_LIB_DIR/libproj.so:." \
    --add-binary "$QGIS_LIB_DIR/libsqlite3.so:." \
    --add-binary "$QGIS_BIN_DIR/QgsOfflineEditing" \
    "$MAIN_SCRIPT"

# ================== STAGE 3: 补全运行时依赖 ==================
echo ""
echo "STAGE 3: Copying QGIS runtime dependencies..."
if [ ! -d "$DIST_PATH" ]; then
    echo "ERROR: PyInstaller failed!"
    exit 1
fi

# 复制 QGIS 插件
cp -r "$QGIS_ENV_DIR/share/qgis" "$DIST_PATH/qgis-resources" 2>/dev/null || true
cp -r "$QGIS_PYTHON_PKG/qgis/plugins" "$DIST_PATH/plugins" 2>/dev/null || true

# 复制 srs.db 和 proj.db
find "$QGIS_ENV_DIR" -name "srs.db" -exec cp {} "$DIST_PATH/" \; 2>/dev/null || true
find "$QGIS_ENV_DIR" -name "proj.db" -exec cp {} "$DIST_PATH/" \; 2>/dev/null || true

# 复制必备的 Qt 平台插件
mkdir -p "$DIST_PATH/platforms"
cp "$QGIS_BIN_DIR/../plugins/platforms/libqxcb.so" "$DIST_PATH/platforms/" 2>/dev/null || true

echo ""
echo "=========================================="
echo "  B U I L D   C O M P L E T E !"
echo "=========================================="
echo ""
echo "Portable app: $DIST_PATH"
echo "Run: $DIST_PATH/$APP_NAME"
echo ""

# 退出 conda 环境
$CONDA_CMD deactivate
