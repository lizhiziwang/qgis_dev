"""QGIS 选择集通用常量与版本兼容助手。

「条件查询」「空间查询」都按 QGIS 的选择行为把结果写入图层选择集，下拉文案与
枚举名的对应关系只在这里维护一份。

QGIS 4 把 ``QgsVectorLayer.SelectionMode`` 挪到了 ``Qgis.SelectBehavior``；本模块
运行时探测，两代 QGIS 都能跑。
"""

from __future__ import annotations

from typing import Any

from qgis.core import Qgis, QgsVectorLayer

#: 选择方式：(界面文案, ``Qgis.SelectBehavior`` 枚举名)
SELECT_METHODS: tuple[tuple[str, str], ...] = (
    ("创建一个新的选择集", "SetSelection"),
    ("增加到当前选择集", "AddToSelection"),
    ("从当前选择集中移除", "RemoveFromSelection"),
)


def selection_behavior(action: str) -> Any:
    """按名字取 QGIS 选择行为枚举，兼容 QGIS 4 的 ``Qgis.SelectBehavior``。"""
    enum = getattr(Qgis, "SelectBehavior", None)
    if enum is None:  # QGIS 4 之前的回退
        enum = getattr(QgsVectorLayer, "SelectionMode")
    return getattr(enum, action)


__all__ = ["SELECT_METHODS", "selection_behavior"]
