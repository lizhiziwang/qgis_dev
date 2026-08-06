"""启动 FastAPI 服务（独立进程）。"""

from __future__ import annotations

import _bootstrap_path  # noqa: F401  把仓库根加进 sys.path

from app.server.cli import main

if __name__ == "__main__":
    raise SystemExit(main())
