"""最短路径分析领域。"""

from __future__ import annotations

from .service import ShortestPathJob, ShortestPathService

__all__ = ["ShortestPathJob", "ShortestPathService"]


def __getattr__(name: str):
    """按需 import 依赖 qgis.core 的任务/反馈类（非 GUI 环境不加载）。"""
    if name in ("ShortestPathFeedback", "ShortestPathRunnerTask"):
        from .task import ShortestPathFeedback, ShortestPathRunnerTask

        return {"ShortestPathFeedback": ShortestPathFeedback, "ShortestPathRunnerTask": ShortestPathRunnerTask}[name]
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
