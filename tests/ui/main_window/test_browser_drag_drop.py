"""数据浏览器拖拽添加图层回归测试。

浏览器视图开启拖出（``setDragEnabled``），画布侧 ``MainWindow.eventFilter``
拦截图层 URI drop，走与双击一致的 ``_add_layer_to_project`` 路径（含 ogr 的
``path_c``）。非图层 URI 的拖拽放行给原生处理。

需要真实 QGIS（PyQt6 + QgsApplication）；CI 默认 ``-m 'not gui'`` 跳过。
"""

from __future__ import annotations

import pytest

pytest.importorskip("PyQt6")

from PyQt6.QtCore import QEvent, QPointF, Qt
from PyQt6.QtGui import QDropEvent  # noqa: E402
from PyQt6.QtWidgets import QAbstractItemView, QMainWindow  # noqa: E402

from qgis.core import QgsMimeDataUtils, QgsProject, QgsVectorLayer  # noqa: E402
from qgis.gui import QgsMapCanvas  # noqa: E402

from app.ui.main_window.browser_dock import BrowserDock  # noqa: E402
from app.ui.main_window.main_window import MainWindow  # noqa: E402


@pytest.fixture(autouse=True)
def _clean_project():
    """每个用例前清空全局工程，避免图层跨用例累积。"""
    QgsProject.instance().removeAllMapLayers()
    yield


class _DropHarness(MainWindow):
    """不跑完整 ``MainWindow.__init__``（避免拉起整个窗口/配置），只装配
    画布 + 工程，就能测 ``eventFilter`` / ``_on_canvas_drop`` / ``_add_layer_to_project``。"""

    def __init__(self) -> None:
        QMainWindow.__init__(self)
        self.canvas = QgsMapCanvas(self)
        self.project = QgsProject.instance()
        self._data_group = None


def _drop_event(mime, event_type: QEvent.Type) -> QDropEvent:
    return QDropEvent(
        QPointF(10.0, 10.0),
        Qt.DropAction.CopyAction,
        mime,
        Qt.MouseButton.LeftButton,
        Qt.KeyboardModifier.NoModifier,
        event_type,
    )


def _layer_uri_mime(geojson_path: str):
    """把某个矢量文件编码成 QGIS 图层 URI 的 mime（模拟浏览器拖出的数据）。"""
    layer = QgsVectorLayer(geojson_path, "drop_test", "ogr")
    return QgsMimeDataUtils.encodeUriList([QgsMimeDataUtils.Uri(layer)])


@pytest.mark.gui
def test_browser_view_drag_enabled(qgis_app):
    """浏览器视图开启拖出：dragEnabled True、DragOnly。"""
    dock = BrowserDock()
    assert dock.view.dragEnabled() is True
    assert dock.view.dragDropMode() == QAbstractItemView.DragDropMode.DragOnly


@pytest.mark.gui
def test_drop_adds_layer_to_project(qgis_app, tmp_path):
    """drop 图层 URI → 图层进入工程，且 isValid。"""
    gj = tmp_path / "t.geojson"
    gj.write_text(
        '{"type":"FeatureCollection","features":[{"type":"Feature",'
        '"properties":{},"geometry":{"type":"Point","coordinates":[1.0,2.0]}}]}',
        encoding="utf-8",
    )
    mw = _DropHarness()
    mime = _layer_uri_mime(str(gj))

    handled = mw.eventFilter(mw.canvas, _drop_event(mime, QEvent.Type.Drop))

    assert handled is True
    assert mw.project.mapLayersByName("drop_test")
    layer = mw.project.mapLayersByName("drop_test")[0]
    assert layer.isValid()


@pytest.mark.gui
def test_drop_sets_path_c_for_ogr(qgis_app, tmp_path):
    """ogr 文件 drop → path_c 指向源文件（供"执行SQL"打开）。"""
    gj = tmp_path / "t.geojson"
    gj.write_text(
        '{"type":"FeatureCollection","features":[{"type":"Feature",'
        '"properties":{},"geometry":{"type":"Point","coordinates":[1.0,2.0]}}]}',
        encoding="utf-8",
    )
    mw = _DropHarness()
    mime = _layer_uri_mime(str(gj))

    mw.eventFilter(mw.canvas, _drop_event(mime, QEvent.Type.Drop))

    layer = mw.project.mapLayersByName("drop_test")[0]
    assert getattr(layer, "path_c", None) == str(gj)


@pytest.mark.gui
def test_drop_goes_to_data_group_when_exists(qgis_app, tmp_path):
    """已建"数据图层"分组时，drop 的图层进分组（与双击一致）。"""
    gj = tmp_path / "t.geojson"
    gj.write_text(
        '{"type":"FeatureCollection","features":[{"type":"Feature",'
        '"properties":{},"geometry":{"type":"Point","coordinates":[1.0,2.0]}}]}',
        encoding="utf-8",
    )
    mw = _DropHarness()
    mw._data_group = mw.project.layerTreeRoot().addGroup("数据图层")
    mime = _layer_uri_mime(str(gj))

    mw.eventFilter(mw.canvas, _drop_event(mime, QEvent.Type.Drop))

    group_names = [c.name() for c in mw._data_group.children()]
    assert "drop_test" in group_names


@pytest.mark.gui
def test_drag_enter_accepted_for_uri_mime(qgis_app, tmp_path):
    """DragEnter / DragMove：图层 URI 被接受（返回 True，进入可 drop 状态）。"""
    gj = tmp_path / "t.geojson"
    gj.write_text(
        '{"type":"FeatureCollection","features":[{"type":"Feature",'
        '"properties":{},"geometry":{"type":"Point","coordinates":[1.0,2.0]}}]}',
        encoding="utf-8",
    )
    mw = _DropHarness()
    mime = _layer_uri_mime(str(gj))

    assert mw.eventFilter(mw.canvas, _drop_event(mime, QEvent.Type.DragEnter)) is True
    assert mw.eventFilter(mw.canvas, _drop_event(mime, QEvent.Type.DragMove)) is True
    assert not mw.project.mapLayersByName("drop_test")  # 拖拽中不应加图层


@pytest.mark.gui
def test_non_uri_drag_not_intercepted(qgis_app):
    """非图层 URI 的拖拽（普通文本）放行给原生处理（返回 False）。"""
    from PyQt6.QtCore import QMimeData

    mw = _DropHarness()
    plain = QMimeData()
    plain.setText("hello")

    handled = mw.eventFilter(mw.canvas, _drop_event(plain, QEvent.Type.DragEnter))
    assert handled is False
