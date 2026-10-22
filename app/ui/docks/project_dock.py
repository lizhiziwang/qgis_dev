"""工程管理 Dock。

点击"文件 → 新建工程"后，先弹窗输入工程名，创建工程后把名字带到本 Dock 展示
（只读，不在 Dock 里创建/编辑）。内容分三部分：
1. 工程名 —— 只读展示（名字来自新建工程弹窗）
2. 数据图层树 / 参考图层树 —— 两个分组的图层列表
3. 渲染与标注 —— 针对当前选中图层的"渲染设置"/"标注设置"入口

UI 只负责发信号，真正清空/创建工程由 :class:`MainWindow` 处理
（沿用本项目"UI 发信号、MainWindow 做业务"的回调注入模式）。
"""

from __future__ import annotations

from qgis.PyQt.QtCore import Qt, pyqtSignal
from qgis.PyQt.QtGui import QFont
from qgis.PyQt.QtWidgets import (
    QDockWidget,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QTreeView,
    QVBoxLayout,
    QWidget,
)

from qgis.core import QgsLayerTreeModel, QgsProject


class ProjectDock(QDockWidget):
    """工程管理面板。"""

    #: 点击"渲染设置"，携带选中的图层
    render_requested = pyqtSignal(object)
    #: 点击"标注设置"，携带选中的图层
    label_requested = pyqtSignal(object)
    #: 图层右键菜单请求，携带 (图层, 全局坐标)；由 MainWindow 弹 QGIS 式图层菜单
    layer_context_menu_requested = pyqtSignal(object, object)

    #: 只读分组树：只列图层名 + 可见性勾选框，不显示图例/符号（避免分类/渐变
    #: 渲染的图例项如 "1"、"2" 挤进树里）
    _GROUP_FLAGS = QgsLayerTreeModel.AllowNodeChangeVisibility

    def __init__(self, parent=None) -> None:
        super().__init__("工程", parent)
        # saveState/restoreState 按 objectName 匹配 Dock；空名字会被 Qt 丢弃并告警。
        self.setObjectName("ProjectDock")
        self.setAllowedAreas(Qt.DockWidgetArea.LeftDockWidgetArea | Qt.DockWidgetArea.RightDockWidgetArea)
        self.setMinimumSize(320, 480)

        self._selected_layer = None
        self._model: QgsLayerTreeModel | None = None

        self._build_ui()

    # ------------------------------------------------------------------ UI

    def _build_ui(self) -> None:
        wrapper = QWidget(self)
        layout = QVBoxLayout(wrapper)
        layout.setContentsMargins(8, 8, 8, 8)
        layout.setSpacing(6)

        # 1. 工程名（只读展示，名字来自新建工程弹窗）
        self.name_label = QLabel("工程名：—")
        font = QFont()
        font.setBold(True)
        self.name_label.setFont(font)
        layout.addWidget(self.name_label)

        # 2. 数据图层树
        layout.addWidget(QLabel("数据图层"))
        self.data_view = QTreeView(self)
        self.data_view.setHeaderHidden(True)  # QgsLayerTreeModel 的 header 会显示 "1"
        self.data_view.clicked.connect(self._on_tree_clicked)
        self.data_view.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.data_view.customContextMenuRequested.connect(self._on_view_context_menu)
        layout.addWidget(self.data_view, 3)

        # 3. 参考图层树
        layout.addWidget(QLabel("参考图层"))
        self.ref_view = QTreeView(self)
        self.ref_view.setHeaderHidden(True)  # QgsLayerTreeModel 的 header 会显示 "1"
        self.ref_view.clicked.connect(self._on_tree_clicked)
        self.ref_view.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.ref_view.customContextMenuRequested.connect(self._on_view_context_menu)
        layout.addWidget(self.ref_view, 3)

        # 4. 渲染与标注
        layout.addWidget(QLabel("渲染与标注"))
        self.current_label = QLabel("当前图层：—")
        layout.addWidget(self.current_label)
        btn_row = QHBoxLayout()
        self.render_btn = QPushButton("渲染设置")
        self.label_btn = QPushButton("标注设置")
        self.render_btn.setEnabled(False)
        self.label_btn.setEnabled(False)
        self.render_btn.clicked.connect(
            lambda: self.render_requested.emit(self._selected_layer)
        )
        self.label_btn.clicked.connect(
            lambda: self.label_requested.emit(self._selected_layer)
        )
        btn_row.addWidget(self.render_btn)
        btn_row.addWidget(self.label_btn)
        btn_row.addStretch()
        layout.addLayout(btn_row)

        self.setWidget(wrapper)

    # -------------------------------------------------------------- 事件

    def _on_tree_clicked(self, index) -> None:
        if self._model is None:
            return
        node = self._model.index2node(index)
        layer = node.layer() if node is not None and hasattr(node, "layer") else None
        self._set_selected_layer(layer)

    def _on_view_context_menu(self, pos) -> None:
        """右键图层节点 → 发信号让 MainWindow 弹 QGIS 式图层菜单。"""
        if self._model is None:
            return
        view = self.sender()
        index = view.indexAt(pos)
        if not index.isValid():
            return
        node = self._model.index2node(index)
        layer = node.layer() if node is not None and hasattr(node, "layer") else None
        if layer is None:
            return
        # customContextMenuRequested 的 pos 是 viewport 坐标
        self.layer_context_menu_requested.emit(layer, view.viewport().mapToGlobal(pos))

    # ------------------------------------------------------------ 对外接口

    def set_project(self, name: str, data_group, ref_group) -> None:
        """工程创建完成后绑定两个分组的树，并显示工程名。"""
        # QgsLayerTreeModel 只接受整棵根树作 model root；QgsLayerTreeView 会强制
        # 显示根树、忽略 setRootIndex，所以这里用普通 QTreeView + 各自的
        # rootIndex 指向分组，实现"只显示该分组"。
        self._clear_tree_models()
        root = QgsProject.instance().layerTreeRoot()
        self._model = QgsLayerTreeModel(root)
        self._model.setFlags(self._GROUP_FLAGS)
        self.data_view.setModel(self._model)
        self.ref_view.setModel(self._model)
        self.data_view.setRootIndex(self._model.node2index(data_group))
        self.ref_view.setRootIndex(self._model.node2index(ref_group))

        self.name_label.setText(f"工程名：{name}")
        self._set_selected_layer(None)

    def reset(self) -> None:
        """复位 UI（打开无分组的工程 / 未创建工程时）。"""
        self._clear_tree_models()
        self.name_label.setText("工程名：—")
        self._set_selected_layer(None)

    # --------------------------------------------------------------- 私有

    def _clear_tree_models(self) -> None:
        """解除旧树绑定并释放旧模型，避免引用已销毁的分组节点。"""
        if self._model is not None:
            self._model.deleteLater()
            self._model = None
        self.data_view.setModel(None)
        self.ref_view.setModel(None)

    def _set_selected_layer(self, layer) -> None:
        self._selected_layer = layer
        name = layer.name() if layer is not None else "—"
        self.current_label.setText(f"当前图层：{name}")
        self.render_btn.setEnabled(layer is not None)
        self.label_btn.setEnabled(layer is not None)


__all__ = ["ProjectDock"]
