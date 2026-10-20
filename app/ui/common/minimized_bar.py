"""把非模态对话框“最小化”成画布底部的一条细条。

与「属性录入」同一套交互：无边框弹窗最小化到系统任务栏后往往没有可靠的
还原入口（owned window 没有任务栏按钮），所以在画布底部浮出一条摘要 +
「恢复」按钮。细条是画布的子控件，不占布局，画布刷新时重新贴底。
"""

from __future__ import annotations

from qgis.PyQt.QtCore import Qt
from qgis.PyQt.QtWidgets import QFrame, QHBoxLayout, QLabel, QPushButton

from app.ui.common.theme import ThemeManager


class MinimizedBar(QFrame):
    """浮在画布底部的「已最小化」细条：一行摘要 + 「恢复」。"""

    def __init__(self, canvas, on_restore) -> None:
        super().__init__(canvas)
        self._canvas = canvas
        self._on_restore = on_restore
        self.setObjectName("MinimizedBottomBar")
        self.setCursor(Qt.CursorShape.ArrowCursor)
        self.hide()

        row = QHBoxLayout(self)
        row.setContentsMargins(10, 4, 10, 4)
        row.setSpacing(8)
        self._label = QLabel("")
        row.addWidget(self._label, 1)
        restore = QPushButton("恢复")
        restore.setObjectName("MinimizedBottomRestore")
        restore.setCursor(Qt.CursorShape.ArrowCursor)
        restore.clicked.connect(self._restore)
        row.addWidget(restore)

        self._apply_style()
        ThemeManager.instance().theme_changed.connect(self._on_theme_changed)
        canvas.mapCanvasRefreshed.connect(self.reposition)

    def set_summary(self, text: str) -> None:
        self._label.setText(text)
        self._label.setToolTip(text)

    def _restore(self) -> None:
        self._on_restore()

    def _on_theme_changed(self, _theme=None) -> None:
        self._apply_style()
        self.reposition()

    def _apply_style(self) -> None:
        t = ThemeManager.instance().current()
        self.setStyleSheet(
            f"""
            QFrame#MinimizedBottomBar {{
                background-color: {t.input_bg};
                border: 1px solid {t.input_border};
                border-radius: 4px;
            }}
            QFrame#MinimizedBottomBar QLabel {{
                color: {t.menu_text};
                font-weight: 600;
            }}
            QPushButton#MinimizedBottomRestore {{ padding: 2px 12px; }}
            """
        )

    def reposition(self) -> None:
        """贴画布底边、横跨整个宽度（画布 resize 后由 ``mapCanvasRefreshed`` 校正）。"""
        try:
            self.setGeometry(
                0,
                max(self._canvas.height() - self.sizeHint().height(), 0),
                self._canvas.width(),
                self.sizeHint().height(),
            )
        except RuntimeError:  # 画布 C++ 对象已销毁
            pass


__all__ = ["MinimizedBar"]
