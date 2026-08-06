"""pytest 共享 fixture。

不启动 QGIS（需要 GUI 的测试自己加 ``@pytest.mark.gui`` 并依赖 QGIS）。
"""

from __future__ import annotations

import sys
from pathlib import Path

# 把项目根加进 sys.path，使 ``app.*`` 始终可导入；并移除父目录的同名 app 干扰
_ROOT = Path(__file__).resolve().parents[1]
_PARENT_WITH_OTHER_APP = "D:/zsh/code/py"
_ROOT_STR = str(_ROOT)
if _PARENT_WITH_OTHER_APP in sys.path:
    sys.path.remove(_PARENT_WITH_OTHER_APP)
if _ROOT_STR in sys.path:
    sys.path.remove(_ROOT_STR)
sys.path.insert(0, _ROOT_STR)


import pytest  # noqa: E402


@pytest.fixture(autouse=True)
def _isolate_logger():
    """每个测试都重置 root logger，避免上一次测试残留 handler 污染。

    若无 QGIS（server / core/data tests），直接跳过。
    """
    try:
        from app.infra.logging.sink import _RootSink

        _RootSink.reset()
    except Exception:  # noqa: BLE001
        pass
    yield
    try:
        from app.infra.logging.sink import _RootSink

        _RootSink.reset()
    except Exception:  # noqa: BLE001
        pass


@pytest.fixture
def tmp_log_dir(tmp_path: Path) -> Path:
    """临时日志目录。"""
    p = tmp_path / "logs"
    p.mkdir()
    return p
