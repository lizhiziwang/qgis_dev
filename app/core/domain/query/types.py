"""查询类型枚举，集中几何映射。

旧 :class:`component.SpaceQuery` 把 5 种几何类型映射硬编码了两次（菜单 +
``tool_classes`` 字典）。新代码集中到本模块，UI 层只 import :class:`QueryType`
枚举，不自己造 dict。
"""

from __future__ import annotations

from enum import Enum


class QueryType(str, Enum):
    """空间查询的几何类型。"""

    POINT = "point"
    LINE = "line"
    POLYGON = "polygon"
    RECTANGLE = "rectangle"
    CIRCLE = "circle"
    ALONG_LINE = "along_the_line"
    BUFFER = "buffer"


class GeometryKind(str, Enum):
    """OGC 简单要素几何类型（用于 SQL 转换与渲染）。"""

    POINT = "Point"
    LINE = "LineString"
    POLYGON = "Polygon"


# 单点映射表（消除硬编码两次的痛点）
GEOMETRY_TYPE_MAP: dict[QueryType, str] = {
    QueryType.POINT: GeometryKind.POINT.value,
    QueryType.LINE: GeometryKind.LINE.value,
    QueryType.POLYGON: GeometryKind.POLYGON.value,
    QueryType.RECTANGLE: GeometryKind.POLYGON.value,
    QueryType.CIRCLE: GeometryKind.POLYGON.value,
    # 沿线查询：默认按线要素对待
    QueryType.ALONG_LINE: GeometryKind.LINE.value,
    # 缓冲区查询：保留 POINT 兼容老菜单
    QueryType.BUFFER: GeometryKind.POINT.value,
}


# 旧的 query_type 字典：保留只读兼容 alias
LEGACY_QUERY_TYPE: dict[str, str] = {
    "point": "点查询",
    "line": "线查询",
    "polygon": "区域查询",
    "circle": "圆形查询",
    "along_the_line": "沿线查询",
    "buffer": "缓冲区查询",
    "rectangle": "框线查询",
}
