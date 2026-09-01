#!/usr/bin/env bash
# 麒麟环境直接跑源码版（python app/main.py），QGIS 环境与打包版一致。
#
# 背景：config/app.yaml 的 qgis.install_path 是 Windows 开发机路径
# （D:/zsh/app/qgis4.2），Linux 真机直接用 python app/main.py 会因
# PROJ_DATA / GDAL_DATA / QGIS_PREFIX_PATH 无效导致 initQgis 部分失败，
# 浏览器缺少 PostgreSQL / XYZ 切片 / ArcGIS REST 等节点。
#
# 本脚本把 QGIS_PREFIX 指到 conda 环境根（标准 Linux 布局），bootstrap
# 检测到 share/qgis 布局后走 QgisRuntime.for_system()，路径全部正确。
#
# 用法: bash tools/kylin/run_dev_kylin.sh
set -e
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
cd "$HERE"

# 定位 conda 环境（支持自定义 CONDA_ENV_NAME）
CONDA_ENV_NAME="${CONDA_ENV_NAME:-qgis_new}"
source "$(conda info --base 2>/dev/null)/etc/profile.d/conda.sh" || source ~/miniconda3/etc/profile.d/conda.sh
conda activate "$CONDA_ENV_NAME"

export QGIS_PREFIX="$CONDA_PREFIX"
echo "== QGIS_PREFIX=$QGIS_PREFIX  (conda env: $CONDA_ENV_NAME) =="
exec python app/main.py "$@"
