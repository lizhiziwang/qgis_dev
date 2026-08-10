"""测试 :class:`app.core.domain.identify.service.FeatureIdentifyService`。"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

pytest.importorskip("qgis", reason="FeatureIdentifyService 用 qgis.gui.QgsMapToolIdentify")


from app.core.domain.identify.service import FeatureIdentifyService, IdentifyResult


def _make_real_canvas():
    """构造一个真实但轻量的 :class:`QgsMapCanvas`，并把引用挂到模块级
    全局列表避免被 GC。"""
    from qgis.gui import QgsMapCanvas
    from PyQt6.QtWidgets import QWidget, QApplication

    QApplication.instance() or QApplication([])
    parent = QWidget()
    _widgets.append(parent)
    canvas = QgsMapCanvas(parent)
    canvas.resize(100, 100)
    _canvases.append(canvas)
    return canvas


_canvases: list = []
_widgets: list = []


def test_identify_with_no_results_returns_empty():
    from qgis.gui import QgsMapToolIdentify

    with patch.object(QgsMapToolIdentify, "identify", return_value=[]):
        svc = FeatureIdentifyService()
        out = svc.identify(_make_real_canvas(), 100, 200)
    assert out == []


def test_identify_with_results_returns_IdentifyResult():
    from qgis.gui import QgsMapToolIdentify

    fake_layer = MagicMock()
    fake_layer.fields.return_value = []

    fake_feature = MagicMock()
    fake_feature.id.return_value = 5
    fake_feature.geometry.return_value.asWkt.return_value = "POINT(0 0)"

    class FakeResult:
        def __init__(self, layer, feature):
            self.mLayer = layer
            self.mFeature = feature

    with patch.object(
        QgsMapToolIdentify, "identify",
        return_value=[FakeResult(fake_layer, fake_feature)],
    ):
        svc = FeatureIdentifyService()
        out = svc.identify(_make_real_canvas(), 10, 20)
    assert len(out) == 1
    assert out[0].fid == 5
    assert out[0].geometry_wkt == "POINT(0 0)"


def test_identify_result_attributes_dict():
    from qgis.gui import QgsMapToolIdentify

    fake_field = MagicMock()
    fake_field.name.return_value = "DLBM"

    fake_layer = MagicMock()
    fake_layer.fields.return_value = [fake_field]

    fake_feature = MagicMock()
    fake_feature.id.return_value = 1
    fake_feature.geometry.return_value.asWkt.return_value = "POINT(1 1)"
    fake_feature.attribute.return_value = "01"

    class FakeResult:
        def __init__(self, layer, feature):
            self.mLayer = layer
            self.mFeature = feature

    with patch.object(
        QgsMapToolIdentify, "identify",
        return_value=[FakeResult(fake_layer, fake_feature)],
    ):
        svc = FeatureIdentifyService()
        out = svc.identify(_make_real_canvas(), 0, 0)
    assert out[0].attributes == {"DLBM": "01"}
