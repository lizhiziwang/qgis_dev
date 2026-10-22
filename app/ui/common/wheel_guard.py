"""对话框滚轮保护：滚过下拉框 / 数值框时不改值，继续滚动所在的滚动区。

Qt 的 ``QComboBox`` 与 ``QAbstractSpinBox`` **不需要焦点**就会被滚轮改值：用户想
继续往下滚动页面，鼠标经过控件时值却被改了（误操作）。本过滤器在指定根控件的
子树里拦下这类滚轮事件，并转发给最近的 ``QAbstractScrollArea``，滚动保持连续。

控件**自己获得焦点**时默认仍按 Qt 原生行为处理（那是用户明确要滚它）。但页面
打开 / 切换时 Qt 会把焦点放到第一个可聚焦控件上（常常正好是个下拉框），这时
滚轮经过它仍会改值、并且页面**再也滚不动**。这类页面用
``scroll_even_focused=True``：无论有没有焦点，滚轮一律滚动页面，改值只能靠
点击展开下拉或键盘。

过滤器挂在 ``QApplication`` 上、只对 ``root`` 子树生效；对话框关闭时调用
:meth:`detach` 卸掉，避免销毁后仍被回调。
"""

from __future__ import annotations

from qgis.PyQt.QtCore import QEvent, QObject, QPointF
from qgis.PyQt.QtGui import QWheelEvent
from qgis.PyQt.QtWidgets import (
    QAbstractScrollArea,
    QAbstractSpinBox,
    QApplication,
    QComboBox,
    QWidget,
)

#: 会吃滚轮改值的控件
_VALUE_WIDGETS = (QComboBox, QAbstractSpinBox)


class WheelValueGuard(QObject):
    """在 ``root`` 的子树里阻止"滚轮经过就改值"。

    Args:
        root: 生效范围（该控件的整棵子树）。
        scroll_even_focused: 为真时即使值控件有焦点也把滚轮转给滚动区。
            用于"打开页面就自动聚焦第一个下拉框"的表单页。
    """

    def __init__(self, root: QWidget, *, scroll_even_focused: bool = False) -> None:
        super().__init__(root)
        self._root = root
        self._scroll_even_focused = scroll_even_focused
        app = QApplication.instance()
        self._app = app
        if app is not None:
            app.installEventFilter(self)

    def detach(self) -> None:
        """卸掉应用级过滤器（对话框关闭时调用）。"""
        app = self._app
        self._app = None
        if app is not None:
            try:
                app.removeEventFilter(self)
            except RuntimeError:  # 应用已销毁
                pass

    def eventFilter(self, obj, event) -> bool:  # noqa: N802 —— Qt 命名
        if event.type() != QEvent.Type.Wheel or self._app is None:
            return False
        if not isinstance(obj, _VALUE_WIDGETS):
            return False
        try:
            if obj.hasFocus() and not self._scroll_even_focused:
                return False  # 有焦点：用户明确在操作它，走原生行为
            if obj is not self._root and not self._root.isAncestorOf(obj):
                return False  # 不是本根控件的子树
        except RuntimeError:  # 控件已销毁
            return False
        scroll = self._scroll_area(obj)
        if scroll is not None and self._forward(scroll, event):
            return True
        # 没有可滚动的地方也吞掉：宁可滚轮无效，也不能误改值
        return True

    @staticmethod
    def _scroll_area(widget: QWidget):
        """最近的 ``QAbstractScrollArea`` 祖先（滚轮要接着滚它）。"""
        node = widget.parentWidget()
        while node is not None:
            if isinstance(node, QAbstractScrollArea):
                return node
            node = node.parentWidget()
        return None

    @staticmethod
    def _forward(scroll, event) -> bool:
        """把滚轮事件重投给滚动区 viewport；成功返回 ``True``。"""
        viewport = scroll.viewport()
        if viewport is None:
            return False
        try:
            pos = QPointF(viewport.mapFromGlobal(event.globalPosition().toPoint()))
            builder = [
                pos,
                event.globalPosition(),
                event.pixelDelta(),
                event.angleDelta(),
                event.buttons(),
                event.modifiers(),
                event.phase(),
                event.inverted(),
            ]
            # 本构建的 PyQt6 未必有第 9 个 source 参数，按需探测（见 canvas_wheel.py）
            source = getattr(event, "source", None)
            if callable(source):
                builder.append(source())
            try:
                forwarded = QWheelEvent(*builder)
            except TypeError:  # 没有 9 参数重载
                forwarded = QWheelEvent(*builder[:8])
            QApplication.sendEvent(viewport, forwarded)
        except Exception:  # noqa: BLE001 —— 构造 / 重投失败就放弃
            return False
        return True


__all__ = ["WheelValueGuard"]
