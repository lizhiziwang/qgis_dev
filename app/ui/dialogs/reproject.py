"""矢量重投影 Dialog。"""

from __future__ import annotations

import os

from qgis.core import QgsApplication, QgsMapLayer, QgsProject
from qgis.PyQt import sip
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

from app.core.domain.reproject import ReprojectJob, ReprojectService
from app.infra.logging import get_logger
from app.infra.messagebar import send
from app.ui.common.algo_desc import make_description_browser
from app.ui.common.base_dialog import BaseDialog
from app.ui.dialogs.create_data_dialog import CrsComboBox
from app.ui.dialogs.crs_pick_dialog import CrsPickDialog

logger = get_logger(__name__)


class ReprojectDialog(BaseDialog):
    """矢量重投影 Dialog。

    窗口设计参考 QGIS 原始"重投影图层"工具：输入图层 + 目标坐标系 + 输出。
    算法在后台线程执行（``QgsProcessingAlgRunnerTask``），主线程事件循环不
    阻塞，进度经 ``progress_updated`` 信号更新进度条，可随时取消。
    结果自动加入工程。
    """

    def __init__(self, parent=None, title: str = "") -> None:
        super().__init__(parent, title)
        # 左参数右描述布局需要更宽一点，比 BaseDialog 的 800x600 略收敛（同 BufferDialog）
        self.setMinimumSize(760, 520)
        self.project = QgsProject.instance()
        self.reproject_service = ReprojectService()
        #: 当前后台任务及其上下文 / 反馈（保留到下次执行，任务收尾仍会用到）
        self._task = None
        self._context = None
        self._feedback = None
        self._job: ReprojectJob | None = None
        #: 最近一条 ERROR 级算法日志（任务失败时展示到 messagebar）
        self._last_error = ""
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
        self._populate_layers()
        layout.addWidget(self.input_combo)
        layout.addSpacing(6)

        # 2. 目标坐标系（与「创建数据」一致：可搜索下拉 + 坐标系选择弹窗）
        layout.addWidget(QLabel("目标坐标系"))
        crs_row = QHBoxLayout()
        crs_row.setContentsMargins(0, 0, 0, 0)
        crs_row.setSpacing(6)
        self.crs_combo = CrsComboBox()
        self.crs_combo.setSizeAdjustPolicy(QComboBox.SizeAdjustPolicy.AdjustToMinimumContentsLengthWithIcon)
        self.crs_combo.setMinimumContentsLength(12)
        project_crs = self.project.crs()
        if project_crs.isValid():
            self.crs_combo.set_crs(project_crs.authid() or project_crs.toWkt())
        else:
            self.crs_combo.set_crs("EPSG:4326")
        crs_row.addWidget(self.crs_combo, 1)
        pick_btn = QPushButton("选择")
        pick_btn.setToolTip("按基准 / 分带挑选，或沿用某个图层、某份数据的空间参考")
        pick_btn.clicked.connect(self._on_pick_crs)
        crs_row.addWidget(pick_btn)
        layout.addLayout(crs_row)
        layout.addSpacing(6)

        # 3. 输出图层名
        layout.addWidget(QLabel("输出图层名"))
        self.output_edit = QLineEdit("转换结果")
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

    def _on_pick_crs(self) -> None:
        """「选择」按钮：开坐标系选择弹窗，确定后把结果写回下拉框。"""
        combo = self.crs_combo
        dialog = CrsPickDialog(
            self,
            current=combo.authid() or combo.currentText().strip(),
            layers=[
                layer
                for layer in self.project.mapLayers().values()
                if layer.type() == QgsMapLayer.VectorLayer
            ],
        )
        accepted = dialog.exec()
        value = dialog.crs_value() if accepted else ""
        dialog.deleteLater()
        if value:
            combo.set_crs(value)

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
        # 图层被移除时 C++ 对象已销毁，``findData`` 再去比较这个悬垂 wrapper
        # 会直接原生崩溃（access violation）。
        if current is not None and not sip.isdeleted(current):
            idx = self.input_combo.findData(current)
            if idx >= 0:
                self.input_combo.setCurrentIndex(idx)

    def _on_execute(self) -> None:
        layer = self.input_combo.currentData()
        if layer is None:
            send("warning", "请先在工程中加载一个矢量图层", duration=3)
            return
        target_crs = self.crs_combo.crs()
        if not target_crs.isValid():
            send("warning", "请选择有效的目标坐标系", duration=3)
            return

        job = ReprojectJob(
            input_layer=layer,
            target_crs=target_crs,
            output_name=self.output_edit.text().strip() or "转换结果",
            output_path=self.output_path_edit.text().strip() or None,
        )

        # 不在 clicked 信号栈里创建临时图层 / 启动任务（仓库记录过的原生崩溃
        # 机制，见 buffer._on_execute 注释）；统一推迟到事件栈外。
        self.execute_btn.setEnabled(False)
        self.cancel_btn.setEnabled(False)
        self.loading.setValue(0)
        self._last_error = ""
        QTimer.singleShot(0, lambda: self._start_task(job))

    # ------------------------------------------------------ 后台任务

    def _start_task(self, job: ReprojectJob) -> None:
        """事件栈外准备参数并启动后台任务（不在任何 Qt 信号回调栈里）。"""
        try:
            task, context, feedback = self.reproject_service.build_task(job)
        except Exception as exc:
            logger.error("重投影任务准备失败: %s", exc, exc_info=True)
            self.execute_btn.setEnabled(True)
            send("critical", f"重投影失败：{exc}", duration=5)
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
            send("warning", "已取消重投影", duration=3)
            return
        if not ok or bool(getattr(feedback, "failed", False)):
            self.loading.setValue(0)
            detail = f"：{error}" if error else ""
            logger.error("重投影失败%s", detail)
            send("critical", f"重投影失败{detail}", duration=5)
            return

        try:
            result = self.reproject_service.finish(job, context, results or {})
        except Exception as exc:
            logger.error("重投影结果装配失败: %s", exc, exc_info=True)
            self.loading.setValue(0)
            send("critical", f"重投影失败：{exc}", duration=5)
            return

        self.loading.setValue(100)
        # 结果加入工程；画布刷新再推迟一拍，等 layersAdded 信号链走完
        self.project.addMapLayer(result)
        dest = f" → {job.output_path}" if job.output_path else ""
        crs_txt = job.target_crs.authid() or job.target_crs.description()
        send("success", f"重投影完成：{result.featureCount()} 个要素 → {crs_txt}{dest}", duration=3)
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
            send("warning", "已取消重投影", duration=3)
            return
        detail = f"：{error}" if error else ""
        logger.error("重投影失败%s", detail)
        send("critical", f"重投影失败{detail}", duration=5)

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
