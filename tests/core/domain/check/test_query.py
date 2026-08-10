"""测试 :func:`app.core.domain.check.query.intersect_features`。

需要真实 QGIS 几何 → 标 ``@pytest.mark.gui``，CI 跑 ``-m 'not gui'`` 跳过。
"""

from __future__ import annotations

import pytest

pytest.importorskip("qgis", reason="intersect_features 用 qgis.core.QgsGeometry")

from app.core.domain.check.query import intersect_features
from app.core.domain.check.space_index import build_spatial_index, clear_spatial_index

pytestmark = pytest.mark.gui


def _make_polygon_layer(qgis_app, polygons, crs_authid="EPSG:4490"):
    """构造一个内存 polygon layer。"""
    from qgis.core import QgsFeature, QgsField, QgsFields, QgsGeometry, QgsVectorLayer
    from PyQt6.QtCore import QVariant

    uri = f"Polygon?crs={crs_authid}"
    layer = QgsVectorLayer(uri, "test_polys", "memory")
    pr = layer.dataProvider()
    pr.addAttributes([QgsField("name", QVariant.String)])
    layer.updateFields()
    feats = []
    for i, wkt in enumerate(polygons):
        f = QgsFeature(layer.fields())
        f.setGeometry(QgsGeometry.fromWkt(wkt))
        f.setAttribute("name", f"f{i}")
        feats.append(f)
    pr.addFeatures(feats)
    layer.updateExtents()
    return layer


def test_intersect_features_with_polygon_overlap(qgis_app):
    layer = _make_polygon_layer(qgis_app, [
        "POLYGON((0 0, 10 0, 10 10, 0 10, 0 0))",          # fid=1
        "POLYGON((5 5, 15 5, 15 15, 5 15, 5 5))",          # fid=2，跟 1 重叠
        "POLYGON((100 100, 110 100, 110 110, 100 110, 100 100))",  # fid=3
    ])
    try:
        from qgis.core import QgsCoordinateReferenceSystem, QgsGeometry

        geom = QgsGeometry.fromWkt("POLYGON((0 0, 5 0, 5 5, 0 5, 0 0))")
        crs = QgsCoordinateReferenceSystem("EPSG:4490")
        fids = intersect_features(layer, geom, source_crs=crs)
        assert 1 in fids
        assert 2 in fids
        assert 3 not in fids
    finally:
        clear_spatial_index(layer)


def test_intersect_features_empty_geom_returns_empty(qgis_app):
    layer = _make_polygon_layer(qgis_app, [
        "POLYGON((0 0, 10 0, 10 10, 0 10, 0 0))",
    ])
    from qgis.core import QgsCoordinateReferenceSystem, QgsGeometry

    try:
        fids = intersect_features(
            layer,
            QgsGeometry(),
            source_crs=QgsCoordinateReferenceSystem("EPSG:4490"),
        )
        assert fids == []
    finally:
        clear_spatial_index(layer)


def test_intersect_features_none_layer_returns_empty():
    fids = intersect_features(None, None, source_crs=None)
    assert fids == []


def test_intersect_features_crs_mismatch_transforms(qgis_app):
    """源 geom 用 EPSG:4326，layer 用 EPSG:4490，跨 CRS 转换后仍能找到相交。"""
    layer = _make_polygon_layer(
        qgis_app,
        ["POLYGON((115 38, 116 38, 116 39, 115 39, 115 38))"],  # 北京附近
        crs_authid="EPSG:4490",
    )
    try:
        from qgis.core import QgsCoordinateReferenceSystem, QgsGeometry

        # 在 WGS84 中表达大致相同区域
        geom = QgsGeometry.fromWkt("POLYGON((115.5 38.5, 115.6 38.5, 115.6 38.6, 115.5 38.6, 115.5 38.5))")
        crs_4326 = QgsCoordinateReferenceSystem("EPSG:4326")
        fids = intersect_features(layer, geom, source_crs=crs_4326)
        assert 1 in fids
    finally:
        clear_spatial_index(layer)
