"""后台瓦片预热：解决天地图 XYZ 底图拖动/放大后白屏。

背景
----
XYZ（天地图）底图渲染是"同步取瓦片"的：渲染线程必须先把视口内所有瓦片
取回（未命中缓存的走网络请求，浏览器端 ``tk`` 还有 QPS 限制）才会出图；
QGIS 4 画布在渲染进行期间显示纯白。于是拖动/缩放后一旦新视口有瓦片不在
缓存，渲染就被网络卡住，白屏持续到瓦片到齐才恢复 —— 矢量图层渲染不依赖
网络、瞬间完成，所以只有加了天地图底图才出现白屏（与分屏工具无关）。

思路
----
在画布渲染完成（``renderComplete``）后，用离屏渲染器在后台线程把**当前视口
四周 8 个相邻视口**提前渲染一遍，把瓦片拉进 ``QgsTileCache``。用户平移过去
时命中缓存 → 渲染不再阻塞网络 → 不白屏（实测：预热后平移 0 白屏，未预热
1.5s 白屏）。

实现要点
--------
* 离屏渲染用 :class:`QgsMapRendererCustomPainterJob`，在独立的 daemon
  worker 线程里 ``start() + waitForFinished()``：阻塞的是 worker 线程，
  不碰 UI 线程，也不依赖 Qt 事件栈（不调用 ``canvas.refresh()``）。
* 预热渲染的**输出尺寸必须与画布一致**（``canvas.mapSettings().outputSize()``）：
  XYZ provider 按渲染分辨率选择瓦片级别 z，尺寸变小会选低一级 z，预热的
  就不是用户将来实际要用的那批瓦片。
* 画布渲染中（``isDrawing()``，含白屏期）不调度预热，避免和正在拉瓦片的
  画布渲染抢带宽；``renderComplete`` 之后用 800ms 防抖，连续拖动/缩放期间
  不打扰。
* 8 个相邻视口串行渲染（不并发），并对每个 extent 的瓦片量做估算上限，
  避免在全景等低级别下把上千张瓦片拉进缓存。
* 同一批 extent 只预热一次（量化去重，容量封顶），进程内不会重复拉。
"""

from __future__ import annotations

import math
import threading
from collections import OrderedDict, deque
from typing import Optional

from PyQt6.QtCore import QObject, QTimer
from PyQt6.QtGui import QColor, QImage, QPainter

from app.infra.logging import get_logger

logger = get_logger(__name__)

# Web 墨卡托世界宽（米）= 2*pi*6378137，与 QGIS xyz 瓦片服务一致
_WEB_MERCATOR_WORLD = 40075016.68557849
# 先四个正方向，再四个角（同一次预热按此顺序渲染，正方向最先就绪）
_NEIGHBOR_OFFSETS = [
    (0, -1), (0, 1), (-1, 0), (1, 0),
    (-1, -1), (1, -1), (-1, 1), (1, 1),
]


class _PrefetchWorker(threading.Thread):
    """后台线程：串行离屏渲染一组 extent，把瓦片拉进瓦片缓存。

    主线程随时可 :meth:`submit` 替换待渲染列表（只保留最新一轮），线程安全
    由 ``Condition`` 保证；单个 extent 渲染失败只记 debug 日志，不影响主流程。
    """

    def __init__(self, project) -> None:
        super().__init__(daemon=True, name="TilePrefetchWorker")
        self._project = project
        self._template = None
        self._queue: deque = deque()
        self._cv = threading.Condition()
        self._running = True

    def submit(self, items, template_settings) -> None:
        """替换待渲染列表为 ``items``（[(layer, extent), ...]），并刷新渲染模板。

        ``template_settings`` 是画布 ``mapSettings()`` 的拷贝：xyz provider 只在
        层的源 CRS（EPSG:3857）下正确选瓦片，画布设置里已带好重投影所需的
        图层变换/变换上下文，直接复用才能保证预热的瓦片与画布将要请求的一致。
        """
        with self._cv:
            self._template = template_settings
            self._queue = deque(items)
            self._cv.notify()

    def stop(self) -> None:
        with self._cv:
            self._running = False
            self._queue.clear()
            self._cv.notify()

    def run(self) -> None:  # noqa: D401
        while True:
            with self._cv:
                while self._running and not self._queue:
                    self._cv.wait()
                if not self._running:
                    return
                item = self._queue.popleft()
            try:
                self._render_one(*item)
            except Exception as exc:  # noqa: BLE001
                logger.debug("tile prefetch render failed: %s", exc)

    def _render_one(self, layer, extent) -> None:
        from qgis.core import QgsMapRendererCustomPainterJob, QgsMapSettings

        ms = QgsMapSettings(self._template)  # 画布设置拷贝：投影/变换上下文与画布一致
        ms.setExtent(extent)
        ms.setLayers([layer])
        ms.setBackgroundColor(QColor(0, 0, 0))
        try:
            ms.setFlag(QgsMapSettings.Flag.RenderPartialOutput, False)
        except Exception:  # noqa: BLE001  API 差异兜底
            pass
        img = QImage(ms.outputSize(), QImage.Format.Format_RGB32)
        img.fill(QColor(0, 0, 0))
        painter = QPainter(img)
        job = QgsMapRendererCustomPainterJob(ms, painter)
        job.start()
        job.waitForFinished()
        painter.end()
        # img/job 出作用域即释放；瓦片已进缓存，渲染结果丢弃


class TilePrefetcher(QObject):
    """挂在 :class:`QgsMapCanvas` 上的后台瓦片预热器（见模块 docstring）。

    用法：主窗口创建画布后 ``TilePrefetcher(canvas, project=project, parent=self)``
    即可，无需手动启停；``renderComplete`` 自动驱动，画布销毁自动清理。
    """

    def __init__(
        self,
        canvas,
        project=None,
        delay_ms: int = 800,
        max_round_tiles: int = 128,
        max_extent_tiles: int = 96,
        parent: Optional[QObject] = None,
    ) -> None:
        super().__init__(parent)
        self._canvas = canvas
        self._project = project
        self._delay_ms = delay_ms
        self._max_round_tiles = max_round_tiles
        self._max_extent_tiles = max_extent_tiles
        # 已预热过的 extent（量化去重，容量封顶）
        self._done: "OrderedDict[str, None]" = OrderedDict()
        self._done_cap = 256
        self._worker: Optional[_PrefetchWorker] = None
        self._timer = QTimer(self)
        self._timer.setSingleShot(True)
        self._timer.setInterval(delay_ms)
        self._timer.timeout.connect(self._schedule_round)
        canvas.renderComplete.connect(self._on_render_complete)
        canvas.destroyed.connect(self.shutdown)

    # ----------------------------------------------------------- 事件

    def _on_render_complete(self) -> None:
        self._timer.start()

    def _schedule_round(self) -> None:
        canvas = self._canvas
        if canvas is None:
            return
        if getattr(canvas, "isDrawing", lambda: False)():
            # 画布渲染中（含白屏期）：不抢带宽，等下一次 renderComplete 再调度
            return
        try:
            from qgis.core import QgsMapSettings

            settings = canvas.mapSettings()
            extent = settings.extent()
            if not extent or extent.width() <= 0 or extent.height() <= 0:
                return
            layers = [l for l in settings.layers() if _is_xyz_layer(l)]
            if not layers:
                return
            viewport_tiles = self._estimate_tiles(settings, extent)
            if viewport_tiles is None:
                return
            if viewport_tiles * len(_NEIGHBOR_OFFSETS) > self._max_round_tiles:
                logger.debug(
                    "tile prefetch skipped: viewport tiles=%s 超限", viewport_tiles
                )
                return
            items, added = self._build_items(settings, extent, layers[0])
            if not items:
                return
            if self._worker is None:
                self._worker = _PrefetchWorker(self._project or canvas.project())
                self._worker.start()
            self._worker.submit(items, QgsMapSettings(settings))
            if added:
                logger.info(
                    "tile prefetch: 预取 %d 个相邻视口（当前视口约 %d 瓦片）",
                    added, viewport_tiles,
                )
        except Exception as exc:  # noqa: BLE001
            logger.debug("tile prefetch schedule failed: %s", exc)

    def _build_items(self, settings, extent, layer):
        from qgis.core import QgsRectangle

        w, h = extent.width(), extent.height()
        items = []
        added = 0
        for dx, dy in _NEIGHBOR_OFFSETS:
            e = QgsRectangle(
                extent.xMinimum() + dx * w,
                extent.yMinimum() + dy * h,
                extent.xMaximum() + dx * w,
                extent.yMaximum() + dy * h,
            )
            key = _extent_key(e)
            if key in self._done:
                continue
            n = self._estimate_tiles(settings, e)
            if n is None or n > self._max_extent_tiles:
                continue
            items.append((layer, e))
            self._remember(key)
            added += 1
        return items, added

    # ----------------------------------------------------------- 估算 / 去重

    def _estimate_tiles(self, settings, extent) -> Optional[int]:
        """估算 ``extent`` 在当前画布分辨率下需要的 XYZ 瓦片数（近似，仅作限额）。

        XYZ 瓦片在 Web 墨卡托（EPSG:3857）下组织，把 extent 变换到 3857 后按
        画布分辨率推算出 provider 会选的级别 z，再数瓦片。
        """
        try:
            from qgis.core import (
                QgsCoordinateReferenceSystem,
                QgsCoordinateTransform,
            )

            size = settings.outputSize()
            if size.width() <= 0 or size.height() <= 0:
                return None
            dest = settings.destinationCrs()
            if dest.authid() == "EPSG:3857":
                w3857, h3857 = extent.width(), extent.height()
            else:
                ct = QgsCoordinateTransform(
                    dest,
                    QgsCoordinateReferenceSystem("EPSG:3857"),
                    self._project or settings.project(),
                )
                e = ct.transformBoundingBox(extent)
                w3857, h3857 = e.width(), e.height()
            if w3857 <= 0 or h3857 <= 0:
                return None
            mpp = w3857 / size.width()  # 3857 下每像素米数（与画布渲染一致）
            if mpp <= 0:
                return None
            z = math.log2(_WEB_MERCATOR_WORLD / (256.0 * mpp))
            if z < 0:
                z = 0.0
            tile_w = _WEB_MERCATOR_WORLD / (2.0 ** z)
            return max(1, math.ceil(w3857 / tile_w) * math.ceil(h3857 / tile_w))
        except Exception:  # noqa: BLE001
            return None

    def _remember(self, key: str) -> None:
        self._done[key] = None
        self._done.move_to_end(key)
        while len(self._done) > self._done_cap:
            self._done.popitem(last=False)

    # ----------------------------------------------------------- 清理

    def shutdown(self) -> None:
        """停 worker 并解绑（画布销毁时自动调用；也可手动调）。"""
        if self._worker is not None:
            self._worker.stop()
            self._worker.join(timeout=2.0)
            self._worker = None
        self._canvas = None


def _is_xyz_layer(layer) -> bool:
    """判断图层是否为 XYZ 瓦片图层（天地图即此类）。"""
    from qgis.core import QgsMapLayer

    if layer.type() != QgsMapLayer.RasterLayer:
        return False
    try:
        return layer.source().startswith("type=xyz")
    except Exception:  # noqa: BLE001
        return False


def _extent_key(extent) -> str:
    """把 extent 量化成字符串 key，用于"同一位置只预热一次"去重。"""
    unit = 1e-4
    return "|".join(
        str(round(v / unit))
        for v in (
            extent.xMinimum(),
            extent.yMinimum(),
            extent.width(),
            extent.height(),
        )
    )


__all__ = ["TilePrefetcher"]
