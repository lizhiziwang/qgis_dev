"""状态栏：鼠标坐标 + 比例尺 + 投影选择器 + 图层加载指示。

图层加载指示是 QGIS 风格的那个 busy 效果：状态栏最左侧转圈图标 + 「正在加载
图层…」文本，由 :meth:`StatusBar.show_loading` / :meth:`hide_loading` 控制
（MainWindow 在加载文件 / 浏览器图层 / 在线底图等入口调用，加载期间亮起、
结束即熄灭）。可嵌套：并发多个加载时计数配平，全部结束才隐藏。
"""

from __future__ import annotations

from qgis.PyQt.QtCore import QEventLoop, Qt, QTimer
from qgis.PyQt.QtWidgets import QApplication, QHBoxLayout, QLabel, QMainWindow, QWidget

from app.infra.logging import get_logger

logger = get_logger(__name__)

#: 图层加载指示的旋转帧（转圈动画字符序列）
_LOADING_FRAMES = ("◐", "◓", "◑", "◒")
#: 旋转帧切换间隔（ms）
_LOADING_INTERVAL_MS = 120
#: 未指定图层名时的默认加载提示
_LOADING_DEFAULT_TEXT = "正在加载图层…"
#: 画布渲染超过该时长才亮指示（避免普通缩放/平移快速重绘时闪烁）
_RENDER_BUSY_DELAY_MS = 250
#: 画布渲染 busy 的文本
_RENDER_BUSY_TEXT = "正在渲染图层…"


class StatusBar:
    """状态栏构建器。"""

    def __init__(self, main_window: QMainWindow) -> None:
        self.main_window = main_window
        self.status_bar = main_window.statusBar()

    def build(self) -> None:
        """组装状态栏：左侧消息栏 + 加载指示，右侧 鼠标坐标 + 比例尺 + 投影选择器。

        加载指示平时隐藏，图层加载时由 :meth:`show_loading` 亮起（见模块
        docstring）；按需求先放在最左侧（普通部件区，消息之后、坐标比例尺等
        永久部件之前）。
        """
        from qgis.gui import QgsProjectionSelectionWidget, QgsScaleWidget

        # self.status_bar.showMessage("就绪")

        # 鼠标坐标（最左侧永久部件，随鼠标移动实时更新）
        self.coord_label = self._build_coordinate_label()
        self.status_bar.addPermanentWidget(self.coord_label)

        # 比例尺：本构建里 QgsScaleWidget(canvas) 的构造参数不生效（没有 setCanvas），
        # 只显示默认 1:1,000,000，且永远不随缩放更新 —— "比例尺未生效" 的根因。
        # 必须显式 setMapCanvas + 连接 scaleChanged，否则缩放后比例尺不变。
        self.scale_widget = QgsScaleWidget()
        self.scale_widget.setMapCanvas(self.main_window.canvas)
        self.scale_widget.updateScales()
        self.scale_widget.setMinimumWidth(120)
        self.main_window.canvas.scaleChanged.connect(self._on_canvas_scale_changed)
        self._update_scale()
        self.status_bar.addPermanentWidget(self.scale_widget)

        # 投影选择器：构造后 crs() 默认无效（authid 空），需显式 setCrs 到当前工程
        # 才能显示正确的 CRS；否则控件永久停留在"无效投影"状态。
        # 同时把 ProjectCrs 选项隐藏（占用空间大、不需要），并绑定 crsChanged 信号
        # 在工程 CRS 变更时同步。
        from qgis.core import Qgis, QgsProject

        proj_selector = QgsProjectionSelectionWidget(self.main_window)
        # 限制下拉最大宽度，避免占用过多状态栏空间
        proj_selector.setMaximumWidth(200)
        # CRS 由 MainWindow 注入前先隐藏 ProjectCrs 选项
        try:
            proj_selector.setOptionVisible(proj_selector.CrsOption.ProjectCrs, False)
        except Exception:  # noqa: BLE001
            pass
        # 拿当前工程 CRS：MainWindow 装配顺序在 _build_status_bar 之前已
        # 把 ``self.project = QgsProject.instance()`` 绑好。若宿主未挂
        # ``project`` 属性（裸 QMainWindow 测试），退回到全局单例。
        project = getattr(self.main_window, "project", None) or QgsProject.instance()

        def _open_crs_pick_dialog() -> None:
            """点状态栏投影按钮：改用图层属性「坐标系统」的 CrsPickDialog。"""
            from qgis.core import QgsCoordinateReferenceSystem, QgsVectorLayer
            from qgis.PyQt.QtWidgets import QDialog

            # 顶层导入会经 base_dialog → frameless → main_window 形成循环
            from app.ui.dialogs.crs_pick_dialog import CrsPickDialog  # noqa: PLC0415 - 避免循环导入

            try:
                current_crs = project.crs()
                current = (
                    (current_crs.authid() or current_crs.toWkt())
                    if current_crs.isValid()
                    else ""
                )
                dlg = CrsPickDialog(
                    self.main_window,
                    current=current,
                    layers=[
                        layer
                        for layer in project.mapLayers().values()
                        if isinstance(layer, QgsVectorLayer)
                    ],
                )
                try:
                    accepted = dlg.exec() == QDialog.DialogCode.Accepted
                    value = dlg.crs_value() if accepted else ""
                finally:
                    dlg.deleteLater()
                if not value:
                    return
                crs = QgsCoordinateReferenceSystem(value)
                if not crs.isValid():
                    crs = QgsCoordinateReferenceSystem.fromWkt(value)
                if not crs.isValid():
                    self.main_window.messageBar.pushMessage(
                        f"无法识别的坐标系：{value}",
                        level=Qgis.MessageLevel.Warning,
                        duration=5,
                    )
                    return
                # setCrs 会触发下方 _on_proj_selector_changed，由它统一写回
                # 工程 + 画布并给出提示，避免两处重复维护切换逻辑。
                proj_selector.setCrs(crs)
            except RuntimeError:
                pass  # 控件 / 窗口销毁流程中
            except Exception as exc:  # noqa: BLE001
                logger.error("打开坐标系选择失败: %s", exc, exc_info=True)
                self.main_window.messageBar.pushMessage(
                    f"打开坐标系选择失败：{exc}",
                    level=Qgis.MessageLevel.Warning,
                    duration=5,
                )

        # QgsProjectionSelectionWidget 内部是「QComboBox + QToolButton」：按钮的
        # clicked 直连 C++ 的 selectCrs()（非虚函数，Python 子类覆盖不到），
        # 只能断开这条连接后换成上面的 CrsPickDialog 入口；控件外观与最近
        # CRS 下拉保持不变。找不到按钮 / 已无连接时退回 QGIS 默认弹窗。
        try:
            from qgis.PyQt.QtWidgets import QToolButton

            crs_button = proj_selector.findChild(QToolButton)
            if crs_button is not None:
                crs_button.clicked.disconnect()
                crs_button.clicked.connect(_open_crs_pick_dialog)
        except TypeError:
            pass
        except Exception as exc:  # noqa: BLE001
            logger.error("替换坐标系选择弹窗失败: %s", exc, exc_info=True)

        proj_selector.setCrs(project.crs())

        # crsChanged 是无参信号，setCrs 需要 CRS 参数 → 包一层 lambda。
        # 控件被销毁后单例 project 仍可能发信号 → 闭包访问已死的 sip 对象会
        # 抛 RuntimeError，所以再 try 一层（见 qgis-ui-integration-gotchas）。
        def _sync_proj_selector() -> None:
            try:
                proj_selector.setCrs(project.crs())
            except RuntimeError:
                pass

        project.crsChanged.connect(_sync_proj_selector)

        # 反过来：在状态栏选一个新 CRS = 真的切工程坐标系。
        # **以前只做了「工程 → 控件」单向同步**，用户点选只改了控件自己显示的那
        # 一行字，``project`` 和画布纹丝不动 —— 用户报的"切换工程坐标系没效果"
        # 就是这个：控件显示 3857、工程其实还是 4490，两边从此对不上。
        # 控件在 _sync_proj_selector 里被程序化 setCrs 时也会发 crsChanged，
        # 与当前工程 CRS 相同就直接返回，避免自激循环。
        def _on_proj_selector_changed(crs) -> None:
            try:
                if crs is None or not crs.isValid() or crs == project.crs():
                    return
                project.setCrs(crs)
                # 画布 destinationCrs 是另一份状态，不跟着改就还是按旧 CRS 出图
                canvas = getattr(self.main_window, "canvas", None)
                if canvas is not None:
                    canvas.setDestinationCrs(crs)
                self.main_window.messageBar.pushMessage(
                    f"工程坐标系已切换为 {crs.authid()}（{crs.description()}）",
                    level=Qgis.MessageLevel.Info,
                    duration=3,
                )
            except RuntimeError:
                pass  # 控件 / 窗口销毁流程中

        proj_selector.crsChanged.connect(_on_proj_selector_changed)
        self.status_bar.addPermanentWidget(proj_selector)

        # 图层加载指示：转圈 + 文本，平时隐藏（show_loading 时亮起）。
        # 用 addWidget 放普通部件区（状态栏左侧）；addPermanentWidget 会挤到最右。
        (
            self.loading_widget,
            self.loading_spinner,
            self.loading_text,
        ) = self._build_loading_indicator()
        self._loading_depth = 0
        self._loading_frame = 0
        self._loading_timer = QTimer(self.status_bar)
        self._loading_timer.setInterval(_LOADING_INTERVAL_MS)
        self._loading_timer.timeout.connect(self._spin_loading)
        self.status_bar.addWidget(self.loading_widget)

        # 画布渲染 busy：首帧渲染 / 大图层重绘超过阈值时亮起指示。
        # 在线底图 FeatureServer 这类“添加很快、首帧渲染 20~30s”的场景全靠
        # 这里兜底（渲染在 QGIS 后台线程，UI 事件循环照常跑，转圈能真正动起来）。
        self._rendering = False
        self._render_busy = False
        try:
            canvas = self.main_window.canvas
            canvas.renderStarting.connect(self._on_canvas_render_started)
            canvas.renderComplete.connect(self._on_canvas_render_finished)
            # 兜底：渲染被中断（平移/缩放取消旧渲染）时 mapCanvasRefreshed 也会发，
            # 用它保证 _rendering 标志最终复位，避免指示卡住不熄。
            canvas.mapCanvasRefreshed.connect(self._on_canvas_render_finished)
        except Exception:  # noqa: BLE001
            # 个别 QGIS 版本无对应信号：退化为纯手动 show/hide 指示
            pass

    # ------------------------------------------------------------ 图层加载指示

    @property
    def is_loading(self) -> bool:
        """是否正在显示图层加载指示（部件未隐藏 且 旋转动画在跑）。"""
        return not self.loading_widget.isHidden() and self._loading_timer.isActive()

    def show_loading(self, text: str = "") -> None:
        """开始显示图层加载指示（左侧大号转圈 + 文本）；与 :meth:`hide_loading` 配对。

        可嵌套（多个图层并发加载）：内部计数，全部结束时才隐藏——避免 A 先加载完
        时把还在加载中的 B 的指示一并关掉。
        """
        self._loading_depth += 1
        if text:
            self.loading_text.setText(text)
        self.loading_widget.show()
        if not self._loading_timer.isActive():
            self._spin_loading()
            self._loading_timer.start()
        # 立即让出一次事件循环把部件画出来。调用方（文件打开、在线底图构建等）
        # 随后常是主线程同步阻塞——不主动刷一次 paint 的话，重绘要等阻塞结束、
        # 指示已被 hide 之后才轮到，效果就是“加载效果不出现”。
        # 注意用全限定枚举名（PyQt6/QGIS 4.2 无 QEventLoop.ExcludeUserInputEvents
        # 非限定别名，PyQt5 两者都有；全限定名两者通用）。
        QApplication.processEvents(QEventLoop.ProcessEventsFlag.ExcludeUserInputEvents)

    def hide_loading(self) -> None:
        """结束一层加载；计数归零才隐藏指示并停掉旋转动画。"""
        if self._loading_depth > 0:
            self._loading_depth -= 1
        if self._loading_depth == 0:
            self._loading_timer.stop()
            self.loading_widget.hide()

    # ---- 画布渲染 busy（异步渲染的兜底指示）----

    def _on_canvas_render_started(self) -> None:
        """画布开始渲染：延迟探测，渲染很快结束（<阈值）就不亮，避免闪烁。"""
        self._rendering = True
        QTimer.singleShot(_RENDER_BUSY_DELAY_MS, self._maybe_show_render_busy)

    def _maybe_show_render_busy(self) -> None:
        """渲染超过阈值仍在跑 → 亮起指示（UI 事件循环未阻塞，转圈动画正常）。"""
        try:
            if self._rendering and not self._render_busy:
                self._render_busy = True
                self.show_loading(_RENDER_BUSY_TEXT)
        except RuntimeError:
            # 延时探测触发时宿主窗口可能已销毁（测试/快速关闭场景）
            pass

    def _on_canvas_render_finished(self, *args) -> None:
        """渲染结束 / 被刷新打断：复位标志并熄灭指示。"""
        try:
            self._rendering = False
            if self._render_busy:
                self._render_busy = False
                self.hide_loading()
        except RuntimeError:
            pass

    def _spin_loading(self) -> None:
        """旋转帧推进（QTimer 回调，转圈动画）。"""
        self._loading_frame = (self._loading_frame + 1) % len(_LOADING_FRAMES)
        self.loading_spinner.setText(_LOADING_FRAMES[self._loading_frame])

    def _build_loading_indicator(self):
        """左侧加载指示部件：大号转圈图标 + 文本，默认隐藏。"""
        widget = QWidget(self.status_bar)
        layout = QHBoxLayout(widget)
        layout.setContentsMargins(10, 0, 6, 0)
        layout.setSpacing(6)
        spinner = QLabel(_LOADING_FRAMES[0])
        # 旋转符号放大（原 9~11px → 22px，约一倍）并加粗，确保加载期间一眼可见
        spinner.setStyleSheet("color: #2f6fb3; font-weight: bold; font-size: 22px;")
        text = QLabel(_LOADING_DEFAULT_TEXT)
        text.setStyleSheet("color: #333; font-weight: bold;")
        layout.addWidget(spinner)
        layout.addWidget(text)
        widget.hide()
        return widget, spinner, text

    def _build_coordinate_label(self) -> QLabel:
        """鼠标位置坐标标签：跟随画布 xyCoordinates 信号更新。"""
        label = QLabel("—, —")
        label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        label.setMinimumWidth(150)
        label.setToolTip("当前鼠标位置坐标")
        canvas = self.main_window.canvas
        canvas.xyCoordinates.connect(self._on_xy_coordinates)
        # 切换投影后坐标显示格式可能变化（地理 / 投影），重置占位
        canvas.destinationCrsChanged.connect(self._on_destination_crs_changed)
        return label

    # ---- 比例尺 ----

    def _on_canvas_scale_changed(self, *args) -> None:
        self._update_scale()

    def _update_scale(self) -> None:
        """把画布当前比例同步到比例尺控件。

        canvas.scale() 在尚未设置范围时为 1.0（哨兵值），此时保留控件默认
        1:1,000,000，避免显示怪异的 1:1。
        """
        if self.main_window.canvas.scale() > 1.0:
            self.scale_widget.setScaleFromCanvas()

    # ---- 鼠标坐标 ----

    def _on_destination_crs_changed(self) -> None:
        self._on_xy_coordinates(None)

    def _on_xy_coordinates(self, point) -> None:
        """鼠标移动时更新坐标标签；离开画布（空点）时显示占位。"""
        if point is None or point.isEmpty():
            self.coord_label.setText("—, —")
            return
        x, y = point.x(), point.y()
        crs = self.main_window.canvas.mapSettings().destinationCrs()
        if crs is not None and crs.isValid() and crs.isGeographic():
            text = f"{x:.5f}°, {y:.5f}°"
        else:
            text = f"{x:.1f}, {y:.1f}"
        self.coord_label.setText(text)
