"""矢量重投影 Dialog。"""

from __future__ import annotations

import os

from PyQt6.QtCore import QTimer
from PyQt6.QtWidgets import (
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

from qgis.core import QgsCoordinateReferenceSystem, QgsMapLayer, QgsProject
from qgis.gui import QgsProjectionSelectionWidget

from app.core.domain.reproject import ReprojectJob, ReprojectService
from app.infra.logging import get_logger
from app.infra.messagebar import send
from app.ui.common.algo_desc import make_description_browser
from app.ui.common.base_dialog import BaseDialog

logger = get_logger(__name__)


class ReprojectDialog(BaseDialog):
    """矢量重投影 Dialog（同步执行）。

    窗口设计参考 QGIS 原始"重投影图层"工具：输入图层 + 目标坐标系 + 输出。
    结果自动加入工程。
    """

    def __init__(self, parent=None, title: str = "") -> None:
        super().__init__(parent, title)
        # 左参数右描述布局需要更宽一点，比 BaseDialog 的 800x600 略收敛（同 BufferDialog）
        self.setMinimumSize(760, 520)
        self.project = QgsProject.instance()
        self.reproject_service = ReprojectService()
        self._init_ui()
        # 工程图层增删时刷新输入下拉：先开窗口后加图层（含窗口复用场景）也能立即看到
        self.project.layersAdded.connect(self._refresh_input_layers)
        self.project.layersRemoved.connect(self._refresh_input_layers)

    def _init_ui(self) -> None:
        layout = QVBoxLayout()

        # ---- 左右两部分：左参数 + 右算法描述 ----
        main = QHBoxLayout()

        param_panel = QWidget()
        self._init_parameters_panel(param_panel)
        main.addWidget(param_panel, stretch=3)

        # 右侧算法描述（processing 注册表取真实 help，不可用时走 algo_desc 兜底）
        main.addWidget(make_description_browser("native:reprojectlayer"), stretch=2)
        layout.addLayout(main, stretch=1)

        # 进度 + 按钮
        b_l = QVBoxLayout()
        b_l.setContentsMargins(0, 0, 0, 0)
        self.loading = QProgressBar()
        self.loading.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        self.loading.setRange(0, 100)
        # 重要：不要 connect feedback.progressChanged → setValue（原生崩溃，
        # 见 buffer._init_ui 注释）。进度条改为手动置 0 → 完成置 100。
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

        # 1. 输入图层（单选，仅矢量图层）
        layout.addWidget(QLabel("输入图层"))
        self.input_combo = QComboBox()
        self._populate_layers()
        layout.addWidget(self.input_combo)
        layout.addSpacing(6)

        # 2. 目标坐标系（与 QGIS 原生重投影对话框一致用 CRS 选择控件）
        layout.addWidget(QLabel("目标坐标系"))
        self.crs_widget = QgsProjectionSelectionWidget()
        default_crs = QgsCoordinateReferenceSystem("EPSG:4326")
        project_crs = self.project.crs()
        if project_crs.isValid():
            default_crs = project_crs
        self.crs_widget.setCrs(default_crs)
        layout.addWidget(self.crs_widget)
        layout.addSpacing(6)

        # 3. 输出图层名
        layout.addWidget(QLabel("输出图层名"))
        self.output_edit = QLineEdit("重投影结果")
        layout.addWidget(self.output_edit)
        layout.addSpacing(6)

        # 4. 输出文件路径（留空 = 临时 memory 图层）
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
        """文件选择器；无扩展名时按选中过滤器补 .gpkg / .shp。"""
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

    def _refresh_input_layers(self, *args) -> None:
        """工程图层变化后刷新输入下拉，尽量保持当前选择。"""
        current = self.input_combo.currentData()
        self._populate_layers()
        if current is not None:
            idx = self.input_combo.findData(current)
            if idx >= 0:
                self.input_combo.setCurrentIndex(idx)

    def _on_execute(self) -> None:
        layer = self.input_combo.currentData()
        if layer is None:
            send("warning", "请先在工程中加载一个矢量图层", duration=3)
            return
        target_crs = self.crs_widget.crs()
        if not target_crs.isValid():
            send("warning", "请选择有效的目标坐标系", duration=3)
            return

        job = ReprojectJob(
            input_layer=layer,
            target_crs=target_crs,
            output_name=self.output_edit.text().strip() or "重投影结果",
            output_path=self.output_path_edit.text().strip() or None,
        )

        # 不能留在 clicked 信号栈里同步跑 processing.run + addMapLayer + refresh：
        # 仓库记录过的原生崩溃机制（见 buffer._on_execute 注释）——Qt 信号回调里
        # 重入原生渲染 → 0xC0000409 进程崩溃且无 Python 异常可记日志。
        self.execute_btn.setEnabled(False)
        self.loading.setValue(0)
        QTimer.singleShot(0, lambda: self._run(job))

    def _run(self, job: ReprojectJob) -> None:
        """事件栈外真正执行重投影（不在任何 Qt 信号回调栈里）。"""
        self.loading.setValue(0)
        try:
            # 不传 feedback：服务内部自建空 QgsProcessingFeedback，进度信号不接任何槽
            result = self.reproject_service.run(job)
        except Exception as exc:  # noqa: BLE001
            logger.error("重投影失败: %s", exc, exc_info=True)
            self.loading.setValue(0)
            send("critical", f"重投影失败：{exc}", duration=5)
            return
        finally:
            self.execute_btn.setEnabled(True)

        # 结果加入工程（已出信号栈）；画布刷新再推迟一拍，等 layersAdded 信号链走完
        self.project.addMapLayer(result)
        self.loading.setValue(100)
        dest = f" → {job.output_path}" if job.output_path else ""
        crs_txt = job.target_crs.authid() or job.target_crs.description()
        send("success", f"重投影完成：{result.featureCount()} 个要素 → {crs_txt}{dest}", duration=3)
        QTimer.singleShot(0, self._refresh_canvas)

    def _refresh_canvas(self) -> None:
        canvas = self.parent().canvas if self.parent() and hasattr(self.parent(), "canvas") else None
        if canvas is not None:
            canvas.refresh()
