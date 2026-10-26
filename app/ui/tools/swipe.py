"""卷帘对比工具（分屏视图状态常驻）。

两侧各自是一套**完整且按图层树顺序合成**的图层栈：

- 左（上）侧 = 可见图层去掉右影像，直接交给画布渲染；
- 右（下）侧 = 可见图层去掉左影像，离屏渲染成一张完整画面，由
  :class:`SwipeOverlay`（canvas 子 QWidget，仓库既定覆盖层模式，见
  :class:`app.ui.common.canvas_zoom.CanvasZoomTool`）按分割线裁剪绘制。

这样左/右影像与其它图层的上下压盖关系都与图层树一致。拖动分割线左右（垂直
卷帘）或上下（水平卷帘）切换。左右影像与其它图层是否参与对比**直接跟随图层树
勾选**：取消勾选某侧影像，对比里对应一侧也立即不可见。

分屏作为**视图状态常驻**：切到任意工具（平移 / 缩放 / 画要素 / 查询）分屏保留，
覆盖层与离屏渲染独立于当前激活工具持续工作；再点「卷帘对比」恢复分隔条拖拽，
而当前已经是卷帘工具时再点一次（或按 **Esc**）才真正退出，按图层树当前显隐
还原画布并移除覆盖层；左影像被移除时也会自动退出。

安全约束（仓库既定结论，见 ``canvas_zoom.py`` / ``main_window``）：

- 不在 Qt 事件栈内调 ``canvas.refresh()`` / ``zoomScale`` / ``unsetMapTool``，
  一律 ``QTimer.singleShot(0, ...)`` 推迟出栈，避免 QGIS 0xC0000409；
- 离屏渲染的 ``finished`` 信号在主线程投递到 Python 槽安全；槽里只更新覆盖层
  （``set_right_image`` + ``update``），不碰画布刷新；
- 过期 / 被取消的渲染 job 用单调递增 ``_render_seq`` 淘汰，finished 里对不上
  序号直接忽略。
"""

from __future__ import annotations

from typing import Optional

from qgis.PyQt import sip
from qgis.PyQt.QtCore import QEvent, QRect, Qt, QTimer
from qgis.PyQt.QtGui import QColor, QImage, QPainter, QPen
from qgis.PyQt.QtWidgets import QWidget
from qgis.core import (
    QgsLayerTreeGroup,
    QgsMapLayer,
    QgsMapRendererParallelJob,
    QgsMapSettings,
    QgsProject,
)
from qgis.gui import QgsMapTool

from app.infra.logging import get_logger

logger = get_logger(__name__)

#: 右侧离屏画面的最小重绘间隔（毫秒）。平移/缩放时限制在 ~25fps，
#: 避免和主画布渲染抢 CPU（左右两侧各渲染一遍，不节流会明显卡顿）。
_RENDER_THROTTLE_MS = 40


class SwipeOverlay(QWidget):
    """画布覆盖层：按分割线裁剪绘制右（下）侧的**完整画面** + 分割线 + 手柄。

    作为 canvas 的子 QWidget（本构建无 ``QgsMapCanvas.setOverlayWidget``）。
    ``WA_TransparentForMouseEvents`` 让鼠标穿透到画布 viewport，普通操作仍交给
    当前 :class:`QgsMapTool`（平移 / 缩放 / 选择）；覆盖层在 viewport 上另装事件
    过滤器，只有按在分割线附近时才拦截并拖动——因此切到漫游等工具后，滑块依然
    可选。左（上）侧由画布直接渲染其图层栈，右侧由离屏渲染成一张完整画面后画在
    覆盖层上；两侧都按图层树顺序合成，未画区域透明露出画布的左（上）影像。
    会话常驻期间，其它工具激活时它仍显示在最上层（``raise_to_top`` 由
    ``mapCanvasRefreshed`` / ``extentsChanged`` 处理器持续触发）。
    """

    #: 命中分割条才进入拖拽的容差（逻辑像素）
    _DRAG_THRESHOLD = 10

    def __init__(self, canvas, orientation: str = "vertical") -> None:
        super().__init__(canvas)
        self._canvas = canvas
        self._orientation = orientation
        self._right_image: Optional[QImage] = None
        self._divider = 0.5
        self._dragging = False
        #: 悬浮消息条（懒查找后缓存，避免每帧递归 findChild）
        self._message_bar = None
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
        rect = QRect(vp.pos(), vp.size())
        # 尺寸没变就别 setGeometry：平移/缩放时 raise_to_top 每帧都调到这里，
        # 重复设几何会触发多余的 resize / repaint，拖慢整幅地图。
        if self.geometry() != rect:
            self.setGeometry(rect)

    def eventFilter(self, watched, event) -> bool:  # noqa: N802 —— Qt 命名
        """同步几何 / 置顶；并在分割线附近拦截鼠标做拖拽。

        事件过滤器装在 canvas viewport 上，**不依赖当前激活的 QgsMapTool**：
        切到漫游 / 缩放后，普通鼠标事件仍交给那些工具，只有按在滑块附近才由
        覆盖层吃掉并拖动分割线。
        """
        if event.type() == QEvent.Type.Resize:
            self.raise_to_top()
            return super().eventFilter(watched, event)
        if self._handle_mouse(watched, event):
            return True
        return super().eventFilter(watched, event)

    def _handle_mouse(self, watched, event) -> bool:
        """分割线拖拽（viewport 事件过滤器）；没命中返回 ``False`` 放行。"""
        try:
            viewport = self._canvas.viewport()
        except RuntimeError:
            return False
        if watched is not viewport and watched is not self._canvas:
            return False
        etype = event.type()
        if etype == QEvent.Type.MouseButtonPress:
            if event.button() != Qt.MouseButton.LeftButton:
                return False
            if not self._near_divider(event.position().toPoint()):
                return False
            self._dragging = True
            return True
        if etype == QEvent.Type.MouseMove and self._dragging:
            self._move_divider(event.position().toPoint())
            return True
        if etype == QEvent.Type.MouseButtonRelease and self._dragging:
            self._dragging = False
            return True
        return False

    def _near_divider(self, pt) -> bool:
        """按压点是否命中分割条（±容差）。"""
        if self._orientation == "vertical":
            return abs(pt.x() - self.divider_px()) <= self._DRAG_THRESHOLD
        return abs(pt.y() - self.divider_px()) <= self._DRAG_THRESHOLD

    def _move_divider(self, pt) -> None:
        if self._orientation == "vertical":
            fraction = pt.x() / max(1, self.width())
        else:
            fraction = pt.y() / max(1, self.height())
        self.set_divider(min(1.0, max(0.0, fraction)))

    def showEvent(self, event) -> None:  # noqa: N802 —— Qt 命名
        super().showEvent(event)
        self.raise_to_top()

    def raise_to_top(self) -> None:
        """画布重绘后重新置顶（底层渲染在 viewport 内重绘，可能盖住覆盖层）。

        消息条也是画布的子控件：卷帘覆盖层盖上去会把它“卷”掉（右侧画面按
        分割线遮挡它）。这里置顶后再把消息条提回最上层，保证提示条始终可见。
        """
        self._sync_geometry()
        self.raise_()
        self._raise_message_bar()

    def _raise_message_bar(self) -> None:
        """把悬浮消息条重新提到卷帘覆盖层之上（无消息条时静默跳过）。"""
        bar = self._message_bar
        if bar is None or sip.isdeleted(bar):
            try:
                bar = self._canvas.findChild(QWidget, "FloatingMessageBar")
            except RuntimeError:
                return
            self._message_bar = bar
        if bar is not None:
            try:
                bar.raise_()
            except RuntimeError:
                pass

    # ------------------------------------------------------------ 状态

    def set_right_image(self, image: QImage) -> None:
        """设置右侧完整画面（含背景、按图层树顺序合成），触发重绘。"""
        self._right_image = image

    def set_divider(self, fraction: float) -> None:
        """设置分割线位置（0~1 归一化），只重绘新旧分割线之间的窄条。

        拖动滑块时若整幅覆盖层重绘（含整张右侧大图），高 DPI 下会明显卡顿；
        两侧画面本身没变，实际变化的只有分割线扫过的那一条区域。
        """
        old = self._divider
        self._divider = fraction
        w, h = self.width(), self.height()
        if w <= 0 or h <= 0:
            self.update()
            return
        margin = 12  # 手柄半径 9 + 线宽
        if self._orientation == "vertical":
            old_x, new_x = int(old * w), int(fraction * w)
            self.update(
                QRect(min(old_x, new_x) - margin, 0, abs(new_x - old_x) + margin * 2, h)
            )
        else:
            old_y, new_y = int(old * h), int(fraction * h)
            self.update(
                QRect(0, min(old_y, new_y) - margin, w, abs(new_y - old_y) + margin * 2)
            )

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
            if self._right_image is not None and not self._right_image.isNull():
                if self._orientation == "vertical":
                    x = int(self._divider * w)
                    clip = QRect(x, 0, w - x, h)
                else:
                    y = int(self._divider * h)
                    clip = QRect(0, y, w, h - y)
                painter.save()
                painter.setClipRect(clip)
                # 右侧完整画面带 devicePixelRatio（离屏渲染时设置），画逻辑矩形即 1:1 清晰
                painter.drawImage(QRect(0, 0, w, h), self._right_image)
                painter.restore()
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

    两侧各自是一套**完整且按图层树顺序合成**的图层栈：左（上）侧 = 可见图层
    去掉右影像，直接交给画布渲染；右（下）侧 = 可见图层去掉左影像，离屏渲染成
    一张完整画面由覆盖层裁剪绘制。这样左/右影像与其它图层的上下压盖关系都与
    图层树一致。

    ``_alive`` = 会话存活（覆盖层 + 图层集 + 离屏渲染工作中），切到其它工具时
    保持 True —— 分屏不被拆；分隔条拖拽由 :class:`SwipeOverlay` 的事件过滤器
    处理，不依赖本工具是否为当前激活地图工具。
    仅 Esc 或影像被移除才 ``_exit_session()`` 拆覆盖层并还原图层集。
    """

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
        #: 激活前的图层集，仅在桥接器不可用时作为退出还原的兜底
        self._orig_layers = list(canvas.layers())
        self._overlay: Optional[SwipeOverlay] = SwipeOverlay(canvas, orientation)
        self._overlay.set_divider(0.5)
        #: 左（上）侧画布图层集：可见图层去掉右影像，保持图层树顺序
        self._left_layers: list[QgsMapLayer] = []
        #: 右（下）侧离屏渲染图层集：可见图层去掉左影像，保持图层树顺序
        self._right_layers: list[QgsMapLayer] = []
        #: 已挂 visibilityChanged 的工程图层树节点（按 C++ 指针去重）
        self._visibility_hooked: set[int] = set()
        self._visibility_nodes: list = []
        self._project_signals_connected = False
        self._jobs: dict[str, QgsMapRendererParallelJob] = {}
        self._job_connections: dict[str, object] = {}
        self._images: dict[str, Optional[QImage]] = {}
        self._pending = 0
        self._render_seq = 0  # 单调递增序号，淘汰过期 job 的 finished
        self._needs_render = True
        self._render_scheduled = False
        #: 右侧画面重绘节流（单次触发；到期渲染最新范围）
        self._render_timer = QTimer(self)
        self._render_timer.setSingleShot(True)
        self._render_timer.setInterval(_RENDER_THROTTLE_MS)
        self._render_timer.timeout.connect(self._run_scheduled_render)
        self._alive = False  # 会话存活（覆盖层 + 渲染工作中）
        self._active = False  # 本工具是当前激活地图工具
        self._restoring = False  # 自己 setLayers 时置 True，屏蔽 layersChanged 回环
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
        super().deactivate()

    def _exit_session(self) -> None:
        """真正退出分屏：断信号、取消渲染、移除覆盖层、按图层树还原画布。"""
        if not self._alive:
            return
        self._alive = False
        self._render_timer.stop()
        self._render_scheduled = False
        # 不依赖 mapToolSet：用户在非卷帘工具下删除影像时 unsetMapTool 是空操作，
        # 这里主动通知主窗口把工具条上的卷帘按钮弹起。
        sync = getattr(self.parent_window, "_sync_swipe_session_active", None)
        if callable(sync):
            try:
                sync()
            except RuntimeError:
                pass
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

    # ------------------------------------------------------------ 键盘

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
        """初始化两侧图层栈：画布画左侧，右侧交给覆盖层离屏渲染。"""
        if not self._alive:
            return
        self._refresh_layers()
        self._set_left_layers()

    def _set_left_layers(self) -> None:
        """把画布图层集重断言为左（上）侧的图层栈（桥接器改回多图层时用）。

        左/右影像及其它图层的显隐和顺序都取自图层树：右影像从左侧图层栈
        剔除，左影像若被取消勾选也不会再出现在画布上。
        """
        # 退出会话时可能还有一次已排队的调用，别再覆盖刚还原的图层集
        if not self._alive:
            return
        self._set_layers(list(self._left_layers))

    def _restore_layers(self, orig) -> None:
        """退出会话后按**当前图层树可见性**还原画布（不能用启动时快照）。

        分屏期间用户可能勾选 / 取消勾选任意图层。若直接恢复 ``orig``（会话
        开始时的画布集），会把已隐藏的图层重新显示、把刚打开的图层又藏起来，
        图层开关就与画布对不上了。优先让桥接器按图层树重建，失败再退回快照。
        """
        bridge = getattr(self.parent_window, "bridge", None)
        if bridge is not None:
            try:
                bridge.setCanvasLayers()
                return
            except (AttributeError, RuntimeError):
                pass
        try:
            layers = self._current_visible_layers()
        except (AttributeError, RuntimeError):
            layers = None
        if layers is None:
            try:
                valid_ids = set(self._project.mapLayers().keys())
            except RuntimeError:
                return
            layers = [layer for layer in orig if layer.id() in valid_ids]
        try:
            self.canvas.setLayers(layers)
        except RuntimeError:
            pass

    # ------------------------------------------------------------ 图层显隐 / 顺序

    def _current_visible_layers(self) -> list:
        """按图层树顺序返回**有效可见**的图层（含祖先组的勾选状态）。

        分屏期间画布图层集被改写，不能读 ``canvas.layers()``；直接遍历图层树，
        保证两侧图层栈的显隐和上下顺序都与图层面板一致。
        """
        root = self._project.layerTreeRoot()
        layers = []
        for node in root.findLayers():
            layer = node.layer()
            if layer is not None and node.isVisible():
                layers.append(layer)
        return layers

    def _refresh_layers(self) -> None:
        """重算左右两侧图层栈（保持图层树顺序）。"""
        visible = self._current_visible_layers()
        base_id = self._base.id()
        compare_id = self._compare.id()
        # 左侧不画右影像，右侧不画左影像；其它图层按图层树顺序同时参与两侧
        self._left_layers = [l for l in visible if l.id() != compare_id]
        self._right_layers = [l for l in visible if l.id() != base_id]

    def _connect_project_signals(self) -> None:
        """接工程图层增删 + 图层树显隐：分屏期间用户切开关也要重绘。"""
        if self._project_signals_connected:
            return
        self._project_signals_connected = True
        try:
            self._project.layersAdded.connect(self._on_project_layers_changed)
            self._project.layersRemoved.connect(self._on_project_layers_changed)
        except (AttributeError, RuntimeError):
            pass
        self._hook_project_visibility()

    def _disconnect_project_signals(self) -> None:
        if not self._project_signals_connected:
            return
        self._project_signals_connected = False
        try:
            self._project.layersAdded.disconnect(self._on_project_layers_changed)
        except (TypeError, RuntimeError):
            pass
        try:
            self._project.layersRemoved.disconnect(self._on_project_layers_changed)
        except (TypeError, RuntimeError):
            pass
        for node in self._visibility_nodes:
            if sip.isdeleted(node):
                continue
            try:
                node.visibilityChanged.disconnect(self._on_project_visibility_changed)
            except (TypeError, RuntimeError):
                pass
            if isinstance(node, QgsLayerTreeGroup):
                for signal_name in ("addedChildren", "removedChildren"):
                    try:
                        getattr(node, signal_name).disconnect(
                            self._on_project_layers_changed
                        )
                    except (TypeError, RuntimeError):
                        pass
        self._visibility_nodes.clear()
        self._visibility_hooked.clear()

    def _prune_visibility_hooks(self) -> None:
        """清掉已被销毁节点的记录，避免 C++ 指针复用后漏挂新节点。"""
        alive = []
        keys = set()
        for node in self._visibility_nodes:
            if sip.isdeleted(node):
                continue
            try:
                key = int(sip.unwrapinstance(node))
            except (TypeError, RuntimeError):
                continue
            alive.append(node)
            keys.add(key)
        self._visibility_nodes = alive
        self._visibility_hooked = keys

    def _hook_project_visibility(self) -> None:
        """给根 / 组 / 图层挂 visibilityChanged。

        组勾选不会给子图层逐个发 visibilityChanged（实测），所以组节点也要挂；
        画布信号在分屏期间不可靠，这里直接监听图层树，显隐变化必到。
        """
        self._prune_visibility_hooks()
        try:
            root = self._project.layerTreeRoot()
            nodes = [root, *root.findGroups(), *root.findLayers()]
        except (AttributeError, RuntimeError):
            return
        for node in nodes:
            try:
                key = int(sip.unwrapinstance(node))
            except (TypeError, RuntimeError):
                continue
            if key in self._visibility_hooked:
                continue
            try:
                node.visibilityChanged.connect(self._on_project_visibility_changed)
            except (AttributeError, RuntimeError):
                continue
            if isinstance(node, QgsLayerTreeGroup):
                # 图层在组内上下移动 / 增删不会发工程级 layersAdded/Removed，
                # 组节点的 addedChildren / removedChildren 才能捕获重排。
                for signal_name in ("addedChildren", "removedChildren"):
                    try:
                        getattr(node, signal_name).connect(
                            self._on_project_layers_changed
                        )
                    except (AttributeError, RuntimeError):
                        pass
            self._visibility_hooked.add(key)
            self._visibility_nodes.append(node)

    def _on_project_visibility_changed(self, *_args) -> None:
        """用户切换图层 / 组显隐：同步两侧图层栈并重绘覆盖层。"""
        if not self._alive:
            return
        self._hook_project_visibility()
        try:
            self._refresh_layers()
        except RuntimeError:
            return
        # 左右影像的勾选直接影响画布图层集；其它图层被桥接器重新塞回画布时
        # 也在这里重新断言为左侧图层栈。
        QTimer.singleShot(0, self._set_left_layers)
        self._schedule_render()

    def _on_project_layers_changed(self, *_args) -> None:
        """工程图层增删：补挂新节点、重算两侧图层；左影像被删则退出。"""
        if not self._alive:
            return
        self._hook_project_visibility()
        try:
            if (
                sip.isdeleted(self._base)
                or sip.isdeleted(self._compare)
                or self._base.id() not in self._project.mapLayers()
                or self._compare.id() not in self._project.mapLayers()
            ):
                # 任一侧影像被移除 → 退出分屏
                QTimer.singleShot(0, self._quit)
                return
            self._refresh_layers()
        except RuntimeError:
            return
        QTimer.singleShot(0, self._set_left_layers)
        self._schedule_render()

    # ------------------------------------------------------------ 画布信号

    def _connect_canvas_signals(self) -> None:
        c = self.canvas
        c.extentsChanged.connect(self._on_canvas_changed)
        c.layersChanged.connect(self._on_layers_changed)
        c.mapCanvasRefreshed.connect(self._on_canvas_refreshed)
        c.magnificationChanged.connect(self._on_canvas_changed)
        c.rotationChanged.connect(self._on_canvas_changed)
        self._connect_project_signals()

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
        self._disconnect_project_signals()

    def _on_canvas_changed(self) -> None:
        if not self._alive:
            return
        # 这里不 raise：画布还没重绘，置顶没意义；等 mapCanvasRefreshed
        # 画完再置顶，避免平移时每帧多做一次覆盖层提层。
        self._schedule_render()

    def _on_canvas_refreshed(self) -> None:
        ov = self._overlay
        if ov is not None:
            ov.raise_to_top()
        if self._needs_render:
            self._schedule_render()

    def _on_layers_changed(self) -> None:
        if not self._alive or self._restoring:
            return
        try:
            if (
                sip.isdeleted(self._base)
                or sip.isdeleted(self._compare)
                or self._base.id() not in self._project.mapLayers()
                or self._compare.id() not in self._project.mapLayers()
            ):
                # 任一侧影像被移除 → 退出分屏
                QTimer.singleShot(0, self._quit)
                return
            # 以图层树的有效可见性/顺序为准：分屏期间画布图层集被改写，
            # 桥接器再更新时未必发 layersChanged，读 canvas.layers() 会漏开关。
            self._hook_project_visibility()
            self._refresh_layers()
        except RuntimeError:
            return
        # 画布只保留左侧图层栈；右侧由覆盖层离屏绘制
        QTimer.singleShot(0, self._set_left_layers)
        self._schedule_render()

    # ------------------------------------------------------------ 离屏渲染

    def _schedule_render(self) -> None:
        if not self._alive:
            return
        self._needs_render = True
        # 节流：平移/缩放时 extentsChanged 很密，每次都起离屏渲染会和主画布
        # 抢 CPU，表现为卡顿。这里最多每 _RENDER_THROTTLE_MS 渲染一帧，
        # 且总是取到期时的最新范围。
        if self._render_scheduled or self._render_timer.isActive():
            return
        self._render_scheduled = True
        self._render_timer.start()

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
        """拷贝画布当前渲染参数（范围 / CRS / 旋转 / DPI / DPR / 尺寸），换图层。

        背景保持不透明（跟随工程背景色）：右侧画面要完整盖住底下画布的左侧
        图层栈，透明背景会把左侧内容透到右侧来。
        """
        try:
            settings = QgsMapSettings(self.canvas.mapSettings())
            settings.setOutputSize(self.canvas.viewport().size())  # 逻辑像素
            settings.setDevicePixelRatio(self.canvas.viewport().devicePixelRatioF())
            settings.setLayers(layers)
            background = settings.backgroundColor()
            if background.alpha() == 0:
                background = QColor(255, 255, 255)
            settings.setBackgroundColor(background)
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
        self._images = {"right": None}
        settings = self._render_settings(list(self._right_layers))
        if settings is not None:
            try:
                job = QgsMapRendererParallelJob(settings)
            except RuntimeError:
                job = None
            if job is not None:
                self._pending += 1
                self._jobs["right"] = job
                self._job_connections["right"] = job.finished.connect(
                    lambda s=seq: self._on_render_finished(s, "right")
                )
                try:
                    job.start()
                except RuntimeError:
                    conn = self._job_connections.pop("right", None)
                    if conn is not None:
                        try:
                            job.finished.disconnect(conn)
                        except (TypeError, RuntimeError):
                            pass
                    self._jobs.pop("right", None)
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
            ov.set_right_image(self._images.get("right"))
            ov.update()
        except RuntimeError:
            return
        # 渲染期间又来了新需求 → 节流后再渲一帧（取最新范围）
        if self._needs_render:
            self._schedule_render()
