"""通用 Dialog 基类。

所有自定义 dialog 继承本类：复用主窗口的无边框标题栏，但只保留
**图标 / 窗口标题 / 最小化 / 最大化 / 关闭** —— 用户信息、主题切换、
模式切换不进对话框边框（``enable_frameless`` 三个 show 参数传 False）。

布局约定：

- 基类经 :meth:`FramelessMixin.enable_frameless` 挂载无边框布局
  ``_frameless_layout``（0 边距，标题栏在上），其下挂一个内容容器
  ``_content``（QWidget，撑满剩余空间）。
- 子类构建内容照旧 ``layout = QVBoxLayout()`` … ``self.setLayout(layout)``：
  :meth:`setLayout` 被重写，把内容挂到 ``_content`` 上（走标准
  ``QWidget.setLayout``，子控件正确重挂父级）。不能直接把内容子布局
  ``addLayout`` 进 ``_frameless_layout``——父less 布局的子控件不会因此被
  重挂到对话框，内容会消失。
- **不要用 ``QVBoxLayout(self)`` 这类父构造式布局**：dialog 已有
  ``_frameless_layout``，Qt 会静默忽略新布局，内容不显示。
"""

from __future__ import annotations

from PyQt6.QtCore import Qt
from PyQt6.QtWidgets import QDialog, QLayout, QWidget

from app.ui.common.frameless import FramelessMixin
from app.ui.common.icons import Icons


class BaseDialog(FramelessMixin, QDialog):
    """所有自定义 Dialog 的基类（无边框 + 精简标题栏）。"""

    def __init__(self, parent=None, title: str = "") -> None:
        super().__init__(parent)
        if title:
            self.setWindowTitle(title)
        # 无边框 + 精简标题栏：只留 图标/标题/最小化/最大化/关闭
        self.title_bar = self.enable_frameless(
            show_user_button=False,
            show_theme_button=False,
            show_mode_switch=False,
        )
        # 内容容器：标题栏之下，撑满剩余空间；子类布局经 setLayout 挂到它上面
        self._content = QWidget(self)
        self._frameless_layout.addWidget(self._content, 1)
        self.setWindowIcon(Icons.app())
        self.setMinimumSize(800, 600)

    def setLayout(self, layout: QLayout) -> None:  # noqa: N802 —— Qt 命名
        """子类 ``self.setLayout(content)`` 挂到内容容器，不替换无边框布局。"""
        content = getattr(self, "_content", None)
        if content is not None:
            content.setLayout(layout)
            return
        super().setLayout(layout)

    def set_ui(self) -> None:
        """子类重写以构建 UI。"""
        self.setMinimumSize(800, 600)
