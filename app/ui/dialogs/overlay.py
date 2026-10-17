"""矢量叠加分析 Dialog。"""

from __future__ import annotations

from qgis.core import QgsApplication, QgsMapLayer, QgsProject
from qgis.PyQt.QtCore import QTimer
from qgis.PyQt.QtWidgets import (
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

from app.core.domain.overlay import OPERATION_NAMES, OverlayJob, OverlayRun, OverlayService
from app.infra.logging import get_logger
from app.infra.messagebar import send
from app.ui.common.algo_desc import make_description_browser
from app.ui.common.base_dialog import BaseDialog

logger = get_logger(__name__)


class OverlayDialog(BaseDialog):
    """矢量叠加分析 Dialog。

    窗口设计参考 QGIS 原始"叠加分析"工具：输入图层 + 叠加图层 + 勾选要执行的
    叠加运算。每个勾选的运算生成一个独立结果图层加入工程。

    勾选的运算**串行**在后台线程执行（每个运算一个
    ``QgsProcessingAlgRunnerTask``，与 QGIS processing 对话框同一机制）：
    主线程事件循环不阻塞，进度条按「已完成运算数 + 当前运算进度」折算，
    可随时取消。
    """

    def __init__(self, parent=None, title: str = "") -> None:
        super().__init__(parent, title)
        # 左参数右描述布局需要更宽一点，比 BaseDialog 的 800x600 略收敛（同 BufferDialog）
        self.setMinimumSize(760, 520)
        self.project = QgsProject.instance()
        self.overlay_service = OverlayService()
        #: 当前运算的执行计划（输入图层已清洗，整轮复用）与结果
        self._plan: OverlayRun | None = None
        self._op_index = -1
        self._op_total = 0
        self._op_results: dict = {}
        #: 当前后台任务及其上下文 / 反馈
        self._task = None
        self._context = None
        self._feedback = None
        #: 本轮所有任务的 context / feedback：任务收尾阶段仍会用裸指针，
        #: 必须活到整轮结束（不能每个运算开始时就把上一个 GC 掉）。
        self._keepalive: list = []
        #: 最近一条 ERROR 级算法日志（任务失败时展示到 messagebar）
        self._last_error = ""
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
        # 进度来自后台任务的 progress_updated 信号。不要 connect
        # feedback.progressChanged → setValue（原生崩溃，见 buffer._init_ui 注释）。
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

        # 不在 clicked 信号栈里创建临时图层 / 启动任务（仓库记录过的原生崩溃
        # 机制，见 buffer._on_execute 注释）；统一推迟到事件栈外。
        self.execute_btn.setEnabled(False)
        self.cancel_btn.setEnabled(False)
        self.loading.setValue(0)
        self._last_error = ""
        QTimer.singleShot(0, lambda: self._start_run(job))

    # ------------------------------------------------------ 后台任务

    def _start_run(self, job: OverlayJob) -> None:
        """事件栈外准备执行计划，并启动第一个运算的后台任务。"""
        try:
            plan = self.overlay_service.prepare(job)
        except Exception as exc:
            logger.error("叠加分析任务准备失败: %s", exc, exc_info=True)
            self._fail_run(f"叠加分析失败：{exc}")
            return
        self._plan = plan
        self._op_results = {}
        self._op_total = len(plan.operations)
        # 上一轮已结束，这里才释放它的 context / feedback
        self._keepalive = []
        self.loading.setValue(0)
        self._start_op(0)

    def _start_op(self, index: int) -> None:
        """为第 ``index`` 个运算构造并启动后台任务。"""
        plan = self._plan
        if plan is None:
            return
        op = plan.operations[index]
        self._op_index = index
        self._last_error = ""
        try:
            task, context, feedback = self.overlay_service.build_task(plan, op)
        except Exception as exc:
            logger.error("叠加分析任务准备失败: %s", exc, exc_info=True)
            self._fail_run(f"叠加分析失败：{exc}")
            return

        # context / feedback 必须持有到任务结束（task 内部是裸指针，被 GC 会悬垂）
        self._task = task
        self._context = context
        self._feedback = feedback
        self._keepalive.append((context, feedback))
        task.progress_updated.connect(self._on_task_progress)
        # 用任务身份做守卫：上一个任务收尾时发出的 terminated 不能误伤下一个任务
        task.executed.connect(
            lambda ok, results, t=task: self._on_task_executed(t, ok, results)
        )
        task.taskTerminated.connect(lambda t=task: self._on_task_terminated(t))
        feedback.log_line.connect(self._on_alg_log)
        self.cancel_btn.setEnabled(True)
        self.cancel_btn.setText("取消")
        QgsApplication.taskManager().addTask(task)

    def _on_task_progress(self, value: int) -> None:
        """把当前运算的进度折算成整轮进度（已完成的运算 + 当前运算）。"""
        if self._op_total <= 0 or self._op_index < 0:
            return
        fraction = max(0, min(100, int(value))) / 100.0
        self.loading.setValue(int((self._op_index + fraction) / self._op_total * 100))

    def _on_alg_log(self, level: str, message: str) -> None:
        """worker 线程算法日志跨线程投递到这里；ERROR 留作失败提示。"""
        if level == "ERROR":
            self._last_error = message
        if level in ("ERROR", "WARNING"):
            logger.warning("[%s] %s", level, message)
        else:
            logger.debug("[%s] %s", level, message)

    def _on_task_executed(self, task, ok: bool, results: dict) -> None:
        """某个运算结束（``executed``，主线程回调）：收结果并启动下一个。"""
        if self._task is not task:
            return  # 已由 _on_task_terminated 处理（取消 / 异常终止）
        plan = self._plan
        context = self._context
        feedback = self._feedback
        error = self._last_error
        index = self._op_index
        self._task = None
        if plan is None:
            return

        if feedback is not None and feedback.isCanceled():
            self._cancel_run()
            return
        if not ok or bool(getattr(feedback, "failed", False)):
            detail = f"：{error}" if error else ""
            logger.error("叠加分析失败%s", detail)
            self._fail_run(f"叠加分析失败{detail}")
            return

        op = plan.operations[index]
        try:
            layer = self.overlay_service.finish_op(plan, op, context, results or {})
        except Exception as exc:
            logger.error("叠加分析结果装配失败: %s", exc, exc_info=True)
            self._fail_run(f"叠加分析失败：{exc}")
            return
        self._op_results[op] = layer

        next_index = index + 1
        if next_index < self._op_total:
            self._start_op(next_index)
            return
        self._finish_run()

    def _on_task_terminated(self, task) -> None:
        """某个运算异常终止 / 取消（``executed`` 未送达时的兜底）。"""
        if self._task is not task:
            return  # 已由 _on_task_executed 处理，或不是当前任务
        feedback = self._feedback
        error = self._last_error
        self._task = None
        if feedback is not None and feedback.isCanceled():
            self._cancel_run()
            return
        detail = f"：{error}" if error else ""
        logger.error("叠加分析失败%s", detail)
        self._fail_run(f"叠加分析失败{detail}")

    def _finish_run(self) -> None:
        """整轮成功：结果加入工程并提示。"""
        plan = self._plan
        self.loading.setValue(100)
        prefix = plan.job.output_prefix if plan is not None else "叠加结果"
        for op, layer in self._op_results.items():
            layer.setName(f"{prefix}_{OPERATION_NAMES[op]}")
            self.project.addMapLayer(layer)
        summary = "、".join(
            f"{OPERATION_NAMES[op]}({layer.featureCount()}要素)"
            for op, layer in self._op_results.items()
        )
        dest = f" → {plan.job.output_dir}" if plan is not None and plan.job.output_dir else ""
        send("success", f"叠加分析完成：{summary}{dest}", duration=3)
        self._end_run()
        QTimer.singleShot(0, self._refresh_canvas)

    def _cancel_run(self) -> None:
        """用户取消：复位 UI 并提示。"""
        self.loading.setValue(0)
        send("warning", "已取消叠加分析", duration=3)
        self._end_run()

    def _fail_run(self, message: str) -> None:
        """整轮失败：复位 UI 并弹 critical。"""
        self.loading.setValue(0)
        send("critical", message, duration=5)
        self._end_run()

    def _end_run(self) -> None:
        """复位整轮执行状态（保留 messagebar 提示由调用方发出）。"""
        self._task = None
        self._plan = None
        self._op_index = -1
        self._op_total = 0
        self._op_results = {}
        self._last_error = ""
        self.execute_btn.setEnabled(True)
        self.cancel_btn.setEnabled(False)
        self.cancel_btn.setText("取消")

    def _on_cancel(self) -> None:
        """请求取消当前运算；后续运算不再启动。"""
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
