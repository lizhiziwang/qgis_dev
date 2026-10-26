"""卷帘对比参数 Dialog。

选择 左（上）影像 / 右（下）影像 / 卷帘方向；确认后由
:class:`app.ui.tools.swipe.SwipeTool` 在地图画布上做双图卷帘。影像列表复用
imagery_tile 的填充模式（图层对象存 ``itemData``，``showEvent`` +
``project.layersAdded/Removed`` 时刷新）。
"""

from __future__ import annotations

from qgis.PyQt.QtWidgets import (
    QButtonGroup,
    QComboBox,
    QHBoxLayout,
    QLabel,
    QMessageBox,
    QPushButton,
    QRadioButton,
    QVBoxLayout,
)
from qgis.core import QgsMapLayer, QgsProject

from app.ui.common.base_dialog import BaseDialog


class SwipeCompareDialog(BaseDialog):
    """卷帘对比参数对话框（``exec()`` 模态）。"""

    def __init__(self, parent=None) -> None:
        super().__init__(parent, "卷帘对比")
        # BaseDialog 默认 800x600，本对话框内容少，收敛尺寸（super 之后设置生效）
        self.setMinimumSize(460, 300)
        self.project = QgsProject.instance()
        self._init_ui()
        self.project.layersAdded.connect(self._populate_layers)
        self.project.layersRemoved.connect(self._populate_layers)

    def _init_ui(self) -> None:
        # BaseDialog 约定：裸建 QVBoxLayout + self.setLayout(...)，不能用父构造布局
        layout = QVBoxLayout()
        layout.setContentsMargins(16, 16, 16, 16)
        layout.setSpacing(10)

        self.base_label = QLabel("左影像")
        layout.addWidget(self.base_label)
        self.base_combo = QComboBox()
        layout.addWidget(self.base_combo)

        self.compare_label = QLabel("右影像")
        layout.addWidget(self.compare_label)
        self.compare_combo = QComboBox()
        layout.addWidget(self.compare_combo)

        layout.addWidget(QLabel("卷帘方向"))
        dir_row = QHBoxLayout()
        self.vertical_radio = QRadioButton("垂直（左右）")
        self.vertical_radio.setChecked(True)
        self.horizontal_radio = QRadioButton("水平（上下）")
        group = QButtonGroup(self)
        group.addButton(self.vertical_radio)
        group.addButton(self.horizontal_radio)
        dir_row.addWidget(self.vertical_radio)
        dir_row.addWidget(self.horizontal_radio)
        dir_row.addStretch()
        layout.addLayout(dir_row)

        layout.addStretch()

        bottom = QHBoxLayout()
        bottom.addStretch()
        self.start_btn = QPushButton("开始")
        self.start_btn.setDefault(True)
        self.start_btn.clicked.connect(self._on_start)
        cancel_btn = QPushButton("取消")
        cancel_btn.clicked.connect(self.reject)
        bottom.addWidget(self.start_btn)
        bottom.addWidget(cancel_btn)
        layout.addLayout(bottom)

        self.setLayout(layout)
        self.vertical_radio.toggled.connect(self._update_orientation_labels)
        self._update_orientation_labels()
        self._populate_layers()

    def _update_orientation_labels(self) -> None:
        """方向标签联动：垂直→左/右影像，水平→上/下影像。"""
        if self.vertical_radio.isChecked():
            self.base_label.setText("左影像")
            self.compare_label.setText("右影像")
        else:
            self.base_label.setText("上影像")
            self.compare_label.setText("下影像")

    def _populate_layers(self, *_args) -> None:
        """按图层树上下顺序填充栅格图层（图层对象存 itemData），保留当前选中项。

        不遍历 ``project.mapLayers()``：它按图层 id 排序，下拉框顺序会像乱序；
        ``layerTreeRoot().findLayers()`` 返回图层面板里从上到下的顺序。
        """
        current = self.base_combo.currentData()
        current_src = current.source() if current is not None else None
        self.base_combo.clear()
        self.compare_combo.clear()
        for node in self.project.layerTreeRoot().findLayers():
            layer = node.layer()
            if layer is None or layer.type() != QgsMapLayer.RasterLayer:
                continue
            self.base_combo.addItem(layer.name(), layer)
            self.compare_combo.addItem(layer.name(), layer)
            if current_src is not None and layer.source() == current_src:
                self.base_combo.setCurrentIndex(self.base_combo.count() - 1)
        # 对比栅格默认选第二个（若有），否则与底层相同由 _on_start 拦下
        if self.compare_combo.count() > 1:
            self.compare_combo.setCurrentIndex(1)

    def showEvent(self, event) -> None:  # noqa: N802 —— Qt 命名
        """每次显示重刷图层（工程变化后也能选到新图层）。"""
        super().showEvent(event)
        self._populate_layers()

    def _on_start(self) -> None:
        base = self.base_combo.currentData()
        compare = self.compare_combo.currentData()
        if base is None or compare is None:
            QMessageBox.warning(self, "提示", "请先加载至少一个影像图层")
            return
        if base is compare:
            QMessageBox.warning(self, "提示", "左影像与右影像不能相同")
            return
        self.accept()

    def selected_params(self) -> tuple:
        """返回 (左影像, 右影像, 方向)；方向为 ``"vertical"`` / ``"horizontal"``。"""
        orientation = "vertical" if self.vertical_radio.isChecked() else "horizontal"
        return self.base_combo.currentData(), self.compare_combo.currentData(), orientation
