"""无人机影像拼接：六步弹窗向导。

向导负责流程配置、真实数据预检，并调用
:func:`app.core.uav.pipeline.run_orthomosaic` 完成空三、密集点云、
DSM 与正射影像解算。
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

from qgis.PyQt.QtCore import QTimer
from qgis.PyQt.QtWidgets import (
    QDialog,
    QHBoxLayout,
    QLabel,
    QListWidget,
    QListWidgetItem,
    QMessageBox,
    QPlainTextEdit,
    QProgressBar,
    QPushButton,
    QStackedWidget,
    QVBoxLayout,
)

from app.core.uav.engine import EngineInfo
from app.core.uav.gcp import load_marks, parse_gcp_file
from app.core.uav.models import PrecheckOutputs, UavJob
from app.core.uav.pipeline import (
    EngineUnavailableError,
    TaskResult,
    run_orthomosaic,
)
from app.core.uav.precheck import (
    PrecheckError,
    default_output_dir,
    run_precheck,
    scan_images,
)
from app.core.uav.presets import PRESET_MAP, apply_preset, get_preset
from app.infra.logging import get_logger
from app.infra.uav import load_settings, resolve_engine, save_settings
from app.ui.common.base_dialog import BaseDialog
from app.ui.dialogs.uav.engine_dialog import UavEngineDialog
from app.ui.dialogs.uav.gcp_dialog import GcpMarkDialog
from app.ui.dialogs.uav.steps import (
    AlignmentStep,
    DenseStep,
    ExportStep,
    OrthoStep,
    ProjectStep,
    SurfaceStep,
)
from app.ui.dialogs.uav.worker import UavWorker

logger = get_logger(__name__)

_NOTICE = (
    "选择航拍照片后一路「下一步」即可。软件会完成空三、密集点云、"
    "DSM 与正射影像解算；只有真实错误才会阻止继续。"
)

_FOOTER_STYLE = {
    "normal": "",
    "warn": "color: #b8860b;",
    "error": "color: #d9534f;",
    "ok": "color: #2e7d32;",
}


@dataclass
class UavWizardResult:
    """向导「完成」后的结果：任务参数、预检产物与解算成果。"""

    job: UavJob
    precheck: PrecheckOutputs | None = None
    task: TaskResult | None = None

    @property
    def outputs(self) -> PrecheckOutputs | None:
        """兼容旧调用：预检产物。"""
        return self.precheck


def _as_int(value, default: int) -> int:
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return default


def _as_float(value, default: float) -> float:
    try:
        return float(str(value).strip())
    except (TypeError, ValueError):
        return default


class UavSurveyWizard(BaseDialog):
    """无人机影像拼接向导。"""

    def __init__(
        self,
        parent=None,
        *,
        settings: Mapping[str, object] | None = None,
        scan_function=scan_images,
        precheck_function=run_precheck,
        engine_function=resolve_engine,
        process_function=run_orthomosaic,
    ) -> None:
        super().__init__(parent, title="无人机影像拼接")
        self.setMinimumSize(1040, 700)
        self.resize(1240, 860)
        self.setModal(True)

        self._settings = dict(settings) if settings is not None else load_settings()
        self._scan_function = scan_function
        self._precheck_function = precheck_function
        self._engine_function = engine_function
        self._processing_function = process_function
        self.job = self._job_from_settings(self._settings)
        self._restore_gcp()

        self._worker: UavWorker | None = None
        self._worker_running = False
        self._close_after_worker = False
        self._result_mode = False
        self._outputs: PrecheckOutputs | None = None
        self._task_result: TaskResult | None = None
        self._engine: EngineInfo | None = None
        self._visited = {0}
        self._max_visited = 0
        self._nav_loading = False
        self._last_progress_message = ""

        self._build_ui()
        self._go_to(0)
        self._refresh_engine_status()

    # ------------------------------------------------------------ 构建

    def _job_from_settings(self, settings: Mapping[str, object]) -> UavJob:
        job = UavJob()
        job.image_dir = str(settings.get("image_dir") or "")
        job.position_file = str(settings.get("position_file") or "")
        job.gcp_file = str(settings.get("gcp_file") or "")
        job.output_dir = str(settings.get("output_dir") or "")
        output_format = str(settings.get("output_format") or "tif").lower()
        job.output_format = output_format if output_format in ("tif", "png", "jpg") else "tif"
        job.crs_epsg = _as_int(settings.get("crs_epsg"), 4527) or 4527
        preset_key = str(settings.get("preset") or "standard")
        job.preset = preset_key if preset_key in PRESET_MAP else "standard"
        gsd_mode = str(settings.get("gsd_mode") or "auto")
        job.gsd_mode = gsd_mode if gsd_mode in ("auto", "manual") else "auto"
        job.gsd_cm = max(0.0, _as_float(settings.get("gsd_cm"), 0.0))
        if job.gsd_mode == "manual" and job.gsd_cm <= 0:
            job.gsd_mode = "auto"
        self._apply_preset_values(job, job.preset)
        return job

    @staticmethod
    def _apply_preset_values(job: UavJob, key: str) -> None:
        preset = get_preset(key)
        job.dense_cloud = preset.dense_cloud
        job.dense_quality = preset.dense_quality
        job.build_dsm = preset.build_dsm
        job.build_dtm = preset.build_dtm

    def _restore_gcp(self) -> None:
        """静默恢复上次使用的控制点文件（含刺点）；解析失败不打扰用户。"""
        path = self.job.gcp_file
        if not path or not Path(path).is_file():
            return
        gcp = parse_gcp_file(path)
        if not gcp.points:
            return
        load_marks(gcp)
        self.job.gcp = gcp
        if self.job.preset != "fast":
            self.job.alignment_mode = "gcp"

    def _build_ui(self) -> None:
        root = QVBoxLayout()
        root.setContentsMargins(16, 12, 16, 12)
        root.setSpacing(10)

        notice = QLabel(_NOTICE)
        notice.setObjectName("PropHint")
        notice.setWordWrap(True)
        root.addWidget(notice)

        body = QHBoxLayout()
        body.setSpacing(12)

        self.nav_list = QListWidget()
        self.nav_list.setFixedWidth(190)
        self.nav_list.setSpacing(2)
        self.nav_list.currentRowChanged.connect(self._on_nav_changed)
        body.addWidget(self.nav_list)

        self.stack = QStackedWidget()
        self.project_step = ProjectStep(self)
        self.alignment_step = AlignmentStep(self)
        self.dense_step = DenseStep(self)
        self.surface_step = SurfaceStep(self)
        self.ortho_step = OrthoStep(self)
        self.export_step = ExportStep(self)
        self.export_step.engine_button.clicked.connect(self.open_engine_settings)
        self.pages = [
            self.project_step,
            self.alignment_step,
            self.dense_step,
            self.surface_step,
            self.ortho_step,
            self.export_step,
        ]
        for page in self.pages:
            self.stack.addWidget(page)
            self.nav_list.addItem(QListWidgetItem(page.title))
        body.addWidget(self.stack, 1)
        root.addLayout(body, 1)

        self.footer_hint = QLabel()
        self.footer_hint.setWordWrap(True)
        root.addWidget(self.footer_hint)

        self.progress_bar = QProgressBar()
        self.progress_bar.setRange(0, 100)
        self.progress_bar.setValue(0)
        self.progress_bar.setTextVisible(True)
        self.progress_bar.setVisible(False)
        root.addWidget(self.progress_bar)

        self.log_edit = QPlainTextEdit()
        self.log_edit.setReadOnly(True)
        self.log_edit.setMaximumBlockCount(500)
        self.log_edit.setFixedHeight(96)
        self.log_edit.setVisible(False)
        root.addWidget(self.log_edit)

        buttons = QHBoxLayout()
        buttons.addStretch(1)
        self.cancel_button = QPushButton("取消")
        self.back_button = QPushButton("上一步")
        self.next_button = QPushButton("下一步")
        self.done_button = QPushButton("完成")
        self.cancel_button.clicked.connect(self.reject)
        self.back_button.clicked.connect(self._go_back)
        self.next_button.clicked.connect(self._go_next)
        self.done_button.clicked.connect(self._finish)
        self.done_button.setVisible(False)
        buttons.addWidget(self.cancel_button)
        buttons.addWidget(self.back_button)
        buttons.addWidget(self.next_button)
        buttons.addWidget(self.done_button)
        root.addLayout(buttons)

        self.setLayout(root)

    # ------------------------------------------------------------ 导航

    def _current_page(self):
        return self.pages[self.stack.currentIndex()]

    def _go_to(self, index: int, *, mark_visited: bool = False) -> None:
        if not 0 <= index < len(self.pages):
            return
        current = self.stack.currentIndex()
        if current != index:
            self.pages[current].commit()
        self.stack.setCurrentIndex(index)
        if mark_visited:
            self._visited.add(index)
            self._max_visited = max(self._max_visited, index)
        self._nav_loading = True
        self.nav_list.setCurrentRow(index)
        self._nav_loading = False
        self._refresh_nav()
        self.pages[index].refresh()
        self._refresh_buttons()
        self._show_page_hint()

    def _refresh_nav(self) -> None:
        for index, page in enumerate(self.pages):
            prefix = "✓ " if index in self._visited else ""
            item = self.nav_list.item(index)
            if item is not None:
                item.setText(prefix + page.title)

    def _on_nav_changed(self, row: int) -> None:
        if self._nav_loading or self._worker_running or self._result_mode:
            return
        if row < 0 or row == self.stack.currentIndex():
            return
        if row > self._max_visited:
            self._nav_loading = True
            self.nav_list.setCurrentRow(self.stack.currentIndex())
            self._nav_loading = False
            self._set_footer("请按顺序完成前面的步骤", "warn")
            return
        self._go_to(row)

    def _go_back(self) -> None:
        if self._worker_running or self._result_mode:
            return
        self._go_to(self.stack.currentIndex() - 1)

    def _go_next(self) -> None:
        if self._worker_running or self._result_mode:
            return
        page = self._current_page()
        page.commit()
        problems = page.validate()
        if problems:
            self._set_footer("✘ " + "；".join(problems), "error")
            return
        if self.stack.currentIndex() == len(self.pages) - 1:
            self._start_processing()
            return
        self._go_to(self.stack.currentIndex() + 1, mark_visited=True)

    def _refresh_buttons(self) -> None:
        index = self.stack.currentIndex()
        running = self._worker_running
        self.back_button.setEnabled(not running and not self._result_mode and index > 0)
        self.next_button.setVisible(not self._result_mode)
        self.next_button.setEnabled(not running and not self._result_mode)
        self.next_button.setText(
            "开始处理" if index == len(self.pages) - 1 else "下一步"
        )
        self.done_button.setVisible(self._result_mode)
        self.cancel_button.setVisible(not self._result_mode)
        self.cancel_button.setText("停止" if running else "取消")
        self.cancel_button.setEnabled(not self._result_mode)

    def _show_page_hint(self) -> None:
        if self._result_mode and self.stack.currentIndex() == len(self.pages) - 1:
            return
        page = self._current_page()
        warnings = page.warnings()
        if warnings:
            suffix = "…" if len(warnings) > 3 else ""
            self._set_footer("提醒：" + "；".join(warnings[:3]) + suffix, "warn")
        elif (
            self.stack.currentIndex() == 0
            and self.job.image_dir
            and not self.job.images
        ):
            self._set_footer("检测到上次使用的影像目录，请点击「重新扫描」读取影像信息", "normal")
        else:
            self._set_footer("", "normal")

    def _set_footer(self, text: str, kind: str = "normal") -> None:
        self.footer_hint.setText(text)
        self.footer_hint.setStyleSheet(_FOOTER_STYLE.get(kind, ""))

    # ------------------------------------------------------------ 扫描

    def start_scan(self, image_dir: str, position_file: str = "") -> bool:
        """在后台扫描影像；返回是否成功启动。"""
        if self._worker_running:
            self.show_message("正在处理上一步，请稍候…")
            return False
        previous_default = default_output_dir(self.job.image_dir) if self.job.image_dir else ""
        self.job.image_dir = image_dir
        self.job.position_file = position_file
        self.job.images = []
        self.job.summary = None
        self.job.relative_altitude_m = None
        self.project_step.altitude_spin.blockSignals(True)
        self.project_step.altitude_spin.setValue(0.0)
        self.project_step.altitude_spin.blockSignals(False)
        if not self.job.output_dir or self.job.output_dir == previous_default:
            self.job.output_dir = default_output_dir(image_dir)
            self.export_step.set_output_dir(self.job.output_dir)
        self._log("")
        self._log(f"开始扫描影像：{image_dir}")

        def function(progress, cancel):
            return self._scan_function(image_dir, position_file, progress, cancel)

        self._run_worker(function, self._on_scan_finished)
        return True

    def _on_scan_finished(self, result) -> None:
        self.job.images = list(result.images)
        self.job.summary = result.summary
        self.project_step.set_scan_result(result)
        self.job.position_file = self.project_step.position_edit.text().strip()
        self.alignment_step.refresh()
        self.export_step.refresh()
        self._log(
            f"扫描完成：{len(result.images)} 张影像，"
            f"有定位 {result.summary.positioned_count} 张"
        )
        for error in result.position_errors[:5]:
            self._log(f"位置文件：{error}")
        if not result.images:
            self._set_footer("✘ 影像文件夹里没有可处理的影像（jpg/jpeg/png/tif/tiff）", "error")
        else:
            self._show_page_hint()

    # ------------------------------------------------------------ GCP

    def import_gcp(self, path: str) -> bool:
        """解析控制点文件并载入已有刺点。"""
        gcp = parse_gcp_file(path)
        if not gcp.points:
            detail = "\n".join(gcp.errors[:6]) or "文件里没有有效记录"
            QMessageBox.warning(self, "控制点文件", f"无法导入控制点：\n{detail}")
            return False
        load_marks(gcp)
        self.job.gcp = gcp
        self.job.gcp_file = path
        if self.job.preset != "fast":
            self.job.alignment_mode = "gcp"
        self.project_step.gcp_edit.setText(path)
        self.project_step.set_gcp()
        self.alignment_step.refresh()
        message = f"已导入控制点：{len(gcp.control_points)} 个控制点，{len(gcp.check_points)} 个检查点"
        if gcp.errors:
            message += f"；{len(gcp.errors)} 行未解析"
        self.show_message(message)
        self._log(message)
        return True

    def open_mark_dialog(self) -> None:
        """打开刺点对话框；保存后更新任务。"""
        gcp = self.job.gcp
        if gcp is None or not gcp.points:
            QMessageBox.information(self, "控制点刺点", "请先在第 ① 步导入控制点文件")
            return
        if not self.job.images:
            QMessageBox.information(self, "控制点刺点", "请先扫描无人机影像")
            return
        dialog = GcpMarkDialog(
            self,
            gcp=gcp,
            image_paths=[image.path for image in self.job.images],
        )
        if dialog.exec() == QDialog.DialogCode.Accepted:
            self.job.gcp = dialog.result_gcp()
            self.job.gcp_file = self.job.gcp.source_path
            self.project_step.set_gcp()
            self.alignment_step.refresh()
            self._log(
                f"刺点已保存：{self.job.gcp.marked_point_count}/"
                f"{len(self.job.gcp.points)} 点，共 {self.job.gcp.total_marks} 个刺点"
            )
            self._set_footer("刺点已保存", "ok")
        dialog.deleteLater()

    # ------------------------------------------------------------ 预设

    def apply_preset(self, key: str) -> bool:
        """切换预设；会覆盖第 ③–⑤ 步参数，先征得用户同意。"""
        if key not in PRESET_MAP or key == self.job.preset:
            return True
        answer = QMessageBox.question(
            self,
            "处理模式",
            "切换处理模式会覆盖第 ③–⑤ 步的参数，继续吗？",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if answer != QMessageBox.StandardButton.Yes:
            return False
        apply_preset(self.job, key)
        for page in (self.dense_step, self.surface_step, self.ortho_step):
            page.refresh()
        self.alignment_step.refresh()
        self._log(f"处理模式：{get_preset(key).label}")
        return True

    # ------------------------------------------------------------ 处理

    def _start_processing(self) -> None:
        """「开始处理」：校验 → 预检 → 真实解算。"""
        if self._worker_running:
            return
        errors: list[tuple[int, str]] = []
        for index, page in enumerate(self.pages):
            page.commit()
            for problem in page.validate():
                errors.append((index, problem))
        if errors:
            first_index, first_problem = errors[0]
            self._go_to(first_index)
            self._set_footer("✘ " + first_problem, "error")
            return
        if not self.job.images:
            self._go_to(0)
            self._set_footer("✘ 请先扫描影像", "error")
            return
        if not self._ensure_engine():
            return

        self.log_edit.clear()
        self._task_result = None
        self._result_mode = False
        self._log("开始预检：检查影像、坐标系、控制点与输出目录…")
        self._set_footer("正在预检，请稍候…", "normal")

        def task(progress, cancel):
            try:
                return self._precheck_function(
                    self.job,
                    lambda percent, text: progress(int(percent * 0.04), text),
                    cancel,
                )
            except PrecheckError as exc:
                return exc

        self._run_worker(task, self._on_precheck_finished)

    def _ensure_engine(self) -> bool:
        """快速检查引擎配置；未安装时引导用户打开引擎设置。"""
        engine = self._resolve_engine(probe=False)
        if engine is not None and Path(engine.python).is_file():
            self._engine = engine
            return True
        answer = QMessageBox.question(
            self,
            "拼接引擎",
            "尚未安装拼接引擎组件，无法进行空三与正射解算。\n"
            "是否现在打开引擎设置下载/指定组件？",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.Yes,
        )
        if answer != QMessageBox.StandardButton.Yes:
            return False
        self.open_engine_settings()
        engine = self._resolve_engine(probe=False)
        if engine is not None and Path(engine.python).is_file():
            self._engine = engine
            return True
        return False

    def _resolve_engine(self, *, probe: bool) -> EngineInfo | None:
        try:
            return self._engine_function(probe=probe)
        except TypeError:
            return self._engine_function()

    def _on_precheck_finished(self, result) -> None:
        if isinstance(result, PrecheckError):
            self.export_step.show_errors(result.issues)
            self._set_footer(
                f"✘ 预检未通过：{len(result.issues)} 个错误，请按提示修改后重试", "error"
            )
            for issue in result.issues:
                self._log(f"✘ {issue.title}：{issue.detail}")
            return
        self._outputs = result
        self._log(
            f"预检通过：{result.error_count} 个错误，{result.warning_count} 个警告"
        )
        for issue in result.issues:
            if issue.level in {"warn", "error"}:
                self._log(f"⚠ {issue.title}：{issue.detail}")
        self._log(f"预检报告：{result.report_path}")
        self._set_footer("预检通过，开始空三解算…", "normal")
        QTimer.singleShot(0, self._start_solver)

    def _start_solver(self) -> None:
        if self._worker_running:
            return
        self._log("开始解算：空三 → 密集点云 → DSM → 正射影像…")

        def task(progress, cancel):
            engine = self._resolve_engine(probe=False)
            if engine is None:
                raise EngineUnavailableError(
                    "尚未配置拼接引擎。请到「引擎设置」安装组件或指定环境。"
                )
            self._engine = engine
            return self._processing_function(
                self.job,
                self.job.output_dir,
                lambda percent, text: progress(
                    4 + int(max(0, min(100, percent)) * 0.96), text
                ),
                cancel,
                engine_python=engine.python,
                engine_root=engine.root,
            )

        self._run_worker(task, self._on_solver_finished)

    def _on_solver_finished(self, result: TaskResult) -> None:
        self._task_result = result
        self.export_step.show_processing_result(result)
        self._result_mode = True
        self._set_footer(
            "✔ 处理完成：正射影像、DSM/DTM 与质检报告已生成",
            "ok",
        )
        self._refresh_buttons()
        for label, path in (
            ("正射影像", result.outputs.dom),
            ("DSM", result.outputs.dsm),
            ("DTM", result.outputs.dtm),
            ("点云", result.outputs.point_cloud),
            ("质检报告", result.outputs.report),
        ):
            if path:
                self._log(f"{label}：{path}")
        for warning in result.warnings:
            self._log(f"⚠ {warning}")
        logger.info(
            "无人机解算完成：影像=%s 注册=%s/%s 正射=%s 报告=%s",
            self.job.image_dir,
            result.summary.registered_images,
            result.summary.total_images,
            result.outputs.dom,
            result.outputs.report,
        )
        self._refresh_engine_status()

    def _refresh_engine_status(self) -> None:
        engine = self._resolve_engine(probe=False)
        if engine is None:
            self.export_step.set_engine_status(
                "未安装（点击「引擎设置」下载组件或指定已有环境）", False
            )
            return
        if not Path(engine.python).is_file():
            self.export_step.set_engine_status("配置无效：Python 不存在", False)
            return
        version = engine.opensfm_version or engine.version
        text = f"{engine.label}：{version or '已配置'}"
        self.export_step.set_engine_status(text, True)

    def open_engine_settings(self) -> None:
        dialog = UavEngineDialog(self)
        dialog.exec()
        dialog.deleteLater()
        self._refresh_engine_status()

    def show_message(self, text: str) -> None:
        self._set_footer(text, "warn")

    def _log(self, text: str) -> None:
        if text or self.log_edit.toPlainText():
            self.log_edit.appendPlainText(text)

    # ------------------------------------------------------------ 后台任务

    def _run_worker(self, function, on_finished) -> None:
        self._set_running(True)
        self.progress_bar.setValue(0)
        self._last_progress_message = ""
        worker = UavWorker(function, self)
        self._worker = worker
        worker.progress.connect(self._on_progress)
        worker.finished.connect(lambda result: self._on_worker_finished(result, on_finished))
        worker.failed.connect(self._on_worker_failed)
        worker.start()

    def _on_progress(self, percent: int, message: str) -> None:
        self.progress_bar.setValue(max(0, min(100, percent)))
        if message and message != self._last_progress_message:
            self._last_progress_message = message
            self._log(message)

    def _on_worker_finished(self, result, callback) -> None:
        self._worker = None
        self._set_running(False)
        if self._close_after_worker:
            self._close_after_worker = False
            super().reject()
            return
        try:
            callback(result)
        except Exception as exc:
            logger.exception("无人机向导回调失败")
            QMessageBox.warning(self, "无人机影像拼接", f"处理结果时出错：{exc}")

    def _on_worker_failed(self, message: str) -> None:
        self._worker = None
        self._set_running(False)
        if self._close_after_worker:
            self._close_after_worker = False
            super().reject()
            return
        self._log(f"失败：{message}")
        self._set_footer(f"✘ {message}", "error")
        if "引擎" in message or "OpenSfM" in message:
            answer = QMessageBox.question(
                self,
                "拼接引擎",
                f"{message}\n\n是否打开引擎设置？",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.Yes,
            )
            if answer == QMessageBox.StandardButton.Yes:
                self.open_engine_settings()
            return
        QMessageBox.warning(self, "无人机影像拼接", message)

    def _set_running(self, running: bool) -> None:
        self._worker_running = running
        self.progress_bar.setVisible(running or self.progress_bar.value() > 0)
        if running:
            self.log_edit.setVisible(True)
        self.nav_list.setEnabled(not running and not self._result_mode)
        self._refresh_buttons()

    # ------------------------------------------------------------ 结束

    def _finish(self) -> None:
        self._save_settings()
        super().accept()

    def _save_settings(self) -> None:
        job = self.job
        try:
            save_settings(
                {
                    "image_dir": job.image_dir,
                    "position_file": job.position_file,
                    "gcp_file": job.gcp_file,
                    "output_dir": job.output_dir,
                    "output_format": job.output_format,
                    "crs_epsg": job.crs_epsg,
                    "preset": job.preset,
                    "gsd_mode": job.gsd_mode,
                    "gsd_cm": job.gsd_cm,
                }
            )
        except Exception:
            logger.exception("保存无人机向导设置失败")

    def wizard_result(self) -> UavWizardResult | None:
        if self._task_result is None:
            return None
        return UavWizardResult(
            job=self.job,
            precheck=self._outputs,
            task=self._task_result,
        )

    def reject(self) -> None:
        if self._worker_running:
            answer = QMessageBox.question(
                self,
                "无人机影像拼接",
                "任务正在运行，确定停止并关闭吗？",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.No,
            )
            if answer != QMessageBox.StandardButton.Yes:
                return
            self._close_after_worker = True
            if self._worker is not None:
                self._worker.cancel()
            self.cancel_button.setEnabled(False)
            self._log("正在停止任务…")
            return
        super().reject()


__all__ = ["UavSurveyWizard", "UavWizardResult"]
