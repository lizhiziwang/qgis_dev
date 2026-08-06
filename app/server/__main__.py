"""``python -m app.server`` 入口。

把 :mod:`app.server.cli` 暴露为 package 的 ``__main__``。
"""

from __future__ import annotations

import sys

from app.server.cli import main

if __name__ == "__main__":
    sys.exit(main())
