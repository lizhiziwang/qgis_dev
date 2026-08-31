"""点选 / 框选工具测试。

覆盖：
1. tool_icons 注册了 "select_point" / "select_box" 图标（防主题切换 KeyError）
2. 通用工具条包含「点选」「框选」按钮，且按顺序排在「平移」之后
3. MainWindow 点「点选」/「框选」激活对应地图工具
4. 点选：单击命中图层内要素（带 10px 容差），点空白处清空选中
5. 框选：拖拽矩形选中矩形内要素，空矩形不误选
6. 无修饰键二次选中替换上一次；按住 Ctrl 追加（多选），Ctrl 点空白不清空

需要真实 QGIS（PyQt6 + QgsApplication）；CI 默认 ``-m 'not gui'`` 跳过。
"""

from __future__ import annotations

import pytest

pytest.importorskip("PyQt6")


@pytest.fixture
def win(qgis_app):
    """构造主窗口（不 show）。"""
    from app.ui.main_window.main_window import MainWindow

    w = MainWindow()
    yield w
    w.close()


class _FakeMouseEvent:
    """最小事件桩：只暴露 button() / pixelPoint() / modifiers()，驱动选择工具。"""

    def __init__(self, pos, button=None, modifiers=None):
        from PyQt6.QtCore import Qt

        self._pos = pos
        self._button = button if button is not None else Qt.MouseButton.LeftButton
        self._modifiers = modifiers if modifiers is not None else Qt.KeyboardModifier.NoModifier

    def button(self):
        return self._button

    def modifiers(self):
        return self._modifiers

    def pixelPoint(self):
        return self._pos


def _make_polygon_layer():
    """两张互不重叠的面要素（EPSG:4326），f1 在左下、f2 在右上。

    返回 ``(layer, fid1, fid2)``；fid 从图层读取（``addFeatures`` 拷贝要素，
    原 ``QgsFeature`` 对象不会被回填 fid）。
    """
    from qgis.core import QgsFeature, QgsGeometry, QgsVectorLayer

    layer = QgsVectorLayer("Polygon?crs=EPSG:4326", "polys", "memory")
    dp = layer.dataProvider()
    f1 = QgsFeature()
    f1.setGeometry(QgsGeometry.fromWkt("POLYGON((10 10, 20 10, 20 20, 10 20, 10 10))"))
    f2 = QgsFeature()
    f2.setGeometry(QgsGeometry.fromWkt("POLYGON((100 100, 110 100, 110 110, 100 110, 100 100))"))
    ok, _errors = dp.addFeatures([f1, f2])
    assert ok
    layer.updateExtents()
    assert layer.featureCount() == 2
    # 按质心 x 区分两张面：f1 质心 x=15，f2 质心 x=105
    by_centroid_x = {
        f.geometry().centroid().asPoint().x(): f.id() for f in layer.getFeatures()
    }
    return layer, by_centroid_x[15.0], by_centroid_x[105.0]


def _make_canvas_with_layer(project, layer):
    """独立工程 + 画布，extent/CRS/尺寸就绪后返回 canvas。"""
    from PyQt6.QtWidgets import QApplication
    from qgis.gui import QgsMapCanvas

    canvas = QgsMapCanvas()
    canvas.setProject(project)
    canvas.setLayers([layer])
    canvas.setDestinationCrs(layer.crs())
    canvas.setExtent(layer.extent())
    canvas.resize(400, 300)
    app = QApplication.instance()
    assert app is not None
    app.processEvents()
    assert canvas.getCoordinateTransform() is not None
    return canvas


def _to_pixel(canvas, x, y):
    """地图坐标 → 画布像素 QPoint（供模拟点击）。"""
    from PyQt6.QtCore import QPoint
    from qgis.core import QgsPointXY

    mtp = canvas.getCoordinateTransform()
    p = mtp.transform(QgsPointXY(x, y))
    return QPoint(int(p.x()), int(p.y()))


def test_select_glyphs_registered():
    """tool_icons 必须注册 select_point / select_box（缺 key 时主题切换会 KeyError）。"""
    from app.ui.common import tool_icons

    assert "select_point" in tool_icons._GLYPHS
    assert "select_box" in tool_icons._GLYPHS


@pytest.mark.gui
def test_select_buttons_present(win):
    """通用工具条应包含「点选」「框选」，且紧跟「平移」之后。"""
    acts = [a for a in win.tool_bar.actions() if not a.isSeparator()]
    texts = [a.text() for a in acts]
    assert "点选" in texts
    assert "框选" in texts
    assert texts.index("平移") < texts.index("点选") < texts.index("框选")


@pytest.mark.gui
def test_on_select_point_activates_tool(win):
    """点「点选」→ 画布切到 PointSelectTool。"""
    from app.ui.tools.select_feature import PointSelectTool

    win._on_select_point()
    assert isinstance(win.canvas.mapTool(), PointSelectTool)


@pytest.mark.gui
def test_on_select_box_activates_tool(win):
    """点「框选」→ 画布切到 RectSelectTool。"""
    from app.ui.tools.select_feature import RectSelectTool

    win._on_select_box()
    assert isinstance(win.canvas.mapTool(), RectSelectTool)


@pytest.mark.gui
def test_point_select_selects_feature_at_click(qgis_app):
    """点选：单击 f1 内部 → 只选中 f1；点空白处 → 清空选中。"""
    from PyQt6.QtWidgets import QApplication
    from qgis.core import QgsProject

    from app.ui.tools.select_feature import PointSelectTool

    layer, fid1, _fid2 = _make_polygon_layer()
    project = QgsProject()
    project.addMapLayers([layer])
    canvas = _make_canvas_with_layer(project, layer)

    tool = PointSelectTool(canvas)
    canvas.setMapTool(tool)

    # 点 f1 中心 (15,15)
    tool.canvasReleaseEvent(
        _FakeMouseEvent(_to_pixel(canvas, 15, 15))
    )
    assert layer.selectedFeatureCount() == 1
    assert fid1 in layer.selectedFeatureIds()

    # 点空白处 (50,50)：清空选中
    tool.canvasReleaseEvent(_FakeMouseEvent(_to_pixel(canvas, 50, 50)))
    assert layer.selectedFeatureCount() == 0
    app = QApplication.instance()
    assert app is not None
    app.processEvents()
    canvas.deleteLater()


@pytest.mark.gui
def test_point_select_second_click_replaces_first(qgis_app):
    """无修饰键：第二次单击替换上一次选中，而不是叠加。"""
    from PyQt6.QtWidgets import QApplication
    from qgis.core import QgsProject

    from app.ui.tools.select_feature import PointSelectTool

    layer, fid1, fid2 = _make_polygon_layer()
    project = QgsProject()
    project.addMapLayers([layer])
    canvas = _make_canvas_with_layer(project, layer)

    tool = PointSelectTool(canvas)
    canvas.setMapTool(tool)

    tool.canvasReleaseEvent(_FakeMouseEvent(_to_pixel(canvas, 15, 15)))
    assert fid1 in layer.selectedFeatureIds()

    # 再点 f2 (105,105)：替换为只剩 f2
    tool.canvasReleaseEvent(_FakeMouseEvent(_to_pixel(canvas, 105, 105)))
    assert layer.selectedFeatureCount() == 1
    assert fid2 in layer.selectedFeatureIds()
    assert fid1 not in layer.selectedFeatureIds()
    app = QApplication.instance()
    assert app is not None
    app.processEvents()
    canvas.deleteLater()


@pytest.mark.gui
def test_point_select_ctrl_adds_to_selection(qgis_app):
    """按住 Ctrl：追加到现有选中（多选）。"""
    from PyQt6.QtCore import Qt
    from PyQt6.QtWidgets import QApplication
    from qgis.core import QgsProject

    from app.ui.tools.select_feature import PointSelectTool

    layer, fid1, fid2 = _make_polygon_layer()
    project = QgsProject()
    project.addMapLayers([layer])
    canvas = _make_canvas_with_layer(project, layer)

    tool = PointSelectTool(canvas)
    canvas.setMapTool(tool)

    tool.canvasReleaseEvent(_FakeMouseEvent(_to_pixel(canvas, 15, 15)))
    ctrl = Qt.KeyboardModifier.ControlModifier
    tool.canvasReleaseEvent(
        _FakeMouseEvent(_to_pixel(canvas, 105, 105), modifiers=ctrl)
    )
    assert layer.selectedFeatureCount() == 2
    assert fid1 in layer.selectedFeatureIds()
    assert fid2 in layer.selectedFeatureIds()
    app = QApplication.instance()
    assert app is not None
    app.processEvents()
    canvas.deleteLater()


@pytest.mark.gui
def test_point_select_ctrl_click_empty_keeps_selection(qgis_app):
    """Ctrl 点到空白处：不清空现有选中。"""
    from PyQt6.QtCore import Qt
    from PyQt6.QtWidgets import QApplication
    from qgis.core import QgsProject

    from app.ui.tools.select_feature import PointSelectTool

    layer, fid1, _fid2 = _make_polygon_layer()
    project = QgsProject()
    project.addMapLayers([layer])
    canvas = _make_canvas_with_layer(project, layer)

    tool = PointSelectTool(canvas)
    canvas.setMapTool(tool)

    tool.canvasReleaseEvent(_FakeMouseEvent(_to_pixel(canvas, 15, 15)))
    ctrl = Qt.KeyboardModifier.ControlModifier
    tool.canvasReleaseEvent(
        _FakeMouseEvent(_to_pixel(canvas, 50, 50), modifiers=ctrl)
    )
    assert fid1 in layer.selectedFeatureIds()
    app = QApplication.instance()
    assert app is not None
    app.processEvents()
    canvas.deleteLater()


@pytest.mark.gui
def test_rect_select_selects_features_in_rect(qgis_app):
    """框选：拖拽 (5,5)→(25,25) → 只选中落入矩形的 f1。"""
    from PyQt6.QtWidgets import QApplication
    from qgis.core import QgsProject

    from app.ui.tools.select_feature import RectSelectTool

    layer, fid1, _fid2 = _make_polygon_layer()
    project = QgsProject()
    project.addMapLayers([layer])
    canvas = _make_canvas_with_layer(project, layer)

    tool = RectSelectTool(canvas)
    canvas.setMapTool(tool)

    tool.canvasPressEvent(_FakeMouseEvent(_to_pixel(canvas, 5, 5)))
    tool.canvasMoveEvent(_FakeMouseEvent(_to_pixel(canvas, 15, 15)))
    tool.canvasReleaseEvent(_FakeMouseEvent(_to_pixel(canvas, 25, 25)))

    assert layer.selectedFeatureCount() == 1
    assert fid1 in layer.selectedFeatureIds()
    app = QApplication.instance()
    assert app is not None
    app.processEvents()
    canvas.deleteLater()


@pytest.mark.gui
def test_rect_select_ctrl_adds_to_selection(qgis_app):
    """框选 + Ctrl：两个矩形分别框住 f1、f2 → 同时选中。"""
    from PyQt6.QtCore import Qt
    from PyQt6.QtWidgets import QApplication
    from qgis.core import QgsProject

    from app.ui.tools.select_feature import RectSelectTool

    layer, fid1, fid2 = _make_polygon_layer()
    project = QgsProject()
    project.addMapLayers([layer])
    canvas = _make_canvas_with_layer(project, layer)

    tool = RectSelectTool(canvas)
    canvas.setMapTool(tool)

    # 第一次拖拽无修饰键：替换（选中 f1）
    tool.canvasPressEvent(_FakeMouseEvent(_to_pixel(canvas, 5, 5)))
    tool.canvasReleaseEvent(_FakeMouseEvent(_to_pixel(canvas, 25, 25)))

    # 第二次拖拽按住 Ctrl：追加（f2 并入）
    ctrl = Qt.KeyboardModifier.ControlModifier
    tool.canvasPressEvent(
        _FakeMouseEvent(_to_pixel(canvas, 95, 95), modifiers=ctrl)
    )
    tool.canvasReleaseEvent(
        _FakeMouseEvent(_to_pixel(canvas, 115, 115), modifiers=ctrl)
    )

    assert layer.selectedFeatureCount() == 2
    assert fid1 in layer.selectedFeatureIds()
    assert fid2 in layer.selectedFeatureIds()
    app = QApplication.instance()
    assert app is not None
    app.processEvents()
    canvas.deleteLater()


@pytest.mark.gui
def test_rect_select_click_only_does_not_select(qgis_app):
    """框选：只点一下不拖拽（零尺寸矩形）→ 不选中任何要素。"""
    from PyQt6.QtWidgets import QApplication
    from qgis.core import QgsProject

    from app.ui.tools.select_feature import RectSelectTool

    layer, _f1, _f2 = _make_polygon_layer()
    project = QgsProject()
    project.addMapLayers([layer])
    canvas = _make_canvas_with_layer(project, layer)

    tool = RectSelectTool(canvas)
    canvas.setMapTool(tool)

    # 在同一像素按下并释放（无拖动 → 零尺寸矩形）
    pos = _to_pixel(canvas, 15, 15)
    tool.canvasPressEvent(_FakeMouseEvent(pos))
    tool.canvasReleaseEvent(_FakeMouseEvent(pos))

    assert layer.selectedFeatureCount() == 0
    app = QApplication.instance()
    assert app is not None
    app.processEvents()
    canvas.deleteLater()
