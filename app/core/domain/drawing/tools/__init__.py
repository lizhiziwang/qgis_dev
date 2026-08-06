"""工具子模块的 ``__init__``。

显式 ``import *`` 让所有 5 个工具类被发现；本模块还导出 :data:`TOOL_REGISTRY`
与 :func:`get_tool_class`，消除 ``base.py`` 的循环 import。
"""

from __future__ import annotations

from .base import BaseDrawTool
from .circle import CircleDrawTool
from .line import LineDrawTool
from .point import PointDrawTool
from .polygon import PolygonDrawTool
from .rectangle import RectangleDrawTool

TOOL_REGISTRY: dict[str, type[BaseDrawTool]] = {
    "point": PointDrawTool,
    "line": LineDrawTool,
    "polygon": PolygonDrawTool,
    "rectangle": RectangleDrawTool,
    "circle": CircleDrawTool,
}


def get_tool_class(name: str) -> type[BaseDrawTool]:
    """按名字拿工具类。"""
    if name not in TOOL_REGISTRY:
        raise KeyError(f"unknown draw tool: {name!r}")
    return TOOL_REGISTRY[name]


__all__ = [
    "BaseDrawTool",
    "CircleDrawTool",
    "LineDrawTool",
    "PointDrawTool",
    "PolygonDrawTool",
    "RectangleDrawTool",
    "TOOL_REGISTRY",
    "get_tool_class",
]
