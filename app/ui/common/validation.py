"""对话框内的校验提示：定位 / 高亮具体控件，替代主窗口浮动提示条。

分析工具都是**独立对话框**，``send()`` 把提示写到主窗口底部 messagebar 时，
弹窗会把提示条挡住，用户看不到「哪一项没填」。必填项校验统一走
:func:`focus_and_highlight`：把焦点移到对应输入框、必要时展开折叠区、描红
并在控件旁弹出提示文字；非字段类错误由调用方改用
:class:`~app.ui.common.message_box.MessageBox` 弹小窗。
"""

from __future__ import annotations

import contextlib

from qgis.PyQt import sip
from qgis.PyQt.QtCore import QPoint, Qt, QTimer
from qgis.PyQt.QtWidgets import (
    QAbstractItemView,
    QAbstractSpinBox,
    QComboBox,
    QLineEdit,
    QToolTip,
    QWidget,
)

#: 校验失败的临时描边（只补边框，不改背景 / 字体，1.8s 后恢复原样式）
_ERROR_BORDER_QSS = "border: 1px solid #d33; border-radius: 4px;"

#: 描边保留时长（ms）
_HIGHLIGHT_MS = 1800


def focus_and_highlight(widget: QWidget | None, message: str = "") -> None:
    """把焦点移到 ``widget``、滚动到可见位置，短暂描红并在下方显示提示。

    :param widget: 校验失败的输入控件（QLineEdit / QComboBox / QSpinBox / 列表等）
    :param message: 就近显示的提示文字（为空则只描红 + 聚焦）
    """
    if widget is None:
        return
    target = _focusable_target(widget)
    try:
        target.setFocus()
        if isinstance(target, (QLineEdit, QAbstractSpinBox)):
            target.selectAll()
    except (RuntimeError, AttributeError):
        return
    if message:
        _show_tip(target, message)
    _flash_border(target)


def _focusable_target(widget: QWidget) -> QWidget:
    """部分 wrapper 外层是不可聚焦的容器：找里面第一个可聚焦输入控件。"""
    try:
        if widget.focusPolicy() != Qt.FocusPolicy.NoFocus:
            return widget
        for cls in (QLineEdit, QComboBox, QAbstractSpinBox, QAbstractItemView):
            child = widget.findChild(cls)
            if child is not None:
                return child
    except RuntimeError:
        pass
    return widget


def _show_tip(widget: QWidget, message: str) -> None:
    """在控件下方就近弹出提示（3.5s 自动消失，不拦操作）。"""
    with contextlib.suppress(RuntimeError, AttributeError):
        QToolTip.showText(
            widget.mapToGlobal(QPoint(0, widget.height())),
            message,
            widget,
            msecShowTime=3500,
        )


def _flash_border(widget: QWidget) -> None:
    """给控件加一层临时红色描边，到期恢复原样式（可重复调用）。"""
    try:
        base = widget.property("_validation_base_qss")
        if base is None:
            base = widget.styleSheet()
            widget.setProperty("_validation_base_qss", base)
        widget.setStyleSheet((base or "") + _ERROR_BORDER_QSS)
    except RuntimeError:
        return

    def _restore() -> None:
        try:
            if sip.isdeleted(widget):
                return
            widget.setStyleSheet(widget.property("_validation_base_qss") or "")
            widget.setProperty("_validation_base_qss", None)
        except (RuntimeError, TypeError):
            pass

    QTimer.singleShot(_HIGHLIGHT_MS, _restore)


__all__ = ["focus_and_highlight"]
