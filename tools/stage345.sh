#!/usr/bin/env bash
# 补跑运行时装配（PyInstaller STAGE 2 已手动完成时用）：
#   - 复用已有 dist/app，不清理、不重跑 PyInstaller
#   - 重跑 build_linux.sh 的 STAGE 3-7：QGIS 运行时（迷你 conda 前缀）/
#     config / resource / 动态库收集 / 启动器 / build_info.json / 布局自检
#
# 用法: bash tools/stage345.sh
#
# 目录逻辑只有 build_linux.sh 一份——以前的独立副本会和主脚本漂移，
# 补跑一次就把产物变回旧布局。
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
exec env QGIS_SKIP_PYINSTALLER=1 bash "$SCRIPT_DIR/build_linux.sh" "$@"
