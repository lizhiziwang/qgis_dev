"""离线下载领域包（方案 §1 目标：按任务信息把数据抓成本地压缩包离线使用）。

分层：
- 纯逻辑（不依赖 QGIS/Qt，可 headless 单测）：:mod:`model` :mod:`task_info`
  :mod:`planner` :mod:`layout` :mod:`disk` :mod:`srs`；
- 运行时才按需加载：:mod:`fetcher`（抓取）:mod:`task`（后台线程）:mod:`service`
  :mod:`manager`（下载管理）。QGIS/Qt 依赖在相应模块内按需 import（方案 §6.6）。
"""

from __future__ import annotations

from . import disk, layout, planner, srs
from .model import (
    ALL_KEYS,
    KEY_LABELS,
    REQUIRED_KEYS,
    DownloadRequest,
    DownloadStatus,
    FetchItem,
    FetchPlan,
    PlanError,
    SpatialScope,
)
from .task_info import (
    DataSource,
    ImageryItem,
    SpatialExtent,
    TaskInfo,
    infer_source_type,
    resolve_source_type,
)

__all__ = [
    "DataSource",
    "ImageryItem",
    "SpatialExtent",
    "TaskInfo",
    "DownloadRequest",
    "DownloadStatus",
    "FetchItem",
    "FetchPlan",
    "PlanError",
    "SpatialScope",
    "KEY_LABELS",
    "REQUIRED_KEYS",
    "ALL_KEYS",
    "infer_source_type",
    "resolve_source_type",
    "disk",
    "layout",
    "planner",
    "srs",
]


def __getattr__(name: str):
    """按需 import 运行时依赖（qgis / Qt）。"""
    if name == "OfflineDownloadTask":
        from .task import OfflineDownloadTask

        return OfflineDownloadTask
    if name == "OfflineDownloadService":
        from .service import OfflineDownloadService

        return OfflineDownloadService
    if name == "DownloadManager":
        from .manager import DownloadManager

        return DownloadManager
    if name in ("FetchContext", "FetchError", "DownloadCanceled"):
        from . import fetcher

        return getattr(fetcher, name)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
