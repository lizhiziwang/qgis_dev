"""定向最小外包矩形 Dialog（``native:orientedminimumboundingbox``）。"""

from __future__ import annotations

import os

from qgis.core import QgsApplication, QgsMapLayer, QgsProject
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

from app.core.domain.oriented_bbox import OrientedBBoxJob, OrientedBBoxService
from app.infra.logging import get_logger
from app.infra.messagebar import send
from app.ui.common.algo_desc import make_description_panel
from app.ui.common.base_dialog import BaseDialog, make_scrollable

logger = get_logger(__name__)


class OrientedBBoxDialog(BaseDialog):
    """定向最小外包矩形生成 Dialog。

    :class:`QgsProcessingFeatureBasedAlgorithm` 按要素生成结果矩形，
    算法自动注入字段（``angle`` / ``area`` / ``perimeter`` / ``width`` /
    ``height``），故参数面板只需选择输入图层。

    算法在后台线程执行（``QgsProcessingAlgRunnerTask``），主线程事件循环不
    阻塞，进度经 ``progress_updated`` 信号更新进度条，可随时取消。
    """

    def __init__(self, parent=None, title: str = "") -> None:
        super().__init__(parent, title)
        self.setMinimumSize(760, 520)
        self.project = QgsProject.instance()
        self.bbox_service = OrientedBBoxService()
        #: 当前后台任务及其上下文 / 反馈（保留到下次执行，任务收尾仍会用到）
        self._task = None
        self._context = None
        self._feedback = None
        self._job: OrientedBBoxJob | None = None
        #: 最近一条 ERROR 级算法日志（任务失败时展示到 messagebar）
        self._last_error = ""
        self._init_ui()

    def _init_ui(self) -> None:
        layout = QVBoxLayout()

        main = QHBoxLayout()

        param_panel = QWidget()
        self._init_parameters_panel(param_panel)
        main.addWidget(make_scrollable(param_panel), stretch=3)

        main.addWidget(make_description_panel("native:orientedminimumboundingbox"), stretch=1)
        layout.addLayout(main, stretch=1)

        b_l = QVBoxLayout()
        b_l.setContentsMargins(0, 0, 0, 0)
        self.loading = QProgressBar()
        self.loading.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        self.loading.setRange(0, 100)
        # 进度来自后台任务的 progress_updated 信号。不要 connect
        # feedback.progressChanged → setValue（实测本环境会原生崩溃 0xC0000409，
        # 见 buffer.py 注释）。
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

        # 不在 clicked 信号栈里创建临时图层 / 启动任务（与 BufferDialog 同原因：
        # 仓库记录过的原生崩溃机制）—— 推迟到事件栈外。
        self.execute_btn.setEnabled(False)
        self.cancel_btn.setEnabled(False)
        self.loading.setValue(0)
        self._last_error = ""
        QTimer.singleShot(0, lambda: self._start_task(job))

    # ------------------------------------------------------ 后台任务

    def _start_task(self, job: OrientedBBoxJob) -> None:
        """事件栈外准备参数并启动后台任务（不在任何 Qt 信号回调栈里）。"""
        try:
            task, context, feedback = self.bbox_service.build_task(job)
        except Exception as exc:
            logger.error("最小外包矩形任务准备失败: %s", exc, exc_info=True)
            self.execute_btn.setEnabled(True)
            send("critical", f"最小外包矩形生成失败：{exc}", duration=5)
            return

        # context / feedback 必须持有到任务结束（task 内部是裸指针，被 GC 会悬垂）
        self._task = task
        self._context = context
        self._feedback = feedback
        self._job = job
        task.progress_updated.connect(self._on_task_progress)
        task.executed.connect(self._on_task_executed)
        task.taskTerminated.connect(self._on_task_terminated)
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
        """后台任务结束（``executed``，主线程回调）。"""
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
            send("warning", "已取消最小外包矩形生成", duration=3)
            return
        if not ok or bool(getattr(feedback, "failed", False)):
            self.loading.setValue(0)
            detail = f"：{error}" if error else ""
            logger.error("最小外包矩形生成失败%s", detail)
            send("critical", f"最小外包矩形生成失败{detail}", duration=5)
            return

        try:
            result = self.bbox_service.finish(job, context, results or {})
        except Exception as exc:
            logger.error("最小外包矩形结果装配失败: %s", exc, exc_info=True)
            self.loading.setValue(0)
            send("critical", f"最小外包矩形生成失败：{exc}", duration=5)
            return

        self.loading.setValue(100)
        self.project.addMapLayer(result)
        dest = f" → {job.output_path}" if job.output_path else ""
        send("success", f"最小外包矩形生成完成：{result.featureCount()} 个要素{dest}", duration=3)
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
            send("warning", "已取消最小外包矩形生成", duration=3)
            return
        detail = f"：{error}" if error else ""
        logger.error("最小外包矩形生成失败%s", detail)
        send("critical", f"最小外包矩形生成失败{detail}", duration=5)

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
