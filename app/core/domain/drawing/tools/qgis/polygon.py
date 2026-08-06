"""QGIS 复用版 - 面绘制工具。

基于 :class:`qgis.gui.QgsMapToolCapture`，``CaptureMode.CapturePolygon``。
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from qgis.core import QgsGeometry
    from qgis.gui import QgsMapCanvas

from qgis.gui import QgsMapToolCapture

from .base import BaseQgsDrawTool


class QGISPolygonDrawTool(BaseQgsDrawTool):
    """画面（QGIS 内置版本），自动获得捕捉 / 闭合 / 撤销。"""

    def __init__(
        self,
        canvas: "QgsMapCanvas",
        *,
        is_buffer: bool = False,
        distance: float = 0.0,
    ) -> None:
        super().__init__(canvas, is_buffer=is_buffer, distance=distance)
        self._tool = QgsMapToolCapture(canvas, QgsMapToolCapture.CapturePolygon)
        self._tool.digitizingFinished.connect(self._on_digitizing_finished)
        self._tool.digitizingCanceled.connect(self._on_digitizing_canceled)

    @property
    def tool(self):  # noqa: ANN201
        return self._tool

    def _on_digitizing_finished(self, geometry) -> None:  # noqa: ANN001
        self._emit_finished(geometry)

    def _on_digitizing_canceled(self) -> None:
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
