"""画布左侧竖排工具条。

位置：左侧面板（图层列表 / 数据浏览器 Dock）与地图画布之间的窄竖条（由
``MainWindow._build_central_widget`` 把本工具条插进中央布局的第一列）。

工具清单（自上而下，与设计稿一致）::

    选择 / 矩形选择 / 遨游 / 缩放 / 全图 / 清除
    一键查询 / 条件查询 / 选择屏蔽 / 取消屏蔽 / 卷帘 / 坐标定位
    线测量 / 面测量 / 范围裁剪

行为约定：

- 项目**已实现**的工具直接接主窗口既有回调（与顶部工具条同一入口）：
  选择 / 矩形选择 / 遨游 / 缩放 / 全图 / 清除 / 条件查询 / 卷帘；
- 其余工具为占位：点击时经消息条提示「该工具正在开发中」（气泡悬浮在
  地图上，不挤压画布）；
- 地图工具（选择 / 矩形选择 / 遨游 / 缩放 / 卷帘）为单选高亮：跟随画布
  激活的 :class:`QgsMapTool`，由 ``MainWindow._on_map_tool_set`` 调
  :meth:`set_active` 与顶部工具条同步；其余按钮为一次性动作，不高亮。

图标用 Font Awesome（见 :mod:`app.ui.common.tool_icons`），随主题变色
（:meth:`apply_theme`）。
"""

from __future__ import annotations

from typing import Callable

from qgis.core import Qgis
from qgis.PyQt.QtCore import Qt
from qgis.PyQt.QtWidgets import QFrame, QSizePolicy, QToolButton, QVBoxLayout, QWidget

from app.ui.common import tool_icons
from app.ui.common.theme import Theme, ThemeManager

#: 地图工具 key（单选高亮组，与顶部工具条 key 一致便于同步）
_TOOL_KEYS = frozenset({"select_point", "select_box", "pan", "zoom_in", "swipe"})


class SideToolBar:
    """画布左侧竖排工具条构建器。

    :param main_window: 主窗口（取 messageBar 提示 / 主题）
    :param callbacks: 工具 key → 回调；**未提供的 key 即占位工具**，点击提示
        「该工具正在开发中」
    """

    def __init__(self, main_window, callbacks: dict[str, Callable[[], None]]) -> None:
        self.main_window = main_window
        self.widget: QWidget | None = None
        #: 工具 key → 按钮（高亮 / 主题刷新用）
        self._actions: dict[str, QToolButton] = {}
        self._icon_items: list[tuple[QToolButton, str]] = []
        self.build(callbacks)

    # ------------------------------------------------------------ 装配

    def build(self, callbacks: dict[str, Callable[[], None]]) -> QWidget:
        """构建竖排工具条控件。

        :param callbacks: 已实现工具的回调；缺 key 的工具按占位处理
        """
        widget = QWidget()
        widget.setObjectName("SideToolBar")
        layout = QVBoxLayout(widget)
        layout.setContentsMargins(2, 4, 2, 4)
        layout.setSpacing(2)

        color = ThemeManager.instance().current().toolbar_text
        # (文本, 图标 key, 分组后是否加分隔线)；callbacks 缺 key → 占位工具
        for text, key, separator_after in [
            ("选择", "select_point", False),
            ("矩形选择", "select_box", False),
            ("遨游", "pan", False),
            ("缩放", "zoom_in", False),
            ("全图", "zoom_full", False),
            ("清除", "clear_selection", True),
            ("一键查询", "one_key_query", False),
            ("条件查询", "condition_query", False),
            ("选择屏蔽", "select_mask", False),
            ("取消屏蔽", "unmask", False),
            ("卷帘", "swipe", False),
            ("坐标定位", "coord_locate", True),
            ("线测量", "measure_line", False),
            ("面测量", "measure_area", False),
            ("范围裁剪", "extent_clip", False),
        ]:
            # 固定统一宽度 + 水平居中：文字短的按钮不再整体靠左
            btn = self._make_button(text, key, callbacks, color)
            btn.setFixedWidth(64)
            layout.addWidget(btn, 0, Qt.AlignmentFlag.AlignHCenter)
            if separator_after:
                layout.addWidget(self._separator())

        layout.addStretch(1)
        widget.setFixedWidth(72)
        widget.setSizePolicy(QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Expanding)
        self.widget = widget
        return widget

    def _make_button(
        self,
        text: str,
        key: str,
        callbacks: dict[str, Callable[[], None]],
        color: str,
    ) -> QToolButton:
        """建一个竖排按钮：图标在上文字在下；占位工具点击提示开发中。"""
        btn = QToolButton()
        btn.setObjectName(f"SideTool_{key}")
        btn.setText(text)
        btn.setToolTip(text)
        btn.setIcon(tool_icons.icon(key, color))
        btn.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextUnderIcon)
        btn.setAutoRaise(True)
        callback = callbacks.get(key)
        if key in _TOOL_KEYS:
            # 地图工具：单选高亮，跟随画布激活工具（set_active 统一推导勾选）
            btn.setCheckable(True)
            self._actions[key] = btn
        btn.clicked.connect(
            callback if callback is not None else (lambda checked=False, t=text: self._not_implemented(t))
        )
        self._icon_items.append((btn, key))
        return btn

    @staticmethod
    def _separator() -> QFrame:
        """按钮分组分隔线。"""
        line = QFrame()
        line.setFrameShape(QFrame.Shape.HLine)
        line.setFrameShadow(QFrame.Shadow.Sunken)
        return line

    # ------------------------------------------------------------ 状态

    def set_active(self, key: str | None) -> None:
        """地图工具单选高亮：仅 ``key`` 对应按钮勾选，其余弹起。"""
        for k, btn in self._actions.items():
            btn.setChecked(k == key)

    def _not_implemented(self, text: str) -> None:
        """占位工具点击：气泡提示开发中（悬浮在地图上，不挤压画布）。"""
        bar = getattr(self.main_window, "messageBar", None)
        if bar is None:
            return
        bar.pushMessage(
            f"「{text}」工具正在开发中，敬请期待",
            level=Qgis.MessageLevel.Info,
            duration=2,
        )

    # ------------------------------------------------------------ 主题

    def apply_theme(self, theme: Theme) -> None:
        """主题切换时刷新按钮图标颜色。"""
        if self.widget is None:
            return
        color = theme.toolbar_text
        for btn, key in self._icon_items:
            btn.setIcon(tool_icons.icon(key, color))
