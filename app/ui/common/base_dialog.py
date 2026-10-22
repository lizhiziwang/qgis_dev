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
from qgis.PyQt.QtGui import QGuiApplication
from qgis.PyQt.QtWidgets import (
    QDialog,
    QFrame,
    QLayout,
    QMainWindow,
    QScrollArea,
    QWidget,
)

from app.ui.common.frameless import FramelessMixin
from app.ui.common.icons import Icons
from app.ui.common.minimized_bar import MinimizedBar


def make_scrollable(widget: QWidget, *, horizontal: bool = False) -> QScrollArea:
    """把参数面板装进无边框滚动区。

    对话框被纵向拉小时，内容保持自身最小高度并由滚动条承载，而不是把控件
    压扁到看不清；内容本身不高时滚动条不出现。
    """
    scroll = QScrollArea()
    scroll.setWidgetResizable(True)
    scroll.setFrameShape(QFrame.Shape.NoFrame)
    scroll.setWidget(widget)
    scroll.setHorizontalScrollBarPolicy(
        Qt.ScrollBarPolicy.ScrollBarAsNeeded
        if horizontal
        else Qt.ScrollBarPolicy.ScrollBarAlwaysOff
    )
    return scroll


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

    # -------------------------------------------------------- 尺寸自适应

    def fit_height_to_content(
        self, content: QWidget, *, min_height: int = 0, extra: int = 0
    ) -> None:
        """按左侧参数内容高度调整初始窗口高度。

        分析工具对话框的参数通常只比默认高度差一点，刚打开就出现滚动条；
        这里在构造完成后按参数面板的 ``sizeHint`` 把窗口高度调到刚好容纳，
        上限用屏幕可用高度（不再受主窗口高度限制），并可用 ``extra`` 再多留
        一点余量避免边缘滚动条。用户后续拉小窗口时，参数区由
        :func:`make_scrollable` 的滚动条承载，控件不会被压扁。
        """
        layout = self._content.layout()
        if layout is None:
            return
        layout.activate()
        main_h = content.sizeHint().height()
        extra_h = 0
        for index in range(1, layout.count()):
            item = layout.itemAt(index)
            if item is not None:
                extra_h += item.sizeHint().height()
        margins = layout.contentsMargins()
        spacing = max(0, layout.spacing()) * max(0, layout.count() - 1)
        title_h = max(self.title_bar.height(), self.title_bar.sizeHint().height())
        total = (
            main_h
            + extra_h
            + margins.top()
            + margins.bottom()
            + spacing
            + title_h
            + 4
            + max(0, extra)
        )
        max_h = self._max_window_height()
        if max_h:
            total = min(total, max_h)
        height = max(total, min_height, self.minimumHeight())
        self.resize(self.width(), height)

    def _max_window_height(self) -> int | None:
        """可用的最大窗口高度：优先用屏幕可用高度。

        参数特别多的算法（Grid IDW / PostgreSQL 导出等）比主窗口还高；
        按主窗口高度封顶会永远差一点、必须滚动。这里改成屏幕可用高度
        减 24px（留出任务栏 / 边缘），让这类窗口能完整展开。
        """
        screen = QGuiApplication.screenAt(self.pos()) or QGuiApplication.primaryScreen()
        if screen is not None:
            return max(400, screen.availableGeometry().height() - 24)
        node = self.parent()
        while node is not None:
            if isinstance(node, QMainWindow) and node.height() > 0:
                return node.height()
            node = node.parent()
        return None

    def set_ui(self) -> None:
        """子类重写以构建 UI。"""
        self.setMinimumSize(800, 600)
