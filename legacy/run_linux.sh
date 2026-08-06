#!/usr/bin/env bash
# 直接运行模式（不打包，依赖系统已安装的 qgis）
# 前置条件：sudo apt install qgis python3-qgis python3-pip
#            pip3 install pyqt5 rasterio fastapi uvicorn networkx matplotlib
set -euo pipefail

APP_DIR="$(cd "$(dirname "$0")" && pwd)"

# 将项目根目录添加到 Python 路径
export PYTHONPATH="$APP_DIR:$PYTHONPATH"

# 直接运行
exec python3 "$APP_DIR/main.py"
