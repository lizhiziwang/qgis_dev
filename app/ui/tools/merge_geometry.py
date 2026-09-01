"""融合几何工具。

编辑工具条「融合几何」按钮激活的交互工具，把多个要素融合成一个：

- **右键单击**要素 → 加入融合集合（再次右键同一要素取消），选中的要素用
  红色高亮框标出；
- **左键双击** → 融合成一个**几何面**：几何 ``unaryUnion`` 求并（相邻/重叠
  要素合并公共边界，**保留全部原始顶点、不做简化**）；若结果是多部件
  （角对角 / 微缝隙 / 完全分离），按部件间距分级处理：
  - 间距小（≤ 包围盒短边 5%，角对角 / 微缝隙等矢量化误差）→ **微膨胀缝合**
    ``buffer(+w).buffer(-w)``：只在缝隙处补上窄带，面积几乎不变；
  - 间距大（真分离）→ **多边形凹包** ``concaveHullOfPolygons``（参照 JTS
    ConcaveHull 的 GEOS 移植）：输入多边形边界保持不变、只填充部件间
    间隙，实测**不丢面积**（GEOS 版 concave_hull 只保点不保面，会丢面积）；
  属性保留**第一个选中**要素的；
- **Esc** → 退出工具。

安全约束（仓库既定结论，见 ``swipe.py`` docstring）：不在 Qt 事件栈内调
``canvas.refresh()`` / ``unsetMapTool``，一律 ``QTimer.singleShot(0, ...)``
推迟出栈。

融合写进编辑缓冲（要求图层处于编辑态）：``beginEditCommand`` 把
``changeGeometry`` + ``deleteFeatures`` 合成**一个**撤销步骤，可整体撤销。
"""

from __future__ import annotations

from collections.abc import Callable

from qgis.PyQt.QtCore import QPointF, Qt, QTimer
from qgis.PyQt.QtGui import QColor
from qgis.core import QgsGeometry, QgsProject, QgsRectangle, QgsVectorLayer, QgsWkbTypes
from qgis.gui import QgsMapTool, QgsRubberBand

from app.core.domain.check.query import intersect_features
from app.core.domain.check.space_index import clear_spatial_index


class MergeGeometryTool(QgsMapTool):
    """右键选要素、左键双击融合的编辑工具。"""

    TOLERANCE_PX = 10  # 右键点击容差（像素）

    #: 部件间距 ≤ 包围盒短边 × 此比例 才做微膨胀缝合（角对角/微缝隙等
    #: 矢量化误差），更大间距视为真分离走多边形凹包
    STITCH_MAX_RATIO = 0.05
    #: 缝合桥宽：间距×0.6 + 短边×0.005（保证焊上且变形可忽略）
    STITCH_W_RATIO = (0.6, 0.005)
    #: 多边形凹包参数（0~1，越小越贴合点集）：concaveHullOfPolygons 是
    #: 参照 JTS ConcaveHull 的 GEOS 实现，多边形边界不变、只填间隙，
    #: 实测各场景均不丢面积
    CONCAVE_HULL_RATIO = 0.3

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
        #: 融合集合（保持选中顺序，第一个 = 融合后保留属性的要素）
        self._fids: list[int] = []
        #: 工具锁定的图层；切换图层时清空集合（防跨层 fid 误删）
        self._layer: QgsVectorLayer | None = None
        self._band_type: QgsWkbTypes.GeometryType | None = None
        self._band: QgsRubberBand | None = None
        self._init_band()
        self.setCursor(Qt.CursorShape.CrossCursor)

    # -------------------------------------------------- 工具生命周期

    def activate(self) -> None:
        self._init_band()
        super().activate()

    def deactivate(self) -> None:
        self._reset_band()
        super().deactivate()

    def keyPressEvent(self, event) -> None:  # noqa: N802
        if event.key() == Qt.Key.Key_Escape:
            event.ignore()
            # 不在键盘事件栈内 unsetMapTool，推迟出栈
            QTimer.singleShot(0, self._quit)
        else:
            super().keyPressEvent(event)

    def _quit(self) -> None:
        try:
            self.canvas.unsetMapTool(self)
        except RuntimeError:
            pass

    # -------------------------------------------------- 交互

    def canvasReleaseEvent(self, event) -> None:  # noqa: N802
        """右键单击：切换点击位置要素的选中态（加入 / 移出融合集合）。"""
        if event.button() != Qt.MouseButton.RightButton:
            return
        fid = self._hit_fid(event)
        if fid is None:
            return
        if fid in self._fids:
            self._fids.remove(fid)
            self._hint(f"已移出融合集合（当前 {len(self._fids)} 个），双击融合")
        else:
            self._fids.append(fid)
            self._hint(f"已选择 {len(self._fids)} 个要素，双击融合；右键再次点击取消")
        self._refresh_band()

    def canvasDoubleClickEvent(self, event) -> None:  # noqa: N802
        """左键双击：融合集合内所有要素为一个。"""
        if event.button() != Qt.MouseButton.LeftButton:
            return
        self._merge()

    # -------------------------------------------------- 命中测试

    def _hit_fid(self, event) -> int | None:
        """点击像素 → 命中要素 fid（10px 容差矩形求交；多要素命中取最上层）。"""
        layer = self._current_layer()
        if layer is None:
            return None
        try:
            pos = event.pixelPoint()
            # 真实 Qt 事件是整数 QPoint；测试 / 兼容路径可能传 QPointF
            if isinstance(pos, (QPointF,)):
                pos = pos.toPoint()
            map_pt = self.canvas.getCoordinateTransform().toMapCoordinates(pos)
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
        # 过滤已删除/失效的要素（空间索引缓存可能含已删 fid）
        fids = [fid for fid in fids if layer.getFeature(fid).isValid()]
        return fids[-1] if fids else None  # 后添加的要素通常绘制在上层

    # -------------------------------------------------- 融合

    def _merge(self) -> None:
        """把集合内要素融合成一个（unaryUnion 保形 + 保留首个要素属性）。"""
        layer = self._current_layer()
        if layer is None or len(self._fids) < 2:
            return
        if not layer.isEditable():
            self._hint("图层未处于编辑态，无法融合")
            return

        keep_fid: int | None = None
        geoms: list[QgsGeometry] = []
        delete_fids: list[int] = []
        for fid in self._fids:
            feature = layer.getFeature(fid)
            if not feature.isValid():
                continue
            geom = feature.geometry()
            if geom is not None and not geom.isNull() and not geom.isEmpty():
                geoms.append(geom)
                delete_fids.append(fid)
                # 保留第一个**有效**要素（防御：首个选中要素几何无效的极端情况）
                if keep_fid is None:
                    keep_fid = fid
        if len(geoms) < 2 or keep_fid is None:
            return

        merged = QgsGeometry.unaryUnion(geoms)
        if merged is None or merged.isEmpty():
            # 兜底：并集失败时退化为多几何收集，形状 100% 保留
            merged = QgsGeometry.collectGeometry(geoms)
        elif merged.isMultipart():
            merged = self._merge_multipart_to_single(merged)
        if merged is None or merged.isEmpty():
            return

        # 一个撤销步骤：换几何 + 删其余要素（保留第一个选中要素的属性）
        layer.beginEditCommand("融合几何")
        try:
            layer.changeGeometry(keep_fid, merged)
            layer.deleteFeatures(delete_fids[1:])
        except RuntimeError:
            layer.cancelEditCommand()
            self._hint("融合失败：图层编辑 API 异常")
            return
        layer.endEditCommand()

        count = len(delete_fids)
        self._fids = []
        self._refresh_band()
        # 结果要素高亮选中，便于确认
        layer.select([keep_fid])
        # 融合删除了要素，空间索引缓存失效 → 清掉让下次命中重建
        clear_spatial_index(layer)
        self._hint(f"已融合 {count} 个要素为一个；继续右键选择、双击融合")
        # 双击事件栈内不直接 refresh（仓库约束），推迟出栈后再刷新
        QTimer.singleShot(0, self._safe_refresh)

    def _safe_refresh(self) -> None:
        try:
            self.canvas.refresh()
        except RuntimeError:
            pass

    # -------------------------------------------------- 多部件融合

    def _parts_min_dist(self, multi: QgsGeometry) -> float:
        """多部件几何的部件间最小距离（两两顶点距离）。"""
        it = multi.constGet()
        n = it.numGeometries()
        # 深拷贝防所有权转移；单部件无意义
        parts = [
            QgsGeometry.fromWkt(it.geometryN(i).asWkt()) for i in range(n)
        ]
        best = float("inf")
        for i in range(n):
            for j in range(i + 1, n):
                d = parts[i].distance(parts[j])
                if d < best:
                    best = d
        return best

    def _merge_multipart_to_single(self, multi: QgsGeometry) -> QgsGeometry:
        """多部件几何融成单面：近距缝合，远距多边形凹包，凸包兜底。

        - **微膨胀缝合**（``buffer(+w).buffer(-w)``）：适用于角对角 / 微缝隙
          等矢量化误差（部件间距 ≤ 包围盒短边 5%），只在缝隙处补窄带，
          面积几乎不变（实测 extra≈0.001~0.2）；
        - **多边形凹包**（``concaveHullOfPolygons``）：真分离部件，参照 JTS
          ConcaveHull 的 GEOS 移植——输入多边形边界保持不变、只填充间隙，
          实测各场景均不丢面积（GEOS 版 ``concave_hull`` 只保证包含输入点、
          不保证包含多边形面积，会丢 50%~99%）；
        - **凸包兜底**：理论不可达（凹包失败时），数学上保证不丢面积。
        """
        bbox = multi.boundingBox()
        short = min(bbox.width(), bbox.height())
        dmin = self._parts_min_dist(multi)
        if dmin <= max(short * self.STITCH_MAX_RATIO, 1e-9):
            w = dmin * self.STITCH_W_RATIO[0] + short * self.STITCH_W_RATIO[1]
            stitched = multi.buffer(w, 8).buffer(-w, 8)
            if (
                stitched is not None
                and not stitched.isEmpty()
                and not stitched.isMultipart()
            ):
                return stitched
        # 真分离 / 缝合失败：多边形凹包（保形且不丢面积）。
        # concaveHullOfPolygons 是 QGIS 3.42+ 新 API（JTS ConcaveHull 移植）；
        # 3.40 及更早（如麒麟/conda 源默认的 3.40）没有 → 直接走凸包兜底。
        hull = None
        if hasattr(QgsGeometry, "concaveHullOfPolygons"):
            hull = QgsGeometry.concaveHullOfPolygons(
                multi, self.CONCAVE_HULL_RATIO, False
            )
        if hull is None or hull.isEmpty() or hull.isMultipart():
            return multi.convexHull()
        return hull

    # -------------------------------------------------- 图层 / 高亮

    def _current_layer(self) -> QgsVectorLayer | None:
        """当前编辑图层；切换图层时清空集合（防跨层 fid 误删）。"""
        layer = self._layer_getter()
        if layer is None or layer.type() != layer.VectorLayer:
            return None
        if self._layer is not None and layer is not self._layer:
            self._fids = []
            self._reset_band()
        self._layer = layer
        return layer

    def _init_band(self) -> None:
        """按图层几何类型建橡皮筋（点 / 线 / 面各用对应类型）。"""
        layer = self._layer_getter()
        if layer is None or layer.type() != layer.VectorLayer:
            return
        band_type = {
            QgsWkbTypes.PointGeometry: QgsWkbTypes.PointGeometry,
            QgsWkbTypes.LineGeometry: QgsWkbTypes.LineGeometry,
            QgsWkbTypes.PolygonGeometry: QgsWkbTypes.PolygonGeometry,
        }.get(layer.geometryType(), QgsWkbTypes.PolygonGeometry)
        if self._band is None or band_type != self._band_type:
            self._band_type = band_type
            self._band = QgsRubberBand(self.canvas, band_type)
            self._band.setColor(QColor(255, 0, 0, 160))
            self._band.setWidth(2)
            self._band.setFillColor(QColor(255, 0, 0, 40))

    def _refresh_band(self) -> None:
        if self._band is None or self._layer is None:
            return
        self._band.reset(self._band_type)
        for fid in self._fids:
            feature = self._layer.getFeature(fid)
            if feature.isValid() and not feature.geometry().isNull():
                self._band.addGeometry(feature.geometry(), self._layer.crs())
        if self._fids:
            self._band.show()
        else:
            self._band.hide()

    def _reset_band(self) -> None:
        if self._band is not None:
            try:
                self._band.reset(self._band_type)
                self._band.hide()
            except RuntimeError:
                pass


__all__ = ["MergeGeometryTool"]
