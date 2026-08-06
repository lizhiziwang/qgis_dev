"""主窗口构建工厂（阶段 4 起切到新 :class:`MainWindow`）。"""

from __future__ import annotations

from typing import Any

from app.ui.main_window import MainWindow


def build_window() -> Any:
    """构建主窗口（新 :class:`MainWindow`）。"""
    return MainWindow()
