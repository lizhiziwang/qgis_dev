"""测试 :class:`app.core.domain.drawing.service.DrawingService` 与 :class:`DrawManager`。

需要 QGIS 几何类型做实例化。
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

pytest.importorskip("qgis", reason="DrawingService 继承 QObject 并用 qgis.gui")


from app.core.domain.drawing.manager import DrawManager
from app.core.domain.drawing.service import DrawRequest, DrawingService
from app.core.domain.drawing.tools import (
    TOOL_REGISTRY,
    get_tool_class,
)
from app.core.domain.drawing.tools import base as tools_base


def test_supported_types_includes_all_five():
    svc = DrawingService()
    types = set(svc.supported_types())
    assert {"point", "line", "polygon", "rectangle", "circle"} <= types


def test_get_tool_class_known():
    cls = get_tool_class("point")
    assert cls.__name__ == "PointDrawTool"


def test_get_tool_class_unknown_raises():
    with pytest.raises(KeyError):
        get_tool_class("unknown")


def test_tool_registry_has_five_tools():
    assert set(TOOL_REGISTRY) == {"point", "line", "polygon", "rectangle", "circle"}


def test_draw_manager_initializes_without_active():
    canvas = MagicMock()
    mgr = DrawManager(canvas)
    assert mgr._active_tool is None
    assert mgr._temp_points == []
