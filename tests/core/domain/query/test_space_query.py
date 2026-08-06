"""测试 :class:`app.core.domain.query.space_query.SpaceQueryService`。"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

pytest.importorskip("qgis", reason="SpaceQueryService 用 qgis.core.QgsGeometry")


from app.core.domain.query.space_query import SpaceQueryService


def test_query_with_none_layer_returns_empty():
    svc = SpaceQueryService()
    assert svc.query_by_geometry(None, MagicMock()) == []


def test_query_with_none_geometry_returns_empty():
    svc = SpaceQueryService()
    layer = MagicMock()
    layer.crs.return_value = "EPSG:4490"
    assert svc.query_by_geometry(layer, None) == []


def test_query_delegates_to_intersect_features():
    svc = SpaceQueryService()
    layer = MagicMock()
    layer.crs.return_value = "EPSG:4490"
    layer.isValid.return_value = True
    geom = MagicMock()
    crs = "EPSG:4326"
    expected = [1, 2, 3]

    # patch 的是 service 实际调用的引用（``from ... import`` 后是独立 binding）
    with patch("app.core.domain.query.space_query.intersect_features", return_value=expected):
        result = svc.query_by_geometry(layer, geom, source_crs=crs)
    assert result == expected


def test_query_uses_layer_crs_when_source_crs_none():
    svc = SpaceQueryService()
    layer = MagicMock()
    layer.crs.return_value = "EPSG:4490"
    layer.isValid.return_value = True
    geom = MagicMock()

    captured: dict = {}

    def fake_intersect(lyr, g, crs, project=None):
        captured["crs"] = crs
        return [42]

    with patch("app.core.domain.query.space_query.intersect_features", side_effect=fake_intersect):
        svc.query_by_geometry(layer, geom)
    assert captured["crs"] == "EPSG:4490"
