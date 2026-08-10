"""通用 Dialog 基类。

所有自定义 dialog 继承本类，自动设置 window icon、title、最小尺寸。
"""

from __future__ import annotations

from PyQt6.QtCore import Qt
from PyQt6.QtGui import QIcon
from PyQt6.QtWidgets import QDialog

from app.ui.common.icons import Icons


class BaseDialog(QDialog):
    """所有自定义 Dialog 的基类。"""

    def __init__(self, parent=None, title: str = "") -> None:
        super().__init__(parent)
        self.title = title
        self.setWindowIcon(Icons.app())
        if title:
            self.setWindowTitle(title)
        self.setMinimumSize(800, 600)

    def set_ui(self) -> None:
        """子类重写以构建 UI。"""
        self.setMinimumSize(800, 600)
