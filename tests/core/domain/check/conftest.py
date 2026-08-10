"""所有 check 测试的统一 conftest。

QGIS 测试需要：

1. 真实 QGIS Python 包（``qgis.core`` 等）已装
2. :class:`qgis.core.QgsApplication` 已 initQgis
3. PROJ 路径已设置（``PROJ_LIB`` 等环境变量）

为了在 CI / 无 QGIS 环境也能跑一部分纯 Python 测试，本 conftest 提供
``qgis_app`` fixture —— 仅在被 ``@pytest.mark.gui`` 标记的测试需要时才
initQgis。CI 默认 ``-m 'not gui'`` 跳过这些。
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[2]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))


import pytest  # noqa: E402


@pytest.fixture(scope="session")
def qgis_app():
    """初始化 :class:`QgsApplication`。被 ``gui`` 测试需要时再调。"""
    try:
        from qgis.core import QgsApplication  # type: ignore
    except ImportError as e:
        pytest.skip(f"QGIS 未安装：{e}", allow_module_level=False)

    # 尝试从本机默认路径探测 prefix
    prefix = os.environ.get("QGIS_PREFIX")
    if not prefix:
        candidates = [
            Path("D:/zsh/app/qgis4.2/apps/qgis"),
            Path("D:/zsh/app/qgis_3.40/apps/qgis-ltr"),
            Path("C:/Program Files/QGIS 3.40/apps/qgis-ltr"),
            Path("C:/OSGeo4W64/apps/qgis-ltr"),
            Path("/usr/lib/qgis"),
        ]
        for c in candidates:
            if c.exists():
                prefix = str(c)
                break
    if not prefix:
        pytest.skip("未设置 QGIS_PREFIX，也未探测到默认安装路径", allow_module_level=False)

    app = QgsApplication.instance() or QgsApplication(
        [bytes(a, sys.getfilesystemencoding()) for a in sys.argv], True
    )
    app.setPrefixPath(prefix, True)
    # QGIS 4.2 不再把 python/plugins 自动加进 sys.path；processing 等
    # Python 插件需要显式加入（与 app.env.qgis_runtime.apply_env 一致）
    _plugins = Path(prefix) / "python" / "plugins"
    if str(_plugins) not in sys.path:
        sys.path.insert(0, str(_plugins))
    if not app.allWidgets():   # 已被 init 过则不再 init
        app.initQgis()
    yield app
