"""项目根 conftest：把项目根加进 sys.path 第一位。

避免被父目录里同名 ``app`` 干扰（同一开发机上有多个项目）。
"""

from __future__ import annotations

import sys
from pathlib import Path

_PROJECT_ROOT = Path(__file__).resolve().parent
_PROJECT_ROOT_STR = str(_PROJECT_ROOT)
_PARENT_WITH_OTHER_APP = "D:/zsh/code/py"

# 移除父目录（如果存在）
if _PARENT_WITH_OTHER_APP in sys.path:
    sys.path.remove(_PARENT_WITH_OTHER_APP)
# 移除已有项
if _PROJECT_ROOT_STR in sys.path:
    sys.path.remove(_PROJECT_ROOT_STR)
sys.path.insert(0, _PROJECT_ROOT_STR)
