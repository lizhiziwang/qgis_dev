"""查询领域。"""

from __future__ import annotations

from .space_query import SpaceQueryService
from .types import GEOMETRY_TYPE_MAP, GeometryKind, QueryType

__all__ = [
    "GEOMETRY_TYPE_MAP",
    "GeometryKind",
    "QueryType",
    "SpaceQueryService",
]
