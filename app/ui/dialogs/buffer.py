"""矢量缓冲区生成 Dialog。"""

from __future__ import annotations

import os

from qgis.core import QgsApplication, QgsMapLayer, QgsProject
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

from app.core.domain.buffer import BufferJob, BufferService
from app.infra.logging import get_logger
from app.infra.messagebar import send
from app.ui.common.algo_desc import make_description_panel
from app.ui.common.base_dialog import BaseDialog, make_scrollable

logger = get_logger(__name__)


class BufferDialog(BaseDialog):
    """矢量缓冲区生成 Dialog。

    算法在后台线程执行（``QgsProcessingAlgRunnerTask``，与 QGIS processing
    对话框同一机制）：主线程事件循环不阻塞，进度经 ``progress_updated`` 信号
    回主线程更新进度条；可随时取消。
    """

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
        #: 当前后台任务及其上下文 / 反馈。任务结束后 ``_task`` 立即置 None；
        #: context / feedback 继续保留（QgsProcessingAlgRunnerTask 内部保存的是
        #: 裸指针，任务收尾阶段仍会使用），下次执行时被新实例覆盖。
        self._task = None
        self._context = None
        self._feedback = None
        self._job: BufferJob | None = None
        #: 最近一条 ERROR 级算法日志（任务失败时展示到 messagebar）
        self._last_error = ""
        self._init_ui()

    def _init_ui(self) -> None:
        layout = QVBoxLayout()

        # ---- 左右两部分：左参数 + 右算法描述 ----
        main = QHBoxLayout()

        param_panel = QWidget()
        self._init_parameters_panel(param_panel)
        main.addWidget(make_scrollable(param_panel), stretch=3)

        # 右侧算法描述（与工具树共用同一套描述逻辑，见 algo_desc）
        main.addWidget(make_description_panel("native:buffer"), stretch=1)
        layout.addLayout(main, stretch=1)

        # 进度 + 按钮
        b_l = QVBoxLayout()
        b_l.setContentsMargins(0, 0, 0, 0)
        self.loading = QProgressBar()
        self.loading.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        self.loading.setRange(0, 100)
        # 进度来自后台任务的 progress_updated 信号（普通 Python 方法转信号）。
        # 重要：不要 connect feedback.progressChanged → setValue——实测这套环境里
        # progressChanged 无论直连还是 QueuedConnection 路由到控件槽都会原生崩溃
        # （0xC0000409，无 Python 异常），故统一走 AlgRunnerTask 的 int 信号。
        b_l.addWidget(self.loading, stretch=2)

        bottom = QHBoxLayout()
        bottom.addStretch()
        self.execute_btn = QPushButton("执行")
        self.execute_btn.clicked.connect(self._on_execute)
        bottom.addWidget(self.execute_btn)
        self.cancel_btn = QPushButton("取消")
        self.cancel_btn.setEnabled(False)
        self.cancel_btn.clicked.connect(self._on_cancel)
        bottom.addWidget(self.cancel_btn)
        close = QPushButton("关闭")
        close.clicked.connect(self.close)
        bottom.addWidget(close)
        b_l.addLayout(bottom)
        layout.addLayout(b_l)

        self.setLayout(layout)
        self.fit_height_to_content(param_panel)

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

        # 不在 clicked 信号栈里创建临时图层 / 启动任务（仓库记录过的原生崩溃
        # 机制，见 main_window._remove_layer 注释）；统一推迟到事件栈外。
        self.execute_btn.setEnabled(False)
        self.cancel_btn.setEnabled(False)
        self.loading.setValue(0)
        self._last_error = ""
        QTimer.singleShot(0, lambda: self._start_task(job))

    # ------------------------------------------------------ 后台任务

    def _start_task(self, job: BufferJob) -> None:
        """事件栈外准备参数并启动后台任务（不在任何 Qt 信号回调栈里）。"""
        try:
            task, context, feedback = self.buffer_service.build_task(job)
        except Exception as exc:
            logger.error("缓冲区任务准备失败: %s", exc, exc_info=True)
            self.execute_btn.setEnabled(True)
            send("critical", f"缓冲区生成失败：{exc}", duration=5)
            return

        # context / feedback 必须持有到任务结束（task 内部是裸指针，被 GC 会悬垂）
        self._task = task
        self._context = context
        self._feedback = feedback
        self._job = job
        task.progress_updated.connect(self._on_task_progress)
        # executed(successful, results) 在主线程回调里装配结果
        task.executed.connect(self._on_task_executed)
        # 取消 / 异常终止时 executed 可能不发，用 taskTerminated 兜底复位按钮
        task.taskTerminated.connect(self._on_task_terminated)
        # 算法日志（worker 线程发出）跨线程投递到普通 Python 方法（安全）
        feedback.log_line.connect(self._on_alg_log)
        self.cancel_btn.setEnabled(True)
        self.cancel_btn.setText("取消")
        QgsApplication.taskManager().addTask(task)

    def _on_task_progress(self, value: int) -> None:
        """worker 线程进度跨线程投递到这里（普通 Python 方法，投递安全）。"""
        self.loading.setValue(int(value))

    def _on_alg_log(self, level: str, message: str) -> None:
        """worker 线程算法日志跨线程投递到这里；ERROR 留作失败提示。"""
        if level == "ERROR":
            self._last_error = message
        if level in ("ERROR", "WARNING"):
            logger.warning("[%s] %s", level, message)
        else:
            logger.debug("[%s] %s", level, message)

    def _on_task_executed(self, ok: bool, results: dict) -> None:
        """后台任务结束（``executed``，主线程回调）。

        先快照再复位：任务随时可能被 ``QgsTaskManager`` 删除，不能再访问任务
        成员；context / feedback 继续保留到下次执行（任务收尾阶段仍会用到）。
        """
        if self._task is None:
            return  # 已由 _on_task_terminated 处理（取消 / 异常终止）
        job = self._job
        context = self._context
        feedback = self._feedback
        error = self._last_error
        self._reset_task_ui()
        if job is None:
            return

        if feedback is not None and feedback.isCanceled():
            self.loading.setValue(0)
            send("warning", "已取消缓冲区生成", duration=3)
            return
        if not ok or bool(getattr(feedback, "failed", False)):
            self.loading.setValue(0)
            detail = f"：{error}" if error else ""
            logger.error("缓冲区生成失败%s", detail)
            send("critical", f"缓冲区生成失败{detail}", duration=5)
            return

        try:
            result = self.buffer_service.finish(job, context, results or {})
        except Exception as exc:
            logger.error("缓冲区结果装配失败: %s", exc, exc_info=True)
            self.loading.setValue(0)
            send("critical", f"缓冲区生成失败：{exc}", duration=5)
            return

        self.loading.setValue(100)
        # 结果加入工程；画布刷新再推迟一拍，等 layersAdded 信号链走完
        self.project.addMapLayer(result)
        dest = f" → {job.output_path}" if job.output_path else ""
        send("success", f"缓冲区生成完成：{result.featureCount()} 个要素{dest}", duration=3)
        QTimer.singleShot(0, self._refresh_canvas)

    def _on_task_terminated(self) -> None:
        """任务异常终止 / 取消（``executed`` 未送达时的兜底）。"""
        if self._task is None:
            return  # 已由 _on_task_executed 处理
        feedback = self._feedback
        error = self._last_error
        self._reset_task_ui()
        self.loading.setValue(0)
        if feedback is not None and feedback.isCanceled():
            send("warning", "已取消缓冲区生成", duration=3)
            return
        detail = f"：{error}" if error else ""
        logger.error("缓冲区生成失败%s", detail)
        send("critical", f"缓冲区生成失败{detail}", duration=5)

    def _reset_task_ui(self) -> None:
        """复位「执行中」状态（按钮 / 任务引用），保留 context / feedback。"""
        self._task = None
        self._job = None
        self._last_error = ""
        self.execute_btn.setEnabled(True)
        self.cancel_btn.setEnabled(False)
        self.cancel_btn.setText("取消")

    def _on_cancel(self) -> None:
        """请求取消后台任务（转发到 ``feedback.cancel()``，算法尽早停止）。"""
        task = self._task
        if task is None:
            return
        try:
            task.cancel()
        except RuntimeError:
            # 任务已完成，C++ 对象已被 QgsTaskManager 删除
            self._task = None
            return
        self.cancel_btn.setEnabled(False)
        self.cancel_btn.setText("取消中…")

    def closeEvent(self, event) -> None:
        """关闭对话框时取消在跑的后台任务（实例被工具树复用）。"""
        self._on_cancel()
        super().closeEvent(event)

    def _refresh_canvas(self) -> None:
        canvas = self.parent().canvas if self.parent() and hasattr(self.parent(), "canvas") else None
        if canvas is not None:
            canvas.refresh()
