"""深度学习训练工作区页面（嵌入主窗口中央区）。

用标注页产出的影像 + 掩膜训练模型：扫描标注成果 → 选任务 / 模型 / 参数 →
外部 Python 子进程执行训练 → 表格与日志实时回显。训练产物写到
``<输出目录>/run_YYYYmmdd_HHMMSS_<task>_<model>/``，包含 ``manifest.json``、
``train.log``、``metrics.json(l)``、``last.pt``、``best.pt``。

页面本身不 import torch；环境探测与训练都在外部 Python 里进行。
"""

from __future__ import annotations

import json
import subprocess
import time
from collections.abc import Callable
from dataclasses import replace
from datetime import datetime
from pathlib import Path

from qgis.PyQt.QtCore import QSize, Qt, QTimer, QUrl, pyqtSignal
from qgis.PyQt.QtGui import QAction, QDesktopServices
from qgis.PyQt.QtWidgets import (
    QAbstractItemView,
    QCheckBox,
    QComboBox,
    QDoubleSpinBox,
    QFileDialog,
    QFormLayout,
    QFrame,
    QGroupBox,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPlainTextEdit,
    QProgressBar,
    QPushButton,
    QScrollArea,
    QSpinBox,
    QSplitter,
    QStatusBar,
    QTableWidget,
    QTableWidgetItem,
    QToolBar,
    QVBoxLayout,
    QWidget,
)

from app.core.ai.sam_environment import DependencyInstaller, stripped_environ_dict
from app.core.training.client import TrainingClient
from app.core.training.dataset import DatasetScan, scan_dataset
from app.core.training.environment import (
    TrainingEnvironmentReport,
    build_install_commands,
    build_probe_command,
    parse_probe_output,
    python_problem,
)
from app.core.training.trainlib import catalog
from app.infra.deep_learning import (
    DeepLearningConfig,
    TrainingConfig,
    load_deep_learning_config,
    save_training_settings,
)
from app.infra.logging import get_logger
from app.ui.common import tool_icons
from app.ui.common.icons import Icons
from app.ui.common.theme import ThemeManager, toolbar_qss, window_qss

logger = get_logger(__name__)

EnvironmentProbe = Callable[[str], TrainingEnvironmentReport]

_PROGRESS_MAX = 1000
_LOG_MAX_BLOCKS = 2000


def probe_environment(python: str) -> TrainingEnvironmentReport:
    """用外部 Python 的 ``find_spec`` 探测训练依赖（不 import torch）。"""
    problem = python_problem(python)
    if problem:
        return TrainingEnvironmentReport(error=problem)
    try:
        completed = subprocess.run(
            build_probe_command(python),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=60,
            env=stripped_environ_dict(),
        )
    except (OSError, subprocess.SubprocessError) as exc:
        return TrainingEnvironmentReport(error=f"训练依赖探测失败：{exc}")
    error = ""
    if completed.returncode != 0:
        error = (completed.stderr or f"退出码 {completed.returncode}").strip()[-2000:]
    return parse_probe_output(completed.stdout or "", error=error)


class TrainingPage(QWidget):
    """训练工作区页面（主窗口单实例，切走页签时训练继续）。"""

    #: 用户点了「关闭」/ Ctrl+W：由主窗口切回地图页（页面状态保留）
    close_requested = pyqtSignal()

    def __init__(
        self,
        parent=None,
        *,
        config: DeepLearningConfig | None = None,
        client: TrainingClient | None = None,
        environment_probe: EnvironmentProbe | None = None,
    ) -> None:
        super().__init__(parent)
        self.setObjectName("TrainingPage")
        self.setWindowTitle("模型训练")
        self.setWindowIcon(Icons.app())

        self._config = config or load_deep_learning_config()
        self._scan: DatasetScan | None = None
        self._run_dir: Path | None = None
        self._training = False
        self._loading = True
        self._shutdown_done = False
        self._shutting_down = False
        self._environment_probe = environment_probe or probe_environment
        self._tool_actions: list[tuple[QAction, str]] = []

        self.client = client or TrainingClient(python=self._python(), parent=self)
        self._installer = DependencyInstaller(self)

        self._build_container()
        self._build_toolbar()
        self._build_body()
        self._load_config()
        self._wire()
        self._apply_theme_qss()
        self._update_model_options()
        self._update_action_state()
        self._status.showMessage("就绪")

    # ------------------------------------------------------------------ 配置
    @property
    def scan(self) -> DatasetScan | None:
        return self._scan

    @property
    def run_dir(self) -> Path | None:
        return self._run_dir

    def _training_config(self) -> TrainingConfig:
        return self._config.training

    def _python(self) -> str:
        return self._training_config().python or self._config.sam.python

    def apply_environment_python(self, python: str) -> None:
        """应用设置中心里的训练解释器；运行中的进程不受影响。"""
        python = str(python or "")
        self._config = replace(
            self._config,
            training=replace(self._config.training, python=python),
        )
        self.client.python = python
        running = bool(getattr(self, "_training", False) or self.client.is_running)
        self._append_log(
            "训练外部 Python 已更新，下次训练生效" if running else "训练外部 Python 已更新"
        )

    # ------------------------------------------------------------------ 装配
    def _build_container(self) -> None:
        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)

        self._toolbar = QToolBar("训练工具", self)
        self._toolbar.setObjectName("TrainingToolbar")
        self._toolbar.setMovable(False)
        self._toolbar.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextBesideIcon)
        self._toolbar.setIconSize(QSize(20, 20))
        root.addWidget(self._toolbar)

        self._splitter = QSplitter(Qt.Orientation.Horizontal, self)
        self._splitter.setChildrenCollapsible(False)
        root.addWidget(self._splitter, 1)

        self._status = QStatusBar(self)
        root.addWidget(self._status)

    def statusBar(self) -> QStatusBar:  # noqa: N802 —— 兼容 QMainWindow API
        return self._status

    def _build_toolbar(self) -> None:
        self.act_choose_dataset = QAction("选择标注成果…", self)
        self.act_choose_dataset.setToolTip("选择标注页保存影像 / 掩膜的根目录")
        self.act_choose_dataset.triggered.connect(self._choose_dataset)
        self._add_tool_action(self.act_choose_dataset, "folder_open")

        self.act_rescan = QAction("重新扫描", self)
        self.act_rescan.setToolTip("重新扫描目录，更新样本与类别")
        self.act_rescan.triggered.connect(self._rescan)
        self._add_tool_action(self.act_rescan, "refresh")

        self.act_open_output = QAction("打开输出目录", self)
        self.act_open_output.setToolTip("在文件管理器中打开当前输出目录")
        self.act_open_output.triggered.connect(self._open_output_dir)
        self._add_tool_action(self.act_open_output, "db_file")

        self._toolbar.addSeparator()

        self.act_start = QAction("开始训练", self)
        self.act_start.setToolTip("按当前参数启动训练")
        self.act_start.triggered.connect(self._start_training)
        self._add_tool_action(self.act_start, "play")

        self.act_stop = QAction("停止训练", self)
        self.act_stop.setToolTip("当前轮结束后停止，并保留 last.pt")
        self.act_stop.triggered.connect(self._stop_training)
        self.act_stop.setEnabled(False)
        self._add_tool_action(self.act_stop, "clear_selection")

        self._toolbar.addSeparator()

        self.act_install = QAction("安装训练依赖", self)
        self.act_install.setToolTip("为外部 Python 安装缺失的训练依赖（pip）")
        self.act_install.triggered.connect(self._install_dependencies)
        self._add_tool_action(self.act_install, "offline_download")

    def _add_tool_action(self, action: QAction, icon_key: str) -> None:
        self._toolbar.addAction(action)
        self._tool_actions.append((action, icon_key))

    def _build_body(self) -> None:
        self.form_body = self._build_form()
        self._splitter.addWidget(self.form_body)

        right = QWidget(self)
        layout = QVBoxLayout(right)
        layout.setContentsMargins(8, 8, 8, 8)
        layout.setSpacing(8)

        status_group = QGroupBox("训练状态", right)
        status_layout = QVBoxLayout(status_group)
        self.status_label = QLabel("就绪")
        self.status_label.setWordWrap(True)
        self.status_label.setObjectName("TrainingStatusLabel")
        status_layout.addWidget(self.status_label)

        self.progress = QProgressBar(status_group)
        self.progress.setRange(0, _PROGRESS_MAX)
        self.progress.setValue(0)
        self.progress.setFormat("%p%")
        status_layout.addWidget(self.progress)

        self.detail_label = QLabel("等待开始训练")
        self.detail_label.setWordWrap(True)
        status_layout.addWidget(self.detail_label)

        self.run_label = QLabel("输出：—")
        self.run_label.setWordWrap(True)
        self.run_label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        status_layout.addWidget(self.run_label)
        layout.addWidget(status_group)

        self.table = QTableWidget(0, 6, right)
        self.table.setObjectName("TrainingMetricsTable")
        self.table.setHorizontalHeaderLabels(
            ["轮次", "训练损失", "验证损失", "验证指标", "学习率", "用时"]
        )
        self.table.verticalHeader().setVisible(False)
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Stretch)
        layout.addWidget(self.table, 3)

        log_group = QGroupBox("训练日志", right)
        log_layout = QVBoxLayout(log_group)
        self.log_view = QPlainTextEdit(log_group)
        self.log_view.setReadOnly(True)
        self.log_view.setMaximumBlockCount(_LOG_MAX_BLOCKS)
        self.log_view.setPlaceholderText("训练过程日志会显示在这里")
        log_layout.addWidget(self.log_view)
        layout.addWidget(log_group, 2)

        self._splitter.addWidget(right)
        self._splitter.setStretchFactor(0, 0)
        self._splitter.setStretchFactor(1, 1)
        self._splitter.setSizes([390, 900])

    def _build_form(self) -> QWidget:
        scroll = QScrollArea(self)
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        body = QWidget(scroll)
        layout = QVBoxLayout(body)
        layout.setContentsMargins(8, 8, 8, 8)
        layout.setSpacing(8)

        layout.addWidget(self._build_dataset_group(body))
        layout.addWidget(self._build_task_group(body))
        layout.addWidget(self._build_params_group(body))
        layout.addWidget(self._build_output_group(body))
        layout.addStretch(1)
        scroll.setWidget(body)
        return scroll

    def _build_dataset_group(self, parent: QWidget) -> QGroupBox:
        group = QGroupBox("数据集", parent)
        form = QFormLayout(group)

        self.dataset_edit = QLineEdit(group)
        self.dataset_edit.setPlaceholderText("标注页的输出目录（含 *_mask.png / *_grid）")
        browse = QPushButton("浏览…", group)
        browse.clicked.connect(self._choose_dataset)
        row = QHBoxLayout()
        row.addWidget(self.dataset_edit, 1)
        row.addWidget(browse)
        form.addRow("标注目录", row)

        self.summary_label = QLabel("尚未扫描")
        self.summary_label.setWordWrap(True)
        form.addRow("扫描结果", self.summary_label)

        self.classes_label = QLabel("—")
        self.classes_label.setWordWrap(True)
        form.addRow("类别", self.classes_label)

        self.skipped_label = QLabel("")
        self.skipped_label.setWordWrap(True)
        self.skipped_label.setVisible(False)
        form.addRow("跳过", self.skipped_label)
        return group

    def _build_task_group(self, parent: QWidget) -> QGroupBox:
        group = QGroupBox("训练任务", parent)
        form = QFormLayout(group)

        self.task_combo = QComboBox(group)
        for task_id, label in catalog.TASKS:
            self.task_combo.addItem(label, task_id)
        form.addRow("任务", self.task_combo)

        self.model_combo = QComboBox(group)
        form.addRow("模型", self.model_combo)

        self.encoder_combo = QComboBox(group)
        form.addRow("骨干", self.encoder_combo)

        self.loss_combo = QComboBox(group)
        for loss_id, label in catalog.SEMANTIC_LOSSES:
            self.loss_combo.addItem(label, loss_id)
        form.addRow("损失", self.loss_combo)

        self.model_note = QLabel("")
        self.model_note.setWordWrap(True)
        self.model_note.setEnabled(False)
        form.addRow("", self.model_note)

        self.pretrained_check = QCheckBox("加载预训练骨干", group)
        form.addRow(self.pretrained_check)
        self.augment_check = QCheckBox("数据增强（翻转 / 旋转）", group)
        form.addRow(self.augment_check)
        return group

    def _build_params_group(self, parent: QWidget) -> QGroupBox:
        group = QGroupBox("训练参数", parent)
        form = QFormLayout(group)

        self.epochs_spin = self._int_spin(group, 1, 10000, 30)
        form.addRow("轮数", self.epochs_spin)

        self.batch_spin = self._int_spin(group, 1, 1024, 4)
        form.addRow("批大小", self.batch_spin)

        self.lr_spin = QDoubleSpinBox(group)
        self.lr_spin.setDecimals(7)
        self.lr_spin.setRange(0.0000001, 1.0)
        self.lr_spin.setSingleStep(0.0001)
        self.lr_spin.setValue(0.0001)
        form.addRow("学习率", self.lr_spin)

        self.tile_spin = self._int_spin(group, 64, 4096, 512)
        self.tile_spin.setSingleStep(64)
        self.tile_spin.setSuffix(" px")
        form.addRow("图像尺寸", self.tile_spin)

        self.val_spin = self._int_spin(group, 0, 90, 20)
        self.val_spin.setSuffix(" %")
        form.addRow("验证比例", self.val_spin)

        self.early_spin = self._int_spin(group, 0, 1000, 8)
        form.addRow("早停轮数", self.early_spin)

        self.seed_spin = self._int_spin(group, 0, 2**31 - 1, 42)
        form.addRow("随机种子", self.seed_spin)

        self.device_combo = QComboBox(group)
        self.device_combo.addItem("自动", "auto")
        self.device_combo.addItem("CUDA", "cuda")
        self.device_combo.addItem("CPU", "cpu")
        form.addRow("设备", self.device_combo)

        self.min_area_spin = self._int_spin(group, 0, 1_000_000, 16)
        form.addRow("实例最小面积", self.min_area_spin)
        return group

    def _build_output_group(self, parent: QWidget) -> QGroupBox:
        group = QGroupBox("输出", parent)
        form = QFormLayout(group)

        self.output_edit = QLineEdit(group)
        self.output_edit.setPlaceholderText("留空 = <标注目录>/training_runs")
        browse = QPushButton("浏览…", group)
        browse.clicked.connect(self._choose_output_dir)
        row = QHBoxLayout()
        row.addWidget(self.output_edit, 1)
        row.addWidget(browse)
        form.addRow("输出目录", row)

        self.checkpoint_edit = QLineEdit(group)
        self.checkpoint_edit.setPlaceholderText("可选：上一次训练的 best.pt / last.pt")
        checkpoint_browse = QPushButton("浏览…", group)
        checkpoint_browse.clicked.connect(self._choose_checkpoint)
        checkpoint_row = QHBoxLayout()
        checkpoint_row.addWidget(self.checkpoint_edit, 1)
        checkpoint_row.addWidget(checkpoint_browse)
        form.addRow("继续训练", checkpoint_row)

        hint = QLabel("每次训练生成 run_日期_时间_任务_模型 子目录。")
        hint.setWordWrap(True)
        hint.setEnabled(False)
        form.addRow("", hint)
        return group

    @staticmethod
    def _int_spin(parent: QWidget, minimum: int, maximum: int, value: int) -> QSpinBox:
        spin = QSpinBox(parent)
        spin.setRange(minimum, maximum)
        spin.setValue(value)
        return spin

    # ------------------------------------------------------------------ 配置读写
    def _load_config(self) -> None:
        config = self._training_config()
        self._loading = True
        try:
            self.dataset_edit.setText(config.dataset_dir)
            self.output_edit.setText(config.output_dir)
            self.checkpoint_edit.setText(config.init_checkpoint)

            index = self.task_combo.findData(config.task)
            self.task_combo.setCurrentIndex(index if index >= 0 else 0)
            task = self.current_task()
            self._populate_models(task, config.model, config.encoder)
            self._select_combo(self.loss_combo, config.loss if catalog.is_semantic_task(task) else "")

            self.epochs_spin.setValue(config.epochs)
            self.batch_spin.setValue(config.batch_size)
            self.lr_spin.setValue(config.lr)
            self.tile_spin.setValue(config.tile_size)
            self.val_spin.setValue(config.val_percent)
            self.early_spin.setValue(config.early_stop)
            self.seed_spin.setValue(config.seed)
            self._select_combo(self.device_combo, config.device)
            self.pretrained_check.setChecked(config.pretrained)
            self.augment_check.setChecked(config.augment)
            self.min_area_spin.setValue(config.min_instance_area)
        finally:
            self._loading = False

    def _form_values(self) -> dict:
        model = self.current_model()
        task = self.current_task()
        return {
            "dataset_dir": self.dataset_edit.text().strip(),
            "output_dir": self.output_edit.text().strip(),
            "task": task,
            "model": model.id if model else "",
            "encoder": self.encoder_combo.currentData() or "",
            "loss": (self.loss_combo.currentData() or "") if catalog.is_semantic_task(task) else "",
            "epochs": self.epochs_spin.value(),
            "batch_size": self.batch_spin.value(),
            "lr": self.lr_spin.value(),
            "tile_size": self.tile_spin.value(),
            "val_percent": self.val_spin.value(),
            "early_stop": self.early_spin.value(),
            "seed": self.seed_spin.value(),
            "device": self.device_combo.currentData() or "auto",
            "pretrained": bool(self.pretrained_check.isChecked() and model and model.pretrained_supported),
            "augment": self.augment_check.isChecked(),
            "min_instance_area": self.min_area_spin.value(),
            "init_checkpoint": self.checkpoint_edit.text().strip(),
        }

    def _save_settings(self) -> None:
        try:
            save_training_settings(self._form_values())
        except Exception:
            logger.exception("保存训练设置失败")

    # ------------------------------------------------------------------ 下拉框
    def current_task(self) -> str:
        return str(self.task_combo.currentData() or catalog.TASK_IDS[0])

    def current_model(self):
        model_id = str(self.model_combo.currentData() or "")
        return catalog.get_model(model_id)

    def _select_combo(self, combo: QComboBox, value: str) -> None:
        index = combo.findData(value)
        combo.setCurrentIndex(index if index >= 0 else 0)

    def _populate_models(self, task: str, model_id: str = "", encoder_id: str = "") -> None:
        models = catalog.models_for_task(task)
        normalized, encoder = catalog.normalize_for_task(task, model_id, encoder_id)
        blocked = self.model_combo.blockSignals(True)
        try:
            self.model_combo.clear()
            for model in models:
                self.model_combo.addItem(model.name, model.id)
            self._select_combo(self.model_combo, normalized)
        finally:
            self.model_combo.blockSignals(blocked)
        self._populate_encoders(encoder)

    def _populate_encoders(self, encoder_id: str = "") -> None:
        model = self.current_model()
        blocked = self.encoder_combo.blockSignals(True)
        try:
            self.encoder_combo.clear()
            if model is not None:
                for encoder in model.encoders:
                    self.encoder_combo.addItem(encoder.name, encoder.id)
                preferred = encoder_id if encoder_id in model.encoder_ids() else model.default_encoder
                self._select_combo(self.encoder_combo, preferred)
        finally:
            self.encoder_combo.blockSignals(blocked)

    def _on_task_changed(self, _index: int = -1) -> None:
        if self._loading:
            return
        task = self.current_task()
        model_id, encoder = catalog.normalize_for_task(task, "", "")
        self._populate_models(task, model_id, encoder)
        self._update_model_options()

    def _on_model_changed(self, _index: int = -1) -> None:
        if self._loading:
            return
        self._populate_encoders()
        self._update_model_options()

    def _update_model_options(self) -> None:
        model = self.current_model()
        task = self.current_task()
        if model is not None:
            note = model.note
            if not model.pretrained_supported:
                note = (note + " " if note else "") + "当前模型没有可用的预训练权重。"
            self.model_note.setText(note)
            self.pretrained_check.setEnabled(model.pretrained_supported)
            self.pretrained_check.setToolTip(
                "" if model.pretrained_supported else "该模型没有官方预训练权重"
            )
        else:
            self.model_note.setText("")
            self.pretrained_check.setEnabled(False)
        semantic = catalog.is_semantic_task(task)
        self.loss_combo.setEnabled(semantic)
        self.min_area_spin.setEnabled(not semantic)

    # ------------------------------------------------------------------ 扫描
    def set_dataset_hint(self, directory: str) -> None:
        """主窗口打开页面时带入标注输出目录（仅在用户尚未填写时）。"""
        directory = str(directory or "").strip()
        if not directory:
            return
        if not self.dataset_edit.text().strip():
            self.dataset_edit.setText(directory)
        if self._scan is None:
            QTimer.singleShot(0, self._rescan)

    def activate(self) -> None:
        if self._scan is None and self.dataset_edit.text().strip():
            QTimer.singleShot(0, self._rescan)

    def _choose_dataset(self) -> None:
        start = self.dataset_edit.text().strip() or str(Path.home())
        directory = QFileDialog.getExistingDirectory(self, "选择标注成果目录", start)
        if not directory:
            return
        self.dataset_edit.setText(directory)
        self._rescan()

    def _rescan(self) -> DatasetScan:
        directory = self.dataset_edit.text().strip()
        scan = scan_dataset(directory)
        self._scan = scan
        self.summary_label.setText(scan.summary())
        names = [str(item.get("name") or "") for item in scan.classes if item.get("name")]
        self.classes_label.setText(" / ".join(names) if names else "—")
        if scan.skipped:
            shown = "；".join(scan.skipped[:5])
            if len(scan.skipped) > 5:
                shown += f"；…… 共 {len(scan.skipped)} 项"
            self.skipped_label.setText(shown)
        self.skipped_label.setVisible(bool(scan.skipped))
        self._update_action_state()
        return scan

    # ------------------------------------------------------------------ 目录
    def _choose_output_dir(self) -> None:
        start = self._resolve_output_base()
        directory = QFileDialog.getExistingDirectory(self, "选择训练输出目录", str(start))
        if directory:
            self.output_edit.setText(directory)
            self._update_action_state()

    def _choose_checkpoint(self) -> None:
        start = self.checkpoint_edit.text().strip() or str(self._resolve_output_base())
        path, _ = QFileDialog.getOpenFileName(
            self, "选择继续训练的权重", start, "PyTorch 权重 (*.pt *.pth);;所有文件 (*)"
        )
        if path:
            self.checkpoint_edit.setText(path)

    def _resolve_output_base(self) -> Path:
        text = self.output_edit.text().strip()
        if text:
            return Path(text)
        dataset = self.dataset_edit.text().strip()
        if dataset:
            return Path(dataset) / "training_runs"
        return Path.cwd() / "training_runs"

    def _open_output_dir(self) -> None:
        base = self._resolve_output_base()
        directory = self._run_dir if self._run_dir is not None and self._run_dir.is_dir() else base
        try:
            directory.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            QMessageBox.warning(self, "打开输出目录", f"无法创建目录：{exc}")
            return
        QDesktopServices.openUrl(QUrl.fromLocalFile(str(directory)))

    def _make_run_dir(self) -> Path:
        base = self._resolve_output_base()
        try:
            base.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            raise ValueError(f"无法创建输出目录：{exc}") from exc
        model = self.current_model()
        model_id = model.id if model else "model"
        stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        name = f"run_{stamp}_{self.current_task()}_{model_id}"
        candidate = base / name
        counter = 2
        while candidate.exists():
            candidate = base / f"{name}_{counter}"
            counter += 1
        try:
            candidate.mkdir()
        except OSError as exc:
            raise ValueError(f"无法创建训练目录：{exc}") from exc
        return candidate

    # ------------------------------------------------------------------ 训练
    def _build_manifest(self, run_dir: Path) -> dict:
        scan = self._scan if self._scan is not None else self._rescan()
        values = self._form_values()
        task = values["task"]
        params = {
            "epochs": values["epochs"],
            "batch_size": values["batch_size"],
            "lr": values["lr"],
            "tile_size": values["tile_size"],
            "val_percent": values["val_percent"],
            "early_stop": values["early_stop"],
            "seed": values["seed"],
            "device": values["device"],
            "pretrained": values["pretrained"],
            "augment": values["augment"],
            "loss": values["loss"] if catalog.is_semantic_task(task) else "",
            "min_instance_area": values["min_instance_area"],
            "init_checkpoint": values["init_checkpoint"],
            "amp": True,
        }
        roots = scan.roots or ([self.dataset_edit.text().strip()] if self.dataset_edit.text().strip() else [])
        return {
            "version": 1,
            "task": task,
            "model": values["model"],
            "encoder": values["encoder"],
            "params": params,
            "dataset": {
                "root": roots[0] if roots else "",
                "classes": [dict(item) for item in scan.classes],
                "samples": [sample.as_dict() for sample in scan.samples],
            },
            "output_dir": str(run_dir),
        }

    def _start_training(self) -> None:
        if self._training or self.client.is_running:
            return
        scan = self._rescan()
        if not scan.samples:
            QMessageBox.warning(self, "开始训练", "当前目录没有可用的影像 / 掩膜，请先选择标注成果目录。")
            return
        checkpoint = self.checkpoint_edit.text().strip()
        if checkpoint and not Path(checkpoint).is_file():
            QMessageBox.warning(self, "开始训练", f"继续训练的权重不存在：\n{checkpoint}")
            return
        python = self._python()
        report = self._check_environment(python)
        if report.error:
            QMessageBox.warning(self, "开始训练", f"无法检查训练环境：\n{report.error}")
            return
        if report.missing:
            titles = "、".join(item.title for item in report.missing)
            QMessageBox.warning(
                self,
                "开始训练",
                f"外部 Python 缺少训练依赖：{titles}\n\n"
                f"请点工具栏「安装训练依赖」，或手动执行：\n"
                f"{python} -m pip install segmentation-models-pytorch",
            )
            return
        try:
            run_dir = self._make_run_dir()
        except ValueError as exc:
            QMessageBox.warning(self, "开始训练", str(exc))
            return
        manifest = self._build_manifest(run_dir)
        manifest_path = run_dir / "manifest.json"
        try:
            manifest_path.write_text(
                json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8"
            )
        except OSError as exc:
            QMessageBox.warning(self, "开始训练", f"写入训练清单失败：{exc}")
            return
        self._save_settings()
        self._run_dir = run_dir
        self.run_label.setText(f"输出：{run_dir}")
        self._clear_run_views()
        self.client.python = python
        self._append_log(f"训练清单：{manifest_path}")
        if not self.client.start(manifest_path):
            self._append_log("训练进程启动失败")
            self._update_action_state()
            return
        self._set_training(True)
        self.status_label.setText("正在启动训练…")
        self.detail_label.setText("等待训练进程就绪…")
        self._status.showMessage("训练已启动")

    def _check_environment(self, python: str) -> TrainingEnvironmentReport:
        try:
            return self._environment_probe(python)
        except Exception as exc:  # noqa: BLE001 —— 探测失败不应让页面崩溃
            return TrainingEnvironmentReport(error=str(exc))

    def _stop_training(self) -> None:
        if not self.client.is_running:
            return
        answer = QMessageBox.question(
            self,
            "停止训练",
            "确定停止训练吗？\n当前轮结束后会保存 last.pt，已完成轮次不会丢失。",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if answer != QMessageBox.StandardButton.Yes:
            return
        self.client.stop(graceful=True)
        self._append_log("已请求停止训练，等待当前轮结束…")
        self.status_label.setText("正在停止训练…")
        self.act_stop.setEnabled(False)

    def _install_dependencies(self) -> None:
        python = self._python()
        problem = python_problem(python)
        if problem:
            QMessageBox.warning(self, "安装训练依赖", problem)
            return
        report = self._check_environment(python)
        if report.error:
            QMessageBox.warning(self, "安装训练依赖", f"无法检查训练环境：\n{report.error}")
            return
        missing = report.missing_keys
        if not missing:
            QMessageBox.information(
                self, "安装训练依赖", "训练依赖已齐全（torch / torchvision / numpy / OpenCV / Pillow / SMP）。"
            )
            return
        commands = build_install_commands(python, missing)
        if not commands:
            QMessageBox.information(self, "安装训练依赖", "没有需要安装的依赖。")
            return
        titles = "、".join(item.title for item in report.missing)
        answer = QMessageBox.question(
            self,
            "安装训练依赖",
            f"将为外部 Python 安装：{titles}\n\nPython：{python}\n"
            "安装过程需要联网，可能持续数分钟。是否继续？",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.Yes,
        )
        if answer != QMessageBox.StandardButton.Yes:
            return
        self._append_log(f"开始安装训练依赖：{titles}")
        self._installer.start_commands(commands)
        self._update_action_state()

    # ------------------------------------------------------------------ 子进程事件
    def _wire(self) -> None:
        self.client.hello.connect(lambda: self._append_log("训练进程已就绪"))
        self.client.log.connect(self._append_log)
        self.client.started.connect(self._on_started)
        self.client.progress.connect(self._on_progress)
        self.client.epoch_finished.connect(self._on_epoch)
        self.client.finished.connect(self._on_done)
        self.client.stopped.connect(self._on_stopped)
        self.client.failed.connect(self._on_failed)
        self.client.running_changed.connect(self._on_running_changed)
        self.client.status_changed.connect(self.status_label.setText)

        self._installer.log.connect(self._append_log)
        self._installer.finished.connect(self._on_install_finished)

        self.task_combo.currentIndexChanged.connect(self._on_task_changed)
        self.model_combo.currentIndexChanged.connect(self._on_model_changed)
        ThemeManager.instance().theme_changed.connect(self._apply_theme_qss)

    def _on_running_changed(self, running: bool) -> None:
        self._set_training(bool(running))
        if running:
            self.status_label.setText("训练进行中…")
        elif self.progress.value() < _PROGRESS_MAX:
            self.act_stop.setEnabled(False)

    def _set_training(self, active: bool) -> None:
        self._training = bool(active)
        self._update_action_state()

    def _update_action_state(self) -> None:
        installing = self._installer.is_running
        busy = self._training or self.client.is_running or installing
        has_samples = bool(self._scan and self._scan.samples)
        self.form_body.setEnabled(not busy)
        self.act_choose_dataset.setEnabled(not busy)
        self.act_rescan.setEnabled(not busy)
        self.act_install.setEnabled(not busy)
        self.act_start.setEnabled(not busy and has_samples)
        self.act_stop.setEnabled(self._training or self.client.is_running)

    def _clear_run_views(self) -> None:
        self.table.setRowCount(0)
        self.table.setHorizontalHeaderLabels(
            ["轮次", "训练损失", "验证损失", "验证指标", "学习率", "用时"]
        )
        self.progress.setValue(0)
        self.detail_label.setText("等待训练进程就绪…")
        self.log_view.clear()

    def _on_started(self, payload: dict) -> None:
        total = payload.get("epochs") or 0
        train = payload.get("train") or 0
        val = payload.get("val") or 0
        device = payload.get("device") or "?"
        classes = payload.get("classes") or []
        names = " / ".join(str(item.get("name") or "") for item in classes if isinstance(item, dict))
        self.progress.setValue(0)
        self.status_label.setText(
            f"训练进行中：{train} 个训练样本 / {val} 个验证样本 / {device}"
        )
        self.detail_label.setText(f"共 {total} 轮" + (f"；类别：{names}" if names else ""))
        self._append_log(
            f"训练开始：{total} 轮，训练样本 {train}，验证样本 {val}，设备 {device}"
        )

    def _on_progress(self, payload: dict) -> None:
        try:
            epoch = int(payload.get("epoch") or 1)
            total = max(1, int(payload.get("total") or 1))
            step = int(payload.get("step") or 0)
            steps = max(1, int(payload.get("steps") or 1))
            loss = payload.get("loss")
        except (TypeError, ValueError):
            return
        fraction = ((epoch - 1) + step / steps) / total
        self.progress.setValue(int(max(0.0, min(1.0, fraction)) * _PROGRESS_MAX))
        loss_text = f"{float(loss):.4f}" if loss is not None else "—"
        self.detail_label.setText(
            f"第 {epoch}/{total} 轮 · 步 {step}/{steps} · 当前损失 {loss_text}"
        )

    def _on_epoch(self, payload: dict) -> None:
        try:
            epoch = int(payload.get("epoch") or 0)
            total = max(1, int(payload.get("total") or 1))
        except (TypeError, ValueError):
            return
        label = str(payload.get("primary_label") or "验证指标")
        header_item = self.table.horizontalHeaderItem(3)
        if header_item is not None and label:
            header_item.setText(label)
        row = self.table.rowCount()
        self.table.insertRow(row)
        values = [
            f"{epoch}/{total}",
            self._format_number(payload.get("train_loss"), 4),
            self._format_number(payload.get("val_loss"), 4),
            self._format_number(payload.get("primary"), 4),
            self._format_number(payload.get("lr"), 8),
            self._format_seconds(payload.get("elapsed")),
        ]
        for column, text in enumerate(values):
            item = QTableWidgetItem(text)
            item.setTextAlignment(Qt.AlignmentFlag.AlignCenter)
            self.table.setItem(row, column, item)
        self.table.scrollToBottom()
        self.progress.setValue(int(_PROGRESS_MAX * min(1.0, epoch / total)))
        primary = self._format_number(payload.get("primary"), 4)
        self.detail_label.setText(f"第 {epoch}/{total} 轮完成 · {label} {primary}")
        self._append_log(
            f"第 {epoch}/{total} 轮：训练损失 {values[1]}，验证损失 {values[2]}，"
            f"{label} {primary}，用时 {values[5]}"
        )

    @staticmethod
    def _format_number(value, digits: int) -> str:
        if value is None:
            return "—"
        try:
            return f"{float(value):.{digits}f}"
        except (TypeError, ValueError):
            return str(value)

    @staticmethod
    def _format_seconds(value) -> str:
        try:
            seconds = max(0, int(float(value)))
        except (TypeError, ValueError):
            return "—"
        minutes, seconds = divmod(seconds, 60)
        hours, minutes = divmod(minutes, 60)
        if hours:
            return f"{hours}h{minutes:02d}m{seconds:02d}s"
        if minutes:
            return f"{minutes}m{seconds:02d}s"
        return f"{seconds}s"

    def _on_done(self, payload: dict) -> None:
        self._set_training(False)
        self.progress.setValue(_PROGRESS_MAX)
        best = str(payload.get("best") or "")
        metrics = str(payload.get("metrics") or "")
        self.status_label.setText("训练完成")
        self.detail_label.setText("训练完成")
        self._append_log(f"训练完成：best={best or '—'}，metrics={metrics or '—'}")
        self._status.showMessage("训练完成")
        lines = ["训练完成。", f"best.pt：{best or '—'}"]
        if metrics:
            lines.append(f"指标：{metrics}")
        if self._run_dir is not None:
            lines.append(f"输出目录：{self._run_dir}")
        QMessageBox.information(self, "训练完成", "\n".join(lines))

    def _on_stopped(self, payload: dict) -> None:
        self._set_training(False)
        epoch = payload.get("epoch") or 0
        self.status_label.setText("训练已停止")
        self.detail_label.setText(f"训练已停止（完成 {epoch} 轮）")
        self._append_log(f"训练已停止，已完成 {epoch} 轮，last.pt 已保留")
        self._status.showMessage("训练已停止")
        QMessageBox.information(
            self,
            "训练已停止",
            f"训练已停止（完成 {epoch} 轮）。\nlast.pt 已保存在输出目录中。",
        )

    def _on_failed(self, message: str, error_type: str) -> None:
        self._set_training(False)
        self.status_label.setText("训练失败")
        self._append_log(f"训练失败（{error_type}）：{message}")
        self._status.showMessage("训练失败")
        QMessageBox.critical(self, "训练失败", message)

    def _on_install_finished(self, ok: bool, code: int) -> None:
        self._update_action_state()
        if ok:
            self._append_log("训练依赖安装完成")
            self._status.showMessage("训练依赖安装完成")
        else:
            self._append_log(f"训练依赖安装结束（code={code}），请检查日志")
            self._status.showMessage("训练依赖安装未完成")

    def _append_log(self, text: str) -> None:
        text = str(text or "").rstrip()
        if not text:
            return
        stamp = time.strftime("%H:%M:%S")
        for line in text.splitlines():
            self.log_view.appendPlainText(f"[{stamp}] {line}")

    # ------------------------------------------------------------------ 主题
    def _apply_theme_qss(self, _theme=None) -> None:
        theme = ThemeManager.instance().current()
        self.setStyleSheet(
            window_qss(theme)
            + f"\n#TrainingPage {{ background-color: {theme.window_bg}; }}"
            + f"\n#TrainingStatusLabel {{ font-weight: 600; color: {theme.toolbar_text}; }}"
        )
        self._toolbar.setStyleSheet(toolbar_qss(theme))
        for action, key in self._tool_actions:
            action.setIcon(tool_icons.icon(key, theme.toolbar_text))

    # ------------------------------------------------------------------ 关闭
    def confirm_leave(self) -> bool:
        """离开页面时若训练仍在进行：确认后优雅停止（轮末保存 last.pt）。"""
        if not self.client.is_running:
            return True
        answer = QMessageBox.question(
            self,
            "训练进行中",
            "训练仍在进行，停止训练并离开吗？\n当前轮结束后会保存 last.pt。",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if answer != QMessageBox.StandardButton.Yes:
            return False
        self.client.stop(graceful=True)
        return True

    def _request_close(self) -> None:
        self.close_requested.emit()

    def closeEvent(self, event) -> None:
        # 页面由主窗口 QStackedWidget 管理：不自行隐藏，交给宿主切回地图页。
        event.ignore()
        self.close_requested.emit()

    def shutdown(self, confirm: bool = True) -> bool:
        """应用退出：保存设置 → 取消安装 → 终止训练进程（幂等）。"""
        if self._shutdown_done:
            return True
        if self._shutting_down:
            return False
        if self.client.is_running and confirm:
            answer = QMessageBox.question(
                self,
                "训练进行中",
                "训练仍在进行，退出将强制停止训练。是否退出？",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.No,
            )
            if answer != QMessageBox.StandardButton.Yes:
                return False
        self._shutting_down = True
        try:
            self._save_settings()
            try:
                self._installer.cancel()
            except Exception:
                logger.exception("取消训练依赖安装失败")
            try:
                self.client.shutdown()
            except Exception:
                logger.exception("停止训练子进程失败")
        finally:
            self._shutting_down = False
            self._shutdown_done = True
        return True


__all__ = ["TrainingPage", "probe_environment"]
