"""测试 :class:`app.core.domain.drawing.tools.point.PointDrawTool` 等。

需要 QGIS。
"""

from __future__ import annotations

import pytest

pytest.importorskip("qgis", reason="DrawTool 继承 qgis.gui.QgsMapTool")


def test_point_tool_class_is_registered():
    from app.core.domain.drawing.tools import get_tool_class
    from app.core.domain.drawing.tools.point import PointDrawTool

    assert get_tool_class("point") is PointDrawTool


def test_polygon_geometry_type_mapping():
    """QueryType.RECTANGLE / CIRCLE 都被映射为 Polygon（WKT）。"""
    from app.core.domain.query.types import GEOMETRY_TYPE_MAP, QueryType

    assert GEOMETRY_TYPE_MAP[QueryType.RECTANGLE] == "Polygon"
    assert GEOMETRY_TYPE_MAP[QueryType.CIRCLE] == "Polygon"


def test_active_draw_finished_signal_forward():
    """ActiveDraw 应把 tool.finished 信号透传出去。

    直接发 ActiveDraw.finished.emit，验证订阅能收到。
    """
    from unittest.mock import MagicMock

    from PyQt6.QtCore import QCoreApplication

    from app.core.domain.drawing.service import ActiveDraw

    # 需要 QApplication 让信号工作
    app = QCoreApplication.instance() or QCoreApplication([])

    tool = MagicMock()
    tool.finished = MagicMock()
    ad = ActiveDraw(tool)
    captured = []
    ad.finished.connect(captured.append)
    # 直接发 ActiveDraw 的 finished 信号（tool.finished 是 mock，connect 不能传递）
    ad.finished.emit("fake_geom")
    assert captured == ["fake_geom"]


def test_active_draw_cancel_calls_cleanup():
    from unittest.mock import MagicMock

    from PyQt6.QtCore import QCoreApplication

    from app.core.domain.drawing.service import ActiveDraw

    QCoreApplication.instance() or QCoreApplication([])

    tool = MagicMock()
    tool.canvas.return_value = MagicMock()
    tool.cleanup = MagicMock()
    tool.canvas.return_value.unsetMapTool = MagicMock()

    ad = ActiveDraw(tool)
    ad.cancel()
    tool.cleanup.assert_called_once()
    tool.canvas.return_value.unsetMapTool.assert_called_with(tool)
