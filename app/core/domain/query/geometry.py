"""几何工具集合。

通用几何类型判断、字段名转义等。"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from qgis.core import QgsGeometry


def geometry_kind(geom: "QgsGeometry") -> str:
    """返回 ``"Point"`` / ``"LineString"`` / ``"Polygon"`` 之一。"""
    from qgis.core import QgsWkbTypes

    t = geom.type()
    return {
        0: "Point",
        1: "LineString",
        2: "Polygon",
    }.get(t, "Unknown")


def is_empty_or_none(geom: "QgsGeometry | None") -> bool:
    if geom is None:
        return True
    return bool(geom.isEmpty())


__all__ = ["geometry_kind", "is_empty_or_none"]
