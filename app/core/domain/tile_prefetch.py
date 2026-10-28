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
* worker 渲染的是图层的**离屏克隆**（:meth:`_clone_for`），不是画布上的
  工程图层；渲染模板（``QgsMapSettings``）也清空图层列表后再交给 worker。
  原因：图层随时可能被删除（移除底图、工程切换），而 ``removeMapLayer``
  会连 C++ 对象一起 delete，worker 渲染中持有的裸指针就悬空了 ——
  ``QgsMapRendererCustomPainterJob.start()`` 直接 native 崩溃
  （0xC0000005，见 logs/crash_faulthandler.log 里多次命中 ``_render_one``）。
  克隆是独立的 provider 实例，瓦片仍进同一份进程级瓦片缓存
  （实测克隆渲染 0 次网络请求即命中），预热效果不变。
"""

from __future__ import annotations

import math
import threading
import time
from collections import OrderedDict, deque
from typing import Any, Optional

from qgis.PyQt import sip
from qgis.PyQt.QtCore import QObject, QTimer
from qgis.PyQt.QtGui import QColor, QImage, QPainter

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

    支持 ``completion_event``：当一轮（队列从非空变空）渲染完成时，worker
    会调用 ``event.set()``（threading.Event 跨线程安全）通知主线程。
    调用方负责管理 event 生命周期；连续 ``submit`` 时旧 event 即便被 set 也
    会被覆盖（这里 set 后调用方可能已丢弃旧 event，无副作用）。
    """

    def __init__(self, project) -> None:
        super().__init__(daemon=True, name="TilePrefetchWorker")
        self._project = project
        self._template = None
        self._queue: deque = deque()
        self._cv = threading.Condition()
        self._running = True
        self._completion_event: Optional[threading.Event] = None

    def submit(
        self,
        items,
        template_settings,
        completion_event: Optional[threading.Event] = None,
    ) -> None:
        """替换待渲染列表为 ``items``（[(layer, extent), ...]），并刷新渲染模板。

        ``items`` 里的 layer **必须是离屏克隆**（见 :meth:`TilePrefetcher._clone_for`），
        不能是画布/工程里的图层：工程图层可能在本轮渲染期间被删除，worker
        线程持有的裸指针会悬空（native 崩溃）。克隆由调用方持有引用直到
        worker 渲染完，生命周期不受工程图层增删影响。

        ``template_settings`` 是画布 ``mapSettings()`` 的拷贝（调用方需先用
        :func:`_detached_template` 清空图层列表）：xyz provider 只在层的源 CRS
        （EPSG:3857）下正确选瓦片，画布设置里已带好重投影所需的变换上下文，
        直接复用才能保证预热的瓦片与画布将要请求的一致。

        ``completion_event`` 可选：传入则当本轮（队列从非空变空）渲染完成时
        触发 ``event.set()``，供 :class:`ZoomTileGuard` 等需要知道何时可解冻
        画布的调用方使用。
        """
        with self._cv:
            self._template = template_settings
            self._queue = deque(items)
            self._completion_event = completion_event
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
            # 检查是否本轮已结束（队列空 → 调用方可以解除冻结）
            with self._cv:
                queue_empty = not self._queue
                evt = self._completion_event
            if queue_empty and evt is not None:
                evt.set()  # threading.Event 跨线程安全

    def _render_one(self, layer, extent) -> None:
        from qgis.core import QgsMapRendererCustomPainterJob, QgsMapSettings

        # layer 是调用方持有的离屏克隆，正常不会被删除；这里仍做一层兜底
        # （克隆在极端时序下也可能已被释放，sip 包装器还在）。
        if layer is None or sip.isdeleted(layer):
            return
        try:
            if not layer.isValid():
                return
        except RuntimeError:
            return

        ms = QgsMapSettings(self._template)  # 画布设置拷贝（模板已清空图层列表）
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
        # 工程图层的离屏克隆（按 layer id 缓存；worker 只渲染克隆，见 _clone_for）
        self._clones: dict[str, Any] = {}
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
            settings = canvas.mapSettings()
            extent = settings.extent()
            if not extent or extent.width() <= 0 or extent.height() <= 0:
                return
            layers = [
                l for l in settings.layers()
                if not sip.isdeleted(l) and _is_xyz_layer(l)
            ]
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
            # worker 渲染离屏克隆，不碰画布上的工程图层（图层随时可能被删除）
            clone = self._clone_for(layers[0])
            if clone is None:
                return
            items, added = self._build_items(settings, extent, clone)
            if not items:
                return
            if self._worker is None:
                self._worker = _PrefetchWorker(self._project or canvas.project())
                self._worker.start()
            self._worker.submit(items, _detached_template(settings))
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

    def _clone_for(self, layer):
        """:func:`_clone_layer` 的绑定版本（见「worker 渲染克隆」的说明）。"""
        return _clone_layer(self._clones, layer)

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
        worker = self._worker
        self._worker = None
        if worker is not None:
            worker.stop()
            worker.join(timeout=2.0)
            if not worker.is_alive():
                # worker 已退出：克隆的引用只剩主线程，可安全释放
                self._clones.clear()
            # 若 worker 仍在收尾（渲染没返回），克隆留到对象销毁时释放——
            # 避免在 worker 线程里析构仍在使用的 QgsRasterLayer（跨线程析构会崩）
        self._canvas = None


def _clone_layer(cache: dict, layer) -> Optional[Any]:
    """返回 ``layer`` 的离屏克隆（按 ``layer.id()`` 缓存在 ``cache`` 里）。

    为什么 worker 不直接渲染工程图层：

    * ``QgsProject.removeMapLayer`` 会**连 C++ 对象一起 delete**（移除底图、
      工程切换、关闭工程都会走到），而 worker 线程渲染期间持有的是裸指针 ——
      图层一删，``QgsMapRendererCustomPainterJob.start()`` 立刻 native 崩溃
      （0xC0000005，见 logs/crash_faulthandler.log）。
    * 画布渲染与 worker 还会同时使用同一个 provider 实例，provider 不保证
      跨线程安全。

    克隆有独立 provider，生命周期与工程无关；XYZ 瓦片缓存在进程内按源 URL
    共享（实测克隆渲染 0 次网络请求即命中），预热效果不变。

    克隆必须在**主线程**创建并由调用方持有引用，worker 只渲染不析构。
    """
    try:
        key = layer.id()
    except RuntimeError:  # Python 包装器还在，底层对象已销毁
        return None
    clone = cache.get(key)
    if clone is not None and not sip.isdeleted(clone):
        return clone
    try:
        clone = layer.clone()
    except Exception:  # noqa: BLE001  克隆失败只是不预热，不影响主流程
        logger.debug("tile prefetch: 图层克隆失败", exc_info=True)
        return None
    if clone is None or sip.isdeleted(clone):
        return None
    try:
        if not clone.isValid():
            return None
    except RuntimeError:
        return None
    cache[key] = clone
    return clone


def _detached_template(settings) -> Any:
    """画布 ``mapSettings()`` 的拷贝，但**清空图层列表**。

    模板只用来复用投影 / 输出尺寸 / 变换上下文；保留画布图层列表会让
    worker 在拷贝模板时间接持有工程图层的裸指针，图层被删除后
    ``job.start()`` 会踩空（同 :func:`_clone_layer` 的崩溃）。
    """
    from qgis.core import QgsMapSettings

    template = QgsMapSettings(settings)
    template.setLayers([])
    return template


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


class ZoomTileGuard(QObject):
    """缩放时后台预取当前视口瓦片，瓦片到齐后只重绘瓦片图层——不冻结画布。

    背景与设计权衡
    --------------
    :class:`TilePrefetcher` 解决的是"平移/缩放后**下一次**视口的白屏"
    （renderComplete 后预热 8 个邻居）。但**当前这一次**缩放本身仍要走
    "渲染→等瓦片→白屏"的流程——用户体感"缩放后总要白一下"。

    早一版（commit ``fc9fee7`` 之后的实现）用 ``canvas.freeze(True)`` 保留旧帧、
    瓦片到齐再 ``freeze(False) + refresh()`` 出新帧。物理上就是"先回退到旧帧、
    再突然跳到新帧"，是个明显的 snap——用户反馈不平滑。

    现版（2026-09-04）：**拿掉 freeze**，只做后台预取 + 完成后重绘瓦片图层。
    流程：
    1. ``canvas.extentsChanged`` → 启动 200ms 防抖（连续缩放时合并）
    2. 防抖到期 → 后台离屏渲染当前视口（单 extent，``completion_event``）
    3. 预取完成 → ``layer.triggerRepaint()``：只把该瓦片图层缓存置脏
       （``QgsMapRendererCache`` 自己监听 ``repaintRequested``），矢量层继续
       复用缓存；取不到图层时才回退 ``canvas.refresh()``
    4. 兜底：预取超时（2s）或失败 → 同上（只重绘瓦片层，不让画布永远等）

    视觉上：画布先出当前 extent（瓦片未到时该位置是白的，**不会被回退到旧
    帧**），瓦片就绪后重绘瓦片层把它填上——连续的、没有 snap。如果白屏明显，
    再考虑用占位灰代替纯白（见 ``QgsRasterLayer`` 的 backgroundColor/placeholder）。

    worker 实例**独立**于 TilePrefetcher 的 worker：
    - :meth:`_PrefetchWorker.submit` 替换队列语义下，共享会让邻居预热被
      当前预热打断（行为正确但不利分析）
    - 多一个 daemon 线程空闲挂 ``Condition``，开销可忽略

    QGIS 4.2 PyQt6 API 注意（已实测 / 已踩坑）
    ------------------------------------------
    - 信号是 ``extentsChanged``（带 s），不是 ``extentChanged``（代码库里
      swipe.py:347、qmap_compare.py:437 等都是这个写法）
    - 仍**不**监听 ``renderComplete``：预取完成时主动 ``triggerRepaint()``
      瓦片图层，renderComplete 仍可能为之前已 in-flight 的渲染触发，与本类逻辑无冲突
    """

    _DEBOUNCE_MS = 200       # 缩放空闲后才预取（连续滚动时合并）
    _POLL_MS = 50            # 主线程轮询完成事件的间隔
    _TIMEOUT_MS = 2000       # 预取硬超时（避免画布永远不刷新）

    def __init__(
        self,
        canvas,
        project=None,
        parent: Optional[QObject] = None,
    ) -> None:
        super().__init__(parent)
        self._canvas = canvas
        self._project = project
        self._seq = 0                      # 取消旧预取用
        self._pending_seq: int = 0         # 当前正在等待的批次
        self._pending_event: Optional[threading.Event] = None
        self._started_at: Optional[float] = None
        self._worker: Optional[_PrefetchWorker] = None
        # 工程图层的离屏克隆（按 layer id 缓存；worker 只渲染克隆，见 _clone_for）
        self._clones: dict[str, Any] = {}
        self._prefetch_layer: Any = None  # 本轮预取的 XYZ 图层（收尾只重绘它）
        # 防抖：缩放空闲 200ms 才调度预取
        self._debounce = QTimer(self)
        self._debounce.setSingleShot(True)
        self._debounce.setInterval(self._DEBOUNCE_MS)
        self._debounce.timeout.connect(self._on_idle)
        # 轮询：worker 完成 / 超时
        self._poll = QTimer(self)
        self._poll.setInterval(self._POLL_MS)
        self._poll.timeout.connect(self._on_poll)
        # 监听缩放（extent 变化包括 zoom 和 pan，都需要拦截；QGIS 4 是 extentsChanged 带 s）
        canvas.extentsChanged.connect(self._on_extents_changed)
        canvas.destroyed.connect(self.shutdown)

    # ----------------------------------------------------------- 事件

    def _clone_for(self, layer):
        """:func:`_clone_layer` 的绑定版本（见「worker 渲染克隆」的说明）。"""
        return _clone_layer(self._clones, layer)

    def _on_extents_changed(self) -> None:
        canvas = self._canvas
        if canvas is None:
            return
        # 取消进行中的预取：seq++ 让旧 event 即使 set 也不被采纳
        self._seq += 1
        self._pending_seq = self._seq
        self._pending_event = None
        # 停掉旧轮询（_on_idle 触发后会 start）—— 节省 CPU，连续缩放时不空转
        if self._poll.isActive():
            self._poll.stop()
        self._debounce.start()  # 重置防抖

    def _on_idle(self) -> None:
        """缩放空闲 200ms：取当前视口 → 预取 → 启动轮询。"""
        canvas = self._canvas
        if canvas is None:
            return
        try:
            settings = canvas.mapSettings()
            extent = settings.extent()
            if not extent or extent.width() <= 0 or extent.height() <= 0:
                return
            layers = [
                l for l in settings.layers()
                if not sip.isdeleted(l) and _is_xyz_layer(l)
            ]
            if not layers:
                return  # 没 XYZ 层就不预取（无瓦片可拉）
            # worker 渲染离屏克隆，不碰画布上的工程图层（图层随时可能被删除）
            clone = self._clone_for(layers[0])
            if clone is None:
                return
            # 自己持有 worker（不与 TilePrefetcher 共享——见类 docstring）
            if self._worker is None:
                self._worker = _PrefetchWorker(self._project or canvas.project())
                self._worker.start()
            self._seq += 1
            current_seq = self._seq
            self._pending_seq = current_seq
            evt = threading.Event()
            self._pending_event = evt
            self._started_at = time.monotonic()
            self._prefetch_layer = layers[0]
            self._worker.submit(
                [(clone, extent)],
                _detached_template(settings),
                completion_event=evt,
            )
            self._poll.start()
        except Exception as exc:  # noqa: BLE001
            logger.debug("zoom tile guard prefetch failed: %s", exc)

    def _on_poll(self) -> None:
        """轮询：worker 完成 / 超时 → 重绘瓦片层（仅当批次匹配当前 seq）。

        注意：用户继续缩放时 ``_on_extents_changed`` 会把 ``_pending_event``
        置 None 并 stop timer，所以本方法只在"当前确实有进行中的预取"时
        执行有意义的逻辑；这里不再额外 stop timer（_on_extents_changed 已经处理）。
        """
        if self._pending_event is None:
            return
        if not self._pending_event.is_set():
            if self._started_at is not None and \
               (time.monotonic() - self._started_at) * 1000 > self._TIMEOUT_MS:
                logger.debug("zoom tile guard prefetch 超时，强制重绘瓦片层")
                self._pending_event = None
                self._pending_seq = 0
                self._poll.stop()
                self._refresh()
            return
        # event set
        if self._pending_seq != self._seq:
            # 用户已再次缩放，忽略这次完成，等下一次 idle
            self._pending_event = None
            return
        self._pending_event = None
        self._pending_seq = 0
        self._poll.stop()
        self._refresh()

    def _refresh(self) -> None:
        """让画布用缓存里的瓦片重绘一次（不重绘其它图层）。

        只调 ``layer.triggerRepaint()``：``QgsMapRendererCache`` 自己监听图层的
        ``repaintRequested`` 并只把该图层缓存置脏，矢量层继续复用缓存。**不要**
        调 ``canvas.refresh()``——那会进入 ``AppMapCanvas.refresh()`` 的
        ``clearCache()``，把整幅地图的所有图层缓存一起废掉（平移/缩放后再全量
        重绘一遍）。图层已删除 / 取不到时才回退整幅刷新。
        """
        canvas = self._canvas
        self._started_at = None
        if canvas is None:
            return
        layer = self._prefetch_layer
        self._prefetch_layer = None
        if layer is not None and not sip.isdeleted(layer):
            try:
                layer.triggerRepaint()
                return
            except Exception:
                logger.debug("瓦片图层 triggerRepaint 失败，回退整幅刷新", exc_info=True)
        try:
            canvas.refresh()
        except Exception:  # noqa: BLE001
            logger.debug("canvas.refresh() 失败（已忽略）")

    # ----------------------------------------------------------- 清理

    def shutdown(self) -> None:
        """停 timer + worker 并解绑（画布销毁时自动调用；也可手动调）。"""
        self._debounce.stop()
        self._poll.stop()
        worker = self._worker
        self._worker = None
        if worker is not None:
            worker.stop()
            worker.join(timeout=2.0)
            if not worker.is_alive():
                # worker 已退出：克隆的引用只剩主线程，可安全释放
                self._clones.clear()
            # 若 worker 仍在收尾（渲染没返回），克隆留到对象销毁时释放——
            # 避免在 worker 线程里析构仍在使用的 QgsRasterLayer（跨线程析构会崩）
        self._prefetch_layer = None
        self._canvas = None


__all__ = ["TilePrefetcher", "ZoomTileGuard"]
