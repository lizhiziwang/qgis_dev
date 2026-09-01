"""应用启动初始化状态页（splash）。

:class:`StartupSplash`：无边框、居中浮窗，在 :class:`QgsApplication` 创建后
立刻弹出，覆盖「登录 → initQgis → 翻译 → 插件注册 → 主窗口构建」等耗时
阶段，主窗口显示时由 bootstrap 关闭。

因为各阶段都在主线程**同步**执行，``set_status`` 内部会调
``QApplication.processEvents()`` 强制重绘一次，否则状态文字不会实时刷新。
状态文字后的省略号动画由 :class:`QTimer` 驱动——同步阶段里事件循环只在
``processEvents`` 时跑，所以动画按阶段步进（每次更新阶段动一格），
正好体现"正在干活"。
"""

from __future__ import annotations

from qgis.PyQt.QtCore import Qt, QTimer
from qgis.PyQt.QtGui import QGuiApplication
from qgis.PyQt.QtWidgets import QApplication, QLabel, QVBoxLayout, QWidget

from app.ui.common.icons import Icons
from app.ui.common.theme import ThemeManager


class StartupSplash(QWidget):
    """启动初始化状态页：应用图标 + 应用名 + 阶段文字（带动画省略号）。"""

    #: 固定尺寸（无边框浮窗）
    WIDTH = 420
    HEIGHT = 240

    #: 状态文字动画后缀（循环步进）
    _DOT_STEPS = ("", "·", "··", "···")

    def __init__(self, app_name: str = "qgis_dev", parent: QWidget | None = None) -> None:
        super().__init__(
            parent,
            Qt.WindowType.FramelessWindowHint | Qt.WindowType.Tool,
        )
        self.setObjectName("StartupSplash")
        # 让对象名的 QSS 背景色生效（普通 QWidget 默认不吃样式背景）
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        self.resize(self.WIDTH, self.HEIGHT)

        self._status_text = ""
        self._dot_step = 0
        self._dot_timer = QTimer(self)
        self._dot_timer.setInterval(300)
        self._dot_timer.timeout.connect(self._tick_dots)
        self._dot_timer.start()

        self._build_ui(app_name)
        self._apply_theme()
        self.set_status("正在初始化")

    # ------------------------------------------------------------ 装配

    def _build_ui(self, app_name: str) -> None:
        layout = QVBoxLayout(self)
        layout.setContentsMargins(36, 28, 36, 26)
        layout.setSpacing(14)

        self._icon = QLabel(self)
        self._icon.setPixmap(Icons.app().pixmap(72, 72))
        self._icon.setAlignment(Qt.AlignmentFlag.AlignCenter)
        layout.addWidget(self._icon)

        self._title = QLabel(app_name, self)
        self._title.setObjectName("SplashTitle")
        self._title.setAlignment(Qt.AlignmentFlag.AlignCenter)
        layout.addWidget(self._title)

        # 装饰性强调线：主题强调色，固定宽度，居中
        accent = QLabel(self)
        accent.setObjectName("SplashAccent")
        accent.setFixedSize(48, 2)
        layout.addWidget(accent, alignment=Qt.AlignmentFlag.AlignHCenter)

        layout.addStretch(1)

        self._status = QLabel(self)
        self._status.setObjectName("SplashStatus")
        self._status.setAlignment(Qt.AlignmentFlag.AlignCenter)
        layout.addWidget(self._status)

    # ------------------------------------------------------------ 对外接口

    def set_status(self, text: str) -> None:
        """更新阶段文字；省略号动画从第一格重新开始。"""
        self._status_text = text
        self._dot_step = 0
        self._refresh_status()
        # 同步初始化阶段下立即重绘，否则要等事件循环才能看到变化
        QApplication.processEvents()

    # ------------------------------------------------------------ 动画

    def _tick_dots(self) -> None:
        self._dot_step = (self._dot_step + 1) % len(self._DOT_STEPS)
        self._refresh_status()

    def _refresh_status(self) -> None:
        self._status.setText(f"{self._status_text}{self._DOT_STEPS[self._dot_step]}")

    def center_on_screen(self) -> None:
        """屏幕可用区居中（任务栏之外）。"""
        screen = QGuiApplication.primaryScreen()
        if screen is None:
            return
        geo = screen.availableGeometry()
        self.move(
            geo.center().x() - self.width() // 2,
            geo.center().y() - self.height() // 2,
        )

    # ------------------------------------------------------------ 主题 / 生命周期

    def _apply_theme(self) -> None:
        t = ThemeManager.instance().current()
        self.setStyleSheet(
            f"QWidget#StartupSplash {{"
            f"  background-color: {t.window_bg};"
            f"  border: 1px solid {t.border};"
            f"  border-radius: 8px;"
            f"}}"
            f"QLabel#SplashTitle {{"
            f"  color: {t.menu_text};"
            f"  font-size: 20px;"
            f"  font-weight: 600;"
            f"}}"
            f"QLabel#SplashAccent {{"
            f"  background-color: {t.input_focus};"
            f"  border-radius: 1px;"
            f"}}"
            f"QLabel#SplashStatus {{"
            f"  color: {t.text_secondary};"
            f"  font-size: 12px;"
            f"}}"
        )

    def closeEvent(self, event) -> None:  # noqa: N802 —— Qt 命名
        # 隐藏后不再空转动画定时器
        self._dot_timer.stop()
        super().closeEvent(event)
