"""画布左侧竖排工具条。

位置：悬浮盖在地图画布**左上角**（left 4px / top 3px，由
``MainWindow._attach_side_toolbar_overlay`` 把本工具条 ``setParent`` 到画布的
**父容器**（中央控件）并摆到「画布左上角 + (4, 3)」——与画布同级，坐标相对父
容器，画布缩放时由 ``MainWindow._place_side_toolbar`` 重算；不占中央布局，
也不挤占画布空间）。

层级：因为与画布**同级**（而非画布子控件），工具条 ``raise_()`` 后永远盖在
画布**及其所有子覆盖层**（悬浮消息条 / 卷帘 / 对比分隔条……）之上；由
``MainWindow._keep_side_toolbar_on_top``（接 ``mapCanvasRefreshed`` /
``extentsChanged``）持续重新置顶。

背景：容器是 :class:`app.ui.common.frosted.FrostedPanel`——把工具条**背后**
的地图做高斯模糊当底图，再叠一层 ``主题底色 @ 60%`` 的半透明，配圆角细边框，
得到跟随主题的毛玻璃效果（浅色主题浅灰、深色主题深灰；底图算法与刷新时机见
:mod:`app.ui.common.frosted`）。

工具清单（自上而下，与设计稿一致）::

    选择 / 框选 / 漫游 / 缩放 / 全图 / 清除
    属性 / 条件查询 / 空间查询 / 选择屏蔽 / 取消屏蔽 / 分屏 / 定位
    线测量 / 面测量 / 范围裁剪

行为约定：

- 项目**已实现**的工具直接接主窗口既有回调（与顶部工具条同一入口）：
  选择 / 框选 / 漫游 / 缩放 / 全图 / 清除 / 条件查询 / 空间查询 / 分屏 / 定位 /
  线测量 / 面测量 / 选择屏蔽 / 取消屏蔽 / 范围裁剪；
- 「属性」与顶部「查询」组的「属性」是同一个入口（``identify``），内部调用
  :class:`~app.ui.tools.i_query.IQueryTool`：点要素后弹出 ArcMap 风格的
  「I查询」浮动面板，按图层列出全部命中要素；两边按钮共用 ``identify`` key，
  单选高亮由 ``MainWindow._on_map_tool_set`` 统一同步；
- 「缩放」用 :class:`~app.ui.tools.pan_zoom.ZoomClickTool`：**左键单击放大、
  按住 Shift 单击缩小**（以点击点为缩放中心），与顶部工具条上独立的「放大 /
  缩小」按钮（拉框缩放）不是同一个入口；
- 「分屏」复用 :mod:`app.ui.tools.qmap_compare` 的「地图对比」入口
  （``MainWindow._on_qmap_compare``：镜像分屏 / 垂直分割 / 水平分割 /
  放大镜），与顶部工具条「地图对比」同一实现，不再用旧的卷帘覆盖层；
- 「定位」点击后弹出 :class:`~app.ui.main_window.coord_locate_overlay.CoordLocateOverlay`
  气泡（浮在地图**右上角**），由主窗口
  :meth:`~app.ui.main_window.mixins.shell.WindowShellMixin._on_coord_locate`
  接管：再点同按钮或点气泡上的 ``×`` 都能关闭，按钮随气泡显隐独立常亮
  （会话型，与分屏同高亮组 :data:`_SESSION_KEYS`）；
- 「线测量」「面测量」接 :mod:`app.ui.tools.measure`：激活后连续左键落点，
  实时在画布底部读数条显示折线长度 / 多边形面积（右键结束、``Esc`` 清除），
  属地图工具（单选高亮，见 :data:`_TOOL_KEYS`）；
- 其余工具为占位：点击时经消息条提示「该工具正在开发中」（气泡悬浮在
  地图上，不挤压画布）；
- 地图工具（选择 / 框选 / 漫游 / 缩放 / 线测量 / 面测量）为单选高亮：跟随画布
  激活的 :class:`QgsMapTool`，由 ``MainWindow._on_map_tool_set`` 调
  :meth:`set_active` 与顶部工具条同步；会话型工具「分屏」独立常亮
  （:meth:`set_session_active`）；其余按钮为一次性动作，不高亮。

图标用 Font Awesome（见 :mod:`app.ui.common.tool_icons`），随主题变色
（:meth:`apply_theme`）。
"""

from __future__ import annotations

from collections.abc import Callable

from qgis.core import Qgis
from qgis.PyQt.QtCore import QSize, Qt
from qgis.PyQt.QtGui import QFont
from qgis.PyQt.QtWidgets import QFrame, QSizePolicy, QToolButton, QVBoxLayout, QWidget

from app.ui.common import tool_icons
from app.ui.common.frosted import FrostedPanel
from app.ui.common.theme import TOOLBAR_BUTTON_RADIUS, Theme, ThemeManager

#: 地图工具 key（单选高亮组）。多数与顶部工具条同 key 便于同步；「缩放」用独立
#: 的 ``side_zoom``，避免与顶部「放大」联动高亮。
_TOOL_KEYS = frozenset({
    "select_point",
    "select_box",
    "pan",
    "side_zoom",
    "identify",
    "measure_line",
    "measure_area",
})
#: 图标 key 别名：按钮的**高亮 key** 与顶部工具条区分（``side_zoom`` 独立高亮），
#: 但图标复用 ``zoom_in`` 的那张，外观不变。
_ICON_ALIAS = {"side_zoom": "zoom_in", "spatial_query": "space_query_polygon"}
#: 会话型工具 key（独立常亮，不受地图工具切换影响；与顶部工具条同 key）。
#: 「定位」也归这里：点开气泡 → 按钮保持高亮表示"气泡在显示"，
#: 关闭气泡（再点同按钮或气泡上的 ×）→ 按钮弹起，与分屏会话同一套高亮逻辑。
#: 「范围裁剪」同理：裁剪生效期间按钮保持高亮，再点一次即取消并弹起。
_SESSION_KEYS = frozenset({"qmap_compare", "coord_locate", "extent_clip"})
#: 侧栏图标逻辑边长（原型左侧工具列 44×36 按钮 / 17px 图标）
_ICON = 17


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
        # 毛玻璃容器：背后地图高斯模糊 + 主题底色 60% 半透明（跟随主题）
        canvas = getattr(self.main_window, "canvas", None)
        widget = FrostedPanel(canvas)
        widget.setObjectName("SideToolBar")
        layout = QVBoxLayout(widget)
        layout.setContentsMargins(2, 4, 2, 4)
        layout.setSpacing(2)

        color = ThemeManager.instance().current().input_focus
        # (文本, 图标 key, 分组后是否加分隔线)；callbacks 缺 key → 占位工具
        for text, key, separator_after in [
            ("选择", "select_point", False),
            ("框选", "select_box", False),
            ("漫游", "pan", False),
            ("缩放", "side_zoom", False),
            ("全图", "zoom_full", False),
            ("前视图", "view_prev", False),
            ("后视图", "view_next", False),
            ("清除", "clear_selection", True),
            ("属性", "identify", False),
            ("条件查询", "condition_query", False),
            ("空间查询", "spatial_query", False),
            ("选择屏蔽", "select_mask", False),
            ("取消屏蔽", "unmask", False),
            ("分屏", "qmap_compare", False),
            ("定位", "coord_locate", True),
            ("线测量", "measure_line", False),
            ("面测量", "measure_area", False),
            ("范围裁剪", "extent_clip", False),
        ]:
            # 固定统一宽度 + 水平居中：文字短的按钮不再整体靠左
            btn = self._make_button(text, key, callbacks, color)
            btn.setFixedWidth(45)
            layout.addWidget(btn, 0, Qt.AlignmentFlag.AlignHCenter)
            if separator_after:
                layout.addWidget(self._separator())

        layout.addStretch(1)
        # 窄条 + 两行文案会顶高按钮：压掉 app_qss 给 QToolButton 的 3px 上下内边距
        # （每个按钮省 6px；挂在本面板上只作用于 15 个侧栏按钮，不影响其它按钮）
        # 圆角跟主/编辑工具条保持一致
        widget.setStyleSheet(f"QToolButton {{ padding: 0px; border-radius: {TOOLBAR_BUTTON_RADIUS}px; }}")
        widget.setFixedWidth(52)
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
        # 文案排版：2~3 字单行（≤ 33px，45px 按钮放得下）；4 字折两行（44px 会顶到边）。
        # 注意 len <= 3 的文案**不能**加 "\n"：尾随换行会多出一个空行，白白高一行
        # （实测 40px → 52px，15 个按钮共 +180px）。
        btn.setText(text if len(text) <= 3 else f"{text[:2]}\n{text[2:]}")

        font = QFont()
        font.setPointSize(8)  # 这里改字号，pt单位；或者 setPixelSize(10) 像素单位
        btn.setFont(font)

        btn.setToolTip(text)
        icon_key = _ICON_ALIAS.get(key, key)
        btn.setIcon(tool_icons.icon(icon_key, color, _ICON))
        btn.setIconSize(QSize(_ICON, _ICON))
        btn.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextUnderIcon)
        btn.setAutoRaise(True)
        callback = callbacks.get(key)
        if key in _TOOL_KEYS or key in _SESSION_KEYS:
            # 地图工具：单选高亮；会话型工具（分屏）：独立常亮
            btn.setCheckable(True)
            self._actions[key] = btn
        btn.clicked.connect(
            callback if callback is not None else (lambda checked=False, t=text: self._not_implemented(t))
        )
        self._icon_items.append((btn, icon_key))
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
        """地图工具单选高亮：仅 ``key`` 对应按钮勾选，其余弹起。

        会话型工具（分屏）不受影响，由 :meth:`set_session_active` 独立控制。
        """
        for k, btn in self._actions.items():
            if k in _SESSION_KEYS:
                continue
            btn.setChecked(k == key)

    def set_session_active(self, key: str, active: bool) -> None:
        """会话型工具独立开关：会话存活期间按钮保持高亮（分屏）。"""
        if key not in _SESSION_KEYS:
            return
        btn = self._actions.get(key)
        if btn is not None:
            btn.setChecked(active)

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
        color = theme.input_focus
        for btn, key in self._icon_items:
            btn.setIcon(tool_icons.icon(key, color, _ICON))
