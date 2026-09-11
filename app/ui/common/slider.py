"""自绘的 FlatSlider，替代 QSlider 解决 Qt windowsvista 风格硬编码灰底问题。

为什么不用 QSlider：Qt 6 windowsvista 风格在 ``QSlider`` 控件自身（groove 上
下空白处）直接画 ``PE_PanelItemViewItem``（menu_hover 浅灰），QSS background、
WA_NoSystemBackground、WA_OpaquePaintEvent、paintEvent 钩子都拦不住——
参见 ``.tmp/probe_slider_v6.py`` / ``probe_flatslider.py`` 的像素验证。

参照 Element Plus 2.x 的 Slider：白底圆手柄 + 蓝色填充轨道 + 圆角空轨道。
颜色全部走 ``ThemeManager.instance().current()``，主题切换时通过
``theme_changed`` 信号自动 ``update()``。
"""

from __future__ import annotations

from qgis.PyQt.QtCore import QPointF, QRectF, QSize, Qt, pyqtSignal
from qgis.PyQt.QtGui import QBrush, QColor, QPainter, QPen
from qgis.PyQt.QtWidgets import QSizePolicy, QWidget

from app.ui.common.theme import Theme, ThemeManager


class FlatSlider(QWidget):
    """Element Plus 风格的水平滑块。

    API 与 :class:`QSlider` 习惯对齐：``setRange`` / ``setValue`` / ``value`` /
    ``valueChanged``，所以 ``general.py`` 里 ``self.opacity_slider`` 的所有调用
    不用改。
    """

    valueChanged = pyqtSignal(int)
    sliderPressed = pyqtSignal()
    sliderReleased = pyqtSignal()

    # 几何常量（与 Element Plus 2.x Slider 对齐）
    TRACK_HEIGHT = 4
    HANDLE_DIAMETER = 16
    MIN_HEIGHT = 24  # 手柄 16 + 上下各 4 留白
    SHADOW_ALPHA = 40  # 按下时手柄阴影

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._min = 0
        self._max = 100
        self._value = 0
        self._step = 1
        self._pressed = False

        self.setMinimumHeight(self.MIN_HEIGHT)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        self.setMouseTracking(True)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        # 控件自身不画系统底色；完全由 paintEvent 自己掌控
        self.setAttribute(Qt.WidgetAttribute.WA_OpaquePaintEvent, True)

        # 主题切换 → 重绘
        ThemeManager.instance().theme_changed.connect(lambda _t: self.update())

    # ----------------------------------------------------------- 公开 API

    def setRange(self, min_v: int, max_v: int) -> None:
        if max_v < min_v:
            max_v = min_v
        self._min = min_v
        self._max = max_v
        self._value = max(self._min, min(self._value, self._max))
        self.update()

    def setMinimum(self, v: int) -> None:
        self.setRange(v, self._max)

    def setMaximum(self, v: int) -> None:
        self.setRange(self._min, v)

    def setSingleStep(self, step: int) -> None:
        self._step = max(1, int(step))

    def singleStep(self) -> int:
        return self._step

    def setValue(self, v: int) -> None:
        clamped = max(self._min, min(int(v), self._max))
        if clamped != self._value:
            self._value = clamped
            self.update()
            self.valueChanged.emit(self._value)

    def value(self) -> int:
        return self._value

    def sizeHint(self) -> QSize:
        return QSize(200, self.MIN_HEIGHT)

    # ----------------------------------------------------------- 内部计算

    def _track_rect(self) -> QRectF:
        """轨道矩形：水平居中，两端各留 HANDLE_RADIUS 让手柄不溢出。"""
        h = self.TRACK_HEIGHT
        y = (self.height() - h) / 2.0
        r = self.HANDLE_DIAMETER / 2.0
        return QRectF(r, y, max(0.0, self.width() - 2 * r), h)

    def _value_to_x(self, v: int) -> float:
        rect = self._track_rect()
        span = rect.width()
        if span <= 0 or self._max == self._min:
            return rect.left()
        ratio = (v - self._min) / (self._max - self._min)
        return rect.left() + ratio * span

    def _x_to_value(self, x: float) -> int:
        rect = self._track_rect()
        if rect.width() <= 0 or self._max == self._min:
            return self._min
        ratio = (x - rect.left()) / rect.width()
        ratio = max(0.0, min(1.0, ratio))
        raw = self._min + ratio * (self._max - self._min)
        # 按 step 对齐（默认 1 等价于四舍五入）
        stepped = round((raw - self._min) / self._step) * self._step + self._min
        return max(self._min, min(self._max, int(stepped)))

    def _handle_center(self) -> QPointF:
        return QPointF(self._value_to_x(self._value), self.height() / 2.0)

    # ----------------------------------------------------------- 颜色

    def _colors(self, t: Theme) -> dict[str, QColor]:
        return {
            "track_empty": QColor(t.border),
            "track_filled": QColor(t.input_focus),
            "handle_fill": QColor(t.window_bg),
            "handle_border": QColor(t.input_focus),
            "shadow": QColor(0, 0, 0, self.SHADOW_ALPHA),
        }

    # ----------------------------------------------------------- 绘制

    def paintEvent(self, _event) -> None:
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        t = ThemeManager.instance().current()
        c = self._colors(t)
        rect = self._track_rect()
        hpos = self._value_to_x(self._value)
        mid_y = self.height() / 2.0
        radius = rect.height() / 2.0

        # 0) 控件整域填主题底色，避免 WA_OpaquePaintEvent + 未画区域出现默认黑底
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(QBrush(QColor(t.window_bg)))
        painter.drawRect(self.rect())

        # 1) 轨道空段（全长铺一层 border 色打底）
        painter.setBrush(QBrush(c["track_empty"]))
        painter.drawRoundedRect(rect, radius, radius)

        # 2) 轨道已填充段（手柄左侧）
        if hpos > rect.left():
            filled = QRectF(rect.left(), rect.y(), hpos - rect.left(), rect.height())
            painter.setBrush(QBrush(c["track_filled"]))
            painter.drawRoundedRect(filled, radius, radius)

        # 3) 手柄阴影（按下时）
        if self._pressed:
            painter.setBrush(QBrush(c["shadow"]))
            handle_r = self.HANDLE_DIAMETER / 2.0 + 1
            painter.drawEllipse(QPointF(hpos, mid_y), handle_r + 1, handle_r + 1)

        # 4) 手柄（白底蓝边圆）
        handle_r = self.HANDLE_DIAMETER / 2.0 + (1 if self._pressed else 0)
        painter.setBrush(QBrush(c["handle_fill"]))
        painter.setPen(QPen(c["handle_border"], 2))
        painter.drawEllipse(QPointF(hpos, mid_y), handle_r, handle_r)

        painter.end()

    # ----------------------------------------------------------- 交互

    def mousePressEvent(self, event) -> None:
        if event.button() == Qt.MouseButton.LeftButton:
            self._pressed = True
            self.sliderPressed.emit()
            self.setValue(self._x_to_value(event.position().x()))
            event.accept()

    def mouseMoveEvent(self, event) -> None:
        if self._pressed:
            self.setValue(self._x_to_value(event.position().x()))
            event.accept()

    def mouseReleaseEvent(self, event) -> None:
        if event.button() == Qt.MouseButton.LeftButton and self._pressed:
            self._pressed = False
            self.sliderReleased.emit()
            self.update()
            event.accept()

    def enterEvent(self, event) -> None:
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        super().enterEvent(event)

    def leaveEvent(self, event) -> None:
        self.unsetCursor()
        super().leaveEvent(event)

    def keyPressEvent(self, event) -> None:
        k = event.key()
        if k in (Qt.Key.Key_Right, Qt.Key.Key_Up):
            self.setValue(self._value + self._step)
            event.accept()
        elif k in (Qt.Key.Key_Left, Qt.Key.Key_Down):
            self.setValue(self._value - self._step)
            event.accept()
        elif k == Qt.Key.Key_Home:
            self.setValue(self._min)
            event.accept()
        elif k == Qt.Key.Key_End:
            self.setValue(self._max)
            event.accept()
        else:
            super().keyPressEvent(event)