#!/usr/bin/env bash
# 启动 FastAPI 服务（独立进程）
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PROJECT_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"

export PYTHONPATH="$PROJECT_ROOT:$PYTHONPATH"
exec python3 -m app.server "$@"
