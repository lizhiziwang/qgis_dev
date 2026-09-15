"""边界修改：沿选中图形的边界画一条整形线，按 QGIS Reshape 语义改写边界。

**用法**：先「开始编辑」并用点选 / 框选选中要改的图形 → 点工具条「边界修改」→
左键逐点画整形线 → 双击完成整形（右键清除已画线、Esc 退出）。

**整形线两端都必须落在图形外侧**，这是 QGIS Reshape 的既有语义（本机实测：
任一端在内部时 ``reshapeGeometry`` 返回 ``NothingHappened``，几何原样不动）。
线与原边界围出的那块按线的走向被**并入或切除**——即「边界跟着线走」。

一次操作会作用到**当前选中的全部面要素**（对齐 QGIS 的 Reshape Features）；
线碰不到的要素会被跳过。全部改动包进**一个** ``beginEditCommand("边界修改")``，
Ctrl+Z 一次撤销整批。按仓库惯例：改完清空间索引缓存（``clear_spatial_index``），
画布刷新由 ``QTimer.singleShot(0, ...)`` 推迟出事件栈。
"""

from __future__ import annotations

from collections.abc import Callable

from qgis.core import (
    Qgis,
    QgsCoordinateTransform,
    QgsGeometry,
    QgsLineString,
    QgsPoint,
    QgsPointXY,
    QgsProject,
    QgsVectorLayer,
    QgsWkbTypes,
)
from qgis.PyQt.QtCore import QPoint, QPointF, Qt, QTimer
from qgis.PyQt.QtGui import QColor
from qgis.gui import QgsMapTool, QgsRubberBand

from app.core.domain.check.space_index import clear_spatial_index
from app.core.domain.drawing.tools.base import _snap_indicator_for
from app.core.domain.drawing.tools.snap import snapped_match
from app.infra.logging import get_logger
from app.ui.tools.feature_attrs import refresh_area_fields

logger = get_logger(__name__)


class BoundaryEditTool(QgsMapTool):
    """画整形线改写**选中**面要素边界的编辑工具。"""

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
        #: 整形线顶点（画布地图坐标；执行时换算到图层 CRS）
        self._pts: list[QgsPointXY] = []
        #: 跟随光标的预览端点（未落点）
        self._preview_pt: QgsPointXY | None = None
        #: Qt 的双击事件序列是 ``press → release → dblclick → release``：完成整形
        #: 后还会**补一个 release**，不吞掉它就会立刻在空线上落下新的一点，
        #: 看起来像「刚整形完又开始画线」。
        self._skip_next_release = False
        self._band: QgsRubberBand | None = None
        self._init_band()
        self.setCursor(Qt.CursorShape.CrossCursor)

    # ============================================================== 生命周期

    def _init_band(self) -> None:
        if self._band is None:
            self._band = QgsRubberBand(self.canvas, QgsWkbTypes.LineGeometry)
            self._band.setColor(QColor(255, 0, 0))
            self._band.setWidth(2)

    def activate(self) -> None:
        self._init_band()
        super().activate()

    def deactivate(self) -> None:
        self._clear_line()
        self._set_snap_match(None)
        super().deactivate()

    def keyPressEvent(self, event) -> None:  # noqa: N802 —— QGIS 命名
        if event.key() == Qt.Key.Key_Escape:
            event.ignore()
            # 不在键盘事件栈内 unsetMapTool，推迟出栈
            QTimer.singleShot(0, self._handle_escape)
        else:
            super().keyPressEvent(event)

    def _handle_escape(self) -> None:
        if self._pts:
            self._clear_line()
            self._hint("已清除整形线：可重新画线、双击完成；再按 Esc 退出工具")
            return
        try:
            self.canvas.unsetMapTool(self)
        except RuntimeError:
            pass

    # ============================================================== 交互

    def canvasPressEvent(self, event) -> None:  # noqa: N802
        """新的按下 = 新手势开始，清掉可能残留的「吞一次 release」标记。"""
        self._skip_next_release = False

    def canvasReleaseEvent(self, event) -> None:  # noqa: N802
        """左键单击加点；右键单击清除已画线。"""
        button = event.button()
        if self._skip_next_release:
            # 双击序列末尾补发的那个 release：只用于抵消标记，不落点
            self._skip_next_release = False
            return
        if button == Qt.MouseButton.RightButton:
            if self._pts:
                self._clear_line()
                self._hint("已清除整形线，可重新绘制")
            return
        if button != Qt.MouseButton.LeftButton:
            return

        layer = self._current_layer()
        if layer is None:
            self._hint("没有编辑图层：请先点「开始编辑」选择图层")
            return
        if not layer.isEditable():
            self._hint("图层未处于编辑态，无法修改边界")
            return
        if not layer.selectedFeatureIds():
            self._hint("请先用点选 / 框选选中要修改边界的面要素")
            return

        pt = self._snapped_pt(event, wait_index=True)
        if pt is None:
            return
        if self._pts and self._px_distance(self._pts[-1], pt) < self.MIN_SPACING_PX:
            return
        self._pts.append(pt)
        self._refresh_band()

    def canvasDoubleClickEvent(self, event) -> None:  # noqa: N802
        """左键双击：把双击点收作末点后执行整形。"""
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
        self._apply_reshape()

    def canvasMoveEvent(self, event) -> None:  # noqa: N802
        """鼠标移动：预览端点跟手。"""
        pt = self._snapped_pt(event, wait_index=False)
        if pt is None:
            return
        if self._preview_pt is None or self._px_distance(self._preview_pt, pt) >= 1.0:
            self._preview_pt = pt
            self._refresh_band()

    # ============================================================== 采集辅助

    def _current_layer(self) -> QgsVectorLayer | None:
        return self._layer_getter()

    def _pixel_of_event(self, event) -> QPoint:
        """事件像素坐标（QPointF 测试桩 / 真实 QPoint 都兼容）。"""
        raw = event.pixelPoint()
        if isinstance(raw, QPointF):
            return raw.toPoint()
        return QPoint(raw.x(), raw.y())

    def _snapped_pt(self, event, wait_index: bool = True) -> QgsPointXY | None:
        """取落点：优先吸附点，吸不到用原始点击位置。"""
        try:
            match = snapped_match(self.canvas, event, wait_index=wait_index)
        except Exception:  # noqa: BLE001 —— 吸附失败退回原始点
            match = None
        if match is not None and match.isValid():
            self._set_snap_match(match)
            return QgsPointXY(match.point())
        self._set_snap_match(None)
        try:
            return self.canvas.getCoordinateTransform().toMapCoordinates(
                self._pixel_of_event(event)
            )
        except RuntimeError:
            return None

    def _px_distance(self, a: QgsPointXY, b: QgsPointXY) -> float:
        """两点在屏幕上的像素距离（用于过滤过密的重复点）。"""
        try:
            transform = self.canvas.getCoordinateTransform()
            pa = transform.transform(a)
            pb = transform.transform(b)
            return ((pa.x() - pb.x()) ** 2 + (pa.y() - pb.y()) ** 2) ** 0.5
        except RuntimeError:
            return float("inf")

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

    def _refresh_band(self) -> None:
        if self._band is None:
            return
        self._band.reset(QgsWkbTypes.LineGeometry)
        if not self._pts:
            return
        for pt in self._pts:
            self._band.addPoint(pt, False)
        if self._preview_pt is not None and self._preview_pt != self._pts[-1]:
            self._band.addPoint(self._preview_pt, True)
        else:
            self._band.addPoint(self._pts[-1], True)

    def _clear_line(self) -> None:
        self._pts.clear()
        self._preview_pt = None
        if self._band is not None:
            try:
                self._band.reset(QgsWkbTypes.LineGeometry)
            except RuntimeError:
                pass

    def _safe_refresh(self) -> None:
        try:
            self.canvas.refresh()
        except RuntimeError:
            pass

    def _to_layer_point(
        self, layer: QgsVectorLayer, map_pt: QgsPointXY
    ) -> QgsPoint | None:
        """画布地图坐标 → 图层 CRS 坐标（CRS 相同 / 无效时恒等）。"""
        src = self.canvas.mapSettings().destinationCrs()
        dst = layer.crs()
        if not src.isValid() or not dst.isValid() or src == dst:
            return QgsPoint(map_pt)
        try:
            xform = QgsCoordinateTransform(
                src, dst, self.canvas.project() or QgsProject.instance()
            )
            return QgsPoint(xform.transform(QgsPointXY(map_pt)))
        except Exception:  # noqa: BLE001
            logger.warning("边界修改：整形线坐标转换失败", exc_info=True)
            return None

    # ============================================================== 执行整形

    def _build_layer_line(self, layer: QgsVectorLayer) -> QgsLineString | None:
        """把采集到的画布坐标整形线换算成图层 CRS 的线。"""
        points: list[QgsPoint] = []
        for pt in self._pts:
            layer_pt = self._to_layer_point(layer, pt)
            if layer_pt is None:
                return None
            points.append(layer_pt)
        if len(points) < 2:
            return None
        return QgsLineString(points)

    @staticmethod
    def _is_reshapeable(layer: QgsVectorLayer, fid: int) -> bool:
        feature = layer.getFeature(fid)
        if not feature.isValid():
            return False
        geometry = feature.geometry()
        if geometry is None or geometry.isNull() or geometry.isEmpty():
            return False
        return (
            QgsWkbTypes.geometryType(geometry.wkbType())
            == Qgis.GeometryType.Polygon
        )

    def _apply_reshape(self) -> None:
        """对当前选中的面要素执行整形，全部改动包进一个撤销命令。"""
        layer = self._current_layer()
        if layer is None:
            self._hint("没有编辑图层：请先点「开始编辑」选择图层")
            return
        if not layer.isEditable():
            self._hint("图层未处于编辑态，无法修改边界")
            return
        if len(self._pts) < 2:
            self._hint("整形线至少需要两个点：左键加点、双击完成")
            return

        line = self._build_layer_line(layer)
        if line is None:
            self._hint("整形线构建失败（坐标转换异常，详见日志）")
            return

        fids = [
            fid for fid in layer.selectedFeatureIds() if self._is_reshapeable(layer, fid)
        ]
        if not fids:
            self._hint("请先用点选 / 框选选中要修改边界的面要素")
            return

        # 几何先算完再开编辑命令：无实际改动时不开命令（beginEditCommand 会截断
        # redo 历史），也避免开了一半再回滚
        work: list[tuple[int, QgsGeometry, list]] = []
        skipped = 0
        for fid in fids:
            feature = layer.getFeature(fid)
            geometry = feature.geometry()
            # 深拷贝：reshapeGeometry 是就地改，且不能把要素自带的几何直接改掉
            candidate = QgsGeometry.fromWkt(geometry.asWkt())
            if candidate.reshapeGeometry(line) != Qgis.GeometryOperationResult.Success:
                skipped += 1
                continue
            if candidate.isEmpty():
                skipped += 1
                continue
            work.append((fid, candidate, list(feature.attributes())))

        if not work:
            logger.info("边界修改：%d 个选中要素都未被整形线改变", skipped)
            self._hint("整形线未改变任何边界：请让线的**两端都落在图形外侧**")
            return

        changed = 0
        layer.beginEditCommand("边界修改")
        try:
            for fid, candidate, attrs in work:
                layer.changeGeometry(fid, candidate)
                before = list(attrs)
                refreshed = refresh_area_fields(layer, list(attrs), candidate)
                for idx, value in enumerate(refreshed):
                    if before[idx] != value:
                        layer.changeAttributeValue(fid, idx, value)
                changed += 1
        except RuntimeError:
            layer.destroyEditCommand()
            logger.exception("边界修改失败: layer=%s", layer.name())
            self._hint("边界修改失败：图层编辑 API 异常")
            return
        layer.endEditCommand()

        # 要素几何已变，空间索引缓存失效 → 清掉让下次命中重建
        clear_spatial_index(layer)
        self._clear_line()
        QTimer.singleShot(0, self._safe_refresh)
        suffix = f"，{skipped} 个未被线触及已跳过" if skipped else ""
        self._hint(f"已修改 {changed} 个图形的边界（可 Ctrl+Z 撤销）{suffix}")


__all__ = ["BoundaryEditTool"]
