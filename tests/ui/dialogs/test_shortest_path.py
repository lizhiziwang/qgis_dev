"""最短路径对话框测试。

回归保护：
- 先开窗口后加图层（或对话框被工具树复用后工程变化）时，网络下拉必须自动
  刷新出现新线图层；非线图层不能进入下拉。
- 起点/终点选定（画布取点/手输/清除）后，画布上对应标记显示/隐藏。

需要真实 QGIS（PyQt5 + QgsApplication）；CI 默认 ``-m 'not gui'`` 跳过。
"""

from __future__ import annotations

import pytest

pytest.importorskip("PyQt6")

from PyQt6.QtWidgets import QMainWindow  # noqa: E402

from app.ui.dialogs.shortest_path import ShortestPathDialog  # noqa: E402


@pytest.fixture
def dialog(qgis_app):
    # 父窗口必须持引用：临时 QMainWindow() 的 Python wrapper 被立即回收会连带
    # 销毁作为其子控件的对话框（PyQt5 所有权模型）
    parent = QMainWindow()
    dlg = ShortestPathDialog(parent, title="最短路径分析")
    yield dlg
    dlg.close()


@pytest.mark.gui
def test_network_combo_refreshes_on_layer_added(dialog):
    """先开窗口后加线图层，网络下拉自动出现该图层。"""
    from qgis.core import QgsFeature, QgsGeometry, QgsProject, QgsVectorLayer

    assert dialog.network_combo.count() == 0
    layer = QgsVectorLayer("LineString?crs=EPSG:4326", "新路线", "memory")
    feature = QgsFeature()
    feature.setGeometry(QgsGeometry.fromWkt("LINESTRING(0 0, 1 1)"))
    layer.dataProvider().addFeatures([feature])
    QgsProject.instance().addMapLayer(layer)
    try:
        assert dialog.network_combo.count() == 1
        assert dialog.network_combo.currentData() is layer
    finally:
        QgsProject.instance().removeMapLayer(layer)


@pytest.mark.gui
def test_network_combo_drops_layer_on_removal(dialog):
    """移除线图层后网络下拉自动消失。"""
    from qgis.core import QgsFeature, QgsGeometry, QgsProject, QgsVectorLayer

    layer = QgsVectorLayer("LineString?crs=EPSG:4326", "将移除", "memory")
    feature = QgsFeature()
    feature.setGeometry(QgsGeometry.fromWkt("LINESTRING(0 0, 1 1)"))
    layer.dataProvider().addFeatures([feature])
    QgsProject.instance().addMapLayer(layer)
    assert dialog.network_combo.count() == 1
    QgsProject.instance().removeMapLayer(layer)
    assert dialog.network_combo.count() == 0


@pytest.mark.gui
def test_network_combo_excludes_polygon_layers(dialog):
    """非线图层（面）不进入网络下拉。"""
    from qgis.core import QgsProject, QgsVectorLayer

    layer = QgsVectorLayer("Polygon?crs=EPSG:4326", "地块", "memory")
    QgsProject.instance().addMapLayer(layer)
    try:
        assert dialog.network_combo.count() == 0
    finally:
        QgsProject.instance().removeMapLayer(layer)


@pytest.mark.gui
def test_point_markers_show_and_hide():
    """起点/终点输入变化时画布标记显示/隐藏；关闭对话框隐藏、重开恢复。"""
    from PyQt6.QtWidgets import QMainWindow
    from qgis.gui import QgsMapCanvas

    parent = QMainWindow()
    canvas = QgsMapCanvas()
    parent.canvas = canvas  # 模拟主窗口暴露 canvas
    parent.setCentralWidget(canvas)
    dlg = ShortestPathDialog(parent, title="最短路径分析")
    dlg.show()
    try:
        # showEvent 时起点标记已惰性创建（画布可用）但文本为空 → 隐藏
        assert dlg._start_marker is not None
        assert not dlg._start_marker.isVisible()
        # 手输起点 → 标记创建并显示
        dlg.start_edit.setText("116.39, 39.90")
        assert dlg._start_marker is not None
        assert dlg._start_marker.isVisible()
        dlg.end_edit.setText("116.41, 39.92")
        assert dlg._end_marker is not None
        assert dlg._end_marker.isVisible()
        # 非法文本 / 清除 → 标记隐藏
        dlg.start_edit.setText("abc")
        assert not dlg._start_marker.isVisible()
        dlg.start_edit.setText("116.39, 39.90")
        assert dlg._start_marker.isVisible()
        # 关闭对话框 → 标记隐藏（对象保留，复用场景重开再显示）
        dlg.close()
        assert not dlg._start_marker.isVisible()
        dlg.show()
        assert dlg._start_marker.isVisible()
    finally:
        dlg.close()
