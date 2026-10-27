"""线分割工具。

编辑工具条「线分割」按钮激活的交互工具，**对当前选中的要素直接画切割线**：

- **左键按下** → 从按下位置开始采集切割线（不松开继续移动 = 沿线加点；松开后
  再次左键按下可在其它位置续画折线段）；
- **移动预览** → 只要已画出起点，鼠标移动（按住或松手均可）时，从最近一个
  已定点到光标之间始终有红色预览线实时跟手，方便预判下一段落点；
- **左键双击** → 完成：把双击点收作末点后执行分割，**只切当前选中的要素**
  （线要素 / 面要素都支持；点要素不参与），写入编辑缓冲、可整体撤销；完成后
  清空切割线，可接着画下一条；
- **磁性捕捉** → 起点 / 转折点 / 末点与光标预览都走画布吸附（顶点 / 边，与
  「面分割」及绘制工具共用 :func:`~app.core.domain.drawing.tools.snap.snapped_match`），
  吸附命中时显示磁吸指示器；是否启用由画布「磁性捕捉」工具条决定；
- **右键单击** → 清除已画的切割线（可重画）；
- **Esc** → 依次：清切割线 → 退出工具。

**流程**：先用选择工具选中要分割的图形 → 点「线分割」→ 直接画线。不再需要
在工具内双击挑目标（老流程），选中集就是它作用的范围——与「面分割」「挖洞」
等编辑类工具一致。

分割实现走 QGIS 核心 ``QgsVectorLayer.splitFeatures``（以**图层当前选中**为
准——正好就是本工具要切的那些，不需要临时改选中再还原）——而不是手动
``changeGeometry`` + ``addFeature``：后者给新增块发负临时 fid，提交到 GPKG 等
强 fid 数据源时 OGR 直接按负 ID 建要素，报 “Cannot create feature of ID …
already exists” / ``UNIQUE constraint failed``；核心 API 自行完成新 fid 分配，
各数据源都能干净提交。落库前会先在**拷贝**几何上用 ``QgsGeometry.splitGeometry``
预演一遍：一个都切不开时提示并保留切割线，不白开一个空的编辑命令。

**编辑命令必须成对关闭**：``beginEditCommand`` 与 ``endEditCommand`` 之间一旦
有异常逃出去，命令就悬着不关，图层撤销栈从此 ``canUndo()`` 为假、后续所有编辑
也撤不了（历史 bug：中间那步 ``layer.select(set)`` 抛 ``TypeError``，而 except
只接了 ``RuntimeError``）。所以这里 except 收宽到 ``Exception``，并且**分割成功
之后不做任何可能抛异常的收尾**——选中集、提示、刷新都放在命令关闭之后。

失败路径用 ``destroyEditCommand()``（丢弃改动）收尾——注意**不是**
``cancelEditCommand()``：**PyQGIS 根本没有这个方法**（本机实测
``hasattr(layer, "cancelEditCommand") is False``），写了会在 except 块里再抛
``AttributeError``，异常逃逸、命令照样悬着，比不写还坏。``destroyEditCommand()``
才是「丢弃本次改动并关闭命令」的语义（实测：调用后几何回退、``canUndo()`` 为假）。

安全约束（仓库既定结论，见 ``swipe.py`` docstring）：不在 Qt 事件栈内调
``canvas.refresh()`` / ``unsetMapTool``，一律 ``QTimer.singleShot(0, ...)``
推迟出栈。几何坐标一律换算到**图层 CRS** 后再喂给分割 API。
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
from qgis.gui import QgsMapTool, QgsRubberBand
from qgis.PyQt.QtCore import QPoint, QPointF, Qt, QTimer
from qgis.PyQt.QtGui import QColor

from app.core.domain.check.space_index import clear_spatial_index
from app.core.domain.drawing.tools.base import _snap_indicator_for
from app.core.domain.drawing.tools.snap import snapped_match
from app.infra.logging import get_logger
from app.ui.tools.feature_attrs import refresh_area_fields

logger = get_logger(__name__)


class SplitFeatureTool(QgsMapTool):
    """对选中要素画切割线分割的编辑工具（左键画线 / 右键完成 / Esc 退出）。"""

    #: 采集采样最小间距（像素）：按住左键移动时，过密点会被跳过
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
        #: 当前编辑图层（锁定；切换图层时清空未完成的切割线，防跨层分割）
        self._layer: QgsVectorLayer | None = None
        #: 切割线顶点（画布地图坐标；分割时再换算成图层 CRS）
        self._cut_pts: list[QgsPointXY] = []
        #: 左键是否按住（按住期间 canvasMoveEvent 采样加点）
        self._capturing = False
        #: 跟随光标的预览端点（地图坐标；从最近已定点画到此处，未提交不参与分割）
        self._preview_pt: QgsPointXY | None = None
        #: 切割线预览橡皮筋（线型）
        self._band: QgsRubberBand | None = None
        #: Qt 的双击事件序列是 ``press → release → dblclick → release``：双击完成的
        #: 那次末尾还会**补一个 release**，不吞掉它就会在已清空的顶点表里立刻落新点，
        #: 看起来像「刚分割完又开始画」。
        self._skip_next_release = False
        self.setCursor(Qt.CursorShape.CrossCursor)

    # -------------------------------------------------- 工具生命周期

    def activate(self) -> None:
        super().activate()

    def deactivate(self) -> None:
        self._clear_line()
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
        self._capturing = False
        if self._cut_pts:
            self._clear_line()
            self._hint("已清除切割线：可继续画线；再按 Esc 退出")
            return
        self._quit()

    def _quit(self) -> None:
        try:
            self.canvas.unsetMapTool(self)
        except RuntimeError:
            pass

    # -------------------------------------------------- 交互

    def canvasPressEvent(self, event) -> None:  # noqa: N802
        """左键按下：新笔手势开始；绘制期落点见 :meth:`canvasReleaseEvent`。"""
        if event.button() == Qt.MouseButton.RightButton:
            # 右键：清除已画的切割线（可重画），不完成分割
            self._capturing = False
            if self._cut_pts:
                self._clear_line()
                self._hint("已清除切割线：按住左键重新画，双击完成分割")
            return
        if event.button() == Qt.MouseButton.LeftButton:
            self._skip_next_release = False
            self._start_or_continue(event)

    def canvasReleaseEvent(self, event) -> None:  # noqa: N802
        """左键松开：把**松开位置**收作顶点后停止采样。

        松开点必须收进去：用户「按住拖到终点再松开」是最自然的画法，而移动事件的
        采样是按 3px 间距跳着采的——只靠它，末点会停在离终点一小段的地方，线到不了
        要素边界就切不开。
        """
        if self._skip_next_release:
            # 双击序列末尾补发的那个 release：只用于抵消标记，不落点
            self._skip_next_release = False
            return
        if event.button() != Qt.MouseButton.LeftButton:
            return
        if not self._capturing:
            return
        self._append_vertex(self._snapped_pt(event, wait_index=True))
        self._capturing = False

    def canvasDoubleClickEvent(self, event) -> None:  # noqa: N802
        """左键双击：完成分割。"""
        if event.button() != Qt.MouseButton.LeftButton:
            return
        # 先立标记：这次双击末尾补发的 release 不该再落点
        self._skip_next_release = True
        self._capturing = False
        # 双击前的第一次单击可能已把该点收进 _cut_pts → 末点间距过近时不再重复加
        self._append_vertex(self._snapped_pt(event, wait_index=True))
        self._finish_split()

    def _append_vertex(self, pt: QgsPointXY | None) -> None:
        """把一个顶点收进切割线（与上一顶点过近则只放到预览端）。"""
        if pt is None:
            return
        last = self._cut_pts[-1] if self._cut_pts else None
        if last is None or self._px_distance(last, pt) >= self.MIN_SPACING_PX:
            self._cut_pts.append(pt)
            self._preview_pt = None
        else:
            self._preview_pt = pt
        self._refresh_band()

    # -------------------------------------------------- 切割线采集

    def canvasMoveEvent(self, event) -> None:  # noqa: N802
        """鼠标移动：按住左键时采样加点；否则/间距不足时把预览端跟到光标。

        预览端也走吸附（``wait_index=False``：只触发索引构建不等待），这样拖动时
        能看到切点贴到已有要素的顶点 / 边上，和落点实际使用的点一致。
        """
        if not self._cut_pts:
            # 还没有第一个点：也刷新磁吸指示器，用户能提前看到起点会吸到哪里
            self._snapped_pt(event, wait_index=False)
            return  # 没有已定点，无从预览
        pt = self._snapped_pt(event, wait_index=False)
        if pt is None:
            return
        last = self._cut_pts[-1]
        if self._capturing and self._px_distance(last, pt) >= self.MIN_SPACING_PX:
            # 按住左键拖够 3px：采样成实点，预览端让位
            self._cut_pts.append(pt)
            self._preview_pt = None
            self._refresh_band()
        else:
            # 间距不足或已松手：只把预览端跟到光标（红色连线实时可见）
            if self._preview_pt is None or self._px_distance(self._preview_pt, pt) >= 1.0:
                self._preview_pt = pt
                self._refresh_band()

    def _start_or_continue(self, event) -> None:
        """左键按下：校验后把按下点收作顶点并开始采样。"""
        layer = self._current_layer()
        if layer is None:
            return
        if not layer.isEditable():
            self._hint("图层未处于编辑态，无法分割要素")
            return
        if self._geom_type_is_point(layer):
            self._hint("点要素无法分割，请切换到线 / 面图层")
            return
        if not layer.selectedFeatureIds():
            self._hint("请先用点选 / 框选选中要分割的要素，再画切割线")
            return
        self._append_vertex(self._snapped_pt(event, wait_index=True))
        self._capturing = True

    def _clear_line(self) -> None:
        """清空切割线 + 预览橡皮筋。"""
        self._capturing = False
        self._cut_pts = []
        self._preview_pt = None
        self._set_snap_match(None)
        if self._band is not None:
            try:
                self._band.reset(QgsWkbTypes.LineGeometry)
                self._band.hide()
            except RuntimeError:
                pass

    def _refresh_band(self) -> None:
        """按当前切割线 + 光标预览端重建红色预览线（画布坐标，无需 CRS 换算）。"""
        if self._band is None:
            self._band = QgsRubberBand(self.canvas, QgsWkbTypes.LineGeometry)
            self._band.setColor(QColor(255, 0, 0, 200))
            self._band.setWidth(2)
        band = self._band
        band.reset(QgsWkbTypes.LineGeometry)
        for pt in self._cut_pts:
            band.addPoint(pt)
        if self._preview_pt is not None:
            band.addPoint(self._preview_pt)  # 光标预览端：画到此处，让线跟手
        if self._cut_pts:
            band.show()
        else:
            band.hide()

    # -------------------------------------------------- 分割

    def _finish_split(self) -> None:
        """双击完成：按切割线分割**选中的**要素（走核心 splitFeatures，入编辑缓冲）。"""
        layer = self._current_layer()
        if layer is None:
            return
        if not layer.isEditable():
            self._hint("图层未处于编辑态，无法分割要素")
            return
        if self._geom_type_is_point(layer):
            self._hint("点要素无法分割，请切换到线 / 面图层")
            return
        fids = list(layer.selectedFeatureIds())
        if not fids:
            self._hint("请先用点选 / 框选选中要分割的要素，再画切割线")
            self._clear_line()
            return
        # 若光标带了一段预览线（最近已定点 → 光标），把它收作切割线末点，所见即所切
        if self._preview_pt is not None and self._cut_pts:
            if self._px_distance(self._cut_pts[-1], self._preview_pt) >= 1.0:
                self._cut_pts.append(self._preview_pt)
        self._preview_pt = None
        if len(self._cut_pts) < 2:
            self._hint("请先按住左键在要素上画一条切割线，再右键完成")
            return

        cleaned = self._layer_line_points()
        if len(cleaned) < 2:
            self._hint("切割线太短，无法分割；请按住左键画完整穿过要素的线")
            self._clear_line()
            return

        # 预演：在**拷贝**几何上试切一遍，统计会被切开的要素数。一个都切不开就
        # 不落库、也不开编辑命令（beginEditCommand 会截断 redo 历史，空开一次
        # 会白白破坏撤销历史）。
        cut_count = 0
        for fid in fids:
            feature = layer.getFeature(fid)
            if not feature.isValid():
                continue
            geom = feature.geometry()
            if geom is None or geom.isNull() or geom.isEmpty():
                continue
            work = QgsGeometry(geom)
            code, new_geoms, _tp = work.splitGeometry(cleaned, False, True)
            if code != Qgis.GeometryOperationResult.Success:
                continue
            parts = [work, *list(new_geoms)]
            if any(p is not None and not p.isNull() and not p.isEmpty() for p in parts):
                cut_count += 1
        if cut_count == 0:
            self._hint("没有要素被切开：切割线需完整穿过选中要素的边界")
            self._clear_line()
            return

        # 用曲线重载（QgsCurve）避开旧版 Iterable[QgsPointXY] 重载在 PyQt 的
        # 过载解析问题：直接传 list[QgsPoint] 会先撞上已废弃的 QgsPointXY
        # 重载并抛 TypeError，曲线重载两版本签名一致且不废弃。
        cut_line = QgsLineString(cleaned)
        layer.beginEditCommand("线分割")
        # QGIS 的 splitFeatures 只动几何、**不碰属性** —— 不补这一步，面积 / 周长类
        # 字段会留着切割前的旧值（用户报过「线分割后面积没变化」）。它给新块发的
        # featureAdded 在下面接住，好把新块也一起重算。
        added_fids: list[int] = []
        try:
            layer.featureAdded.connect(added_fids.append)
            try:
                # 核心 API 只切图层**当前选中**的要素——正是上面读到的 fids，
                # 不需要临时改选中（改完还得还原，那一步出错会把编辑命令悬着不关）
                code, _split_pts = layer.splitFeatures(cut_line, False, False)
            finally:
                try:
                    layer.featureAdded.disconnect(added_fids.append)
                except TypeError:  # 没连上（或已被断开）
                    pass
            if code != Qgis.GeometryOperationResult.Success:
                layer.destroyEditCommand()
                self._hint("没有要素被切开：切割线需完整穿过选中要素的边界")
                self._clear_line()
                return
            # 重算**必须在编辑命令内**：命令关掉之后再改属性会另起一个撤销步骤，
            # 用户按一次 Ctrl+Z 只会撤掉面积修正、切开的图形留在那儿
            self._refresh_cut_fields(layer, [*fids, *added_fids])
        except Exception:  # noqa: BLE001 —— 收宽：任何异常都必须关掉编辑命令
            layer.destroyEditCommand()
            logger.exception("线分割失败: layer=%s", layer.name())
            self._hint("分割失败：图层编辑 API 异常")
            self._clear_line()
            return
        layer.endEditCommand()

        # ↓ 编辑命令已关闭，下面全是收尾：任何失败都不该再影响撤销栈
        clear_spatial_index(layer)
        self._clear_line()
        self._hint(
            f"已按切割线分割 {cut_count} 个要素（可 Ctrl+Z 撤销）；继续画线可再分割"
        )
        # 编辑命令栈外刷新画布（仓库约束：事件栈内不直接 refresh）
        QTimer.singleShot(0, self._safe_refresh)

    @staticmethod
    def _refresh_cut_fields(layer: QgsVectorLayer, fids: list[int]) -> None:
        """把 ``fids`` 里每个要素的面积 / 周长类字段按**当前几何**重算并写回。

        ``splitFeatures`` 只改几何，属性得由调用方补；找不到面积类字段时无操作。
        只为真正变了的字段写回（与其它几何编辑工具同一套口径）。
        """
        for fid in fids:
            feature = layer.getFeature(fid)
            if not feature.isValid():
                continue
            geometry = feature.geometry()
            if geometry is None or geometry.isEmpty():
                continue
            attrs = list(feature.attributes())
            before = list(attrs)
            refreshed = refresh_area_fields(layer, attrs, geometry)
            for idx, value in enumerate(refreshed):
                if before[idx] != value:
                    layer.changeAttributeValue(fid, idx, value)

    def _layer_line_points(self) -> list[QgsPoint]:
        """切割线顶点换算到图层 CRS 并去重（分割 API 要求与几何同 CRS）。"""
        pts = [self._to_layer_point(p) for p in self._cut_pts]
        cleaned: list[QgsPoint] = []
        for pt in pts:
            if pt is None:
                continue
            if cleaned and abs(cleaned[-1].x() - pt.x()) < 1e-9 and abs(cleaned[-1].y() - pt.y()) < 1e-9:
                continue
            cleaned.append(pt)
        return cleaned

    # -------------------------------------------------- 图层 / 坐标

    def _current_layer(self) -> QgsVectorLayer | None:
        """当前编辑图层；切换图层时清空未完成切割线（防跨层误分割）。"""
        layer = self._layer_getter()
        if layer is None or layer.type() != layer.VectorLayer:
            return None
        if self._layer is not None and layer is not self._layer:
            self._clear_line()
        self._layer = layer
        return layer

    def _geom_type_is_point(self, layer: QgsVectorLayer) -> bool:
        """图层是否是点几何（点要素无法被线分割）。"""
        return layer.geometryType() == QgsWkbTypes.PointGeometry

    def _safe_refresh(self) -> None:
        try:
            self.canvas.refresh()
        except RuntimeError:
            pass

    def _pixel_of_event(self, event) -> QPoint | None:
        """事件像素坐标（QPointF 测试桩 / 真实 QPoint 都兼容）。"""
        try:
            raw = event.pixelPoint()
        except AttributeError:
            raw = event.pos()
        if isinstance(raw, QPointF):
            return raw.toPoint()
        return QPoint(raw.x(), raw.y())

    def _map_point_of_event(self, event) -> QgsPointXY | None:
        """事件像素 → 画布地图坐标（canvas 目标 CRS）。"""
        px = self._pixel_of_event(event)
        if px is None:
            return None
        try:
            return self.canvas.getCoordinateTransform().toMapCoordinates(px)
        except RuntimeError:
            return None

    def _snapped_pt(self, event, wait_index: bool = True) -> QgsPointXY | None:
        """事件像素 → **吸附后**的地图坐标；同步磁吸指示器。

        与「面分割」/ 绘制工具共用 :func:`snapped_match`：开关、类型、容差全部
        由画布吸附配置决定；没开吸附 / 没命中时回退原始地图坐标。
        """
        match = snapped_match(self.canvas, event, wait_index=wait_index)
        self._set_snap_match(match)
        if match is not None:
            return match.point()
        return self._map_point_of_event(event)

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
            logger.warning("线分割切割线坐标转换失败，回退原坐标", exc_info=True)
            return QgsPoint(map_pt)


__all__ = ["SplitFeatureTool"]
