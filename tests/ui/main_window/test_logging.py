"""主窗口操作日志回归测试。

用户要求：切换任务/工程模式、导入/导出图层都要记录日志，供日志面板追溯操作历史。

注意不能用 ``caplog``：``tests/conftest.py`` 的 ``_isolate_logger`` autouse fixture
每个测试前都会 ``_RootSink.reset()``，而代码第一次 ``get_logger`` 又触发
``_RootSink.configure`` **清空 root 上所有 handler**（含 caplog 挂的那个）。
这里改用「先触发 configure、再挂临时 list handler」的方式捕获消息，稳定可靠。

需要真实 QGIS（PyQt6 + QgsApplication）；CI 默认 ``-m 'not gui'`` 跳过。
"""

from __future__ import annotations

import logging
from contextlib import contextmanager

import pytest

pytest.importorskip("PyQt6")


class _ListHandler(logging.Handler):
    """把日志消息攒进列表的临时 handler。"""

    def __init__(self) -> None:
        super().__init__()
        self.records: list[str] = []

    def emit(self, record: logging.LogRecord) -> None:
        self.records.append(record.getMessage())


@contextmanager
def _capture_logs():
    """捕获运行期间写入应用日志（root logger）的消息列表。"""
    from app.infra.logging import get_logger

    # 先触发 _RootSink.configure，避免之后代码里首次 get_logger 清掉我们的 handler
    get_logger()
    root = logging.getLogger()
    handler = _ListHandler()
    handler.setLevel(logging.INFO)
    root.addHandler(handler)
    try:
        yield handler.records
    finally:
        root.removeHandler(handler)


@pytest.fixture
def win(qgis_app):
    """构造主窗口（不 show）。"""
    from app.ui.main_window.main_window import MainWindow

    w = MainWindow()
    yield w
    w.close()


@pytest.mark.gui
def test_mode_switch_logs(win, qgis_app):
    """点击「任务模式」→ 应用日志记「模式切换：任务模式」。"""
    from app.ui.main_window.title_bar import MODE_TASK

    with _capture_logs() as records:
        win.title_bar._mode_switch._buttons[MODE_TASK].click()
        qgis_app.processEvents()

    assert any("模式切换：任务模式" in m for m in records), f"缺少模式切换日志：{records}"


@pytest.mark.gui
def test_import_layer_logs(win, qgis_app):
    """导入图层（``_add_layer_to_project``）→ 记「导入图层：…（来源=…）」。"""
    from PyQt6.QtCore import QVariant
    from qgis.core import QgsFeature, QgsField, QgsGeometry, QgsVectorLayer

    layer = QgsVectorLayer("Polygon?crs=EPSG:4490", "导入测试层", "memory")
    layer.dataProvider().addAttributes([QgsField("name", QVariant.String)])
    layer.updateFields()
    feature = QgsFeature(layer.fields())
    feature.setGeometry(QgsGeometry.fromWkt("POLYGON((0 0,1 0,1 1,0 1,0 0))"))
    feature.setAttribute("name", "A")
    layer.dataProvider().addFeatures([feature])

    with _capture_logs() as records:
        win._add_layer_to_project(layer)
        qgis_app.processEvents()

    assert any("导入图层：导入测试层" in m for m in records), f"缺少导入图层日志：{records}"


@pytest.mark.gui
def test_export_layer_logs(win, qgis_app, monkeypatch):
    """导出图层（保存对话框 Accepted）→ 记「导出图层：… → …」。"""
    from PyQt6.QtCore import QVariant
    from PyQt6.QtWidgets import QDialog
    from qgis.core import QgsFeature, QgsField, QgsGeometry, QgsVectorLayer

    import app.ui.main_window.main_window as mw

    class _FakeSaveAsDialog:
        """替身：exec 直接 Accepted，fileName 返回目标路径。"""

        def __init__(self, layer) -> None:
            self._layer = layer

        def findChildren(self, _type):
            return []

        def exec(self):
            return QDialog.DialogCode.Accepted

        def fileName(self) -> str:
            return r"D:\tmp\export_test.gpkg"

    monkeypatch.setattr(mw, "QgsVectorLayerSaveAsDialog", _FakeSaveAsDialog)

    layer = QgsVectorLayer("Polygon?crs=EPSG:4490", "导出测试层", "memory")
    layer.dataProvider().addAttributes([QgsField("name", QVariant.String)])
    layer.updateFields()
    feature = QgsFeature(layer.fields())
    feature.setGeometry(QgsGeometry.fromWkt("POLYGON((0 0,1 0,1 1,0 1,0 0))"))
    feature.setAttribute("name", "A")
    layer.dataProvider().addFeatures([feature])

    with _capture_logs() as records:
        win._export_layer(layer)
        qgis_app.processEvents()

    assert any(
        "导出图层：导出测试层 →" in m and r"D:\tmp\export_test.gpkg" in m
        for m in records
    ), f"缺少导出图层日志：{records}"
