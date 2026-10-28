"""质检专项对话框的「再次打开」状态处理。

这些窗口由 :class:`QualityDock` **复用**（关闭不销毁）：图层、参考图层、过滤条件、
阈值、输出路径等参数原样保留，下次打开不用重新选一遍。每次显示只做两件事：

1. 按当前工程重刷图层下拉，但**保留已选图层**（图层被删掉了才退回上下文图层）；
2. 复位上一轮检查的运行态（进度条、阶段文字、确认按钮、失败列表），
   避免下次打开看到上次的 100% 进度或按钮变灰。
"""

from __future__ import annotations

from app.infra.logging import get_logger

logger = get_logger(__name__)

#: 质检窗口的初始阶段文字（各窗口原 `_STATUS_READY` 统一到这里）
READY_TEXT = "就绪：点「确认」开始检查"


class CheckDialogRunStateMixin:
    """给 7 个空间质检对话框复用的一套 show/复位逻辑。"""

    READY_TEXT = READY_TEXT

    def showEvent(self, event) -> None:  # noqa: N802 —— Qt 命名
        super().showEvent(event)
        try:
            self._populate_layers()   # 刷新图层列表，但保留已选图层
        except Exception as exc:  # noqa: BLE001 —— 打开窗口不能因刷新下拉失败
            logger.warning("质检窗口刷新图层列表失败：%s", exc, exc_info=True)
        self.reset_check_run_state()

    def reset_check_run_state(self) -> None:
        """清掉上一轮检查的运行态；参数控件（图层/过滤/阈值/输出路径）不动。"""
        self._cancelled = True
        self._failed = []
        self._seen = 0
        self._total = 0
        self._param = None
        self._output_path = None
        self._iterator = None
        confirm = getattr(self, "confirm_btn", None)
        if confirm is not None:
            confirm.setEnabled(True)
        progress = getattr(self, "progress", None)
        if progress is not None:
            progress.setValue(0)
        label = getattr(self, "progress_label", None)
        if label is not None:
            label.setText(self.READY_TEXT)

    @staticmethod
    def _layer_id(layer):
        """图层 id；图层已被销毁时返回 ``None``。

        图层从工程移除后，combo 的 itemData 可能只剩一个**裸 QObject**（Qt 不再
        持有 ``QgsVectorLayer`` 包装），此时 ``.id()`` 会抛 ``AttributeError``；
        不能让它把整个下拉刷新打断。
        """
        try:
            return layer.id() if layer is not None else None
        except (AttributeError, RuntimeError):
            return None

    def _fill_layer_combo(self, combo, layers, context_id) -> None:
        """重建单个图层下拉：**上次选中的图层 > 上下文图层 > 第一项**。

        只有上次选中的图层已从工程移除时才会退回上下文图层，保证再次打开窗口
        参数不被刷新。
        """
        previous_id = self._layer_id(combo.currentData())
        combo.blockSignals(True)
        combo.clear()
        for layer in layers:
            combo.addItem(layer.name(), layer)

        index = 0 if layers else -1
        for i in range(combo.count()):
            if previous_id is not None and self._layer_id(combo.itemData(i)) == previous_id:
                index = i
                break
        else:
            for i in range(combo.count()):
                if context_id is not None and self._layer_id(combo.itemData(i)) == context_id:
                    index = i
                    break
        combo.setCurrentIndex(index)
        combo.blockSignals(False)


__all__ = ["READY_TEXT", "CheckDialogRunStateMixin"]
