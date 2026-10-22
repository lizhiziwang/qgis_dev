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

import tempfile
from dataclasses import dataclass
from pathlib import Path

from qgis.PyQt.QtCore import QCoreApplication, QObject, QSettings, pyqtSignal
from qgis.PyQt.QtGui import QColor, QPalette
from qgis.PyQt.QtWidgets import QApplication, QToolTip


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


#: 启动时是否恢复上次选择的主题（``QSettings`` 的 ``ui/theme``）。
#:
#: 产品规定「系统启动主题 = 浅色」，所以默认 **False**：启动固定浅色，
#: 在标题栏切主题只在本次运行内生效。原因：偏好会被任何 ``set_theme`` 写进注册表，
#: 离屏验证脚本 ``for t in THEMES: set_theme(t)``、测试、手点主题按钮都会落盘，
#: 一恢复就会出现「上次跑脚本把主题改绿了 → 下次启动是绿色」（见 memory 04）。
#: 要改回「记住上次选择」把这个常量置 True 即可。
_RESTORE_SAVED_THEME = False


class ThemeManager(QObject):
    """主题切换器（单例）。切换时发 :attr:`theme_changed`，并把选择写入 QSettings。

    启动主题固定浅色（见 :data:`_RESTORE_SAVED_THEME`），**不**恢复上次选择。
    """

    theme_changed = pyqtSignal(object)  # Theme

    _instance = None

    def __init__(self, parent: QObject = None) -> None:
        super().__init__(parent)
        self._current_key = "light"
        if _RESTORE_SAVED_THEME and QCoreApplication.instance() is not None:
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

#: 停靠面板里「选中行」底色的不透明度（0-255）。QSS 与图层树自绘的「标注」胶囊
#: **共用这一个值** —— 两边各写一个迟早漂移（之前就是因为改了 QSS 没改自绘，
#: 选中行上露出一块颜色不同的补丁）。
DOCK_SELECTION_ALPHA = 55

#: 弹窗列表（导入图层的待导入 / 数据源图层列表）的选中底色不透明度。
#: 比停靠面板重得多：弹窗列表没有悬停色作对照，全局规则给的 ``toolbar_hover``
#: 又几乎是背景色，用户会以为"根本没选上"（见 :func:`dock_selection_color`）。
DIALOG_LIST_SELECTION_ALPHA = 110


def dock_selection_color(t: "Theme", alpha: int = DOCK_SELECTION_ALPHA) -> str:
    """选中行的**实色**（把强调色按 ``alpha`` 叠到窗体底色上）。

    QSS 里**不用** ``rgba(...)``：Qt 对 ``selection-background-color`` 这类调色板
    角色会丢 alpha / 按自己的规则合成，于是"分支区一种深浅、item 区另一种"
    （实测分支区 #98bee9、item 区 #cbdbed）。这里给一个算好的实色，两边必然同色。
    图层树自绘的「标注」胶囊也用同一套算法（见 ``layer_dock._blend_over``）。

    ``alpha`` 默认取 :data:`DOCK_SELECTION_ALPHA`（停靠面板的浅选中态）；弹窗里的
    列表（如「导入图层」的待导入列表）用更重的 :data:`DIALOG_LIST_SELECTION_ALPHA`
    —— 弹窗背景更亮、列表没有悬停对照，太浅的选中色会被误认成"没选上"（用户报过）。
    """
    fg = QColor(t.input_focus)
    bg = QColor(t.window_bg)
    a = max(0, min(255, alpha)) / 255.0
    return QColor(
        round(fg.red() * a + bg.red() * (1.0 - a)),
        round(fg.green() * a + bg.green() * (1.0 - a)),
        round(fg.blue() * a + bg.blue() * (1.0 - a)),
    ).name()


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


def _with_alpha(hex_color: str, alpha: int) -> str:
    """#rrggbb → rgba(r, g, b, alpha)，用于生成半透明强调色。"""
    c = QColor(hex_color)
    return f"rgba({c.red()}, {c.green()}, {c.blue()}, {alpha})"


#: QToolBar 相邻项间距（px）。QSS ``spacing`` 只在工具条**可见后**重刷样式时才生效，
#: 首次布局早于 polish 的工具条（如通用工具条）会退化成风格默认的 1px；
#: 因此 :class:`RibbonToolBar` 与 :meth:`MainWindow._apply_theme_qss` 都会再
#: 程序化 ``layout().setSpacing(TOOLBAR_ITEM_GAP)`` 兜底。
TOOLBAR_ITEM_GAP = 6

#: 工具条按钮圆角（px）。比原型 ``border-radius:4px`` 更圆，视觉更柔和。
TOOLBAR_BUTTON_RADIUS = 8

#: Ribbon 工具条（通用 / 编辑）按钮与组名字号（px）。比普通工具条（10px）大一号，
#: 因为图标在下且带组名的方块按钮信息量更大，字太小不易读；
#: :class:`RibbonToolBar` 画组名时也用它，两处保持一致。
RIBBON_FONT_PX = 11


def toolbar_qss(t: Theme) -> str:
    """主工具条 QSS（对标设计原型的 Ribbon 方形图标按钮）。

    按钮为方形圆角块（图标在上 + 小字在下），浅底细边框，hover 用主题
    强调色淡底 + 强调色边框，选中态用稍深的强调色底。所有颜色走主题 token
    （``input_bg`` / ``input_border`` / ``input_focus``），四个主题自动适配。

    普通工具条按钮字号 10px；Ribbon 工具条（``[ribbon="true"]``，即通用 / 编辑）
    按 :data:`RIBBON_FONT_PX` 放大一号。
    """
    hover_bg = _with_alpha(t.input_focus, 38)
    checked_bg = _with_alpha(t.input_focus, 74)
    return f"""
    QToolBar {{
        background-color: {t.toolbar_bg};
        border: none;
        border-bottom: 1px solid {t.border};
        padding: 5px 6px;
        spacing: {TOOLBAR_ITEM_GAP}px;
    }}
    /* Ribbon 工具条：组名留白带由 RibbonToolBar.sizeHint() 加高，见 ribbon.py */
    QToolBar::handle {{ background: transparent; }}
    QToolBar::separator {{
        background-color: {t.border};
        width: 1px;
        margin: 6px 4px;
    }}
    QToolButton {{
        background-color: {t.input_bg};
        border: 1px solid {t.input_border};
        border-radius: {TOOLBAR_BUTTON_RADIUS}px;
        padding: 3px 4px;
        color: {t.toolbar_text};
        font-size: 10px;
    }}
    /* 原型组按钮：方形块，图标在上 + 小字在下；
       宽度由 RibbonToolBar._apply_button_widths() 统一定成 BUTTON_WIDTH，
       字号比普通工具条大一号（通用 / 编辑工具条）。 */
    QToolBar[ribbon="true"] QToolButton {{
        min-height: 48px;
        padding: 3px 2px;
        font-size: {RIBBON_FONT_PX}px;
    }}
    QToolButton:hover {{
        background-color: {hover_bg};
        border-color: {t.input_focus};
    }}
    QToolButton:pressed {{
        background-color: {_with_alpha(t.input_focus, 96)};
    }}
    QToolButton:checked {{
        background-color: {checked_bg};
        border-color: {t.input_focus};
        color: {t.toolbar_text};
    }}
    QToolButton:disabled {{
        color: {t.text_secondary};
        background-color: {t.panel_bg};
        border-color: {t.panel_border};
    }}
    QToolButton::menu-indicator {{ image: none; width: 0px; }}
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
    QTabBar#ChromeTabBar {{
        background-color: {t.menu_bg};
        border-bottom: 1px solid {t.border};
    }}
    QTabBar#ChromeTabBar::tab {{
        background-color: transparent;
        color: {t.menu_text};
        border: none;
        padding: 6px 18px;
        font-size: 12px;
    }}
    QTabBar#ChromeTabBar::tab:hover:!selected {{ background-color: {t.menu_hover}; }}
    QTabBar#ChromeTabBar::tab:selected {{
        color: {t.menu_text};
        font-weight: 600;
        border-bottom: 2px solid {t.input_focus};
    }}
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
    /* 图层面板底部的「图层信息」块：浅一档的底 + 上边框，与树区分开 */
    QWidget#LayerDockInfo {{
        background-color: {t.input_bg};
        border-top: 1px solid {t.border};
    }}
    QLabel#LayerDockInfoTitle {{ color: {t.menu_text}; font-weight: 600; }}
    QLabel#LayerDockInfoLine {{ color: {t.text_secondary}; }}
    QDockWidget QTreeView, QDockWidget QListView, QDockWidget QTableView,
    QDockWidget QTextBrowser {{
        background-color: {t.window_bg};
        color: {t.menu_text};
        border: none;
    }}
    QDockWidget QTreeView::item:hover, QDockWidget QListView::item:hover {{
        background-color: {t.menu_hover};
    }}
    /* 选中行（图层树 / 属性表 / dock 里的各种列表）：
       - **不要焦点框**：Qt 会给"当前项"额外描一圈方框，看着就是"选中行有边框线"；
       - 底色用强调色而非灰：原先 selections 与 hover 同为灰系（toolbar_hover vs
         menu_hover 几乎一个色），根本看不出哪行是选中的（用户报"背景显示不明显"）；
       - ``show-decoration-selected: 1``：让选中底色**铺满整行**（包括左侧缩进 / 分支
         区），否则左边会空出一条竖杠（用户报"选中行左侧多一个竖条"）。 */
    QDockWidget QTreeView, QDockWidget QListView {{
        show-decoration-selected: 1;
        /* **视图级选中色也跟 ::item:selected 统一**：分支 / 缩进区不吃 ::item 规则，
           用的是这里的 selection-background-color —— 不统一的话，选中行左边会空出
           一条另一种颜色的竖条（app_qss 给视图默认设的是 toolbar_hover 灰）。
           这里刻意用**实色**（见 dock_selection_color）而不是 rgba：Qt 对
           selection-background-color 这类调色板角色会丢 alpha 自己合成，实测分支区
           渲染成 #98bee9、item 区 #cbdbed，左边照样一条深色竖条。 */
        selection-background-color: {dock_selection_color(t)};
        selection-color: {t.menu_text};
    }}
    /* 分支区：**故意不写 ``QDockWidget QTreeView::branch {{ ... }}`` 基规则**，
       只给"有子节点"两态（展开 / 折叠）贴三角箭头图（见 _branch_url）。
       实测（windows11 原生样式，真实平台，选中一个二级行）：
         - 只要存在基规则（无论 ``image: none`` 还是给张透明图），选中行左侧的每个
           缩进列都会冒出一条青色竖线（原生样式借 ``show-decoration-selected`` 画的
           父→子连接线，用户报的"两个竖条"；属性表是 QTableView、无分支列，故不出现）；
         - 删掉基规则、只留下面两条带 ``image`` 的箭头规则 → 竖条消失，三角箭头与
           选中底色都还在。
       所以这里**不要**补基规则。*/
    QDockWidget QTreeView::branch:has-children:!has-siblings:closed,
    QDockWidget QTreeView::branch:closed:has-children:has-siblings {{
        image: url({_branch_url(t.menu_text, False)});
    }}
    QDockWidget QTreeView::branch:open:has-children:!has-siblings,
    QDockWidget QTreeView::branch:open:has-children:has-siblings {{
        image: url({_branch_url(t.menu_text, True)});
    }}
    QDockWidget QTreeView::item:selected, QDockWidget QListView::item:selected,
    QDockWidget QTableView::item:selected, QDockWidget QTreeWidget::item:selected,
    QDockWidget QListWidget::item:selected, QDockWidget QTableWidget::item:selected {{
        /* 与上面的 selection-background-color 用同一个实色，保证缩进区 / 条目区同色 */
        background-color: {dock_selection_color(t)};
        color: {t.menu_text};
        outline: none;
    }}
    QHeaderView::section {{
        background-color: {t.menu_bg};
        color: {t.menu_text};
        border: none;
        border-right: 1px solid {t.border};
        padding: 4px;
    }}
    /* 表头排序三角：**必须自绘** —— 给表头设了 QSS 之后 Qt 就不画它自带的排序指示器了
       （与 ::branch 那类坑同源）。箭头复用 _arrow_url 生成的 PNG（升 = ▲，降 = ▼）。 */
    QDockWidget QHeaderView::up-arrow, QDockWidget QHeaderView::down-arrow {{
        width: 9px;
        height: 9px;
        subcontrol-position: center right;
        right: 3px;
    }}
    QDockWidget QHeaderView::up-arrow {{ image: url({_arrow_url(t.menu_text, True)}); }}
    QDockWidget QHeaderView::down-arrow {{ image: url({_arrow_url(t.menu_text, False)}); }}

    /* 图层树 Dock 头部工具条：背景用 toolbar_bg 与工具条一致、底边 1px 分隔线
       与树视图分隔；按钮 hover/pressed 用 toolbar 系列色，避免 app_qss 默认的
       btn 系列（按钮底色更深）显得过重。样式集中在这里管理，切主题自动跟随。 */
    QWidget#LayerDockHeader {{
        background-color: {t.toolbar_bg};
        border-bottom: 1px solid {t.border};
    }}
    QWidget#LayerDockHeader QToolButton {{
        border: none;
        background-color: transparent;
        border-radius: 2px;
        padding: 0px;
        color: {t.menu_text};
    }}
    QWidget#LayerDockHeader QToolButton:hover {{
        background-color: {t.toolbar_hover};
    }}
    QWidget#LayerDockHeader QToolButton:pressed {{
        background-color: {t.toolbar_pressed};
    }}
    /* 图层展示方式（按图层顺序 / 按数据源）：勾选态要一眼看出当前用的是哪个 */
    QWidget#LayerDockHeader QToolButton:checked {{
        background-color: {t.toolbar_pressed};
        border: 1px solid {t.border};
    }}

    /* 数据管理 Dock 左栏「连接管理树」：统一字号 / 行距 / 悬停选中态。
       节点级的字重、文字色与图标选不中单个 item，由
       DataManageDock._apply_tree_theme 用 setFont/setForeground/setIcon 刷，
       这里只管控件层外观。 */
    QWidget#DataManageTreePane {{ background-color: {t.window_bg}; }}
    QLabel#DataManageTreeHeader {{
        color: {t.text_secondary};
        font-size: 11px;
        font-weight: 600;
        padding: 3px 2px 1px 2px;
    }}
    QTreeWidget#DataManageTree {{
        background-color: {t.window_bg};
        border: none;
        /* 去掉聚焦时的虚线外框（选中态已有底色，再套虚线框显脏） */
        outline: 0;
        padding: 2px 0;
    }}
    QTreeWidget#DataManageTree::item {{
        padding: 3px 2px;
        border-radius: 3px;
    }}
    QTreeWidget#DataManageTree::item:hover {{
        background-color: {t.menu_hover};
    }}
    /* 选中态文字强制回主色：类别分组平时是次要色，选中后要和其他层级一样清楚 */
    QTreeWidget#DataManageTree::item:selected {{
        background-color: {t.toolbar_hover};
        color: {t.menu_text};
    }}
    QPushButton#DataManageAddConn {{
        background-color: transparent;
        color: {t.input_focus};
        border: 1px solid {t.input_border};
        border-radius: 3px;
        padding: 4px 8px;
        font-size: 12px;
    }}
    QPushButton#DataManageAddConn:hover {{
        background-color: {_with_alpha(t.input_focus, 24)};
        border-color: {t.input_focus};
    }}
    QPushButton#DataManageAddConn:pressed {{
        background-color: {_with_alpha(t.input_focus, 44)};
    }}
    """


_ARROW_CACHE: dict[tuple[str, bool], str] = {}


#: 树的展开 / 折叠三角 PNG 缓存（键 = (颜色, 是否展开)）
_BRANCH_CACHE: dict[tuple[str, bool], str] = {}


def _branch_url(color: str, opened: bool) -> str:
    """树的展开 / 折叠三角 PNG：展开 = 下三角 ▼，折叠 = 右三角 ▶。

    原生样式除了三角还会画一条「父节点 → 子节点」的**竖向连接线**，在选中行上看着
    就是"左侧多了一条竖条"（用户报的）。所以这里把箭头换成自绘 PNG，三角才好认。

    但**光换箭头不够**：连接线是原生样式在"存在 ``QTreeView::branch`` 基规则"时画的，
    必须**连基规则一起不写**才压得掉 —— 见 ``window_qss`` 里分支区那段注释与
    ``_branch_url`` 的调用处。
    """
    key = (color, opened)
    if key in _BRANCH_CACHE:
        return _BRANCH_CACHE[key]

    from qgis.PyQt.QtCore import QPoint, Qt
    from qgis.PyQt.QtGui import QColor, QPainter, QPolygon, QPixmap

    size = 9
    pm = QPixmap(size, size)
    pm.fill(Qt.GlobalColor.transparent)
    painter = QPainter(pm)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing)
    c = QColor(color)
    painter.setPen(c)
    painter.setBrush(c)
    if opened:  # ▼
        painter.drawPolygon(QPoint(1, 3), QPoint(7, 3), QPoint(4, 6))
    else:       # ▶
        painter.drawPolygon(QPoint(3, 1), QPoint(3, 7), QPoint(6, 4))
    painter.end()

    stem = "open" if opened else "closed"
    path = Path(tempfile.gettempdir()) / f"qgis_dev_branch_{stem}_{color.lstrip('#').upper()}.png"
    if not pm.save(str(path), "PNG"):
        raise RuntimeError(f"无法写入树分支三角 PNG: {path}")
    url = path.as_posix()
    _BRANCH_CACHE[key] = url
    return url


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


    from qgis.PyQt.QtCore import QPoint, Qt
    from qgis.PyQt.QtGui import QColor, QPainter, QPixmap

    pm = QPixmap(10, 8)
    pm.fill(Qt.GlobalColor.transparent)
    painter = QPainter(pm)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing)
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


_CHECK_CACHE: dict[str, str] = {}


def _check_url(color: str) -> str:
    """按颜色生成一个对勾 PNG（勾选框选中态），返回 QSS 可用的 ``url(...)``。

    勾选框的选中态**不能只靠背景色区分**：一旦用 QSS 覆盖 ``::indicator``，
    原生样式画的勾就被替换掉了（真实 Windows 上会退化成"实心方块、看不出勾"）。
    这里显式画一个对勾 ``image``，保证任何平台/样式下选中态都有清晰的 ✓。
    按颜色缓存。
    """
    if color in _CHECK_CACHE:
        return _CHECK_CACHE[color]

    from qgis.PyQt.QtCore import QPoint, Qt
    from qgis.PyQt.QtGui import QColor, QPainter, QPen, QPixmap, QPolygon

    pm = QPixmap(14, 14)
    pm.fill(Qt.GlobalColor.transparent)
    painter = QPainter(pm)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing)
    pen = QPen(QColor(color))
    pen.setWidth(2)
    painter.setPen(pen)
    painter.drawPolyline(QPolygon([QPoint(3, 8), QPoint(6, 11), QPoint(11, 4)]))
    painter.end()

    path = Path(tempfile.gettempdir()) / f"qgis_dev_check_{color.lstrip('#').upper()}.png"
    if not pm.save(str(path), "PNG"):
        raise RuntimeError(f"无法写入对勾 PNG: {path}")
    url = path.as_posix()
    _CHECK_CACHE[color] = url
    return url


#: 「半选」横杠 PNG 缓存（与 :data:`_CHECK_CACHE` 同理）
_DASH_CACHE: dict[str, str] = {}


def _dash_url(color: str) -> str:
    """按颜色生成一个「半选」横杠 PNG（三态勾选框的 PartiallyChecked），返回 url(...)。

    三态勾选框（如「全选」）的半选态必须有自己的图形：只靠背景色的话，跟"没勾"
    几乎看不出区别。与 :func:`_check_url` 同一套做法。
    """
    if color in _DASH_CACHE:
        return _DASH_CACHE[color]

    from qgis.PyQt.QtCore import QPoint, Qt
    from qgis.PyQt.QtGui import QColor, QPainter, QPen, QPixmap

    pm = QPixmap(14, 14)
    pm.fill(Qt.GlobalColor.transparent)
    painter = QPainter(pm)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing)
    pen = QPen(QColor(color))
    pen.setWidth(2)
    painter.setPen(pen)
    painter.drawLine(QPoint(4, 7), QPoint(10, 7))
    painter.end()

    path = Path(tempfile.gettempdir()) / f"qgis_dev_dash_{color.lstrip('#').upper()}.png"
    if not pm.save(str(path), "PNG"):
        raise RuntimeError(f"无法写入半选横杠 PNG: {path}")
    url = path.as_posix()
    _DASH_CACHE[color] = url
    return url


def app_qss(t: Theme) -> str:
    """全局控件 QSS（QApplication 级）。

    弹窗 / 右键菜单 / 消息框 / ToolTip 都是**顶层窗口**，不继承主窗体的
    ``window_qss``，只有 QApplication 级样式能覆盖到它们。这里把对话框、
    输入控件、按钮、表格、滚动条等通用控件的颜色全部用主题渲染，保证
    "所有要素颜色统一走主题"。主窗体 chrome 仍由 ``window_qss``（窗体自身
    QSS，优先级更高）负责。
    """
    primary = QColor(t.input_focus)
    primary_hover = primary.darker(112).name()
    primary_pressed = primary.darker(126).name()
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
    QDockWidget > QWidget {{ background-color: {t.window_bg}; }}
    /* 滚动区：**viewport 必须显式透明**。QScrollArea 的 viewport 是个 QWidget，
       默认按调色板的 Base/Window 色（浅色）自绘 —— 深色主题下就在弹窗里留下一大块
       浅灰，而里面的字段标签用主题文字色（浅色）画上去，直接看不见了（用户报
       "属性录入 / 编辑面要素属性弹窗背景没适配"）。透明后透出 QDialog 的主题底色。 */
    QScrollArea {{ background: transparent; border: none; }}
    QScrollArea > QWidget#qt_scrollarea_viewport {{ background-color: transparent; }}
    QScrollArea > QWidget > QWidget {{ background-color: transparent; }}
    QLabel {{ color: {t.menu_text}; }}
    QLabel#loginSubtitle {{ color: {t.text_secondary}; }}
    QLabel#loginError {{ color: {t.text_error}; font-weight: bold; }}
    QLabel#DialogTitle {{ font-size: 14px; font-weight: bold; color: {t.menu_text}; }}
    /* 属性类面板的数值（图层对象个数 / 范围坐标 / 透明度百分比等）：
       之前用 input_focus 蓝跟主操作色撞色，改成主文本色 + 加粗，
       信息层级靠"加粗"而非"换色"区分。*/
    QLabel#PropValue {{ color: {t.menu_text}; font-weight: bold; }}
    QLabel#PropHint {{ color: {t.text_secondary}; }}
    QLabel#SectionLabel {{ color: {t.menu_text}; font-weight: 600; }}
    /* 通用弹窗控件（质检「狭长面处理」等）：
       - #DialogPrimary 主色实心按钮，与 LoginPrimary/StartupPrimary 同源
         （t.input_focus + hover/按下自动加深），刻意不绑业务名；
       - #SectionLabel 分栏小标题（如「参考提示」）；
       - #CheckDesc 说明 / 参考提示框（左侧红色强调条）。 */
    QPushButton#DialogPrimary {{
        background-color: {t.input_focus};
        color: #ffffff;
        border: none;
        border-radius: 4px;
        padding: 6px 26px;
        font-size: 13px;
        font-weight: 600;
    }}
    QPushButton#DialogPrimary:hover {{ background-color: {primary_hover}; }}
    QPushButton#DialogPrimary:pressed {{ background-color: {primary_pressed}; }}
    QPushButton#DialogPrimary:focus {{ outline: none; }}
    /* 参考提示框：底色 + 左侧红色强调条，对应设计稿的红框提示卡片 */
    QLabel#CheckDesc {{
        background-color: {t.input_bg};
        border: 1px solid {t.input_border};
        border-left: 3px solid {t.text_error};
        border-radius: 3px;
        padding: 10px;
    }}

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
    QSpinBox, QDoubleSpinBox {{
        padding-right: 22px;
    }}
    QSpinBox::up-button, QDoubleSpinBox::up-button {{
        subcontrol-origin: border;
        subcontrol-position: top right;
        width: 20px;
        height: 14px;
        border-left: 1px solid {t.input_border};
        border-bottom: 1px solid {t.input_border};
        background-color: {t.btn_bg};
    }}
    QSpinBox::down-button, QDoubleSpinBox::down-button {{
        subcontrol-origin: border;
        subcontrol-position: bottom right;
        width: 20px;
        height: 14px;
        border-left: 1px solid {t.input_border};
        background-color: {t.btn_bg};
    }}
    QSpinBox::up-button:hover, QDoubleSpinBox::up-button:hover,
    QSpinBox::down-button:hover, QDoubleSpinBox::down-button:hover {{
        background-color: {t.btn_hover};
    }}
    QSpinBox::up-button:pressed, QDoubleSpinBox::up-button:pressed,
    QSpinBox::down-button:pressed, QDoubleSpinBox::down-button:pressed {{
        background-color: {t.btn_pressed};
    }}
    QSpinBox::up-arrow, QDoubleSpinBox::up-arrow {{
        image: url({_arrow_url(t.menu_text, up=True)});
        width: 10px;
        height: 8px;
    }}
    QSpinBox::down-arrow, QDoubleSpinBox::down-arrow {{
        image: url({_arrow_url(t.menu_text, up=False)});
        width: 10px;
        height: 8px;
    }}
    QLineEdit:disabled, QComboBox:disabled {{
        background-color: {t.panel_bg};
        color: {t.text_secondary};
    }}
    QComboBox::drop-down {{ border: none; width: 22px; background-color: transparent; }}
    /* 日期 / 时间输入框的日历按钮：**必须显式给 ::drop-down 宽度和箭头图**。
       Qt 的规矩是——只要给控件套了边框/背景（上面的共用规则就套了），原生下拉
       按钮就不再画；不给 ::drop-down，日历弹窗**根本点不开**（用户报的"日期字段
       没法快速赋值日期"）。画法与组合框保持一致。 */
    QDateEdit::drop-down, QDateTimeEdit::drop-down, QTimeEdit::drop-down {{
        border: none; width: 22px; background-color: transparent;
    }}
    QDateEdit::down-arrow, QDateTimeEdit::down-arrow, QTimeEdit::down-arrow {{
        image: url({_arrow_url(t.menu_text)});
        width: 10px;
        height: 8px;
        margin-right: 4px;
    }}
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

    /* 单选：**显式画圆点**。没有这条规则时，选中态在部分平台 / 主题下只剩一个
       空圈、看不出"选中了"（用户反映过）。未选 = 空圈，选中 = 强调色圈 + 中心圆点。 */
    QRadioButton::indicator {{
        width: 14px;
        height: 14px;
        border: 1px solid {t.input_border};
        border-radius: 7px;
        background: {t.input_bg};
    }}
    QRadioButton::indicator:hover {{ border-color: {t.input_focus}; }}
    QRadioButton::indicator:checked {{
        border: 1px solid {t.input_focus};
        background: qradialgradient(cx:0.5, cy:0.5, radius:0.5, fx:0.5, fy:0.5,
                    stop:0 {t.input_focus}, stop:0.4 {t.input_focus},
                    stop:0.45 {t.input_bg}, stop:1 {t.input_bg});
    }}
    QRadioButton::indicator:disabled {{ border-color: {t.text_secondary}; }}

    /* 勾选框：**显式画 indicator**。只靠平台原生样式的话，选中态在部分环境下会
       退化成"实心方块、看不出勾"（见 :func:`_check_url` 的说明），用户反映
       "勾选的颜色看不清"。这里统一成：未选 = 空框，选中 = 强调色描边 + 现画的
       对勾 PNG，半选 = 强调色描边 + 现画的横杠，任何主题 / 平台都清楚。
       ``QTableView::indicator`` 覆盖表格里 ``setCheckState`` 的复选框
       （QTableWidget 是 QTableView 的子类，同一条规则生效）。 */
    QCheckBox::indicator,
    QTableView::indicator {{
        width: 14px;
        height: 14px;
        border: 1px solid {t.input_border};
        border-radius: 2px;
        background: {t.input_bg};
    }}
    QCheckBox::indicator:hover,
    QTableView::indicator:hover {{ border-color: {t.input_focus}; }}
    QCheckBox::indicator:checked,
    QTableView::indicator:checked {{
        border: 1px solid {t.input_focus};
        background: {t.input_bg};
        image: url({_check_url(t.input_focus)});
    }}
    QCheckBox::indicator:indeterminate,
    QTableView::indicator:indeterminate {{
        border: 1px solid {t.input_focus};
        background: {t.input_bg};
        image: url({_dash_url(t.input_focus)});
    }}
    QCheckBox::indicator:disabled,
    QTableView::indicator:disabled {{
        border-color: {t.text_secondary};
        background: {t.panel_bg};
    }}

    QGroupBox {{
        /* 不显式设背景时 Qt 会落到系统默认色（Windows 上偏灰），导致内部控件
           （如 QSlider 控件外的空白区）显示一块突兀的灰。改成跟 QDialog 一致的
           window_bg，跟整体面板无缝衔接。*/
        background-color: {t.window_bg};
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
    /* 字段计算器的两个可勾选分组：标题里的勾选框默认会被顶到分组框上边框上、把
       边框挡住。这里把标题放到**边框内侧**（subcontrol-origin: border），上边框线
       在勾选框上方完整可见（与 QGIS 原生分组框观感一致）。QSS 选不中"可勾选"的
       分组框，故用 objectName 标定。 */
    QGroupBox#FieldCalcGroup {{ margin-top: 0px; padding-top: 24px; }}
    QGroupBox#FieldCalcGroup::title {{
        subcontrol-origin: border;
        subcontrol-position: top left;
        left: 8px;
        top: 2px;
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
    /* 「导入图层」弹窗里的图层列表（待导入 / 数据源 / GDB 子图层）：
       全局选中色 toolbar_hover 跟背景几乎同色，用户报"选中了看着像没选"。
       这里统一用强调色叠底的实色（比停靠面板重，见 DIALOG_LIST_SELECTION_ALPHA），
       文字保持主题前景色 —— 浅色主题下白字对比不足，深字才清楚。 */
    QListWidget#ImportLayerList, QListWidget#ExternalLayerList,
    QListWidget#ExternalSublayerList {{
        selection-background-color: {dock_selection_color(t, DIALOG_LIST_SELECTION_ALPHA)};
        selection-color: {t.menu_text};
    }}
    QListWidget#ImportLayerList::item:selected,
    QListWidget#ExternalLayerList::item:selected,
    QListWidget#ExternalSublayerList::item:selected {{
        background-color: {dock_selection_color(t, DIALOG_LIST_SELECTION_ALPHA)};
        color: {t.menu_text};
    }}
    /* 「组织列」对话框的字段勾选框：QSS 覆盖 indicator 后原生勾会被顶掉，
       所以未选/选中都显式画：未选 = 空框，选中 = 强调色描边 + 对勾 image。
       对勾用 ``_check_url`` 现画 PNG（主题色），保证任何平台/样式下都看得出勾。 */
    QListWidget#OrganizeColumnsList::indicator {{
        width: 14px;
        height: 14px;
        border: 1px solid {t.input_border};
        border-radius: 2px;
        background: {t.input_bg};
    }}
    QListWidget#OrganizeColumnsList::indicator:hover {{
        border-color: {t.input_focus};
    }}
    QListWidget#OrganizeColumnsList::indicator:checked {{
        border: 1px solid {t.input_focus};
        background: {t.input_bg};
        image: url({_check_url(t.input_focus)});
    }}
    /* 树里的勾选框（节点列表的节点行）：QTreeView 此前没有 indicator 规则，
       勾选框会退化成原生样式 —— Windows 原生样式常画成"实心方块、没有对勾"。
       与上面「组织列」那份同一套画法。 */
    QTreeView::indicator {{
        width: 14px;
        height: 14px;
        border: 1px solid {t.input_border};
        border-radius: 2px;
        background: {t.input_bg};
    }}
    QTreeView::indicator:hover {{
        border-color: {t.input_focus};
    }}
    QTreeView::indicator:checked {{
        border: 1px solid {t.input_focus};
        background: {t.input_bg};
        image: url({_check_url(t.input_focus)});
    }}
    /* 「样式引用」的符号分组勾选框：QListWidget 没有 indicator 规则时是平台原生
       方块，与节点列表（QTreeView）不一致；这里套同一套画法。 */
    QListWidget#SymbolStyleReferenceList::indicator {{
        width: 14px;
        height: 14px;
        border: 1px solid {t.input_border};
        border-radius: 2px;
        background: {t.input_bg};
    }}
    QListWidget#SymbolStyleReferenceList::indicator:hover {{
        border-color: {t.input_focus};
    }}
    QListWidget#SymbolStyleReferenceList::indicator:checked {{
        border: 1px solid {t.input_focus};
        background: {t.input_bg};
        image: url({_check_url(t.input_focus)});
    }}
    /* 节点列表那张表：X / Y 格是**直接在单元格里编辑**的。全局 QLineEdit 那条带
       ``padding: 4px 6px`` + 1px 边框，光文字就占 22px，塞进表格行里必然被切 ——
       这里把上下内距收掉只留左右，并把编辑文字也居中（与只读态对齐）。
       行高本身在代码里显式设（字体高 + 12），不靠 QSS padding 撑。 */
    QTableWidget#NodeListTable QLineEdit {{
        padding: 0 4px;
        border-radius: 0;
        text-align: center;
    }}
    /* 环切换页签（放在表格外面）：轻量一点，别抢表格的视觉重心 */
    QTabBar#NodeListRingBar::tab {{
        padding: 4px 14px;
        margin-right: 2px;
    }}
    QHeaderView::section {{
        background-color: {t.menu_bg};
        color: {t.menu_text};
        border: none;
        border-right: 1px solid {t.border};
        border-bottom: 1px solid {t.border};
        padding: 5px 8px;
    }}
    /* 表头排序箭头：与属性表同一套位置（字段名右侧），别让它跑到表头上方。
       给了 QHeaderView QSS 之后 Qt 不再自绘指示器，必须显式给图片和位置。 */
    QHeaderView::up-arrow, QHeaderView::down-arrow {{
        width: 9px;
        height: 9px;
        subcontrol-position: center right;
        right: 3px;
    }}
    QHeaderView::up-arrow {{ image: url({_arrow_url(t.menu_text, True)}); }}
    QHeaderView::down-arrow {{ image: url({_arrow_url(t.menu_text, False)}); }}
    QTableCornerButton::section {{
        background-color: {t.menu_bg};
        border: none;
        border-right: 1px solid {t.border};
        border-bottom: 1px solid {t.border};
    }}
    /* 表格行内小按钮（图层属性「字段」页的 更新 / 删除） */
    QPushButton#RowActionBtn, QPushButton#RowActionDanger {{
        background-color: transparent;
        border: 1px solid {t.border};
        border-radius: 3px;
        padding: 1px 8px;
        font-size: 11px;
    }}
    QPushButton#RowActionBtn {{ color: {t.input_focus}; }}
    QPushButton#RowActionBtn:hover {{
        background-color: {t.btn_hover};
        border-color: {t.input_focus};
    }}
    QPushButton#RowActionDanger {{ color: {t.text_error}; }}
    QPushButton#RowActionDanger:hover {{
        background-color: {_with_alpha(t.text_error, 26)};
        border-color: {t.text_error};
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

    /* 水平滑块。仅服务**垂直方向**或自定高度的 QSlider；图层属性的「透明度」
       等水平滑块已切到 ``app.ui.common.slider.FlatSlider``（完全自绘，
       不受 windowsvista 风格硬编码的 PE_PanelItemViewItem 影响）——后者
       在 paintEvent 里直接走 ThemeManager 取色，主题切换自动重绘。 */
    QSlider::groove:horizontal {{
        height: 6px;
        background: {t.window_bg};
        border: none;
        border-radius: 3px;
    }}
    QSlider {{
        background: {t.window_bg};
    }}
    QSlider::sub-page:horizontal {{
        background: {t.input_focus};
        border-radius: 3px;
    }}
    QSlider::add-page:horizontal {{
        background: {t.window_bg};
        border-radius: 3px;
    }}
    QSlider::handle:horizontal {{
        width: 12px;
        height: 12px;
        margin: -5px 0;
        border-radius: 6px;
        background: {t.window_bg};
        border: 1px solid {t.input_focus};
    }}
    QSlider::handle:horizontal:hover {{ background: {t.input_focus}; }}
    QSlider::handle:horizontal:pressed {{ background: {t.btn_pressed}; }}
    QSlider:disabled::sub-page:horizontal {{ background: {t.menu_hover}; }}

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
    /* 选中态：顶部加 2px 强调色描边 + 加粗，让激活标签从标签条上明显"抬"起来。
       普通态下边框已有 1px border；选中态通过 border-top-width 改到 2px 会撑高 1px，
       用 margin-top: -1px 抵消，保持标签条总高不变。*/
    QTabBar::tab:selected {{
        background-color: {t.window_bg};
        color: {t.menu_text};
        font-weight: bold;
        border-top: 2px solid {t.input_focus};
        margin-top: -1px;
    }}
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


#: 分隔线用主题强调色降透明度绘制——通栏实线会像地图上的一道疤，
#: 太淡又在深浅主题下都看不见，取中间值。
_DIVIDER_LINE_ALPHA = 110


def divider_colors(t: Theme) -> tuple[QColor, QColor, QColor]:
    """画布分隔条（地图对比 / 卷帘）配色 → ``(分隔线, 抓手填充, 抓手描边)``。

    分隔条由覆盖层用 QPainter 直接绘制、不吃 QSS，故这里返回 :class:`QColor`
    供 ``paintEvent`` 使用；配色仍集中在 :class:`Theme`，切主题自动跟随。

    分隔线用主题强调色 ``input_focus`` 降透明度：通栏实线会像地图上的一道疤，
    太淡又在深浅主题下都看不见。抓手用窗底色填充 + 主题强调色描边，深浅主题下
    都醒目可抓。
    """
    line = QColor(t.input_focus)
    line.setAlpha(_DIVIDER_LINE_ALPHA)
    return line, QColor(t.window_bg), QColor(t.input_focus)


def flow_panel_qss(t: Theme) -> str:
    """流程编辑器节点参数面板 QSS（作用于 dock 内容容器）。

    现代风格：卡片式通用区（:class:`QFrame` ``ParamCard``）、下划线式
    ``QTabBar``、胶囊算法快捷按钮（``ChipButton``）、虚线「新增」（``AddButton``）、
    危险删除按钮（``DangerButton``）、次要文字表头（``panelHeader`` 属性）。

    只套在参数面板的内容控件上，不触碰 QDockWidget 标题栏（标题栏仍由
    ``window_qss`` 负责，避免自设样式把 dock 标题压掉）。
    """
    danger_glow = _with_alpha(t.text_error, 26)  # 删除按钮 hover 半透明底
    return f"""
    QScrollArea {{ background-color: {t.window_bg}; border: none; }}
    QScrollArea > QWidget > QWidget {{ background-color: transparent; }}
    QWidget#ParamRoot {{ background-color: transparent; }}

    QLabel#CardTitle {{
        color: {t.menu_text};
        font-size: 12px;
        font-weight: 600;
    }}
    QLabel[panelHeader="true"] {{
        color: {t.text_secondary};
        font-size: 11px;
        font-weight: 600;
    }}

    QFrame#ParamCard {{
        background-color: {t.panel_bg};
        border: 1px solid {t.border};
        border-radius: 6px;
    }}

    QTabWidget::pane {{
        border: none;
        border-top: 1px solid {t.border};
        background-color: transparent;
        top: -1px;
    }}
    QTabBar::tab {{
        background: transparent;
        color: {t.text_secondary};
        border: none;
        border-bottom: 2px solid transparent;
        padding: 6px 10px;
        margin-right: 4px;
        font-size: 12px;
    }}
    QTabBar::tab:selected {{
        color: {t.menu_text};
        font-weight: 600;
        border-bottom: 2px solid {t.input_focus};
    }}
    QTabBar::tab:hover:!selected {{ color: {t.menu_text}; }}

    QPushButton {{
        background-color: {t.btn_bg};
        color: {t.menu_text};
        border: 1px solid {t.input_border};
        border-radius: 4px;
        padding: 5px 12px;
    }}
    QPushButton:hover {{ background-color: {t.btn_hover}; border-color: {t.border}; }}
    QPushButton:pressed {{ background-color: {t.btn_pressed}; }}

    QPushButton#ChipButton {{
        background-color: {t.input_bg};
        border: 1px solid {t.input_border};
        border-radius: 12px;
        padding: 3px 10px;
        font-size: 11px;
    }}
    QPushButton#ChipButton:hover {{
        background-color: {t.btn_hover};
        border-color: {t.input_focus};
        color: {t.input_focus};
    }}

    QPushButton#AddButton {{
        background-color: transparent;
        color: {t.input_focus};
        border: 1px dashed {t.input_border};
        border-radius: 4px;
        padding: 5px;
    }}
    QPushButton#AddButton:hover {{
        background-color: {t.btn_hover};
        border-color: {t.input_focus};
    }}

    QPushButton#DangerButton {{
        background-color: transparent;
        color: {t.text_error};
        border: 1px solid {t.border};
        border-radius: 4px;
    }}
    QPushButton#DangerButton:hover {{
        background-color: {danger_glow};
        border-color: {t.text_error};
    }}

    QToolButton#RowDel {{
        border: none;
        background-color: transparent;
        color: {t.text_secondary};
        border-radius: 3px;
        padding: 2px 6px;
        font-weight: 600;
    }}
    QToolButton#RowDel:hover {{
        color: {t.text_error};
        background-color: {danger_glow};
    }}
    """


def login_qss(t: Theme) -> str:
    """登录对话框 QSS（现代简洁卡片风格，与启动 splash 视觉统一）。

    结构：
    - 品牌区：应用图标 + 标题 + 副标题 + 强调线（``LoginTitle / LoginSubtitle /
      LoginAccent``）
    - 卡片表单：``LoginCard`` 圆角卡片，``LoginTab`` 登录方式切换（账号密码 /
      手机号，选中项用强调色下划线），``QStackedWidget#LoginStack`` 承载两套
      字段页；字段标签在输入框上方（``LoginFieldLabel``），圆角输入框
      ``LoginField``（聚焦/悬浮强调色描边）
    - 操作：主色实心 ``LoginPrimary``（hover/按下自动加深）+ 描边 ``LoginCancel``
    - 错误提示复用全局 ``loginError`` 规则（text_error 色），此处再显式声明一次

    内容走无边框标题栏（:class:`app.ui.common.base_dialog.BaseDialog`），
    ``:disabled`` 不单独设色：禁用态保持 Qt/平台原生观感。
    """
    primary = QColor(t.input_focus)
    hover = primary.darker(112)
    pressed = primary.darker(126)
    return f"""
    QDialog#LoginDialog {{
        background-color: {t.window_bg};
    }}
    QLabel#LoginTitle {{
        color: {t.menu_text};
        font-size: 20px;
        font-weight: 600;
    }}
    QLabel#LoginSubtitle {{
        color: {t.text_secondary};
        font-size: 12px;
    }}
    QLabel#LoginAccent {{
        background-color: {t.input_focus};
        border-radius: 1px;
    }}
    QFrame#LoginCard {{
        background-color: {t.panel_bg};
        border: 1px solid {t.border};
        border-radius: 8px;
    }}
    QLabel#LoginFieldLabel {{
        color: {t.text_secondary};
        font-size: 11px;
        font-weight: 600;
    }}
    QPushButton#LoginTab {{
        background: transparent;
        color: {t.text_secondary};
        border: none;
        border-bottom: 2px solid transparent;
        padding: 6px 4px;
        font-size: 13px;
    }}
    QPushButton#LoginTab:hover {{ color: {t.menu_text}; }}
    QPushButton#LoginTab:checked {{
        color: {t.input_focus};
        border-bottom: 2px solid {t.input_focus};
        font-weight: 600;
    }}
    QStackedWidget#LoginStack {{
        background: transparent;
        border: none;
    }}
    QPushButton#LoginCode {{
        background-color: transparent;
        color: {t.input_focus};
        border: 1px solid {t.input_border};
        border-radius: 6px;
        padding: 8px 12px;
        font-size: 12px;
    }}
    QPushButton#LoginCode:hover {{ border-color: {t.input_focus}; }}
    QPushButton#LoginCode:pressed {{ background-color: {t.btn_hover}; }}
    QPushButton#LoginCode:focus {{ outline: none; }}
    QLineEdit#LoginField {{
        background-color: {t.input_bg};
        color: {t.menu_text};
        border: 1px solid {t.input_border};
        border-radius: 6px;
        padding: 8px 10px;
        font-size: 13px;
        selection-background-color: {t.input_focus};
        selection-color: #ffffff;
    }}
    QLineEdit#LoginField:hover {{
        border-color: {t.input_focus};
    }}
    QLineEdit#LoginField:focus {{
        border-color: {t.input_focus};
    }}
    QLabel#loginError {{
        color: {t.text_error};
        font-size: 12px;
    }}
    QPushButton#LoginPrimary {{
        background-color: {t.input_focus};
        color: #ffffff;
        border: none;
        border-radius: 6px;
        padding: 8px 22px;
        font-size: 13px;
        font-weight: 600;
    }}
    QPushButton#LoginPrimary:hover {{ background-color: {hover.name()}; }}
    QPushButton#LoginPrimary:pressed {{ background-color: {pressed.name()}; }}
    QPushButton#LoginPrimary:focus {{ outline: none; }}
    QPushButton#LoginCancel {{
        background-color: transparent;
        color: {t.menu_text};
        border: 1px solid {t.input_border};
        border-radius: 6px;
        padding: 8px 22px;
        font-size: 13px;
    }}
    QPushButton#LoginCancel:hover {{ background-color: {t.btn_hover}; }}
    QPushButton#LoginCancel:pressed {{ background-color: {t.btn_pressed}; }}
    QPushButton#LoginCancel:focus {{ outline: none; }}
    """


def saved_login_popup_qss(t: Theme) -> str:
    """已保存账号气泡 QSS（:class:`app.ui.dialogs.saved_login_popup.SavedLoginPopup`）。

    这是一个 ``Qt.Popup`` 顶层窗口，**与登录框样式彼此独立**：

    - ``SavedLoginPopup``：圆角气泡本体（``menu_bg`` + ``border``，半透明底才能
      露出圆角）
    - ``SavedLoginHeader``：小标题（次要文字色）
    - ``SavedLoginList``：透明列表；行只给 ``border-radius`` / hover 底色，
      **不加 padding**（行高由 item 的 sizeHint 控制，加 padding 会让绘制区
      比行距高、相邻行互相覆盖）
    - ``SavedLoginUser`` / ``SavedLoginMask``：用户名 + 掩码密码
    - ``SavedLoginRemove`` / ``SavedLoginClear``：无底色小按钮，hover 变错误色

    选中 / 悬浮都用 ``menu_hover`` 这类**浅底色**而不是强调色实心：行内是真实
    子控件（QLabel），QSS 无法按 ``::item:selected`` 改子控件文字色，用实心强调色
    会出现深色文字压在深色底上。
    """
    return f"""
    QWidget#SavedLoginPopup {{
        background-color: {t.menu_bg};
        border: 1px solid {t.border};
        border-radius: 8px;
    }}
    QLabel#SavedLoginHeader {{
        color: {t.text_secondary};
        font-size: 11px;
        padding: 2px 4px;
    }}
    QListWidget#SavedLoginList {{
        background: transparent;
        border: none;
        outline: none;
    }}
    QListWidget#SavedLoginList::item {{
        border-radius: 4px;
    }}
    QListWidget#SavedLoginList::item:hover {{ background-color: {t.menu_hover}; }}
    QListWidget#SavedLoginList::item:selected {{ background-color: {t.menu_hover}; }}
    QLabel#SavedLoginUser {{
        color: {t.menu_text};
        font-size: 12px;
    }}
    QLabel#SavedLoginMask {{
        color: {t.text_secondary};
        font-size: 11px;
    }}
    QPushButton#SavedLoginRemove, QPushButton#SavedLoginClear {{
        background: transparent;
        border: none;
        color: {t.text_secondary};
        font-size: 11px;
    }}
    QPushButton#SavedLoginRemove:hover, QPushButton#SavedLoginClear:hover {{
        color: {t.text_error};
    }}
    """


def startup_project_qss(t: Theme) -> str:
    """启动「打开地图文档」对话框 QSS（ArcMap 启动页风格）。

    结构：标题 + 副标题；左窄栏分类导航 ``StartupNav``（圆角面板，选中行用
    强调色实心）；右侧最近工程明细表 ``StartupRecent``（表头次要色、隔行底色）；
    底部「文件名」输入行 ``StartupPath`` + 主/次按钮（``StartupPrimary`` /
    ``StartupSecondary``）。配色全部来自当前主题。
    """
    primary = QColor(t.input_focus)
    hover = primary.darker(112)
    pressed = primary.darker(126)
    return f"""
    QDialog#StartupProjectDialog {{
        background-color: {t.window_bg};
    }}
    QLabel#StartupHeading {{
        color: {t.menu_text};
        font-size: 18px;
        font-weight: 600;
    }}
    QLabel#StartupSub {{
        color: {t.text_secondary};
        font-size: 12px;
    }}
    QListWidget#StartupNav {{
        background-color: {t.panel_bg};
        border: 1px solid {t.border};
        border-radius: 6px;
        padding: 4px;
        outline: none;
        /* 必须在这里显式声明字体：全局 app_qss 的 `QDialog {{ font-size }}`
           是在弹窗 polish 之后才下发到 nav 的，QListWidget 的行高已按旧字体
           缓存，于是 `::item` 的 padding 只加在选中底色矩形上、不加进行距，
           选中时会压住相邻项。写死与 QDialog 一致的字体后，字体在样式解析
           阶段即确定，行距与绘制矩形一致。 */
        font-family: "Microsoft YaHei", "Segoe UI", sans-serif;
        font-size: 12px;
    }}
    QListWidget#StartupNav::item {{
        color: {t.menu_text};
        padding: 6px 8px;
        border-radius: 4px;
    }}
    QListWidget#StartupNav::item:hover {{
        background-color: {t.btn_hover};
    }}
    QListWidget#StartupNav::item:selected {{
        background-color: {t.input_focus};
        color: #ffffff;
    }}
    QTreeWidget#StartupRecent {{
        background-color: {t.input_bg};
        alternate-background-color: {t.panel_bg};
        border: 1px solid {t.border};
        border-radius: 6px;
        color: {t.menu_text};
        outline: none;
    }}
    QTreeWidget#StartupRecent::item {{
        padding: 4px 2px;
    }}
    QTreeWidget#StartupRecent::item:hover {{
        background-color: {t.btn_hover};
    }}
    QTreeWidget#StartupRecent::item:selected {{
        background-color: {t.input_focus};
        color: #ffffff;
    }}
    QTreeWidget#StartupRecent QHeaderView::section {{
        background-color: {t.panel_bg};
        color: {t.text_secondary};
        border: none;
        border-bottom: 1px solid {t.border};
        padding: 5px 6px;
    }}
    QLabel#StartupFieldLabel {{
        color: {t.text_secondary};
    }}
    QLineEdit#StartupPath {{
        background-color: {t.input_bg};
        color: {t.menu_text};
        border: 1px solid {t.input_border};
        border-radius: 5px;
        padding: 6px 8px;
        selection-background-color: {t.input_focus};
        selection-color: #ffffff;
    }}
    QLineEdit#StartupPath:hover {{
        border-color: {t.input_focus};
    }}
    QLineEdit#StartupPath:focus {{
        border-color: {t.input_focus};
    }}
    QPushButton#StartupPrimary {{
        background-color: {t.input_focus};
        color: #ffffff;
        border: none;
        border-radius: 5px;
        padding: 7px 22px;
        font-size: 13px;
        font-weight: 600;
    }}
    QPushButton#StartupPrimary:hover {{ background-color: {hover.name()}; }}
    QPushButton#StartupPrimary:pressed {{ background-color: {pressed.name()}; }}
    QPushButton#StartupPrimary:disabled {{
        background-color: {t.btn_pressed};
        color: {t.text_secondary};
    }}
    QPushButton#StartupPrimary:focus {{ outline: none; }}
    QPushButton#StartupSecondary {{
        background-color: transparent;
        color: {t.menu_text};
        border: 1px solid {t.input_border};
        border-radius: 5px;
        padding: 6px 14px;
        font-size: 13px;
    }}
    QPushButton#StartupSecondary:hover {{ background-color: {t.btn_hover}; }}
    QPushButton#StartupSecondary:pressed {{ background-color: {t.btn_pressed}; }}
    QPushButton#StartupSecondary:focus {{ outline: none; }}
    """


def add_data_qss(t: Theme) -> str:
    """「添加数据」弹窗 QSS（与启动「打开地图文档」同一套视觉语言）。

    结构：左栏 ``AddDataNav``（最近目录 / 快速访问 / 盘符，分组标题用次要色加粗）、
    右侧路径栏 ``AddDataPath`` + 类型筛选 ``AddDataFilter`` + 文件树
    ``AddDataTree``；底部选择摘要与主/次按钮。配色全部取当前主题。

    **文件树不写 ``::branch`` 基规则**：Qt 会因为没有 QSS 分支规则而退回原生
    分支绘制；一旦写了一条通配的 branch 规则，原生树会额外画连接线（见
    ``docks`` 里树面板的注释），离屏测试还看不出来，只在真实平台上翻车。
    """
    primary = QColor(t.input_focus)
    hover = primary.darker(112)
    pressed = primary.darker(126)
    return f"""
    QDialog#AddDataDialog {{
        background-color: {t.window_bg};
    }}
    QLabel#AddDataHeading {{
        color: {t.menu_text};
        font-size: 18px;
        font-weight: 600;
    }}
    QLabel#AddDataSub {{
        color: {t.text_secondary};
        font-size: 12px;
    }}
    QListWidget#AddDataNav {{
        background-color: {t.panel_bg};
        border: 1px solid {t.border};
        border-radius: 6px;
        padding: 4px;
        outline: none;
        /* 显式声明字体：全局 QDialog 的 font-size 在弹窗 polish 之后才下发，
           行高会按旧字体缓存，导致 ::item padding 只加在选中底色上（同
           StartupNav 的处理）。 */
        font-family: "Microsoft YaHei", "Segoe UI", sans-serif;
        font-size: 12px;
    }}
    QListWidget#AddDataNav::item {{
        color: {t.menu_text};
        padding: 6px 8px;
        border-radius: 4px;
    }}
    QListWidget#AddDataNav::item:hover {{
        background-color: {t.btn_hover};
    }}
    QListWidget#AddDataNav::item:selected {{
        background-color: {t.input_focus};
        color: #ffffff;
    }}
    QTreeWidget#AddDataTree {{
        background-color: {t.input_bg};
        alternate-background-color: {t.panel_bg};
        border: 1px solid {t.border};
        border-radius: 6px;
        color: {t.menu_text};
        outline: none;
        font-family: "Microsoft YaHei", "Segoe UI", sans-serif;
        font-size: 12px;
    }}
    QTreeWidget#AddDataTree::item {{
        padding: 4px 2px;
    }}
    QTreeWidget#AddDataTree::item:hover {{
        background-color: {t.btn_hover};
    }}
    QTreeWidget#AddDataTree::item:selected {{
        background-color: {t.input_focus};
        color: #ffffff;
    }}
    QTreeWidget#AddDataTree::item:disabled {{
        color: {t.text_secondary};
    }}
    QTreeWidget#AddDataTree QHeaderView::section {{
        background-color: {t.panel_bg};
        color: {t.text_secondary};
        border: none;
        border-bottom: 1px solid {t.border};
        padding: 5px 6px;
    }}
    QComboBox#AddDataPath {{
        background-color: {t.input_bg};
        color: {t.menu_text};
        border: 1px solid {t.input_border};
        border-radius: 5px;
        padding: 2px 6px;
        min-height: 22px;
    }}
    QComboBox#AddDataPath:hover, QComboBox#AddDataPath:focus {{
        border-color: {t.input_focus};
    }}
    QComboBox#AddDataPath QLineEdit {{
        background: transparent;
        border: none;
        color: {t.menu_text};
        padding: 3px 4px;
        selection-background-color: {t.input_focus};
        selection-color: #ffffff;
    }}
    QComboBox#AddDataPath::drop-down {{
        border: none;
        width: 22px;
    }}
    /* 路径下拉树（最近目录 / 快速访问 / 我的电脑） */
    QComboBox#AddDataPath QTreeView {{
        background-color: {t.input_bg};
        color: {t.menu_text};
        border: 1px solid {t.border};
        border-radius: 4px;
        outline: none;
        selection-background-color: {t.input_focus};
        selection-color: #ffffff;
    }}
    QComboBox#AddDataPath QTreeView::item {{
        padding: 5px 6px;
    }}
    QComboBox#AddDataPath QTreeView::item:hover {{
        background-color: {t.btn_hover};
    }}
    QComboBox#AddDataFilter {{
        background-color: {t.input_bg};
        color: {t.menu_text};
        border: 1px solid {t.input_border};
        border-radius: 5px;
        padding: 4px 8px;
        min-width: 96px;
    }}
    QComboBox#AddDataFilter:hover, QComboBox#AddDataFilter:focus {{
        border-color: {t.input_focus};
    }}
    QComboBox#AddDataFilter::drop-down {{
        border: none;
        width: 18px;
    }}
    QComboBox#AddDataFilter QAbstractItemView {{
        background-color: {t.input_bg};
        color: {t.menu_text};
        border: 1px solid {t.border};
        selection-background-color: {t.input_focus};
        selection-color: #ffffff;
        outline: none;
    }}
    QToolButton#AddDataTool {{
        background-color: transparent;
        border: 1px solid transparent;
        border-radius: 5px;
        padding: 4px;
    }}
    QToolButton#AddDataTool:hover {{
        background-color: {t.btn_hover};
        border-color: {t.input_border};
    }}
    QToolButton#AddDataTool:pressed {{
        background-color: {t.btn_pressed};
    }}
    QToolButton#AddDataTool:disabled {{
        background-color: transparent;
        border-color: transparent;
    }}
    QLabel#AddDataSummary {{
        color: {t.text_secondary};
        font-size: 12px;
    }}
    QLabel#AddDataHint {{
        color: {t.text_secondary};
        font-size: 12px;
    }}
    QPushButton#AddDataPrimary {{
        background-color: {t.input_focus};
        color: #ffffff;
        border: none;
        border-radius: 5px;
        padding: 7px 22px;
        font-size: 13px;
        font-weight: 600;
    }}
    QPushButton#AddDataPrimary:hover {{ background-color: {hover.name()}; }}
    QPushButton#AddDataPrimary:pressed {{ background-color: {pressed.name()}; }}
    QPushButton#AddDataPrimary:disabled {{
        background-color: {t.btn_pressed};
        color: {t.text_secondary};
    }}
    QPushButton#AddDataPrimary:focus {{ outline: none; }}
    QPushButton#AddDataSecondary {{
        background-color: transparent;
        color: {t.menu_text};
        border: 1px solid {t.input_border};
        border-radius: 5px;
        padding: 6px 14px;
        font-size: 13px;
    }}
    QPushButton#AddDataSecondary:hover {{ background-color: {t.btn_hover}; }}
    QPushButton#AddDataSecondary:pressed {{ background-color: {t.btn_pressed}; }}
    QPushButton#AddDataSecondary:focus {{ outline: none; }}
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
    pal.setColor(QPalette.ColorRole.ToolTipBase, QColor(theme.tooltip_bg))
    pal.setColor(QPalette.ColorRole.ToolTipText, QColor(theme.tooltip_text))
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
    "divider_colors",
    "flow_panel_qss",
    "login_qss",
    "saved_login_popup_qss",
    "apply_app_theme",
    "apply_app_tooltip_style",
]
