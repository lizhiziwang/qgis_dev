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
    from qgis.core import QgsGeometry
    from qgis.gui import QgsMapCanvas, QgsRubberBand

from qgis.core import QgsWkbTypes
from qgis.gui import QgsRubberBand
from qgis.PyQt.QtCore import QObject, Qt, pyqtSignal

from app.infra.logging import get_logger

from .service import ActiveDraw, DrawingService
from .tools import get_tool_class

logger = get_logger(__name__)


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

    def clear_draw_tool(self) -> None:
        """清掉当前激活的绘制工具，并清掉绘制预览（临时图形）。"""
        self._detach_tool()
        self.clear_preview()

    def _detach_tool(self) -> None:
        """卸下画布上的绘制工具 + 清临时点，**保留**共享 rubber_band 里的预览。

        与 :meth:`clear_preview` 分开是为了「绘制完成 → 属性弹窗关闭」这段：
        那期间工具不该再响应画布交互，但刚画的那一笔要留在地图上。
        """
        if self._active_tool is not None:
            try:
                if hasattr(self._active_tool, "cleanup"):
                    self._active_tool.cleanup()
            except Exception:  # noqa: BLE001
                logger.exception("清理绘制工具 %s 的 cleanup 失败", type(self._active_tool).__name__)
            # 保护性 unset：_active_tool 可能是带 .tool 属性的 wrapper
            #（历史兼容），也可能就是 QgsMapTool 本身
            try:
                real_tool = getattr(self._active_tool, "tool", self._active_tool)
                self.canvas.unsetMapTool(real_tool)
            except Exception:  # noqa: BLE001
                logger.exception("从画布卸下绘制工具 %s 失败", type(self._active_tool).__name__)
        self._active_tool = None
        self._temp_points.clear()

    def clear_preview(self) -> None:
        """清掉共享 rubber_band 里的临时图形。

        绘制完成到属性弹窗关闭之间**不要**调它——临时图形要留在地图上给用户
        对照着填属性（见 :meth:`finish_draw`）。
        """
        if self._shared_rubber_band is not None:
            self._shared_rubber_band.reset()

    def finish_draw(self, geometry: "QgsGeometry") -> None:
        """绘制工具回调：发出 finished 信号并卸下工具。

        **不在这里清预览**：``finished`` 的订阅方（工具条）会弹属性对话框，刚画的
        那一笔要一直留到那个对话框关闭为止——清早了用户填属性时就看不到自己画的
        是哪一块。UI 层在对话框关闭时调 :meth:`clear_preview`。
        """
        self.finished.emit(geometry)
        self._detach_tool()

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
            self._shared_rubber_band.setColor(Qt.GlobalColor.red)
            self._shared_rubber_band.setWidth(2)
            self._shared_rubber_band.setFillColor(Qt.GlobalColor.red)
        return self._shared_rubber_band
