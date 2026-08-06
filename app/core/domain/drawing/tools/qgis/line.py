"""QGIS 复用版 - 线绘制工具。

基于 :class:`qgis.gui.QgsMapToolCapture`：

- 左键加点 → ``addVertex`` 由 QGIS 内部管理
- 右键或双击结束 → ``digitizingFinished`` 信号，参数为 ``QgsGeometry``
- 节点捕捉 / 撤销 / Advanced Digitizing Dock 等能力自动获得

实现：单继承 ``BaseQgsDrawTool``（QObject），内部持有 ``QgsMapToolCapture``
实例（``tool`` 属性暴露给 :class:`DrawManager`）。
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from qgis.core import QgsGeometry
    from qgis.gui import QgsMapCanvas

from qgis.gui import QgsMapToolCapture

from .base import BaseQgsDrawTool


class QGISLineDrawTool(BaseQgsDrawTool):
    """画线（QGIS 内置版本），自动获得捕捉 / 撤销 / 高级数字化。"""

    def __init__(
        self,
        canvas: "QgsMapCanvas",
        *,
        is_buffer: bool = False,
        distance: float = 0.0,
    ) -> None:
        super().__init__(canvas, is_buffer=is_buffer, distance=distance)
        # CaptureMode.CaptureLine 限定为多段线（不支持闭合）
        self._tool = QgsMapToolCapture(canvas, QgsMapToolCapture.CaptureLine)
        self._tool.digitizingFinished.connect(self._on_digitizing_finished)
        self._tool.digitizingCanceled.connect(self._on_digitizing_canceled)

    @property
    def tool(self):  # noqa: ANN201
        return self._tool

    def _on_digitizing_finished(self, geometry) -> None:  # noqa: ANN001
        self._emit_finished(geometry)

    def _on_digitizing_canceled(self) -> None:
        # 取消也算一次结束（不发 finished，避免上层误以为绘制成功）
        try:
            self._safe_unset_tool()
        except Exception:  # noqa: BLE001
            pass

    def cleanup(self) -> None:
        try:
            self._tool.digitizingFinished.disconnect(self._on_digitizing_finished)
            self._tool.digitizingCanceled.disconnect(self._on_digitizing_canceled)
        except Exception:  # noqa: BLE001
            pass
        super().cleanup()
