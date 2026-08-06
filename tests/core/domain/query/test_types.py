"""测试 :mod:`app.core.domain.query.types` 与几何映射。"""

from __future__ import annotations

import pytest

from app.core.domain.query.types import (
    GEOMETRY_TYPE_MAP,
    GeometryKind,
    LEGACY_QUERY_TYPE,
    QueryType,
)


def test_query_type_values():
    assert QueryType.POINT.value == "point"
    assert QueryType.LINE.value == "line"
    assert QueryType.POLYGON.value == "polygon"
    assert QueryType.RECTANGLE.value == "rectangle"
    assert QueryType.CIRCLE.value == "circle"


def test_geometry_type_map_covers_all_query_types():
    for qt in QueryType:
        assert qt in GEOMETRY_TYPE_MAP


def test_geometry_type_map_returns_wkt_compatible_strings():
    assert GEOMETRY_TYPE_MAP[QueryType.POINT] == "Point"
    assert GEOMETRY_TYPE_MAP[QueryType.LINE] == "LineString"
    assert GEOMETRY_TYPE_MAP[QueryType.POLYGON] == "Polygon"
    # 矩形和圆都映射为 polygon
    assert GEOMETRY_TYPE_MAP[QueryType.RECTANGLE] == "Polygon"
    assert GEOMETRY_TYPE_MAP[QueryType.CIRCLE] == "Polygon"


def test_legacy_query_type_chinese_labels():
    assert LEGACY_QUERY_TYPE["point"] == "点查询"
    assert LEGACY_QUERY_TYPE["polygon"] == "区域查询"
    assert "buffer" in LEGACY_QUERY_TYPE


def test_geometry_kind_values():
    assert GeometryKind.POINT.value == "Point"
    assert GeometryKind.LINE.value == "LineString"
    assert GeometryKind.POLYGON.value == "Polygon"
