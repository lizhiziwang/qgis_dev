"""线分割工具。

编辑工具条「线分割」按钮激活的交互工具，**先双击选中要分割的要素，再画
切割线分割**：

- **左键双击要素** → 选中为分割目标（黄色描边高亮；双击空白处取消，双击
  其它要素可切换）。**未选中任何要素时不能画线**——绘制阶段以此为前提；
- **左键按下（需先选中）** → 从按下位置开始采集切割线（不松开继续移动 =
  沿线加点；松开后再次左键按下可在其它位置续画折线段）；
- **移动预览** → 只要已画出起点，鼠标移动（按住或松手均可）时，从最近一
  个已定点到光标之间始终有红色预览线实时跟手，方便预判下一段落点；
- **右键单击** → 完成：把光标处预览端点收进切割线后，**只分割双击选中的
  那一要素**（线要素 / 面要素都支持；点要素不参与），写入编辑缓冲、可整体
  撤销；完成后清空切割线与选择，需重新双击选择下一要素；
- **Esc** → 依次：清切割线 → 取消选中要素 → 退出工具。

分割实现走 QGIS 核心 ``QgsVectorLayer.splitFeatures``（以**图层当前选中**为
准：调用前临时 ``select`` 选中目标要素，调用后还原原选择，因此只分割双击
选中的那一要素）——而不是手动 ``changeGeometry`` + ``addFeature``：后者给
新增块发负临时 fid，提交到 GPKG 等强 fid 数据源时 OGR 直接按负 ID 建要素，
报 “Cannot create feature of ID … already exists” / ``UNIQUE constraint
failed``；核心 API 自行完成新 fid 分配，各数据源都能干净提交。调用前会先在
**拷贝**几何上用 ``QgsGeometry.splitGeometry`` 预演一遍：判定线是否完整穿过
要素边界（未穿过则提示、不落库），并统计将产生的块数用于提示文案。切割线
以 ``QgsLineString``（曲线重载）传入：3.40 下直传 ``list[QgsPoint]`` 会误中
已废弃的 ``Iterable[QgsPointXY]`` 重载抛 TypeError，曲线重载两版本签名一致
且不废弃。整个过程包成一个编辑命令（``beginEditCommand`` …），Ctrl+Z 可
整体撤销。

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
    QgsRectangle,
    QgsVectorLayer,
    QgsWkbTypes,
)
from qgis.gui import QgsMapTool, QgsRubberBand
from qgis.PyQt.QtCore import QPoint, QPointF, Qt, QTimer
from qgis.PyQt.QtGui import QColor

from app.core.domain.check.query import intersect_features
from app.core.domain.check.space_index import clear_spatial_index


class SplitFeatureTool(QgsMapTool):
    """双击选要素 → 画切割线 → 右键分割的编辑工具（线 / 面图层）。"""

    #: 采集采样最小间距（像素）：按住左键移动时，过密点会被跳过
    MIN_SPACING_PX = 3
    #: 候选要素拾取容差（像素，画布坐标扩边用）
    TOLERANCE_PX = 4

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
        #: 双击选中的待分割要素 fid（None = 未选中，此时不允许画切割线）
        self._selected_fid: int | None = None
        #: 选中要素的高亮橡皮筋（黄色描边，线 / 面通用）
        self._select_band: QgsRubberBand | None = None
        self.setCursor(Qt.CursorShape.CrossCursor)

    # -------------------------------------------------- 工具生命周期

    def activate(self) -> None:
        super().activate()

    def deactivate(self) -> None:
        self._clear_line()
        self._clear_selection(hint=False)
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
            self._hint("已清除切割线：可继续画线；再按 Esc 取消选中")
            return
        if self._selected_fid is not None:
            self._clear_selection()
            return
        self._quit()

    def _quit(self) -> None:
        try:
            self.canvas.unsetMapTool(self)
        except RuntimeError:
            pass

    # -------------------------------------------------- 交互

    def canvasPressEvent(self, event) -> None:  # noqa: N802
        """左键按下：有选中要素时开始 / 续画切割线；右键按下：完成分割选中要素。"""
        button = event.button()
        if button == Qt.MouseButton.RightButton:
            # 右键完成：若正按住左键采集，先停止采集再分割
            self._capturing = False
            self._finish_split()
        elif button == Qt.MouseButton.LeftButton:
            if self._selected_fid is None:
                # 双击选中要素前的第一次按下会先到这儿：未选中则只提示、不起笔
                self._hint("请先左键双击选择要分割的要素")
                return
            self._start_or_continue(event)

    def canvasDoubleClickEvent(self, event) -> None:  # noqa: N802
        """左键双击：选中 / 切换待分割要素（点空白处则取消选中）。"""
        if event.button() != Qt.MouseButton.LeftButton:
            return
        layer = self._current_layer()
        if layer is None:
            return
        if not layer.isEditable():
            self._hint("图层未处于编辑态，无法分割要素")
            return
        if self._geom_type_is_point(layer):
            self._hint("点要素无法分割，请切换到线 / 面图层")
            return
        # 双击切换选中时，前一次单击可能已起笔留点 → 先清掉再选
        if self._cut_pts:
            self._clear_line()
        fid = self._hit_fid(event)
        if fid is None:
            self._clear_selection(hint=False)
            self._hint("未命中要素：双击要素可选中，Esc 退出")
            return
        self._selected_fid = fid
        self._refresh_select_band()
        self._hint("已选中要素：按住左键画切割线，右键完成分割")

    def canvasMoveEvent(self, event) -> None:  # noqa: N802
        """鼠标移动：按住左键时采样加点；否则/间距不足时把预览端跟到光标。"""
        if not self._cut_pts:
            return  # 没有已定点，无从预览
        pt = self._map_point_of_event(event)
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

    def canvasReleaseEvent(self, event) -> None:  # noqa: N802
        """左键松开：停止采样（切割线保留，等待续画或右键完成）。"""
        if event.button() == Qt.MouseButton.LeftButton:
            self._capturing = False

    # -------------------------------------------------- 选中要素

    def _hit_fid(self, event) -> int | None:
        """点击像素 → 命中要素 fid（10px 容差矩形求交；多要素命中取最上层）。"""
        layer = self._current_layer()
        if layer is None:
            return None
        px = self._pixel_of_event(event)
        if px is None:
            return None
        try:
            map_pt = self.canvas.getCoordinateTransform().toMapCoordinates(px)
        except RuntimeError:
            return None
        tol = self.TOLERANCE_PX * self.canvas.mapSettings().mapUnitsPerPixel()
        rect = QgsRectangle(
            map_pt.x() - tol, map_pt.y() - tol, map_pt.x() + tol, map_pt.y() + tol
        )
        project = self.canvas.project() or QgsProject.instance()
        fids = intersect_features(
            layer,
            QgsGeometry.fromRect(rect),
            self.canvas.mapSettings().destinationCrs(),
            project,
        )
        # 过滤已删除 / 失效要素（空间索引缓存可能含已删 fid）
        fids = [fid for fid in fids if layer.getFeature(fid).isValid()]
        return fids[-1] if fids else None  # 后添加的要素通常绘制在上层

    def _refresh_select_band(self) -> None:
        """把选中要素几何画成黄色描边高亮（图层 CRS → 画布 CRS）。"""
        layer = self._current_layer()
        if layer is None or self._selected_fid is None:
            return
        feature = layer.getFeature(self._selected_fid)
        if not feature.isValid():
            return
        geom = feature.geometry()
        if geom is None or geom.isNull() or geom.isEmpty():
            return
        if self._select_band is None:
            self._select_band = QgsRubberBand(self.canvas, QgsWkbTypes.LineGeometry)
            self._select_band.setColor(QColor(255, 200, 0, 230))
            self._select_band.setWidth(3)
        canvas_geom = self._to_canvas_geom(layer, geom)
        band = self._select_band
        band.reset(QgsWkbTypes.LineGeometry)
        for v in canvas_geom.vertices():
            band.addPoint(QgsPointXY(v.x(), v.y()))
        band.show()

    def _clear_selection(self, hint: bool = True) -> None:
        """取消选中要素并隐藏高亮。"""
        self._selected_fid = None
        if self._select_band is not None:
            try:
                self._select_band.reset(QgsWkbTypes.LineGeometry)
                self._select_band.hide()
            except RuntimeError:
                pass
        if hint:
            self._hint("已取消选择：左键双击重选要素，Esc 退出")

    # -------------------------------------------------- 切割线采集

    def _start_or_continue(self, event) -> None:
        """左键按下：校验编辑态后把按下点加入切割线并开始采样。"""
        layer = self._current_layer()
        if layer is None:
            return
        if not layer.isEditable():
            self._hint("图层未处于编辑态，无法分割要素")
            return
        if self._geom_type_is_point(layer):
            self._hint("点要素无法分割，请切换到线 / 面图层")
            return
        pt = self._map_point_of_event(event)
        if pt is None:
            return
        last = self._cut_pts[-1] if self._cut_pts else None
        if last is None or self._px_distance(last, pt) >= self.MIN_SPACING_PX:
            # 续画点离上一已定点够远 → 收作实点；否则仅把预览端放到位
            self._cut_pts.append(pt)
            self._preview_pt = None
            self._refresh_band()
        else:
            self._preview_pt = pt
            self._refresh_band()
        self._capturing = True

    def _clear_line(self) -> None:
        """清空切割线 + 预览橡皮筋。"""
        self._capturing = False
        self._cut_pts = []
        self._preview_pt = None
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
        """右键完成：按切割线分割**选中的那一要素**（走核心 splitFeatures，入编辑缓冲）。"""
        layer = self._current_layer()
        if layer is None:
            return
        fid = self._selected_fid
        if fid is None:
            self._hint("请先左键双击选择要分割的要素")
            return
        feature = layer.getFeature(fid)
        if not feature.isValid():
            # 选中要素已被外部删除 / 撤销 → 失效，提示重选
            self._clear_selection(hint=False)
            self._hint("所选要素已不存在，请重新双击选择")
            return
        # 若光标带了一段预览线（最近已定点 → 光标），把它收作切割线末点，所见即所切
        if self._preview_pt is not None and self._cut_pts:
            if self._px_distance(self._cut_pts[-1], self._preview_pt) >= 1.0:
                self._cut_pts.append(self._preview_pt)
        self._preview_pt = None
        if len(self._cut_pts) < 2:
            self._hint("请先按住左键在要素上画一条切割线，再右键完成")
            return
        if not layer.isEditable():
            self._hint("图层未处于编辑态，无法分割要素")
            return
        if self._geom_type_is_point(layer):
            self._hint("点要素无法分割，请切换到线 / 面图层")
            return

        # 切割线顶点 → 图层 CRS（分割 API 要求与几何同 CRS）
        line_pts = [self._to_layer_point(p) for p in self._cut_pts]
        line_pts = [pt for pt in line_pts if pt is not None]
        cleaned: list[QgsPoint] = []
        for pt in line_pts:
            if cleaned and abs(cleaned[-1].x() - pt.x()) < 1e-9 and abs(cleaned[-1].y() - pt.y()) < 1e-9:
                continue
            cleaned.append(pt)
        if len(cleaned) < 2:
            self._hint("切割线太短，无法分割；请按住左键画完整穿过要素的线")
            self._clear_line()
            return

        # 只分割双击选中的那一要素。实现走 QGIS 核心 splitFeatures（以图层
        # 当前选中为准，先临时选中再还原），而不是手动 changeGeometry +
        # addFeature：后者给新块发负临时 fid，提交到 GPKG 等强 fid 数据源时
        # OGR 会直接按负 ID 建要素，报 “Cannot create feature of ID …
        # already exists” / UNIQUE 冲突；核心 API 自行完成新 fid 分配，
        # 各数据源都能干净提交，且仍是单个撤销步骤。
        geom = feature.geometry()
        if geom is None or geom.isNull() or geom.isEmpty():
            self._clear_selection(hint=False)
            self._hint("所选要素几何为空，请重新双击选择")
            return
        # 拷贝几何预演一次，只为统计将产生的块数（提示文案），不落库
        work = QgsGeometry(geom)
        code, new_geoms, _tp = work.splitGeometry(cleaned, False, True)
        parts = [work, *list(new_geoms)]
        valid = [
            part
            for part in parts
            if part is not None and not part.isNull() and not part.isEmpty()
        ]
        if code != Qgis.GeometryOperationResult.Success or len(valid) < 2:
            # 线没完整穿过边界（NothingHappened 等）→ 切不开，保留选中可重画
            self._hint("所选要素未被切开：切割线需完整穿过要素边界")
            self._clear_line()
            return
        prev_sel = set(layer.selectedFeatureIds())
        # 用曲线重载（QgsCurve）避开旧版 Iterable[QgsPointXY] 重载在 PyQt 的
        # 过载解析问题：直接传 list[QgsPoint] 会先撞上已废弃的 QgsPointXY
        # 重载并抛 TypeError，曲线重载两版本签名一致且不废弃。
        cut_line = QgsLineString(cleaned)
        layer.beginEditCommand("线分割")
        try:
            layer.select([fid])  # 核心 API 只切当前选中的要素
            code, _split_pts = layer.splitFeatures(cut_line, False, False)
            # 还原调用前的图层选中状态（本工具用自绘高亮，不长期占用图层选择）
            if prev_sel:
                layer.select(prev_sel)
            else:
                layer.removeSelection()
        except RuntimeError:
            layer.cancelEditCommand()
            self._hint("分割失败：图层编辑 API 异常")
            return
        if code != Qgis.GeometryOperationResult.Success:
            layer.cancelEditCommand()
            self._hint("所选要素未被切开：切割线需完整穿过要素边界")
            self._clear_line()
            return
        layer.endEditCommand()
        # 新增 / 改了要素，空间索引缓存失效 → 清掉让下次命中重建
        clear_spatial_index(layer)
        self._clear_line()
        self._clear_selection(hint=False)
        self._hint(f"已按切割线分割为 {len(valid)} 个要素（可 Ctrl+Z 撤销）；双击选择下一要素")
        # 编辑命令栈外刷新画布（仓库约束：事件栈内不直接 refresh）
        QTimer.singleShot(0, self._safe_refresh)

    # -------------------------------------------------- 图层 / 坐标

    def _current_layer(self) -> QgsVectorLayer | None:
        """当前编辑图层；切换图层时清空未完成切割线（防跨层误分割）。"""
        layer = self._layer_getter()
        if layer is None or layer.type() != layer.VectorLayer:
            return None
        if self._layer is not None and layer is not self._layer:
            self._clear_line()
            self._clear_selection(hint=False)
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

    # -------------------------------------------------- 坐标换算

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

    def _px_distance(self, a: QgsPointXY, b: QgsPointXY) -> float:
        """两个地图坐标点的画布像素距离（采样去重用）。"""
        mupp = self.canvas.mapSettings().mapUnitsPerPixel()
        if mupp <= 0:
            return float("inf")
        return ((a.x() - b.x()) ** 2 + (a.y() - b.y()) ** 2) ** 0.5 / mupp

    def _to_canvas_geom(self, layer: QgsVectorLayer, geom: QgsGeometry) -> QgsGeometry:
        """图层 CRS 几何 → 画布 CRS 几何（CRS 相同 / 无效时恒等）。"""
        src = layer.crs()
        dst = self.canvas.mapSettings().destinationCrs()
        if not src.isValid() or not dst.isValid() or src == dst:
            return geom
        try:
            xform = QgsCoordinateTransform(
                src, dst, self.canvas.project() or QgsProject.instance()
            )
            g = QgsGeometry(geom)
            g.transform(xform)
            return g
        except Exception:
            return geom

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
            return QgsPoint(map_pt)


__all__ = ["SplitFeatureTool"]
