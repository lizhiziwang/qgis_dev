"""矢量瓦片生成 Dialog。"""

from __future__ import annotations

from typing import Optional

from PyQt5.QtCore import Qt, pyqtSignal
from PyQt5.QtWidgets import (
    QComboBox,
    QFileDialog,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QProgressBar,
    QPushButton,
    QScrollArea,
    QSizePolicy,
    QSpinBox,
    QVBoxLayout,
    QWidget,
)

from qgis.core import Qgis, QgsApplication, QgsProject

from app.core.domain.tiles import TileJob, TileService
from app.ui.common.base_dialog import BaseDialog
from app.ui.common.widgets import open_file_dir


class VectorTileDialog(BaseDialog):
    """矢量瓦片生成 Dialog。"""

    def __init__(self, parent=None, title: str = "", dia_type: Optional[str] = None) -> None:
        super().__init__(parent, title)
        self.dia_type = dia_type
        if not self.dia_type:
            from app.infra.messagebar import send
            send("warning", "功能待开发", duration=3)
            self.close()
            return
        self.project = QgsProject.instance()
        self.select_layers: list = []
        self.out_extent = None
        self.task = None
        self.tile_service = TileService()
        self._init_ui()

    def _init_ui(self) -> None:
        layout = QVBoxLayout()

        param_panel = QWidget()
        self._init_parameters_panel(param_panel)
        layout.addWidget(param_panel)

        desc_group = QGroupBox("矢量瓦片(XYZ)生成")
        desc_label = QLabel(
            "该算法导出一个或多个矢量图层到矢量瓦片格式，"
            "为快速地图渲染和小数据量而优化的数据格式。"
        )
        desc_label.setWordWrap(True)
        desc_layout = QVBoxLayout(desc_group)
        desc_layout.addWidget(desc_label)
        desc_layout.addStretch()
        layout.addWidget(desc_group)

        # 进度 + 按钮
        b_l = QVBoxLayout()
        b_l.setContentsMargins(0, 0, 0, 0)
        self.loading = QProgressBar()
        self.loading.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        self.loading.setRange(0, 100)
        b_l.addWidget(self.loading, stretch=2)

        bottom = QHBoxLayout()
        bottom.addWidget(QPushButton("Advanced"))
        bottom.addWidget(QPushButton("Run as Batch Process..."))
        bottom.addStretch()
        bottom.addWidget(QPushButton("取消"))
        execute = QPushButton("执行")
        execute.clicked.connect(self._on_execute)
        bottom.addWidget(execute)
        close = QPushButton("关闭")
        close.clicked.connect(self.close)
        bottom.addWidget(close)
        b_l.addLayout(bottom)
        layout.addLayout(b_l)

        self.setLayout(layout)

    def _init_parameters_panel(self, parent) -> None:
        layout = QVBoxLayout(parent)
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        content = QWidget()
        sl = QVBoxLayout(content)

        # 1. 文件模版
        row = QVBoxLayout()
        row.setSpacing(8)
        row.addWidget(QLabel("文件模版"))
        h = QHBoxLayout()
        self.xyz_temp = QLineEdit("{z}/{x}/{y}.pbf")
        h.addWidget(self.xyz_temp)
        row.addLayout(h)
        sl.addLayout(row)
        sl.addSpacing(15)

        # 2. 输入图层
        row = QVBoxLayout()
        row.setSpacing(8)
        row.addWidget(QLabel("输入图层"))
        self.list_widget = QListWidget()
        self.list_widget.setFixedHeight(130)
        self.list_widget.itemClicked.connect(self._on_layer_clicked)
        for layer in self.project.mapLayers().values():
            item = QListWidgetItem(layer.name(), self.list_widget)
            item.setFlags(item.flags() | Qt.ItemIsUserCheckable)
            item.setCheckState(Qt.Unchecked)
        row.addWidget(self.list_widget)
        sl.addLayout(row)
        sl.addSpacing(15)

        # 3. 最小/最大等级
        for label, default, attr in (("最小等级", 0, "min_zoom_spin"), ("最大等级", 3, "max_zoom_spin")):
            row = QVBoxLayout()
            row.setSpacing(8)
            row.addWidget(QLabel(label))
            h = QHBoxLayout()
            spin = QSpinBox()
            spin.setRange(0, 20)
            spin.setValue(default)
            setattr(self, attr, spin)
            h.addWidget(spin)
            row.addLayout(h)
            sl.addLayout(row)
            sl.addSpacing(15)

        # 4. 输出范围
        row = QVBoxLayout()
        row.setSpacing(8)
        row.addWidget(QLabel("输出范围"))
        h = QHBoxLayout()
        self.extent_text = QLineEdit()
        self.extent_text.setReadOnly(True)
        h.addWidget(self.extent_text, stretch=8)
        self.extent_edit = QComboBox()
        self.extent_edit.addItems(["图层范围", "当前画布范围"])
        self.extent_edit.setCurrentIndex(-1)
        self.extent_edit.currentTextChanged.connect(self._on_extent_changed)
        h.addWidget(self.extent_edit, stretch=1)
        row.addLayout(h)
        sl.addLayout(row)
        sl.addSpacing(15)

        # 5. 输出目录
        row = QVBoxLayout()
        row.setSpacing(8)
        row.addWidget(QLabel("输出目录"))
        h = QHBoxLayout()
        self.output_edit = QLineEdit()
        self.output_edit.setPlaceholderText("保存为临时目录")
        self.output_edit.setReadOnly(True)
        h.addWidget(self.output_edit)
        sel = QPushButton("选择")
        sel.clicked.connect(lambda: self.output_edit.setText(open_file_dir(self, is_dir=True)))
        h.addWidget(sel)
        row.addLayout(h)
        sl.addLayout(row)

        sl.addStretch()
        scroll.setWidget(content)
        layout.addWidget(scroll)

    def _on_layer_clicked(self, item) -> None:
        if item.checkState() == Qt.Checked:
            self.select_layers.append(self.project.mapLayersByName(item.text())[0])
        else:
            self.select_layers = [l for l in self.select_layers if l.name() != item.text()]

    def _on_extent_changed(self, value: str) -> None:
        try:
            if value == "图层范围":
                combined = None
                for l in self.select_layers:
                    combined = l.extent() if combined is None else combined.combineExtentWith(l.extent())
                self.out_extent = combined
            elif value == "当前画布范围":
                if self.parent() and hasattr(self.parent(), "canvas"):
                    self.out_extent = self.parent().canvas.extent()
            if self.out_extent:
                self.extent_text.setText(self.out_extent.toString())
        except Exception:  # noqa: BLE001
            pass

    def _on_execute(self) -> None:
        try:
            self.loading.setValue(0)
            job = self._build_job()
            self.task = self.tile_service.submit(job)
            if self.task is not None:
                self.task.progressChanged.connect(self.loading.setValue)
                self.task.finished.connect(self._on_task_finished)
        except Exception as e:  # noqa: BLE001
            print(e)

    def _on_task_finished(self, ok: bool) -> None:
        if ok:
            self.loading.setValue(100)
        else:
            self.loading.setValue(0)

    def _build_job(self) -> TileJob:
        if self.out_extent is None:
            self.out_extent = self.parent().canvas.extent()
        authid = self.select_layers[0].crs().authid() if self.select_layers else "EPSG:4490"
        return TileJob(
            output_dir=self.output_edit.text() or "D:/tmp/xyz",
            xyz_template=self.xyz_temp.text(),
            layer_sources=[l.source() for l in self.select_layers],
            min_zoom=self.min_zoom_spin.value(),
            max_zoom=self.max_zoom_spin.value(),
            extent=(self.out_extent.xMinimum(), self.out_extent.xMaximum(),
                    self.out_extent.yMinimum(), self.out_extent.yMaximum(), authid),
        )
