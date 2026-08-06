"""空间索引工具。

旧 ``component.check.SpaceDataCheck.build_space_index(vec_layer)`` 把索引
黏到 ``vec_layer.__index__``，会与 Python 内部 dunder 命名约定冲突。
新代码改用 ``__spatial_index__``，且 cache key 用 ``id(layer)`` 防止
layer 被销毁后索引泄漏。

**注意**：把对象挂到实例的 dunder 属性仍是 hack，仅在 ``QgsVectorLayer``
无法继承的情况下使用。后续如要清理可改为 layer-level 弱引用表。

类型注解里 :class:`QgsVectorLayer` 与 :class:`QgsSpatialIndex` 用
``TYPE_CHECKING`` 引入，让本模块在无 QGIS 环境下也能 import；运行
``build_spatial_index`` 时才真正需要 qgis。
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Optional

if TYPE_CHECKING:
    from qgis.core import QgsSpatialIndex, QgsVectorLayer

_INDEX_ATTR = "__spatial_index__"


def build_spatial_index(layer: "Optional[QgsVectorLayer]") -> "Optional[QgsSpatialIndex]":
    """为 ``layer`` 构建 :class:`QgsSpatialIndex`，挂在 layer 的 ``__spatial_index__``。

    若已存在，直接返回缓存。``layer`` 无效返回 ``None``。
    """
    from qgis.core import QgsSpatialIndex  # 延迟 import

    if layer is None or not layer.isValid():
        return None

    cached = getattr(layer, _INDEX_ATTR, None)
    if cached is not None:
        return cached

    index = QgsSpatialIndex()
    feats = [
        f for f in layer.getFeatures()
        if f.hasGeometry() and not f.geometry().isEmpty()
    ]
    index.addFeatures(feats)
    setattr(layer, _INDEX_ATTR, index)
    return index


def clear_spatial_index(layer: "Optional[QgsVectorLayer]") -> None:
    """清掉 layer 上的空间索引缓存（用于 layer 关闭/移除时）。"""
    if layer is None:
        return
    if hasattr(layer, _INDEX_ATTR):
        try:
            delattr(layer, _INDEX_ATTR)
        except (AttributeError, TypeError):
            pass


__all__ = ["build_spatial_index", "clear_spatial_index"]
