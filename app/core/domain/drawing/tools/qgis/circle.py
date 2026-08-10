"""QGIS 复用版 - 圆形绘制工具。

QGIS 4 移除了 :class:`qgis.gui.QgsMapToolShapeCircle`（探针确认），
本类回退到自定义实现 :class:`app.core.domain.drawing.tools.circle.CircleDrawTool`：

- 左键点圆心 → 拖动预览半径 → 右键结束，``finished`` 信号携带圆形几何
- 保持 :class:`BaseQgsDrawTool` 接口（``tool`` 属性 / ``finished`` / ``cleanup``），
  ``DrawManager.enable_qgis_draw_tool`` 无需改动
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from qgis.core import QgsGeometry
    from qgis.gui import QgsMapCanvas

from .base import BaseQgsDrawTool
from app.core.domain.drawing.tools.circle import CircleDrawTool


class QGISCircleDrawTool(BaseQgsDrawTool):
    """圆心→半径绘制圆（QGIS 4 回退到自定义实现）。"""

    def __init__(
        self,
        canvas: "QgsMapCanvas",
        *,
        is_buffer: bool = False,
        distance: float = 0.0,
    ) -> None:
        BaseQgsDrawTool.__init__(self, canvas, is_buffer=is_buffer, distance=distance)
        # 自定义工具自身就是 QgsMapTool 子类；draw_manager=None 时自建 rubber_band
        self._inner = CircleDrawTool(
            canvas,
            draw_manager=None,
            is_buffer=is_buffer,
            distance=distance,
        )
        # 自定义工具的 finished 信号透传到 wrapper 的统一信号
        self._inner.finished.connect(self.finished)
        self._tool = self._inner

    @property
    def tool(self):  # noqa: ANN201
        return self._inner

    def cleanup(self) -> None:
        try:
            self._inner.finished.disconnect(self.finished)
        except Exception:  # noqa: BLE001
            pass
        try:
            self._inner.cleanup()
        except Exception:  # noqa: BLE001
            pass
        super().cleanup()
