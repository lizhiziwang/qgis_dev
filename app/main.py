"""桌面端入口。

用法::

    # 推荐
    python -m app.main
    # 或
    python scripts/run_dev.py
    # 或直接跑（也支持）
    python app/main.py
"""

from __future__ import annotations

import os
import sys

# 把项目根加进 sys.path（允许直接 ``python app/main.py`` 运行）
_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_HERE)
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from app.bootstrap import main  # noqa: E402

if __name__ == "__main__":
    raise SystemExit(main())
