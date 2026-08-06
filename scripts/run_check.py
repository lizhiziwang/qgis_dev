"""maxent 目录 → PostGIS CLI 入口。"""

from __future__ import annotations

import _bootstrap_path  # noqa: F401

from app.core.data.import_maxent import cli_main

if __name__ == "__main__":
    raise SystemExit(cli_main())
