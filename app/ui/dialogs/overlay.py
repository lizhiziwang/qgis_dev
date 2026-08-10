"""矢量叠加分析 Dialog。"""

from __future__ import annotations

from PyQt6.QtCore import QTimer
from PyQt6.QtWidgets import (
    QCheckBox,
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

from app.core.domain.overlay import OPERATION_NAMES, OverlayJob, OverlayService
from app.infra.logging import get_logger
from app.infra.messagebar import send
from app.ui.common.algo_desc import make_description_browser
from app.ui.common.base_dialog import BaseDialog

logger = get_logger(__name__)


class OverlayDialog(BaseDialog):
    """矢量叠加分析 Dialog（同步执行）。

    窗口设计参考 QGIS 原始"叠加分析"工具：输入图层 + 叠加图层 + 勾选要执行的
    叠加运算。每个勾选的运算生成一个独立结果图层加入工程。
    """

    def __init__(self, parent=None, title: str = "") -> None:
        super().__init__(parent, title)
        # 左参数右描述布局需要更宽一点，比 BaseDialog 的 800x600 略收敛（同 BufferDialog）
        self.setMinimumSize(760, 520)
        self.project = QgsProject.instance()
        self.overlay_service = OverlayService()
        self._init_ui()

    def _init_ui(self) -> None:
        layout = QVBoxLayout()

        # ---- 左右两部分：左参数 + 右算法描述 ----
        main = QHBoxLayout()

        param_panel = QWidget()
        self._init_parameters_panel(param_panel)
        main.addWidget(param_panel, stretch=3)

        # 右侧算法描述（overlay 无单一算法 id，走 algo_desc 的兜底文案）
        main.addWidget(make_description_browser("overlay:analysis"), stretch=2)
        layout.addLayout(main, stretch=1)

        # 进度 + 按钮
        b_l = QVBoxLayout()
        b_l.setContentsMargins(0, 0, 0, 0)
        self.loading = QProgressBar()
        self.loading.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        self.loading.setRange(0, 100)
        # 重要：不要 connect feedback.progressChanged → setValue。实测这套环境里
        # progressChanged 无论直连还是 QueuedConnection 路由到控件槽都会原生崩溃
        # （0xC0000409，无 Python 异常）。进度条改为手动置 0 → 完成置 100。
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
        self._populate_layers(self.input_combo)
        layout.addWidget(self.input_combo)
        layout.addSpacing(6)

        # 2. 叠加图层
        layout.addWidget(QLabel("叠加图层"))
        self.overlay_combo = QComboBox()
        self._populate_layers(self.overlay_combo)
        layout.addWidget(self.overlay_combo)
        layout.addSpacing(6)

        # 3. 叠加运算（QGIS 原始"叠加分析"的勾选式；本版无 identity 算法故不提供"标识"）
        layout.addWidget(QLabel("叠加运算"))
        self.op_checks: dict = {}
        for key, label in OPERATION_NAMES.items():
            check = QCheckBox(label)
            check.setChecked(True)
            layout.addWidget(check)
            self.op_checks[key] = check
        layout.addSpacing(6)

        # 4. 输出图层名前缀
        layout.addWidget(QLabel("输出图层名前缀"))
        self.output_edit = QLineEdit("叠加结果")
        layout.addWidget(self.output_edit)
        layout.addSpacing(6)

        # 5. 输出目录（留空 = 临时 memory 图层；给出时每个运算写一个 gpkg 文件）
        layout.addWidget(QLabel("输出目录（留空为临时图层）"))
        dir_row = QHBoxLayout()
        self.output_dir_edit = QLineEdit()
        self.output_dir_edit.setPlaceholderText("留空则生成临时缓存图层")
        dir_row.addWidget(self.output_dir_edit, stretch=1)
        browse_btn = QPushButton("浏览…")
        browse_btn.clicked.connect(self._browse_output_dir)
        dir_row.addWidget(browse_btn)
        layout.addLayout(dir_row)

        layout.addStretch()

    def _browse_output_dir(self) -> None:
        from PyQt6.QtWidgets import QFileDialog

        path = QFileDialog.getExistingDirectory(
            self, "选择输出目录", self.output_dir_edit.text()
        )
        if path:
            self.output_dir_edit.setText(path)

    def _populate_layers(self, combo: QComboBox) -> None:
        """把工程里的矢量图层填充进下拉框（图层对象存 itemData）。"""
        combo.clear()
        for layer in self.project.mapLayers().values():
            if layer.type() == QgsMapLayer.VectorLayer:
                combo.addItem(layer.name(), layer)

    def _on_execute(self) -> None:
        input_layer = self.input_combo.currentData()
        overlay_layer = self.overlay_combo.currentData()
        if input_layer is None or overlay_layer is None:
            send("warning", "请先在工程中加载矢量图层", duration=3)
            return
        if input_layer is overlay_layer:
            send("warning", "输入图层与叠加图层不能相同", duration=3)
            return
        operations = tuple(k for k, check in self.op_checks.items() if check.isChecked())
        if not operations:
            send("warning", "请至少勾选一种叠加运算", duration=3)
            return

        job = OverlayJob(
            input_layer=input_layer,
            overlay_layer=overlay_layer,
            operations=operations,
            output_prefix=self.output_edit.text().strip() or "叠加结果",
            output_dir=self.output_dir_edit.text().strip() or None,
        )

        # 不能留在 clicked 信号栈里同步跑 processing.run + addMapLayer + refresh：
        # 仓库记录过的原生崩溃机制（见 buffer._on_execute 注释）——Qt 信号回调里
        # 重入原生渲染 → 0xC0000409 进程崩溃且无 Python 异常可记日志。
        self.execute_btn.setEnabled(False)
        self.loading.setValue(0)
        QTimer.singleShot(0, lambda: self._run(job))

    def _run(self, job: OverlayJob) -> None:
        """事件栈外真正执行叠加分析（不在任何 Qt 信号回调栈里）。"""
        self.loading.setValue(0)
        try:
            # 不传 feedback：服务内部自建空 QgsProcessingFeedback，进度信号不接任何槽
            results = self.overlay_service.run(job)
        except Exception as exc:  # noqa: BLE001
            logger.error("叠加分析失败: %s", exc, exc_info=True)
            self.loading.setValue(0)
            send("critical", f"叠加分析失败：{exc}", duration=5)
            return
        finally:
            self.execute_btn.setEnabled(True)

        # 结果加入工程（已出信号栈）；画布刷新再推迟一拍，等 layersAdded 信号链走完
        for op, layer in results.items():
            layer.setName(f"{job.output_prefix}_{OPERATION_NAMES[op]}")
            self.project.addMapLayer(layer)
        self.loading.setValue(100)
        summary = "、".join(
            f"{OPERATION_NAMES[op]}({layer.featureCount()}要素)"
            for op, layer in results.items()
        )
        dest = f" → {job.output_dir}" if job.output_dir else ""
        send("success", f"叠加分析完成：{summary}{dest}", duration=3)
        QTimer.singleShot(0, self._refresh_canvas)

    def _refresh_canvas(self) -> None:
        canvas = self.parent().canvas if self.parent() and hasattr(self.parent(), "canvas") else None
        if canvas is not None:
            canvas.refresh()
