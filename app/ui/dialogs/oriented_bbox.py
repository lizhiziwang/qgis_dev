"""定向最小外包矩形 Dialog（``native:orientedminimumboundingbox``）。"""

from __future__ import annotations

import os

from qgis.PyQt.QtCore import QTimer
from qgis.PyQt.QtWidgets import (
    QComboBox,
    QFileDialog,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QProgressBar,
    QPushButton,
    QSizePolicy,
    QVBoxLayout,
    QWidget,
)

from qgis.core import QgsMapLayer, QgsProject

from app.core.domain.oriented_bbox import OrientedBBoxJob, OrientedBBoxService
from app.infra.logging import get_logger
from app.infra.messagebar import send
from app.ui.common.algo_desc import make_description_browser
from app.ui.common.base_dialog import BaseDialog

logger = get_logger(__name__)


class OrientedBBoxDialog(BaseDialog):
    """定向最小外包矩形生成 Dialog（同步执行）。

    :class:`QgsProcessingFeatureBasedAlgorithm` 按要素生成结果矩形，
    算法自动注入字段（``angle`` / ``area`` / ``perimeter`` / ``width`` /
    ``height``），故参数面板只需选择输入图层。
    """

    def __init__(self, parent=None, title: str = "") -> None:
        super().__init__(parent, title)
        self.setMinimumSize(760, 520)
        self.project = QgsProject.instance()
        self.bbox_service = OrientedBBoxService()
        self._init_ui()

    def _init_ui(self) -> None:
        layout = QVBoxLayout()

        main = QHBoxLayout()

        param_panel = QWidget()
        self._init_parameters_panel(param_panel)
        main.addWidget(param_panel, stretch=3)

        main.addWidget(make_description_browser("native:orientedminimumboundingbox"), stretch=2)
        layout.addLayout(main, stretch=1)

        b_l = QVBoxLayout()
        b_l.setContentsMargins(0, 0, 0, 0)
        self.loading = QProgressBar()
        self.loading.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        self.loading.setRange(0, 100)
        # 与 BufferDialog 一致：不接 progressChanged → setValue（实测本环境
        # 会原生崩溃 0xC0000409，见 buffer.py:83-86 注释）；改为手动 0 → 100。
        b_l.addWidget(self.loading, stretch=2)

        bottom = QHBoxLayout()
        bottom.addStretch()
        self.execute_btn = QPushButton("执行")
        self.execute_btn.clicked.connect(self._on_execute)
        bottom.addWidget(self.execute_btn)
        close = QPushButton("关闭")
        close.clicked.connect(self.close)
        bottom.addWidget(close)
        b_l.addLayout(bottom)
        layout.addLayout(b_l)

        self.setLayout(layout)

    def _init_parameters_panel(self, parent) -> None:
        layout = QVBoxLayout(parent)
        layout.setSpacing(12)

        layout.addWidget(QLabel("输入图层"))
        self.input_combo = QComboBox()
        self._populate_layers()
        layout.addWidget(self.input_combo)
        layout.addSpacing(6)

        layout.addWidget(QLabel("输出图层名"))
        self.output_edit = QLineEdit("最小外包矩形结果")
        layout.addWidget(self.output_edit)
        layout.addSpacing(6)

        layout.addWidget(QLabel("输出文件路径（留空为临时图层）"))
        path_row = QHBoxLayout()
        self.output_path_edit = QLineEdit()
        self.output_path_edit.setPlaceholderText("留空则生成临时缓存图层")
        path_row.addWidget(self.output_path_edit, stretch=1)
        browse_btn = QPushButton("浏览…")
        browse_btn.clicked.connect(self._browse_output_path)
        path_row.addWidget(browse_btn)
        layout.addLayout(path_row)

        layout.addStretch()

    def _browse_output_path(self) -> None:
        """文件选择器；无扩展名时按选中过滤器补 .gpkg。"""
        path, _ = QFileDialog.getSaveFileName(
            self,
            "选择输出文件",
            self.output_path_edit.text(),
            "GeoPackage (*.gpkg);;Shapefile (*.shp)",
        )
        if path and not os.path.splitext(path)[1]:
            path += ".gpkg"
        if path:
            self.output_path_edit.setText(path)

    def _populate_layers(self) -> None:
        """把工程里的矢量图层填充进下拉框（图层对象存 itemData）。"""
        self.input_combo.clear()
        for layer in self.project.mapLayers().values():
            if layer.type() == QgsMapLayer.VectorLayer:
                self.input_combo.addItem(layer.name(), layer)

    def _on_execute(self) -> None:
        layer = self.input_combo.currentData()
        if layer is None:
            send("warning", "请先在工程中加载一个矢量图层", duration=3)
            return

        job = OrientedBBoxJob(
            layer=layer,
            output_name=self.output_edit.text().strip() or "最小外包矩形结果",
            output_path=self.output_path_edit.text().strip() or None,
        )

        # 不能留在 clicked 信号栈里同步跑 processing.run + addMapLayer +
        # refresh：与 BufferDialog 同原因（Qt 回调内重入原生渲染 → 原生
        # 崩溃 0xC0000409，无 Python 异常可记日志）—— 推迟到事件栈外。
        self.execute_btn.setEnabled(False)
        self.loading.setValue(0)
        QTimer.singleShot(0, lambda: self._run(job))

    def _run(self, job: OrientedBBoxJob) -> None:
        """事件栈外真正执行最小外包矩形（不在任何 Qt 信号回调栈里）。"""
        self.loading.setValue(0)
        try:
            result = self.bbox_service.run(job)
        except Exception as exc:  # noqa: BLE001
            logger.error("最小外包矩形生成失败: %s", exc, exc_info=True)
            self.loading.setValue(0)
            send("critical", f"最小外包矩形生成失败：{exc}", duration=5)
            return
        finally:
            self.execute_btn.setEnabled(True)

        self.project.addMapLayer(result)
        self.loading.setValue(100)
        dest = f" → {job.output_path}" if job.output_path else ""
        send("success", f"最小外包矩形生成完成：{result.featureCount()} 个要素{dest}", duration=3)
        QTimer.singleShot(0, self._refresh_canvas)

    def _refresh_canvas(self) -> None:
        canvas = self.parent().canvas if self.parent() and hasattr(self.parent(), "canvas") else None
        if canvas is not None:
            canvas.refresh()