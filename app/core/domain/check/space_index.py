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

**使用范围（重要）**：交互式的点选 / 框选（``intersect_features``）**已不再**用它
—— 那条路径改为把 bbox 下沉给 provider（``QgsFeatureRequest.setFilterRect``），
避开了「读全表建索引 + 逐 fid ``getFeature`` 回读」的开销（2 万要素 1.4 s -> 84 ms）。
本模块现在只服务**质检批量检查**（``space_overlap``）这类一次跑完、需要反复做
Python 侧相交判断的场景。
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Optional

if TYPE_CHECKING:
    from qgis.core import QgsSpatialIndex, QgsVectorLayer

_INDEX_ATTR = "__spatial_index__"


def build_spatial_index(layer: "Optional[QgsVectorLayer]") -> "Optional[QgsSpatialIndex]":
    """为 ``layer`` 构建 :class:`QgsSpatialIndex`，挂在 layer 的 ``__spatial_index__``。

    若已存在，直接返回缓存。``layer`` 无效返回 ``None``。

    注意：这会把**整层要素（含属性）读进 Python**（2 万要素实测 ~0.5 s），且是
    快照——编辑后必须 ``clear_spatial_index``。交互路径请改用 provider 的
    ``setFilterRect`` 过滤（见 ``query.intersect_features``）。
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
