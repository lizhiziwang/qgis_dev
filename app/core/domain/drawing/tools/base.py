"""5 种绘制工具的抽象基类。

设计：

- 业务层不依赖 Qt widget 控件（除了 :class:`QgsMapTool`，它本身是 Qt 抽象类，
  没办法）。所以允许本模块 import qgis.gui。
- 几何采集由工具类完成，业务层通过 :class:`ActiveDraw` 拿到 :class:`QgsGeometry`。
- 工具通过 :attr:`finished` 信号通知绘制完成；UI 层订阅后做属性编辑等后处理。

类型注解里 QGIS 类型用 ``TYPE_CHECKING`` 引入，类继承则需要运行时 import
（因为 :class:`QgsMapTool` 必须真实存在），所以模块顶层 import qgis。
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from qgis.core import QgsGeometry, QgsPointXY
    from qgis.gui import QgsMapCanvas, QgsMapTool

from qgis.PyQt.QtCore import pyqtSignal
from qgis.core import QgsWkbTypes
from qgis.gui import QgsMapTool, QgsRubberBand
from qgis.PyQt.QtCore import Qt
from qgis.PyQt.QtGui import QColor

from app.infra.logging import get_logger
from .snap import snapped_match

logger = get_logger(__name__)

#: 磁吸指示器构建失败的哨兵（QgsSnapIndicator 懒创建只尝试一次）
_SNAP_INDICATOR_NONE = object()

#: 每画布复用一个 :class:`QgsSnapIndicator`。它内部的 QgsVertexMarker 挂在画布上
#: （QObject 以画布为 parent），若每个绘制工具实例都 new 一个，工具销毁后标记仍
#: 留在画布上（虽隐藏）——长时间反复绘制会累积隐藏标记，因此按画布共享一个。
_SNAP_INDICATORS: dict = {}


def _snap_indicator_for(canvas):
    """返回画布共享的磁吸指示器（懒建）；当前构建不支持时返回 ``None``。"""
    indicator = _SNAP_INDICATORS.get(canvas)
    if indicator is not None:
        return None if indicator is _SNAP_INDICATOR_NONE else indicator
    try:
        from qgis.gui import QgsSnapIndicator

        indicator = QgsSnapIndicator(canvas)
    except Exception:  # noqa: BLE001 —— 无此组件则静默禁用提示
        indicator = _SNAP_INDICATOR_NONE
    _SNAP_INDICATORS[canvas] = indicator
    return None if indicator is _SNAP_INDICATOR_NONE else indicator


class BaseDrawTool(QgsMapTool):
    """5 种 DrawTool 的基类。

    除几何采集外，本基类统一提供**磁吸预览**：自定义工具取点不经过 QGIS 原生
    capture 的吸附管线，因此吸附命中标记（QGIS 桌面/ArcMap 里那种「吸到边/吸
    到顶点」的实时图标提示）原本不显示。这里懒建一个 :class:`QgsSnapIndicator`
    （画布上由 QGIS 自己画顶点/边两套不同标记），工具每次取点（鼠标移动预览 /
    落点）都会把吸附 match 喂给它——吸到**顶点**与吸到**边**显示不同图标，
    吸不到就隐藏，用户能直观看到当前这笔是否会吸、吸到的是边还是顶点。
    """

    #: 几何采集完成时发出，参数是采集到的 :class:`QgsGeometry`。
    finished = pyqtSignal(object)

    def __init__(self, canvas: "QgsMapCanvas", *, is_buffer: bool = False, distance: float = 0.0) -> None:
        super().__init__(canvas)
        self.canvas = canvas
        self.is_buffer = is_buffer
        self.distance = float(distance)
        self.geom = None  # type: ignore[assignment]
        #: 最近一次取点的吸附 match（:class:`QgsPointLocator.Match` 或 None），
        #: 追踪（:meth:`traced_vertex`）用来判断该顶点是否吸到已有要素
        self._last_snap_match = None
        #: 上一个已落顶点是否吸到已有要素（追踪起点资格）
        self._prev_vertex_snapped = False

        # 磁吸预览（指示器按画布共享，见 _snap_indicator_for；无实例字段）

        # 缓冲预览（仅在 is_buffer=True 时启用）
        self.rubber_band_buffer = QgsRubberBand(canvas, QgsWkbTypes.PolygonGeometry)
        self.rubber_band_buffer.setColor(Qt.GlobalColor.red)
        self.rubber_band_buffer.setWidth(2)
        self.rubber_band_buffer.setLineStyle(Qt.PenStyle.DashLine)

    # ---------------------------------------------------------- 磁吸预览

    def _snap_indicator_widget(self):
        """画布共享的 :class:`QgsSnapIndicator`；构建不支持时返回 ``None``。"""
        return _snap_indicator_for(self.canvas)

    def _set_snap_match(self, match) -> None:
        """把吸附命中的 match 同步给磁吸指示器（顶点/边不同图标）。

        :param match: :class:`QgsPointLocator.Match`（或 None）
        """
        widget = self._snap_indicator_widget()
        if widget is None:
            return
        try:
            if match is not None and match.isValid():
                widget.setMatch(match)
                widget.setVisible(True)
            else:
                # 无效 match / 未吸到 → 隐藏，表示「当前没吸上」
                widget.setVisible(False)
        except Exception:  # noqa: BLE001
            pass

    def hide_snap_indicator(self) -> None:
        """隐藏磁吸指示器（工具停用 / 绘制完成时收起标记）。"""
        self._set_snap_match(None)

    def snapped_point(self, event, *, wait_index: bool = True) -> "QgsPointXY":
        """吸附取点并同步更新磁吸指示器。

        与模块级 ``snapped_map_point`` 语义一致（命中用吸附点、否则回退原地图
        坐标），额外在鼠标移动时实时反馈是否吸到、吸到顶点还是边。绘制工具的
        move/press 统一走这里，不要直接调模块级 ``snapped_map_point``。
        """
        match = snapped_match(self.canvas, event, wait_index=wait_index)
        self._set_snap_match(match)
        self._last_snap_match = match
        if match is not None:
            return match.point()
        return self.canvas.getCoordinateTransform().toMapCoordinates(event.pixelPoint())

    def traced_vertex(self, point: "QgsPointXY") -> "list[QgsPointXY]":
        """追踪钩子：落顶点前调用，返回应插在本顶点之前的追踪中间点。

        语义与 QGIS 桌面追踪一致：**上一个顶点**与本顶点都吸附到已有要素时，
        两点间沿已有要素几何的最短路径自动采进几何（见
        :mod:`app.core.domain.drawing.tools.trace`）。任一端未吸附 / 追踪未
        开启 / 无路径 → 返回空（降级为普通取点）。

        调用顺序约定（线 / 面工具左键落点）::

            point = self.snapped_point(event)
            for p in self.traced_vertex(point):
                self.temp_points.append(p)
            self.temp_points.append(point)

        本方法内部同时把「本顶点是否吸附」登记为下一个顶点的追踪起点资格。
        """
        extension: "list[QgsPointXY]" = []
        snapped_now = (
            self._last_snap_match is not None
            and self._last_snap_match.isValid()
        )
        if (
            snapped_now
            and self._prev_vertex_snapped
            and self.temp_points
        ):
            try:
                from app.core.domain.drawing.tools.trace import trace_between

                extension = trace_between(self.canvas, self.temp_points[-1], point)
            except Exception:  # noqa: BLE001 —— 追踪失败降级为不插点
                logger.exception("追踪取点失败")
                extension = []
        self._prev_vertex_snapped = snapped_now
        return extension

    def commit_geometry(self, geometry) -> None:
        """一笔绘制完成：先收起磁吸标记，再按是否挂 draw_manager 派发。

        工具挂 draw_manager（DigitizingToolbar / DrawManager 路径）时走
        ``draw_manager.finish_draw`` —— 该路径**不会**卸载/清理工具实例（工具为
        连续绘制保留）。若不在提交点先收起磁吸标记，指示器会一直停在最后一个
        落点上（「绘制完捕捉标记不消失」）。落点/右键完成几何的代码统一走这里，
        不要直接调 ``draw_manager.finish_draw``。
        """
        self.hide_snap_indicator()
        if self.draw_manager is not None:
            self.draw_manager.finish_draw(geometry)
        else:
            self.temp_points.clear()
            self.finish_draw()

    def deactivate(self) -> None:
        """工具被停用（结束 / 被平移等工具替换）→ 收起磁吸标记。"""
        try:
            self.hide_snap_indicator()
        except Exception:  # noqa: BLE001
            logger.exception("绘制工具 %s 收起磁吸标记失败", type(self).__name__)
        super().deactivate()

    def finish_draw(self) -> None:
        """工具内部调用：发出 finished 信号。"""
        try:
            self.hide_snap_indicator()
            self.canvas.unsetMapTool(self)
            if self.is_buffer and self.geom is not None and not self.geom.isEmpty():
                buffered = self.geom.buffer(self.distance, 16)
                self.rubber_band_buffer.addGeometry(buffered)
            self.finished.emit(self.geom)
        except Exception:  # noqa: BLE001
            logger.exception("绘制工具 %s 完成回调失败", type(self).__name__)

    def cleanup(self) -> None:
        """清理资源（rubber_band、临时点等）。子类按需重写。"""
        try:
            self.hide_snap_indicator()
        except Exception:  # noqa: BLE001
            logger.exception("绘制工具 %s cleanup 收起磁吸标记失败", type(self).__name__)
        try:
            self.rubber_band_buffer.hide()
        except Exception:  # noqa: BLE001
            logger.exception("绘制工具 %s cleanup 隐藏缓冲橡皮筋失败", type(self).__name__)

