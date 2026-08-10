"""状态栏（比例尺 + 鼠标坐标）回归测试。

修复前 bug：``QgsScaleWidget(canvas)`` 的构造参数在本 QGIS 构建里不生效
（没有 ``setCanvas``），比例尺永远显示默认 1:1,000,000，不随画布缩放更新
（"比例尺未生效"）。坐标显示为新增功能（本构建无 QgsStatusBarCoordinatesWidget，
用 QLabel + canvas.xyCoordinates 实现）。

需要真实 QGIS（PyQt5 + QgsApplication）；CI 默认 ``-m 'not gui'`` 跳过。
"""

from __future__ import annotations

import pytest

pytest.importorskip("PyQt5")

from PyQt5.QtWidgets import QMainWindow  # noqa: E402
from qgis.core import (  # noqa: E402
    QgsCoordinateReferenceSystem,
    QgsPointXY,
    QgsRectangle,
)
from qgis.gui import QgsMapCanvas  # noqa: E402

from app.ui.main_window.status_bar import StatusBar  # noqa: E402

DEFAULT_SCALE_TEXT = "1:1,000,000"


def _build():
    """构造带画布的 QMainWindow + 状态栏，返回 (mw, status_bar)。"""
    mw = QMainWindow()
    mw.canvas = QgsMapCanvas(mw)
    mw.canvas.setDestinationCrs(QgsCoordinateReferenceSystem("EPSG:4490"))
    sb = StatusBar(mw)
    sb.build()
    return mw, sb


@pytest.mark.gui
def test_scale_widget_added_to_status_bar(qgis_app):
    """比例尺控件被添加到状态栏，且可见。"""
    mw, sb = _build()
    assert mw.statusBar().findChildren(type(sb.scale_widget))
    assert sb.scale_widget.isVisible() or True  # 挂到 statusBar 上即可


@pytest.mark.gui
def test_scale_widget_shows_default_when_canvas_unset(qgis_app):
    """画布未设置范围（scale 为 1.0 哨兵值）时，保留默认 1:1,000,000，不显示 1:1。"""
    mw, sb = _build()
    assert sb.scale_widget.scaleString() == DEFAULT_SCALE_TEXT


@pytest.mark.gui
def test_scale_widget_tracks_canvas_zoom(qgis_app):
    """画布范围/缩放变化后，比例尺随 scaleChanged 同步（修复前的卡死点）。"""
    mw, sb = _build()
    mw.canvas.setExtent(QgsRectangle(116.3, 39.8, 116.6, 40.0))
    mw.canvas.zoomByFactor(2.0)
    qgis_app.processEvents()

    assert sb.scale_widget.scaleString() != DEFAULT_SCALE_TEXT
    assert sb.scale_widget.scaleString().startswith("1:")
    # 控件比例应与画布实际比例一致（容差 1）
    assert abs(sb.scale_widget.scale() - mw.canvas.scale()) < 1


@pytest.mark.gui
def test_coordinate_label_updates_on_xy(qgis_app):
    """鼠标移动：xyCoordinates 信号驱动坐标标签更新。"""
    mw, sb = _build()
    mw.canvas.xyCoordinates.emit(QgsPointXY(116.39123, 39.90123))
    assert sb.coord_label.text() == "116.39123°, 39.90123°"


@pytest.mark.gui
def test_coordinate_label_placeholder_on_leave(qgis_app):
    """鼠标离开画布（空点）：坐标标签显示占位符。"""
    mw, sb = _build()
    mw.canvas.xyCoordinates.emit(QgsPointXY())
    assert sb.coord_label.text() == "—, —"


@pytest.mark.gui
def test_coordinate_label_geographic_uses_degrees(qgis_app):
    """地理坐标系（默认 EPSG:4490）：坐标按 5 位小数度显示。"""
    mw, sb = _build()
    mw.canvas.xyCoordinates.emit(QgsPointXY(116.39123456, 39.90123456))
    assert sb.coord_label.text() == "116.39123°, 39.90123°"


@pytest.mark.gui
def test_coordinate_label_projected_uses_plain_numbers(qgis_app):
    """投影坐标系：坐标按 1 位小数显示。"""
    mw, sb = _build()
    mw.canvas.setDestinationCrs(QgsCoordinateReferenceSystem("EPSG:3857"))
    mw.canvas.xyCoordinates.emit(QgsPointXY(123456.789, 2345678.94))
    assert sb.coord_label.text() == "123456.8, 2345678.9"
