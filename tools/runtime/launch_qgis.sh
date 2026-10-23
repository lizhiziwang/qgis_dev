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

# 便携打包：动态库统一在 _internal/（PyInstaller 的 collected 目录；构建脚本
# 额外收集的 .so 也平铺在这里，避免根目录出现几百个库文件）。
export LD_LIBRARY_PATH="$DIST_DIR/_internal:$DIST_DIR/runtime/lib${LD_LIBRARY_PATH:+:$LD_LIBRARY_PATH}"
# Qt 插件（platforms/ imageformats/ xcbglintegrations/ tls/ ...）在 runtime 里：
# Qt6 构建为 runtime/lib/qt6/plugins，Qt5 构建为 runtime/lib/qt5/plugins，
# 老式 conda 布局为 runtime/plugins。取第一个含 platforms/ 的目录——写死 qt6
# 会让 Qt5 产物找不到 xcb 平台插件，QApplication 在登录框之前就退出。
QT_PLUGIN_ROOT=""
for _cand in "$DIST_DIR/runtime/lib/qt6/plugins" \
             "$DIST_DIR/runtime/lib/qt5/plugins" \
             "$DIST_DIR/runtime/plugins"; do
    if [ -d "$_cand/platforms" ]; then
        QT_PLUGIN_ROOT="$_cand"
        break
    fi
done
if [ -n "$QT_PLUGIN_ROOT" ]; then
    export QT_PLUGIN_PATH="$QT_PLUGIN_ROOT"
    export QT_QPA_PLATFORM_PLUGIN_PATH="$QT_PLUGIN_ROOT/platforms"
fi
# GDAL 命令行工具（gdal_translate / gdalwarp ...）：processing 的 gdal
# provider 通过 PATH 调用，包内 runtime/bin 必须排在系统前面。
export PATH="$DIST_DIR/runtime/bin:$PATH"

# QGIS 前缀：runtime/ 按 conda 布局组装（share/qgis 数据 + lib/qgis/plugins
# C++ 插件），bootstrap 据此识别为 for_system；QGIS_PREFIX_PATH 会在
# QgsApplication 初始化时覆盖 setPrefixPath()，必须指向最终目录。
export QGIS_PREFIX="$DIST_DIR/runtime"

# 打包后 __file__ 指向 _internal，config / resource 在顶层，用环境变量指回去。
export QGIS_DEV_PATHS_PROJECT_ROOT="$DIST_DIR"
export QGIS_DEV_PATHS_RESOURCE="$DIST_DIR/resource"

# 配置目录：便携 / 用户可写目录直接用包内 config；/opt 部署（root:root 755）时
# 普通用户写不了，迁移到每用户目录。首次运行只补齐缺失文件，绝不覆盖用户改动。
BUNDLE_CONFIG="$DIST_DIR/config"
USER_CONFIG_DIR="${XDG_CONFIG_HOME:-$HOME/.config}/qgis_dev/config"
if [ -d "$BUNDLE_CONFIG" ] && [ -w "$BUNDLE_CONFIG" ]; then
    export QGIS_DEV_CONFIG="$BUNDLE_CONFIG/app.yaml"
    export QGIS_DEV_PATHS_CONFIG="$BUNDLE_CONFIG"
else
    mkdir -p "$USER_CONFIG_DIR" 2>/dev/null || true
    if [ -d "$BUNDLE_CONFIG" ]; then
        for src in "$BUNDLE_CONFIG"/*; do
            [ -e "$src" ] || continue
            name="$(basename "$src")"
            [ -e "$USER_CONFIG_DIR/$name" ] || cp -a "$src" "$USER_CONFIG_DIR/" 2>/dev/null || true
        done
    fi
    export QGIS_DEV_CONFIG="$USER_CONFIG_DIR/app.yaml"
    export QGIS_DEV_PATHS_CONFIG="$USER_CONFIG_DIR"
fi

# 日志 / 临时目录：/opt 部署时包内目录可能只读，退到每用户数据目录。
USER_DATA_HOME="${XDG_DATA_HOME:-$HOME/.local/share}/qgis_dev"
LOG_DIR="$DIST_DIR/logs"
if ! mkdir -p "$LOG_DIR" 2>/dev/null || [ ! -w "$LOG_DIR" ]; then
    LOG_DIR="$USER_DATA_HOME/logs"
    mkdir -p "$LOG_DIR" 2>/dev/null || true
fi
TMP_DIR="$DIST_DIR/.tmp"
if ! mkdir -p "$TMP_DIR" 2>/dev/null || [ ! -w "$TMP_DIR" ]; then
    TMP_DIR="$USER_DATA_HOME/tmp"
    mkdir -p "$TMP_DIR" 2>/dev/null || true
fi
export QGIS_DEV_PATHS_LOG="$LOG_DIR"
export QGIS_DEV_PATHS_TMP="$TMP_DIR"
# 日志模块（app.infra.logging.config / crash）只认 QGIS_DEV_LOG_DIR，且它在
# import 期就会建目录——早于 bootstrap 把 PATHS_LOG 换算成 LOG_DIR。打包后
# __file__ 在 _internal/ 下，不导出这一项会去 /opt/qgis_app/_internal/logs 建
# 目录，deb 安装（root:root 755）的普通用户直接 PermissionError 起不来。
export QGIS_DEV_LOG_DIR="$LOG_DIR"

# 更新暂存 / 备份 / 日志统一放每用户目录（不往 /opt 写）
export QGIS_DEV_UPDATE_DIR="${QGIS_DEV_UPDATE_DIR:-$USER_DATA_HOME/updates}"
mkdir -p "$QGIS_DEV_UPDATE_DIR" 2>/dev/null || true

exec "$APP_EXE" "$@"
