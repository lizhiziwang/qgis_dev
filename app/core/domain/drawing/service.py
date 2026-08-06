"""绘图服务。

提供 :class:`DrawingService` 与 :class:`DrawRequest` 数据类，业务层调用
:meth:`DrawingService.start` 即可激活一个绘制工具，工具完成时通过
:class:`ActiveDraw.finished` 信号返回 :class:`QgsGeometry`。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Optional

if TYPE_CHECKING:
    from qgis.core import QgsGeometry, QgsVectorLayer
    from qgis.gui import QgsMapCanvas, QgsMapTool

from PyQt5.QtCore import QObject, pyqtSignal

from .tools import get_tool_class


@dataclass(frozen=True)
class DrawRequest:
    """激活一次绘制所需参数。"""

    canvas: "QgsMapCanvas"
    geometry_type: str  # "point" | "line" | "polygon" | "rectangle" | "circle"
    is_buffer: bool = False
    distance: float = 0.0


class ActiveDraw(QObject):
    """一次激活的绘制会话。

    业务层在 :attr:`finished` 信号中拿到采集到的 :class:`QgsGeometry`；
    调用 :meth:`cancel` 主动放弃。
    """

    finished = pyqtSignal(object)  # 发出 :class:`QgsGeometry` 或 None
    cancelled = pyqtSignal()

    def __init__(self, tool: "QgsMapTool") -> None:
        super().__init__()
        self.tool = tool
        # 把工具的 ``finished`` 信号转发到本 ActiveDraw
        tool.finished.connect(self.finished.emit)

    def cancel(self) -> None:
        """主动取消：清理工具并取消激活。"""
        try:
            if hasattr(self.tool, "cleanup"):
                self.tool.cleanup()
            self.tool.canvas().unsetMapTool(self.tool)
        except Exception:  # noqa: BLE001
            pass
        self.cancelled.emit()


class DrawingService:
    """绘图服务门面。"""

    def __init__(self) -> None:
        # 把工具类已 import
        from . import tools  # noqa: F401

    def start(self, request: DrawRequest) -> ActiveDraw:
        """激活一个绘制工具。"""
        cls = get_tool_class(request.geometry_type)
        tool = cls(
            request.canvas,
            is_buffer=request.is_buffer,
            distance=request.distance,
        )
        # 多边形/矩形/圆需要共享 rubber_band 才能画出填充；这里创建新工具时不传 draw_manager
        # 由 UI 层 wrap 到 DrawManager 时再传
        request.canvas.setMapTool(tool)
        return ActiveDraw(tool)

    def supported_types(self) -> list[str]:
        """返回所有支持的几何类型。"""
        from .tools import TOOL_REGISTRY

        return list(TOOL_REGISTRY.keys())
