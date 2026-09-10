"""编辑栈（撤销 / 重做）。

撤销重做的栈步进、当前可编辑图层的判定与编辑后的安全刷新。

由 :class:`~app.ui.main_window.main_window.MainWindow` 多继承组合，方法只依赖
``self`` 上的共享状态。
"""

from __future__ import annotations

from contextlib import suppress

from qgis.core import QgsVectorLayer

from app.core.domain.check.space_index import clear_spatial_index


class EditStackMixin:
    """EditStackMixin：见模块 docstring。"""

    # ===================================================== 编辑撤销 / 重做

    def _undo_edit(self) -> None:
        """Ctrl+Z：撤销当前编辑图层最近一个编辑命令。

        目标图层取 :meth:`_first_editable_layer`（优先编辑目标，其次画布上任意
        可编辑矢量图层）；执行后清空间索引缓存并推迟出栈刷新画布。
        """
        self._step_edit_stack(undo=True)

    def _redo_edit(self) -> None:
        """Ctrl+Y / Ctrl+Shift+Z：重做最近被撤销的编辑命令。"""
        self._step_edit_stack(undo=False)

    def _step_edit_stack(self, undo: bool) -> None:
        """对优先编辑图层执行一步 undo / redo；无目标或无可用步骤则静默返回。"""
        target = self._first_editable_layer()
        if target is None:
            return
        stack = target.undoStack()
        if stack is None:
            return
        if undo and not stack.canUndo():
            return
        if not undo and not stack.canRedo():
            return
        if undo:
            stack.undo()
        else:
            stack.redo()
        # undo/redo 可能恢复或再次删除要素 → 空间索引缓存失效，下次命中需重建
        clear_spatial_index(target)
        from qgis.PyQt.QtCore import QTimer

        QTimer.singleShot(0, self._safe_edit_refresh)

    def _first_editable_layer(self) -> QgsVectorLayer | None:
        """撤销目标图层：优先 ``self.edit_layer``，否则画布上任一可编辑矢量图层。"""
        if self._is_undoable(self.edit_layer):
            return self.edit_layer
        for layer in self.canvas.layers():
            if self._is_undoable(layer):
                return layer
        return None

    @staticmethod
    def _is_undoable(layer) -> bool:
        """目标资格：矢量图层、处于编辑态、且已有编辑命令栈。"""
        return (
            layer is not None
            and isinstance(layer, QgsVectorLayer)
            and layer.isEditable()
            and layer.undoStack() is not None
        )

    def _safe_edit_refresh(self) -> None:
        """undo/redo 后刷新画布（事件栈外执行；窗口已销毁时静默）。"""
        with suppress(RuntimeError):
            self.canvas.refresh()
