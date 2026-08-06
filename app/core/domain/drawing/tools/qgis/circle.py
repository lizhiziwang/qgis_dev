"""QGIS 复用版 - 圆形绘制工具。

基于 :class:`qgis.gui.QgsMapToolShapeCircle`：

- 圆心 → 拖出半径 → ``shapeFinished`` 信号携带最终圆形几何
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from qgis.core import QgsGeometry
    from qgis.gui import QgsMapCanvas

from qgis.gui import QgsMapToolShapeCircle

from .base import BaseQgsDrawTool


class QGISCircleDrawTool(BaseQgsDrawTool):
    """圆心→半径绘制圆（QGIS 内置版本）。"""

    def __init__(
        self,
        canvas: "QgsMapCanvas",
        *,
        is_buffer: bool = False,
        distance: float = 0.0,
    ) -> None:
        super().__init__(canvas, is_buffer=is_buffer, distance=distance)
        self._tool = QgsMapToolShapeCircle(canvas)
        self._tool.shapeFinished.connect(self._on_shape_finished)
        self._tool.shapeCanceled.connect(self._on_shape_canceled)

    @property
    def tool(self):  # noqa: ANN201
        return self._tool

    def _on_shape_finished(self, geometry) -> None:  # noqa: ANN001
        self._emit_finished(geometry)

    def _on_shape_canceled(self) -> None:
        try:
            self._safe_unset_tool()
        except Exception:  # noqa: BLE001
            pass

    def cleanup(self) -> None:
        try:
            self._tool.shapeFinished.disconnect(self._on_shape_finished)
            self._tool.shapeCanceled.disconnect(self._on_shape_canceled)
        except Exception:  # noqa: BLE001
            pass
        super().cleanup()
