"""面分割工具。

编辑工具条「面分割」按钮激活的交互工具：在要分割的要素上**左键画多边形**
作为切割区域（支持磁性捕捉），**双击完成**后，多边形覆盖到的要素被切开：

- **左键单击** → 添加多边形顶点，顶点经画布吸附管线吸附一次（可吸到已有
  要素的顶点 / 边，磁吸标记实时反馈）；
- **鼠标移动** → 半透明多边形预览实时跟手；
- **左键双击** → 完成：闭合多边形并执行分割；
- **右键单击** → 清除已画的多边形（可重新画）；
- **Esc** → 依次：清多边形 → 退出工具。

分割语义：画的多边形是**切割区域**——多边形覆盖的要素部分被切出成为**新
要素**（属性继承：主键清空、FGUID 重新生成、面积 / 周长按新几何重算，其余
照抄，见 :mod:`app.ui.tools.feature_attrs`），原要素保留剩余部分
（``difference``）。一次可同时切多个被覆盖的要素；**未被真正切开**（多边形
没盖住要素 / 完全盖住要素）时提示并清空多边形，可继续画下一个。线 / 面图
层都支持（点图层不参与）。

分割写进编辑缓冲（要求图层处于编辑态）：``beginEditCommand`` 把每个受影响
要素的 ``changeGeometry`` + ``changeAttributeValue``（面积类字段重算）+
``addFeature``（切出部分）合成一个撤销步骤，可整体撤销。几何坐标一律换算
到**图层 CRS** 后再做相交 / 差集运算。

安全约束（仓库既定结论，见 ``swipe.py`` docstring）：不在 Qt 事件栈内调
``canvas.refresh()`` / ``unsetMapTool``，一律 ``QTimer.singleShot(0, ...)``
推迟出栈。
"""

from __future__ import annotations

from collections.abc import Callable

from qgis.core import (
    QgsCoordinateTransform,
    QgsFeature,
    QgsGeometry,
    QgsLineString,
    QgsPoint,
    QgsPointXY,
    QgsPolygon,
    QgsProject,
    QgsVectorLayer,
    QgsWkbTypes,
)
from qgis.gui import QgsMapTool, QgsRubberBand
from qgis.PyQt.QtCore import QPoint, QPointF, Qt, QTimer
from qgis.PyQt.QtGui import QColor

from app.core.domain.check.query import intersect_features
from app.core.domain.check.space_index import clear_spatial_index
from app.core.domain.drawing.tools.base import _snap_indicator_for
from app.core.domain.drawing.tools.snap import snapped_match
from app.infra.logging import get_logger
from app.ui.tools.feature_attrs import new_feature_attributes, refresh_area_fields

logger = get_logger(__name__)


class SplitPolygonTool(QgsMapTool):
    """画多边形切割区域的编辑工具（左键加点 / 双击分割 / 右键清除 / Esc 退出）。"""

    #: 顶点采样最小间距（像素）：连续单击过密会被忽略（防手抖重复点）
    MIN_SPACING_PX = 3

    def __init__(
        self,
        canvas,
        editing_layer_getter: Callable[[], QgsVectorLayer | None] = lambda: None,
        hint: Callable[[str], None] | None = None,
    ) -> None:
        super().__init__(canvas)
        self.canvas = canvas
        self._layer_getter = editing_layer_getter
        self._hint = hint or (lambda text: None)
        #: 当前编辑图层（锁定；切换图层清空未画完的多边形，防跨层误切）
        self._layer: QgsVectorLayer | None = None
        #: 多边形顶点（画布地图坐标；完成后换算到图层 CRS 构建切割面）
        self._pts: list[QgsPointXY] = []
        #: 跟随光标的预览端点（地图坐标；未落点，闭合预览用）
        self._preview_pt: QgsPointXY | None = None
        #: Qt 的双击事件序列是 ``press → release → dblclick → release``：切割完成后
        #: 还会**补一个 release**，不吞掉它就会在已清空的顶点表里立刻落新点，
        #: 看起来像「刚分割完又开始画切割区」。
        self._skip_next_release = False
        #: 预览橡皮筋（面型，半透明填充）
        self._band: QgsRubberBand | None = None
        self._init_band()
        self.setCursor(Qt.CursorShape.CrossCursor)

    # -------------------------------------------------- 工具生命周期

    def activate(self) -> None:
        self._init_band()
        super().activate()

    def deactivate(self) -> None:
        self._clear_polygon()
        self._set_snap_match(None)
        super().deactivate()

    def keyPressEvent(self, event) -> None:  # noqa: N802
        if event.key() == Qt.Key.Key_Escape:
            event.ignore()
            # 不在键盘事件栈内 unsetMapTool，推迟出栈
            QTimer.singleShot(0, self._handle_escape)
        else:
            super().keyPressEvent(event)

    def _handle_escape(self) -> None:
        if self._pts:
            self._clear_polygon()
            self._hint("已清除多边形：可继续左键画多边形、双击分割；再按 Esc 退出")
            return
        self._quit()

    def _quit(self) -> None:
        try:
            self.canvas.unsetMapTool(self)
        except RuntimeError:
            pass

    # -------------------------------------------------- 交互

    def canvasPressEvent(self, event) -> None:  # noqa: N802
        """新的按下 = 新手势开始，清掉可能残留的「吞一次 release」标记。"""
        self._skip_next_release = False

    def canvasReleaseEvent(self, event) -> None:  # noqa: N802
        """左键单击：加顶点；右键单击：清除已画多边形。"""
        button = event.button()
        if self._skip_next_release:
            # 双击序列末尾补发的那个 release：只用于抵消标记，不落点
            self._skip_next_release = False
            return
        if button == Qt.MouseButton.RightButton:
            if self._pts:
                self._clear_polygon()
                self._hint("已清除多边形，可重新绘制")
            return
        if button != Qt.MouseButton.LeftButton:
            return
        self._add_vertex(event, wait_index=True)

    def canvasDoubleClickEvent(self, event) -> None:  # noqa: N802
        """左键双击：把双击点收作末点（防单击已加过）后闭合多边形并执行切割动作。"""
        if event.button() != Qt.MouseButton.LeftButton:
            return
        # 先立标记：无论后面成功与否，这次双击末尾补发的 release 都不该再落点
        self._skip_next_release = True
        pt = self._snapped_pt(event, wait_index=True)
        if pt is None:
            return
        # 双击前的第一次单击可能已把该点收进 _pts → 末点间距过近时不再重复加
        if not self._pts or self._px_distance(self._pts[-1], pt) >= self.MIN_SPACING_PX:
            self._pts.append(pt)
        self._preview_pt = None
        self._run_cutter_action()

    def canvasMoveEvent(self, event) -> None:  # noqa: N802
        """鼠标移动：预览端点跟手 + 磁吸标记实时反馈（不等待索引防卡顿）。"""
        pt = self._snapped_pt(event, wait_index=False)
        if pt is None:
            return
        if self._preview_pt is None or self._px_distance(self._preview_pt, pt) >= 1.0:
            self._preview_pt = pt
            self._refresh_band()
        else:
            # 移动微距也要保证磁吸标记跟随（_snapped_pt 内部已同步指示器）
            pass

    # -------------------------------------------------- 顶点采集

    def _add_vertex(self, event, wait_index: bool = True) -> None:
        """单击落点：校验编辑态后把吸附点收作多边形顶点。"""
        layer = self._current_layer()
        if layer is None:
            self._hint("没有编辑图层：请先点「开始编辑」选择图层")
            return
        if not layer.isEditable():
            self._hint("图层未处于编辑态，无法面分割")
            return
        if self._geom_type_is_point(layer):
            self._hint("点图层无法面分割，请切换到线 / 面图层")
            return
        pt = self._snapped_pt(event, wait_index=wait_index)
        if pt is None:
            return
        if self._pts and self._px_distance(self._pts[-1], pt) < self.MIN_SPACING_PX:
            # 与上一顶点过近：只把预览端放到位，不重复采点
            self._preview_pt = pt
        else:
            self._pts.append(pt)
            self._preview_pt = None
        self._refresh_band()

    # -------------------------------------------------- 分割

    def _run_cutter_action(self) -> None:
        """双击完成：闭合多边形并执行切割动作（子类重写以改变语义，默认面分割）。"""
        layer, cutter = self._prepare_cutter()
        if cutter is None:
            return
        # 用切割面（已换算到图层 CRS）找被覆盖的要素 → 逐个切
        fids = intersect_features(layer, cutter, layer.crs())
        fids = [fid for fid in fids if layer.getFeature(fid).isValid()]
        plans: list[tuple[int, QgsGeometry, QgsGeometry, list]] = []
        for fid in fids:
            feature = layer.getFeature(fid)
            geom = feature.geometry()
            if geom is None or geom.isNull() or geom.isEmpty():
                continue
            inside = geom.intersection(cutter)
            outside = geom.difference(cutter)
            if inside is None or outside is None:
                continue
            # 只有"真正切开"才处理：退化接触（擦边 / 只碰到顶点，交集成点或
            # 零面积线）与完全覆盖（无剩余部分）都不构成分割。
            # 面图层按面积、线图层按长度判定，过滤边界相切的假命中。
            if self._measure(layer, inside) <= 0.0 or self._measure(layer, outside) <= 0.0:
                continue
            plans.append((fid, outside, inside, list(feature.attributes())))
        if not plans:
            self._hint("多边形未切开任何要素：请把多边形画在要素上（部分覆盖），双击分割")
            self._clear_polygon()
            return
        self._commit_split(layer, plans)

    def _commit_split(
        self, layer: QgsVectorLayer, plans: list[tuple[int, QgsGeometry, QgsGeometry, list]]
    ) -> None:
        """写入一个撤销命令：原要素换剩余几何 + 面积字段重算 + 切出部分成新要素。"""
        layer.beginEditCommand("面分割")
        new_fids: list[int] = []
        try:
            for fid, outside, inside, attrs in plans:
                layer.changeGeometry(fid, outside)
                # 原要素面积类字段过期 → 按剩余几何重算（只为真正变了的字段写回）
                before = list(attrs)
                refreshed = refresh_area_fields(layer, attrs, outside)
                for idx, value in enumerate(refreshed):
                    if before[idx] != value:
                        layer.changeAttributeValue(fid, idx, value)
                new_attr = new_feature_attributes(layer, refreshed, inside)
                new_feature = QgsFeature(layer.fields())
                new_feature.setGeometry(inside)
                new_feature.setAttributes(new_attr)
                layer.addFeature(new_feature)
                new_fids.append(new_feature.id())
        except RuntimeError:
            layer.destroyEditCommand()
            self._hint("面分割失败：图层编辑 API 异常")
            self._clear_polygon()
            return
        layer.endEditCommand()

        # 要素增改，空间索引缓存失效 → 清掉让下次命中重建
        clear_spatial_index(layer)
        # 切出的新要素高亮选中，便于确认
        try:
            layer.select(new_fids)
        except RuntimeError:
            logger.warning("面分割后选中新要素失败（不影响分割结果）", exc_info=True)
        self._hint(
            f"已按多边形分割 {len(plans)} 个要素为 {len(plans) + len(new_fids)} 个"
            "（原要素保留剩余部分，切出部分为新要素，可 Ctrl+Z 撤销）"
        )
        self._clear_polygon()
        # 双击事件栈内不直接 refresh（仓库约束），推迟出栈后再刷新
        QTimer.singleShot(0, self._safe_refresh)

    def _prepare_cutter(self) -> "tuple[QgsVectorLayer, QgsGeometry] | tuple[None, None]":
        """双击后的公共准备：校验 + 顶点去重 + 构建图层 CRS 的切割面。

        校验失败 / 多边形无效时给出提示、清空多边形并返回 ``(None, None)``；
        成功返回 ``(编辑图层, 切割面)``。
        """
        layer = self._current_layer()
        if layer is None:
            return None, None
        # 顶点去重（吸附可能把相邻点吸到同一位置）
        cleaned: list[QgsPointXY] = []
        for pt in self._pts:
            if cleaned and self._px_distance(cleaned[-1], pt) < self.MIN_SPACING_PX:
                continue
            cleaned.append(pt)
        # 首尾吸到同一点时闭合环会退化 → 去尾留首
        if len(cleaned) > 1 and self._px_distance(cleaned[0], cleaned[-1]) < self.MIN_SPACING_PX:
            cleaned.pop()
        if len(cleaned) < 3:
            self._hint("请先左键画一个多边形（至少 3 个顶点），再双击完成")
            self._clear_polygon()
            return None, None
        if not layer.isEditable():
            self._hint("图层未处于编辑态，无法执行该操作")
            return None, None
        if self._geom_type_is_point(layer):
            self._hint("点图层不支持该操作，请切换到线 / 面图层")
            return None, None

        cutter = self._build_cutter(cleaned)
        if cutter is None:
            self._hint("多边形无效（顶点退化），请重新绘制")
            self._clear_polygon()
            return None, None
        return layer, cutter

    def _measure(self, layer: QgsVectorLayer, geometry: QgsGeometry) -> float:
        """判定"切到多少"的度量：面图层用面积、线图层用长度（点图层无意义）。"""
        if geometry is None or geometry.isNull() or geometry.isEmpty():
            return 0.0
        try:
            if self._geom_type_is_point(layer):
                return 1.0
            if layer.geometryType() == QgsWkbTypes.PolygonGeometry:
                return geometry.area()
            return geometry.length()
        except Exception:  # noqa: BLE001
            return 0.0

    def _build_cutter(self, pts: list[QgsPointXY]) -> QgsGeometry | None:
        """画布坐标顶点 → 图层 CRS 的切割面几何（闭合环）。"""
        layer_pts: list[QgsPoint] = []
        for pt in pts:
            p = self._to_layer_point(pt)
            if p is None:
                return None
            # 相邻吸附点可能换算后重合 → 去重防退化环
            if (
                layer_pts
                and abs(layer_pts[-1].x() - p.x()) < 1e-9
                and abs(layer_pts[-1].y() - p.y()) < 1e-9
            ):
                continue
            layer_pts.append(p)
        if len(layer_pts) < 3:
            return None
        # 闭合环（QgsPolygon 要求首尾闭合；首尾已被吸附吸到一起则不加重复点）
        ring_pts = [*layer_pts, layer_pts[0]]
        cutter = QgsGeometry(QgsPolygon(QgsLineString(ring_pts)))
        if cutter.isNull() or cutter.isEmpty():
            return None
        return cutter

    # -------------------------------------------------- 预览 / 状态清理

    def _init_band(self) -> None:
        if self._band is None:
            self._band = QgsRubberBand(self.canvas, QgsWkbTypes.PolygonGeometry)
            self._band.setColor(QColor(255, 0, 0, 200))
            self._band.setWidth(2)
            self._band.setFillColor(QColor(255, 0, 0, 40))

    def _refresh_band(self) -> None:
        if self._band is None:
            return
        band = self._band
        band.reset(QgsWkbTypes.PolygonGeometry)
        for pt in self._pts:
            band.addPoint(pt)
        if self._preview_pt is not None:
            band.addPoint(self._preview_pt)
        if self._pts:
            band.show()
        else:
            band.hide()

    def _clear_polygon(self) -> None:
        self._pts = []
        self._preview_pt = None
        if self._band is not None:
            try:
                self._band.reset(QgsWkbTypes.PolygonGeometry)
                self._band.hide()
            except RuntimeError:
                pass

    # -------------------------------------------------- 图层 / 磁吸 / 坐标

    def _current_layer(self) -> QgsVectorLayer | None:
        """当前编辑图层；切换图层时清空未画完的多边形（防跨层误切）。"""
        layer = self._layer_getter()
        if layer is None or layer.type() != layer.VectorLayer:
            return None
        if self._layer is not None and layer is not self._layer:
            self._clear_polygon()
        self._layer = layer
        return layer

    def _geom_type_is_point(self, layer: QgsVectorLayer) -> bool:
        return layer.geometryType() == QgsWkbTypes.PointGeometry

    def _snapped_pt(self, event, wait_index: bool = True) -> QgsPointXY | None:
        """事件像素 → 吸附命中的地图坐标；同步磁吸指示器（顶点 / 边不同图标）。"""
        match = snapped_match(self.canvas, event, wait_index=wait_index)
        self._set_snap_match(match)
        if match is not None:
            return match.point()
        try:
            return self.canvas.getCoordinateTransform().toMapCoordinates(
                self._pixel_of_event(event)
            )
        except RuntimeError:
            return None

    def _set_snap_match(self, match) -> None:
        """把吸附 match 喂给画布共享的 :class:`QgsSnapIndicator`（吸不到隐藏）。"""
        widget = _snap_indicator_for(self.canvas)
        if widget is None:
            return
        try:
            if match is not None and match.isValid():
                widget.setMatch(match)
                widget.setVisible(True)
            else:
                widget.setVisible(False)
        except Exception:  # noqa: BLE001
            pass

    def _pixel_of_event(self, event) -> QPoint:
        """事件像素坐标（QPointF 测试桩 / 真实 QPoint 都兼容）。"""
        raw = event.pixelPoint()
        if isinstance(raw, QPointF):
            return raw.toPoint()
        return QPoint(raw.x(), raw.y())

    def _px_distance(self, a: QgsPointXY, b: QgsPointXY) -> float:
        """两个地图坐标点的画布像素距离（采样去重用）。"""
        mupp = self.canvas.mapSettings().mapUnitsPerPixel()
        if mupp <= 0:
            return float("inf")
        return ((a.x() - b.x()) ** 2 + (a.y() - b.y()) ** 2) ** 0.5 / mupp

    def _to_layer_point(self, map_pt: QgsPointXY) -> QgsPoint | None:
        """画布地图坐标 → 图层 CRS 坐标（CRS 相同 / 无效时恒等）。"""
        layer = self._layer
        if layer is None:
            return QgsPoint(map_pt)
        src = self.canvas.mapSettings().destinationCrs()
        dst = layer.crs()
        if not src.isValid() or not dst.isValid() or src == dst:
            return QgsPoint(map_pt)
        try:
            xform = QgsCoordinateTransform(src, dst, self.canvas.project() or QgsProject.instance())
            return QgsPoint(xform.transform(QgsPointXY(map_pt)))
        except Exception:
            logger.warning("面分割顶点坐标转换失败，回退原坐标", exc_info=True)
            return QgsPoint(map_pt)

    def _safe_refresh(self) -> None:
        try:
            self.canvas.refresh()
        except RuntimeError:
            pass


__all__ = ["SplitPolygonTool"]
