#!/usr/bin/env bash
# Linux 桌面端 launcher：与 app 可执行文件同一目录，设置 QGIS 环境变量后启动。
set -euo pipefail

DIST_DIR="$(cd "$(dirname "$0")" && pwd)/"

if [ ! -x "$DIST_DIR/app" ]; then
    echo "ERROR: $DIST_DIR/app 不存在，请把本脚本和可执行文件放在同一目录。" >&2
    exit 1
fi

export LD_LIBRARY_PATH="$DIST_DIR:${LD_LIBRARY_PATH:-}"

# 便携打包：QGIS 运行时都打进本目录。
export QGIS_PREFIX="$DIST_DIR"

# 打包后 __file__ 指向 _internal，config / resource 在顶层，用环境变量指回去。
export QGIS_DEV_CONFIG="$DIST_DIR/config/app.yaml"
export QGIS_DEV_PATHS_PROJECT_ROOT="$DIST_DIR"
export QGIS_DEV_PATHS_RESOURCE="$DIST_DIR/resource"
export QGIS_DEV_PATHS_LOG="$DIST_DIR/logs"
export QGIS_DEV_PATHS_TMP="$DIST_DIR/.tmp"
export QGIS_DEV_PATHS_CONFIG="$DIST_DIR/config"

exec "$DIST_DIR/app" "$@"
