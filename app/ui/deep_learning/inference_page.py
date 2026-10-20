"""深度学习推理工作区页面（嵌入主窗口中央区）。

选择训练产出的 checkpoint 与新影像，用外部 Python 子进程做**重叠滑动窗口**
推理：左侧模型 / 参数，中间源影像 + 类别叠加预览，右侧状态 / 统计 / 日志。
产物为带调色板的类别 ID GeoTIFF 与 ``result.json``，不自动加载到地图。
"""

from __future__ import annotations

import json
import subprocess
import time
from collections.abc import Callable
from datetime import datetime
from pathlib import Path

from qgis.PyQt.QtCore import QSize, Qt, QTimer, QUrl, pyqtSignal
from qgis.PyQt.QtGui import QAction, QDesktopServices, QKeySequence
from qgis.PyQt.QtWidgets import (
    QAbstractItemView,
    QApplication,
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
    QSlider,
    QSpinBox,
    QSplitter,
    QStackedWidget,
    QStatusBar,
    QTableWidget,
    QTableWidgetItem,
    QToolBar,
    QVBoxLayout,
    QWidget,
)

from app.core.ai.sam_environment import DependencyInstaller, stripped_environ_dict
from app.core.inference.client import InferenceClient, inspect_checkpoint
from app.core.inference.environment import (
    INFERENCE_DEPENDENCIES,
    build_install_commands,
    build_probe_command,
    parse_probe_output,
)
from app.core.inference.runs import RunInfo, read_run, scan_runs
from app.core.training.environment import TrainingEnvironmentReport, python_problem
from app.core.training.trainlib import catalog
from app.infra.deep_learning import (
    DeepLearningConfig,
    InferenceConfig,
    load_deep_learning_config,
    save_inference_settings,
)
from app.infra.logging import get_logger
from app.ui.common import tool_icons
from app.ui.common.icons import Icons
from app.ui.common.theme import ThemeManager, toolbar_qss, window_qss
from app.ui.deep_learning.annotation_canvas import MODE_PAN, AnnotationView
from app.ui.deep_learning.annotation_model import AnnotationDocument, LabelSet
from app.ui.deep_learning.raster_io import (
    RASTER_FILTER,
    RasterInfo,
    default_bands,
    read_class_preview,
    read_preview,
    read_raster_info,
)

logger = get_logger(__name__)

EnvironmentProbe = Callable[[str], TrainingEnvironmentReport]

_PROGRESS_MAX = 1000
_LOG_MAX_BLOCKS = 2000


def probe_inference_environment(python: str) -> TrainingEnvironmentReport:
    """用外部 Python 的 ``find_spec`` 探测推理依赖（不 import torch）。"""
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
        return TrainingEnvironmentReport(error=f"推理依赖探测失败：{exc}")
    error = ""
    if completed.returncode != 0:
        error = (completed.stderr or f"退出码 {completed.returncode}").strip()[-2000:]
    return parse_probe_output(completed.stdout or "", error=error)


class InferencePage(QWidget):
    """推理工作区页面（主窗口单实例，切走页签时推理继续）。"""

    #: 用户点了「关闭」/ Ctrl+W：由主窗口切回地图页（页面状态保留）
    close_requested = pyqtSignal()

    def __init__(
        self,
        parent=None,
        *,
        config: DeepLearningConfig | None = None,
        client: InferenceClient | None = None,
        environment_probe: EnvironmentProbe | None = None,
    ) -> None:
        super().__init__(parent)
        self.setObjectName("InferencePage")
        self.setWindowTitle("模型推理")
        self.setWindowIcon(Icons.app())

        self._config = config or load_deep_learning_config()
        self._loading = True
        self._inference = False
        self._shutdown_done = False
        self._shutting_down = False
        self._environment_probe = environment_probe or probe_inference_environment
        self._tool_actions: list[tuple[QAction, str]] = []

        self._runs_by_checkpoint: dict[str, RunInfo] = {}
        self._inspect_cache: dict[str, dict] = {}
        self._checkpoint_info: dict = {}
        self._current_task = ""
        self._image_info: RasterInfo | None = None
        self._source_path = ""
        self._source_preview = None
        self._document: AnnotationDocument | None = None
        self._labels: LabelSet | None = None
        self._output_file = ""
        self._output_dir: Path | None = None
        self._updating_bands = False
        self._preview_timer: QTimer | None = None

        self.client = client or InferenceClient(python=self._python(), parent=self)
        self._installer = DependencyInstaller(self)

        self._build_container()
        self._build_toolbar()
        self._build_body()
        self._load_config()
        self._wire()
        self._apply_theme_qss()
        self._update_action_state()
        self._status.showMessage("就绪")

    # ------------------------------------------------------------------ 配置
    @property
    def inference_config(self) -> InferenceConfig:
        return self._config.inference

    def _python(self) -> str:
        return (
            self.inference_config.python
            or self._config.training.python
            or self._config.sam.python
        )

    # ------------------------------------------------------------------ 装配
    def _build_container(self) -> None:
        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)

        self._toolbar = QToolBar("推理工具", self)
        self._toolbar.setObjectName("InferenceToolbar")
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
        self.act_start = QAction("开始推理", self)
        self.act_start.setToolTip("按当前参数对整幅影像做滑动窗口推理")
        self.act_start.triggered.connect(self._start_inference)
        self._add_tool_action(self.act_start, "play")

        self.act_stop = QAction("停止推理", self)
        self.act_stop.setToolTip("当前窗口批次结束后停止，已写入的块保留")
        self.act_stop.triggered.connect(self._stop_inference)
        self.act_stop.setEnabled(False)
        self._add_tool_action(self.act_stop, "clear_selection")

        self.act_open_output = QAction("打开输出目录", self)
        self.act_open_output.setToolTip("在文件管理器中打开推理输出目录")
        self.act_open_output.triggered.connect(self._open_output_dir)
        self._add_tool_action(self.act_open_output, "db_file")

        self._toolbar.addSeparator()

        self.act_install = QAction("安装推理依赖", self)
        self.act_install.setToolTip("为外部 Python 安装缺失的推理依赖（含 Rasterio）")
        self.act_install.triggered.connect(self._install_dependencies)
        self._add_tool_action(self.act_install, "offline_download")

        self._toolbar.addSeparator()

        self.act_close = QAction("关闭", self)
        self.act_close.setShortcut(QKeySequence("Ctrl+W"))
        self.act_close.triggered.connect(self._request_close)
        # 关闭不配图标：工具箱里没有“关闭”语义的线性图标，菜单快捷键足够
        self._toolbar.addAction(self.act_close)

        for action in (
            self.act_start,
            self.act_stop,
            self.act_open_output,
            self.act_install,
            self.act_close,
        ):
            action.setShortcutContext(Qt.ShortcutContext.WidgetWithChildrenShortcut)
            self.addAction(action)

    def _add_tool_action(self, action: QAction, icon_key: str) -> None:
        self._toolbar.addAction(action)
        self._tool_actions.append((action, icon_key))

    def _build_body(self) -> None:
        self.form_body = self._build_form()
        self._splitter.addWidget(self.form_body)
        self._splitter.addWidget(self._build_preview())
        self._splitter.addWidget(self._build_result_panel())
        self._splitter.setStretchFactor(0, 0)
        self._splitter.setStretchFactor(1, 1)
        self._splitter.setStretchFactor(2, 0)
        self._splitter.setSizes([360, 820, 420])

    def _build_form(self) -> QWidget:
        scroll = QScrollArea(self)
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        body = QWidget(scroll)
        layout = QVBoxLayout(body)
        layout.setContentsMargins(8, 8, 8, 8)
        layout.setSpacing(8)

        layout.addWidget(self._build_model_group(body))
        layout.addWidget(self._build_image_group(body))
        layout.addWidget(self._build_params_group(body))
        layout.addWidget(self._build_output_group(body))
        layout.addStretch(1)
        scroll.setWidget(body)
        return scroll

    def _build_model_group(self, parent: QWidget) -> QGroupBox:
        group = QGroupBox("模型", parent)
        form = QFormLayout(group)

        self.model_dir_edit = QLineEdit(group)
        self.model_dir_edit.setPlaceholderText("训练输出根目录（含 run_* 子目录）")
        browse_dir = QPushButton("浏览…", group)
        browse_dir.clicked.connect(self._choose_model_dir)
        row = QHBoxLayout()
        row.addWidget(self.model_dir_edit, 1)
        row.addWidget(browse_dir)
        form.addRow("模型目录", row)

        self.model_combo = QComboBox(group)
        self.model_combo.setSizeAdjustPolicy(QComboBox.SizeAdjustPolicy.AdjustToMinimumContentsLengthWithIcon)
        self.model_combo.setMinimumContentsLength(18)
        self.model_combo.currentIndexChanged.connect(self._on_model_selected)
        refresh = QPushButton("刷新", group)
        refresh.clicked.connect(lambda: self._refresh_models())
        row = QHBoxLayout()
        row.addWidget(self.model_combo, 1)
        row.addWidget(refresh)
        form.addRow("模型", row)

        browse_weight = QPushButton("浏览权重…", group)
        browse_weight.setToolTip("选择 run 之外的 best.pt / last.pt，并读取其结构信息")
        browse_weight.clicked.connect(self._browse_checkpoint)
        form.addRow("", browse_weight)

        self.model_info_label = QLabel("尚未选择模型")
        self.model_info_label.setWordWrap(True)
        self.model_info_label.setTextInteractionFlags(
            Qt.TextInteractionFlag.TextSelectableByMouse
        )
        form.addRow("模型信息", self.model_info_label)
        return group

    def _build_image_group(self, parent: QWidget) -> QGroupBox:
        group = QGroupBox("输入影像", parent)
        form = QFormLayout(group)

        self.image_edit = QLineEdit(group)
        self.image_edit.setPlaceholderText("待推理的 GeoTIFF / 影像")
        self.image_edit.editingFinished.connect(self._on_image_changed)
        browse = QPushButton("浏览…", group)
        browse.clicked.connect(self._choose_image)
        row = QHBoxLayout()
        row.addWidget(self.image_edit, 1)
        row.addWidget(browse)
        form.addRow("影像", row)

        self.band_r = self._band_spin(group)
        self.band_g = self._band_spin(group)
        self.band_b = self._band_spin(group)
        row = QHBoxLayout()
        row.addWidget(QLabel("R", group))
        row.addWidget(self.band_r)
        row.addWidget(QLabel("G", group))
        row.addWidget(self.band_g)
        row.addWidget(QLabel("B", group))
        row.addWidget(self.band_b)
        row.addStretch(1)
        form.addRow("波段", row)

        self.image_info_label = QLabel("尚未选择影像")
        self.image_info_label.setWordWrap(True)
        self.image_info_label.setTextInteractionFlags(
            Qt.TextInteractionFlag.TextSelectableByMouse
        )
        form.addRow("影像信息", self.image_info_label)
        return group

    def _build_params_group(self, parent: QWidget) -> QGroupBox:
        group = QGroupBox("推理参数", parent)
        form = QFormLayout(group)

        self.window_spin = QSpinBox(group)
        self.window_spin.setRange(0, 8192)
        self.window_spin.setSingleStep(64)
        self.window_spin.setSuffix(" px")
        self.window_spin.setSpecialValueText("跟随模型")
        self.window_spin.setToolTip("0 = 使用 checkpoint 的训练窗口；实例分割强制跟随")
        form.addRow("窗口大小", self.window_spin)

        self.overlap_spin = QSpinBox(group)
        self.overlap_spin.setRange(0, 50)
        self.overlap_spin.setSuffix(" %")
        self.overlap_spin.setToolTip("相邻窗口重叠比例（0–50%）；越大越慢但接缝越稳")
        form.addRow("重叠", self.overlap_spin)

        self.zero_nodata_check = QCheckBox("全 0 视为 NoData", group)
        self.zero_nodata_check.setToolTip(
            "选中波段同时为 0 的像元（常见黑色填充边界）不参与推理，也不计入统计"
        )
        form.addRow("", self.zero_nodata_check)

        self.batch_spin = QSpinBox(group)
        self.batch_spin.setRange(1, 1024)
        form.addRow("批大小", self.batch_spin)

        self.device_combo = QComboBox(group)
        self.device_combo.addItem("自动", "auto")
        self.device_combo.addItem("CUDA", "cuda")
        self.device_combo.addItem("CPU", "cpu")
        form.addRow("设备", self.device_combo)

        self.score_spin = QDoubleSpinBox(group)
        self.score_spin.setRange(0.0, 1.0)
        self.score_spin.setDecimals(2)
        self.score_spin.setSingleStep(0.05)
        form.addRow("实例阈值", self.score_spin)

        self.min_pixels_spin = QSpinBox(group)
        self.min_pixels_spin.setRange(0, 1_000_000)
        form.addRow("最小实例像素", self.min_pixels_spin)

        self.preview_spin = QSpinBox(group)
        self.preview_spin.setRange(256, 16384)
        self.preview_spin.setSingleStep(512)
        self.preview_spin.setSuffix(" px")
        self.preview_spin.setToolTip("预览图长边；成果 GeoTIFF 始终是全分辨率")
        form.addRow("预览分辨率", self.preview_spin)
        return group

    def _build_output_group(self, parent: QWidget) -> QGroupBox:
        group = QGroupBox("输出", parent)
        form = QFormLayout(group)

        self.output_edit = QLineEdit(group)
        self.output_edit.setPlaceholderText("留空 = <模型目录>/predictions")
        browse = QPushButton("浏览…", group)
        browse.clicked.connect(self._choose_output_dir)
        row = QHBoxLayout()
        row.addWidget(self.output_edit, 1)
        row.addWidget(browse)
        form.addRow("输出根目录", row)

        hint = QLabel("每次推理生成 predict_日期_时间 子目录，含 prediction.tif 与 result.json。")
        hint.setWordWrap(True)
        hint.setEnabled(False)
        form.addRow("", hint)
        return group

    def _band_spin(self, parent: QWidget) -> QSpinBox:
        spin = QSpinBox(parent)
        spin.setRange(1, 64)
        spin.setValue(1)
        spin.valueChanged.connect(self._on_bands_changed)
        return spin

    def _build_preview(self) -> QWidget:
        panel = QWidget(self)
        layout = QVBoxLayout(panel)
        layout.setContentsMargins(6, 8, 6, 8)
        layout.setSpacing(6)

        controls = QHBoxLayout()
        self.overlay_check = QCheckBox("叠加结果", panel)
        self.overlay_check.setChecked(True)
        self.overlay_check.toggled.connect(self._apply_overlay)
        controls.addWidget(self.overlay_check)

        controls.addWidget(QLabel("透明度", panel))
        self.opacity_slider = QSlider(Qt.Orientation.Horizontal, panel)
        self.opacity_slider.setRange(0, 100)
        self.opacity_slider.setValue(60)
        self.opacity_slider.setFixedWidth(120)
        self.opacity_slider.valueChanged.connect(self._apply_overlay)
        controls.addWidget(self.opacity_slider)

        fit = QPushButton("适应窗口", panel)
        fit.clicked.connect(self._fit_preview)
        controls.addWidget(fit)
        controls.addStretch(1)
        hint = QLabel("滚轮缩放 / 中键平移 · 预览为缩略图，成果以 GeoTIFF 为准", panel)
        hint.setEnabled(False)
        controls.addWidget(hint)
        layout.addLayout(controls)

        self.preview_stack = QStackedWidget(panel)
        self.preview_placeholder = QLabel(
            "选择影像后显示预览；开始推理后在这里叠加预测类别颜色。", panel
        )
        self.preview_placeholder.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.preview_placeholder.setWordWrap(True)
        self.preview_stack.addWidget(self.preview_placeholder)

        self.view = AnnotationView(panel)
        self.view.set_mode(MODE_PAN)
        self.preview_stack.addWidget(self.view)
        self.preview_stack.setCurrentWidget(self.preview_placeholder)
        layout.addWidget(self.preview_stack, 1)
        return panel

    def _build_result_panel(self) -> QWidget:
        scroll = QScrollArea(self)
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        body = QWidget(scroll)
        layout = QVBoxLayout(body)
        layout.setContentsMargins(8, 8, 8, 8)
        layout.setSpacing(8)

        status_group = QGroupBox("状态", body)
        status_layout = QVBoxLayout(status_group)
        self.status_label = QLabel("就绪")
        self.status_label.setWordWrap(True)
        self.status_label.setObjectName("InferenceStatusLabel")
        status_layout.addWidget(self.status_label)

        self.progress = QProgressBar(status_group)
        self.progress.setRange(0, _PROGRESS_MAX)
        self.progress.setValue(0)
        self.progress.setFormat("%p%")
        status_layout.addWidget(self.progress)

        self.detail_label = QLabel("等待开始推理")
        self.detail_label.setWordWrap(True)
        status_layout.addWidget(self.detail_label)
        layout.addWidget(status_group)

        info_group = QGroupBox("信息", body)
        info_layout = QVBoxLayout(info_group)
        self.info_label = QLabel("模型：—\n影像：—\n参数：—")
        self.info_label.setWordWrap(True)
        self.info_label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        info_layout.addWidget(self.info_label)
        layout.addWidget(info_group)

        stats_group = QGroupBox("类别统计", body)
        stats_layout = QVBoxLayout(stats_group)
        self.stats_table = QTableWidget(0, 5, stats_group)
        self.stats_table.setObjectName("InferenceStatsTable")
        self.stats_table.setHorizontalHeaderLabels(["类别", "像素", "占比", "面积", "实例数"])
        self.stats_table.verticalHeader().setVisible(False)
        self.stats_table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.stats_table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.stats_table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Stretch)
        stats_layout.addWidget(self.stats_table)
        layout.addWidget(stats_group, 2)

        output_group = QGroupBox("成果", body)
        output_layout = QVBoxLayout(output_group)
        self.output_label = QLabel("输出：—")
        self.output_label.setWordWrap(True)
        self.output_label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        output_layout.addWidget(self.output_label)

        buttons = QHBoxLayout()
        self.load_map_button = QPushButton("加载到地图", output_group)
        self.load_map_button.setToolTip("把 prediction.tif 作为栅格图层加入当前工程")
        self.load_map_button.clicked.connect(self._load_result_to_map)
        self.load_map_button.setEnabled(False)
        buttons.addWidget(self.load_map_button)
        open_output = QPushButton("打开输出目录", output_group)
        open_output.clicked.connect(self._open_output_dir)
        buttons.addWidget(open_output)
        buttons.addStretch(1)
        output_layout.addLayout(buttons)
        layout.addWidget(output_group)

        log_group = QGroupBox("推理日志", body)
        log_layout = QVBoxLayout(log_group)
        self.log_view = QPlainTextEdit(log_group)
        self.log_view.setReadOnly(True)
        self.log_view.setMaximumBlockCount(_LOG_MAX_BLOCKS)
        self.log_view.setPlaceholderText("推理过程日志会显示在这里")
        log_layout.addWidget(self.log_view)
        layout.addWidget(log_group, 2)

        scroll.setWidget(body)
        return scroll

    # ------------------------------------------------------------------ 配置读写
    def _load_config(self) -> None:
        config = self.inference_config
        self._loading = True
        try:
            self.model_dir_edit.setText(
                config.model_dir or self._config.training.output_dir
            )
            self.image_edit.setText(config.image)
            self.output_edit.setText(config.output_dir)
            self.window_spin.setValue(config.window_size)
            self.overlap_spin.setValue(config.overlap_percent)
            self.zero_nodata_check.setChecked(config.zero_as_nodata)
            self.batch_spin.setValue(config.batch_size)
            index = self.device_combo.findData(config.device)
            self.device_combo.setCurrentIndex(index if index >= 0 else 0)
            self.score_spin.setValue(config.score_threshold)
            self.min_pixels_spin.setValue(config.min_instance_pixels)
            self.preview_spin.setValue(config.preview_max_side)
            self._refresh_models()
        finally:
            self._loading = False
        self._on_model_selected()
        if config.image:
            QTimer.singleShot(0, self._on_image_changed)

    def _effective_preview_side(self) -> int:
        return max(256, int(self.preview_spin.value()))

    def _form_values(self) -> dict:
        return {
            "model_dir": self.model_dir_edit.text().strip(),
            "image": self.image_edit.text().strip(),
            "output_dir": self.output_edit.text().strip(),
            "window_size": self.window_spin.value(),
            "overlap_percent": self.overlap_spin.value(),
            "zero_as_nodata": self.zero_nodata_check.isChecked(),
            "batch_size": self.batch_spin.value(),
            "device": self.device_combo.currentData() or "auto",
            "bands": [self.band_r.value(), self.band_g.value(), self.band_b.value()],
            "score_threshold": self.score_spin.value(),
            "min_instance_pixels": self.min_pixels_spin.value(),
            "preview_max_side": self.preview_spin.value(),
        }

    def _save_settings(self) -> None:
        try:
            save_inference_settings(self._form_values())
        except Exception:
            logger.exception("保存推理设置失败")

    def activate(self) -> None:
        if self._image_info is None and self.image_edit.text().strip():
            QTimer.singleShot(0, self._on_image_changed)

    # ------------------------------------------------------------------ 模型
    def current_checkpoint(self) -> str:
        return str(self.model_combo.currentData() or "")

    def _refresh_models(self, preferred: str = "") -> None:
        """扫描模型目录下的训练 run，尽量保留当前选择。"""
        root = self.model_dir_edit.text().strip()
        current = str(preferred or self.current_checkpoint())
        runs = scan_runs(root) if root else []
        self._runs_by_checkpoint = {run.checkpoint: run for run in runs}
        blocked = self.model_combo.blockSignals(True)
        try:
            self.model_combo.clear()
            for run in runs:
                self.model_combo.addItem(run.summary, run.checkpoint)
                self.model_combo.setItemData(
                    self.model_combo.count() - 1,
                    f"{run.checkpoint}\n{run.run_dir}",
                    Qt.ItemDataRole.ToolTipRole,
                )
            if current:
                index = self.model_combo.findData(current)
                if index < 0:
                    self.model_combo.addItem(f"自定义：{Path(current).name}", current)
                    index = self.model_combo.count() - 1
                self.model_combo.setCurrentIndex(index)
                if current not in self._runs_by_checkpoint:
                    run = read_run(Path(current).parent)
                    if run is not None and Path(run.checkpoint) == Path(current):
                        self._runs_by_checkpoint[current] = run
        finally:
            self.model_combo.blockSignals(blocked)
        self._on_model_selected()

    def _choose_model_dir(self) -> None:
        start = self.model_dir_edit.text().strip() or str(Path.home())
        directory = QFileDialog.getExistingDirectory(self, "选择训练输出目录", start)
        if not directory:
            return
        self.model_dir_edit.setText(directory)
        self._refresh_models()

    def _on_model_selected(self, _index: int = -1) -> None:
        if self._loading:
            return
        checkpoint = self.current_checkpoint()
        info: dict = {}
        if checkpoint:
            cached = self._inspect_cache.get(checkpoint)
            if cached:
                info = dict(cached)
            else:
                run = self._runs_by_checkpoint.get(checkpoint)
                if run is not None:
                    info = self._info_from_run(run)
        self._apply_checkpoint_info(info)
        self._update_action_state()

    @staticmethod
    def _info_from_run(run: RunInfo) -> dict:
        return {
            "checkpoint": run.checkpoint,
            "task": run.task,
            "model": run.model,
            "encoder": run.encoder,
            "tile_size": run.tile_size,
            "class_names": [str(item.get("name") or "") for item in run.classes],
            "class_colors": [str(item.get("color") or "") for item in run.classes],
            "best_value": run.best_value,
            "best_metric": run.best_metric,
            "best_epoch": run.best_epoch,
            "status": run.status,
        }

    def _apply_checkpoint_info(self, info: dict) -> None:
        self._checkpoint_info = dict(info or {})
        task = str(info.get("task") or "")
        self._current_task = task
        tile_size = int(info.get("tile_size") or 0)

        if not info:
            self.model_info_label.setText(
                "尚未选择模型" if not self.current_checkpoint() else "未读取权重信息（仍可开始，子进程会校验）"
            )
        else:
            names = [str(item) for item in info.get("class_names") or [] if str(item)]
            lines = [
                f"任务：{catalog.task_label(task) if task else '未知'}",
                f"模型：{info.get('model') or '—'}",
                f"骨干：{info.get('encoder') or '—'}",
                f"训练窗口：{tile_size or '—'} px",
            ]
            if names:
                lines.append(f"类别：{' / '.join(names)}")
            metric = str(info.get("best_metric") or "")
            value = info.get("best_value")
            if metric and value:
                lines.append(f"最佳 {metric}：{float(value):.4f}")
            elif value:
                lines.append(f"最佳指标：{float(value):.4f}")
            self.model_info_label.setText("\n".join(lines))

        instance_task = task == "instances"
        if instance_task and tile_size:
            self.window_spin.setValue(tile_size)
        self.window_spin.setEnabled(not instance_task)
        self.score_spin.setEnabled(not task or instance_task)
        self.min_pixels_spin.setEnabled(not task or instance_task)

    def _browse_checkpoint(self) -> None:
        start = self.current_checkpoint() or self.model_dir_edit.text().strip() or str(Path.home())
        path, _ = QFileDialog.getOpenFileName(
            self, "选择推理权重", start, "PyTorch 权重 (*.pt *.pth);;所有文件 (*)"
        )
        if not path:
            return
        QApplication.setOverrideCursor(Qt.CursorShape.WaitCursor)
        info: dict | None = None
        problem = ""
        try:
            info = inspect_checkpoint(self._python(), path)
        except Exception as exc:  # noqa: BLE001 —— 探测失败不阻塞手动开始
            problem = str(exc)
        finally:
            QApplication.restoreOverrideCursor()
        if info is None:
            QMessageBox.warning(self, "读取权重", problem or "无法读取权重信息")
            return
        self._inspect_cache[path] = dict(info)
        index = self.model_combo.findData(path)
        blocked = self.model_combo.blockSignals(True)
        try:
            if index < 0:
                self.model_combo.addItem(f"自定义：{Path(path).name}", path)
                index = self.model_combo.count() - 1
            self.model_combo.setCurrentIndex(index)
        finally:
            self.model_combo.blockSignals(blocked)
        self._on_model_selected()
        self._append_log(f"已读取权重信息：{path}")

    def set_checkpoint_hint(self, path: str) -> None:
        """主窗口从训练页带入最近一次训练的权重（仅在用户尚未选择时）。"""
        path = str(path or "").strip()
        if not path:
            return
        if not self.model_dir_edit.text().strip():
            run_dir = Path(path).parent
            self.model_dir_edit.setText(str(run_dir.parent))
        self._refresh_models(preferred=path)

    # ------------------------------------------------------------------ 影像
    def _choose_image(self) -> None:
        start = self.image_edit.text().strip() or str(Path.home())
        path, _ = QFileDialog.getOpenFileName(self, "选择待推理影像", start, RASTER_FILTER)
        if not path:
            return
        self.image_edit.setText(path)
        self._on_image_changed()

    def _on_image_changed(self) -> None:
        path = self.image_edit.text().strip()
        if path != self._source_path:
            self._reset_result()
            self._source_path = path
            self._source_preview = None
        if not path or not Path(path).is_file():
            self._image_info = None
            self.image_info_label.setText("尚未选择影像")
            self._update_action_state()
            return
        info = read_raster_info(path)
        self._image_info = info
        lines = [
            f"尺寸：{info.width} × {info.height}",
            f"波段：{info.band_count}（{info.dtype_name}）",
            f"坐标系：{info.projection_name}" + (f"（{info.unit}）" if info.unit else ""),
        ]
        if info.pixel_size:
            lines.append(f"像元：{info.pixel_size[0]:.8g} × {info.pixel_size[1]:.8g}")
        if info.nodata is not None:
            lines.append(f"nodata：{info.nodata:g}")
        self.image_info_label.setText("\n".join(lines))

        self._updating_bands = True
        try:
            maximum = max(1, int(info.band_count))
            for spin in (self.band_r, self.band_g, self.band_b):
                spin.setRange(1, maximum)
            bands = default_bands(info)
            self.band_r.setValue(bands[0])
            self.band_g.setValue(bands[1])
            self.band_b.setValue(bands[2])
        finally:
            self._updating_bands = False
        self._schedule_source_preview()
        self._update_action_state()

    def _on_bands_changed(self, _value: int = 0) -> None:
        if self._updating_bands:
            return
        self._schedule_source_preview()

    def _current_bands(self) -> tuple[int, int, int]:
        maximum = max(1, int(self._image_info.band_count)) if self._image_info else 64
        return tuple(
            min(max(1, spin.value()), maximum)
            for spin in (self.band_r, self.band_g, self.band_b)
        )  # type: ignore[return-value]

    def _schedule_source_preview(self) -> None:
        if self._preview_timer is None:
            self._preview_timer = QTimer(self)
            self._preview_timer.setSingleShot(True)
            self._preview_timer.timeout.connect(self._load_source_preview)
        self._preview_timer.start(250)

    def _load_source_preview(self) -> None:
        path = self.image_edit.text().strip()
        if not path or not Path(path).is_file():
            return
        try:
            image, _scale = read_preview(
                path,
                bands=self._current_bands(),
                max_side=self._effective_preview_side(),
            )
        except Exception as exc:  # noqa: BLE001 —— 预览失败不影响推理
            logger.warning("读取源影像预览失败 %s：%s", path, exc)
            self._append_log(f"源影像预览失败：{exc}")
            return
        self._source_preview = image
        if (
            self._document is None
            or self._document.width != image.width()
            or self._document.height != image.height()
        ):
            labels = LabelSet()
            document = AnnotationDocument(
                image, source_path=path, labels=labels, undo_limit=1, parent=self
            )
            self._document = document
            self._labels = labels
            self.view.set_document(document, image)
        else:
            self.view.set_image(image)
        self.view.set_mode(MODE_PAN)
        self._apply_overlay()
        self.preview_stack.setCurrentWidget(self.view)

    def _reset_result(self) -> None:
        self._document = None
        self._labels = None
        self._output_file = ""
        self.output_label.setText("输出：—")
        self.load_map_button.setEnabled(False)
        self.stats_table.setRowCount(0)
        self.view.set_document(None)
        self.preview_stack.setCurrentWidget(self.preview_placeholder)

    def _apply_overlay(self, *_args) -> None:
        self.view.set_mask_visible(self.overlay_check.isChecked())
        self.view.set_mask_opacity(self.opacity_slider.value() / 100.0)

    def _fit_preview(self) -> None:
        self.view.fit_to_view()

    # ------------------------------------------------------------------ 输出
    def _resolve_output_base(self) -> Path:
        text = self.output_edit.text().strip()
        if text:
            return Path(text)
        checkpoint = self.current_checkpoint()
        if checkpoint:
            return Path(checkpoint).parent / "predictions"
        return Path.cwd() / "predictions"

    def _choose_output_dir(self) -> None:
        start = str(self._resolve_output_base())
        directory = QFileDialog.getExistingDirectory(self, "选择推理输出根目录", start)
        if directory:
            self.output_edit.setText(directory)

    def _make_output_dir(self) -> Path:
        base = self._resolve_output_base()
        try:
            base.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            raise ValueError(f"无法创建输出目录：{exc}") from exc
        stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        candidate = base / f"predict_{stamp}"
        counter = 2
        while candidate.exists():
            candidate = base / f"predict_{stamp}_{counter}"
            counter += 1
        try:
            candidate.mkdir()
        except OSError as exc:
            raise ValueError(f"无法创建推理目录：{exc}") from exc
        return candidate

    def _open_output_dir(self) -> None:
        base = self._resolve_output_base()
        directory = (
            self._output_dir
            if self._output_dir is not None and self._output_dir.is_dir()
            else base
        )
        try:
            directory.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            QMessageBox.warning(self, "打开输出目录", f"无法创建目录：{exc}")
            return
        QDesktopServices.openUrl(QUrl.fromLocalFile(str(directory)))

    def _load_result_to_map(self) -> None:
        path = self._output_file
        if not path or not Path(path).is_file():
            QMessageBox.warning(self, "加载到地图", "当前没有可加载的推理成果。")
            return
        try:
            from qgis.core import QgsProject, QgsRasterLayer

            name = f"推理 {Path(path).parent.name}"
            layer = QgsRasterLayer(path, name)
            if not layer.isValid():
                QMessageBox.warning(self, "加载到地图", f"无法打开推理成果：\n{path}")
                return
            QgsProject.instance().addMapLayer(layer)
            self._status.showMessage("已加载推理成果到地图")
            self._append_log(f"已加载到地图：{name}")
        except Exception as exc:
            logger.exception("加载推理成果失败")
            QMessageBox.warning(self, "加载到地图", f"加载失败：{exc}")

    # ------------------------------------------------------------------ 推理
    def _build_manifest(self, output_dir: Path) -> dict:
        checkpoint = self.current_checkpoint()
        image = self.image_edit.text().strip()
        window = self.window_spin.value()
        if self._current_task == "instances":
            window = int(self._checkpoint_info.get("tile_size") or window)
        return {
            "version": 1,
            "checkpoint": checkpoint,
            "image": image,
            "output_dir": str(output_dir),
            "params": {
                "window_size": int(window),
                "overlap_percent": self.overlap_spin.value(),
                "zero_as_nodata": self.zero_nodata_check.isChecked(),
                "batch_size": self.batch_spin.value(),
                "device": self.device_combo.currentData() or "auto",
                "bands": list(self._current_bands()),
                "score_threshold": self.score_spin.value(),
                "min_instance_pixels": self.min_pixels_spin.value(),
                "build_overviews": True,
                "preview_max_side": self._effective_preview_side(),
            },
        }

    def _start_inference(self) -> None:
        if self._inference or self.client.is_running:
            return
        checkpoint = self.current_checkpoint()
        if not checkpoint:
            QMessageBox.warning(self, "开始推理", "请先选择要使用的模型权重。")
            return
        if not Path(checkpoint).is_file():
            QMessageBox.warning(self, "开始推理", f"权重不存在：\n{checkpoint}")
            return
        image = self.image_edit.text().strip()
        if not image or not Path(image).is_file():
            QMessageBox.warning(self, "开始推理", "请选择待推理的影像。")
            return

        python = self._python()
        report = self._check_environment(python)
        if report.error:
            QMessageBox.warning(self, "开始推理", f"无法检查推理环境：\n{report.error}")
            return
        if report.missing:
            titles = "、".join(item.title for item in report.missing)
            QMessageBox.warning(
                self,
                "开始推理",
                f"外部 Python 缺少推理依赖：{titles}\n\n"
                f"请点工具栏「安装推理依赖」，或手动执行：\n"
                f"{python} -m pip install rasterio",
            )
            return

        try:
            output_dir = self._make_output_dir()
        except ValueError as exc:
            QMessageBox.warning(self, "开始推理", str(exc))
            return
        manifest = self._build_manifest(output_dir)
        manifest_path = output_dir / "manifest.json"
        try:
            manifest_path.write_text(
                json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8"
            )
        except OSError as exc:
            QMessageBox.warning(self, "开始推理", f"写入推理清单失败：{exc}")
            return

        self._save_settings()
        self._output_dir = output_dir
        self._output_file = ""
        self._reset_result()
        self.progress.setValue(0)
        self.detail_label.setText("等待推理进程就绪…")
        self.status_label.setText("正在启动推理…")
        self._append_log(f"推理清单：{manifest_path}")
        QTimer.singleShot(0, self._load_source_preview)
        self.client.python = python
        if not self.client.start(manifest_path):
            self._append_log("推理进程启动失败")
            self._update_action_state()
            return
        self._set_inference(True)
        self._status.showMessage("推理已启动")

    def _check_environment(self, python: str) -> TrainingEnvironmentReport:
        try:
            return self._environment_probe(python)
        except Exception as exc:  # noqa: BLE001 —— 探测失败不应让页面崩溃
            return TrainingEnvironmentReport(error=str(exc))

    def _stop_inference(self) -> None:
        if not self.client.is_running:
            return
        answer = QMessageBox.question(
            self,
            "停止推理",
            "确定停止推理吗？\n当前窗口批次结束后停止，已写入的块会保留，"
            "result.json 会标记为不完整。",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if answer != QMessageBox.StandardButton.Yes:
            return
        self.client.stop(graceful=True)
        self._append_log("已请求停止推理，等待当前窗口完成…")
        self.status_label.setText("正在停止推理…")
        self.act_stop.setEnabled(False)

    def _install_dependencies(self) -> None:
        python = self._python()
        problem = python_problem(python)
        if problem:
            QMessageBox.warning(self, "安装推理依赖", problem)
            return
        report = self._check_environment(python)
        if report.error:
            QMessageBox.warning(self, "安装推理依赖", f"无法检查推理环境：\n{report.error}")
            return
        missing = report.missing_keys
        if not missing:
            titles = " / ".join(item["title"] for item in INFERENCE_DEPENDENCIES)
            QMessageBox.information(self, "安装推理依赖", f"推理依赖已齐全（{titles}）。")
            return
        commands = build_install_commands(python, missing)
        if not commands:
            QMessageBox.information(self, "安装推理依赖", "没有需要安装的依赖。")
            return
        titles = "、".join(item.title for item in report.missing)
        answer = QMessageBox.question(
            self,
            "安装推理依赖",
            f"将为外部 Python 安装：{titles}\n\nPython：{python}\n"
            "安装过程需要联网，可能持续数分钟。是否继续？",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.Yes,
        )
        if answer != QMessageBox.StandardButton.Yes:
            return
        self._append_log(f"开始安装推理依赖：{titles}")
        self._installer.start_commands(commands)
        self._update_action_state()

    # ------------------------------------------------------------------ 子进程事件
    def _wire(self) -> None:
        self.client.hello.connect(lambda: self._append_log("推理进程已就绪"))
        self.client.log.connect(self._append_log)
        self.client.started.connect(self._on_started)
        self.client.progress.connect(self._on_progress)
        self.client.finished.connect(self._on_done)
        self.client.stopped.connect(self._on_stopped)
        self.client.failed.connect(self._on_failed)
        self.client.running_changed.connect(self._on_running_changed)
        self.client.status_changed.connect(self.status_label.setText)

        self._installer.log.connect(self._append_log)
        self._installer.finished.connect(self._on_install_finished)

        ThemeManager.instance().theme_changed.connect(self._apply_theme_qss)

    def _on_running_changed(self, running: bool) -> None:
        self._set_inference(bool(running))
        if running:
            self.status_label.setText("推理进行中…")

    def _set_inference(self, active: bool) -> None:
        self._inference = bool(active)
        self._update_action_state()

    def _update_action_state(self) -> None:
        installing = self._installer.is_running
        busy = self._inference or self.client.is_running or installing
        has_model = bool(self.current_checkpoint())
        has_image = bool(self.image_edit.text().strip())
        self.form_body.setEnabled(not busy)
        self.act_install.setEnabled(not busy)
        self.act_start.setEnabled(not busy and has_model and has_image)
        self.act_stop.setEnabled(self._inference or self.client.is_running)

    def _on_started(self, payload: dict) -> None:
        task = str(payload.get("task") or "")
        self._current_task = task
        classes = payload.get("classes") or []
        names = " / ".join(
            str(item.get("name") or "") for item in classes if isinstance(item, dict)
        )
        windows = int(payload.get("windows") or 0)
        window_size = int(payload.get("window_size") or 0)
        overlap = int(payload.get("overlap_percent") or 0)
        device = str(payload.get("device") or "?")
        self.progress.setValue(0)
        self.status_label.setText(f"推理进行中：{windows} 个窗口 / {device}")
        self.detail_label.setText(
            f"窗口 {window_size}px，重叠 {overlap}%"
            + (f"；类别：{names}" if names else "")
        )
        self.info_label.setText(
            "\n".join(
                [
                    f"模型：{payload.get('model') or '—'} / {payload.get('encoder') or '—'}"
                    f"（{catalog.task_label(task) if task else '—'}）",
                    f"影像：{payload.get('width')} × {payload.get('height')}"
                    f"，{payload.get('band_count')} 波段",
                    f"参数：窗口 {window_size}，重叠 {overlap}%，"
                    f"步长 {payload.get('keep_size') or '—'}，设备 {device}",
                ]
            )
        )
        self._append_log(
            f"推理开始：{windows} 个窗口，窗口 {window_size}px，重叠 {overlap}%，设备 {device}"
        )

    def _on_progress(self, payload: dict) -> None:
        if str(payload.get("stage") or "") != "inferring":
            return
        try:
            done = int(payload.get("done") or 0)
            total = max(1, int(payload.get("total") or 1))
        except (TypeError, ValueError):
            return
        self.progress.setValue(int(max(0.0, min(1.0, done / total)) * _PROGRESS_MAX))
        elapsed = self._format_seconds(payload.get("elapsed"))
        eta = self._format_seconds(payload.get("eta"))
        self.detail_label.setText(
            f"窗口 {done}/{total} · 用时 {elapsed} · 剩余约 {eta}"
        )

    def _on_done(self, payload: dict) -> None:
        self._set_inference(False)
        self.progress.setValue(_PROGRESS_MAX)
        self._finish(payload, partial=False)
        self._status.showMessage("推理完成")

    def _on_stopped(self, payload: dict) -> None:
        self._set_inference(False)
        self._finish(payload, partial=True)
        self._status.showMessage("推理已停止（结果不完整）")

    def _finish(self, payload: dict, *, partial: bool) -> None:
        stats = payload.get("stats") if isinstance(payload.get("stats"), dict) else {}
        classes = stats.get("classes") or payload.get("classes") or []
        output = str(payload.get("output") or "")
        if output:
            self._output_file = output
            self._output_dir = Path(output).parent
            self.output_label.setText(f"输出：{output}")
            self.load_map_button.setEnabled(True)
        windows = int(payload.get("windows") or stats.get("windows_total") or 0)
        done = int(payload.get("done") or stats.get("windows_done") or windows)
        duration = self._format_seconds(payload.get("duration") or stats.get("duration_seconds"))
        if partial:
            self.status_label.setText("推理已停止（结果不完整）")
            self.detail_label.setText(f"已完成 {done}/{windows} 个窗口 · 用时 {duration}")
            self._append_log(f"推理已停止：完成 {done}/{windows} 个窗口，用时 {duration}")
        else:
            nodata = int(stats.get("nodata_pixels") or 0)
            nodata_text = (
                f" · 已排除 NoData {self._format_int(nodata)} 像素" if nodata > 0 else ""
            )
            self.status_label.setText("推理完成")
            self.detail_label.setText(f"{windows} 个窗口 · 用时 {duration}{nodata_text}")
            self._append_log(
                f"推理完成：{windows} 个窗口，用时 {duration}，输出 {output}{nodata_text}"
            )
        self._show_stats(classes, stats)
        if output:
            self._show_result_preview(output, classes)
        self._update_action_state()

    def _show_stats(self, classes, stats: dict) -> None:
        self.stats_table.setRowCount(0)
        instance_task = self._current_task == "instances"
        self.stats_table.setColumnHidden(4, not instance_task)
        for row, item in enumerate(classes):
            if not isinstance(item, dict):
                continue
            self.stats_table.insertRow(row)
            ratio = item.get("ratio")
            try:
                ratio_text = f"{float(ratio) * 100:.2f}%"
            except (TypeError, ValueError):
                ratio_text = "—"
            values = [
                str(item.get("name") or f"类别{item.get('id')}"),
                self._format_int(item.get("pixels")),
                ratio_text,
                str(item.get("area_text") or "—"),
                self._format_int(item.get("instances")),
            ]
            for column, text in enumerate(values):
                cell = QTableWidgetItem(text)
                cell.setTextAlignment(Qt.AlignmentFlag.AlignCenter)
                self.stats_table.setItem(row, column, cell)
        if stats.get("area_approximate"):
            self._append_log("面积为按影像中心纬度近似换算，仅供估算。")

    @staticmethod
    def _format_int(value) -> str:
        try:
            return f"{int(value):,}"
        except (TypeError, ValueError):
            return "—"

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

    def _on_failed(self, message: str, error_type: str) -> None:
        self._set_inference(False)
        self.status_label.setText("推理失败")
        self.detail_label.setText(str(message).splitlines()[0])
        self._append_log(f"推理失败（{error_type}）：{message}")
        self._status.showMessage("推理失败", 10000)
        # 页面可能不在前台：不弹阻塞式对话框，错误写在状态与日志里

    def _on_install_finished(self, ok: bool, code: int) -> None:
        self._update_action_state()
        if ok:
            self._append_log("推理依赖安装完成")
            self._status.showMessage("推理依赖安装完成")
        else:
            self._append_log(f"推理依赖安装结束（code={code}），请检查日志")
            self._status.showMessage("推理依赖安装未完成")

    # ------------------------------------------------------------------ 预览渲染
    def _show_result_preview(self, output: str, classes) -> None:
        image_path = self.image_edit.text().strip()
        if not image_path or not Path(output).is_file():
            return
        max_side = self._effective_preview_side()
        try:
            source, _scale = read_preview(
                image_path, bands=self._current_bands(), max_side=max_side
            )
            mask_image, _scale = read_class_preview(output, max_side=max_side)
        except Exception as exc:  # noqa: BLE001 —— 预览失败不影响成果
            logger.warning("读取推理成果预览失败 %s：%s", output, exc)
            self._append_log(f"成果预览失败：{exc}")
            return

        labels = LabelSet()
        labels.replace_from_dicts(
            [
                {
                    "id": int(item["id"]),
                    "name": str(item.get("name") or f"类别{item['id']}"),
                    "color": str(item.get("color") or ""),
                }
                for item in classes
                if isinstance(item, dict) and int(item.get("id") or 0) > 0
            ]
        )
        document = AnnotationDocument(
            source, source_path=image_path, labels=labels, undo_limit=1, parent=self
        )
        try:
            document.load_mask(mask_image)
        except Exception as exc:  # noqa: BLE001
            logger.warning("载入推理掩膜失败：%s", exc)
            self._append_log(f"载入推理掩膜失败：{exc}")
            return
        self._document = document
        self._labels = labels
        self._source_preview = source
        self.view.set_document(document, source)
        self.view.set_mode(MODE_PAN)
        self._apply_overlay()
        self.preview_stack.setCurrentWidget(self.view)

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
            + f"\n#InferencePage {{ background-color: {theme.window_bg}; }}"
            + f"\n#InferenceStatusLabel {{ font-weight: 600; color: {theme.toolbar_text}; }}"
        )
        self._toolbar.setStyleSheet(toolbar_qss(theme))
        for action, key in self._tool_actions:
            action.setIcon(tool_icons.icon(key, theme.toolbar_text))

    # ------------------------------------------------------------------ 关闭
    def confirm_leave(self) -> bool:
        """离开页面时若推理仍在进行：确认后优雅停止（当前批次结束后退出）。"""
        if not self.client.is_running:
            return True
        answer = QMessageBox.question(
            self,
            "推理进行中",
            "推理仍在进行，停止推理并离开吗？\n已写入的块会保留，结果标记为不完整。",
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
        """应用退出：保存设置 → 取消安装 → 终止推理进程（幂等）。"""
        if self._shutdown_done:
            return True
        if self._shutting_down:
            return False
        if self.client.is_running and confirm:
            answer = QMessageBox.question(
                self,
                "推理进行中",
                "推理仍在进行，退出将强制停止推理。是否退出？",
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
                logger.exception("取消推理依赖安装失败")
            try:
                self.client.shutdown()
            except Exception:
                logger.exception("停止推理子进程失败")
        finally:
            self._shutting_down = False
            self._shutdown_done = True
        return True


__all__ = ["InferencePage", "probe_inference_environment"]
