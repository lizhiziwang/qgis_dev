#!/usr/bin/env bash
# 麒麟环境直接跑源码版（python app/main.py），QGIS 环境与打包版一致。
#
# 背景：config/app.yaml 的 qgis.install_path 默认是 Windows 开发机路径
# （D:/zsh/app/qgis4.2），Linux 真机直接跑会因路径无效导致 initQgis
# 部分失败、浏览器缺少 PostgreSQL / XYZ 切片 / ArcGIS REST 等节点。
# 现在 bootstrap 已支持自动探测（检测到 install_path 不存在时自动找
# conda/系统 QGIS），**不 export 也能跑**；本脚本再显式把 QGIS_PREFIX
# 指到 conda 环境根，省去每次启动的探测开销，结果一致且更稳。
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
