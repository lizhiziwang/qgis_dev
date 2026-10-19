"""图层编辑服务。

迁自旧 :class:`component.map_tool.LayerEditTool` 与 :class:`Draw.stop_edit`，
去掉对主窗口的耦合。
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Optional

if TYPE_CHECKING:
    from qgis.core import QgsMapLayer, QgsVectorLayer


class LayerEditService:
    """封装"开始/停止/保存/回滚"等图层编辑操作。

    所有方法接受 :class:`QgsVectorLayer`，不依赖任何 UI 组件。UI 层
    （如 :class:`PagedAttributeTableWidget`）只调本服务，不直接调
    ``layer.startEditing()`` 等。
    """

    def start(self, layer: "QgsVectorLayer") -> bool:
        if layer is None or layer.type() != layer.VectorLayer:
            return False
        if layer.isEditable():
            return True
        ok = layer.startEditing()
        return bool(ok)

    def stop(self, layer: "QgsVectorLayer", save: bool = True) -> bool:
        """停止编辑。``save=True`` 提交；``False`` 回滚并结束编辑。

        **返回值的口径是「图层真的离开编辑态了没」**：``commitChanges(stopEditing=True)``
        在「没有改动要提交」时（回滚之后就是这样）会返回 ``False``，但编辑确实已经结束 ——
        拿返回码当成功标志会把"停好了"误报成失败。提交失败时图层仍处于编辑态，
        所以 ``not isEditable()`` 正好同时表达两种结果。
        """
        if not layer.isEditable():
            return True
        if save:
            layer.commitChanges(stopEditing=True)
        else:
            layer.rollBack()
            if layer.isEditable():
                # rollBack 只清编辑缓冲、仍在编辑态；再 commit 一次把它关掉
                layer.commitChanges(stopEditing=True)
        return not layer.isEditable()

    def commit(self, layer: "QgsVectorLayer", stop_editing: bool = False) -> bool:
        if not layer.isEditable():
            return True
        return bool(layer.commitChanges(stopEditing=stop_editing))

    def rollback(self, layer: "QgsVectorLayer") -> bool:
        if not layer.isEditable():
            return True
        return bool(layer.rollBack())

    def is_modified(self, layer: "QgsVectorLayer") -> bool:
        if layer is None or not layer.isEditable():
            return False
        return bool(layer.isModified())

    def is_editable(self, layer: "QgsMapLayer") -> bool:
        if layer is None:
            return False
        if not hasattr(layer, "isEditable"):
            return False
        return bool(layer.isEditable())
