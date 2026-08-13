"""流程编排：左侧节点库 dock。

按分组列出内置模板（数据源 / 矢量分析 / 栅格分析 / 通用），顶部提供搜索过滤。
「处理算法」分组首次展开时从 QGIS 处理工具箱注册表懒加载全部算法（可能上百个，
展开一次后缓存）。拖拽叶子节点发出自定义 mime（:data:`TEMPLATE_MIME`），
由画布 :class:`FlowView` 接收建节点。
"""

from __future__ import annotations

from PyQt6.QtCore import QMimeData, QSize, Qt
from PyQt6.QtGui import QDrag
from PyQt6.QtWidgets import (
    QAbstractItemView,
    QDockWidget,
    QHBoxLayout,
    QLineEdit,
    QToolButton,
    QTreeWidget,
    QTreeWidgetItem,
    QVBoxLayout,
    QWidget,
)

from app.core.domain.flow.algorithm_library import BUILTIN_TEMPLATES, NODE_GROUPS, list_processing_algorithms
from app.ui.flow_editor.view import TEMPLATE_MIME

_ALGO_GROUP = "处理算法"


class _LibraryTree(QTreeWidget):
    """节点库树：拖拽叶子时发送模板 key（自定义 mime）。"""

    def startDrag(self, supportedActions) -> None:  # noqa: N802, N803 —— Qt 命名
        item = self.currentItem()
        key = getattr(item, "_key_", None)
        if not key:
            return
        drag = QDrag(self)
        mime = QMimeData()
        mime.setData(TEMPLATE_MIME, key.encode("utf-8"))
        mime.setText(key)
        drag.setMimeData(mime)
        drag.exec(Qt.DropAction.CopyAction)


class NodeLibraryDock(QDockWidget):
    """节点库 dock。"""

    def __init__(self, parent=None) -> None:
        super().__init__("节点库", parent)
        self.setFeatures(
            QDockWidget.DockWidgetFeature.DockWidgetClosable
            | QDockWidget.DockWidgetFeature.DockWidgetMovable
            | QDockWidget.DockWidgetFeature.DockWidgetFloatable
        )
        self.setAllowedAreas(
            Qt.DockWidgetArea.LeftDockWidgetArea
            | Qt.DockWidgetArea.RightDockWidgetArea
            | Qt.DockWidgetArea.BottomDockWidgetArea
        )
        self._algo_group_loaded = False
        self._build_ui()

    # ------------------------------------------------------------ UI

    def _build_ui(self) -> None:
        wrapper = QWidget(self)
        layout = QVBoxLayout(wrapper)
        layout.setContentsMargins(8, 8, 8, 8)
        layout.setSpacing(6)

        header = QHBoxLayout()
        header.setContentsMargins(0, 0, 0, 0)
        header.setSpacing(4)
        self.search_edit = QLineEdit()
        self.search_edit.setPlaceholderText("搜索节点…")
        self.search_edit.setClearButtonEnabled(True)
        self.search_edit.textChanged.connect(self._apply_filter)
        header.addWidget(self.search_edit, 1)

        self.btn_expand = QToolButton()
        self.btn_expand.setText("展开")
        self.btn_expand.setToolTip("展开所有分组")
        self.btn_expand.clicked.connect(lambda: self.tree.expandAll())
        header.addWidget(self.btn_expand)

        self.btn_collapse = QToolButton()
        self.btn_collapse.setText("折叠")
        self.btn_collapse.setToolTip("折叠所有分组")
        self.btn_collapse.clicked.connect(lambda: self.tree.collapseAll())
        header.addWidget(self.btn_collapse)
        layout.addLayout(header)

        self.tree = _LibraryTree()
        self.tree.setHeaderHidden(True)
        self.tree.setAnimated(True)
        self.tree.setIndentation(18)
        self.tree.setUniformRowHeights(True)
        self.tree.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.tree.setDragEnabled(True)
        self.tree.itemExpanded.connect(self._on_item_expanded)
        layout.addWidget(self.tree, 1)

        self._build_tree()
        self.setWidget(wrapper)

    def _build_tree(self) -> None:
        self.tree.clear()
        group_font = self.tree.font()
        group_font.setBold(True)

        grouped: dict[str, list] = {}
        for t in BUILTIN_TEMPLATES.values():
            grouped.setdefault(t.group, []).append(t)
        for gname in NODE_GROUPS:
            group = QTreeWidgetItem(self.tree)
            group.setText(0, gname)
            group.setFont(0, group_font)
            group.setSizeHint(0, QSize(0, 28))
            for t in grouped.get(gname, []):
                child = QTreeWidgetItem(group)
                child.setText(0, t.name)
                child._key_ = t.key
                if t.description:
                    child.setToolTip(0, t.description)
                child.setSizeHint(0, QSize(0, 26))
            group.setExpanded(True)

        # 「处理算法」分组：首次展开时懒加载（可能数百条）
        algo_group = QTreeWidgetItem(self.tree)
        algo_group.setText(0, _ALGO_GROUP)
        algo_group.setFont(0, group_font)
        algo_group.setSizeHint(0, QSize(0, 28))
        algo_group.setExpanded(False)

    def _on_item_expanded(self, item) -> None:
        if item.text(0) != _ALGO_GROUP or self._algo_group_loaded:
            return
        self._algo_group_loaded = True
        for alg_id, name in list_processing_algorithms():
            child = QTreeWidgetItem(item)
            child.setText(0, name)
            child._key_ = alg_id
            child.setToolTip(0, alg_id)
            child.setSizeHint(0, QSize(0, 24))
        # 加载后全树条目数变大，刷新一次视图
        self.tree.viewport().update()

    # ------------------------------------------------------------ 交互

    def _apply_filter(self, text: str) -> None:
        text = text.strip().lower()
        for i in range(self.tree.topLevelItemCount()):
            group = self.tree.topLevelItem(i)
            if not text:
                group.setHidden(False)
                for j in range(group.childCount()):
                    group.child(j).setHidden(False)
                group.setExpanded(group.text(0) != _ALGO_GROUP or self._algo_group_loaded)
                continue
            group_match = text in group.text(0).lower()
            matched = 0
            for j in range(group.childCount()):
                child = group.child(j)
                show = group_match or text in child.text(0).lower()
                child.setHidden(not show)
                if show:
                    matched += 1
            group.setHidden(matched == 0)
            if group_match and matched:
                group.setExpanded(True)
