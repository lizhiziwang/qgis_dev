"""把选中要素画在**所有图层之上**的覆盖层。

QGIS 自带的选中高亮是在**该图层自己的绘制流程里**画的：图层渲染是按顺序一层层
画的，下层图层里被选中的图形会被上层图层（或同层后画的图形，比如后加的大面）
整块盖住——用户点了半天"明明选中了却看不见"。

``QgsMapCanvas`` 的橡皮筋（:class:`QgsRubberBand`）是在**所有图层画完之后**画的，
天然盖在最上层。本类把各可见矢量图层的选中几何收集到这样一个橡皮筋上，保证
"选中就看得见"。

自带的选中高亮**不关掉**：它画在图层内、我们画在最上，颜色一致，重叠看不出来；
关掉它反而会让"本层没被遮住的选中"失去高亮。

用法::

    overlay = SelectionOverlay(canvas)
    overlay.attach(project)      # 接管工程里所有矢量图层的 selectionChanged
    overlay.sync()               # 也可以手动刷（如工程图层增删后）
    overlay.detach()             # 窗口销毁 / 换画布时收尾
"""

from __future__ import annotations

from qgis.core import QgsCoordinateTransform, QgsGeometry, QgsProject, QgsVectorLayer, QgsWkbTypes
from qgis.gui import QgsRubberBand
from qgis.PyQt.QtCore import QObject, QTimer
from qgis.PyQt.QtGui import QColor

from app.infra.logging import get_logger

logger = get_logger(__name__)

#: 几何变化后重画覆盖层的防抖间隔（毫秒）
_GEOMETRY_REDRAW_DEBOUNCE_MS = 60

#: 覆盖层颜色 = QGIS 默认选中黄，与自带高亮一致（重叠时看不出双重描边）
_OVERLAY_COLOR = QColor(255, 255, 0, 255)


class SelectionOverlay(QObject):
    """各可见矢量图层的选中要素，统一画在最上层。"""

    def __init__(self, canvas, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self._canvas = canvas
        self._band: QgsRubberBand | None = None
        self._project = None
        self._hooked: set[str] = set()
        # 几何变化的重画防抖：一次批量移动（移动要素 / 补缝…）会逐要素发
        # geometryChanged，逐个重建覆盖层太浪费
        self._redraw_timer = QTimer(self)
        self._redraw_timer.setSingleShot(True)
        self._redraw_timer.setInterval(_GEOMETRY_REDRAW_DEBOUNCE_MS)
        self._redraw_timer.timeout.connect(self.sync)

    # ------------------------------------------------------------ 生命周期

    def attach(self, project=None) -> None:
        """接管工程里所有矢量图层的 ``selectionChanged``，并跟随图层增删。"""
        self._project = project or QgsProject.instance()
        try:
            self._project.layersAdded.connect(self.sync)
            self._project.layersRemoved.connect(self.sync)
        except Exception:  # noqa: BLE001
            logger.exception("选择高亮层：连接工程图层增删信号失败")
        self.sync()

    def detach(self) -> None:
        """断开全部连接并清掉覆盖层（窗口收尾）。"""
        project = self._project
        self._project = None
        if project is not None:
            for signal in (project.layersAdded, project.layersRemoved):
                try:
                    signal.disconnect(self.sync)
                except Exception:  # noqa: BLE001 —— 收尾场景属正常
                    pass
        for layer in self._layers():
            self._unhook(layer)
        self._hooked.clear()
        self._clear_band()

    # ------------------------------------------------------------ 刷新

    def sync(self, *_args) -> None:
        """重建覆盖层：当前所有**可见**矢量图层的选中要素。"""
        if self._canvas is None:
            return
        # 挂载按**工程里的矢量图层**来，不按画布可见图层：``layersAdded`` 触发时
        # 画布的图层列表还没同步（QgsLayerTreeMapCanvasBridge 是异步的），按画布
        # 图层挂会漏掉刚加进来的那一层——它之后就没被监听，选中永远刷不出来。
        hook_targets = self._project_vector_layers()
        alive = {layer.id() for layer in hook_targets}
        for layer_id in list(self._hooked - alive):
            stale = QgsProject.instance().mapLayer(layer_id)
            if stale is not None:
                self._unhook(stale)
            self._hooked.discard(layer_id)
        for layer in hook_targets:
            if layer.id() not in self._hooked:
                self._hook(layer)

        band = self._ensure_band()
        if band is None:
            return
        try:
            band.reset(QgsWkbTypes.PolygonGeometry)
        except RuntimeError:
            return
        canvas_crs = self._canvas.mapSettings().destinationCrs()
        project = self._canvas.project() or QgsProject.instance()
        geoms: list[QgsGeometry] = []
        for layer in self._layers():
            fids = layer.selectedFeatureIds()
            if not fids:
                continue
            try:
                xform = None
                if layer.crs() != canvas_crs:
                    xform = QgsCoordinateTransform(layer.crs(), canvas_crs, project)
                for feature in layer.selectedFeatures():
                    geom = feature.geometry()
                    if geom is None or geom.isNull() or geom.isEmpty():
                        continue
                    if QgsWkbTypes.geometryType(geom.wkbType()) != QgsWkbTypes.PolygonGeometry:
                        continue  # 本覆盖层是面类型的带子，点 / 线画不了
                    if xform is not None:
                        geom = QgsGeometry(geom)
                        geom.transform(xform)
                    geoms.append(geom)
            except RuntimeError:
                continue  # 图层 / 画布已销毁
        try:
            if geoms:
                # **一次设入**，不要逐个 ``addGeometry``：后者是**超线性**的——内存
                # 实测 5 千个选中要素 1.9 秒、2 万个 33 秒（QTBUG 级别的大坑），
                # 框选一大片时会把界面活活冻住几十秒，连事件循环都进不去。
                # 先合成一个多部件几何再设一次：同样两万个只要几十毫秒。
                combined = QgsGeometry.collectGeometry(geoms)
                combined.convertToMultiType()
                band.setToGeometry(combined, None)
                band.show()
            else:
                band.hide()
        except RuntimeError:
            pass

    # ------------------------------------------------------------ 内部

    def _layers(self) -> list[QgsVectorLayer]:
        """画布上当前可见的矢量图层（``canvas.layers()`` 只含渲染图层）。"""
        if self._canvas is None:
            return []
        try:
            return [
                layer
                for layer in self._canvas.layers()
                if isinstance(layer, QgsVectorLayer) and layer.isValid()
            ]
        except RuntimeError:
            return []

    def _project_vector_layers(self) -> list[QgsVectorLayer]:
        """工程里的全部矢量图层（挂选中信号的依据，见 :meth:`sync`）。"""
        try:
            project = self._project or QgsProject.instance()
            return [
                item
                for item in project.mapLayers().values()
                if isinstance(item, QgsVectorLayer) and item.isValid()
            ]
        except RuntimeError:
            return []

    def _hook(self, layer: QgsVectorLayer) -> None:
        """挂选中信号 + **几何变化 / 要素删除**信号。

        只挂 ``selectionChanged`` 是不够的，两种常见操作都不发它：

        - **移动要素**（以及节点编辑、补缝）改的是几何、选中集没变；
        - **删除要素**：``deleteFeatures`` 会把被删的 fid 直接从选中集里摘掉，而且
          摘的时候**不发** ``selectionChanged`` —— 于是覆盖层不重画，带子上还留着
          **删除前那份几何**画在所有图层之上，用户看着就像"没删掉"（高亮把原图形
          挡得严严实实）。

        这两种都得靠各自的信号补上。
        """
        try:
            layer.selectionChanged.connect(self.sync)
            layer.geometryChanged.connect(self._schedule_redraw)
            try:
                layer.featuresDeleted.connect(self._schedule_redraw)
            except AttributeError:  # 该构建没有这个信号：退回只靠几何 / 选中信号
                logger.debug("选择高亮层：本构建没有 featuresDeleted 信号")
            self._hooked.add(layer.id())
        except Exception:  # noqa: BLE001
            logger.warning("选择高亮层：连接 %s 的信号失败", layer.name(), exc_info=True)

    def _unhook(self, layer: QgsVectorLayer) -> None:
        pairs = [
            (layer.selectionChanged, self.sync),
            (layer.geometryChanged, self._schedule_redraw),
        ]
        if hasattr(layer, "featuresDeleted"):
            pairs.append((layer.featuresDeleted, self._schedule_redraw))
        for signal, slot in pairs:
            try:
                signal.disconnect(slot)
            except Exception:  # noqa: BLE001 —— 图层销毁等收尾场景属正常
                pass

    def _schedule_redraw(self, *_args) -> None:
        """几何变了：推迟一拍重画（合并同一次批量操作里的多次信号）。"""
        self._redraw_timer.start()

    def _ensure_band(self) -> QgsRubberBand | None:
        if self._canvas is None:
            return None
        if self._band is None:
            try:
                band = QgsRubberBand(self._canvas, QgsWkbTypes.PolygonGeometry)
                band.setColor(_OVERLAY_COLOR)
                band.setWidth(2)
                band.setFillColor(QColor(255, 255, 0, 60))
                self._band = band
            except RuntimeError:
                return None
        return self._band

    def _clear_band(self) -> None:
        if self._band is not None:
            try:
                self._band.reset(QgsWkbTypes.PolygonGeometry)
            except RuntimeError:
                pass
            self._band = None


__all__ = ["SelectionOverlay"]
