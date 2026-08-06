"""测试 :func:`app.core.domain.check.space_index.build_spatial_index`。

需要真实 QGIS → ``@pytest.mark.gui``。
"""

from __future__ import annotations

import pytest

pytest.importorskip("qgis", reason="build_spatial_index 用 qgis.core")

from app.core.domain.check.space_index import build_spatial_index, clear_spatial_index

pytestmark = pytest.mark.gui


def test_build_index_caches_on_layer(qgis_app):
    from qgis.core import QgsFeature, QgsField, QgsGeometry, QgsVectorLayer
    from PyQt5.QtCore import QVariant

    layer = QgsVectorLayer("Polygon?crs=EPSG:4490", "t", "memory")
    pr = layer.dataProvider()
    pr.addAttributes([QgsField("n", QVariant.String)])
    layer.updateFields()
    f = QgsFeature(layer.fields())
    f.setGeometry(QgsGeometry.fromWkt("POLYGON((0 0, 1 0, 1 1, 0 1, 0 0))"))
    pr.addFeatures([f])
    layer.updateExtents()

    idx1 = build_spatial_index(layer)
    idx2 = build_spatial_index(layer)
    assert idx1 is not None
    assert idx1 is idx2    # 缓存命中

    clear_spatial_index(layer)
    idx3 = build_spatial_index(layer)
    assert idx3 is not idx1


def test_build_index_invalid_layer_returns_none():
    assert build_spatial_index(None) is None
