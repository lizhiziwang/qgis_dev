"""画布左上角缩放工具。

竖向排列：放大按钮 → 垂直进度条（可拖动滑块）→ 缩小按钮。
滑块与画布比例尺做**对数空间**映射，每步恒定缩放因子（约 10.4%），
拖动 / 按钮缩放都围绕画布中心进行，缩放级别由 ``canvas.scaleChanged``
信号自动同步回来。

覆盖方式：本 QGIS 3.40 构建没有绑定 ``QgsMapCanvas.setOverlayWidget``，
故作为 canvas 的子 QWidget，用 ``move()`` 定位在左上角 + ``raise_()`` 置顶；
子控件坐标相对父左上角，父窗口 resize 时自动保持锚定。
"""

from __future__ import annotations

import math
from typing import Optional

from PyQt6.QtCore import QSize, Qt, QTimer
from PyQt6.QtGui import QColor
from PyQt6.QtWidgets import (
    QFrame,
    QGraphicsDropShadowEffect,
    QPushButton,
    QSizePolicy,
    QSlider,
    QVBoxLayout,
)

from app.infra.logging import get_logger
from app.ui.common.icons import Icons
from app.ui.common.theme import ThemeManager, canvas_zoom_qss

logger = get_logger(__name__)

# 滑块映射比例尺范围（比例尺分母，1:X；放大 → 分母变小）
MIN_SCALE = 500.0  # 1:500       —— 最放大（滑块顶，值 100）
MAX_SCALE = 10_000_000.0  # 1:1000 万 —— 最缩小（滑块底，值 0）
_STEPS = 100  # 滑块范围 0..100
_MARGIN = 8  # 距画布左上角边距

# 滑块拖动的缩放节流毫秒数：拖动时 valueChanged 高频触发，每个事件都同步
# 调 zoomScale 会在滑块鼠标事件栈内重入触发渲染 → QGIS 原生崩溃（0xC0000409）。
# 用固定间隔计时器合并高频拖动，只在事件栈外应用缩放。
_ZOOM_DEBOUNCE_MS = 40


class CanvasZoomTool(QFrame):
    """地图画布左上角的缩放工具。"""

    def __init__(self, canvas) -> None:
        super().__init__(canvas)
        self._canvas = canvas
        self._syncing = False  # 程序化回写滑块时置 True，避免反馈循环
        self._pending_scale: Optional[float] = None  # 节流待应用的缩放
        self._dragging = False  # 用户正在拖动滑块（拖动期间不回写手柄）
        self._zoom_timer = QTimer(self)
        self._zoom_timer.setInterval(_ZOOM_DEBOUNCE_MS)
        self._zoom_timer.timeout.connect(self._apply_zoom)
        self._build_ui()
        # 悬浮子控件不被父布局管理，show() 不会自动套用 sizeHint，
        # 必须显式 adjustSize()，否则塌缩成 30px 高 → 按钮重叠、滑块压扁。
        self.adjustSize()
        self._wire_signals()
        # 主题：面板 + 按钮 + 滑块配色统一走主题，切换时重绘
        ThemeManager.instance().theme_changed.connect(self._on_theme_changed)
        self.move(_MARGIN, _MARGIN)
        self._sync_from_canvas()
        self.raise_()

    def _on_theme_changed(self, theme) -> None:
        self.setStyleSheet(canvas_zoom_qss(theme))

    # ===================================================== 装配

    def _build_ui(self) -> None:
        self.setFixedWidth(60)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        self.setStyleSheet(canvas_zoom_qss(ThemeManager.instance().current()))
        # 柔和投影，让工具浮在地图
        shadow = QGraphicsDropShadowEffect(self)
        shadow.setBlurRadius(12)
        shadow.setOffset(0, 2)
        shadow.setColor(QColor(0, 0, 0, 60))
        self.setGraphicsEffect(shadow)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(7, 8, 7, 8)
        layout.setSpacing(6)

        # 放大按钮（顶部）——SVG 图标（放大镜 +）
        self.btn_in = QPushButton(self)
        self.btn_in.setIcon(Icons.zoom_in())
        self.btn_in.setIconSize(QSize(20, 20))
        self.btn_in.setToolTip("放大（+）")
        self.btn_in.setFixedHeight(34)
        layout.addWidget(self.btn_in)

        # 垂直进度条（滑块，可拖动缩放）。水平撑满面板宽度，让轨道与按钮
        # 在同一个竖直中心线上；设最小高度让轨道足够长、好抓取。
        self.slider = QSlider(Qt.Orientation.Vertical, self)
        self.slider.setRange(0, _STEPS)
        self.slider.setSingleStep(1)
        self.slider.setPageStep(5)
        self.slider.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        self.slider.setMinimumHeight(110)
        self.slider.setToolTip("拖动调整缩放级别")
        layout.addWidget(self.slider, 1)

        # 缩小按钮（底部）——SVG 图标（放大镜 -）
        self.btn_out = QPushButton(self)
        self.btn_out.setIcon(Icons.zoom_out())
        self.btn_out.setIconSize(QSize(20, 20))
        self.btn_out.setToolTip("缩小（−）")
        self.btn_out.setFixedHeight(34)
        layout.addWidget(self.btn_out)

    def _wire_signals(self) -> None:
        self.btn_in.clicked.connect(self._on_zoom_in_clicked)
        self.btn_out.clicked.connect(self._on_zoom_out_clicked)
        # valueChanged（而非 sliderMoved）保证点槽 / 键盘也能缩放
        self.slider.valueChanged.connect(self._on_slider_changed)
        self.slider.sliderPressed.connect(self._on_slider_pressed)
        self.slider.sliderReleased.connect(self._on_slider_released)
        self._canvas.scaleChanged.connect(self._sync_from_canvas)

    # ===================================================== 交互

    def _on_zoom_in_clicked(self) -> None:
        """放大：单次立即缩放，不切换当前激活工具。"""
        self._canvas.zoomIn()

    def _on_zoom_out_clicked(self) -> None:
        """缩小：单次立即缩放，不切换当前激活工具。"""
        self._canvas.zoomOut()

    def _on_slider_changed(self, value: int) -> None:
        """拖动滑块 → 记录目标比例尺，节流计时器统一应用。

        不在 valueChanged 信号栈里直接 ``zoomScale``：拖动高频触发时同步缩放
        会在滑块鼠标事件栈内重入渲染，导致 QGIS 0xC0000409 崩溃。改为记录
        最新目标，由固定间隔的 :attr:`_zoom_timer` 在事件栈外每 40ms 应用一次，
        拖动期间保持实时、又不重入。
        """
        if self._syncing:
            return
        self._pending_scale = self._slider_to_scale(value)
        if not self._zoom_timer.isActive():
            self._zoom_timer.start()

    def _on_slider_pressed(self) -> None:
        """按下滑块：标记拖动中，期间不回写手柄（避免 setValue 抢用户位置）。"""
        self._dragging = True

    def _on_slider_released(self) -> None:
        """松手：停表，把最终缩放与画布对齐推迟到事件栈外。

        sliderReleased 在鼠标事件栈里，直接调 ``zoomScale`` / ``setValue`` 和
        拖动期同步缩放一样有 0xC0000409 重入渲染风险，统一用 ``singleShot(0)``
        推到下一拍执行。
        """
        self._dragging = False
        self._zoom_timer.stop()
        QTimer.singleShot(0, self._apply_zoom)
        QTimer.singleShot(0, self._sync_from_canvas)

    def _apply_zoom(self) -> None:
        """节流计时器到期 / 松手：把画布缩放到最新目标比例尺。

        注意：本 QGIS 3.40 构建的 ``QgsMapCanvas.zoomScale(scale,
        ignoreScaleLock=False)`` 只有两个参数、**没有** center 参数（实测传第二参
        即 ``TypeError``，曾导致这里静默吞异常、滑块缩放"没反应"）。单参调用默认
        以当前画布中心为缩放中心（实测 center 保持不变），正好符合"围绕中心缩放"。
        """
        scale = self._pending_scale
        self._pending_scale = None
        if scale is None:
            return
        try:
            if self._canvas.extent().isEmpty():
                return
            self._canvas.zoomScale(scale)
        except Exception as exc:  # noqa: BLE001
            # 不静默吞掉：曾因第二参传 QgsPointXY 抛 TypeError 被吞，
            # 导致整个会话滑块缩放"没反应"。记录日志便于排查。
            logger.error("画布缩放失败 scale=%s: %s", scale, exc, exc_info=True)
            return

    def _sync_from_canvas(self) -> None:
        """画布缩放级别变化 → 回写滑块位置（外部缩放也同步）。

        用户正在拖动滑块时跳过回写：此时滑块值由鼠标主导，回写 setValue 会把
        手柄拉回已应用位置，和拖动打架（表现为"拖动没反应"）。
        """
        if self._dragging:
            return
        try:
            scale = self._canvas.mapSettings().scale()
            if self._canvas.extent().isEmpty():
                return
        except Exception:  # noqa: BLE001
            return
        value = self._scale_to_slider(scale)
        self._syncing = True
        try:
            self.slider.setValue(value)
        finally:
            self._syncing = False
        # 画布重绘后覆盖层可能被 viewport 盖住，重新置顶
        self.raise_()

    # ===================================================== 映射

    def _slider_to_scale(self, value: int) -> float:
        """滑块值 → 比例尺分母（对数空间线性插值）。"""
        ratio = MIN_SCALE / MAX_SCALE
        return MAX_SCALE * (ratio ** (value / _STEPS))

    def _scale_to_slider(self, scale: float) -> int:
        """比例尺分母 → 滑块值（越界钳制到端点）。"""
        if scale <= 0:
            return 0
        value = _STEPS * math.log(scale / MAX_SCALE) / math.log(MIN_SCALE / MAX_SCALE)
        return max(0, min(_STEPS, int(round(value))))

    # ===================================================== 置顶保持

    def showEvent(self, event) -> None:
        """每次显示都重新置顶，避免被画布 viewport 压住。"""
        super().showEvent(event)
        self.raise_()
