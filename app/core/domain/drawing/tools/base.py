"""5 种绘制工具的抽象基类。

设计：

- 业务层不依赖 PyQt6 widget（除了 :class:`QgsMapTool`，它本身是 Qt 抽象类，
  没办法）。所以允许本模块 import qgis.gui。
- 几何采集由工具类完成，业务层通过 :class:`ActiveDraw` 拿到 :class:`QgsGeometry`。
- 工具通过 :attr:`finished` 信号通知绘制完成；UI 层订阅后做属性编辑等后处理。

类型注解里 QGIS 类型用 ``TYPE_CHECKING`` 引入，类继承则需要运行时 import
（因为 :class:`QgsMapTool` 必须真实存在），所以模块顶层 import qgis。
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from qgis.core import QgsGeometry
    from qgis.gui import QgsMapCanvas, QgsMapTool

from PyQt6.QtCore import pyqtSignal
from qgis.core import QgsWkbTypes
from qgis.gui import QgsMapTool, QgsRubberBand
from PyQt6.QtCore import Qt
from PyQt6.QtGui import QColor


class BaseDrawTool(QgsMapTool):
    """5 种 DrawTool 的基类。"""

    #: 几何采集完成时发出，参数是采集到的 :class:`QgsGeometry`。
    finished = pyqtSignal(object)

    def __init__(self, canvas: "QgsMapCanvas", *, is_buffer: bool = False, distance: float = 0.0) -> None:
        super().__init__(canvas)
        self.canvas = canvas
        self.is_buffer = is_buffer
        self.distance = float(distance)
        self.geom = None  # type: ignore[assignment]

        # 缓冲预览（仅在 is_buffer=True 时启用）
        self.rubber_band_buffer = QgsRubberBand(canvas, QgsWkbTypes.PolygonGeometry)
        self.rubber_band_buffer.setColor(Qt.GlobalColor.red)
        self.rubber_band_buffer.setWidth(2)
        self.rubber_band_buffer.setLineStyle(Qt.PenStyle.DashLine)

    def finish_draw(self) -> None:
        """工具内部调用：发出 finished 信号。"""
        try:
            self.canvas.unsetMapTool(self)
            if self.is_buffer and self.geom is not None and not self.geom.isEmpty():
                buffered = self.geom.buffer(self.distance, 16)
                self.rubber_band_buffer.addGeometry(buffered)
            self.finished.emit(self.geom)
        except Exception:  # noqa: BLE001
            pass

    def cleanup(self) -> None:
        """清理资源（rubber_band、临时点等）。子类按需重写。"""
        try:
            self.rubber_band_buffer.hide()
        except Exception:  # noqa: BLE001
            pass

