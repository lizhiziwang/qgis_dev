"""窗体主题（颜色集中管理）。

把窗体所有要素的颜色抽到 :class:`Theme` 里：标题栏、菜单栏、工具条、
状态栏、Dock、ToolTip、**弹窗 / 右键菜单 / 输入框 / 按钮 / 表格 / 滚动条**
等通用控件。:class:`ThemeManager` 负责切换并广播 ``theme_changed``：

- 主窗体 chrome 用 ``title_bar_qss / toolbar_qss / window_qss`` 渲染自身 QSS；
- 弹窗、右键菜单、ToolTip 等**顶层窗口**不吃主窗体 QSS，统一由
  ``app_qss`` 设到 QApplication（:func:`apply_app_theme`），切换时整体刷新。

主题列表见 :data:`THEMES`（浅色 / 深色 / 蓝色 / 绿色）。新增主题只需往
:data:`THEMES` 加一项，标题栏主题菜单会自动出现。
"""

from __future__ import annotations

from dataclasses import dataclass

from PyQt5.QtCore import QCoreApplication, QObject, QSettings, pyqtSignal
from PyQt5.QtGui import QColor, QPalette
from PyQt5.QtWidgets import QApplication, QToolTip


@dataclass(frozen=True)
class Theme:
    """一组窗体配色。所有字段为 QSS 可直接使用的颜色字符串。"""

    key: str            # 内部标识（持久化用）
    name: str           # 主题菜单显示名

    # ---- 窗体整体 ----
    window_bg: str      # 窗体底色
    border: str         # 分隔线 / 边框
    canvas_bg: str      # 地图画布底色（QgsMapCanvas.setCanvasColor）

    # ---- 标题栏 ----
    title_bg: str       # 标题栏底色
    title_text: str     # 标题文字色
    title_icon: str     # 窗口按钮图标 / 文字色
    title_hover: str    # 窗口按钮 hover 底色
    title_pressed: str  # 窗口按钮按下底色
    close_hover: str    # 关闭按钮 hover 底色（红）
    close_pressed: str  # 关闭按钮按下底色

    # ---- 菜单栏 / 弹出菜单 ----
    menu_bg: str
    menu_text: str
    menu_hover: str

    # ---- 工具条 ----
    toolbar_bg: str
    toolbar_text: str
    toolbar_hover: str
    toolbar_pressed: str

    # ---- 状态栏 ----
    status_bg: str
    status_text: str

    # ---- ToolTip ----
    tooltip_bg: str
    tooltip_text: str
    tooltip_border: str

    # ---- 对话框 / 通用控件（弹窗、右键菜单、输入框、按钮、表格等）----
    text_secondary: str  # 次要文字（副标题 / 占位 / 提示）
    text_error: str      # 错误 / 校验失败文字
    input_bg: str        # 输入框底色
    input_border: str    # 输入框 / 按钮边框
    input_focus: str     # 输入框聚焦边框（强调色）
    btn_bg: str          # 普通按钮底色
    btn_hover: str       # 按钮 / 控件 hover 底色
    btn_pressed: str     # 按钮按下底色
    panel_bg: str        # 面板 / 表格隔行底色
    panel_border: str    # 面板 / 表格内部细分隔线


THEMES: dict[str, Theme] = {
    "light": Theme(
        key="light",
        name="浅色",
        window_bg="#f0f0f0",
        border="#dcdfe4",
        canvas_bg="#ffffff",
        title_bg="#f0f0f0",
        title_text="#2c3e50",
        title_icon="#5a5f66",
        title_hover="#e2e5e9",
        title_pressed="#d3d7dc",
        close_hover="#e81123",
        close_pressed="#bf0f1f",
        menu_bg="#f0f0f0",
        menu_text="#2c3e50",
        menu_hover="#e0e3e7",
        toolbar_bg="#f0f0f0",
        toolbar_text="#2c3e50",
        toolbar_hover="#e8e8e8",
        toolbar_pressed="#dcdcdc",
        status_bg="#f0f0f0",
        status_text="#2c3e50",
        tooltip_bg="#ffffff",
        tooltip_text="#2c3e50",
        tooltip_border="#c9c9c9",
        text_secondary="#666666",
        text_error="#d33",
        input_bg="#ffffff",
        input_border="#c9c9c9",
        input_focus="#4a90e2",
        btn_bg="#f5f5f5",
        btn_hover="#e5e5e5",
        btn_pressed="#dcdcdc",
        panel_bg="#f8f9fa",
        panel_border="#e5e5e5",
    ),
    "dark": Theme(
        key="dark",
        name="深色",
        window_bg="#2b2d30",
        border="#3e4147",
        canvas_bg="#2f3136",
        title_bg="#2b2d30",
        title_text="#e8e8e8",
        title_icon="#b9bec6",
        title_hover="#3a3d42",
        title_pressed="#44484e",
        close_hover="#e81123",
        close_pressed="#bf0f1f",
        menu_bg="#2b2d30",
        menu_text="#d8dade",
        menu_hover="#3a3d42",
        toolbar_bg="#2b2d30",
        toolbar_text="#d8dade",
        toolbar_hover="#3a3d42",
        toolbar_pressed="#44484e",
        status_bg="#2b2d30",
        status_text="#b9bec6",
        tooltip_bg="#1f2124",
        tooltip_text="#e8e8e8",
        tooltip_border="#44484e",
        text_secondary="#9aa1a9",
        text_error="#ef5350",
        input_bg="#232528",
        input_border="#4a4d54",
        input_focus="#5b9df0",
        btn_bg="#3a3d42",
        btn_hover="#44484e",
        btn_pressed="#4e5259",
        panel_bg="#33363a",
        panel_border="#3a3d42",
    ),
    "blue": Theme(
        key="blue",
        name="蓝色",
        window_bg="#eef3fa",
        border="#c9d4e0",
        canvas_bg="#f4f8fe",
        title_bg="#2f5f8f",
        title_text="#ffffff",
        title_icon="#dfe8f2",
        title_hover="#29527c",
        title_pressed="#22466b",
        close_hover="#e81123",
        close_pressed="#bf0f1f",
        menu_bg="#eef3fa",
        menu_text="#1f3a5c",
        menu_hover="#d8e4f2",
        toolbar_bg="#eef3fa",
        toolbar_text="#1f3a5c",
        toolbar_hover="#d8e4f2",
        toolbar_pressed="#c4d6ea",
        status_bg="#eef3fa",
        status_text="#2c3e50",
        tooltip_bg="#ffffff",
        tooltip_text="#1f3a5c",
        tooltip_border="#b8c9da",
        text_secondary="#5a7a9a",
        text_error="#d33",
        input_bg="#ffffff",
        input_border="#b8c9da",
        input_focus="#2f5f8f",
        btn_bg="#e3edf7",
        btn_hover="#d0e0f0",
        btn_pressed="#bcd2e6",
        panel_bg="#f6fafd",
        panel_border="#dfe8f2",
    ),
    "green": Theme(
        key="green",
        name="绿色",
        window_bg="#eaf5ee",
        border="#c9dccf",
        canvas_bg="#f3f9f5",
        title_bg="#2e7d5b",
        title_text="#ffffff",
        title_icon="#e0efe7",
        title_hover="#286d4e",
        title_pressed="#215d42",
        close_hover="#e81123",
        close_pressed="#bf0f1f",
        menu_bg="#eaf5ee",
        menu_text="#1f3a2c",
        menu_hover="#d3e8db",
        toolbar_bg="#eaf5ee",
        toolbar_text="#1f3a2c",
        toolbar_hover="#d3e8db",
        toolbar_pressed="#bfdcc9",
        status_bg="#eaf5ee",
        status_text="#2c3e50",
        tooltip_bg="#ffffff",
        tooltip_text="#1f3a2c",
        tooltip_border="#bad6c6",
        text_secondary="#5a8a6a",
        text_error="#d33",
        input_bg="#ffffff",
        input_border="#bad6c6",
        input_focus="#2e7d5b",
        btn_bg="#e0efe7",
        btn_hover="#cde5d6",
        btn_pressed="#bcdcc8",
        panel_bg="#f4f9f6",
        panel_border="#dcebe2",
    ),
}


class ThemeManager(QObject):
    """主题切换器（单例）。切换时发 :attr:`theme_changed`，并把选择写入 QSettings。"""

    theme_changed = pyqtSignal(object)  # Theme

    _instance = None

    def __init__(self, parent: QObject = None) -> None:
        super().__init__(parent)
        self._current_key = "light"
        if QCoreApplication.instance() is not None:
            saved = QSettings("qgis_dev", "qgis_dev").value("ui/theme")
            if saved in THEMES:
                self._current_key = saved
        apply_app_theme(self.current())

    @classmethod
    def instance(cls) -> "ThemeManager":
        if cls._instance is None:
            cls._instance = cls()
        return cls._instance

    def current(self) -> Theme:
        return THEMES[self._current_key]

    def set_theme(self, key: str) -> None:
        if key not in THEMES or key == self._current_key:
            return
        self._current_key = key
        if QCoreApplication.instance() is not None:
            QSettings("qgis_dev", "qgis_dev").setValue("ui/theme", key)
        apply_app_theme(self.current())
        self.theme_changed.emit(self.current())


# ---------------------------------------------------------------------------
# QSS 渲染：各组件用当前主题渲染自己的样式表
# ---------------------------------------------------------------------------


def title_bar_qss(t: Theme) -> str:
    """标题栏 QSS。含标题栏空白区、窗口按钮、用户按钮及其弹出菜单。"""
    return f"""
    QWidget#TitleBar {{ background-color: {t.title_bg}; }}
    QLabel#TitleBarTitle {{
        font-size: 13px;
        font-weight: 600;
        color: {t.title_text};
        border: none;
    }}
    QToolButton {{
        border: none;
        background-color: transparent;
        color: {t.title_icon};
        border-radius: 3px;
        padding: 2px 6px;
    }}
    QToolButton:hover {{ background-color: {t.title_hover}; }}
    QToolButton:pressed {{ background-color: {t.title_pressed}; }}
    QToolButton#TitleBarClose:hover {{ background-color: {t.close_hover}; }}
    QToolButton#TitleBarClose:pressed {{ background-color: {t.close_pressed}; }}
    QToolButton::menu-indicator {{ image: none; width: 0px; }}
    QMenu {{
        background-color: {t.menu_bg};
        color: {t.menu_text};
        border: 1px solid {t.border};
    }}
    QMenu::item:selected {{ background-color: {t.menu_hover}; }}
    """


def toolbar_qss(t: Theme) -> str:
    """主工具条 QSS（工具按钮 hover/按下/选中态）。"""
    return f"""
    QToolBar {{
        background-color: {t.toolbar_bg};
        border: none;
        padding: 2px 4px;
        spacing: 6px;
    }}
    QToolBar::handle {{ background: transparent; }}
    QToolButton {{
        border: none;
        background-color: transparent;
        border-radius: 3px;
        padding: 4px 6px;
        color: {t.toolbar_text};
    }}
    QToolButton:hover {{ background-color: {t.toolbar_hover}; }}
    QToolButton:pressed {{ background-color: {t.toolbar_pressed}; }}
    QToolButton:checked {{
        background-color: {t.toolbar_hover};
        color: {t.toolbar_text};
    }}
    """


def window_qss(t: Theme) -> str:
    """主窗体 chrome QSS：菜单栏、弹出菜单、状态栏、Dock 及其内容。

    注意：范围只覆盖 QMainWindow 的直接 chrome 与 Dock 内容（树/列表/表格/
    文本）。地图画布底色不走 QSS——它由
    :meth:`MainWindow._apply_theme_qss` 用 ``setCanvasColor(theme.canvas_bg)``
    单独设置（QgsMapCanvas 的渲染层不吃 QSS 背景）。
    """
    return f"""
    QMainWindow {{ background-color: {t.window_bg}; }}
    QMenuBar {{
        background-color: {t.menu_bg};
        color: {t.menu_text};
        border-bottom: 1px solid {t.border};
    }}
    QMenuBar::item {{ background: transparent; padding: 4px 10px; }}
    QMenuBar::item:selected {{ background-color: {t.menu_hover}; }}
    QMenuBar::item:pressed {{ background-color: {t.menu_hover}; }}
    QMenu {{
        background-color: {t.menu_bg};
        color: {t.menu_text};
        border: 1px solid {t.border};
    }}
    QMenu::item {{ padding: 5px 24px 5px 20px; }}
    QMenu::item:selected {{ background-color: {t.menu_hover}; color: {t.menu_text}; }}
    QMenu::separator {{ height: 1px; background: {t.border}; margin: 4px 8px; }}
    QStatusBar {{
        background-color: {t.status_bg};
        color: {t.status_text};
        border-top: 1px solid {t.border};
    }}
    QStatusBar::item {{ border: none; }}
    QToolBar {{ background-color: {t.toolbar_bg}; border: none; }}
    QDockWidget {{ color: {t.menu_text}; font-size: 12px; }}
    QDockWidget::title {{
        background-color: {t.menu_bg};
        color: {t.menu_text};
        /* 标题栏高度由原生样式按字体决定（~22px 固定）；纵向 padding 过大会
           压缩文本内容区，中文回退字体行高略高时会把字形底部裁掉约 10%，
           所以垂直 padding 只留 3px，给文本留足余量。 */
        padding: 3px 8px;
        border-bottom: 1px solid {t.border};
    }}
    QDockWidget > QWidget {{ background-color: {t.window_bg}; }}
    QDockWidget QTreeView, QDockWidget QListView, QDockWidget QTableView,
    QDockWidget QTextBrowser {{
        background-color: {t.window_bg};
        color: {t.menu_text};
        border: none;
    }}
    QDockWidget QTreeView::item:hover, QDockWidget QListView::item:hover {{
        background-color: {t.menu_hover};
    }}
    QDockWidget QTreeView::item:selected, QDockWidget QListView::item:selected {{
        background-color: {t.toolbar_hover};
        color: {t.menu_text};
    }}
    QHeaderView::section {{
        background-color: {t.menu_bg};
        color: {t.menu_text};
        border: none;
        border-right: 1px solid {t.border};
        padding: 4px;
    }}
    """


_ARROW_CACHE: dict[tuple[str, bool], str] = {}


def _arrow_url(color: str, up: bool = False) -> str:
    """按主题文字色生成一个三角 PNG（下三角/上三角），返回 QSS 可用的 ``url(...)``。

    不能用 CSS 的 border 三角技巧：Qt QSS 不做斜角边框衔接，``border-top``
    会渲染成实心方块（下拉按钮里出现一团黑块）。这里用 QPainter 现画一个
    抗锯齿三角，颜色跟主题文字色一致，深色主题下也清晰可见。

    ``up=True`` 时画上三角（组合框弹出时箭头翻转，见 ``QComboBox::down-arrow:on``）。
    注意 Qt QSS 里 ``image`` 无法旋转，翻转只能靠换一张 PNG；按 (颜色, 方向) 缓存。
    """
    key = (color, up)
    if key in _ARROW_CACHE:
        return _ARROW_CACHE[key]

    import tempfile
    from pathlib import Path

    from PyQt5.QtCore import QPoint, Qt
    from PyQt5.QtGui import QColor, QPainter, QPixmap

    pm = QPixmap(10, 8)
    pm.fill(Qt.transparent)
    painter = QPainter(pm)
    painter.setRenderHint(QPainter.Antialiasing)
    arrow_color = QColor(color)
    painter.setPen(arrow_color)
    painter.setBrush(arrow_color)
    if up:
        painter.drawPolygon(QPoint(1, 7), QPoint(9, 7), QPoint(5, 1))
    else:
        painter.drawPolygon(QPoint(1, 1), QPoint(9, 1), QPoint(5, 7))
    painter.end()

    stem = "up" if up else "down"
    path = Path(tempfile.gettempdir()) / f"qgis_dev_{stem}_arrow_{color.lstrip('#').upper()}.png"
    if not pm.save(str(path), "PNG"):
        raise RuntimeError(f"无法写入下拉箭头 PNG: {path}")
    url = path.as_posix()
    _ARROW_CACHE[key] = url
    return url


def app_qss(t: Theme) -> str:
    """全局控件 QSS（QApplication 级）。

    弹窗 / 右键菜单 / 消息框 / ToolTip 都是**顶层窗口**，不继承主窗体的
    ``window_qss``，只有 QApplication 级样式能覆盖到它们。这里把对话框、
    输入控件、按钮、表格、滚动条等通用控件的颜色全部用主题渲染，保证
    "所有要素颜色统一走主题"。主窗体 chrome 仍由 ``window_qss``（窗体自身
    QSS，优先级更高）负责。
    """
    return f"""
    QToolTip {{
        background-color: {t.tooltip_bg};
        color: {t.tooltip_text};
        border: 1px solid {t.tooltip_border};
        padding: 4px;
    }}
    QMenu {{
        background-color: {t.menu_bg};
        color: {t.menu_text};
        border: 1px solid {t.border};
    }}
    QMenu::item {{ padding: 5px 24px 5px 20px; }}
    QMenu::item:selected {{ background-color: {t.menu_hover}; color: {t.menu_text}; }}
    QMenu::item:disabled {{ color: {t.text_secondary}; }}
    QMenu::separator {{ height: 1px; background: {t.border}; margin: 4px 8px; }}

    QDialog {{
        background-color: {t.window_bg};
        color: {t.menu_text};
        font-family: "Microsoft YaHei", "Segoe UI", sans-serif;
        font-size: 12px;
    }}
    QLabel {{ color: {t.menu_text}; }}
    QLabel#loginSubtitle {{ color: {t.text_secondary}; }}
    QLabel#loginError {{ color: {t.text_error}; font-weight: bold; }}
    QLabel#DialogTitle {{ font-size: 14px; font-weight: bold; color: {t.menu_text}; }}

    QLineEdit, QTextEdit, QPlainTextEdit, QComboBox, QSpinBox,
    QDateTimeEdit, QDateEdit, QDoubleSpinBox {{
        background-color: {t.input_bg};
        color: {t.menu_text};
        border: 1px solid {t.input_border};
        border-radius: 3px;
        padding: 4px 6px;
        selection-background-color: {t.input_focus};
        selection-color: #ffffff;
    }}
    QLineEdit:focus, QTextEdit:focus, QPlainTextEdit:focus, QComboBox:focus,
    QSpinBox:focus, QDateTimeEdit:focus, QDateEdit:focus, QDoubleSpinBox:focus {{
        border-color: {t.input_focus};
    }}
    QLineEdit:disabled, QComboBox:disabled {{
        background-color: {t.panel_bg};
        color: {t.text_secondary};
    }}
    QComboBox::drop-down {{ border: none; width: 22px; background-color: transparent; }}
    QComboBox::down-arrow {{
        /* 用生成的下三角 PNG（见 _arrow_url）；border 三角会渲染成实心块。
           宽度/高度/边距放基础规则里；弹出（:on 状态）时只用 :on 规则换图，
           复用这里的尺寸。 */
        image: url({_arrow_url(t.menu_text)});
        width: 10px;
        height: 8px;
        margin-right: 4px;
    }}
    QComboBox::down-arrow:on {{
        /* 下拉弹出时箭头翻成上三角。QSS 的 image 不能旋转，翻转只能换 PNG；
           实测 `QComboBox::down-arrow:on` 子控件状态选择器在本构建生效，
           而文档里的 `QComboBox:on::down-arrow` 会破坏整个 combo 渲染。 */
        image: url({_arrow_url(t.menu_text, up=True)});
    }}
    QComboBox QAbstractItemView {{
        background-color: {t.menu_bg};
        color: {t.menu_text};
        border: 1px solid {t.border};
        selection-background-color: {t.menu_hover};
        selection-color: {t.menu_text};
    }}

    QPushButton {{
        background-color: {t.btn_bg};
        color: {t.menu_text};
        border: 1px solid {t.input_border};
        border-radius: 3px;
        padding: 5px 12px;
    }}
    QPushButton:hover {{ background-color: {t.btn_hover}; border-color: {t.border}; }}
    QPushButton:pressed {{ background-color: {t.btn_pressed}; }}
    QPushButton:default {{ border-color: {t.input_focus}; }}
    QPushButton:disabled {{ color: {t.text_secondary}; background-color: {t.panel_bg}; }}
    QPushButton#FlatButton, QPushButton[flat="true"] {{
        border: none;
        background-color: transparent;
        border-radius: 3px;
    }}
    QPushButton#FlatButton:hover, QPushButton[flat="true"]:hover {{
        background-color: {t.btn_hover};
    }}
    QPushButton#FlatButton:pressed, QPushButton[flat="true"]:pressed {{
        background-color: {t.btn_pressed};
    }}

    QToolButton {{
        border: none;
        background-color: transparent;
        border-radius: 3px;
        color: {t.menu_text};
        padding: 3px 6px;
    }}
    QToolButton:hover {{ background-color: {t.btn_hover}; }}
    QToolButton:pressed {{ background-color: {t.btn_pressed}; }}
    QToolButton:checked {{ background-color: {t.btn_hover}; }}
    QToolButton:disabled {{ color: {t.text_secondary}; }}

    QCheckBox, QRadioButton {{ color: {t.menu_text}; }}
    QCheckBox:disabled, QRadioButton:disabled {{ color: {t.text_secondary}; }}

    QGroupBox {{
        color: {t.menu_text};
        border: 1px solid {t.border};
        border-radius: 4px;
        margin-top: 8px;
        padding-top: 6px;
    }}
    QGroupBox::title {{
        subcontrol-origin: margin;
        left: 10px;
        padding: 0 4px;
        color: {t.menu_text};
    }}

    QTableWidget, QTableView, QTreeWidget, QTreeView, QListView, QListWidget,
    QColumnView {{
        background-color: {t.window_bg};
        alternate-background-color: {t.panel_bg};
        color: {t.menu_text};
        border: 1px solid {t.input_border};
        selection-background-color: {t.toolbar_hover};
        selection-color: {t.menu_text};
    }}
    QTableWidget::item:selected, QTableView::item:selected,
    QTreeWidget::item:selected, QTreeView::item:selected,
    QListView::item:selected, QListWidget::item:selected {{
        background-color: {t.toolbar_hover};
        color: {t.menu_text};
    }}
    QHeaderView::section {{
        background-color: {t.menu_bg};
        color: {t.menu_text};
        border: none;
        border-right: 1px solid {t.border};
        border-bottom: 1px solid {t.border};
        padding: 5px 8px;
    }}
    QTableCornerButton::section {{
        background-color: {t.menu_bg};
        border: none;
        border-right: 1px solid {t.border};
        border-bottom: 1px solid {t.border};
    }}

    QTextBrowser {{
        background-color: {t.window_bg};
        color: {t.menu_text};
        border: 1px solid {t.input_border};
        padding: 6px;
    }}

    QScrollBar:vertical {{
        background: transparent;
        width: 12px;
        margin: 0;
    }}
    QScrollBar::handle:vertical {{
        background: {t.border};
        border-radius: 3px;
        min-height: 24px;
        margin: 2px;
    }}
    QScrollBar::handle:vertical:hover {{ background: {t.text_secondary}; }}
    QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical {{ height: 0; }}
    QScrollBar:horizontal {{
        background: transparent;
        height: 12px;
        margin: 0;
    }}
    QScrollBar::handle:horizontal {{
        background: {t.border};
        border-radius: 3px;
        min-width: 24px;
        margin: 2px;
    }}
    QScrollBar::handle:horizontal:hover {{ background: {t.text_secondary}; }}
    QScrollBar::add-line:horizontal, QScrollBar::sub-line:horizontal {{ width: 0; }}

    QSplitter::handle {{ background: transparent; }}
    QSplitter::handle:hover {{ background: {t.toolbar_hover}; }}
    QSplitter::handle:horizontal {{ width: 4px; }}
    QSplitter::handle:vertical {{ height: 4px; }}

    QProgressBar {{
        background-color: {t.panel_bg};
        color: {t.menu_text};
        border: 1px solid {t.input_border};
        border-radius: 4px;
        text-align: center;
        min-height: 14px;
    }}
    QProgressBar::chunk {{
        background-color: {t.input_focus};
        border-radius: 3px;
    }}

    QTabWidget::pane {{
        border: 1px solid {t.border};
        background-color: {t.window_bg};
        top: -1px;
    }}
    QTabBar::tab {{
        background-color: {t.menu_bg};
        color: {t.menu_text};
        padding: 6px 14px;
        border: 1px solid {t.border};
        border-bottom: none;
        border-top-left-radius: 3px;
        border-top-right-radius: 3px;
        margin-right: 2px;
    }}
    QTabBar::tab:selected {{ background-color: {t.window_bg}; color: {t.menu_text}; }}
    QTabBar::tab:hover:!selected {{ background-color: {t.menu_hover}; }}
    """


def _with_alpha(color: str, alpha: int) -> str:
    """hex 颜色 → ``rgba(r, g, b, a)`` 字符串（alpha 0-255）。"""
    c = QColor(color)
    c.setAlpha(alpha)
    return f"rgba({c.red()}, {c.green()}, {c.blue()}, {alpha / 255:.2f})"


def canvas_zoom_qss(t: Theme) -> str:
    """画布左上角缩放工具 QSS（半透明面板 + 主题强调色）。"""
    panel = _with_alpha(t.window_bg, 235)
    groove = _with_alpha(t.menu_text, 26)  # 轨道：前景色低透明度，深浅主题都可见
    return f"""
    QFrame {{
        background-color: {panel};
        border: 1px solid {_with_alpha(t.border, 200)};
        border-radius: 8px;
    }}
    QPushButton {{
        border: none;
        background-color: transparent;
        border-radius: 5px;
    }}
    QPushButton:hover {{ background-color: {t.btn_hover}; }}
    QPushButton:pressed {{ background-color: {t.btn_pressed}; }}
    QPushButton:focus {{ outline: none; }}
    QPushButton:disabled {{ color: {t.text_secondary}; }}
    QSlider::groove:vertical {{
        width: 6px;
        background: {groove};
        border-radius: 3px;
    }}
    QSlider::sub-page:vertical {{ background: {groove}; border-radius: 3px; }}
    QSlider::add-page:vertical {{
        background: qlineargradient(
            x1: 0, y1: 0, x2: 1, y2: 0,
            stop: 0 {t.input_focus}, stop: 1 {t.btn_hover}
        );
        border-radius: 3px;
    }}
    QSlider::handle:vertical {{
        width: 12px;
        height: 12px;
        margin: -5px 0;
        border-radius: 6px;
        background: {t.input_focus};
        border: 1px solid {t.window_bg};
    }}
    QSlider::handle:vertical:hover {{ background: {t.btn_hover}; }}
    QSlider::handle:vertical:pressed {{ background: {t.btn_pressed}; }}
    """


def apply_app_theme(theme: Theme) -> None:
    """把全局控件配色设到 QApplication（弹窗 / 右键菜单 / ToolTip 等顶层窗口）。

    ``QApplication.instance()`` 可能返回的是 QCoreApplication（无
    ``setStyleSheet``），所以要额外判 isinstance。
    """
    app = QApplication.instance()
    if app is None or not isinstance(app, QApplication):
        return
    app.setStyleSheet(app_qss(theme))
    # 原生 windowsvista 样式不吃 QToolTip 的 QSS background-color（QTBUG-25692），
    # 工具条图标化后 Tooltip 是高频交互，必须额外用 QToolTip palette 兜底主题色。
    # QToolTip.setPalette 是设 "QTipLabel" 类的应用级 palette，随主题切换刷新。
    pal = QToolTip.palette()
    pal.setColor(QPalette.ToolTipBase, QColor(theme.tooltip_bg))
    pal.setColor(QPalette.ToolTipText, QColor(theme.tooltip_text))
    QToolTip.setPalette(pal)


# 兼容旧名：早期版本只设 ToolTip 配色，现在已是全量全局 QSS
apply_app_tooltip_style = apply_app_theme


__all__ = [
    "Theme",
    "THEMES",
    "ThemeManager",
    "title_bar_qss",
    "toolbar_qss",
    "window_qss",
    "app_qss",
    "canvas_zoom_qss",
    "apply_app_theme",
    "apply_app_tooltip_style",
]
