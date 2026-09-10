"""图层树 Dock。

封装 :class:`QgsLayerTreeView` + :class:`QgsLayerTreeModel`，并发出右键菜单信号。

Dock 内容顶部带一条 QGIS 风格的头部工具条（只发信号，行为由主窗口实现）：

- 打开图层样式面板 → ``style_panel_requested``
- 添加图层组 → ``add_group_requested``
- 删除图层/组 → ``remove_requested``
- 全部展开 / 全部折叠 → ``expand_all_requested`` / ``collapse_all_requested``
- 上移 / 下移 → ``move_up_requested`` / ``move_down_requested``

右键菜单同样只发信号：``context_menu_requested(layer, 全局坐标)``——图层由
``index2node`` 从被点的节点直接解出（**不按图层名回查**，重名图层不会拿错），
菜单本体由主窗口构建，且与工程管理 Dock 共用同一套。
"""

from __future__ import annotations

from contextlib import suppress

from qgis.core import QgsApplication, QgsLayerTreeModel, QgsProject
from qgis.gui import QgsLayerTreeView
from qgis.PyQt.QtCore import QPoint, QSize, Qt, pyqtSignal
from qgis.PyQt.QtGui import QIcon
from qgis.PyQt.QtWidgets import (
    QDockWidget,
    QHBoxLayout,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

# QGIS 主题图标名（3.40 / 4.2 两个运行时均已实测可用）
_THEME_ICONS = {
    "style": "mActionOptions.svg",
    "group": "mActionAddGroup.svg",
    "remove": "mActionRemoveLayer.svg",
    "expand": "mActionExpandTree.svg",
    "collapse": "mActionCollapseTree.svg",
    "up": "mActionArrowUp.svg",
    "down": "mActionArrowDown.svg",
}

# (按钮属性名, 图标键, 信号名, 悬停提示)
# 顺序按 QGIS 桌面图层面板：样式 → 添加组 → 删除 → 展开/折叠所有 → 上移/下移
_HEADER_SPECS = (
    ("btn_style_panel", "style", "style_panel_requested", "打开图层样式面板"),
    ("btn_add_group", "group", "add_group_requested", "添加图层组"),
    ("btn_remove", "remove", "remove_requested", "删除图层/组"),
    ("btn_expand_all", "expand", "expand_all_requested", "全部展开"),
    ("btn_collapse_all", "collapse", "collapse_all_requested", "全部折叠"),
    ("btn_move_up", "up", "move_up_requested", "上移图层/组"),
    ("btn_move_down", "down", "move_down_requested", "下移图层/组"),
)


def _theme_icon(name: str) -> QIcon:
    """取 QGIS 主题图标；不可用时返回空图标（按钮仍可点）。"""
    with suppress(Exception):
        icon = QgsApplication.getThemeIcon(name)
        if icon is not None and not icon.isNull():
            return icon
    return QIcon()


class LayerDock(QDockWidget):
    """独立的图层树面板。"""

    context_menu_requested = pyqtSignal(object, object)  # (QgsMapLayer, QPoint 全局坐标)
    layer_clicked = pyqtSignal(object)
    style_panel_requested = pyqtSignal()
    add_group_requested = pyqtSignal()
    remove_requested = pyqtSignal()
    expand_all_requested = pyqtSignal()
    collapse_all_requested = pyqtSignal()
    move_up_requested = pyqtSignal()
    move_down_requested = pyqtSignal()

    def __init__(self, parent=None) -> None:
        super().__init__("图层列表", parent)
        self.setAllowedAreas(Qt.DockWidgetArea.LeftDockWidgetArea | Qt.DockWidgetArea.RightDockWidgetArea)

        self.project: QgsProject = QgsProject.instance()
        self.tree_root = self.project.layerTreeRoot()

        self.model = QgsLayerTreeModel(self.tree_root)
        self.model.setFlags(
            QgsLayerTreeModel.AllowNodeRename
            | QgsLayerTreeModel.AllowNodeReorder
            | QgsLayerTreeModel.AllowNodeChangeVisibility
            # 不显示图例：分类/渐变渲染会挤进 "1"、"2" 等图例项
        )

        self.view = QgsLayerTreeView(self)
        self.view.setModel(self.model)
        self.view.clicked.connect(self.layer_clicked.emit)
        self.view.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.view.customContextMenuRequested.connect(self._on_view_context_menu)

        self._build_header()

        # 内容 = 头部工具条 + 树视图（保留原生 Dock 标题栏）
        body = QWidget(self)
        body_layout = QVBoxLayout(body)
        body_layout.setContentsMargins(0, 0, 0, 0)
        body_layout.setSpacing(0)
        body_layout.addWidget(self.header_bar)
        body_layout.addWidget(self.view, 1)
        self.setWidget(body)

    # ------------------------------------------------------------- 事件

    def _on_view_context_menu(self, pos: QPoint) -> None:
        """右键图层节点 → 发 ``(layer, 全局坐标)``，菜单本体由主窗口构建。

        图层用 ``index2node`` 从被点的节点解出，而不是拿显示名去
        ``QgsProject.mapLayersByName`` 回查——**同名图层会回查到错的那个**
        （树里看是 A，菜单作用的却是 B）。

        ``customContextMenuRequested`` 的 pos 是 **viewport 坐标**：``indexAt``
        与 ``mapToGlobal`` 都按 viewport 解释，与工程管理 Dock 的写法一致。

        组节点 / 图例节点没有 ``layer()``（返回 None），无效位置也直接返回，
        一律不弹菜单。
        """
        index = self.view.indexAt(pos)
        if not index.isValid():
            return
        # index2node 对越界/野索引会抛异常；槽内异常未捕获会崩（0xC0000409）
        try:
            node = self.view.index2node(index)
        except Exception:  # noqa: BLE001
            return
        layer_getter = getattr(node, "layer", None)
        layer = layer_getter() if callable(layer_getter) else None
        if layer is None:
            return
        self.context_menu_requested.emit(layer, self.view.viewport().mapToGlobal(pos))

    # ------------------------------------------------------- 头部工具条

    def _build_header(self) -> None:
        """构造 QGIS 式头部工具条：左对齐的小图标按钮。

        样式集中到 :func:`app.ui.common.theme.window_qss` 的
        ``QWidget#LayerDockHeader`` 规则（背景色 + 底部分隔线 + 按钮 hover/press），
        主题切换自动跟随；这里只负责布局（紧凑：边距 / 间距 / 按钮 / 图标尺寸）
        和 ``objectName`` 绑定。
        """
        self.header_bar = QWidget(self)
        self.header_bar.setObjectName("LayerDockHeader")
        header_layout = QHBoxLayout(self.header_bar)
        # 紧凑：上下贴边，左右留 4px，按钮间距 1px
        header_layout.setContentsMargins(4, 0, 4, 0)
        header_layout.setSpacing(2)
        for attr, icon_key, signal_name, tip in _HEADER_SPECS:
            btn = QToolButton(self.header_bar)
            btn.setAutoRaise(True)
            btn.setIcon(_theme_icon(_THEME_ICONS[icon_key]))
            btn.setToolTip(tip)
            btn.setIconSize(QSize(14, 14))
            btn.setFixedSize(QSize(20, 20))
            btn.clicked.connect(getattr(self, signal_name).emit)
            header_layout.addWidget(btn)
            setattr(self, attr, btn)
        # QGIS 桌面风格：按钮靠左排列，右侧留白
        header_layout.addStretch(1)
