"""画布上的毛玻璃面板（backdrop blur）。

把面板**背后**的地图做高斯模糊后当作面板底图，再叠加一层半透明的主题底色，
得到 Windows Acrylic / macOS Vibrancy 那种毛玻璃质感（底色跟随主题，深浅主题
下分别是浅灰 / 深灰半透明）。

Qt 的 ``QGraphicsBlurEffect`` 只能模糊控件*自身*绘制的内容，拍不到控件背后
的父窗口，所以这里手动取景：

1. ``QgsMapCanvas`` 是 ``QGraphicsView``，地图画在 ``canvas.viewport()`` 这个
   子控件上；面板是画布的**另一个子控件**，与 viewport 同级、叠在它上面。
   取景用 ``canvas.viewport().grab(区域)``，拿到的**只有地图**（覆盖层是画布
   的兄弟子控件，不在 viewport 里，不会被拍进去）；**不能**用
   ``canvas.grab()``——那会把面板自己、消息条等覆盖层一起画进底图。
2. 用 ``QGraphicsBlurEffect`` 走离屏渲染做高斯模糊（Qt 没有直接可用的
   "模糊 QPixmap" 接口，这是官方推荐的迂回做法）。
3. ``paintEvent`` 里先画模糊底图，再叠加 ``主题底色 @ 60%``，最后描一圈
   细边框 + 圆角；圆角外不绘制（``WA_TranslucentBackground``）→ 露出画布。

坐标换算：面板在画布坐标系里的位置要减去 viewport 的边框偏移
（``QGraphicsView`` 的 viewport 通常从 (1, 1) 开始）才落到 viewport 坐标系。

性能：取景 + 模糊属于"贵操作"，用 40ms 单发计时器合并——画布连续重绘
（平移 / 缩放）时最多每 40ms 重新取景一次，且只在事件栈外执行 ``grab()``。
"""

from __future__ import annotations

from qgis.PyQt.QtCore import QRect, QRectF, Qt, QTimer
from qgis.PyQt.QtGui import QBrush, QColor, QImage, QPainter, QPainterPath, QPen, QPixmap
from qgis.PyQt.QtWidgets import (
    QGraphicsBlurEffect,
    QGraphicsPixmapItem,
    QGraphicsScene,
    QWidget,
)

from app.ui.common.theme import ThemeManager

#: 取景（grab + 模糊）合并间隔（毫秒）：画布连续重绘时合并、节流
_REFRESH_INTERVAL_MS = 40

#: 默认外观参数（逻辑像素 / 0..1 不透明度）
_DEFAULT_RADIUS = 10
_DEFAULT_BLUR_RADIUS = 18
_DEFAULT_OPACITY = 0.6


def _blur_pixmap(source: QPixmap, radius: float) -> QPixmap:
    """用 ``QGraphicsBlurEffect`` 对 pixmap 做高斯模糊，返回同尺寸新 pixmap。

    ``QGraphicsBlurEffect`` 会把 item 的 ``boundingRect`` 向外扩 ``radius``，
    所以先把场景矩形钉回图像本身，否则 ``scene.render`` 会按放大后的场景取景 → 错位。
    """
    if source.isNull() or radius <= 0:
        return source
    scene = QGraphicsScene()
    item = QGraphicsPixmapItem(source)
    effect = QGraphicsBlurEffect()
    effect.setBlurRadius(radius)
    effect.setBlurHints(QGraphicsBlurEffect.BlurHint.QualityHint)
    item.setGraphicsEffect(effect)
    scene.addItem(item)  # 所有权交给场景
    scene.setSceneRect(QRectF(0, 0, source.width(), source.height()))

    out = QImage(source.size(), QImage.Format.Format_ARGB32_Premultiplied)
    out.fill(Qt.GlobalColor.transparent)
    painter = QPainter(out)
    painter.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform, True)
    scene.render(painter, QRectF(out.rect()), scene.sceneRect())
    painter.end()

    blurred = QPixmap.fromImage(out)
    blurred.setDevicePixelRatio(source.devicePixelRatio())
    return blurred


class FrostedPanel(QWidget):
    """悬浮在 ``QgsMapCanvas`` 上的毛玻璃容器。

    作为画布子控件使用（``FrostedPanel(canvas)`` 已把自身 parent 设为画布），
    定位仍由调用方 ``move()`` / ``resize`` 决定；本控件只负责"背景=背后地图的
    高斯模糊 + 主题底色 60% 半透明"。

    :param canvas: 承载面板的画布；为 ``None`` 时退化为不透明纯色面板
        （取不到底图，便于测试 / 非画布场景），不抛异常
    :param radius: 圆角半径（逻辑像素）
    :param blur_radius: 高斯模糊半径（逻辑像素，会按 devicePixelRatio 放大）
    :param opacity: 叠加的主题底色不透明度（0..1），默认 ``0.6``
    """

    def __init__(
        self,
        canvas=None,
        *,
        radius: int = _DEFAULT_RADIUS,
        blur_radius: int = _DEFAULT_BLUR_RADIUS,
        opacity: float = _DEFAULT_OPACITY,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent if parent is not None else canvas)
        self._canvas = canvas
        self._radius = radius
        self._blur_radius = blur_radius
        self._opacity = opacity
        self._backdrop: QPixmap | None = None  # 模糊后的地图底图
        # 圆角外不绘制 → 露出画布
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        # 取景节流：连续重绘时不重复 grab / 模糊
        self._refresh_timer = QTimer(self)
        self._refresh_timer.setSingleShot(True)
        self._refresh_timer.setInterval(_REFRESH_INTERVAL_MS)
        self._refresh_timer.timeout.connect(self.refresh_backdrop)
        if canvas is not None:
            canvas.mapCanvasRefreshed.connect(self._schedule_refresh)
        ThemeManager.instance().theme_changed.connect(self._on_theme_changed)

    # ===================================================== 取景

    def refresh_backdrop(self) -> None:
        """重新抓取背后地图并模糊，作为面板底图（事件栈外调用）。"""
        canvas = self._canvas
        if canvas is None or not self.isVisible() or self.width() <= 0:
            return
        if self.parentWidget() is not canvas:
            return  # 尚未挂到画布上，坐标换算没有意义
        viewport = canvas.viewport()
        if viewport is None or viewport.width() <= 0 or viewport.height() <= 0:
            return
        # 画布坐标系 → viewport 坐标系（viewport 有边框偏移，通常是 1px）
        vp_rect = QRect(self.pos() - viewport.pos(), self.size())
        vp_rect = vp_rect.intersected(viewport.rect())
        if vp_rect.isEmpty():
            return
        source = viewport.grab(vp_rect)
        if source.isNull():
            return
        dpr = source.devicePixelRatio() or 1.0
        self._backdrop = _blur_pixmap(source, self._blur_radius * dpr)
        self.update()

    def _schedule_refresh(self, *_args) -> None:
        """画布重绘完成 → 节流地安排一次重新取景。"""
        if not self._refresh_timer.isActive():
            self._refresh_timer.start()

    def _on_theme_changed(self, _theme=None) -> None:
        """主题切换：底色/边框色在 ``paintEvent`` 里现取，重绘即可。"""
        self.update()

    # ===================================================== 生命周期

    def resizeEvent(self, event) -> None:
        super().resizeEvent(event)
        # 旧底图尺寸已失效，先丢弃（回退到不透明底色），等节流取景补上
        self._backdrop = None
        self._schedule_refresh()

    def showEvent(self, event) -> None:
        super().showEvent(event)
        self.raise_()  # 盖在同级 viewport 之上
        self._schedule_refresh()

    def hideEvent(self, event) -> None:
        super().hideEvent(event)
        self._refresh_timer.stop()

    # ===================================================== 绘制

    def paintEvent(self, event) -> None:
        """模糊底图 + 主题底色 60% + 圆角细边框。"""
        theme = ThemeManager.instance().current()
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)

        # 半像素内缩：1px 边框正好落在像素内，不糊
        path = QPainterPath()
        path.addRoundedRect(
            QRectF(self.rect()).adjusted(0.5, 0.5, -0.5, -0.5),
            self._radius,
            self._radius,
        )
        painter.setClipPath(path)  # 圆角外不绘制 → 露出画布

        if self._backdrop is not None and not self._backdrop.isNull():
            painter.drawPixmap(0, 0, self._backdrop)
        else:
            # 兜底：未取到画布 / 底图失效时用不透明主题底色，避免脏底
            painter.fillPath(path, QBrush(QColor(theme.window_bg)))

        # 主题底色 @ opacity（默认 60%）：叠在模糊地图上 → 毛玻璃
        tint = QColor(theme.window_bg)
        tint.setAlphaF(self._opacity)
        painter.fillPath(path, QBrush(tint))

        # 细边框：把面板从地图上"框"出来
        painter.setClipping(False)
        pen = QPen(QColor(theme.border))
        pen.setWidth(1)
        painter.setPen(pen)
        painter.setBrush(Qt.BrushStyle.NoBrush)
        painter.drawPath(path)
