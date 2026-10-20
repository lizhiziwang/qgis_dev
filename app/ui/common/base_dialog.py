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

from qgis.PyQt.QtCore import Qt
from qgis.PyQt.QtWidgets import QDialog, QLayout, QWidget

from app.ui.common.frameless import FramelessMixin
from app.ui.common.icons import Icons
from app.ui.common.minimized_bar import MinimizedBar


class BaseDialog(FramelessMixin, QDialog):
    """所有自定义 Dialog 的基类（无边框 + 精简标题栏）。"""

    def __init__(
        self,
        parent=None,
        title: str = "",
        *,
        show_min_button: bool = True,
        show_max_button: bool = True,
    ) -> None:
        super().__init__(parent)
        if title:
            self.setWindowTitle(title)
        # 无边框 + 精简标题栏：只留 图标/标题/最小化/最大化/关闭
        # （小弹窗不需要最小化 / 最大化时，构造时传 False 即可，别去 hide 按钮）
        self.title_bar = self.enable_frameless(
            show_user_button=False,
            show_theme_button=False,
            show_mode_switch=False,
            show_min_button=show_min_button,
            show_max_button=show_max_button,
        )
        # 内容容器：标题栏之下，撑满剩余空间；子类布局经 setLayout 挂到它上面
        self._content = QWidget(self)
        self._frameless_layout.addWidget(self._content, 1)
        self.setWindowIcon(Icons.app())
        self.setMinimumSize(800, 600)
        #: 最小化到画布底部的细条（懒建）与最小化前几何
        self._mini_bar = None
        self._mini_restore_geo = None
        if show_min_button:
            # FramelessMixin 默认把最小化接到 showMinimized；这里改成和
            # 「属性录入」一样缩成画布底部小条。模态弹窗仍走系统最小化
            # （模态期间画布/小条都点不到，缩成小条反而无法恢复）。
            try:
                self.title_bar.minimize_requested.disconnect()
            except TypeError:
                pass
            self.title_bar.minimize_requested.connect(self._minimize_to_canvas_bar)

    # ------------------------------------------------------------ 最小化小条

    def _dialog_canvas(self):
        """找一个可承载底部小条的画布（本窗口 / 父窗口链 / 显式 canvas 属性）。"""
        canvas = getattr(self, "canvas", None) or getattr(self, "_canvas", None)
        if canvas is not None:
            return canvas
        node = self.parent()
        while node is not None:
            canvas = getattr(node, "canvas", None)
            if canvas is not None:
                return canvas
            node = node.parent()
        return None

    def _minimize_to_canvas_bar(self) -> None:
        canvas = self._dialog_canvas()
        if canvas is None or self.isModal():
            # 没有画布 / 模态弹窗：保持系统最小化，别让用户失去还原入口
            self.showMinimized()
            return
        self._mini_restore_geo = self.geometry()
        self.hide()
        bar = self._ensure_mini_bar(canvas)
        bar.set_summary(self.windowTitle() or "窗口已最小化")
        bar.show()
        bar.raise_()
        bar.reposition()

    def _ensure_mini_bar(self, canvas):
        bar = self._mini_bar
        if bar is not None:
            try:
                bar.objectName()
            except RuntimeError:  # C++ 对象已销毁
                bar = None
        if bar is None:
            bar = MinimizedBar(canvas, on_restore=self._restore_from_mini_bar)
            self._mini_bar = bar
        return bar

    def _restore_from_mini_bar(self) -> None:
        self._hide_mini_bar()
        if self._mini_restore_geo is not None:
            self.setGeometry(self._mini_restore_geo)
        self.show()
        self.raise_()
        self.activateWindow()

    def _hide_mini_bar(self) -> None:
        bar = self._mini_bar
        if bar is None:
            return
        try:
            bar.hide()
        except RuntimeError:  # C++ 对象已销毁
            self._mini_bar = None

    def showEvent(self, event) -> None:  # noqa: N802 —— Qt 命名
        # 从底部小条恢复（或从别的入口重新 show）时，小条让位
        self._hide_mini_bar()
        super().showEvent(event)

    def done(self, result: int) -> None:  # noqa: N802 —— Qt 命名
        self._hide_mini_bar()
        super().done(result)

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
