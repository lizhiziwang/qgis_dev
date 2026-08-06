"""测试 3 个空间 check 子类。需要 QGIS → ``@pytest.mark.gui``。"""

from __future__ import annotations

import pytest

pytest.importorskip("qgis", reason="空间 check 用 qgis.core.QgsGeometry")

pytestmark = pytest.mark.gui


def _make_layer(qgis_app, wkt_list, crs="EPSG:4490"):
    from qgis.core import QgsFeature, QgsField, QgsGeometry, QgsVectorLayer
    from PyQt5.QtCore import QVariant

    layer = QgsVectorLayer(f"Polygon?crs={crs}", "t", "memory")
    pr = layer.dataProvider()
    pr.addAttributes([QgsField("n", QVariant.String)])
    layer.updateFields()
    feats = []
    for i, w in enumerate(wkt_list):
        f = QgsFeature(layer.fields())
        f.setGeometry(QgsGeometry.fromWkt(w))
        f.setAttribute("n", f"f{i}")
        feats.append(f)
    pr.addFeatures(feats)
    layer.updateExtents()
    return layer


def test_self_intersect_finds_bowtie(qgis_app):
    from app.core.domain.check.builtin.space_self_intersect import SpaceSelfIntersectCheck
    from app.core.domain.check.param import CheckParam
    from app.core.domain.check.space_index import clear_spatial_index

    # "bowtie" 自相交 polygon
    layer = _make_layer(qgis_app, [
        "POLYGON((0 0, 10 10, 10 0, 0 10, 0 0))",
        "POLYGON((20 20, 30 20, 30 30, 20 30, 20 20))",
    ])
    try:
        check = SpaceSelfIntersectCheck()
        res = check.run(CheckParam(layer=layer))
        assert 1 in res.failed_feature_ids
        assert 2 not in res.failed_feature_ids
    finally:
        clear_spatial_index(layer)


def test_overlap_finds_intersect_but_not_touch(qgis_app):
    from app.core.domain.check.builtin.space_overlap import SpaceOverlapCheck
    from app.core.domain.check.param import CheckParam
    from app.core.domain.check.space_index import clear_spatial_index

    # 两个完全重叠的 polygon
    layer = _make_layer(qgis_app, [
        "POLYGON((0 0, 10 0, 10 10, 0 10, 0 0))",
        "POLYGON((5 5, 15 5, 15 15, 5 15, 5 5))",
    ])
    try:
        check = SpaceOverlapCheck()
        res = check.run(CheckParam(layer=layer))
        assert 1 in res.failed_feature_ids
        assert 2 in res.failed_feature_ids
    finally:
        clear_spatial_index(layer)


def test_overlap_touching_polygons_are_ok(qgis_app):
    from app.core.domain.check.builtin.space_overlap import SpaceOverlapCheck
    from app.core.domain.check.param import CheckParam
    from app.core.domain.check.space_index import clear_spatial_index

    # 两个共享一条边的 polygon（touches 不算 overlap）
    layer = _make_layer(qgis_app, [
        "POLYGON((0 0, 10 0, 10 10, 0 10, 0 0))",
        "POLYGON((10 0, 20 0, 20 10, 10 10, 10 0))",
    ])
    try:
        check = SpaceOverlapCheck()
        res = check.run(CheckParam(layer=layer))
        assert res.is_pass is True
    finally:
        clear_spatial_index(layer)


def test_long_thin_detects_high_ratio(qgis_app):
    from app.core.domain.check.builtin.space_long_thin import SpaceLongThinCheck
    from app.core.domain.check.param import CheckParam

    # 1:10 长条 polygon
    layer = _make_layer(qgis_app, [
        "POLYGON((0 0, 100 0, 100 1, 0 1, 0 0))",
        "POLYGON((0 10, 10 10, 10 12, 0 12, 0 10))",
    ])
    check = SpaceLongThinCheck()
    res = check.run(CheckParam(layer=layer, extra={"ratio": 5}))
    assert 1 in res.failed_feature_ids
    assert 2 not in res.failed_feature_ids
