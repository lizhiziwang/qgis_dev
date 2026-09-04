#!/usr/bin/env bash
# 在 qgis-builder 容器内执行完整构建：conda 环境 → 便携包 → .deb
# 用法：见同目录 Dockerfile 顶部注释
set -euo pipefail

source /opt/conda/etc/profile.d/conda.sh

ENV_NAME="${QGIS_BUILD_ENV:-qgis_build}"
echo "== 创建 conda 环境 $ENV_NAME（conda-forge，首次约 10-20 分钟）=="

# 关键：GIS/服务依赖全部走 conda-forge，避免 pip 覆盖 conda 的 osgeo（ABI 冲突）；
# 之后 pip install -e . --no-deps 只装项目本身。
if ! conda env list | grep -q "/$ENV_NAME$"; then
    conda create -n "$ENV_NAME" -c conda-forge \
        python=3.10 \
        qgis pyqt pyinstaller \
        gdal rasterio networkx scipy \
        fastapi uvicorn pydantic pyyaml psycopg2 \
        -y
fi
conda activate "$ENV_NAME"
python --version
python -c "import qgis; print('qgis', qgis.__version__)"

echo "== pip install -e . --no-deps =="
pip install -e . --no-deps --no-build-isolation -q

echo "== 构建便携包 =="
bash tools/build_linux.sh

echo "== 打 .deb =="
bash tools/kylin/build_deb.sh

echo ""
echo "=============================================="
echo "  D O C K E R   B U I L D   C O M P L E T E !"
echo "=============================================="
echo "容器内产物：/workspace/dist/"
echo "宿主取回：  cp dist/qgis-dev-app_*.deb ."
