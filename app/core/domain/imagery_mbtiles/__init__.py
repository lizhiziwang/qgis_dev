"""影像切片（MBTiles 输出）领域。"""

from __future__ import annotations

from .service import ImageryMbtilesJob, ImageryMbtilesService

__all__ = ["ImageryMbtilesJob", "ImageryMbtilesService"]


def __getattr__(name: str):
    """按需 import :class:`CreateImageryMbtilesTask`（依赖 qgis.core）。"""
    if name == "CreateImageryMbtilesTask":
        from .task import CreateImageryMbtilesTask

        return CreateImageryMbtilesTask
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
