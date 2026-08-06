"""绘图管理器（UI 层使用的 facade）。

UI 层（工具条、菜单）只需要：

- 调 :meth:`enable_draw_tool` 激活某个工具
- 调 :meth:`start_edit` / :meth:`stop_edit` / :meth:`save_edit` 控制图层编辑
- 订阅 :attr:`finished` 信号，拿到 :class:`QgsGeometry` 后做属性编辑

旧 :class:`component.draw_tool.Draw` 把所有 5 个工具的菜单 + 信号都放
一个类里，新 :class:`DrawManager` 拆成"业务" :class:`DrawingService` +
"UI 装配" :class:`DrawManager`。业务层不需要 import QMenu/QToolBar。
"""

from __future__ import annotations

from typing import TYPE_CHECKING, List, Optional

if TYPE_CHECKING:
    from qgis.core import QgsGeometry, QgsVectorLayer
    from qgis.gui import QgsMapCanvas, QgsRubberBand

    from .tools.qgis.base import BaseQgsDrawTool

from PyQt5.QtCore import QObject, pyqtSignal
from qgis.core import QgsWkbTypes
from qgis.gui import QgsRubberBand
from PyQt5.QtCore import Qt

from .service import ActiveDraw, DrawingService


class DrawManager(QObject):
    """绘图管理器，UI 层直接使用。"""

    #: 几何采集完成时发出
    finished = pyqtSignal(object)
    #: 取消时发出
    cancelled = pyqtSignal()

    def __init__(
        self,
        canvas: "QgsMapCanvas",
        editing_layer_getter=None,
    ) -> None:
        super().__init__()
        self.canvas = canvas
        # callable returning the current editing layer (or None)
        self._editing_layer_getter = editing_layer_getter
        self._active: Optional[ActiveDraw] = None
        self._service = DrawingService()
        self._active_tool = None
        self._temp_points: List = []
        # 共享 rubber_band（多边形 / 矩形 / 圆需要）
        self._shared_rubber_band: Optional["QgsRubberBand"] = None

    # ------------------------------------------------------------------ 工具

    def enable_draw_tool(self, geometry_type: str, *, is_buffer: bool = False, distance: float = 0.0) -> ActiveDraw:
        """激活指定类型的自定义绘制工具。

        与 :class:`DrawingService.start` 区别：本方法传入 ``draw_manager=self``，
        让工具能用共享 rubber_band 和 temp_points 列表。
        """
        from .tools import get_tool_class

        if self._active is not None:
            self.clear_draw_tool()

        cls = get_tool_class(geometry_type)
        # 注入 rubber_band 与 temp_points：工具类的 __init__ 接受 draw_manager 参数
        tool = cls(
            self.canvas,
            draw_manager=self,
            is_buffer=is_buffer,
            distance=distance,
        )
        self._active_tool = tool
        self.canvas.setMapTool(tool)
        self._active = ActiveDraw(tool)
        self._active.finished.connect(self._on_finished)
        self._active.cancelled.connect(self._on_cancelled)
        return self._active

    def enable_qgis_draw_tool(
        self,
        geometry_type: str,
        *,
        is_buffer: bool = False,
        distance: float = 0.0,
    ) -> "BaseQgsDrawTool":
        """激活指定类型的 **QGIS 复用版** 绘制工具。

        与 :meth:`enable_draw_tool` 的区别：

        - 本方法走 :mod:`app.core.domain.drawing.tools.qgis` 的内置工具，
          自动获得 QGIS 的捕捉 / 撤销 / 高级数字化能力。
        - 返回 :class:`BaseQgsDrawTool` 包装器（不是 :class:`ActiveDraw`）；
          其 ``tool`` 属性才是真正的 :class:`QgsMapTool`。
        - 仍然支持 ``is_buffer`` / ``distance`` 参数（在 finished 后处理）。
        """
        from .tools.qgis import get_qgis_tool_class

        if self._active is not None or self._active_tool is not None:
            self.clear_draw_tool()

        cls = get_qgis_tool_class(geometry_type)
        wrapper = cls(self.canvas, is_buffer=is_buffer, distance=distance)
        self._active_tool = wrapper
        # QGIS 内置工具才是真正 setMapTool 的对象
        self.canvas.setMapTool(wrapper.tool)
        # 包装器的 finished 信号直接透传到 manager.finished
        wrapper.finished.connect(self.finished)
        return wrapper

    def clear_draw_tool(self) -> None:
        """清掉当前激活的绘制工具。

        同时兼容两类工具：

        - **自定义版**（:class:`BaseDrawTool`）：``_active_tool`` 本身就是
          :class:`QgsMapTool`，可以直接 ``unsetMapTool``。
        - **QGIS 复用版**（:class:`BaseQgsDrawTool`）：``_active_tool`` 是 Python
          wrapper，需要走它的 ``tool`` 属性拿到真正的 :class:`QgsMapTool`。
        """
        if self._active_tool is not None:
            try:
                if hasattr(self._active_tool, "cleanup"):
                    self._active_tool.cleanup()
            except Exception:  # noqa: BLE001
                pass
            # wrapper.cleanup() 已经走 _safe_unset_tool，重复调用无害；
            # 这里再保护性调一次，确保任何清理路径都被覆盖
            try:
                real_tool = getattr(self._active_tool, "tool", self._active_tool)
                self.canvas.unsetMapTool(real_tool)
            except Exception:  # noqa: BLE001
                pass
        self._active_tool = None
        if self._shared_rubber_band is not None:
            self._shared_rubber_band.reset()
        self._temp_points.clear()

    def finish_draw(self, geometry: "QgsGeometry") -> None:
        """绘制工具回调：发出 finished 信号并清理。"""
        self.finished.emit(geometry)
        self.clear_draw_tool()

    def _on_finished(self, geometry) -> None:
        # ActiveDraw.finished 已经透传过来了；这里清理
        pass

    def _on_cancelled(self) -> None:
        self.cancelled.emit()

    # -------------------------------------------------- 共享资源（给工具类用）

    @property
    def temp_points(self) -> List:
        return self._temp_points

    @property
    def rubber_band(self) -> "QgsRubberBand":
        """工具类访问共享 rubber_band（多边形/矩形/圆用）。"""
        if self._shared_rubber_band is None:
            self._shared_rubber_band = QgsRubberBand(self.canvas, QgsWkbTypes.PolygonGeometry)
            self._shared_rubber_band.setColor(Qt.red)
            self._shared_rubber_band.setWidth(2)
            self._shared_rubber_band.setFillColor(Qt.red)
        return self._shared_rubber_band
