#!/usr/bin/env bash
# 在 qgis-builder 容器内执行完整构建：conda 环境 → 便携包 → .deb
# 用法：见同目录 Dockerfile 顶部注释
set -euo pipefail

source /opt/conda/etc/profile.d/conda.sh

ENV_NAME="${QGIS_BUILD_ENV:-qgis_build}"
echo "== 创建 conda 环境 $ENV_NAME（conda-forge，首次约 10-20 分钟）=="

# 关键：GIS/服务依赖全部走 conda-forge，避免 pip 覆盖 conda 的 osgeo（ABI 冲突）；
# 之后 pip install -e . --no-deps 只装项目本身。
# qgis 钉在 3.44：与已发布的麒麟运行时保持一致（runtimeId=qgis3.44）。
# rasterio/networkx/scipy 目前 app 代码未 import，且 conda-forge 的 rasterio
# 旧版会与 qgis 3.44 的 libgdal/geos 冲突，构建环境先不装（pyproject 声明保留）。
# xlwt/xlrd/openpyxl 是 spec 的 hiddenimports，缺了打包不报错但 Excel 功能运行期崩；
# cython/setuptools 供 STAGE 1.6 原生编译保护使用。
if ! conda env list | grep -q "/$ENV_NAME$"; then
    conda create -n "$ENV_NAME" -c conda-forge \
        python=3.10 \
        qgis=3.44 pyqt pyinstaller \
        gdal \
        fastapi uvicorn pydantic pyyaml psycopg2 \
        cython setuptools wheel pip \
        xlwt xlrd openpyxl \
        -y
fi
# conda-forge 的 activate.d 脚本（libpdal-core）在 set -u 下会因未定义变量中断。
set +u
conda activate "$ENV_NAME"
set -u
python --version
python -c "import qgis.core as qc; print('qgis', qc.Qgis.QGIS_VERSION)"

# local_db_util 顶层 import pysqlite3；conda-forge 没有对应包，走 pip。
if ! python -c "import pysqlite3" >/dev/null 2>&1; then
    pip install pysqlite3-binary
fi
python -c "import pysqlite3; print('pysqlite3 ok')"

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
