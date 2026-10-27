"""矢量瓦片领域。"""

from __future__ import annotations

from .service import TileJob, TileService, write_vector_tile_viewer

__all__ = ["TileJob", "TileService", "write_vector_tile_viewer"]


def __getattr__(name: str):
    """按需 import :class:`CreateTileTask`（依赖 qgis.core）。"""
    if name == "CreateTileTask":
        from .task import CreateTileTask

        return CreateTileTask
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
