"""把选中要素画在**所有图层之上**的覆盖层。

QGIS 自带的选中高亮是在**该图层自己的绘制流程里**画的：图层渲染是按顺序一层层
画的，下层图层里被选中的图形会被上层图层（或同层后画的图形，比如后加的大面）
整块盖住——用户点了半天"明明选中了却看不见"。

``QgsMapCanvas`` 的橡皮筋（:class:`QgsRubberBand`）是在**所有图层画完之后**画的，
天然盖在最上层。本类把各可见矢量图层的选中几何收集到这样一个橡皮筋上，保证
"选中就看得见"。

高亮颜色**按几何类型分**（点 / 线 / 面各一条带子），取自
:class:`~app.ui.common.selection_style.SelectionStyleManager`，由「图层属性 → 选择」页
配置。因此这里**把自带高亮关掉**（``canvas.setSelectionColor(透明)``）：自带的那套只有
**一个全局色**，三类颜色不同时它会与我们的带子双重描边（面的半透明填充叠在自带底色上
看着发浑）。关掉后本覆盖层是唯一来源，颜色所见即所得。

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
from app.ui.common.selection_style import KINDS, SelectionStyleManager, kind_of_geometry_type, style_band

logger = get_logger(__name__)

#: 几何变化后重画覆盖层的防抖间隔（毫秒）
_GEOMETRY_REDRAW_DEBOUNCE_MS = 60

#: kind → 橡皮筋几何类型
_BAND_TYPES = {
    "point": QgsWkbTypes.GeometryType.PointGeometry,
    "line": QgsWkbTypes.GeometryType.LineGeometry,
    "polygon": QgsWkbTypes.GeometryType.PolygonGeometry,
}

#: 选中要素超过这个数就**不再自己画**覆盖层（改成让画布自带高亮去画）。
#: 自己画要把选中要素的几何一个个读出来，实测 14.8 万的多边形 GDB 图层要 ~10 秒，
#: 界面直接卡死；自带高亮在图层渲染里顺带画，代价只与可视范围有关。
MAX_OVERLAY_GEOMETRIES = 20000

#: 点的标记大小见 selection_style.POINT_BAND_ICON_SIZE（各高亮件共用同一套上色规则）


#: 点带子的标记尺寸已挪进 selection_style（各高亮件共用同一套上色规则）
def _kind_of_geometry(geom: QgsGeometry) -> str | None:
    """几何 → 高亮种类名（``point`` / ``line`` / ``polygon``）；认不出返回 ``None``。"""
    if geom is None:
        return None
    try:
        return kind_of_geometry_type(QgsWkbTypes.geometryType(geom.wkbType()))
    except (RuntimeError, AttributeError):
        return None


class SelectionOverlay(QObject):
    """各可见矢量图层的选中要素，统一画在最上层。"""

    def __init__(self, canvas, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self._canvas = canvas
        #: kind → 橡皮筋带子
        self._bands: dict[str, QgsRubberBand] = {}
        #: 当前设给画布自带高亮的颜色（None = 还没设过）；用于"值没变就别动"
        self._builtin_color: QColor | None = None
        self._project = None
        self._hooked: set[str] = set()
        # 几何变化的重画防抖：一次批量移动（移动要素 / 补缝…）会逐要素发
        # geometryChanged，逐个重建覆盖层太浪费
        self._redraw_timer = QTimer(self)
        self._redraw_timer.setSingleShot(True)
        self._redraw_timer.setInterval(_GEOMETRY_REDRAW_DEBOUNCE_MS)
        self._redraw_timer.timeout.connect(self.sync)

    @property
    def _band(self) -> QgsRubberBand | None:
        """兼容旧调用点：老脚本按"一条面带子"取 ``overlay._band``。

        改造后是三条带子（点 / 线 / 面），这里返回**面**那条——历来用它测的都是
        面图层（``verify_selection_overlay.py`` / ``verify_move_highlight.py`` /
        ``verify_round3.py`` / ``verify_round9.py``），语义不变。
        """
        return self._bands.get("polygon")


    # ------------------------------------------------------------ 生命周期

    def attach(self, project=None) -> None:
        """接管工程里所有矢量图层的 ``selectionChanged``，并跟随图层增删。

        同时**关掉画布自带的选中高亮**（设成全透明）并订阅高亮样式的变化 —— 见模块
        docstring：三类颜色不同时，自带的单一全局色会与我们双重描边。
        """
        self._project = project or QgsProject.instance()
        try:
            self._project.layersAdded.connect(self.sync)
            self._project.layersRemoved.connect(self.sync)
        except Exception:  # noqa: BLE001
            logger.exception("选择高亮层：连接工程图层增删信号失败")
        self._suppress_builtin_highlight(True)
        try:
            SelectionStyleManager.instance().changed.connect(self._on_style_changed)
        except Exception:  # noqa: BLE001
            logger.exception("选择高亮层：订阅高亮样式变化失败")
        # **可见图层集合一变就重算**：图层隐藏期间改了选中集，等它再勾选显示出来时
        # 没人重算过覆盖层，而自带高亮又被我们按成透明——于是"选中集覆盖整层"的图层
        # 会整层看不见（用户报过：属性表里 Ctrl+A 后把隐藏图层勾回来，地图上空的）。
        try:
            self._canvas.layersChanged.connect(self.sync)
        except Exception:  # noqa: BLE001
            logger.exception("选择高亮层：订阅画布可见图层变化失败")
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
        try:
            SelectionStyleManager.instance().changed.disconnect(self._on_style_changed)
        except Exception:  # noqa: BLE001
            pass
        try:
            self._canvas.layersChanged.disconnect(self.sync)
        except Exception:  # noqa: BLE001 —— 画布已销毁 / 本就未连
            pass
        self._suppress_builtin_highlight(False)
        for layer in self._layers():
            self._unhook(layer)
        self._hooked.clear()
        self._clear_band()

    def _suppress_builtin_highlight(self, suppress: bool) -> None:
        """开关画布自带的选中高亮（透明 = 关）。"""
        self._apply_builtin_highlight(not suppress, None)

    def _apply_builtin_highlight(self, on: bool, color=None) -> None:
        """设画布自带高亮的开关与颜色；**值没变就不动**（免得每次 sync 都触发重绘）。"""
        if self._canvas is None:
            return
        if on:
            target = QColor(color) if color is not None else QColor(255, 255, 0, 255)
        else:
            target = QColor(0, 0, 0, 0)
        if self._builtin_color is not None and target == self._builtin_color:
            return
        try:
            self._canvas.setSelectionColor(target)
            self._builtin_color = target
        except RuntimeError:
            pass  # 画布已销毁

    def _too_many_selected(self) -> bool:
        """选中的要素是不是多到不值得自己画（只数个数，不取几何）。"""
        total = 0
        for layer in self._layers():
            try:
                if kind_of_geometry_type(layer.geometryType()) is None:
                    continue
                total += len(layer.selectedFeatureIds())
            except RuntimeError:
                continue
            if total > MAX_OVERLAY_GEOMETRIES:
                return True
        return False

    def _use_builtin_highlight(self) -> None:
        """选中太多：藏掉自己的带子，改用画布自带高亮画。

        自带高亮只有一个全局色，取不到"按几何类型分开"，所以用**选中最多的那一类**
        的描边色顶上——视觉上仍是你配的颜色。
        """
        for band in self._bands.values():
            try:
                band.hide()
            except RuntimeError:
                pass
        color = None
        try:
            counts: dict[str, int] = {}
            for layer in self._layers():
                kind = kind_of_geometry_type(layer.geometryType())
                if kind is None:
                    continue
                counts[kind] = counts.get(kind, 0) + len(layer.selectedFeatureIds())
            if counts:
                dominant = max(counts, key=counts.get)
                color = SelectionStyleManager.instance().style(dominant).stroke_qcolor()
        except RuntimeError:
            color = None
        self._apply_builtin_highlight(True, color)

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

        canvas_crs = self._canvas.mapSettings().destinationCrs()
        project = self._canvas.project() or QgsProject.instance()

        # 先只**数**一遍（便宜）：选中元素太多时不在 Python 里逐个取几何 —— 取几何要
        # 把选中要素全读出来，实测 14.8 万的多边形 GDB 图层要 ~10 秒，界面直接卡住。
        # 这种情况交回 QGIS 自带高亮：它随图层渲染顺带画，代价只与可视范围有关。
        if self._too_many_selected():
            self._use_builtin_highlight()
            return
        self._suppress_builtin_highlight(True)

        # 按几何类型分桶：一次遍历、每类一次 setToGeometry（见下方性能注释）
        by_kind: dict[str, list[QgsGeometry]] = {kind: [] for kind in KINDS}
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
                    kind = _kind_of_geometry(geom)
                    if kind is None:
                        continue  # 无几何 / 认不出的类型，画不了
                    if xform is not None:
                        geom = QgsGeometry(geom)
                        geom.transform(xform)
                    by_kind[kind].append(geom)
            except RuntimeError:
                continue  # 图层 / 画布已销毁

        # 先按 z-order 建齐（polygon → line → point），后续逐个填几何
        self._ensure_bands()
        for kind in KINDS:
            band = self._ensure_band(kind)
            if band is None:
                continue
            geoms = by_kind[kind]
            try:
                if not geoms:
                    band.hide()
                    band.reset(_BAND_TYPES[kind])
                    continue
                # **一次设入**，不要逐个 ``addGeometry``：后者是**超线性**的——内存
                # 实测 5 千个选中要素 1.9 秒、2 万个 33 秒（QTBUG 级别的大坑），
                # 框选一大片时会把界面活活冻住几十秒，连事件循环都进不去。
                # 先合成一个多部件几何再设一次：同样两万个只要几十毫秒。
                combined = QgsGeometry.collectGeometry(geoms)
                combined.convertToMultiType()
                band.setToGeometry(combined, None)
                band.show()
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

    def _ensure_band(self, kind: str) -> QgsRubberBand | None:
        """取 / 建某一类的带子。

        建的时候按 **polygon → line → point** 的顺序（见 :meth:`_ensure_bands`），
        让点的带子最后建、盖在线 / 面之上，免得面的半透明填充糊住点。
        """
        if self._canvas is None:
            return None
        band = self._bands.get(kind)
        if band is None:
            try:
                band = QgsRubberBand(self._canvas, _BAND_TYPES[kind])
            except (RuntimeError, KeyError):
                return None
            self._bands[kind] = band
            self._style_band(kind, band)
        return band

    def _ensure_bands(self) -> None:
        """按 z-order 建齐三条带子（后建的画在上面）。"""
        for kind in ("polygon", "line", "point"):
            self._ensure_band(kind)

    def _style_band(self, kind: str, band: QgsRubberBand) -> None:
        """按该类的样式给带子上色（规则见 :func:`selection_style.style_band`）。"""
        style_band(band, kind, SelectionStyleManager.instance().style(kind))

    def _on_style_changed(self) -> None:
        """高亮样式改了：三条带子换色重画。"""
        for kind, band in self._bands.items():
            self._style_band(kind, band)
        self.sync()

    def _clear_band(self) -> None:
        for kind, band in self._bands.items():
            try:
                band.reset(_BAND_TYPES[kind])
            except RuntimeError:
                pass
        self._bands.clear()


__all__ = ["SelectionOverlay"]
