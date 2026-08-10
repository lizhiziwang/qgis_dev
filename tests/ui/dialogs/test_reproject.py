"""重投影对话框测试。

回归保护：
- 先开窗口后加图层（或对话框被工具树复用后工程变化）时，输入下拉必须自动
  刷新出现新图层。
- 执行后生成重投影结果图层（目标坐标系正确）并加入工程。

需要真实 QGIS（PyQt5 + QgsApplication）；CI 默认 ``-m 'not gui'`` 跳过。
"""

from __future__ import annotations

import pytest

pytest.importorskip("PyQt5")

from PyQt5.QtWidgets import QMainWindow  # noqa: E402

from app.ui.dialogs.reproject import ReprojectDialog  # noqa: E402


@pytest.fixture
def dialog(qgis_app):
    # 父窗口必须持引用：临时 QMainWindow() 的 Python wrapper 被立即回收会连带
    # 销毁作为其子控件的对话框（PyQt5 所有权模型）
    parent = QMainWindow()
    dlg = ReprojectDialog(parent, title="重投影")
    yield dlg
    dlg.close()


def _pump_until(predicate, timeout_ms: int = 5000) -> None:
    """泵事件循环直到 predicate() 为真或超时。"""
    from PyQt5.QtCore import QEventLoop, QTimer

    loop = QEventLoop()
    t = QTimer()
    t.setInterval(20)
    t.timeout.connect(lambda: loop.quit() if predicate() else None)
    t.start()
    QTimer.singleShot(timeout_ms, loop.quit)
    loop.exec()
    t.stop()


@pytest.mark.gui
def test_input_combo_refreshes_on_layer_added(dialog):
    """先开窗口后加矢量图层，输入下拉自动出现该图层。"""
    from qgis.core import QgsProject, QgsVectorLayer

    assert dialog.input_combo.count() == 0
    layer = QgsVectorLayer("Point?crs=EPSG:4326", "新点", "memory")
    QgsProject.instance().addMapLayer(layer)
    try:
        assert dialog.input_combo.count() == 1
        assert dialog.input_combo.currentData() is layer
    finally:
        QgsProject.instance().removeMapLayer(layer)


@pytest.mark.gui
def test_input_combo_drops_layer_on_removal(dialog):
    """移除图层后输入下拉自动消失。"""
    from qgis.core import QgsProject, QgsVectorLayer

    layer = QgsVectorLayer("Point?crs=EPSG:4326", "将移除", "memory")
    QgsProject.instance().addMapLayer(layer)
    assert dialog.input_combo.count() == 1
    QgsProject.instance().removeMapLayer(layer)
    assert dialog.input_combo.count() == 0


@pytest.mark.gui
def test_execute_adds_reprojected_layer(qgis_app):
    """执行重投影：结果图层加入工程且目标坐标系正确。"""
    from qgis.core import (
        QgsCoordinateReferenceSystem,
        QgsFeature,
        QgsGeometry,
        QgsProject,
        QgsVectorLayer,
    )
    from processing.core.Processing import Processing

    # 真实 app 启动时已初始化 Processing；测试环境需手动确保（否则报
    # "Processing plugin has not been loaded"）
    Processing.initialize()

    parent = QMainWindow()
    dlg = ReprojectDialog(parent, title="重投影")
    layer = QgsVectorLayer("Point?crs=EPSG:4326", "输入", "memory")
    f = QgsFeature()
    f.setGeometry(QgsGeometry.fromWkt("POINT(116.39 39.90)"))
    layer.dataProvider().addFeatures([f])
    QgsProject.instance().addMapLayer(layer)
    try:
        dlg.crs_widget.setCrs(QgsCoordinateReferenceSystem("EPSG:3857"))
        dlg._on_execute()
        _pump_until(lambda: any(
            lyr.name() == "重投影结果" for lyr in QgsProject.instance().mapLayers().values()
        ))
        results = [
            lyr for lyr in QgsProject.instance().mapLayers().values()
            if lyr.name() == "重投影结果"
        ]
        assert len(results) == 1
        assert results[0].crs().authid() == "EPSG:3857"
        assert results[0].featureCount() == 1
        assert dlg.loading.value() == 100
    finally:
        QgsProject.instance().removeMapLayers(
            [lyr.id() for lyr in QgsProject.instance().mapLayers().values()]
        )
        dlg.close()
