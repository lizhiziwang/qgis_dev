"""UI 测试统一 conftest（QGIS fixture）。

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
    """初始化 :class:`QgsApplication`。被 ``gui`` 测试需要时再调。

    Windows 上必须用 bytes argv，否则 sip 绑定抛
    ``TypeError: expected bytes, str found``（见 qgis-ui-integration-gotchas）。
    """
    try:
        from qgis.core import QgsApplication  # type: ignore
    except ImportError as exc:
        pytest.skip(f"QGIS 未安装：{exc}", allow_module_level=False)

    prefix = os.environ.get("QGIS_PREFIX")
    if not prefix:
        for candidate in (
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
    if not app.allWidgets():
        app.initQgis()
    yield app
