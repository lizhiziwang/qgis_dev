"""影像切片领域。"""

from __future__ import annotations

from .service import ImageryTileJob, ImageryTileService

__all__ = ["ImageryTileJob", "ImageryTileService"]


def __getattr__(name: str):
    """按需 import :class:`CreateImageryTileTask`（依赖 qgis.core）。"""
    if name == "CreateImageryTileTask":
        from .task import CreateImageryTileTask

        return CreateImageryTileTask
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
