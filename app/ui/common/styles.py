"""UI 通用样式（QSS 集中管理）。"""

from __future__ import annotations


# ---------------------------------------------------------------------------
# QSS 字符串集中
# ---------------------------------------------------------------------------

BUTTON_TOOLBAR_QSS = """
    QToolButton {
        border: none;
        background-color: transparent;
        border-radius: 3px;
        padding: 4px;
    }
    QToolButton:hover {
        background-color: #e8e8e8;
    }
    QToolButton:pressed {
        background-color: #dcdcdc;
    }
"""

TEXT_BROWSER_QSS = """
    QTextBrowser {
        font-family: "微软雅黑", "宋体", sans-serif;
        font-size: 14px;
        color: #2c3e50;
        padding: 10px;
        border: 1px solid #dee2e6;
        selection-background-color: #3498db;
        selection-color: white;
    }
    QTextBrowser:hover {
        border-color: #95a5a6;
    }
"""

DIALOG_BASE_QSS = """
    QDialog {
        font-family: "Microsoft YaHei", "Segoe UI", sans-serif;
        font-size: 10pt;
    }
    QLineEdit {
        padding: 6px;
        border: 1px solid #ccc;
        border-radius: 4px;
        background: #fff;
    }
    QLineEdit:focus {
        border: 1px solid #4A90E2;
        outline: none;
    }
    QPushButton {
        padding: 6px 12px;
        border: 1px solid #ccc;
        border-radius: 4px;
        background-color: #f5f5f5;
        color: #333;
    }
    QPushButton:hover {
        background-color: #e5e5e5;
        border-color: #999;
    }
    QPushButton:pressed {
        background-color: #ddd;
    }
"""


def apply_dialog_style(dialog) -> None:
    """给 dialog 应用 :data:`DIALOG_BASE_QSS`。"""
    dialog.setStyleSheet(DIALOG_BASE_QSS)


def apply_text_browser_style(text_browser) -> None:
    """给 QTextBrowser 应用 :data:`TEXT_BROWSER_QSS`。"""
    text_browser.setStyleSheet(TEXT_BROWSER_QSS)
