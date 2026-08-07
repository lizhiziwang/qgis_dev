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

# ---------------------------------------------------------------------------
# 画布左上角缩放工具（CanvasZoomTool）样式
# ---------------------------------------------------------------------------

CANVAS_ZOOM_PANEL_QSS = """
    QFrame {
        background-color: rgba(255, 255, 255, 0.92);
        border: 1px solid rgba(0, 0, 0, 0.16);
        border-radius: 8px;
    }
"""

# 面板内图标按钮（放大/缩小）：透明底 + 蓝色悬浮反馈，与滑块主题色一致；
# focus 去掉虚线框，disabled 置灰（按钮本身仍可用，保留未来禁用入口）
CANVAS_ZOOM_BUTTON_QSS = """
    QPushButton {
        border: none;
        background-color: transparent;
        border-radius: 5px;
    }
    QPushButton:hover {
        background-color: rgba(74, 144, 226, 0.12);
    }
    QPushButton:pressed {
        background-color: rgba(74, 144, 226, 0.20);
    }
    QPushButton:focus {
        outline: none;
    }
    QPushButton:disabled {
        color: #b0b0b0;
    }
"""

# 垂直滑块：宽 groove + 白色描边蓝圆点。
# 注意：本 QGIS 环境里 QSS 的 sub-page 实际渲染成"从顶到手柄"（与标准 Qt 相反，
# 像素实测），蓝色填充因此放在 add-page 上才能得到"从底部到手柄"（放大时蓝色上升）。
CANVAS_ZOOM_SLIDER_QSS = """
    QSlider::groove:vertical {
        width: 6px;
        background: rgba(0, 0, 0, 0.10);
        border-radius: 3px;
    }
    QSlider::sub-page:vertical {
        background: rgba(0, 0, 0, 0.10);
        border-radius: 3px;
    }
    QSlider::add-page:vertical {
        background: qlineargradient(
            x1: 0, y1: 0, x2: 1, y2: 0,
            stop: 0 #4A90E2, stop: 1 #6ab0f0
        );
        border-radius: 3px;
    }
    QSlider::handle:vertical {
        width: 12px;
        height: 12px;
        margin: -5px 0;
        border-radius: 6px;
        background: #4A90E2;
        border: 1px solid #ffffff;
    }
    QSlider::handle:vertical:hover {
        background: #5b9df0;
    }
    QSlider::handle:vertical:pressed {
        background: #3a80d2;
    }
"""

CANVAS_ZOOM_QSS = (
    CANVAS_ZOOM_PANEL_QSS + CANVAS_ZOOM_BUTTON_QSS + CANVAS_ZOOM_SLIDER_QSS
)


def apply_dialog_style(dialog) -> None:
    """给 dialog 应用 :data:`DIALOG_BASE_QSS`。"""
    dialog.setStyleSheet(DIALOG_BASE_QSS)


def apply_text_browser_style(text_browser) -> None:
    """给 QTextBrowser 应用 :data:`TEXT_BROWSER_QSS`。"""
    text_browser.setStyleSheet(TEXT_BROWSER_QSS)
