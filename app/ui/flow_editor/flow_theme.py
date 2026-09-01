"""流程编排：主题调色板。

把全局 :class:`app.ui.common.theme.Theme` 映射成画布图形项（节点 / 连线）的
配色，让流程编辑器画布跟随全局主题（浅色 / 深色 / 蓝色 / 绿色）。中性色
（画布底、节点底、边框、连线）全部取自主题；语义色不随主题变，保持辨识度：

- 端口类型色（:data:`PORT_COLORS`）
- 节点状态色（运行中蓝 / 成功绿 / 失败红 / 跳过灰）
- 高亮橙（选中描边 / 依赖链高亮 / 连线高亮）
"""

from __future__ import annotations

from dataclasses import dataclass

from qgis.PyQt.QtGui import QColor

from app.ui.common.theme import THEMES, Theme

#: 语义色（深浅主题都醒目，不随主题变）
_ACCENT = QColor("#ff9800")       # 选中描边 / 连线高亮
_TITLE_TEXT = QColor("#ffffff")   # 标题栏文字（压在主题强调色上）
_TITLE_DISABLED = QColor("#9e9e9e")  # 禁用节点标题栏
_BADGE_TEXT = QColor("#ffcc80")   # 禁用/跳过徽标


@dataclass(frozen=True)
class FlowPalette:
    """画布图形项配色。除语义色字段外全部由 :class:`Theme` 派生。"""

    canvas_bg: QColor       # 画布底色（视图 + 场景背景刷）
    node_fill: QColor       # 节点主体 / 输入端口空心插口底色
    node_text: QColor       # 节点体内输入/输出名称文字
    node_shadow: QColor     # 节点投影阴影（浅色主题黑影，深色主题微亮影）
    node_border: QColor     # 节点边框
    title_bg: QColor        # 标题栏默认色（无运行状态时）
    title_text: QColor      # 标题文字
    title_disabled: QColor  # 禁用节点标题栏
    badge_text: QColor      # 禁用/跳过徽标
    selected: QColor        # 选中描边
    edge_normal: QColor     # 连线常态
    edge_highlight: QColor  # 连线高亮


def _shadow_color(theme: Theme) -> QColor:
    """按主题背景亮度选阴影色：浅色用黑影，深色用微亮影（黑影在深底上不可见）。"""
    c = QColor(theme.window_bg)
    lum = 0.299 * c.red() + 0.587 * c.green() + 0.114 * c.blue()
    if lum < 128:
        return QColor(255, 255, 255, 22)
    return QColor(0, 0, 0, 88)


def flow_palette(theme: Theme) -> FlowPalette:
    """:class:`Theme` → :class:`FlowPalette`。中性色取自主题，语义色固定。"""
    return FlowPalette(
        canvas_bg=QColor(theme.window_bg),
        node_fill=QColor(theme.panel_bg),
        node_text=QColor(theme.menu_text),
        node_shadow=_shadow_color(theme),
        node_border=QColor(theme.input_border),
        title_bg=QColor(theme.input_focus),
        title_text=_TITLE_TEXT,
        title_disabled=_TITLE_DISABLED,
        badge_text=_BADGE_TEXT,
        selected=_ACCENT,
        edge_normal=QColor(theme.text_secondary),
        edge_highlight=_ACCENT,
    )


#: 当前生效调色板。未显式应用主题时为浅色（独立构造场景 / 测试也成立）。
_CURRENT = flow_palette(THEMES["light"])


def apply_theme(theme: Theme) -> None:
    """切换全局调色板（画布图形项跟随全局主题）。"""
    global _CURRENT
    _CURRENT = flow_palette(theme)


def current() -> FlowPalette:
    return _CURRENT


__all__ = ["FlowPalette", "apply_theme", "current", "flow_palette"]
