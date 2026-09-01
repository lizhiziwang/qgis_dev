"""地图对比参数 Dialog（参照 qmapcompare 插件面板）。

左侧图层树勾选要对比的图层（任意数量、支持分组递归）；右侧选择对比
模式（镜像分屏 / 垂直分割 / 水平分割 / 放大镜）及放大镜形状与大小。
确认后由 :mod:`app.ui.tools.qmap_compare` 启动对应会话。
"""

from __future__ import annotations

from qgis.PyQt.QtCore import Qt
from qgis.PyQt.QtWidgets import (
    QButtonGroup,
    QHBoxLayout,
    QLabel,
    QMessageBox,
    QPushButton,
    QRadioButton,
    QSlider,
    QSplitter,
    QTreeWidget,
    QTreeWidgetItem,
    QVBoxLayout,
    QWidget,
)
from qgis.core import QgsLayerTreeGroup, QgsLayerTreeLayer, QgsProject

from app.ui.common.base_dialog import BaseDialog
from app.ui.tools.qmap_compare import (
    LENS_MAX_SIZE_RATE,
    LENS_MIN_SIZE_RATE,
)

#: 模式 key -> 中文名（Mirror 在 mask 方案之外，单独处理）
MODE_LABELS = {
    "vertical": "垂直分割（左/右）",
    "horizontal": "水平分割（上/下）",
    "lens": "放大镜（跟随鼠标）",
    "mirror": "镜像分屏（双画布）",
}


class QMapCompareDialog(BaseDialog):
    """地图对比参数对话框（``exec()`` 模态）。"""

    def __init__(self, parent=None) -> None:
        super().__init__(parent, "地图对比")
        self.setMinimumSize(640, 460)
        self.project = QgsProject.instance()
        #: 用户勾选过的图层 id（重新 populate 时保持勾选）
        self.checked_layers: list[str] = []
        self._init_ui()
        self.project.layersAdded.connect(self._populate_tree)
        self.project.layersRemoved.connect(self._populate_tree)

    # ------------------------------------------------------------------
    def _init_ui(self) -> None:
        # BaseDialog 约定：裸建 QVBoxLayout + self.setLayout(...)
        layout = QVBoxLayout()
        layout.setContentsMargins(16, 16, 16, 16)
        layout.setSpacing(10)

        splitter = QSplitter(Qt.Orientation.Horizontal)
        splitter.setChildrenCollapsible(False)

        # ---- 左：图层勾选树 ----
        left = QWidget()
        left_layout = QVBoxLayout(left)
        left_layout.setContentsMargins(0, 0, 0, 0)
        left_layout.addWidget(QLabel("对比图层（可多选）"))
        self.layer_tree = QTreeWidget()
        self.layer_tree.setHeaderHidden(True)
        self.layer_tree.itemChanged.connect(self._on_item_changed)
        left_layout.addWidget(self.layer_tree)
        splitter.addWidget(left)

        # ---- 右：模式 + 放大镜设置 ----
        right = QWidget()
        right_layout = QVBoxLayout(right)
        right_layout.setContentsMargins(8, 0, 0, 0)
        right_layout.addWidget(QLabel("对比模式"))
        self._mode_group = QButtonGroup(self)
        self._mode_radios: dict[str, QRadioButton] = {}
        for key, label in MODE_LABELS.items():
            radio = QRadioButton(label)
            self._mode_group.addButton(radio)
            self._mode_radios[key] = radio
            right_layout.addWidget(radio)
        self._mode_radios["vertical"].setChecked(True)

        # 放大镜设置（默认垂直模式下隐藏，切到 lens 才显示）
        self._lens_box = QVBoxLayout()
        self._lens_box.addWidget(QLabel("放大镜形状"))
        shape_row = QHBoxLayout()
        self._circle_radio = QRadioButton("圆形")
        self._circle_radio.setChecked(True)
        self._square_radio = QRadioButton("方形")
        self._shape_group = QButtonGroup(self)
        self._shape_group.addButton(self._circle_radio)
        self._shape_group.addButton(self._square_radio)
        shape_row.addWidget(self._circle_radio)
        shape_row.addWidget(self._square_radio)
        shape_row.addStretch()
        # 放大镜设置整体放进容器 widget（可见性一次切换，避免 layout 嵌套转移）
        self._lens_widget = QWidget()
        lens_layout = QVBoxLayout(self._lens_widget)
        lens_layout.setContentsMargins(0, 0, 0, 0)
        lens_layout.addLayout(shape_row)
        lens_layout.addWidget(QLabel("放大镜大小"))
        self._size_slider = QSlider(Qt.Orientation.Horizontal)
        self._size_slider.setRange(
            int(LENS_MIN_SIZE_RATE * 100), int(LENS_MAX_SIZE_RATE * 100)
        )
        self._size_slider.setValue(15)
        self._size_value_label = QLabel("15%")
        self._size_slider.valueChanged.connect(
            lambda v: self._size_value_label.setText(f"{v}%")
        )
        size_row = QHBoxLayout()
        size_row.addWidget(self._size_slider)
        size_row.addWidget(self._size_value_label)
        lens_layout.addLayout(size_row)
        right_layout.addWidget(self._lens_widget)
        self._update_lens_visibility()

        self._mode_group.buttonToggled.connect(lambda *_: self._update_lens_visibility())
        right_layout.addStretch()
        splitter.addWidget(right)
        splitter.setSizes([380, 240])
        layout.addWidget(splitter)

        # ---- 底部按钮 ----
        bottom = QHBoxLayout()
        bottom.addStretch()
        self.start_btn = QPushButton("开始对比")
        self.start_btn.setDefault(True)
        self.start_btn.clicked.connect(self._on_start)
        cancel_btn = QPushButton("取消")
        cancel_btn.clicked.connect(self.reject)
        bottom.addWidget(self.start_btn)
        bottom.addWidget(cancel_btn)
        layout.addLayout(bottom)

        self.setLayout(layout)
        self._populate_tree()

    # ------------------------------------------------------------------
    def _update_lens_visibility(self) -> None:
        self._lens_widget.setVisible(self._mode_radios["lens"].isChecked())

    def _on_item_changed(self, item: QTreeWidgetItem, _column: int) -> None:
        """记忆用户勾选状态（populate 时保持）。"""
        layer_id = item.data(0, Qt.ItemDataRole.UserRole)
        if not layer_id:
            return
        if item.checkState(0) == Qt.CheckState.Checked:
            if layer_id not in self.checked_layers:
                self.checked_layers.append(layer_id)
        else:
            self.checked_layers = [i for i in self.checked_layers if i != layer_id]

    # ------------------------------------------------------------------
    def _populate_tree(self, *_args) -> None:
        """从图层树重建勾选树（保留用户勾选）。"""
        self.layer_tree.blockSignals(True)
        self.layer_tree.clear()
        self._populate_recursive(self.project.layerTreeRoot(), None)
        self.layer_tree.blockSignals(False)
        self.layer_tree.expandAll()

    def _populate_recursive(self, node, parent_item: QTreeWidgetItem | None) -> None:
        for child in node.children():
            if isinstance(child, QgsLayerTreeGroup):
                item = QTreeWidgetItem([child.name()])
                item.setFlags(item.flags() & ~Qt.ItemFlag.ItemIsUserCheckable)
                item.setIcon(0, self.layer_tree.style().standardIcon(
                    self.layer_tree.style().StandardPixmap.SP_DirIcon
                ))
                if parent_item is None:
                    self.layer_tree.addTopLevelItem(item)
                else:
                    parent_item.addChild(item)
                self._populate_recursive(child, item)
            elif isinstance(child, QgsLayerTreeLayer):
                layer = child.layer()
                if layer is None or not layer.isValid():
                    continue
                item = QTreeWidgetItem([layer.name()])
                item.setData(0, Qt.ItemDataRole.UserRole, layer.id())
                item.setFlags(
                    item.flags() | Qt.ItemFlag.ItemIsUserCheckable
                )
                checked = layer.id() in self.checked_layers
                item.setCheckState(
                    0,
                    Qt.CheckState.Checked if checked else Qt.CheckState.Unchecked,
                )
                if parent_item is None:
                    self.layer_tree.addTopLevelItem(item)
                else:
                    parent_item.addChild(item)

    # ------------------------------------------------------------------
    def showEvent(self, event) -> None:  # noqa: N802 —— Qt 命名
        """每次显示重刷图层树（工程变化后也能选到新图层）。"""
        super().showEvent(event)
        self._populate_tree()

    def _on_start(self) -> None:
        layers = self._checked_layers()
        if not layers:
            QMessageBox.warning(self, "提示", "请至少勾选一个对比图层")
            return
        if self._mode() == "mirror" and len(layers) == 0:
            QMessageBox.warning(self, "提示", "镜像分屏至少需要一个对比图层")
            return
        self.accept()

    # ------------------------------------------------------------------
    def _checked_layers(self) -> list:
        """返回勾选图层（树序，父节点在后即子在前）。"""
        layers = []
        self._collect_checked(self.layer_tree.invisibleRootItem(), layers)
        return layers

    def _collect_checked(self, item: QTreeWidgetItem, out: list) -> None:
        for i in range(item.childCount()):
            child = item.child(i)
            if child.childCount() > 0:
                self._collect_checked(child, out)
            layer_id = child.data(0, Qt.ItemDataRole.UserRole)
            if layer_id and child.checkState(0) == Qt.CheckState.Checked:
                layer = self.project.mapLayer(layer_id)
                if layer is not None:
                    out.append(layer)

    def _mode(self) -> str:
        for key, radio in self._mode_radios.items():
            if radio.isChecked():
                return key
        return "vertical"

    # ------------------------------------------------------------------
    def selected(self) -> tuple:
        """返回 (对比图层列表, 模式, 放大镜形状, 放大镜大小比例)。"""
        shape = "circle" if self._circle_radio.isChecked() else "square"
        rate = self._size_slider.value() / 100.0
        return self._checked_layers(), self._mode(), shape, rate

