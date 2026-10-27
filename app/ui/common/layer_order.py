"""图层下拉框的工程图层顺序：以**图层面板（图层树）从上到下**为准。

``QgsProject.mapLayers()`` 底层是 ``QMap<QString, layer>``，按图层 id（UUID）
排序，拿来填下拉框看起来就是乱序（用户报过多次）。``layerTreeRoot().findLayers()``
返回的才是图层面板里从上到下的顺序。

只负责"取哪些图层、按什么顺序"，不过滤几何类型；调用方自己按需筛。
"""

from __future__ import annotations

from qgis.core import QgsMapLayer, QgsProject, QgsVectorLayer


def layers_in_tree_order(project: QgsProject | None = None) -> list[QgsMapLayer]:
    """按图层树从上到下返回工程里的全部图层。

    图层树不可用（极少见）时退回 ``mapLayers()``；不在树里的图层补在末尾，
    避免出现"工程里有、下拉框里选不到"。
    """
    project = project or QgsProject.instance()
    layers: list[QgsMapLayer] = []
    try:
        root = project.layerTreeRoot()
    except (AttributeError, RuntimeError):
        root = None
    if root is not None:
        for node in root.findLayers():
            layer = node.layer()
            if layer is not None:
                layers.append(layer)
    known = {layer.id() for layer in layers}
    for layer in project.mapLayers().values():
        if layer.id() not in known:
            layers.append(layer)
    return layers


def vector_layers_in_tree_order(project: QgsProject | None = None) -> list[QgsVectorLayer]:
    """按图层树从上到下的顺序返回工程里**有效**的矢量图层。"""
    return [
        layer
        for layer in layers_in_tree_order(project)
        if isinstance(layer, QgsVectorLayer) and layer.isValid()
    ]


__all__ = ["layers_in_tree_order", "vector_layers_in_tree_order"]
