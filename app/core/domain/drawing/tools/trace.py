"""绘制追踪（QGIS「追踪/Tracing」的等效实现）。

语义与 QGIS 桌面一致：画线 / 画面时，若**前后两个顶点都吸附到已有要素**上，
两点之间沿已有要素几何的最短路径会被自动采进新几何——沿共享边追踪取点，
不必逐点描红。

实现：

- :class:`CanvasTracer` 包装 :class:`qgis.core.QgsTracer`：图层清单取画布
  snapping utils 的 extra snap layers（与磁性捕捉工具条注册的吸附目标同源，
  「追踪跟随捕捉」语义），CRS 用画布目标 CRS，图幅用画布当前范围外扩一倍；
  画布 ``extentsChanged``（平移 / 缩放）只**标记过期**，下次真正要追踪时才
  重建图（惰性，避免拖动时反复建图卡顿）。要素编辑后图可能过期，同样靠
  下一次平移 / 缩放或重开工具刷新（追踪失败静默降级为不插点，不阻断绘制）。
- 开关状态按画布登记（:func:`set_tracing_enabled` / :func:`tracing_enabled`），
  磁性捕捉工具条「追踪」按钮驱动；关闭时 :func:`trace_between` 恒返回空。
- :func:`trace_between` 返回**中间点**（不含两端点，调用方自己追加两端）；
  两端点必须落在既有要素几何上（吸附 match 已保证），落在图外则返回空——
  降级为普通取点，不报错。
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from qgis.core import QgsPointXY, QgsVectorLayer
    from qgis.gui import QgsMapCanvas

from qgis.core import (
    QgsCoordinateReferenceSystem,
    QgsProject,
    QgsRectangle,
    QgsTracer,
    QgsVectorLayer,
)
from qgis.PyQt.QtCore import QObject

from app.infra.logging import get_logger

logger = get_logger(__name__)

#: 每画布一个 :class:`CanvasTracer`（惰建，随画布生命周期复用）
_TRACERS: dict = {}

#: 每画布的追踪开关（磁性捕捉工具条「追踪」按钮写入）
_TRACING_ENABLED: dict = {}


def set_tracing_enabled(canvas: "QgsMapCanvas", enabled: bool) -> None:
    """设置画布的追踪开关（磁性捕捉工具条「追踪」按钮调用）。"""
    _TRACING_ENABLED[canvas] = bool(enabled)


def tracing_enabled(canvas: "QgsMapCanvas") -> bool:
    """画布是否开启了追踪（默认关闭）。"""
    return bool(_TRACING_ENABLED.get(canvas, False))


def _canvas_tracer(canvas: "QgsMapCanvas") -> "CanvasTracer":
    """画布对应的 :class:`CanvasTracer`（惰建单例）。"""
    tracer = _TRACERS.get(canvas)
    if tracer is None:
        tracer = CanvasTracer(canvas)
        _TRACERS[canvas] = tracer
    return tracer


def trace_between(
    canvas: "QgsMapCanvas", p1: "QgsPointXY", p2: "QgsPointXY"
) -> "list[QgsPointXY]":
    """两点之间沿已有要素几何的追踪中间点（不含两端点）。

    追踪未开启 / 无吸附图层 / 两点不都在既有要素上 / 找不到路径时返回空列表
    （调用方降级为普通取点，两点间不插值）。
    """
    if not tracing_enabled(canvas):
        return []
    if p1 is None or p2 is None:
        return []
    if abs(p1.x() - p2.x()) < 1e-12 and abs(p1.y() - p2.y()) < 1e-12:
        return []
    pts = _canvas_tracer(canvas).path(p1, p2)
    # findShortestPath 返回含两端点的完整路径；去掉端点（调用方自己加）
    return pts[1:-1] if len(pts) >= 2 else []


class CanvasTracer(QObject):
    """画布级追踪器：惰性维护 :class:`QgsTracer` 图并按需寻路。"""

    def __init__(self, canvas: "QgsMapCanvas") -> None:
        super().__init__(canvas)
        self._canvas = canvas
        self._tracer: QgsTracer | None = None
        self._stale = True
        # 平移 / 缩放 → 图过期（追踪范围随视口变化）；下次用时再重建
        canvas.extentsChanged.connect(self._mark_stale)

    def _mark_stale(self, *_args) -> None:
        self._stale = True

    # -------------------------------------------------- 图构建

    def _snap_layers(self) -> "list[QgsVectorLayer]":
        """追踪图层 = 磁性捕捉注册的吸附目标（extra snap layers）。"""
        try:
            utils = self._canvas.snappingUtils()
            if utils is None:
                return []
            layers = [lyr for lyr in utils.getExtraSnapLayers() if isinstance(lyr, QgsVectorLayer)]
            if layers:
                return layers
            # 兜底：未注册 extra snap layers 时用画布上的矢量图层
            return [
                lyr
                for lyr in (self._canvas.layers() or [])
                if isinstance(lyr, QgsVectorLayer) and lyr.isValid()
            ]
        except Exception:  # noqa: BLE001
            logger.warning("读取追踪图层失败", exc_info=True)
            return []

    def _rebuild(self) -> None:
        """按当前视口 / 图层清单重建追踪图（失败时保持未初始化 → 寻路返回空）。"""
        self._stale = False
        layers = self._snap_layers()
        crs: QgsCoordinateReferenceSystem = self._canvas.mapSettings().destinationCrs()
        if not layers or not crs.isValid():
            self._tracer = None
            return
        # 视口外扩一倍：相邻两次点击间小幅平移不致端点落图外
        ext: QgsRectangle = self._canvas.extent()
        if ext.isEmpty() or ext.width() <= 0 or ext.height() <= 0:
            # 画布还没刷过（无图层 / 未刷新）→ 用图层联合范围兜底
            ext = layers[0].extent()
            for layer in layers[1:]:
                ext.combineExtentWith(layer.extent())
            if ext.isEmpty():
                self._tracer = None
                return
        else:
            ext = QgsRectangle(ext)
            ext.grow(max(ext.width(), ext.height()))

        if self._tracer is None:
            self._tracer = QgsTracer()
        tracer = self._tracer
        tracer.setLayers(layers)
        project = self._canvas.project() or QgsProject.instance()
        tracer.setDestinationCrs(crs, project.transformContext())
        tracer.setExtent(ext)
        # 追踪路径可横穿相交要素的交点（QGIS「在交点上加点」语义）
        tracer.setAddPointsOnIntersectionsEnabled(True)
        try:
            if not tracer.init():
                logger.warning("追踪图构建失败: layers=%s", [l.name() for l in layers])
        except Exception:  # noqa: BLE001
            logger.exception("追踪图构建异常")

    # -------------------------------------------------- 寻路

    def path(self, p1: "QgsPointXY", p2: "QgsPointXY") -> "list[QgsPointXY]":
        """两点间最短追踪路径（含两端点）；失败返回空列表（不抛异常）。"""
        if self._stale or self._tracer is None or not self._tracer.isInitialized():
            self._rebuild()
        if self._tracer is None or not self._tracer.isInitialized():
            return []
        try:
            pts, err = self._tracer.findShortestPath(p1, p2)
            if err != QgsTracer.ErrNone or not pts:
                return []
            return list(pts)
        except Exception:  # noqa: BLE001 —— 追踪失败不阻断绘制
            logger.debug("追踪寻路失败（降级为普通取点）", exc_info=True)
            return []


__all__ = [
    "CanvasTracer",
    "set_tracing_enabled",
    "trace_between",
    "tracing_enabled",
]
