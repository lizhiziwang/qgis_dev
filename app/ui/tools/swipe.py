"""卷帘对比工具（分屏视图状态常驻）。

激活后画布只显示**左（上）影像**；右（下）影像离屏渲染成 :class:`QImage`，
矢量等其它图层也离屏渲染成透明 QImage，两者由 :class:`SwipeOverlay`（canvas 子
QWidget，仓库既定覆盖层模式，见 :class:`app.ui.common.canvas_zoom.CanvasZoomTool`）
合成：右（下）影像画在分割线一侧，矢量等其它图层画在**两侧影像之上**。
拖动分割线左右（垂直卷帘）或上下（水平卷帘）切换。

分屏作为**视图状态常驻**：切到任意工具（平移 / 缩放 / 画要素 / 查询）分屏保留，
覆盖层与离屏渲染独立于当前激活工具持续工作；再点「卷帘对比」恢复分隔条拖拽，
而当前已经是卷帘工具时再点一次（或按 **Esc**）才真正退出，还原原图层集并移除
覆盖层；左影像被移除时也会自动退出。

安全约束（仓库既定结论，见 ``canvas_zoom.py`` / ``main_window``）：

- 不在 Qt 事件栈内调 ``canvas.refresh()`` / ``zoomScale`` / ``unsetMapTool``，
  一律 ``QTimer.singleShot(0, ...)`` 推迟出栈，避免 QGIS 0xC0000409；
- 离屏渲染的 ``finished`` 信号在主线程投递到 Python 槽安全；槽里只更新覆盖层
  （``set_images`` + ``update``），不碰画布刷新；
- 过期 / 被取消的渲染 job 用单调递增 ``_render_seq`` 淘汰，finished 里对不上
  序号直接忽略。
"""

from __future__ import annotations

from typing import Optional

from qgis.PyQt.QtCore import QEvent, QRect, Qt, QTimer
from qgis.PyQt.QtGui import QColor, QImage, QPainter, QPen
from qgis.PyQt.QtWidgets import QWidget
from qgis.core import QgsMapLayer, QgsMapRendererParallelJob, QgsMapSettings, QgsProject
from qgis.gui import QgsMapTool

from app.infra.logging import get_logger

logger = get_logger(__name__)


class SwipeOverlay(QWidget):
    """画布覆盖层：绘制右（下）影像（按分割线裁剪）+ 其它图层 + 分割线 + 手柄。

    作为 canvas 的子 QWidget（本构建无 ``QgsMapCanvas.setOverlayWidget``）。
    ``WA_TransparentForMouseEvents`` 让鼠标穿透到画布 viewport → 当前
    :class:`QgsMapTool` 收到事件 → 拖动分割线。覆盖层只画：分割线一侧的右（下）
    影像 + 矢量等其它图层（透明背景），未画区域透明露出画布的左（上）影像。
    会话常驻期间，其它工具激活时它仍显示在最上层（``raise_to_top`` 由
    ``mapCanvasRefreshed`` / ``extentsChanged`` 处理器持续触发）。
    """

    def __init__(self, canvas, orientation: str = "vertical") -> None:
        super().__init__(canvas)
        self._canvas = canvas
        self._orientation = orientation
        self._compare_image: Optional[QImage] = None
        self._others_image: Optional[QImage] = None
        self._divider = 0.5
        self.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        self.setAttribute(Qt.WidgetAttribute.WA_NoSystemBackground)
        self._sync_geometry()
        canvas.installEventFilter(self)
        canvas.viewport().installEventFilter(self)

    # ------------------------------------------------------------ 几何与置顶

    def _sync_geometry(self) -> None:
        """把覆盖层贴到画布 viewport（本构建 viewport 填满画布，无滚动条）。"""
        try:
            vp = self._canvas.viewport()
        except RuntimeError:
            return
        if vp is None:
            return
        self.setGeometry(QRect(vp.pos(), vp.size()))

    def eventFilter(self, watched, event) -> bool:  # noqa: N802 —— Qt 命名
        """画布 / viewport 尺寸变化时同步覆盖层几何并重新置顶。"""
        if event.type() == QEvent.Type.Resize:
            self._sync_geometry()
            self.raise_()
        return super().eventFilter(watched, event)

    def showEvent(self, event) -> None:  # noqa: N802 —— Qt 命名
        super().showEvent(event)
        self._sync_geometry()
        self.raise_()

    def raise_to_top(self) -> None:
        """画布重绘后重新置顶（底层渲染在 viewport 内重绘，可能盖住覆盖层）。"""
        self._sync_geometry()
        self.raise_()

    # ------------------------------------------------------------ 状态

    def set_images(self, compare: QImage, others: Optional[QImage]) -> None:
        """设置离屏渲染结果（右影像 + 其它图层），触发重绘。"""
        self._compare_image = compare
        self._others_image = others

    def set_divider(self, fraction: float) -> None:
        """设置分割线位置（0~1 归一化），触发重绘。"""
        self._divider = fraction
        self.update()

    def divider_px(self) -> int:
        """分割线在当前尺寸下的像素位置（垂直：x；水平：y）。"""
        if self._orientation == "vertical":
            return int(self._divider * self.width())
        return int(self._divider * self.height())

    # ------------------------------------------------------------ 绘制

    def paintEvent(self, event) -> None:  # noqa: N802 —— Qt 命名
        painter = QPainter(self)
        try:
            w, h = self.width(), self.height()
            if w <= 0 or h <= 0:
                return
            if self._compare_image is not None and not self._compare_image.isNull():
                if self._orientation == "vertical":
                    x = int(self._divider * w)
                    clip = QRect(x, 0, w - x, h)
                else:
                    y = int(self._divider * h)
                    clip = QRect(0, y, w, h - y)
                painter.save()
                painter.setClipRect(clip)
                # 右影像带 devicePixelRatio（离屏渲染时设置），画逻辑矩形即 1:1 清晰
                painter.drawImage(QRect(0, 0, w, h), self._compare_image)
                painter.restore()
            if self._others_image is not None and not self._others_image.isNull():
                # 矢量等其它图层（透明背景）画在两侧影像之上
                painter.drawImage(QRect(0, 0, w, h), self._others_image)
            self._draw_divider(painter, w, h)
        finally:
            painter.end()

    def _draw_divider(self, painter: QPainter, w: int, h: int) -> None:
        pen = QPen(QColor(0, 0, 0, 210))
        pen.setWidth(2)
        painter.setPen(pen)
        handle = QColor(255, 255, 255, 230)
        if self._orientation == "vertical":
            x = int(self._divider * w)
            painter.drawLine(x, 0, x, h)
            painter.setBrush(handle)
            painter.drawEllipse(QRect(x - 9, h // 2 - 9, 18, 18))
        else:
            y = int(self._divider * h)
            painter.drawLine(0, y, w, y)
            painter.setBrush(handle)
            painter.drawEllipse(QRect(w // 2 - 9, y - 9, 18, 18))


class SwipeTool(QgsMapTool):
    """卷帘对比地图工具（分屏视图状态常驻）。

    ``_alive`` = 会话存活（覆盖层 + 图层集 + 离屏渲染工作中），切到其它工具时
    保持 True —— 分屏不被拆；``_active`` = 本工具是当前激活地图工具（能拖分隔条）。
    仅 Esc 或左影像被移除才 ``_exit_session()`` 拆覆盖层并还原图层集。
    """

    #: 命中分隔条才进入拖拽的容差（逻辑像素）
    _DRAG_THRESHOLD = 10

    def __init__(
        self,
        canvas,
        base_layer: QgsMapLayer,
        compare_layer: QgsMapLayer,
        orientation: str = "vertical",
        parent: Optional[object] = None,
    ) -> None:
        super().__init__(canvas)
        self.canvas = canvas
        self._base = base_layer
        self._compare = compare_layer
        self._orientation = orientation
        self.parent_window = parent
        self._project = canvas.project() or QgsProject.instance()
        #: 激活前的图层集，退出时还原（__init__ 在 setLayers 之前调用，捕获原始集）
        self._orig_layers = list(canvas.layers())
        self._overlay: Optional[SwipeOverlay] = SwipeOverlay(canvas, orientation)
        self._overlay.set_divider(0.5)
        #: 覆盖层渲染集：除两影像外的其它图层（矢量等），画在两侧影像之上
        self._others: list[QgsMapLayer] = []
        self._jobs: dict[str, QgsMapRendererParallelJob] = {}
        self._job_connections: dict[str, object] = {}
        self._images: dict[str, Optional[QImage]] = {}
        self._pending = 0
        self._render_seq = 0  # 单调递增序号，淘汰过期 job 的 finished
        self._needs_render = True
        self._render_scheduled = False
        self._alive = False  # 会话存活（覆盖层 + 渲染工作中）
        self._active = False  # 本工具是当前激活地图工具
        self._restoring = False  # 自己 setLayers 时置 True，屏蔽 layersChanged 回环
        self._dragging = False
        if orientation == "vertical":
            self.setCursor(Qt.CursorShape.SplitHCursor)
        else:
            self.setCursor(Qt.CursorShape.SplitVCursor)

    def is_alive(self) -> bool:
        """分屏会话是否存活（供 MainWindow 判断「点卷帘对比」是恢复还是新建）。"""
        return self._alive

    def stop(self) -> None:
        """外部停止会话（工具条再次点击）：拆覆盖层并还原图层。

        与 Esc 的完整退出路径相比少一步 ``unsetMapTool``——调用方
        （:meth:`MainWindow._on_swipe`）会紧接着切回平移工具。
        """
        self._exit_session()

    # ------------------------------------------------------------ QgsMapTool 生命周期

    def activate(self) -> None:
        if self._overlay is None:
            return  # 会话已退出，工具失效
        if not self._alive:
            self._alive = True
            self._connect_canvas_signals()
            self._set_base_content()
            self._overlay.show()
        self._active = True
        self._overlay.raise_to_top()
        # 从工具条按钮 / 参数对话框激活时焦点不在画布上，Esc 到不了地图工具；
        # 推迟一拍把焦点还给画布（对话框这时才真正关闭），保证「Esc 退出卷帘」可用。
        QTimer.singleShot(0, self.canvas.setFocus)
        self._schedule_render()

    def deactivate(self) -> None:
        # 视图状态常驻：切换工具不拆覆盖层、不还原图层；只有 _exit_session 才拆。
        self._active = False
        self._dragging = False
        super().deactivate()

    def _exit_session(self) -> None:
        """真正退出分屏：断信号、取消渲染、移除覆盖层、还原原图层集。"""
        if not self._alive:
            return
        self._alive = False
        self._disconnect_canvas_signals()
        self._cancel_jobs()
        ov, self._overlay = self._overlay, None
        if ov is not None:
            try:
                self.canvas.removeEventFilter(ov)
                self.canvas.viewport().removeEventFilter(ov)
            except RuntimeError:
                pass
            ov.hide()
            ov.deleteLater()
        if self._orig_layers is not None:
            orig, self._orig_layers = self._orig_layers, None
            # 推迟出栈还原：不在事件栈内动画布，且新工具已先激活
            QTimer.singleShot(0, lambda: self._restore_layers(orig))

    # ------------------------------------------------------------ 鼠标 / 键盘

    def canvasPressEvent(self, event) -> None:  # noqa: N802 —— QGIS 命名
        if event.button() != Qt.MouseButton.LeftButton:
            return
        try:
            pt = event.pixelPoint()
        except RuntimeError:
            return
        # 只有按住分隔条附近才进入拖拽；点其它位置不改变分屏占比
        if self._near_divider(pt):
            self._dragging = True

    def _near_divider(self, pt) -> bool:
        """按压点是否命中分隔条（±容差）。"""
        ov = self._overlay
        if ov is None:
            return False
        try:
            if self._orientation == "vertical":
                return abs(pt.x() - ov.divider_px()) <= self._DRAG_THRESHOLD
            return abs(pt.y() - ov.divider_px()) <= self._DRAG_THRESHOLD
        except RuntimeError:
            return False

    def canvasMoveEvent(self, event) -> None:  # noqa: N802 —— QGIS 命名
        if self._dragging:
            self._update_divider(event)

    def canvasReleaseEvent(self, event) -> None:  # noqa: N802 —— QGIS 命名
        self._dragging = False

    def keyPressEvent(self, event) -> None:  # noqa: N802 —— QGIS 命名
        if event.key() == Qt.Key.Key_Escape:
            event.ignore()
            # 不在键盘事件栈内拆会话 / unsetMapTool，推迟出栈
            QTimer.singleShot(0, self._quit)
        else:
            super().keyPressEvent(event)

    def _quit(self) -> None:
        self._exit_session()
        try:
            self.canvas.unsetMapTool(self)
        except RuntimeError:
            pass

    def _update_divider(self, event) -> None:
        ov = self._overlay
        if ov is None:
            return
        try:
            pt = event.pixelPoint()  # Qt6：QgsMapMouseEvent 无 x()/y()
            if self._orientation == "vertical":
                f = pt.x() / max(1, ov.width())
            else:
                f = pt.y() / max(1, ov.height())
        except RuntimeError:
            return
        ov.set_divider(min(1.0, max(0.0, f)))

    # ------------------------------------------------------------ 图层守卫

    def _set_layers(self, layers) -> None:
        """设置画布图层集；``_restoring`` 屏蔽自己的 layersChanged 回环。"""
        self._restoring = True
        try:
            self.canvas.setLayers(layers)
        except RuntimeError:
            pass
        finally:
            self._restoring = False

    def _set_base_content(self) -> None:
        """画布只留左（上）影像；右影像与其它图层由覆盖层离屏渲染。"""
        if not self._alive:
            return
        compare_id = self._compare.id()
        base_id = self._base.id()
        self._others = [layer for layer in self._orig_layers if layer.id() not in (base_id, compare_id)]
        self._set_layers([self._base])

    def _set_base_only(self) -> None:
        """把画布图层集重断言为只有左（上）影像（桥接器改回多图层时用）。"""
        self._set_layers([self._base])

    def _restore_layers(self, orig) -> None:
        try:
            valid_ids = set(self._project.mapLayers().keys())
        except RuntimeError:
            return
        try:
            self.canvas.setLayers([layer for layer in orig if layer.id() in valid_ids])
        except RuntimeError:
            pass

    # ------------------------------------------------------------ 画布信号

    def _connect_canvas_signals(self) -> None:
        c = self.canvas
        c.extentsChanged.connect(self._on_canvas_changed)
        c.layersChanged.connect(self._on_layers_changed)
        c.mapCanvasRefreshed.connect(self._on_canvas_refreshed)
        c.magnificationChanged.connect(self._on_canvas_changed)
        c.rotationChanged.connect(self._on_canvas_changed)

    def _disconnect_canvas_signals(self) -> None:
        c = self.canvas
        try:
            c.extentsChanged.disconnect(self._on_canvas_changed)
            c.layersChanged.disconnect(self._on_layers_changed)
            c.mapCanvasRefreshed.disconnect(self._on_canvas_refreshed)
            c.magnificationChanged.disconnect(self._on_canvas_changed)
            c.rotationChanged.disconnect(self._on_canvas_changed)
        except (TypeError, RuntimeError):
            pass

    def _on_canvas_changed(self) -> None:
        if not self._alive:
            return
        self._needs_render = True
        ov = self._overlay
        if ov is not None:
            ov.raise_to_top()
        self._schedule_render()

    def _on_canvas_refreshed(self) -> None:
        ov = self._overlay
        if ov is not None:
            ov.raise_to_top()

    def _on_layers_changed(self) -> None:
        if not self._alive or self._restoring:
            return
        try:
            if self._base.id() not in self._project.mapLayers():
                # 左影像被移除 → 退出分屏
                QTimer.singleShot(0, self._quit)
                return
            cur = list(self.canvas.layers())
        except RuntimeError:
            return
        compare_id = self._compare.id()
        base_id = self._base.id()
        # 桥接器当前画布集即用户可见图层 → 除两影像外的交给覆盖层渲染
        self._others = [layer for layer in cur if layer.id() not in (base_id, compare_id)]
        # 画布只留左（上）影像；矢量等由覆盖层绘制在两侧影像之上
        QTimer.singleShot(0, self._set_base_only)
        self._schedule_render()

    # ------------------------------------------------------------ 离屏渲染

    def _schedule_render(self) -> None:
        if not self._alive:
            return
        self._needs_render = True
        if not self._render_scheduled:
            self._render_scheduled = True
            QTimer.singleShot(0, self._run_scheduled_render)

    def _run_scheduled_render(self) -> None:
        self._render_scheduled = False
        if not self._alive or not self._needs_render:
            return
        if any(job.isActive() for job in self._jobs.values()):
            # 渲染进行中 → 等它的 finished 后再拾取 pending 标记
            return
        self._needs_render = False
        self._start_render()

    def _cancel_jobs(self) -> None:
        """取消所有离屏渲染 job 并断开 finished，杜绝过期回调打到已拆会话上。"""
        for kind, job in self._jobs.items():
            conn = self._job_connections.pop(kind, None)
            if conn is not None:
                try:
                    job.finished.disconnect(conn)  # 取消后不再收到 finished
                except (TypeError, RuntimeError):
                    pass
            try:
                job.cancel()  # parallel job：非阻塞请求取消
            except RuntimeError:
                pass
        self._jobs = {}
        self._job_connections = {}
        self._pending = 0

    def _render_settings(self, layers) -> Optional[QgsMapSettings]:
        """拷贝画布当前渲染参数（范围 / CRS / 旋转 / DPI / DPR / 尺寸），换图层。"""
        try:
            settings = QgsMapSettings(self.canvas.mapSettings())
            settings.setOutputSize(self.canvas.viewport().size())  # 逻辑像素
            settings.setDevicePixelRatio(self.canvas.viewport().devicePixelRatioF())
            settings.setLayers(layers)
            # 离屏渲染必须透明背景：canvas 背景是不透明的纯白，不换掉会整块
            # 盖住两侧影像（新增矢量图层进 _others 后分屏即消失）。
            settings.setBackgroundColor(QColor(0, 0, 0, 0))
            if not settings.hasValidSettings() or settings.outputSize().isEmpty() or settings.extent().isEmpty():
                return None
            return settings
        except RuntimeError:
            return None

    def _start_render(self) -> None:
        if not self._alive:
            return
        self._render_seq += 1
        seq = self._render_seq
        self._cancel_jobs()
        self._images = {"compare": None, "others": None}
        for kind, layers in (("compare", [self._compare]), ("others", list(self._others))):
            if not layers:
                continue
            settings = self._render_settings(layers)
            if settings is None:
                continue
            try:
                job = QgsMapRendererParallelJob(settings)
            except RuntimeError:
                continue
            self._pending += 1
            self._jobs[kind] = job
            self._job_connections[kind] = job.finished.connect(lambda k=kind, s=seq: self._on_render_finished(s, k))
            try:
                job.start()
            except RuntimeError:
                conn = self._job_connections.pop(kind, None)
                if conn is not None:
                    try:
                        job.finished.disconnect(conn)
                    except (TypeError, RuntimeError):
                        pass
                self._jobs.pop(kind, None)
                self._pending -= 1
        if self._pending == 0:
            self._apply_images()

    def _on_render_finished(self, seq: int, kind: str) -> None:
        if not getattr(self, "_alive", False) or seq != self._render_seq:
            return  # 过期 / 被取消 job 的 finished，忽略（会话可能已拆）
        job = self._jobs.get(kind)
        if job is None:
            return
        try:
            self._images[kind] = job.renderedImage()
        except RuntimeError:
            self._images[kind] = None
        self._pending -= 1
        if self._pending <= 0:
            self._apply_images()

    def _apply_images(self) -> None:
        ov = self._overlay
        if ov is None:
            return
        try:
            ov.set_images(self._images.get("compare"), self._images.get("others"))
            ov.update()
        except RuntimeError:
            return
        # 渲染期间又来了新需求 → 接着再渲一帧
        if self._needs_render and not self._render_scheduled:
            self._render_scheduled = True
            QTimer.singleShot(0, self._run_scheduled_render)
