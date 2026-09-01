"""矢量缓冲区生成 Dialog。"""

from __future__ import annotations

import os

from qgis.PyQt.QtCore import QTimer
from qgis.PyQt.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDoubleSpinBox,
    QFileDialog,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QProgressBar,
    QPushButton,
    QSizePolicy,
    QSpinBox,
    QVBoxLayout,
    QWidget,
)

from qgis.core import QgsMapLayer, QgsProject

from app.core.domain.buffer import BufferJob, BufferService
from app.infra.logging import get_logger
from app.infra.messagebar import send
from app.ui.common.algo_desc import make_description_browser
from app.ui.common.base_dialog import BaseDialog

logger = get_logger(__name__)


class BufferDialog(BaseDialog):
    """矢量缓冲区生成 Dialog（同步执行）。"""

    #: native:buffer 的样式枚举中文名，索引即算法取值（0 圆 / 1 平 / 2 方 等）
    _END_CAP_LABELS = ("圆", "平", "方")
    _JOIN_STYLE_LABELS = ("圆", "斜接", "平切")
    #: 距离单位下拉项：(中文名, QgsUnitTypes.DistanceUnit 值)
    _DISTANCE_UNITS = (
        ("米", 0),          # DistanceMeters
        ("千米", 1),        # DistanceKilometers
        ("英尺", 2),        # DistanceFeet
        ("海里", 3),        # DistanceNauticalMiles
        ("码", 4),          # DistanceYards
        ("英里", 5),        # DistanceMiles
        ("度", 6),          # DistanceDegrees
        ("厘米", 7),        # DistanceCentimeters
        ("毫米", 8),        # DistanceMillimeters
    )

    def __init__(self, parent=None, title: str = "") -> None:
        super().__init__(parent, title)
        # 左参数右描述布局需要更宽一点，比 BaseDialog 的 800x600 略收敛
        self.setMinimumSize(760, 520)
        self.project = QgsProject.instance()
        self.buffer_service = BufferService()
        self._init_ui()

    def _init_ui(self) -> None:
        layout = QVBoxLayout()

        # ---- 左右两部分：左参数 + 右算法描述 ----
        main = QHBoxLayout()

        param_panel = QWidget()
        self._init_parameters_panel(param_panel)
        main.addWidget(param_panel, stretch=3)

        # 右侧算法描述（与工具树共用同一套描述逻辑，见 algo_desc）
        main.addWidget(make_description_browser("native:buffer"), stretch=2)
        layout.addLayout(main, stretch=1)

        # 进度 + 按钮
        b_l = QVBoxLayout()
        b_l.setContentsMargins(0, 0, 0, 0)
        self.loading = QProgressBar()
        self.loading.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        self.loading.setRange(0, 100)
        # 重要：不要 connect feedback.progressChanged → setValue。实测这套环境里
        # progressChanged 无论直连还是 QueuedConnection 路由到控件槽都会原生崩溃
        # （0xC0000409，无 Python 异常）：直连崩在 processing.run 原生栈里，排队崩
        # 在投递时。进度条改为手动置 0 → 完成置 100。
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

        # 2. 缓冲距离（数值 + 单位）
        layout.addWidget(QLabel("缓冲距离"))
        dist_row = QHBoxLayout()
        self.distance_spin = QDoubleSpinBox()
        self.distance_spin.setDecimals(6)
        self.distance_spin.setRange(0.000001, 100_000_000.0)
        self.distance_spin.setValue(100.0)
        dist_row.addWidget(self.distance_spin, stretch=1)
        self.distance_unit_combo = QComboBox()
        for label, unit in self._DISTANCE_UNITS:
            self.distance_unit_combo.addItem(label, unit)
        dist_row.addWidget(self.distance_unit_combo)
        layout.addLayout(dist_row)
        layout.addSpacing(6)

        # 3. 分段数
        layout.addWidget(QLabel("圆弧分段数"))
        self.segments_spin = QSpinBox()
        self.segments_spin.setRange(1, 100)
        self.segments_spin.setValue(8)
        layout.addWidget(self.segments_spin)
        layout.addSpacing(6)

        # 4. 端点样式
        layout.addWidget(QLabel("端点样式"))
        self.end_cap_combo = QComboBox()
        self.end_cap_combo.addItems(self._END_CAP_LABELS)
        layout.addWidget(self.end_cap_combo)
        layout.addSpacing(6)

        # 5. 连接样式
        layout.addWidget(QLabel("连接样式"))
        self.join_style_combo = QComboBox()
        self.join_style_combo.addItems(self._JOIN_STYLE_LABELS)
        layout.addWidget(self.join_style_combo)
        layout.addSpacing(6)

        # 6. 融合结果
        self.dissolve_check = QCheckBox("融合重叠的缓冲区")
        layout.addWidget(self.dissolve_check)
        layout.addSpacing(6)

        # 7. 输出图层名
        layout.addWidget(QLabel("输出图层名"))
        self.output_edit = QLineEdit("缓冲区结果")
        layout.addWidget(self.output_edit)
        layout.addSpacing(6)

        # 8. 输出文件路径（留空 = 临时 memory 图层）
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

    def _on_execute(self) -> None:
        layer = self.input_combo.currentData()
        if layer is None:
            send("warning", "请先在工程中加载一个矢量图层", duration=3)
            return
        distance = self.distance_spin.value()
        if distance <= 0:
            send("warning", "缓冲距离必须大于 0", duration=3)
            return

        job = BufferJob(
            layer=layer,
            distance=distance,
            distance_unit=self.distance_unit_combo.currentData(),
            segments=self.segments_spin.value(),
            dissolve=self.dissolve_check.isChecked(),
            end_cap=self.end_cap_combo.currentIndex(),
            join_style=self.join_style_combo.currentIndex(),
            output_name=self.output_edit.text().strip() or "缓冲区结果",
            output_path=self.output_path_edit.text().strip() or None,
        )

        # 不能留在 clicked 信号栈里同步跑 processing.run + addMapLayer + refresh：
        # 仓库记录过的原生崩溃机制（见 main_window._remove_layer 注释）——Qt 信号
        # 回调里重入原生渲染 → 0xC0000409 进程崩溃且无 Python 异常可记日志。
        # 统一推迟到事件栈外（QTimer.singleShot），与仓库既定修法一致。
        self.execute_btn.setEnabled(False)
        self.loading.setValue(0)
        QTimer.singleShot(0, lambda: self._run(job))

    def _run(self, job: BufferJob) -> None:
        """事件栈外真正执行缓冲区（不在任何 Qt 信号回调栈里）。"""
        self.loading.setValue(0)
        try:
            # 不传 feedback：服务内部自建空 QgsProcessingFeedback，进度信号不接任何
            # 槽（接控件槽即原生崩溃，见 _init_ui 注释）
            result = self.buffer_service.run(job)
        except Exception as exc:  # noqa: BLE001
            logger.error("缓冲区生成失败: %s", exc, exc_info=True)
            self.loading.setValue(0)
            send("critical", f"缓冲区生成失败：{exc}", duration=5)
            return
        finally:
            self.execute_btn.setEnabled(True)

        # 结果加入工程（已出信号栈）；画布刷新再推迟一拍，等 layersAdded 信号链走完
        self.project.addMapLayer(result)
        self.loading.setValue(100)
        dest = f" → {job.output_path}" if job.output_path else ""
        send("success", f"缓冲区生成完成：{result.featureCount()} 个要素{dest}", duration=3)
        QTimer.singleShot(0, self._refresh_canvas)

    def _refresh_canvas(self) -> None:
        canvas = self.parent().canvas if self.parent() and hasattr(self.parent(), "canvas") else None
        if canvas is not None:
            canvas.refresh()
