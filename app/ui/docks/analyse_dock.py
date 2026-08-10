"""分析面板 Dock。

真实"分析工具"dock 实现见 :mod:`app.ui.docks.tools_dock`（:class:`ToolsDock`）。
本模块保留 :class:`AnalyseDock` 兼容名并指向真实实现，避免"两个分析工具 dock"
同名类带来的混淆。
"""

from __future__ import annotations

from app.ui.docks.tools_dock import ToolsDock

AnalyseDock = ToolsDock

__all__ = ["AnalyseDock"]
