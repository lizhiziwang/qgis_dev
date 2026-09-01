#!/usr/bin/env bash
# Linux 桌面端 launcher：与 app 可执行文件同一目录，设置 QGIS 环境后启动。
# 兼容：便携包（QGIS 运行时全部在本目录）和 目标机已装 QGIS 两种部署。
set -euo pipefail

DIST_DIR="$(cd "$(dirname "$0")" && pwd)"
APP_EXE="$DIST_DIR/qgis_dev"

if [ ! -x "$APP_EXE" ]; then
    echo "ERROR: $DIST_DIR/qgis_dev 不存在，请把本脚本和可执行文件放在同一目录。" >&2
    exit 1
fi

# 便携打包：动态库在 lib/，QGIS 运行时在根下。
export LD_LIBRARY_PATH="$DIST_DIR/lib:$DIST_DIR:${LD_LIBRARY_PATH:-}"
# Qt 插件（platforms/ imageformats/ 等在根下）优先从包内找
export QT_PLUGIN_PATH="$DIST_DIR"

# 便携打包：QGIS 前缀指到本目录（bootstrap 探测到非 apps/qgis 布局 → for_portable）
export QGIS_PREFIX="$DIST_DIR"

# 打包后 __file__ 指向 _internal，config / resource 在顶层，用环境变量指回去。
export QGIS_DEV_CONFIG="$DIST_DIR/config/app.yaml"
export QGIS_DEV_PATHS_PROJECT_ROOT="$DIST_DIR"
export QGIS_DEV_PATHS_RESOURCE="$DIST_DIR/resource"
export QGIS_DEV_PATHS_LOG="$DIST_DIR/logs"
export QGIS_DEV_PATHS_TMP="$DIST_DIR/.tmp"
export QGIS_DEV_PATHS_CONFIG="$DIST_DIR/config"

# /opt 部署时 logs/.tmp 可能只读，确保可写
mkdir -p "$QGIS_DEV_PATHS_LOG" "$QGIS_DEV_PATHS_TMP" 2>/dev/null || true
[ -w "$QGIS_DEV_PATHS_LOG" ] || export QGIS_DEV_PATHS_LOG="$HOME/.local/share/qgis_dev/logs"
[ -w "$QGIS_DEV_PATHS_TMP" ] || export QGIS_DEV_PATHS_TMP="$HOME/.local/share/qgis_dev/tmp"
mkdir -p "$QGIS_DEV_PATHS_LOG" "$QGIS_DEV_PATHS_TMP" 2>/dev/null || true

exec "$APP_EXE" "$@"
