"""绘图管理器 UI 集成。

业务逻辑在 :mod:`app.core.domain.drawing`，本模块只负责把 :class:`DrawManager`
挂到工具条/菜单上。
"""

from __future__ import annotations

from typing import Callable, Optional

from PyQt5.QtCore import Qt
from PyQt5.QtWidgets import QAction, QMainWindow, QMenu, QToolBar, QToolButton

from app.core.domain.drawing import DrawManager


class DrawManagerToolbar:
    """把 :class:`DrawManager` 挂到主窗口的工具条。"""

    def __init__(self, draw_manager: DrawManager) -> None:
        self.draw_manager = draw_manager

    def add_to_toolbar(self, main_window: QMainWindow) -> QToolBar:
        tb = main_window.addToolBar("编辑工具")
        tb.setObjectName("DigitizingToolbar")

        for label, geom_type in [
            ("开始编辑", "_start_edit"),
            ("停止编辑", "_stop_edit"),
            ("保存编辑", "_save_edit"),
            ("添加点", "point"),
            ("添加线", "line"),
            ("添加面", "polygon"),
        ]:
            btn = QToolButton()
            btn.setText(label)
            btn.setPopupMode(QToolButton.InstantPopup)
            tb.addWidget(btn)
        return tb

    def enable(self, geometry_type: str) -> None:
        self.draw_manager.enable_draw_tool(geometry_type)
