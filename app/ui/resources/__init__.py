"""UI 资源（图标、QSS 加载）。"""

from __future__ import annotations

from PyQt5.QtWidgets import QApplication


def apply_app_style(app: QApplication) -> None:
    """应用全局 QSS。"""
    app.setStyleSheet("""
        QToolTip {
            background-color: #2c3e50;
            color: white;
            border: 1px solid #34495e;
            padding: 4px;
        }
    """)
