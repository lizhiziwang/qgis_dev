"""编辑栈（撤销 / 重做）。

撤销重做的栈步进、当前可编辑图层的判定与编辑后的安全刷新。

由 :class:`~app.ui.main_window.main_window.MainWindow` 多继承组合，方法只依赖
``self`` 上的共享状态。
"""

from __future__ import annotations

from contextlib import suppress

from qgis.core import QgsVectorLayer

from app.core.domain.check.space_index import clear_spatial_index
from app.infra.logging import get_logger

logger = get_logger(__name__)


class EditStackMixin:
    """EditStackMixin：见模块 docstring。"""

    # ===================================================== 编辑撤销 / 重做

    def _undo_edit(self) -> None:
        """Ctrl+Z：**画到一半先回退落点**；否则撤销编辑图层最近一个编辑命令。

        目标图层取 :meth:`_first_editable_layer`（优先编辑目标，其次画布上任意
        可编辑矢量图层）；执行后清空间索引缓存并推迟出栈刷新画布。
        """
        if self._step_drawing_point(undo=True):
            return
        self._step_edit_stack(undo=True)

    def _redo_edit(self) -> None:
        """Ctrl+Y / Ctrl+Shift+Z：画到一半先放回落点；否则重做编辑命令。"""
        if self._step_drawing_point(undo=False):
            return
        self._step_edit_stack(undo=False)

    def _step_drawing_point(self, *, undo: bool) -> bool:
        """当前画布工具正在绘制（还有落点）时，先让它的落点栈走一步。

        **必须在这里拦一道**：画布上的 Ctrl+Z / Ctrl+Y 是 QShortcut，优先级高于
        地图工具的 ``keyPressEvent``——工具自己收不到这个键。画到一半按 Ctrl+Z，
        用户要的是"退回上一个点"（ArcMap 的手感），不是把整幅编辑撤销掉。
        """
        try:
            tool = self.canvas.mapTool()
        except RuntimeError:
            return False
        if tool is None:
            return False
        method = getattr(tool, "undo_last_point" if undo else "redo_last_point", None)
        if not callable(method):
            return False
        try:
            return bool(method())
        except Exception:  # noqa: BLE001 —— 退回失败就落回常规的编辑撤销路径
            logger.exception("绘制中回退落点失败")
            return False

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
