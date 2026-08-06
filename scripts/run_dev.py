"""桌面端开发期入口。

用法::

    python scripts/run_dev.py
    # 或
    set PYTHONPATH=D:\zsh\code\py\qgis_dev & python -m app.main
"""

from __future__ import annotations

import _bootstrap_path  # noqa: F401  把仓库根加进 sys.path

from app.bootstrap import main

if __name__ == "__main__":
    raise SystemExit(main())
