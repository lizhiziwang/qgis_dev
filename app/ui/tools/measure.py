"""线测量 / 面测量地图工具。

侧栏「线测量」「面测量」按钮激活的交互工具：在地图上连续点击落点，
**线测量**累计折线长度，**面测量**累计多边形面积（同时给出周长）。

交互（对齐 QGIS 桌面「测量」工具）：

- 左键落点；移动鼠标时实时显示「当前累计值」+ 预览虚线段；
- 右键结束当前测量（图形与结果保留在图上），再左键即开始新的一次测量；
- ``Backspace`` 撤销上一个落点，``Esc`` 清除全部（图形 + 结果）；
- 结果用悬浮在画布底部的读数条 :class:`MeasureReadout` 显示。

量算口径：用 :class:`QgsDistanceArea`（源 CRS = 画布 CRS、椭球取该 CRS 椭球）
做 **椭球量算**，得到米 / 平方米，再按中国人习惯的档位显示为
米 / 千米与 平方米 / 公顷 / 平方千米。与质检模块的椭球量算口径一致
（见 :mod:`app.core.domain.check.builtin.measure`）。

落点走 :func:`app.core.domain.drawing.tools.snap.snapped_map_point`，与绘制
工具同一套磁吸：开启吸附时会自动吸到已有要素的顶点 / 边。

.. note::
   本工程使用的 QGIS 构建（4.2）已无 ``QgsMeasureTool``（``qgis.gui`` 里
   不存在该类），故在此自实现；即便换回仍有该类的 3.40 构建，本实现同样可用。
"""

from __future__ import annotations

import contextlib
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from qgis.gui import QgsMapCanvas, QgsMapMouseEvent
    from qgis.PyQt.QtGui import QKeyEvent

from qgis.core import (
    Qgis,
    QgsDistanceArea,
    QgsGeometry,
    QgsPointXY,
    QgsProject,
    QgsUnitTypes,
    QgsWkbTypes,
)
from qgis.gui import QgsMapTool, QgsRubberBand
from qgis.PyQt.QtCore import Qt, QTimer
from qgis.PyQt.QtGui import QColor
from qgis.PyQt.QtWidgets import QGraphicsDropShadowEffect, QLabel

from app.core.domain.drawing.tools.snap import snapped_map_point
from app.infra.logging import get_logger
from app.ui.common.theme import ThemeManager

logger = get_logger(__name__)

#: 主图形颜色（已落点连成的线 / 面）
_MAIN_COLOR = QColor(220, 40, 40)
#: 面填充色（半透明，避免压住底图）
_FILL_COLOR = QColor(220, 40, 40, 45)
#: 预览虚线颜色
_PREVIEW_COLOR = QColor(220, 40, 40, 190)
#: 读数条距画布底边的高度（像素）
_READOUT_BOTTOM_GAP = 56


def _format_length(meters: float) -> str:
    """米 → 「米 / 千米」档位（>=1km 用千米）。"""
    if meters >= 1000.0:
        return f"{meters / 1000.0:.3f} 千米"
    return f"{meters:.2f} 米"


def _format_area(square_meters: float) -> str:
    """平方米 → 「平方米 / 公顷 / 平方千米」档位。"""
    if square_meters >= 1_000_000.0:
        return f"{square_meters / 1_000_000.0:.4f} 平方千米"
    if square_meters >= 10_000.0:
        return f"{square_meters / 10_000.0:.4f} 公顷"
    return f"{square_meters:.2f} 平方米"


class MeasureReadout(QLabel):
    """画布底部的测量读数条：浮在地图上，随测量实时刷新。

    作为画布的子控件（仓库既定覆盖层模式，见
    :class:`app.ui.common.canvas_zoom.CanvasZoomTool`），父窗口 resize 时靠
    ``mapCanvasRefreshed`` 重新居中。设 ``WA_TransparentForMouseEvents``，让
    鼠标事件穿透到画布——读数条盖在地图上时不能吞掉落点。
    """

    def __init__(self, canvas: QgsMapCanvas) -> None:
        super().__init__(canvas)
        self._canvas = canvas
        self.setObjectName("MeasureReadout")
        self.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, True)
        self.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.hide()
        self._apply_style()
        ThemeManager.instance().theme_changed.connect(self._on_theme_changed)
        canvas.mapCanvasRefreshed.connect(self._reposition)

    def _on_theme_changed(self, _theme=None) -> None:
        self._apply_style()
        self._reposition()

    def _apply_style(self) -> None:
        theme = ThemeManager.instance().current()
        self.setStyleSheet(
            f"""
            QLabel#MeasureReadout {{
                background-color: {theme.input_bg};
                color: {theme.menu_text};
                border: 1px solid {theme.input_border};
                border-radius: 6px;
                padding: 6px 14px;
                font-weight: 600;
            }}
            """
        )
        # 柔和投影：让读数条浮在地图上（与 CanvasZoomTool / 坐标定位气泡同款）
        if self.graphicsEffect() is None:
            shadow = QGraphicsDropShadowEffect(self)
            shadow.setBlurRadius(12)
            shadow.setOffset(0, 2)
            shadow.setColor(QColor(0, 0, 0, 80))
            self.setGraphicsEffect(shadow)

    def show_text(self, text: str) -> None:
        """刷新读数并显示（按内容自动调整宽高后重新定位）。"""
        self.setText(text)
        self.ensurePolished()
        self.adjustSize()
        self._reposition()
        self.show()
        self.raise_()

    def hide_readout(self) -> None:
        """收起读数条（不清空文本，便于下次直接复用）。"""
        self.hide()

    def _reposition(self) -> None:
        """居中贴底；画布尺寸变化后由 ``mapCanvasRefreshed`` 触发校正。"""
        try:
            x = max((self._canvas.width() - self.width()) // 2, 0)
            y = max(self._canvas.height() - self.height() - _READOUT_BOTTOM_GAP, 0)
            self.move(x, y)
        except RuntimeError:  # 画布 C++ 对象已销毁
            pass


class MeasureToolBase(QgsMapTool):
    """线 / 面测量公共逻辑（子类只需给出几何类型与结果文案）。"""

    #: 读数条前缀（如「线测量」）
    _LABEL = "测量"

    def __init__(self, canvas: QgsMapCanvas) -> None:
        super().__init__(canvas)
        self.canvas = canvas
        self._points: list[QgsPointXY] = []
        #: 上一次测量是否已结束（右键）——结束态停在图上，左键开始新的一次
        self._finished = False
        self.setCursor(Qt.CursorShape.CrossCursor)

        self._measure = self._make_measure()
        self._readout = MeasureReadout(canvas)

        # 已落点连成的图形（线 / 面）
        self._band = QgsRubberBand(canvas, self._band_type())
        self._band.setColor(_MAIN_COLOR)
        self._band.setWidth(2)
        self._band.setFillColor(_FILL_COLOR)

        # 鼠标移动时的预览虚线（末点 → 光标；面额外画首点 → 光标的闭合边）
        self._preview = QgsRubberBand(canvas, QgsWkbTypes.LineGeometry)
        self._preview.setColor(_PREVIEW_COLOR)
        self._preview.setWidth(2)
        self._preview.setLineStyle(Qt.PenStyle.DashLine)

    # ------------------------------------------------------------ 子类接口

    def _band_type(self) -> QgsWkbTypes.GeometryType:
        """主橡皮筋几何类型（线 / 面）。"""
        raise NotImplementedError

    def _min_points(self) -> int:
        """成图形所需最少点数。"""
        raise NotImplementedError

    def _hint_text(self) -> str:
        """点数还不够时的提示文案。"""
        raise NotImplementedError

    def _result_text(self, points: list[QgsPointXY]) -> str:
        """按点数给出量算结果文案（调用时已保证点数足够）。"""
        raise NotImplementedError

    # ------------------------------------------------------------ 事件

    def canvasPressEvent(self, event: QgsMapMouseEvent) -> None:
        if event.button() == Qt.MouseButton.LeftButton:
            self._add_point(event)
        elif event.button() == Qt.MouseButton.RightButton:
            self._finish()

    def canvasMoveEvent(self, event: QgsMapMouseEvent) -> None:
        point = self._map_point(event, wait_index=False)
        self._update_preview(point)

    def keyPressEvent(self, event: QKeyEvent) -> None:
        key = event.key()
        if key == Qt.Key.Key_Escape:
            self.clear()
            return
        if key == Qt.Key.Key_Backspace:
            self._undo_last()
            return
        super().keyPressEvent(event)

    def activate(self) -> None:
        """激活：清空上一次的图形与读数，从干净状态开始。"""
        super().activate()
        self.clear()

    def deactivate(self) -> None:
        """停用：收起预览与读数条（已画出的图形保留在图上）。"""
        try:
            self._preview.hide()
            self._readout.hide_readout()
        except RuntimeError:
            pass
        super().deactivate()

    # ------------------------------------------------------------ 交互

    def _add_point(self, event: QgsMapMouseEvent) -> None:
        # 上一笔已结束 → 本次左键开新的一笔
        if self._finished:
            self._reset_measurement()
        point = self._map_point(event, wait_index=True)
        if self._points and self._points[-1] == point:
            # 同一处重复落点无意义（例如双击的第二下），忽略
            return
        self._points.append(QgsPointXY(point))
        self._update_band()
        self._refresh_readout()

    def _undo_last(self) -> None:
        if self._finished or not self._points:
            return
        self._points.pop()
        self._update_band()
        self._preview.hide()
        self._refresh_readout()

    def _finish(self) -> None:
        """右键结束当前测量：保留图形与结果，等下一次左键开新的一笔。"""
        if self._finished:
            return
        if len(self._points) < self._min_points():
            # 点数不足，成不了图形 → 直接清掉
            self.clear()
            return
        self._finished = True
        with contextlib.suppress(RuntimeError):
            self._preview.hide()
        self._update_band()
        self._refresh_readout()
        # 不在鼠标事件栈里直接 refresh（仓库约定：见 app/ui/tools/qmap_compare.py）
        QTimer.singleShot(0, self._safe_refresh)

    def clear(self) -> None:
        """清除全部：落点、图形、读数条。"""
        self._points.clear()
        self._finished = False
        try:
            self._band.reset(self._band_type())
            self._preview.reset(QgsWkbTypes.LineGeometry)
        except RuntimeError:
            pass
        self._readout.hide_readout()

    def _reset_measurement(self) -> None:
        """结束态下开始新的一笔：清图形与读数，但保留「已结束」以外的状态。"""
        self.clear()

    def _safe_refresh(self) -> None:
        with contextlib.suppress(RuntimeError):
            self.canvas.refresh()

    # ------------------------------------------------------------ 渲染

    def _update_band(self) -> None:
        """按当前落点重画主图形。"""
        try:
            self._band.reset(self._band_type())
            for point in self._points:
                self._band.addPoint(point)
            if self._points:
                self._band.show()
            else:
                self._band.hide()
        except RuntimeError:
            pass

    def _update_preview(self, point: QgsPointXY) -> None:
        """鼠标移动：末点 → 光标的预览虚线（面另加首点 → 光标的闭合边）。"""
        if self._finished or not self._points:
            with contextlib.suppress(RuntimeError):
                self._preview.hide()
            return
        try:
            self._preview.reset(QgsWkbTypes.LineGeometry)
            self._preview.addPoint(self._points[-1])
            self._preview.addPoint(point)
            if self._band_type() == QgsWkbTypes.PolygonGeometry:
                # 面的「合口」虚线：首点 → 光标（接在上一条之后，故只再加首点）
                self._preview.addPoint(self._points[0])
            self._preview.show()
        except RuntimeError:
            return
        self._refresh_readout(preview_point=point)

    def _refresh_readout(self, preview_point: QgsPointXY | None = None) -> None:
        """刷新读数条：含预览点时为「实时值」，否则为「已落点值」。"""
        if not self._points:
            self._readout.hide_readout()
            return
        if self._finished:
            self._readout.show_text(self._result_text(self._points))
            return
        if preview_point is not None and len(self._points) + 1 >= self._min_points():
            points = [*self._points, preview_point]
        else:
            points = list(self._points)
        if len(points) < self._min_points():
            self._readout.show_text(self._hint_text())
            return
        self._readout.show_text(self._result_text(points))

    # ------------------------------------------------------------ 量算

    def _map_point(self, event: QgsMapMouseEvent, *, wait_index: bool) -> QgsPointXY:
        """事件 → 地图点（走与绘制工具同一套磁吸）。"""
        return snapped_map_point(self.canvas, event, wait_index=wait_index)

    def _make_measure(self) -> QgsDistanceArea | None:
        """构造椭球量算器：源 CRS = 画布 CRS，椭球取该 CRS 椭球。"""
        crs = self.canvas.mapSettings().destinationCrs()
        if crs is None or not crs.isValid():
            return None
        measure = QgsDistanceArea()
        try:
            measure.setSourceCrs(crs, QgsProject.instance().transformContext())
        except Exception:
            logger.warning("测量工具构造 CRS 失败，降级为平面量算", exc_info=True)
            return None
        ellipsoid = crs.ellipsoidAcronym()
        if ellipsoid:
            measure.setEllipsoid(ellipsoid)
        return measure

    def _ellipsoid_measure(self) -> QgsDistanceArea | None:
        """可用的椭球量算器；未启用椭球时返回 None（调用方降级为平面量算）。"""
        measure = self._measure
        if measure is None or not measure.willUseEllipsoid():
            return None
        return measure

    @staticmethod
    def _length_to_meters(measure: QgsDistanceArea, value: float) -> float:
        """按量算器当前长度单位换算成米。"""
        return value * QgsUnitTypes.fromUnitToUnitFactor(measure.lengthUnits(), Qgis.DistanceUnit.Meters)

    def _length_meters(self, geometry: QgsGeometry) -> float | None:
        """折线长度（米）；无法椭球量算时返回 None。"""
        measure = self._ellipsoid_measure()
        if measure is None:
            return None
        try:
            return self._length_to_meters(measure, measure.measureLength(geometry))
        except Exception:
            logger.debug("椭球长度量算失败，降级为平面", exc_info=True)
            return None

    def _perimeter_meters(self, geometry: QgsGeometry) -> float | None:
        """多边形周长（米）；无法椭球量算时返回 None。

        注意面必须用 ``measurePerimeter``：实测 QGIS 4.2 下 ``measureLength``
        对多边形几何直接返回 0（周长会显示成 0.00 米）。
        """
        measure = self._ellipsoid_measure()
        if measure is None:
            return None
        try:
            return self._length_to_meters(measure, measure.measurePerimeter(geometry))
        except Exception:
            logger.debug("椭球周长度量失败，降级为平面", exc_info=True)
            return None

    def _area_square_meters(self, geometry: QgsGeometry) -> float | None:
        """多边形面积（平方米）；无法椭球量算时返回 None。"""
        measure = self._ellipsoid_measure()
        if measure is None:
            return None
        try:
            value = measure.measureArea(geometry)
        except Exception:
            logger.debug("椭球面积量算失败，降级为平面", exc_info=True)
            return None
        return value * QgsUnitTypes.fromUnitToUnitFactor(measure.areaUnits(), Qgis.AreaUnit.SquareMeters)

    def _planar_unit(self) -> str:
        """平面量算降级时的单位后缀（CRS 自身单位）。"""
        try:
            unit = self.canvas.mapSettings().destinationCrs().mapUnits()
            return QgsUnitTypes.toAbbreviatedString(unit)
        except Exception:  # noqa: BLE001
            return ""


class MeasureLengthTool(MeasureToolBase):
    """线测量：连续点击落点，累计折线长度。"""

    _LABEL = "线测量"

    def _band_type(self) -> QgsWkbTypes.GeometryType:
        return QgsWkbTypes.LineGeometry

    def _min_points(self) -> int:
        return 2

    def _hint_text(self) -> str:
        return f"{self._LABEL}：左键落点，右键结束"

    def _result_text(self, points: list[QgsPointXY]) -> str:
        geometry = QgsGeometry.fromPolylineXY([QgsPointXY(p) for p in points])
        meters = self._length_meters(geometry)
        if meters is None:
            return f"长度：{geometry.length():.2f} {self._planar_unit()}".rstrip()
        return f"长度：{_format_length(meters)}"


class MeasureAreaTool(MeasureToolBase):
    """面测量：连续点击落点，累计多边形面积（同时给出周长）。"""

    _LABEL = "面测量"

    def _band_type(self) -> QgsWkbTypes.GeometryType:
        return QgsWkbTypes.PolygonGeometry

    def _min_points(self) -> int:
        return 3

    def _hint_text(self) -> str:
        return f"{self._LABEL}：至少 3 个点，右键结束"

    def _result_text(self, points: list[QgsPointXY]) -> str:
        geometry = QgsGeometry.fromPolygonXY([[QgsPointXY(p) for p in points]])
        area = self._area_square_meters(geometry)
        perimeter = self._perimeter_meters(geometry)
        if area is None or perimeter is None:
            unit = self._planar_unit()
            return f"面积：{geometry.area():.2f} {unit}²　周长：{geometry.length():.2f} {unit}"
        return f"面积：{_format_area(area)}　周长：{_format_length(perimeter)}"
