"""最短路径测试统一 conftest（QGIS fixture）。

QGIS 测试需要真实 QGIS 包 + initQgis，说明见 core/domain/check/conftest.py；
CI 默认 ``-m 'not gui'`` 跳过这些测试。
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest


@pytest.fixture(scope="session")
def qgis_app():
    """初始化 :class:`QgsApplication`。被 ``gui`` 测试需要时再调。"""
    try:
        from qgis.core import QgsApplication  # type: ignore
    except ImportError as exc:
        pytest.skip(f"QGIS 未安装：{exc}", allow_module_level=False)

    prefix = os.environ.get("QGIS_PREFIX")
    if not prefix:
        for candidate in (
            Path("D:/zsh/app/qgis4.2/apps/qgis"),
            Path("D:/zsh/app/qgis_3.40/apps/qgis-ltr"),
            Path("C:/Program Files/QGIS 3.40/apps/qgis-ltr"),
            Path("C:/OSGeo4W64/apps/qgis-ltr"),
            Path("/usr/lib/qgis"),
        ):
            if candidate.exists():
                prefix = str(candidate)
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
    if not app.allWidgets():
        app.initQgis()
    yield app
