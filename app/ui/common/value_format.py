"""属性值的**展示**格式：把 QGIS / PyQt 的值渲染成给人看的文本。

**日期 / 时间不能直接 ``str()``**：PyQt6 下 ``str(QDateTime(...))`` 给的是

    PyQt6.QtCore.QDateTime(2026, 2, 6, 11, 56, 10, 0, PyQt6.QtCore.Qt.TimeSpec(1))

这种 repr —— 用户报过「要素详情里『修改时间』显示成这一长串」。展示一律走这里。

注意与 :meth:`app.ui.dialogs.custom_attr.CustomAttrDialog._format_field_value` 的区别：
那个用于**编辑器文本**（要与解析函数互逆、还要把 bool 写成「是/否」），这里只负责
"给人看"，不参与回写。
"""

from __future__ import annotations

from qgis.PyQt.QtCore import QDate, QDateTime, QTime, QVariant


def unwrap(value):
    """``QVariant`` → 里面的 Python 值；不是 ``QVariant`` 就原样返回。"""
    return value.value() if isinstance(value, QVariant) else value


def format_value(value) -> str:
    """属性值 → 展示文本。``None`` 返回空串（"无数据"那类占位由调用方决定）。"""
    value = unwrap(value)
    if value is None:
        return ""
    if isinstance(value, QDateTime):
        return value.toString("yyyy-MM-dd HH:mm:ss")
    if isinstance(value, QDate):
        return value.toString("yyyy-MM-dd")
    if isinstance(value, QTime):
        return value.toString("HH:mm:ss")
    return str(value)


__all__ = ["format_value", "unwrap"]
