"""ArcMap 风格「I 查询」地图工具。

与现有「属性」识别工具并存：本工具把一次点击命中的**全部**要素交给
:class:`app.ui.docks.i_query_dock.IQueryDock`，在 ArcMap Identify 风格的面板里
按图层分组展示；面板的「识别来源」可限制为最顶层 / 可见 / 全部 / 指定图层。
"""

from __future__ import annotations

from typing import Optional

from qgis.core import QgsCoordinateTransform, QgsCoordinateTransformContext, QgsRectangle
from qgis.gui import QgsMapCanvas, QgsMapToolIdentify

from app.core.domain.identify import FeatureIdentifyService, IdentifyResult
from app.ui.common.feature_highlight import FeatureHighlighter

#: 定位要素时四周留的边距倍数（与属性表双击定位一致）
_ZOOM_PADDING = 1.2


class IQueryTool(QgsMapToolIdentify):
    """I 查询工具：点击地图，在结果面板里按图层列出命中要素。"""

    def __init__(self, canvas: QgsMapCanvas, parent: Optional[object] = None, dock=None) -> None:
        super().__init__(canvas)
        self.canvas = canvas
        self.parent_window = parent
        self.dock = dock
        self.identify_service = FeatureIdentifyService()
        self.highlight = None
        #: 最后一次点击的画布坐标：切换「识别来源」时在原地重查
        self.last_point = None
        #: 双击定位用的闪烁件（绑定当前图层，换层时重建）
        self._flasher = None
        self._flasher_layer = None

    # ------------------------------------------------------------ 地图点击

    def canvasReleaseEvent(self, event) -> None:  # noqa: N802 —— QGIS 命名
        # Qt6 下 QgsMapMouseEvent 不再有 x()/y()，用 pixelPoint() 取画布像素坐标
        self.last_point = event.pixelPoint()
        self._identify_at(self.last_point)

    def reidentify(self) -> None:
        """在最后一次点击的位置重新识别（「识别来源」变化后调用）。"""
        if self.last_point is not None:
            self._identify_at(self.last_point)

    def _identify_at(self, point) -> None:
        mode = self.IdentifyMode.TopDownAll
        layers = None
        if self.dock is not None:
            source, layers = self.dock.identify_source()
            if source == "topmost":
                mode = self.IdentifyMode.TopDownStopAtFirst
            elif source == "visible":
                mode = self.IdentifyMode.TopDownAll
                layers = None
        results = self.identify_service.identify(
            self.canvas,
            point.x(),
            point.y(),
            mode,
            layers=layers,
        )
        if results:
            self._show_highlight(results[0])
        else:
            self.clear_highlight()
        if self.dock is not None:
            self.dock.show_results(results, self._location_text(point))

    def _location_text(self, point) -> str:
        """点击点的地图坐标（与画布当前坐标系一致）。"""
        try:
            pt = self.canvas.mapSettings().mapToPixel().toMapCoordinates(int(point.x()), int(point.y()))
        except RuntimeError:
            return ""
        return f"X: {pt.x():.6f}  Y: {pt.y():.6f}"

    # ------------------------------------------------------------ 定位 + 闪烁

    def locate_and_flash(self, result: IdentifyResult) -> None:
        """双击结果树里的要素：缩放到它并在图上闪烁几下。"""
        self._zoom_to_result(result)
        self._flash_result(result)

    def _zoom_to_result(self, result: IdentifyResult) -> None:
        feature = result.feature
        geom = feature.geometry() if feature is not None else None
        if geom is None or geom.isNull() or geom.isEmpty():
            return
        extent = self._zoom_extent(geom.boundingBox(), result.layer)
        # 优先走主窗口已有的「按图层 CRS 转画布 CRS」缩放（含脏几何回退）
        zoomer = getattr(self.parent_window, "zoom_to_layer_extent_transform", None)
        if callable(zoomer):
            try:
                zoomer(extent, result.layer.crs())
                return
            except Exception:  # noqa: BLE001 —— 回退到下面自己转
                pass
        try:
            src = result.layer.crs()
            dest = self.canvas.mapSettings().destinationCrs()
            if src != dest:
                transform = QgsCoordinateTransform(src, dest, QgsCoordinateTransformContext())
                extent = transform.transformBoundingBox(extent)
            self.canvas.setExtent(extent)
            self.canvas.refresh()
        except RuntimeError:
            pass

    def _zoom_extent(self, bbox, layer) -> QgsRectangle:
        """把要素包围盒撑成适合缩放的矩形（单点 / 共线也要有可见跨度）。"""
        size = self.canvas.size()
        aspect = (size.width() / size.height()) if size.height() else 1.0
        width, height = bbox.width(), bbox.height()
        if width <= 0 and height <= 0:
            # 单点：按图层范围的 1/50 给个跨度；图层本身退化就退回 1
            ext = layer.extent()
            base = max(ext.width(), ext.height()) / 50.0 if ext is not None and not ext.isNull() else 1.0
            if base <= 0:
                base = 1.0
            height = base
            width = base * aspect
        elif width <= 0:
            width = height * aspect
        elif height <= 0:
            height = width / aspect
        else:
            width *= _ZOOM_PADDING
            height *= _ZOOM_PADDING
        center = bbox.center()
        return QgsRectangle(
            center.x() - width / 2.0,
            center.y() - height / 2.0,
            center.x() + width / 2.0,
            center.y() + height / 2.0,
        )

    def _flash_result(self, result: IdentifyResult) -> None:
        """在选中要素上闪几下（洋红色，与红色识别高亮区分）。"""
        if result.is_raster or result.feature is None:
            return  # 栅格没有 QgsVectorLayer.geometryType()，不能走橡皮筋闪烁
        try:
            flasher = self._ensure_flasher(result.layer)
            if flasher is not None:
                flasher.flash(result.fid, feature=result.feature)
        except RuntimeError:
            pass

    def _ensure_flasher(self, layer):
        if self._flasher is None or self._flasher_layer is not layer:
            self.clear_flash()
            self._flasher = FeatureHighlighter(self.canvas, layer, parent=self)
            # 闪完收掉：要素本身已有识别高亮，再留一个洋红框会碍事
            self._flasher.finished.connect(self._flasher.clear)
            self._flasher_layer = layer
        return self._flasher

    def clear_flash(self) -> None:
        flasher, self._flasher = self._flasher, None
        self._flasher_layer = None
        if flasher is not None:
            try:
                flasher.close()
            except Exception:  # noqa: BLE001
                pass

    # ------------------------------------------------------------ 高亮

    def highlight_result(self, result: IdentifyResult) -> None:
        """高亮指定结果（结果树切换时由主窗口调用）。"""
        self._show_highlight(result)

    def clear_highlight(self) -> None:
        if self.highlight is not None:
            try:
                self.canvas.scene().removeItem(self.highlight)
            except RuntimeError:  # 画布已销毁
                pass
            self.highlight = None

    def _show_highlight(self, result: IdentifyResult) -> None:
        from qgis.gui import QgsHighlight
        from qgis.PyQt.QtGui import QColor

        self.clear_highlight()
        if result.is_raster:
            # 栅格没有「要素高亮」API：用像元几何 + 图层几何重载，高亮点击的格子
            geom = result.feature.geometry() if result.feature is not None else None
            if geom is None or geom.isEmpty():
                return
            self.highlight = QgsHighlight(self.canvas, geom, result.layer)
            self.highlight.setColor(QColor(255, 0, 0))
            self.highlight.setFillColor(QColor(255, 0, 0, 50))
            self.highlight.setWidth(1)
            self.highlight.show()
            return
        self.highlight = self.identify_service.highlight(self.canvas, result.layer, result.feature)

    def deactivate(self) -> None:
        self.clear_highlight()
        self.clear_flash()
        super().deactivate()
