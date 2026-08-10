"""QGIS 复用版 - 点绘制工具。

基于 :class:`qgis.gui.QgsMapToolEmitPoint`：

- 点击画布 → ``canvasClicked(point, button)`` 信号
- 我们订阅该信号，把 ``point`` 包成 :class:`QgsGeometry` 后 emit ``finished``

实现：单继承 ``BaseQgsDrawTool``（QObject），内部持有
``QgsMapToolEmitPoint`` 实例（``tool`` 属性暴露给 :class:`DrawManager` 激活）。
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from qgis.core import QgsGeometry
    from qgis.gui import QgsMapCanvas

from PyQt6.QtCore import Qt
from qgis.core import QgsGeometry
from qgis.gui import QgsMapToolEmitPoint

from .base import BaseQgsDrawTool


class QGISPointDrawTool(BaseQgsDrawTool):
    """点击画布取点（QGIS 内置版本）。"""

    def __init__(
        self,
        canvas: "QgsMapCanvas",
        *,
        is_buffer: bool = False,
        distance: float = 0.0,
    ) -> None:
        super().__init__(canvas, is_buffer=is_buffer, distance=distance)
        self._tool = QgsMapToolEmitPoint(canvas)
        # QGIS 内置信号 → 我们的统一 finished
        self._tool.canvasClicked.connect(self._on_canvas_clicked)

    @property
    def tool(self):  # noqa: ANN201 — 动态返回 QGIS 工具
        """供 :class:`DrawManager` 调 ``setMapTool`` 用。"""
        return self._tool

    def _on_canvas_clicked(self, point, button) -> None:  # noqa: ANN001
        # 只接受左键（QGIS 工具右键也会触发 canvasClicked）
        if button != Qt.MouseButton.LeftButton:
            return
        # point 已是 QgsPointXY，直接 fromPointXY
        geometry = QgsGeometry.fromPointXY(point)
        self._emit_finished(geometry)

    def cleanup(self) -> None:
        # 先断开信号，避免后续 cleanup 与 QGIS 工具生命周期冲突
        try:
            self._tool.canvasClicked.disconnect(self._on_canvas_clicked)
        except Exception:  # noqa: BLE001
            pass
        super().cleanup()
