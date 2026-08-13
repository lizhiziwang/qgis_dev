"""工具树 Dock。

迁自 :class:`component.TreeTool.PGISTools`，把"工具树 UI"与"瓦片后台任务"分离。

纯工具树展示：工具按类别分组，顶部提供搜索过滤与展开/折叠控制。
点击工具节点弹出对应参数对话框（对话框内部是"左参数 + 右算法描述"布局，
描述逻辑见 :mod:`app.ui.common.algo_desc`）；同一算法只维护一个对话框实例，
已打开时点击置前。
"""

from __future__ import annotations

from PyQt6.QtCore import QSize, Qt
from PyQt6.QtWidgets import (
    QAbstractItemView,
    QDockWidget,
    QHBoxLayout,
    QLineEdit,
    QMenu,
    QToolButton,
    QTreeWidget,
    QTreeWidgetItem,
    QVBoxLayout,
    QWidget,
)

from app.infra.logging import get_logger

logger = get_logger(__name__)


def _make_vector_tile_dialog(parent):
    from app.ui.dialogs.vector_tile import VectorTileDialog

    return VectorTileDialog(parent, title="创建矢量瓦片", dia_type="native:writevectortiles_xyz")


def _make_buffer_dialog(parent):
    from app.ui.dialogs.buffer import BufferDialog

    return BufferDialog(parent, title="矢量缓冲区")


def _make_overlay_dialog(parent):
    from app.ui.dialogs.overlay import OverlayDialog

    return OverlayDialog(parent, title="叠加分析")


def _make_shortest_path_dialog(parent):
    from app.ui.dialogs.shortest_path import ShortestPathDialog

    return ShortestPathDialog(parent, title="最短路径分析")


def _make_reproject_dialog(parent):
    from app.ui.dialogs.reproject import ReprojectDialog

    return ReprojectDialog(parent, title="重投影")


def _make_imagery_tile_dialog(parent):
    from app.ui.dialogs.imagery_tile import ImageryTileDialog

    return ImageryTileDialog(parent, title="影像切片")


def _make_imagery_mbtiles_dialog(parent):
    from app.ui.dialogs.imagery_mbtiles import ImageryMbtilesDialog

    return ImageryMbtilesDialog(parent, title="影像切片（MBTiles）")


def _make_flow_editor(parent):
    """流程节点编排工具：打开主窗口内的流程编辑器（懒建单实例）。"""
    opener = getattr(parent, "_open_flow_editor", None)
    if opener is not None:
        opener()


#: 工具节点 code → 对话框工厂（懒 import，避免 dock 构造时拉进全部 dialog）
_DIALOG_FACTORIES = {
    "native:writevectortiles_xyz": _make_vector_tile_dialog,
    "native:buffer": _make_buffer_dialog,
    "overlay": _make_overlay_dialog,
    "native:shortestpathpointtopoint": _make_shortest_path_dialog,
    "native:reprojectlayer": _make_reproject_dialog,
    "native:tilesxyzdirectory": _make_imagery_tile_dialog,
    "native:tilesxyzmbtiles": _make_imagery_mbtiles_dialog,
}

#: 工具节点 code → 一次性动作（打开非对话框窗口，无需实例缓存）
_SIMPLE_ACTIONS = {
    "flow:editor": _make_flow_editor,
}

#: 工具节点 code → 一行说明（树节点 tooltip）
_TOOL_TIPS = {
    "native:writevectortiles_xyz": "按 XYZ 模板把矢量图层导出为矢量瓦片",
    "native:buffer": "按指定距离为输入图层生成缓冲区面",
    "overlay": "对两个矢量图层做叠加分析",
    "native:shortestpathpointtopoint": "计算两点之间的最短路径",
    "native:reprojectlayer": "把图层重投影到目标坐标系",
    "native:tilesxyzdirectory": "把当前地图画布切片为 XYZ 瓦片目录",
    "native:tilesxyzmbtiles": "把当前地图画布切片为 MBTiles 文件",
    "flow:editor": "可视化流程节点编排：拖节点建流程、连线、运行与导出",
}

#: 树分组：(分组名, [(工具名, 算法 code), ...])
_TOOL_GROUPS = [
    (
        "矢量分析",
        [
            ("矢量缓冲区", "native:buffer"),
            ("叠加分析", "overlay"),
            ("最短路径分析", "native:shortestpathpointtopoint"),
            ("重投影", "native:reprojectlayer"),
        ],
    ),
    (
        "数据导出",
        [
            ("生成矢量瓦片（XYZ）", "native:writevectortiles_xyz"),
            ("影像切片（XYZ）", "native:tilesxyzdirectory"),
            ("影像切片（MBTiles）", "native:tilesxyzmbtiles"),
        ],
    ),
    (
        "流程编排",
        [
            ("节点式流程编排", "flow:editor"),
        ],
    ),
]


class ToolsDock(QDockWidget):
    """工具树 dock。"""

    def __init__(self, parent=None) -> None:
        super().__init__("分析工具", parent)
        self.topParent = parent
        self.setFeatures(QDockWidget.DockWidgetFeature.DockWidgetClosable | QDockWidget.DockWidgetFeature.DockWidgetMovable | QDockWidget.DockWidgetFeature.DockWidgetFloatable)
        self.setAllowedAreas(
            Qt.DockWidgetArea.LeftDockWidgetArea | Qt.DockWidgetArea.RightDockWidgetArea | Qt.DockWidgetArea.BottomDockWidgetArea
        )
        #: 算法 code → 当前对话框实例。同一算法只允许一个窗口，已打开时点击置前。
        self._dialogs: dict = {}
        self._build_ui()

    def _build_ui(self) -> None:
        wrapper = QWidget(self)
        layout = QVBoxLayout(wrapper)
        layout.setContentsMargins(8, 8, 8, 8)
        layout.setSpacing(6)

        # ---- 顶部工具条：搜索过滤 + 展开/折叠 ----
        header = QHBoxLayout()
        header.setContentsMargins(0, 0, 0, 0)
        header.setSpacing(4)
        self.search_edit = QLineEdit()
        self.search_edit.setPlaceholderText("搜索工具…")
        self.search_edit.setClearButtonEnabled(True)
        self.search_edit.textChanged.connect(self._apply_filter)
        header.addWidget(self.search_edit, 1)

        self.btn_expand = QToolButton()
        self.btn_expand.setText("展开")
        self.btn_expand.setToolTip("展开所有分组")
        self.btn_expand.clicked.connect(self._expand_all)
        header.addWidget(self.btn_expand)

        self.btn_collapse = QToolButton()
        self.btn_collapse.setText("折叠")
        self.btn_collapse.setToolTip("折叠所有分组")
        self.btn_collapse.clicked.connect(self._collapse_all)
        header.addWidget(self.btn_collapse)

        layout.addLayout(header)

        # ---- 工具树 ----
        self.tree = QTreeWidget()
        self.tree.setHeaderHidden(True)
        self.tree.setAnimated(True)
        self.tree.setIndentation(18)
        self.tree.setUniformRowHeights(True)
        self.tree.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.tree.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.tree.itemClicked.connect(self._on_item_clicked)
        # 双击 / 回车同样打开（对话框复用逻辑天然幂等）
        self.tree.itemActivated.connect(self._on_item_clicked)
        self.tree.customContextMenuRequested.connect(self._on_context_menu)
        layout.addWidget(self.tree, 1)

        self._build_tree()
        self.setWidget(wrapper)

    def _build_tree(self) -> None:
        """按 :data:`_TOOL_GROUPS` 构建分组树；分组加粗，叶子加 tooltip。"""
        group_font = self.tree.font()
        group_font.setBold(True)
        for group_name, tools in _TOOL_GROUPS:
            group = QTreeWidgetItem(self.tree)
            group.setText(0, group_name)
            group.setFont(0, group_font)
            group.setSizeHint(0, QSize(0, 28))
            for text, code in tools:
                item = QTreeWidgetItem(group)
                item.setText(0, text)
                item._code_ = code
                tip = _TOOL_TIPS.get(code)
                if tip:
                    item.setToolTip(0, tip)
                item.setSizeHint(0, QSize(0, 28))
            group.setExpanded(True)

    # ------------------------------------------------------------ 交互

    def _expand_all(self) -> None:
        self.tree.expandAll()

    def _collapse_all(self) -> None:
        self.tree.collapseAll()

    def _apply_filter(self, text: str) -> None:
        """按名称实时过滤树：隐藏不匹配叶子与无匹配分组；清空后恢复并展开。"""
        text = text.strip().lower()
        for i in range(self.tree.topLevelItemCount()):
            group = self.tree.topLevelItem(i)
            if not text:
                group.setHidden(False)
                for j in range(group.childCount()):
                    group.child(j).setHidden(False)
                group.setExpanded(True)
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
            if group_match:
                group.setExpanded(True)

    def _on_context_menu(self, pos) -> None:
        """右键菜单：叶子 → 打开工具；分组 → 展开/折叠。"""
        item = self.tree.itemAt(pos)
        if item is None:
            return
        menu = QMenu(self.tree)
        code = getattr(item, "_code_", "")
        if code in _DIALOG_FACTORIES or code in _SIMPLE_ACTIONS:
            act = menu.addAction("打开工具")
            act.triggered.connect(lambda: self._on_item_clicked(item, 0))
        else:
            act_text = "折叠" if item.isExpanded() else "展开"
            act = menu.addAction(act_text)
            act.triggered.connect(lambda: item.setExpanded(not item.isExpanded()))
        menu.exec(self.tree.viewport().mapToGlobal(pos))

    def _on_item_clicked(self, item, column) -> None:
        code = getattr(item, "_code_", "")
        try:
            # 一次性动作（打开非对话框窗口，如流程编辑器）
            simple = _SIMPLE_ACTIONS.get(code)
            if simple is not None:
                simple(self.topParent)
                return
            factory = _DIALOG_FACTORIES.get(code)
            if factory is None:
                return
            dlg = self._dialogs.get(code)
            if dlg is not None:
                try:
                    if dlg.isVisible():
                        # 已有该算法窗口实例：置前，不新建
                        dlg.raise_()
                        dlg.activateWindow()
                        return
                except RuntimeError:
                    # C++ 对象已被销毁（防御性清理，正常流程不会走到）
                    self._dialogs.pop(code, None)
                    dlg = None
            if dlg is None:
                dlg = factory(self.topParent)
                self._dialogs[code] = dlg
            dlg.show()
        except Exception as exc:  # noqa: BLE001
            # 不静默吞掉：曾因静默异常导致功能"无反应"难排查（见 CanvasZoomTool 教训）
            logger.error("打开工具对话框失败 code=%s: %s", code, exc, exc_info=True)
