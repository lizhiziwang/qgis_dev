"""深度学习设置：依赖项 / 模型管理 / 缓存 / 帮助。

- 依赖页统一管理标注 / 训练 / 推理的外部 Python 依赖；
- 所有选项即时写入 QSettings（`deep_learning/*`），关闭后由主窗口/标注窗口读取并应用；
- 依赖探测/一键 pip 安装在 GUI 线程用 QProcess 异步执行，日志流式显示；
- 模型下载在 daemon 线程（`ModelDownloadTask`），支持续传与取消；
- 缓存页只做目录统计/清空，真正的特征缓存由 sidecar 读写。
"""

from __future__ import annotations

import contextlib
import os
from dataclasses import replace
from pathlib import Path
from typing import ClassVar

from qgis.PyQt.QtCore import QProcess, QProcessEnvironment, Qt, QTimer, QUrl
from qgis.PyQt.QtGui import QColor, QDesktopServices
from qgis.PyQt.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDialogButtonBox,
    QFileDialog,
    QFormLayout,
    QGroupBox,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPlainTextEdit,
    QProgressBar,
    QPushButton,
    QSpinBox,
    QTabBar,
    QTableWidgetItem,
    QTabWidget,
    QTextBrowser,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

from app.core.ai.sam_download import ModelDownloadTask, resolve_download_headers
from app.core.ai.sam_environment import (
    MAX_LOG_LINES,
    DependencyInstaller,
    clean_process_environment,
    python_problem,
)
from app.core.ai.sam_models import (
    MODELS,
    SamModel,
    infer_family,
    model_for_checkpoint,
    model_path,
    resolve_sam_paths,
    sam2_config_for_filename,
)
from app.core.deep_learning.environment import (
    DEEP_LEARNING_DEPENDENCIES,
    USAGE_TITLES,
    build_install_commands,
    build_probe_command,
    parse_probe_output,
)
from app.core.training.environment import TrainingEnvironmentReport
from app.infra.deep_learning import (
    SETTING_CACHE_CLEAR_ON_CLOSE,
    SETTING_CACHE_DIR,
    SETTING_CACHE_ENABLED,
    SETTING_CACHE_MAX_MB,
    SETTING_CHECKPOINT,
    SETTING_MODEL_ID,
    SETTING_MODEL_TYPE,
    SETTING_MODELS_DIR,
    SETTING_PYTHON,
    SETTING_SAM2_CONFIG,
    SETTING_SAM_SRC,
    DeepLearningConfig,
    save_inference_setting,
    save_sam_setting,
    save_training_setting,
)
from app.ui.common.base_dialog import BaseDialog
from app.ui.common.widgets import make_table
from app.ui.deep_learning.sam_cache import cache_usage, clear_cache, format_bytes

_FILTERS: tuple[tuple[str, str], ...] = (
    ("全部", ""),
    ("SAM1", "SAM1"),
    ("SAM2", "SAM2"),
    ("SAM2.1", "SAM2.1"),
    ("SAM3", "SAM3"),
    ("SAM3.1", "SAM3.1"),
)

#: 依赖探测超时（秒）
_PROBE_TIMEOUT_MS = 30_000


def _same_path(left: str, right: str | Path) -> bool:
    if not left or not right:
        return False
    try:
        return os.path.normcase(os.path.abspath(str(left))) == os.path.normcase(os.path.abspath(str(right)))
    except OSError:
        return False


def _open_directory(path: str | Path) -> None:
    directory = Path(path)
    with contextlib.suppress(OSError):
        directory.mkdir(parents=True, exist_ok=True)
    QDesktopServices.openUrl(QUrl.fromLocalFile(str(directory)))


class DeepLearningSettingsDialog(BaseDialog):
    """标注 / 训练 / 推理的统一设置对话框（依赖、SAM 模型、缓存）。"""

    _TAB_INDEX: ClassVar[dict[str, int]] = {
        "dependencies": 0,
        "models": 1,
        "cache": 2,
        "help": 3,
    }

    def __init__(
        self,
        config: DeepLearningConfig,
        parent: QWidget | None = None,
        initial_tab: str = "dependencies",
    ) -> None:
        super().__init__(parent, "深度学习设置", show_max_button=False)
        self._config = config
        self._sam = resolve_sam_paths(config.sam)
        self._shared_python = self._sam.python
        self._training_python = config.training.python or self._shared_python
        self._inference_python = config.inference.python or self._training_python
        self._checkpoint = self._sam.checkpoint
        self._model_id = self._sam.model_id
        self._sam2_config = self._sam.sam2_config
        self._model_type = self._sam.model_type
        self._report: TrainingEnvironmentReport | None = None
        self._missing_keys: tuple[str, ...] = ()
        self._probe_process: QProcess | None = None
        self._probe_stdout = b""
        self._probe_stderr = ""
        self._probe_timed_out = False
        self._probed_once = False
        self._closing = False
        self._download_task: ModelDownloadTask | None = None
        self._installing = False

        self.setMinimumSize(0, 0)
        self.resize(960, 720)

        self._probe_timer = QTimer(self)
        self._probe_timer.setSingleShot(True)
        self._probe_timer.timeout.connect(self._on_probe_timeout)

        self.installer = DependencyInstaller(self)
        self.installer.log.connect(self._append_log)
        self.installer.started.connect(self._on_install_started)
        self.installer.finished.connect(self._on_install_finished)

        layout = QVBoxLayout()
        layout.setContentsMargins(10, 10, 10, 10)
        layout.setSpacing(8)
        self.tabs = QTabWidget()
        self.tabs.addTab(self._build_dependencies_tab(), "依赖项")
        self.tabs.addTab(self._build_models_tab(), "模型管理")
        self.tabs.addTab(self._build_cache_tab(), "缓存")
        self.tabs.addTab(self._build_help_tab(), "帮助")
        layout.addWidget(self.tabs, 1)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)
        self.setLayout(layout)

        self.tabs.setCurrentIndex(self._TAB_INDEX.get(str(initial_tab), 0))
        self._update_effective_pythons()
        self._refresh_models()
        self._refresh_cache_usage()

    # ================================================================== 依赖页
    def _build_dependencies_tab(self) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setSpacing(8)

        runtime = QGroupBox("外部运行环境")
        runtime_layout = QVBoxLayout(runtime)
        python_row = QHBoxLayout()
        python_row.addWidget(QLabel("共享 Python："))
        self.python_edit = QLineEdit(self._sam.python)
        self.python_edit.setPlaceholderText("选择独立 Python（不能是 QGIS 自带解释器）")
        self.python_edit.editingFinished.connect(self._on_python_changed)
        self.btn_python_browse = QToolButton()
        self.btn_python_browse.setText("浏览…")
        self.btn_python_browse.clicked.connect(self._browse_python)
        self.btn_python_detect = QToolButton()
        self.btn_python_detect.setText("检测")
        self.btn_python_detect.clicked.connect(self.refresh_dependencies)
        python_row.addWidget(self.python_edit, 1)
        python_row.addWidget(self.btn_python_browse)
        python_row.addWidget(self.btn_python_detect)
        runtime_layout.addLayout(python_row)

        self.effective_label = QLabel()
        self.effective_label.setWordWrap(True)
        self.effective_label.setTextFormat(Qt.TextFormat.PlainText)
        self.effective_label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        runtime_layout.addWidget(self.effective_label)

        override_row = QHBoxLayout()
        self.override_label = QLabel()
        self.override_label.setWordWrap(True)
        self.override_label.setStyleSheet("color: #b8860b;")
        self.btn_unify_python = QPushButton("统一为共享 Python")
        self.btn_unify_python.setToolTip("清除训练 / 推理的独立 Python 设置，让它们回退到上面的共享 Python")
        self.btn_unify_python.clicked.connect(self._unify_python)
        override_row.addWidget(self.override_label, 1)
        override_row.addWidget(self.btn_unify_python)
        runtime_layout.addLayout(override_row)
        self.override_label.setVisible(False)
        self.btn_unify_python.setVisible(False)

        sam_src_row = QHBoxLayout()
        sam_src_row.addWidget(QLabel("SAM 源码："))
        self.sam_src_edit = QLineEdit(self._sam.sam_src)
        self.sam_src_edit.setPlaceholderText("可选（使用 pip 安装的 segment-anything 时留空）")
        self.sam_src_edit.editingFinished.connect(self._on_sam_src_changed)
        self.btn_sam_src_browse = QToolButton()
        self.btn_sam_src_browse.setText("浏览…")
        self.btn_sam_src_browse.clicked.connect(self._browse_sam_src)
        self.btn_sam_src_clear = QToolButton()
        self.btn_sam_src_clear.setText("清除")
        self.btn_sam_src_clear.clicked.connect(self._clear_sam_src)
        sam_src_row.addWidget(self.sam_src_edit, 1)
        sam_src_row.addWidget(self.btn_sam_src_browse)
        sam_src_row.addWidget(self.btn_sam_src_clear)
        runtime_layout.addLayout(sam_src_row)
        layout.addWidget(runtime)

        deps = QGroupBox("依赖项")
        deps_layout = QVBoxLayout(deps)
        self.dependency_status = QLabel("尚未检测")
        self.dependency_status.setWordWrap(True)
        deps_layout.addWidget(self.dependency_status)
        self.dependency_table = make_table(["依赖", "用途", "状态", "版本"])
        self.dependency_table.verticalHeader().setVisible(False)
        deps_layout.addWidget(self.dependency_table, 1)

        install_row = QHBoxLayout()
        install_row.addWidget(QLabel("PyTorch："))
        self.torch_variant_combo = QComboBox()
        self.torch_variant_combo.addItem("CPU（推荐）", "cpu")
        self.torch_variant_combo.addItem("CUDA 12.4", "cuda124")
        install_row.addWidget(self.torch_variant_combo)
        install_row.addStretch(1)
        self.btn_refresh_deps = QPushButton("重新检测")
        self.btn_refresh_deps.clicked.connect(self.refresh_dependencies)
        self.btn_install = QPushButton("安装缺失依赖")
        self.btn_install.setEnabled(False)
        self.btn_install.clicked.connect(self._install_missing)
        self.btn_cancel_install = QPushButton("取消安装")
        self.btn_cancel_install.setEnabled(False)
        self.btn_cancel_install.clicked.connect(self._cancel_install)
        install_row.addWidget(self.btn_refresh_deps)
        install_row.addWidget(self.btn_install)
        install_row.addWidget(self.btn_cancel_install)
        deps_layout.addLayout(install_row)
        self.install_progress = QProgressBar()
        self.install_progress.setRange(0, 0)
        self.install_progress.setVisible(False)
        deps_layout.addWidget(self.install_progress)
        self.install_log = QPlainTextEdit()
        self.install_log.setReadOnly(True)
        self.install_log.setMaximumBlockCount(MAX_LOG_LINES)
        self.install_log.setPlaceholderText("pip 安装输出会显示在这里")
        self.install_log.setMinimumHeight(140)
        deps_layout.addWidget(self.install_log)
        layout.addWidget(deps, 1)
        return page

    def _on_python_changed(self) -> None:
        save_sam_setting(SETTING_PYTHON, self.python_edit.text().strip())
        self.refresh_dependencies()

    def _on_sam_src_changed(self) -> None:
        save_sam_setting(SETTING_SAM_SRC, self.sam_src_edit.text().strip())
        self.refresh_dependencies()

    def _persist_environment(self) -> None:
        save_sam_setting(SETTING_PYTHON, self.python_edit.text().strip())
        save_sam_setting(SETTING_SAM_SRC, self.sam_src_edit.text().strip())

    def _sync_shared_python(self) -> None:
        """共享 Python 改动后，把沿用它的训练/推理有效路径一起更新。"""
        new = self.python_edit.text().strip()
        old = self._shared_python
        if new == old:
            return
        old_training = self._training_python
        if self._training_python in ("", old):
            self._training_python = new
        if self._inference_python in ("", old, old_training):
            self._inference_python = self._training_python
        self._shared_python = new

    def _usage_pythons(self) -> dict[str, str]:
        return {
            "annotation": self._shared_python,
            "training": self._training_python,
            "inference": self._inference_python,
        }

    def _update_effective_pythons(self) -> None:
        shared = self._shared_python
        training = self._training_python
        inference = self._inference_python

        def text(path: str) -> str:
            return path or "未配置"

        if not training:
            training_note = "未配置"
        elif training == shared:
            training_note = "沿用共享"
        else:
            training_note = "独立"

        if not inference:
            inference_note = "未配置"
        elif inference == training:
            inference_note = "沿用训练"
        elif inference == shared:
            inference_note = "沿用共享"
        else:
            inference_note = "独立"

        self.effective_label.setText(
            "\n".join(
                (
                    f"标注：{text(shared)}（共享）",
                    f"训练：{text(training)}（{training_note}）",
                    f"推理：{text(inference)}（{inference_note}）",
                )
            )
        )

        overrides = [
            USAGE_TITLES[usage] for usage in ("training", "inference") if self._usage_pythons()[usage] != shared
        ]
        if overrides:
            self.override_label.setText(
                f"{'、'.join(overrides)}使用独立 Python；本页只检测/安装共享 Python 的依赖。"
                "可在对应页面使用「安装…依赖」，或点击右侧统一。"
            )
        self.override_label.setVisible(bool(overrides))
        self.btn_unify_python.setVisible(bool(overrides))
        self.btn_unify_python.setEnabled(bool(overrides) and bool(shared))

    def _unify_python(self) -> None:
        shared = self._shared_python
        if not shared:
            QMessageBox.warning(self, "统一 Python", "请先配置共享 Python。")
            return
        reply = QMessageBox.question(
            self,
            "统一 Python",
            f"清除训练 / 推理的独立 Python 设置，让两者都回退到共享 Python？\n\n{shared}",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if reply != QMessageBox.StandardButton.Yes:
            return
        save_training_setting("python", "")
        save_inference_setting("python", "")
        self._training_python = shared
        self._inference_python = shared
        self._update_effective_pythons()
        self.refresh_dependencies()

    def _browse_python(self) -> None:
        path, _ = QFileDialog.getOpenFileName(
            self,
            "选择外部 Python",
            self.python_edit.text().strip(),
            "Python (python.exe python);;所有文件 (*)",
        )
        if path:
            self.python_edit.setText(path)
            self._on_python_changed()

    def _browse_sam_src(self) -> None:
        path = QFileDialog.getExistingDirectory(self, "选择 SAM 源码目录", self.sam_src_edit.text())
        if path:
            self.sam_src_edit.setText(path)
            self._on_sam_src_changed()

    def _clear_sam_src(self) -> None:
        self.sam_src_edit.clear()
        self._on_sam_src_changed()

    def refresh_dependencies(self) -> None:
        """异步检测共享 Python 的依赖（只读，不改环境）。"""
        if self._installing or self._closing:
            return
        self._persist_environment()
        self._sync_shared_python()
        self._update_effective_pythons()
        python = self._shared_python
        problem = python_problem(python)
        if problem:
            self._probe_stdout = b""
            self._apply_report(TrainingEnvironmentReport(error=problem))
            return
        self._stop_probe()
        self._probe_stdout = b""
        self._probe_stderr = ""
        self._probe_timed_out = False
        self.dependency_status.setText("正在检测依赖…")
        self.btn_install.setEnabled(False)

        process = QProcess(self)
        process.setProcessChannelMode(QProcess.ProcessChannelMode.SeparateChannels)
        command = build_probe_command(python, self.sam_src_edit.text().strip())
        process.setProgram(command[0])
        process.setArguments(command[1:])
        process.setProcessEnvironment(clean_process_environment(QProcessEnvironment.systemEnvironment()))
        process.readyReadStandardOutput.connect(lambda p=process: self._read_probe_stdout(p))
        process.readyReadStandardError.connect(lambda p=process: self._read_probe_stderr(p))
        process.finished.connect(lambda code, status, p=process: self._on_probe_finished(p, code, status))
        process.errorOccurred.connect(lambda error, p=process: self._on_probe_error(p, error))
        self._probe_process = process
        self._probe_timer.start(_PROBE_TIMEOUT_MS)
        process.start()

    def _read_probe_stdout(self, process: QProcess) -> None:
        self._probe_stdout += bytes(process.readAllStandardOutput())

    def _read_probe_stderr(self, process: QProcess) -> None:
        text = bytes(process.readAllStandardError()).decode("utf-8", errors="replace")
        if text:
            self._probe_stderr = (self._probe_stderr + text)[-4000:]

    def _on_probe_finished(self, process: QProcess, code: int, status: QProcess.ExitStatus) -> None:
        if process is not self._probe_process:
            return
        self._read_probe_stdout(process)
        self._read_probe_stderr(process)
        self._probe_timer.stop()
        self._probe_process = None
        process.deleteLater()
        if self._probe_timed_out:
            return
        text = self._probe_stdout.decode("utf-8", errors="replace")
        error = ""
        if status != QProcess.ExitStatus.NormalExit or code != 0:
            tail = self._probe_stderr.strip()[-600:]
            error = f"依赖探测失败（退出码 {code}）" + (f"：{tail}" if tail else "")
        report = parse_probe_output(text, error=error)
        if not text.strip() and not error:
            error = "依赖探测没有输出"
            report = TrainingEnvironmentReport(error=error)
        self._apply_report(report)

    def _on_probe_error(self, process: QProcess, _error: QProcess.ProcessError) -> None:
        if process is not self._probe_process:
            return
        message = process.errorString()
        self._probe_timer.stop()
        self._probe_process = None
        process.deleteLater()
        self._apply_report(TrainingEnvironmentReport(error=f"无法启动依赖探测：{message}"))

    def _on_probe_timeout(self) -> None:
        process = self._probe_process
        if process is None:
            return
        self._probe_timed_out = True
        process.kill()
        process.waitForFinished(1000)
        self._probe_process = None
        process.deleteLater()
        self._apply_report(TrainingEnvironmentReport(error="依赖探测超时"))

    def _stop_probe(self) -> None:
        process = self._probe_process
        self._probe_timer.stop()
        self._probe_process = None
        if process is not None:
            if process.state() != QProcess.ProcessState.NotRunning:
                process.kill()
                process.waitForFinished(500)
            process.deleteLater()

    def _apply_report(self, report: TrainingEnvironmentReport) -> None:
        self._report = report
        usage_paths = self._usage_pythons()
        shared = self._shared_python
        missing_keys: list[str] = []
        relevant_total = 0
        available = 0

        self.dependency_table.setRowCount(len(DEEP_LEARNING_DEPENDENCIES))
        for row, spec in enumerate(DEEP_LEARNING_DEPENDENCIES):
            shared_usages = [usage for usage in spec["usages"] if usage_paths.get(usage) == shared]
            usages_text = " / ".join(
                USAGE_TITLES[usage] + ("(独立)" if usage_paths.get(usage) != shared else "") for usage in spec["usages"]
            )
            status = report.status(spec["key"])
            found = bool(status and status.found)

            if shared_usages:
                relevant_total += 1
                if found:
                    available += 1
                elif not report.error:
                    missing_keys.append(spec["key"])

            if not shared_usages:
                state_text, color, version = "—", "#808080", ""
                tooltip = "该依赖仅用于独立 Python 环境，本页不检测"
            elif report.error and not report.statuses:
                state_text, color, version = "未知", "#808080", ""
                tooltip = report.error
            elif status is None:
                state_text, color, version = "未知", "#808080", ""
                tooltip = "探测结果缺少该依赖"
            elif found:
                state_text, color, version = "已安装", "#2e8b57", status.version
                tooltip = f"版本：{version}" if version else "已安装"
            else:
                state_text, color, version = "缺失", "#c0392b", ""
                tooltip = f"未检测到模块 {spec['module']}"

            self._set_table_item(row, 0, spec["title"])
            self._set_table_item(row, 1, usages_text)
            state_item = self._make_item(state_text)
            state_item.setForeground(QColor(color))
            state_item.setToolTip(tooltip)
            self.dependency_table.setItem(row, 2, state_item)
            self._set_table_item(row, 3, version or "—")

        header = self.dependency_table.horizontalHeader()
        header.setStretchLastSection(False)
        for column in range(4):
            header.setSectionResizeMode(column, QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        self.dependency_table.verticalHeader().setVisible(False)

        self._missing_keys = tuple(missing_keys)
        if not shared:
            self.dependency_status.setText("尚未配置外部 Python")
        elif report.error:
            self.dependency_status.setText(report.error)
        else:
            self.dependency_status.setText(f"Python {report.python or '?'} · {available}/{relevant_total} 项依赖可用")

        count = len(missing_keys)
        self.btn_install.setText(f"安装缺失依赖（{count} 项）" if count else "安装缺失依赖")
        self.btn_install.setEnabled(
            bool(report.statuses) and count > 0 and not report.error and not self._installing and not self._closing
        )

    def _set_table_item(self, row: int, column: int, text: str) -> None:
        self.dependency_table.setItem(row, column, self._make_item(text))

    @staticmethod
    def _make_item(text: str) -> QTableWidgetItem:
        item = QTableWidgetItem(str(text))
        item.setFlags(item.flags() & ~Qt.ItemFlag.ItemIsEditable)
        return item

    def _install_missing(self) -> None:
        keys = self._missing_keys
        if not keys or self._installing or self._closing:
            return
        python = self._shared_python
        titles = "、".join(spec["title"] for spec in DEEP_LEARNING_DEPENDENCIES if spec["key"] in keys)
        problem = python_problem(python)
        if problem:
            QMessageBox.warning(self, "深度学习设置", problem)
            return
        reply = QMessageBox.question(
            self,
            "安装深度学习依赖",
            f"将使用以下共享 Python 安装 {len(keys)} 项依赖：\n\n{python}\n\n缺失：{titles}\n\n是否继续？",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if reply != QMessageBox.StandardButton.Yes:
            return
        commands = build_install_commands(
            python,
            keys,
            torch_variant=str(self.torch_variant_combo.currentData() or "cpu"),
        )
        if not commands:
            return
        self.install_log.appendPlainText(f"—— 开始安装：{python}")
        self.installer.start_commands(commands)

    def _on_install_started(self) -> None:
        self._installing = True
        self.install_progress.setVisible(True)
        self.btn_install.setEnabled(False)
        self.btn_refresh_deps.setEnabled(False)
        self.btn_cancel_install.setEnabled(True)
        self.python_edit.setEnabled(False)
        self.sam_src_edit.setEnabled(False)
        self.btn_python_browse.setEnabled(False)
        self.btn_python_detect.setEnabled(False)
        self.btn_sam_src_browse.setEnabled(False)
        self.btn_sam_src_clear.setEnabled(False)
        self.torch_variant_combo.setEnabled(False)

    def _cancel_install(self) -> None:
        if self._installing:
            self.installer.cancel()

    def _on_install_finished(self, ok: bool, code: int) -> None:
        self._installing = False
        self.install_progress.setVisible(False)
        self.btn_refresh_deps.setEnabled(True)
        self.btn_cancel_install.setEnabled(False)
        self.python_edit.setEnabled(True)
        self.sam_src_edit.setEnabled(True)
        self.btn_python_browse.setEnabled(True)
        self.btn_python_detect.setEnabled(True)
        self.btn_sam_src_browse.setEnabled(True)
        self.btn_sam_src_clear.setEnabled(True)
        self.torch_variant_combo.setEnabled(True)
        if ok:
            self.install_log.appendPlainText("—— 安装完成，重新检测依赖")
            self.refresh_dependencies()
        elif code == -2:
            self.install_log.appendPlainText("—— 安装已取消")
            self.refresh_dependencies()
        else:
            self.install_log.appendPlainText(f"—— 安装失败（退出码 {code}），请查看上方日志")
            self.dependency_status.setText(f"依赖安装失败（退出码 {code}）")

    def _append_log(self, text: str) -> None:
        if text:
            self.install_log.appendPlainText(text)

    # ================================================================== 模型页
    def _build_models_tab(self) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setSpacing(8)

        dir_row = QHBoxLayout()
        dir_row.addWidget(QLabel("模型目录："))
        self.models_dir_edit = QLineEdit(self._sam.models_dir)
        self.models_dir_edit.editingFinished.connect(self._on_models_dir_changed)
        self.btn_models_dir = QToolButton()
        self.btn_models_dir.setText("浏览…")
        self.btn_models_dir.clicked.connect(self._browse_models_dir)
        self.btn_open_models_dir = QToolButton()
        self.btn_open_models_dir.setText("打开")
        self.btn_open_models_dir.clicked.connect(lambda: _open_directory(self.models_dir_edit.text().strip()))
        dir_row.addWidget(self.models_dir_edit, 1)
        dir_row.addWidget(self.btn_models_dir)
        dir_row.addWidget(self.btn_open_models_dir)
        layout.addLayout(dir_row)

        self.model_filter = QTabBar()
        for title, key in _FILTERS:
            self.model_filter.addTab(title)
            self.model_filter.setTabData(self.model_filter.count() - 1, key)
        self.model_filter.currentChanged.connect(lambda _index: self._refresh_models())
        layout.addWidget(self.model_filter)

        self.model_table = make_table(["模型", "文件", "状态", "大小"])
        self.model_table.verticalHeader().setVisible(False)
        self.model_table.itemSelectionChanged.connect(self._update_model_buttons)
        layout.addWidget(self.model_table, 1)

        self.current_model_label = QLabel()
        self.current_model_label.setWordWrap(True)
        layout.addWidget(self.current_model_label)

        buttons = QHBoxLayout()
        self.btn_download = QPushButton("下载选中")
        self.btn_download.clicked.connect(self._download_selected)
        self.btn_cancel_download = QPushButton("取消下载")
        self.btn_cancel_download.setEnabled(False)
        self.btn_cancel_download.clicked.connect(self._cancel_download)
        self.btn_delete_model = QPushButton("删除")
        self.btn_delete_model.clicked.connect(self._delete_selected)
        self.btn_use_model = QPushButton("使用该模型")
        self.btn_use_model.clicked.connect(self._use_selected)
        self.btn_import_model = QPushButton("导入本地权重…")
        self.btn_import_model.clicked.connect(self._import_weights)
        self.btn_model_page = QPushButton("权重页面")
        self.btn_model_page.setToolTip("打开模型权重主页（SAM3 需在此申请 HuggingFace 访问）")
        self.btn_model_page.clicked.connect(self._open_model_page)
        self.btn_refresh_models = QPushButton("刷新")
        self.btn_refresh_models.clicked.connect(self._refresh_models)
        for button in (
            self.btn_download,
            self.btn_cancel_download,
            self.btn_delete_model,
            self.btn_use_model,
            self.btn_import_model,
            self.btn_model_page,
            self.btn_refresh_models,
        ):
            buttons.addWidget(button)
        buttons.addStretch(1)
        layout.addLayout(buttons)

        self.model_progress = QProgressBar()
        self.model_progress.setVisible(False)
        layout.addWidget(self.model_progress)
        self.model_status = QLabel("选择模型后可下载或设为当前模型")
        self.model_status.setWordWrap(True)
        layout.addWidget(self.model_status)
        return page

    def _on_models_dir_changed(self) -> None:
        save_sam_setting(SETTING_MODELS_DIR, self.models_dir_edit.text().strip())
        self._refresh_models()

    def _browse_models_dir(self) -> None:
        path = QFileDialog.getExistingDirectory(self, "选择模型目录", self.models_dir_edit.text())
        if path:
            self.models_dir_edit.setText(path)
            self._on_models_dir_changed()

    def _visible_models(self) -> list[SamModel]:
        index = max(0, self.model_filter.currentIndex())
        key = str(self.model_filter.tabData(index) or "")
        if not key:
            return list(MODELS)
        return [model for model in MODELS if model.version_label == key]

    def _refresh_models(self) -> None:
        models_dir = self.models_dir_edit.text().strip() or self._sam.models_dir
        selected_model = self._selected_model()
        selected_id = selected_model.id if selected_model is not None else ""
        self.model_table.blockSignals(True)
        self.model_table.setRowCount(0)
        self._row_models: list[SamModel] = []
        for model in self._visible_models():
            row = self.model_table.rowCount()
            self.model_table.insertRow(row)
            self._row_models.append(model)
            path = model_path(model, models_dir)
            part = path.with_name(path.name + ".part")
            downloaded = path.is_file()
            if _same_path(self._checkpoint, path):
                state = "当前使用"
                state_color = "#2e8b57"
            elif self._download_task is not None and _same_path(self._download_task.target, path):
                state = "下载中"
                state_color = "#4363d8"
            elif downloaded:
                state = "已下载"
                state_color = "#2e8b57"
            elif part.is_file():
                state = "未下载（可续传）"
                state_color = "#b8860b"
            elif model.gated:
                state = "未下载（需 HF 授权）"
                state_color = "#b8860b"
            else:
                state = "未下载"
                state_color = "#808080"
            name_item = self._make_item(model.name)
            name_item.setData(Qt.ItemDataRole.UserRole, model.id)
            self.model_table.setItem(row, 0, name_item)
            file_item = self._make_item(model.filename)
            file_item.setToolTip(f"{path}\n{model.page_url}" if model.gated else str(path))
            self.model_table.setItem(row, 1, file_item)
            state_item = self._make_item(state)
            state_item.setForeground(QColor(state_color))
            self.model_table.setItem(row, 2, state_item)
            if downloaded:
                try:
                    size = format_bytes(path.stat().st_size)
                except OSError:
                    size = "—"
            else:
                size = f"约 {format_bytes(model.size_bytes)}"
            self.model_table.setItem(row, 3, self._make_item(size))
        header = self.model_table.horizontalHeader()
        header.setStretchLastSection(False)
        header.setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        for column in (1, 2, 3):
            header.setSectionResizeMode(column, QHeaderView.ResizeMode.ResizeToContents)
        if self.model_table.rowCount():
            target_row = 0
            for row, model in enumerate(self._row_models):
                if model.id == selected_id:
                    target_row = row
                    break
            self.model_table.selectRow(target_row)
        self.model_table.blockSignals(False)
        self._update_model_buttons()

    def _selected_model(self) -> SamModel | None:
        row = self.model_table.currentRow()
        if 0 <= row < len(getattr(self, "_row_models", [])):
            return self._row_models[row]
        return None

    def _selected_model_path(self) -> Path | None:
        model = self._selected_model()
        if model is None:
            return None
        return model_path(model, self.models_dir_edit.text().strip() or self._sam.models_dir)

    def _update_model_buttons(self) -> None:
        model = self._selected_model()
        path = self._selected_model_path()
        downloaded = bool(path and path.is_file())
        part = bool(path and path.with_name(path.name + ".part").is_file())
        busy = self._download_task is not None
        self.btn_download.setText("继续下载" if part else "下载选中")
        self.btn_download.setEnabled(model is not None and not downloaded and not busy)
        self.btn_cancel_download.setEnabled(busy)
        self.btn_delete_model.setEnabled(model is not None and (downloaded or part) and not busy)
        self.btn_use_model.setEnabled(model is not None and downloaded and not busy)
        self.btn_import_model.setEnabled(not busy)
        self.btn_model_page.setEnabled(model is not None and bool(model.page_url) and not busy)
        self.btn_refresh_models.setEnabled(not busy)
        self.current_model_label.setText(self._current_model_text())
        if model is not None and model.gated and not downloaded and not busy:
            current = self.model_status.text()
            if (
                not current
                or current.startswith("选择模型后可下载")
                or current.startswith(f"{model.name}：")
                or current.startswith("已设为当前模型")
            ):
                token = os.environ.get("HF_TOKEN") or os.environ.get("HUGGING_FACE_HUB_TOKEN")
                hint = (
                    "已检测到 HF_TOKEN，可直接下载。"
                    if token
                    else "需先在权重页面申请访问，并设置环境变量 HF_TOKEN 后下载；也可手动下载后点「导入本地权重…」。"
                )
                self.model_status.setText(f"{model.name}：{hint}")

    def _open_model_page(self) -> None:
        model = self._selected_model()
        if model is not None and model.page_url:
            QDesktopServices.openUrl(QUrl(model.page_url))

    def _current_model_text(self) -> str:
        if not self._checkpoint:
            return "当前模型：未选择"
        entry = model_for_checkpoint(self._checkpoint, self._model_id)
        name = entry.name if entry is not None else Path(self._checkpoint).name
        return f"当前模型：{name}\n{self._checkpoint}"

    def _download_selected(self) -> None:
        if self._download_task is not None:
            return
        model = self._selected_model()
        path = self._selected_model_path()
        if model is None or path is None:
            return
        if path.is_file():
            self.model_status.setText("模型已存在")
            return
        task = ModelDownloadTask(
            model.url,
            path,
            parent=self,
            headers=resolve_download_headers(model.url),
        )
        task.progress.connect(self._on_download_progress)
        task.log.connect(self.model_status.setText)
        task.finished.connect(self._on_download_finished)
        self._download_task = task
        self.model_progress.setVisible(True)
        self.model_progress.setRange(0, 0)
        self.model_status.setText(f"准备下载 {model.filename}…")
        self._refresh_models()
        task.start()

    def _on_download_progress(self, done: int, total: int) -> None:
        if total > 0:
            self.model_progress.setRange(0, 1000)
            self.model_progress.setValue(int(max(0, min(total, done)) * 1000 / total))
            percent = done * 100.0 / total
            self.model_status.setText(f"正在下载：{format_bytes(done)} / {format_bytes(total)}（{percent:.1f}%）")
        else:
            self.model_progress.setRange(0, 0)
            self.model_status.setText(f"正在下载：{format_bytes(done)}")

    def _on_download_finished(self, ok: bool, message: str) -> None:
        self._download_task = None
        self.model_progress.setVisible(False)
        self.model_progress.setRange(0, 0)
        if ok:
            self.model_status.setText(f"下载完成：{Path(message).name}")
        else:
            self.model_status.setText(f"下载未完成：{message}")
        self._refresh_models()

    def _cancel_download(self) -> None:
        if self._download_task is not None:
            self.model_status.setText("正在取消下载…")
            self._download_task.cancel()
            self.btn_cancel_download.setEnabled(False)

    def _delete_selected(self) -> None:
        model = self._selected_model()
        path = self._selected_model_path()
        if model is None or path is None:
            return
        reply = QMessageBox.question(
            self,
            "删除模型",
            f"确定删除 {model.name}？\n\n{path}",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if reply != QMessageBox.StandardButton.Yes:
            return
        removed = False
        for target in (path, path.with_name(path.name + ".part")):
            try:
                target.unlink(missing_ok=True)
                removed = True
            except OSError as exc:
                QMessageBox.warning(self, "删除模型", f"删除失败：{exc}")
                return
        if _same_path(self._checkpoint, path):
            self._set_checkpoint("", "", "", "auto")
        self.model_status.setText("已删除" if removed else "文件不存在")
        self._refresh_models()

    def _use_selected(self) -> None:
        model = self._selected_model()
        path = self._selected_model_path()
        if model is None or path is None or not path.is_file():
            return
        model_type = model.model_type if model.family == "sam1" else "auto"
        self._set_checkpoint(str(path), model.id, model.config, model_type)
        self.model_status.setText(f"已设为当前模型：{model.name}")

    def _import_weights(self) -> None:
        start = self.models_dir_edit.text().strip() or self._sam.models_dir
        path, _ = QFileDialog.getOpenFileName(self, "导入 SAM 权重", start, "PyTorch 权重 (*.pth *.pt);;所有文件 (*)")
        if not path:
            return
        entry = model_for_checkpoint(path)
        if entry is not None:
            model_type = entry.model_type if entry.family == "sam1" else "auto"
            self._set_checkpoint(path, entry.id, entry.config, model_type)
            self.model_status.setText(f"已导入：{entry.name}")
            return
        family = infer_family(path)
        config = sam2_config_for_filename(path) if family == "sam2" else ""
        self._set_checkpoint(path, "", config, "auto")
        if family == "sam2" and not config:
            QMessageBox.warning(
                self,
                "导入 SAM 权重",
                "无法从文件名推断 SAM2 配置。\n"
                "请把权重改名为 sam2.1_hiera_*.pt 之类的标准名称，"
                "或在「模型管理」下载注册表模型。",
            )
        self.model_status.setText(f"已导入外部权重：{Path(path).name}")

    def _set_checkpoint(self, checkpoint: str, model_id: str, sam2_config: str, model_type: str) -> None:
        self._checkpoint = str(checkpoint or "")
        self._model_id = str(model_id or "")
        self._sam2_config = str(sam2_config or "")
        self._model_type = str(model_type or "auto")
        save_sam_setting(SETTING_CHECKPOINT, self._checkpoint)
        save_sam_setting(SETTING_MODEL_ID, self._model_id)
        save_sam_setting(SETTING_SAM2_CONFIG, self._sam2_config)
        save_sam_setting(SETTING_MODEL_TYPE, self._model_type)
        self.current_model_label.setText(self._current_model_text())
        self._refresh_models()

    # ================================================================== 缓存页
    def _build_cache_tab(self) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setSpacing(8)

        group = QGroupBox("SAM 图像特征缓存")
        group_layout = QVBoxLayout(group)
        self.cache_enabled_check = QCheckBox("启用图像特征缓存（同一影像再次打开时跳过编码器）")
        self.cache_enabled_check.setChecked(self._sam.cache_enabled)
        self.cache_enabled_check.toggled.connect(self._on_cache_enabled_changed)
        group_layout.addWidget(self.cache_enabled_check)

        dir_row = QHBoxLayout()
        dir_row.addWidget(QLabel("缓存目录："))
        self.cache_dir_edit = QLineEdit(self._sam.cache_dir)
        self.cache_dir_edit.editingFinished.connect(self._on_cache_dir_changed)
        self.btn_cache_dir = QToolButton()
        self.btn_cache_dir.setText("浏览…")
        self.btn_cache_dir.clicked.connect(self._browse_cache_dir)
        self.btn_open_cache_dir = QToolButton()
        self.btn_open_cache_dir.setText("打开")
        self.btn_open_cache_dir.clicked.connect(lambda: _open_directory(self.cache_dir_edit.text().strip()))
        dir_row.addWidget(self.cache_dir_edit, 1)
        dir_row.addWidget(self.btn_cache_dir)
        dir_row.addWidget(self.btn_open_cache_dir)
        group_layout.addLayout(dir_row)

        form = QFormLayout()
        self.cache_max_spin = QSpinBox()
        self.cache_max_spin.setRange(0, 102400)
        self.cache_max_spin.setSuffix(" MB")
        self.cache_max_spin.setSpecialValueText("不限制")
        self.cache_max_spin.setValue(int(self._sam.cache_max_mb))
        self.cache_max_spin.valueChanged.connect(lambda value: save_sam_setting(SETTING_CACHE_MAX_MB, int(value)))
        form.addRow("容量上限", self.cache_max_spin)
        group_layout.addLayout(form)

        self.cache_clear_on_close_check = QCheckBox("退出程序时清空缓存")
        self.cache_clear_on_close_check.setChecked(self._sam.cache_clear_on_close)
        self.cache_clear_on_close_check.setToolTip("只清空程序退出时；不勾选则缓存保留，下次启动仍可复用")
        self.cache_clear_on_close_check.toggled.connect(
            lambda checked: save_sam_setting(SETTING_CACHE_CLEAR_ON_CLOSE, bool(checked))
        )
        group_layout.addWidget(self.cache_clear_on_close_check)

        usage_row = QHBoxLayout()
        usage_row.addWidget(QLabel("当前占用："))
        self.cache_usage_label = QLabel("0 个文件 · 0 B")
        usage_row.addWidget(self.cache_usage_label, 1)
        self.btn_cache_refresh = QPushButton("刷新")
        self.btn_cache_refresh.clicked.connect(self._refresh_cache_usage)
        self.btn_cache_clear = QPushButton("清空缓存")
        self.btn_cache_clear.clicked.connect(self._clear_cache)
        usage_row.addWidget(self.btn_cache_refresh)
        usage_row.addWidget(self.btn_cache_clear)
        group_layout.addLayout(usage_row)
        layout.addWidget(group)

        hint = QLabel(
            "缓存键由「图像内容 + 模型身份（模型 id / 权重文件大小与修改时间）」组成，"
            "换模型不会命中旧缓存。缓存文件是 PyTorch 特征张量，单个 512×512 块通常几 MB。"
        )
        hint.setWordWrap(True)
        layout.addWidget(hint)
        layout.addStretch(1)
        return page

    def _on_cache_enabled_changed(self, checked: bool) -> None:
        save_sam_setting(SETTING_CACHE_ENABLED, bool(checked))

    def _on_cache_dir_changed(self) -> None:
        save_sam_setting(SETTING_CACHE_DIR, self.cache_dir_edit.text().strip())
        self._refresh_cache_usage()

    def _browse_cache_dir(self) -> None:
        path = QFileDialog.getExistingDirectory(self, "选择缓存目录", self.cache_dir_edit.text())
        if path:
            self.cache_dir_edit.setText(path)
            self._on_cache_dir_changed()

    def _refresh_cache_usage(self) -> None:
        count, size = cache_usage(self.cache_dir_edit.text().strip())
        self.cache_usage_label.setText(f"{count} 个文件 · {format_bytes(size)}")
        self.btn_cache_clear.setEnabled(count > 0)

    def _clear_cache(self) -> None:
        directory = self.cache_dir_edit.text().strip()
        count, size = cache_usage(directory)
        if count <= 0:
            self._refresh_cache_usage()
            return
        reply = QMessageBox.question(
            self,
            "清空缓存",
            f"删除 {count} 个缓存文件（{format_bytes(size)}）？\n\n{directory}",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if reply != QMessageBox.StandardButton.Yes:
            return
        removed, freed = clear_cache(directory)
        self.cache_usage_label.setText(f"已删除 {removed} 个文件 · {format_bytes(freed)}")
        self._refresh_cache_usage()

    # ================================================================== 帮助页
    def _build_help_tab(self) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)
        browser = QTextBrowser()
        browser.setOpenExternalLinks(True)
        browser.setHtml(
            """
            <h3>深度学习设置说明</h3>
            <p><b>依赖项</b>：标注 / 训练 / 推理都不在 QGIS 自带 Python 里运行，而是共用
            外部 Python（训练、推理也可在对应页面或 YAML 中指定独立解释器）。请选择一个
            独立 Python（SAM2 需要 Python 3.10 及以上，SAM3 建议 3.12 及以上），
            「安装缺失依赖」会执行 pip 安装标注 / 训练 / 推理所需的全部包：</p>
            <ul>
              <li>三者共用：<code>torch / torchvision / numpy / opencv-python</code>；</li>
              <li>仅标注：Meta 官方 <code>segment-anything</code>、<code>sam2</code>、<code>sam3</code> 源码包；</li>
              <li>训练 / 推理：<code>pillow</code>、<code>segmentation-models-pytorch</code>；</li>
              <li>仅推理：<code>rasterio&gt;=1.4</code>。</li>
            </ul>
            <p>PyTorch 可选 CPU 或 CUDA 12.4。安装日志会实时显示；取消只会终止当前 pip。
            SAM3 采用 <code>--no-deps</code> 安装再补齐依赖，避免其 numpy&lt;2 约束把
            Python 3.13 环境的 numpy 降级。</p>
            <p>若训练 / 推理使用了独立 Python，依赖页顶部会显示黄色提示；本页只检测和
            安装共享 Python 的依赖。可点「统一为共享 Python」清除独立设置，或继续在
            训练 / 推理页面用各自的「安装…依赖」快捷入口。</p>

            <p><b>模型管理</b>：SAM1 权重为 <code>.pth</code>，SAM2 / SAM2.1 / SAM3 / SAM3.1 为
            <code>.pt</code>。下载写入模型目录（支持断点续传），点击「使用该模型」后，
            在标注窗口点「加载模型」。外部权重可「导入本地权重…」，不会复制文件。</p>
            <p>SAM3 权重托管在 HuggingFace 受限仓库：先在「权重页面」申请访问，再设置环境变量
            <code>HF_TOKEN</code> 后下载；也可以手动下载 <code>sam3.pt</code> 后导入。
            SAM3 的点选 / 框选 / 涂鸦使用其交互式 Tracker；「自动分割」在 SAM3 下使用
            网格正点 + IoU 去重生成候选。</p>

            <p><b>缓存</b>：按图像内容与模型身份缓存 SAM 图像特征。缓存命中时跳过编码器，
            再次打开同一影像/宫格块会明显更快。缓存目录可以随时清空；勾选「退出程序时
            清空缓存」只影响程序退出。</p>

            <p><b>手动安装（离线/公司网络）</b>：在外层命令行执行：</p>
            <pre>python -m pip install torch torchvision --index-url https://download.pytorch.org/whl/cpu
python -m pip install numpy opencv-python pillow segmentation-models-pytorch "rasterio&gt;=1.4"
python -m pip install https://github.com/facebookresearch/segment-anything/archive/refs/heads/main.zip
python -m pip install https://github.com/facebookresearch/sam2/archive/refs/heads/main.zip
python -m pip install --no-deps https://github.com/facebookresearch/sam3/archive/refs/heads/main.zip
python -m pip install timm ftfy==6.1.1 regex iopath huggingface_hub tqdm typing_extensions einops pycocotools "setuptools&lt;81"
python -m pip install triton-windows  # 仅 Windows：sam3 顶层 import triton</pre>

            <p><b>注意</b>：SAM 权重不随安装包分发；首次加载大模型需要几十秒。
            SAM3 权重约 3.5 GB，官方建议 10 GB 以上显存；显存不足时换 Hiera-Tiny/Small、
            SAM3 以外的模型或 CPU。</p>
            """
        )
        layout.addWidget(browser)
        return page

    # ================================================================== 配置
    def config(self) -> DeepLearningConfig:
        """返回当前设置的完整内存快照。

        模型 / 缓存选项在控件改动时已写入 QSettings；这里再落一次共享 Python
        与 SAM 源码。训练 / 推理的 Python 只返回内存值，不写 QSettings，
        避免把回退结果固化（主窗口关闭后会重新加载权威配置）。
        """
        python = self.python_edit.text().strip()
        sam_src = self.sam_src_edit.text().strip()
        save_sam_setting(SETTING_PYTHON, python)
        save_sam_setting(SETTING_SAM_SRC, sam_src)
        self._sync_shared_python()
        self._update_effective_pythons()

        sam = replace(
            self._sam,
            python=python,
            sam_src=sam_src,
            checkpoint=self._checkpoint,
            model_type=self._model_type,
            device=self._sam.device,
            model_id=self._model_id,
            sam2_config=self._sam2_config,
            models_dir=self.models_dir_edit.text().strip() or self._sam.models_dir,
            cache_enabled=self.cache_enabled_check.isChecked(),
            cache_dir=self.cache_dir_edit.text().strip() or self._sam.cache_dir,
            cache_max_mb=int(self.cache_max_spin.value()),
            cache_clear_on_close=self.cache_clear_on_close_check.isChecked(),
        )
        return replace(
            self._config,
            sam=sam,
            training=replace(self._config.training, python=self._training_python),
            inference=replace(self._config.inference, python=self._inference_python),
        )

    # ================================================================== 生命周期
    def showEvent(self, event) -> None:
        super().showEvent(event)
        if not self._probed_once:
            self._probed_once = True
            QTimer.singleShot(0, self.refresh_dependencies)
        self._refresh_models()
        self._refresh_cache_usage()

    def reject(self) -> None:
        if not self._confirm_close():
            return
        self._shutdown_tasks()
        super().reject()

    def closeEvent(self, event) -> None:
        if not self._confirm_close():
            event.ignore()
            return
        self._shutdown_tasks()
        super().closeEvent(event)

    def _confirm_close(self) -> bool:
        if self._closing:
            return True
        install_running = self._installing or self.installer.is_running
        download_running = self._download_task is not None
        if not install_running and not download_running:
            return True
        reply = QMessageBox.question(
            self,
            "深度学习设置",
            "依赖安装或模型下载正在进行，关闭会取消任务。确定关闭？",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        return reply == QMessageBox.StandardButton.Yes

    def _shutdown_tasks(self) -> None:
        if self._closing:
            return
        self._closing = True
        self._stop_probe()
        if self.installer.is_running:
            self.installer.cancel()
        if self._download_task is not None:
            self._download_task.cancel()
            self._download_task.join(1.0)


__all__ = ["DeepLearningSettingsDialog"]
