"""EISeg 风格的影像标注工作区页面（嵌入主窗口中央区）。

左侧影像列表 / 波段 / 保存设置 / 地理信息，中间画布（多边形、画笔、橡皮擦、
SAM 点选/框选/自动分割），右侧模型与标签、候选掩膜、分割设置。

页面不再单独开顶层窗口：由主窗口用 ``QStackedWidget`` 承载，切走页签时保留状态，
关闭页面只是切回地图。应用退出时主窗口调用 :meth:`AnnotationPage.shutdown`
停止 SAM 子进程并清理临时目录。
"""

from __future__ import annotations

import os
import shutil
import tempfile
import uuid
from dataclasses import replace
from pathlib import Path

import numpy as np
from qgis.PyQt.QtCore import QRectF, QSize, Qt, QTimer, pyqtSignal
from qgis.PyQt.QtGui import QAction, QActionGroup, QColor, QImage, QKeySequence
from qgis.PyQt.QtWidgets import (
    QApplication,
    QCheckBox,
    QColorDialog,
    QDialog,
    QDialogButtonBox,
    QFileDialog,
    QFormLayout,
    QInputDialog,
    QLabel,
    QLineEdit,
    QMenuBar,
    QMessageBox,
    QSpinBox,
    QSplitter,
    QStatusBar,
    QToolBar,
    QVBoxLayout,
    QWidget,
)

from app.core.ai.prompt_translate import translate_prompt
from app.core.ai.sam_client import SamClient
from app.core.ai.sam_models import (
    infer_family,
    model_for_checkpoint,
    resolve_sam_paths,
    sam2_config_for_filename,
)
from app.infra.deep_learning import (
    SETTING_DEVICE,
    SETTING_MODEL_ID,
    SETTING_MODEL_TYPE,
    SETTING_SAM2_CONFIG,
    DeepLearningConfig,
    SamConfig,
    load_deep_learning_config,
    save_checkpoint_override,
    save_sam_setting,
)
from app.infra.logging import get_logger
from app.ui.common import tool_icons
from app.ui.common.icons import Icons
from app.ui.common.theme import ThemeManager, toolbar_qss, window_qss
from app.ui.deep_learning.annotation_canvas import (
    MODE_BRUSH,
    MODE_ERASER,
    MODE_PAN,
    MODE_POLYGON,
    MODE_SAM_BOX,
    MODE_SAM_POINT,
    MODE_SAM_SCRIBBLE,
    AnnotationView,
)
from app.ui.deep_learning.annotation_model import (
    AnnotationDocument,
    LabelSet,
    mask_ids_from_image,
)
from app.ui.deep_learning.annotation_project import (
    PROJECT_FILTER,
    PROJECT_SUFFIX,
    AnnotationProject,
    ProjectImage,
    load_project,
    save_project,
)
from app.ui.deep_learning.grid_slicing import (
    MAX_OVERLAP_PERCENT,
    MAX_TILE_SIZE,
    MIN_TILE_SIZE,
    GridSpec,
    TileSpec,
    overlap_px_from_percent,
)
from app.ui.deep_learning.panels import AnnotationPanelDock, ImageEntry, ImageListDock
from app.ui.deep_learning.raster_io import (
    RASTER_FILTER,
    RASTER_SUFFIXES,
    RasterInfo,
    compute_band_ranges,
    default_bands,
    read_preview,
    read_raster_info,
    read_tile,
    write_mask_geotiff,
)
from app.ui.deep_learning.sam_cache import clear_cache
from app.ui.deep_learning.sam_settings_dialog import SamSettingsDialog

logger = get_logger(__name__)

_MODE_HINTS = {
    MODE_PAN: "平移：中键拖拽，滚轮缩放",
    MODE_POLYGON: "多边形：左键逐点，双击/Enter/点起点闭合，Backspace 删点，Esc 取消",
    MODE_BRUSH: "画笔：按住左键涂抹当前标签",
    MODE_ERASER: "橡皮擦：按住左键擦回未标注",
    MODE_SAM_POINT: "SAM 点选：左键正点，右键/Shift+左键负点，Enter 应用，Backspace 撤点",
    MODE_SAM_BOX: "SAM 框选：拖出目标框，Enter 应用",
    MODE_SAM_SCRIBBLE: (
        "SAM 涂鸦：按住左键涂抹目标，右键/Shift+左键为负涂鸦，"
        "Enter 应用，Backspace 撤销最后一笔"
    ),
}


class GridOptionsDialog(QDialog):
    """导入影像时的宫格切分参数。"""

    def __init__(
        self,
        parent,
        *,
        enabled: bool,
        tile_size: int,
        overlap_percent: int,
        note: str = "",
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle("导入影像 — 宫格切分")
        self.setModal(True)
        layout = QVBoxLayout(self)

        intro = QLabel("大幅影像可切成宫格逐块标注，每块单独保存 PNG + JSON。" + note)
        intro.setWordWrap(True)
        layout.addWidget(intro)

        form = QFormLayout()
        self.enable_check = QCheckBox("切分为宫格")
        self.enable_check.setChecked(enabled)
        form.addRow(self.enable_check)

        self.tile_spin = QSpinBox()
        self.tile_spin.setRange(MIN_TILE_SIZE, MAX_TILE_SIZE)
        self.tile_spin.setSingleStep(64)
        self.tile_spin.setValue(tile_size)
        self.tile_spin.setSuffix(" px")
        form.addRow("块大小", self.tile_spin)

        self.overlap_spin = QSpinBox()
        self.overlap_spin.setRange(0, MAX_OVERLAP_PERCENT)
        self.overlap_spin.setValue(overlap_percent)
        self.overlap_spin.setSuffix(" %")
        form.addRow("重叠度", self.overlap_spin)

        self.overlap_px_label = QLabel()
        form.addRow("", self.overlap_px_label)
        layout.addLayout(form)

        hint = QLabel("重叠度按块边长的百分比计算；相邻块共享重叠区域，便于后续拼接。")
        hint.setWordWrap(True)
        hint.setEnabled(False)
        layout.addWidget(hint)

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

        self.tile_spin.valueChanged.connect(self._update_overlap_label)
        self.overlap_spin.valueChanged.connect(self._update_overlap_label)
        self.enable_check.toggled.connect(self._set_enabled)
        self._update_overlap_label()
        self._set_enabled(enabled)

    def _update_overlap_label(self) -> None:
        px = overlap_px_from_percent(self.tile_spin.value(), self.overlap_spin.value())
        self.overlap_px_label.setText(f"相邻块重叠 {px} 像素")

    def _set_enabled(self, enabled: bool) -> None:
        self.tile_spin.setEnabled(enabled)
        self.overlap_spin.setEnabled(enabled)
        self.overlap_px_label.setEnabled(enabled)

    def values(self) -> tuple[bool, int, int]:
        return self.enable_check.isChecked(), self.tile_spin.value(), self.overlap_spin.value()


class AnnotationPage(QWidget):
    """影像标注工作区页面（主窗口单实例，切走时保留状态）。"""

    #: 用户点了「关闭」/ Ctrl+W：由主窗口切回地图页（页面状态保留）
    close_requested = pyqtSignal()

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.setObjectName("AnnotationPage")
        self.setWindowTitle("影像标注")
        self.setWindowIcon(Icons.app())

        self._config: DeepLearningConfig = load_deep_learning_config()
        self._config = replace(self._config, sam=resolve_sam_paths(self._config.sam))
        self._labels = LabelSet(self._config.annotation.palette)
        self._document: AnnotationDocument | None = None
        self._preview_image: QImage | None = None
        self._canvas_image: QImage | None = None
        self._raster_info: RasterInfo | None = None
        self._source_size: tuple[int, int] = (0, 0)
        self._current_image = ""
        self._entries: list[ImageEntry] = []
        self._output_override = ""
        self._mask_override: Path | None = None
        self._last_dir = ""
        #: 标注工程（.qann）；空路径表示尚未保存
        self._project_path: Path | None = None
        self._project_dirty = False
        self._project_suspend = False
        #: 影像路径 -> 最近一次扫描到的宫格进度（已完成块数, 总块数）
        self._tile_progress: dict[str, tuple[int, int]] = {}
        #: 宫格目录 -> 是否使用旧 0 基命名（目录内出现 r00_* 即为旧目录）
        self._tile_layout_cache: dict[str, str] = {}
        #: 影像路径 -> (块大小, 重叠像素)；不在表里表示不切宫格
        self._grid_settings: dict[str, tuple[int, int]] = {}
        self._grid_spec: GridSpec | None = None
        self._tiles: list[TileSpec] = []
        self._tile_map: dict[tuple[int, int], TileSpec] = {}
        self._current_tile: tuple[int, int] | None = None
        self._tile_annotated: set[tuple[int, int]] = set()
        self._band_ranges: tuple[tuple[float, float], ...] | None = None
        self._last_grid_tile_size = self._config.annotation.grid_tile_size
        self._last_grid_overlap_percent = self._config.annotation.grid_overlap_percent
        self._sam_token = ""
        self._sam_image_token = ""
        self._sam_points: list[tuple[float, float, int]] = []
        self._sam_box = None
        self._sam_candidates: list[dict] = []
        self._model_loading = False
        self._temp_dir = Path(tempfile.mkdtemp(prefix="qgis_ann_"))
        self._sam = SamClient(self._config.sam, parent=self)
        self._sam_ready = False
        self._sam_family = ""
        #: 上一次框选的样本框；应用样本后画布提示会被清空，批量提取仍可复用它
        self._last_sample_box = None
        #: shutdown() 幂等 / 防重入：应用退出时可能被主窗口与 aboutToQuit 各调一次
        self._shutdown_done = False
        self._shutting_down = False

        self._build_container()
        self._build_docks()
        self._build_actions()
        self._build_menus()
        self._build_toolbar()
        self._wire()
        self._apply_theme_qss()

        self.panel.set_auto_params(self._config.auto)
        self.panel.set_model_type(self._config.sam.model_type)
        self.panel.set_device(self._config.sam.device)
        self.panel.brush_spin.setValue(20)
        self.view.set_brush_size(self.panel.brush_size())
        self.view.set_mask_opacity(self.panel.opacity() / 100.0)
        self.view.set_prompt_radius(self.panel.prompt_radius())
        self._refresh_sam_availability()
        self._refresh_undo_actions()
        self._refresh_actions()
        self.statusBar().showMessage("就绪")
        self._build_status_widgets()
        self._update_title()

    # ------------------------------------------------------------------ 装配
    def _build_container(self) -> None:
        """页面外框：菜单栏 / 工具条 / 三栏分隔器 / 状态栏。"""
        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)

        self._menubar = QMenuBar(self)
        root.addWidget(self._menubar)

        self._toolbar = QToolBar("标注工具", self)
        self._toolbar.setObjectName("AnnotationToolbar")
        self._toolbar.setMovable(False)
        self._toolbar.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonIconOnly)
        self._toolbar.setIconSize(QSize(24, 24))
        root.addWidget(self._toolbar)

        self._splitter = QSplitter(Qt.Orientation.Horizontal, self)
        self._splitter.setChildrenCollapsible(False)
        root.addWidget(self._splitter, 1)

        self.view = AnnotationView(self)
        self._splitter.addWidget(self.view)

        self._status = QStatusBar(self)
        root.addWidget(self._status)

    def statusBar(self) -> QStatusBar:  # noqa: N802 —— 兼容 QMainWindow API
        """兼容原 QMainWindow API：返回页面自己的状态栏。"""
        return self._status

    def _build_docks(self) -> None:
        self.image_dock = ImageListDock(self)
        self.panel = AnnotationPanelDock(self)
        # QDockWidget 直接放进 QSplitter：保留原生标题栏与 QDockWidget 作用域的
        # 主题样式；不能再允许 Movable（拖动会 setFloating 变顶层窗口）。
        self._splitter.insertWidget(0, self.image_dock)
        self._splitter.addWidget(self.panel)
        self._splitter.setStretchFactor(0, 0)
        self._splitter.setStretchFactor(1, 1)
        self._splitter.setStretchFactor(2, 0)
        self._splitter.setSizes([280, 800, 330])

    def _build_actions(self) -> None:
        theme = ThemeManager.instance().current()
        color = theme.toolbar_text
        self._tool_actions: list[tuple[QAction, str]] = []
        self._tool_group = QActionGroup(self)
        self._tool_group.setExclusive(True)

        def tool_action(key: str, text: str, mode: str, tip: str) -> QAction:
            action = QAction(tool_icons.icon(key, color), text, self)
            action.setCheckable(True)
            action.setToolTip(tip)
            action.setData(mode)
            action.triggered.connect(lambda _checked=False, m=mode: self._set_mode(m))
            self._tool_group.addAction(action)
            self._tool_actions.append((action, key))
            return action

        self.act_pan = tool_action("pan", "平移", MODE_PAN, "平移 / 缩放画布")
        self.act_polygon = tool_action(
            "space_query_polygon", "多边形", MODE_POLYGON, "多边形标注（左键逐点，双击闭合）"
        )
        self.act_brush = tool_action("brush", "画笔", MODE_BRUSH, "画笔标注当前标签")
        self.act_eraser = tool_action("eraser", "橡皮擦", MODE_ERASER, "橡皮擦：擦回未标注")
        self.act_sam_point = tool_action(
            "select_point", "SAM 点选", MODE_SAM_POINT, "左键正点 / 右键负点，Enter 应用"
        )
        self.act_sam_box = tool_action("select_box", "SAM 框选", MODE_SAM_BOX, "拖框提示 SAM")
        self.act_sam_scribble = tool_action(
            "draw",
            "SAM 涂鸦",
            MODE_SAM_SCRIBBLE,
            "按住左键涂画目标，右键/Shift+左键负涂鸦，Enter 应用",
        )

        self.act_auto = QAction(tool_icons.icon("magic", color), "自动分割", self)
        self.act_auto.setToolTip("SAM 全图自动生成候选掩膜")
        self.act_auto.triggered.connect(self._run_auto)
        self._tool_actions.append((self.act_auto, "magic"))

        # 工程
        self.act_project_new = QAction("新建标注工程", self)
        self.act_project_new.setShortcut(QKeySequence("Ctrl+Alt+N"))
        self.act_project_new.triggered.connect(self._new_project)
        self.act_project_open = QAction("打开标注工程…", self)
        self.act_project_open.setShortcut(QKeySequence("Ctrl+Alt+O"))
        self.act_project_open.triggered.connect(self._open_project)
        self.act_project_save = QAction("保存标注工程", self)
        self.act_project_save.setShortcut(QKeySequence("Ctrl+Alt+S"))
        self.act_project_save.triggered.connect(lambda: self._save_project())
        self.act_project_save_as = QAction("标注工程另存为…", self)
        self.act_project_save_as.triggered.connect(lambda: self._save_project(save_as=True))

        # 文件
        self.act_open = QAction("打开影像…", self)
        self.act_open.setShortcut(QKeySequence("Ctrl+O"))
        self.act_open.triggered.connect(self._choose_images)
        self.act_open_folder = QAction("打开文件夹…", self)
        self.act_open_folder.setShortcut(QKeySequence("Ctrl+Shift+O"))
        self.act_open_folder.triggered.connect(self._open_folder)
        self.act_save = QAction("保存标注", self)
        self.act_save.setShortcut(QKeySequence("Ctrl+S"))
        self.act_save.triggered.connect(lambda: self._save())
        self.act_save_as = QAction("标注另存为…", self)
        self.act_save_as.setShortcut(QKeySequence("Ctrl+Shift+S"))
        self.act_save_as.triggered.connect(lambda: self._save(save_as=True))
        self.act_close = QAction("关闭", self)
        self.act_close.setShortcut(QKeySequence("Ctrl+W"))
        self.act_close.triggered.connect(self._request_close)

        # 标注
        self.act_undo = QAction(tool_icons.icon("undo", color), "撤销", self)
        self.act_undo.setShortcut(QKeySequence(QKeySequence.StandardKey.Undo))
        self.act_undo.triggered.connect(self._undo)
        self.act_redo = QAction(tool_icons.icon("step", color), "重做", self)
        self.act_redo.setShortcut(QKeySequence(QKeySequence.StandardKey.Redo))
        self.act_redo.triggered.connect(self._redo)
        self.act_clear_label = QAction("清空当前标签", self)
        self.act_clear_label.triggered.connect(self._clear_current_label)
        self.act_clear_all = QAction("清空全部标注", self)
        self.act_clear_all.triggered.connect(self._clear_all)

        # 功能
        self.act_load_model = QAction("加载 SAM 模型", self)
        self.act_load_model.triggered.connect(self._load_model)
        self.act_sam_settings = QAction("SAM 设置…", self)
        self.act_sam_settings.triggered.connect(self._open_sam_settings)
        self.act_sam_point.setShortcut(QKeySequence("P"))
        self.act_sam_point.setShortcutContext(Qt.ShortcutContext.WidgetWithChildrenShortcut)
        self.view.addAction(self.act_sam_point)
        self.act_sam_box.setShortcut(QKeySequence("X"))
        self.act_sam_box.setShortcutContext(Qt.ShortcutContext.WidgetWithChildrenShortcut)
        self.view.addAction(self.act_sam_box)
        self.act_sam_scribble.setShortcut(QKeySequence("S"))
        self.act_sam_scribble.setShortcutContext(
            Qt.ShortcutContext.WidgetWithChildrenShortcut
        )
        self.view.addAction(self.act_sam_scribble)
        self.act_auto.setShortcut(QKeySequence("F4"))
        self.act_clear_candidates = QAction("清除候选掩膜", self)
        self.act_clear_candidates.triggered.connect(self._clear_candidates)

        # 显示
        self.act_zoom_in = QAction(tool_icons.icon("zoom_in", color), "放大", self)
        self.act_zoom_in.setShortcut(QKeySequence("Ctrl+="))
        self.act_zoom_in.triggered.connect(self.view.zoom_in)
        self.act_zoom_out = QAction(tool_icons.icon("zoom_out", color), "缩小", self)
        self.act_zoom_out.setShortcut(QKeySequence("Ctrl+-"))
        self.act_zoom_out.triggered.connect(self.view.zoom_out)
        self.act_fit = QAction(tool_icons.icon("expand", color), "适应窗口", self)
        self.act_fit.setShortcut(QKeySequence("Ctrl+0"))
        self.act_fit.triggered.connect(self.view.fit_to_view)
        self.act_mask_visible = QAction("显示掩膜", self)
        self.act_mask_visible.setCheckable(True)
        self.act_mask_visible.setChecked(True)
        self.act_mask_visible.toggled.connect(self.view.set_mask_visible)

        # 帮助
        self.act_help = QAction("使用说明", self)
        self.act_help.triggered.connect(self._show_help)
        self.act_about = QAction("关于", self)
        self.act_about.triggered.connect(self._show_about)

        self.act_pan.setChecked(True)

        # 页面内快捷键一律 WidgetWithChildrenShortcut 并注册到页面：焦点在页面里才
        # 生效，且不会和主窗口的同键 action 互相抢（主窗口的冲突 action 由宿主在
        # 页面激活时禁用，见 MainWindow._set_annotation_shortcuts_blocked）。
        for action in (
            self.act_project_new,
            self.act_project_open,
            self.act_project_save,
            self.act_project_save_as,
            self.act_open,
            self.act_open_folder,
            self.act_save,
            self.act_save_as,
            self.act_close,
            self.act_undo,
            self.act_redo,
            self.act_auto,
            self.act_zoom_in,
            self.act_zoom_out,
            self.act_fit,
        ):
            action.setShortcutContext(Qt.ShortcutContext.WidgetWithChildrenShortcut)
            self.addAction(action)

    def _build_menus(self) -> None:
        menubar = self._menubar
        project_menu = menubar.addMenu("工程")
        project_menu.addAction(self.act_project_new)
        project_menu.addAction(self.act_project_open)
        project_menu.addSeparator()
        project_menu.addAction(self.act_project_save)
        project_menu.addAction(self.act_project_save_as)

        file_menu = menubar.addMenu("文件")
        file_menu.addAction(self.act_open)
        file_menu.addAction(self.act_open_folder)
        file_menu.addSeparator()
        file_menu.addAction(self.act_save)
        file_menu.addAction(self.act_save_as)
        file_menu.addSeparator()
        file_menu.addAction(self.act_close)

        label_menu = menubar.addMenu("标注")
        label_menu.addAction(self.act_undo)
        label_menu.addAction(self.act_redo)
        label_menu.addSeparator()
        label_menu.addAction(self.act_clear_label)
        label_menu.addAction(self.act_clear_all)

        tool_menu = menubar.addMenu("功能")
        tool_menu.addAction(self.act_load_model)
        tool_menu.addAction(self.act_sam_settings)
        tool_menu.addSeparator()
        tool_menu.addAction(self.act_sam_point)
        tool_menu.addAction(self.act_sam_box)
        tool_menu.addAction(self.act_sam_scribble)
        tool_menu.addAction(self.act_auto)
        tool_menu.addAction(self.act_clear_candidates)
        tool_menu.addSeparator()
        tool_menu.addAction(self.act_polygon)
        tool_menu.addAction(self.act_brush)
        tool_menu.addAction(self.act_eraser)
        tool_menu.addAction(self.act_pan)

        view_menu = menubar.addMenu("显示")
        view_menu.addAction(self.act_zoom_in)
        view_menu.addAction(self.act_zoom_out)
        view_menu.addAction(self.act_fit)
        view_menu.addSeparator()
        view_menu.addAction(self.act_mask_visible)

        help_menu = menubar.addMenu("帮助")
        help_menu.addAction(self.act_help)
        help_menu.addAction(self.act_about)

    def _build_toolbar(self) -> None:
        toolbar = self._toolbar
        for action in (
            self.act_pan,
            self.act_polygon,
            self.act_brush,
            self.act_eraser,
            self.act_sam_point,
            self.act_sam_box,
            self.act_sam_scribble,
            self.act_auto,
        ):
            toolbar.addAction(action)
        toolbar.addSeparator()
        toolbar.addAction(self.act_undo)
        toolbar.addAction(self.act_redo)
        toolbar.addSeparator()
        toolbar.addAction(self.act_zoom_in)
        toolbar.addAction(self.act_zoom_out)
        toolbar.addAction(self.act_fit)
        toolbar.addSeparator()
        toolbar.addAction(self.act_save)

    def _build_status_widgets(self) -> None:
        from qgis.PyQt.QtWidgets import QLabel

        # 嵌入主窗口后 setWindowTitle 不可见，标题信息同时显示在状态栏左侧
        self._title_label = QLabel("未打开影像 — 影像标注")
        self._title_label.setObjectName("AnnotationTitleLabel")
        self._coord_label = QLabel("—")
        self._zoom_label = QLabel("100%")
        self.statusBar().addPermanentWidget(self._title_label, 1)
        self.statusBar().addPermanentWidget(self._coord_label)
        self.statusBar().addPermanentWidget(self._zoom_label)

    # ------------------------------------------------------------------ 标注工程
    def _mark_project_dirty(self) -> None:
        if self._project_suspend or self._project_dirty:
            return
        self._project_dirty = True
        self._update_title()

    def _project_snapshot(self) -> AnnotationProject:
        images: list[ProjectImage] = []
        for entry in self._entries:
            path = str(Path(entry.path))
            settings = self._grid_settings.get(path)
            done, total = self._tile_progress.get(path, (0, 0))
            images.append(
                ProjectImage(
                    path=entry.path,
                    grid_tile_size=settings[0] if settings else 0,
                    grid_overlap_px=settings[1] if settings else 0,
                    annotated=self._image_annotated(entry.path),
                    done=done,
                    total=total,
                )
            )
        return AnnotationProject(
            images=images,
            labels=[label.as_dict() for label in self._labels.labels],
            current=self._current_image,
            current_tile=self._current_tile,
            output_dir=self._output_override,
            last_grid_tile_size=self._last_grid_tile_size,
            last_grid_overlap_percent=self._last_grid_overlap_percent,
            selected_label_id=self.panel.current_label_id(),
        )

    def _default_project_path(self) -> Path:
        if self._project_path is not None:
            return self._project_path
        directory = self._last_dir
        if not directory and self._entries:
            directory = str(Path(self._entries[0].path).parent)
        if not directory and self._current_image:
            directory = str(Path(self._current_image).parent)
        stem = Path(self._entries[0].path).stem if self._entries else "标注工程"
        return Path(directory or ".") / f"{stem}{PROJECT_SUFFIX}"

    def _save_project(self, save_as: bool = False) -> bool:
        path = self._project_path
        if save_as or path is None:
            chosen, _ = QFileDialog.getSaveFileName(
                self,
                "保存标注工程",
                str(self._default_project_path()),
                PROJECT_FILTER,
            )
            if not chosen:
                return False
            path = Path(chosen)
            if path.suffix.lower() != PROJECT_SUFFIX:
                path = path.with_suffix(PROJECT_SUFFIX)
        try:
            save_project(path, self._project_snapshot())
        except Exception as exc:
            logger.exception("保存标注工程失败：%s", path)
            QMessageBox.warning(self, "保存标注工程", f"保存失败：\n{exc}")
            return False
        self._project_path = path
        self._project_dirty = False
        self._update_title()
        self._show_status(f"已保存标注工程：{path}")
        return True

    def _confirm_save_project(self) -> bool:
        """关闭 / 新建 / 打开工程前，询问是否保存未保存的工程状态。"""
        if not self._project_dirty or not (self._entries or self._labels.labels):
            return True
        answer = QMessageBox.question(
            self,
            "标注工程",
            "标注工程有未保存的更改，是否保存？",
            QMessageBox.StandardButton.Save
            | QMessageBox.StandardButton.Discard
            | QMessageBox.StandardButton.Cancel,
            QMessageBox.StandardButton.Save,
        )
        if answer == QMessageBox.StandardButton.Cancel:
            return False
        if answer == QMessageBox.StandardButton.Save:
            return self._save_project()
        return True

    def _new_project(self) -> None:
        if self._sam.is_busy:
            self._show_status("SAM 正在推理，完成后再新建工程")
            return
        if not self._confirm_save_project():
            return
        if not self._confirm_discard():
            return
        self._project_suspend = True
        try:
            self._reset_session()
        finally:
            self._project_suspend = False
        self._project_path = None
        self._project_dirty = False
        self._update_title()
        self._show_status("已新建标注工程")

    def _open_project(self) -> None:
        if self._sam.is_busy:
            self._show_status("SAM 正在推理，完成后再打开工程")
            return
        if not self._confirm_save_project():
            return
        if not self._confirm_discard():
            return
        start = ""
        if self._project_path is not None:
            start = str(self._project_path.parent)
        elif self._last_dir:
            start = self._last_dir
        chosen, _ = QFileDialog.getOpenFileName(
            self, "打开标注工程", start, PROJECT_FILTER
        )
        if not chosen:
            return
        self._load_project_file(Path(chosen))

    def _load_project_file(self, path: Path) -> bool:
        try:
            project = load_project(path)
        except ValueError as exc:
            QMessageBox.warning(self, "打开标注工程", str(exc))
            return False
        self._project_suspend = True
        try:
            self._reset_session()
            self._project_path = path
            self._labels.replace_from_dicts(project.labels)
            self._entries = []
            self._grid_settings = {}
            self._tile_progress = {}
            for image in project.images:
                self._entries.append(ImageEntry(image.path, image.annotated))
                settings = image.grid_settings()
                key = str(Path(image.path))
                if settings is not None:
                    self._grid_settings[key] = settings
                if image.total > 0:
                    self._tile_progress[key] = (image.done, image.total)
            self._output_override = project.output_dir
            self.image_dock.set_output_dir(project.output_dir)
            self._last_grid_tile_size = project.last_grid_tile_size
            self._last_grid_overlap_percent = project.last_grid_overlap_percent
            self._refresh_image_list()

            current = project.current
            known = {entry.path for entry in self._entries}
            if current not in known:
                current = self._entries[0].path if self._entries else ""
            if current:
                self._switch_image(current)
            if project.current_tile is not None and self._grid_spec is not None:
                tile = self._tile_map.get(project.current_tile)
                if tile is not None:
                    self._on_tile_activated(tile.row, tile.col)
            self._refresh_labels_panel()
            if (
                project.selected_label_id
                and self._labels.get(project.selected_label_id) is not None
            ):
                self.panel.set_labels(
                    list(self._labels.labels), project.selected_label_id
                )
                self._sync_canvas_label(project.selected_label_id)
        finally:
            self._project_suspend = False
        self._project_dirty = False
        self._update_title()
        missing = [image.path for image in project.images if not Path(image.path).exists()]
        if missing:
            QMessageBox.warning(
                self,
                "打开标注工程",
                f"有 {len(missing)} 张影像不存在，已保留在列表中：\n"
                + "\n".join(missing[:8])
                + ("\n…" if len(missing) > 8 else ""),
            )
        self._show_status(f"已打开标注工程：{path}")
        return True

    def _reset_session(self) -> None:
        self._document = None
        self.view.set_document(None)
        self._current_image = ""
        self._preview_image = None
        self._canvas_image = None
        self._raster_info = None
        self._source_size = (0, 0)
        self._entries = []
        self._grid_settings = {}
        self._tile_progress = {}
        self._tile_layout_cache.clear()
        self._grid_spec = None
        self._tiles = []
        self._tile_map = {}
        self._current_tile = None
        self._tile_annotated = set()
        self._band_ranges = None
        self._mask_override = None
        self._output_override = ""
        self.image_dock.set_output_dir("")
        self.image_dock.clear_tiles()
        self.image_dock.set_geo_info(None)
        self.image_dock.set_images([], "")
        self._clear_candidates()
        self._labels.clear()
        self._refresh_labels_panel()
        self._refresh_undo_actions()
        self._refresh_actions()
        self._update_title()

    # ------------------------------------------------------------------ 信号
    def _wire(self) -> None:
        view = self.view
        view.polygon_finished.connect(self._on_polygon_finished)
        view.message.connect(self._show_status)
        view.sam_prompt_changed.connect(self._on_sam_prompt_changed)
        view.sam_box_changed.connect(self._on_sam_box_changed)
        view.sam_apply_requested.connect(self._apply_sam_preview)
        view.cursor_moved.connect(self._on_cursor_moved)
        view.zoom_changed.connect(self._on_zoom_changed)

        dock = self.image_dock
        dock.open_requested.connect(self._choose_images)
        dock.folder_requested.connect(self._open_folder)
        dock.remove_requested.connect(self._remove_current_image)
        dock.image_activated.connect(self._switch_image)
        dock.tile_activated.connect(self._on_tile_activated)
        dock.bands_changed.connect(self._on_bands_changed)
        dock.output_dir_changed.connect(self._on_output_dir_changed)

        panel = self.panel
        panel.choose_checkpoint_requested.connect(self._choose_checkpoint)
        panel.load_model_requested.connect(self._load_model)
        panel.sam_settings_requested.connect(self._open_sam_settings)
        panel.add_label_requested.connect(self._add_label)
        panel.rename_label_requested.connect(self._rename_label)
        panel.color_label_requested.connect(self._color_label)
        panel.delete_label_requested.connect(self._delete_label)
        panel.label_selected.connect(self._on_label_selected)
        panel.candidate_preview_requested.connect(self._preview_candidate)
        panel.apply_candidates_requested.connect(self._apply_candidates)
        panel.clear_candidates_requested.connect(self._clear_candidates)
        panel.text_prompt_requested.connect(self._run_text_prompt)
        panel.exemplar_requested.connect(self._run_exemplar)
        panel.save_requested.connect(lambda: self._save())
        panel.brush_size_changed.connect(self.view.set_brush_size)
        panel.opacity_changed.connect(lambda value: self.view.set_mask_opacity(value / 100.0))
        panel.prompt_radius_changed.connect(self.view.set_prompt_radius)
        panel.model_type_combo.currentIndexChanged.connect(self._on_model_config_changed)
        panel.device_combo.currentIndexChanged.connect(self._on_model_config_changed)

        self._labels.changed.connect(self._on_labels_changed)

        sam = self._sam
        sam.status_changed.connect(self._on_sam_status)
        sam.ready_changed.connect(self._on_sam_ready)
        sam.error.connect(self._on_sam_error)
        sam.progress.connect(self._on_sam_progress)
        sam.busy_changed.connect(self._on_sam_busy)

        self._predict_timer = QTimer(self)
        self._predict_timer.setSingleShot(True)
        self._predict_timer.setInterval(120)
        self._predict_timer.timeout.connect(self._predict_now)

        ThemeManager.instance().theme_changed.connect(self._apply_theme_qss)

    # ------------------------------------------------------------------ 主题
    def _apply_theme_qss(self, _theme=None) -> None:
        theme = ThemeManager.instance().current()
        self.setStyleSheet(
            window_qss(theme)
            + f"\n#AnnotationPage {{ background-color: {theme.window_bg}; }}"
        )
        toolbar = getattr(self, "_toolbar", None)
        if toolbar is not None:
            toolbar.setStyleSheet(toolbar_qss(theme))
        color = theme.toolbar_text
        for action, key in self._tool_actions:
            action.setIcon(tool_icons.icon(key, color))
        for action, key in (
            (self.act_undo, "undo"),
            (self.act_redo, "step"),
            (self.act_zoom_in, "zoom_in"),
            (self.act_zoom_out, "zoom_out"),
            (self.act_fit, "expand"),
        ):
            action.setIcon(tool_icons.icon(key, color))
        self.view.set_background_color(theme.window_bg)
        self.image_dock.apply_theme()
        self.panel.apply_theme()

    # ------------------------------------------------------------------ 焦点
    def activate(self) -> None:
        """页面成为当前页后把焦点交给画布（页面快捷键依赖焦点在页面内）。"""
        QTimer.singleShot(0, self._focus_view)

    def _focus_view(self) -> None:
        if not self.isVisible():
            return
        if QApplication.activeModalWidget() is not None:
            return
        current = QApplication.focusWidget()
        if current is not None and (current is self or self.isAncestorOf(current)):
            return
        self.view.setFocus(Qt.FocusReason.OtherFocusReason)

    # ------------------------------------------------------------------ 影像
    def _choose_images(self) -> None:
        start = self._last_dir or (str(Path(self._current_image).parent) if self._current_image else "")
        paths, _ = QFileDialog.getOpenFileNames(self, "打开影像", start, RASTER_FILTER)
        if not paths:
            return
        self._last_dir = str(Path(paths[0]).parent)
        new_paths = [path for path in paths if not self._has_entry(path)]
        if new_paths and not self._ask_grid_options(new_paths):
            return
        for path in paths:
            self._add_entry(path)
        self._refresh_image_list()
        self._switch_image(paths[0])

    def _open_folder(self) -> None:
        start = self._last_dir or (str(Path(self._current_image).parent) if self._current_image else "")
        directory = QFileDialog.getExistingDirectory(self, "打开文件夹", start)
        if not directory:
            return
        self._last_dir = directory
        found = []
        try:
            for item in sorted(Path(directory).iterdir(), key=lambda p: p.name.lower()):
                if item.is_file() and item.suffix.lower() in RASTER_SUFFIXES:
                    found.append(str(item))
        except OSError as exc:
            QMessageBox.warning(self, "打开文件夹", f"读取目录失败：{exc}")
            return
        if not found:
            QMessageBox.information(self, "打开文件夹", "该目录下没有可识别的影像文件")
            return
        new_paths = [path for path in found if not self._has_entry(path)]
        if new_paths and not self._ask_grid_options(new_paths):
            return
        for path in found:
            self._add_entry(path)
        self._refresh_image_list()
        if not self._current_image:
            self._switch_image(found[0])

    def _has_entry(self, path: str) -> bool:
        path = str(Path(path))
        return any(entry.path == path for entry in self._entries)

    # ------------------------------------------------------------------ 宫格导入参数
    def _ask_grid_options(self, paths: list[str]) -> bool:
        """导入前询问切分参数；返回 False 表示用户取消导入。"""
        tile_size = self._last_grid_tile_size
        overlap_percent = self._last_grid_overlap_percent
        enabled: bool | None = None
        note = ""
        detected = self._detect_grid_settings(paths[0]) if paths else None
        if detected is not None:
            tile_size, overlap_px = detected
            if tile_size > 0:
                overlap_percent = round(overlap_px / tile_size * 100)
            enabled = True
            note = f"\n检测到已有宫格成果（块 {tile_size} px），已沿用该设置。"
        if enabled is None:
            try:
                info = read_raster_info(paths[0])
                max_side = max(info.width, info.height)
                enabled = max_side > self._config.annotation.grid_auto_threshold
                note = f"\n影像长边 {max_side} 像素。"
            except Exception:  # noqa: BLE001 —— 读不到信息就按不启用宫格处理
                enabled = False
        dialog = GridOptionsDialog(
            self,
            enabled=enabled,
            tile_size=tile_size,
            overlap_percent=overlap_percent,
            note=note,
        )
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return False
        enabled, tile_size, overlap_percent = dialog.values()
        self._last_grid_tile_size = tile_size
        self._last_grid_overlap_percent = overlap_percent
        overlap_px = overlap_px_from_percent(tile_size, overlap_percent)
        for path in paths:
            path = str(Path(path))
            if enabled:
                self._grid_settings[path] = (tile_size, overlap_px)
            else:
                self._grid_settings.pop(path, None)
        self._mark_project_dirty()
        return True

    def _detect_grid_settings(self, path: str) -> tuple[int, int] | None:
        """从已有的宫格成果 JSON 里恢复块大小 / 重叠像素。"""
        directory = self._tile_dir_for(path)
        if not directory.is_dir():
            return None
        json_files = list(directory.glob("*_mask.json"))
        masks_dir = directory / "masks"
        if masks_dir.is_dir():
            json_files.extend(masks_dir.glob("*.json"))
        for json_path in sorted(json_files):
            try:
                import json

                payload = json.loads(json_path.read_text(encoding="utf-8"))
                grid = payload.get("grid") or {}
                tile_size = int(grid.get("tile_size") or 0)
                overlap_px = int(grid.get("overlap_px") or 0)
            except Exception:  # noqa: BLE001 —— 旧成果 JSON 损坏时按无宫格处理
                continue
            if tile_size > 0:
                return tile_size, max(0, overlap_px)
        return None

    def _grid_for(self, path: str, info: RasterInfo) -> GridSpec | None:
        settings = self._grid_settings.get(str(Path(path)))
        if not settings:
            return None
        tile_size, overlap_px = settings
        return GridSpec(
            image_width=max(1, info.width),
            image_height=max(1, info.height),
            tile_size=tile_size,
            overlap_px=max(0, min(tile_size - 1, overlap_px)),
        )

    def _add_entry(self, path: str) -> None:
        path = str(Path(path))
        if any(entry.path == path for entry in self._entries):
            return
        self._entries.append(ImageEntry(path, self._image_annotated(path)))
        self._mark_project_dirty()

    def _refresh_image_list(self) -> None:
        self.image_dock.set_images(list(self._entries), self._current_image)

    def _remove_current_image(self) -> None:
        path = self.image_dock.current_path() or self._current_image
        if not path:
            return
        if path == self._current_image and not self._confirm_discard():
            return
        self._entries = [entry for entry in self._entries if entry.path != path]
        self._grid_settings.pop(str(Path(path)), None)
        self._tile_progress.pop(str(Path(path)), None)
        self._mark_project_dirty()
        if path == self._current_image:
            self._document = None
            self.view.set_document(None)
            self._current_image = ""
            self._preview_image = None
            self._canvas_image = None
            self._raster_info = None
            self._grid_spec = None
            self._tiles = []
            self._tile_map = {}
            self._current_tile = None
            self._tile_annotated = set()
            self._band_ranges = None
            self._clear_candidates()
            self.image_dock.clear_tiles()
            self.image_dock.set_geo_info(None)
            self._update_title()
        self._refresh_image_list()
        if self._entries:
            self._switch_image(self._entries[0].path)

    def _switch_image(self, path: str) -> None:
        path = str(Path(path)) if path else ""
        if not path or path == self._current_image:
            return
        if self._sam.is_busy:
            self._show_status("SAM 正在推理，完成后再切换影像")
            self.image_dock.set_current(self._current_image)
            return
        if not self._confirm_discard():
            self.image_dock.set_current(self._current_image)
            return
        try:
            info = read_raster_info(path)
            bands = default_bands(info)
            preview, _scale = read_preview(
                path, bands=bands, max_side=self._config.annotation.max_side
            )
        except Exception as exc:
            logger.error("打开影像失败 %s：%s", path, exc, exc_info=True)
            QMessageBox.warning(self, "打开影像", f"无法读取影像：\n{path}\n\n{exc}")
            self.image_dock.set_current(self._current_image)
            return

        self._raster_info = info
        self._preview_image = preview
        self._source_size = (info.width, info.height)
        self._current_image = path
        self._mask_override = None
        self._sam_token = uuid.uuid4().hex
        self._sam_image_token = ""
        self._sam_points = []
        self._sam_box = None
        self._grid_spec = self._grid_for(path, info)
        self._tiles = self._grid_spec.tiles() if self._grid_spec else []
        self._tile_map = {(tile.row, tile.col): tile for tile in self._tiles}
        self._tile_annotated = self._scan_tile_masks(path) if self._grid_spec else set()
        if self._grid_spec:
            self._tile_progress[path] = (len(self._tile_annotated), len(self._tiles))
        else:
            self._tile_progress.pop(path, None)
        self._band_ranges = compute_band_ranges(path, tuple(bands)) if self._grid_spec else None
        self._current_tile = None

        self.image_dock.set_bands(info)
        self.image_dock.set_band_selection(bands)
        self.image_dock.set_geo_info(info)
        self.image_dock.set_current(path)
        self._refresh_labels_panel()

        if self._grid_spec and self._tiles:
            tile = self._tiles[0]
            loaded = self._open_tile(tile, bands=bands, confirm=False, refresh_dock=False)
            self._current_tile = (tile.row, tile.col)
            self.image_dock.set_tiles(
                self._grid_spec,
                preview,
                self._current_tile,
                self._tile_annotated,
                select_tab=True,
            )
        else:
            self._grid_spec = None
            document = self._new_document(preview)
            loaded = self._load_existing_mask(document, self._mask_file_for(path))
            self._apply_document(document, preview)
            self.image_dock.set_tiles(None, None, select_tab=True)

        self._refresh_undo_actions()
        self._update_title()
        self._refresh_actions()
        name = Path(path).name
        if self._grid_spec:
            self._show_status(
                f"已打开 {name}（宫格 {self._grid_spec.rows}×{self._grid_spec.cols}），"
                f"左侧「宫格」tab 可切换块"
            )
        elif loaded:
            self._show_status(f"已打开 {name}，并载入已有标注")
        else:
            self._show_status(f"已打开 {name}")
        self._mark_project_dirty()

    # ------------------------------------------------------------------ 文档 / 宫格切换
    def _new_document(self, image: QImage) -> AnnotationDocument:
        document = AnnotationDocument(
            image,
            source_path=self._current_image,
            labels=self._labels,
            undo_limit=self._config.annotation.undo_limit,
            parent=self,
        )
        old = self._document
        self._document = document
        document.dirty_changed.connect(self._on_dirty_changed)
        document.undo_state_changed.connect(self._refresh_undo_actions)
        if old is not None and old is not document:
            old.deleteLater()
        return document

    def _apply_document(self, document: AnnotationDocument, image: QImage) -> None:
        self._canvas_image = image
        self.view.set_document(document, image)
        self.view.set_mask_opacity(self.panel.opacity() / 100.0)
        self.view.set_brush_size(self.panel.brush_size())
        self.view.set_prompt_radius(self.panel.prompt_radius())
        self._set_sam_candidates([])
        self.view.clear_preview()
        self._last_sample_box = None
        if self._sam.is_ready:
            self._send_sam_image()

    def _current_tile_spec(self) -> TileSpec | None:
        if self._current_tile is None:
            return None
        return self._tile_map.get(self._current_tile)

    def _open_tile(
        self,
        tile: TileSpec,
        *,
        bands: tuple[int, int, int] | None = None,
        confirm: bool = True,
        refresh_dock: bool = True,
    ) -> bool:
        if self._sam.is_busy:
            self._show_status("SAM 正在推理，完成后再切换宫格")
            return False
        if confirm and not self._confirm_discard():
            if refresh_dock:
                self.image_dock.set_current_tile(self._current_tile)
            return False
        bands = tuple(bands or self.image_dock.bands())
        try:
            image = read_tile(
                self._current_image,
                tile.x,
                tile.y,
                tile.width,
                tile.height,
                bands=bands,
                ranges=self._band_ranges,
            )
        except Exception as exc:
            logger.error("读取宫格失败 %s：%s", self._current_image, exc, exc_info=True)
            QMessageBox.warning(
                self,
                "打开宫格",
                f"无法读取宫格（{tile.row + 1}-{tile.col + 1}）：\n{exc}",
            )
            if refresh_dock:
                self.image_dock.set_current_tile(self._current_tile)
            return False
        document = self._new_document(image)
        self._current_tile = (tile.row, tile.col)
        self._sam_token = uuid.uuid4().hex
        self._sam_image_token = ""
        self._sam_points = []
        self._sam_box = None
        loaded = self._load_existing_mask(
            document,
            self._existing_tile_mask_file(self._current_image, tile),
        )
        self._apply_document(document, image)
        if refresh_dock:
            self.image_dock.set_current_tile(self._current_tile)
        self._refresh_undo_actions()
        self._update_title()
        self._refresh_actions()
        if loaded:
            self._show_status(f"已切换到宫格 {tile.row + 1}-{tile.col + 1}，并载入已有标注")
        else:
            self._show_status(f"已切换到宫格 {tile.row + 1}-{tile.col + 1}")
        self._mark_project_dirty()
        return True

    def _on_tile_activated(self, row: int, col: int) -> None:
        tile = self._tile_map.get((row, col))
        if tile is None or self._current_tile == (row, col):
            self.image_dock.set_current_tile(self._current_tile)
            return
        if self._sam.is_busy:
            self._show_status("SAM 正在推理，完成后再切换宫格")
            self.image_dock.set_current_tile(self._current_tile)
            return
        if not self._confirm_discard():
            self.image_dock.set_current_tile(self._current_tile)
            return
        self._open_tile(tile, confirm=False)

    def _on_bands_changed(self, bands) -> None:
        if not self._current_image or self._document is None:
            return
        if self._sam.is_busy:
            self._show_status("SAM 正在推理，完成后再切换波段")
            return
        bands = tuple(bands)
        try:
            preview, _scale = read_preview(
                self._current_image,
                bands=bands,
                max_side=self._config.annotation.max_side,
            )
        except Exception as exc:  # noqa: BLE001 —— 读影像失败要回显给用户
            QMessageBox.warning(self, "波段设置", f"重新读取影像失败：{exc}")
            return
        self._preview_image = preview
        # 掩膜与底图同分辨率：波段只换底图
        if self._grid_spec:
            tile = self._current_tile_spec()
            self._band_ranges = compute_band_ranges(self._current_image, bands)
            if tile is not None:
                try:
                    image = read_tile(
                        self._current_image,
                        tile.x,
                        tile.y,
                        tile.width,
                        tile.height,
                        bands=bands,
                        ranges=self._band_ranges,
                    )
                except Exception as exc:  # noqa: BLE001 —— 单块重读失败不影响其他块
                    logger.warning("切换波段后重读宫格失败：%s", exc)
                else:
                    self._canvas_image = image
                    self.view.set_image(image)
            self.image_dock.set_tiles(
                self._grid_spec,
                preview,
                self._current_tile,
                self._tile_annotated,
                select_tab=False,
            )
        else:
            self._canvas_image = preview
            self.view.set_image(preview)
        if self._sam.is_ready:
            self._send_sam_image()

    def _on_output_dir_changed(self, path: str) -> None:
        self._output_override = path
        self._tile_layout_cache.clear()
        self._mark_project_dirty()
        if not self._current_image:
            return
        if self._grid_spec:
            self._tile_annotated = self._scan_tile_masks(self._current_image)
            self._tile_progress[self._current_image] = (
                len(self._tile_annotated),
                len(self._tiles),
            )
            self.image_dock.set_tiles(
                self._grid_spec,
                self._preview_image,
                self._current_tile,
                self._tile_annotated,
                select_tab=False,
            )
        self.image_dock.set_annotated(self._current_image, self._image_annotated(self._current_image))

    # ------------------------------------------------------------------ 已有掩膜
    def _mask_file_for(self, image_path: str) -> Path:
        stem = Path(image_path).stem
        directory = self._output_override or str(Path(image_path).parent)
        return Path(directory) / f"{stem}_mask.png"

    def _tile_dir_for(self, image_path: str) -> Path:
        stem = Path(image_path).stem
        directory = self._output_override or str(Path(image_path).parent)
        return Path(directory) / f"{stem}_grid"

    def _tile_images_dir_for(self, image_path: str) -> Path:
        """原始影像切片目录，与掩膜目录同名一一对应。"""
        return self._tile_dir_for(image_path) / "images"

    def _tile_masks_dir_for(self, image_path: str) -> Path:
        return self._tile_dir_for(image_path) / "masks"

    def _tile_layout(self, image_path: str) -> str:
        """判断宫格目录的产物布局。

        - ``paired``：``images/r01_c01.png`` + ``masks/r01_c01.png``（当前格式）；
        - ``legacy1``：``r01_c01_mask.png`` 平铺（上一版 1 基命名）；
        - ``legacy0``：``r00_c00_mask.png`` 平铺（更早的 0 基命名）。

        按目录整体判断，避免 ``r01_c01`` 同时被当成旧文档 (1,1) 和新文档 (0,0)。
        """
        directory = self._tile_dir_for(image_path)
        key = str(directory)
        cached = self._tile_layout_cache.get(key)
        if cached is not None:
            return cached
        layout = "paired"
        try:
            masks_dir = directory / "masks"
            if masks_dir.is_dir() and any(masks_dir.iterdir()):
                layout = "paired"
            elif next(directory.glob("r00_*_mask.*"), None) is not None:
                layout = "legacy0"
            elif next(directory.glob("r??_c??_mask.*"), None) is not None:
                layout = "legacy1"
        except OSError:
            layout = "paired"
        self._tile_layout_cache[key] = layout
        return layout

    def _tile_stem_for(self, image_path: str, tile: TileSpec) -> str:
        if self._tile_layout(image_path) == "legacy0":
            return f"r{tile.row:02d}_c{tile.col:02d}"
        # 新格式与旧 1 基格式都使用和宫格面板一致的 1 基编号
        return f"r{tile.row + 1:02d}_c{tile.col + 1:02d}"

    def _tile_mask_file_for(self, image_path: str, tile: TileSpec) -> Path:
        layout = self._tile_layout(image_path)
        stem = self._tile_stem_for(image_path, tile)
        if layout == "paired":
            return self._tile_masks_dir_for(image_path) / f"{stem}.png"
        return self._tile_dir_for(image_path) / f"{stem}_mask.png"

    def _tile_image_file_for(self, image_path: str, tile: TileSpec) -> Path:
        stem = self._tile_stem_for(image_path, tile)
        return self._tile_images_dir_for(image_path) / f"{stem}.png"

    def _write_tile_image(self, image_path: str, tile: TileSpec) -> Path | None:
        """把当前宫格的原始切片写到 ``images/``，与掩膜同名。"""
        if self._tile_layout(image_path) != "paired":
            return None
        image = self._canvas_image
        if image is None or image.isNull():
            return None
        target = self._tile_image_file_for(image_path, tile)
        target.parent.mkdir(parents=True, exist_ok=True)
        tmp = target.with_name(target.name + ".tmp")
        if not image.save(str(tmp), "PNG"):
            raise OSError(f"写入影像切片失败：{target}")
        os.replace(tmp, target)
        return target

    def _existing_tile_mask_file(self, image_path: str, tile: TileSpec) -> Path:
        return self._tile_mask_file_for(image_path, tile)

    def _scan_tile_masks(self, image_path: str) -> set[tuple[int, int]]:
        result: set[tuple[int, int]] = set()
        for tile in self._tiles:
            if self._existing_tile_mask_file(image_path, tile).exists():
                result.add((tile.row, tile.col))
        return result

    def _image_annotated(self, image_path: str) -> bool:
        """影像列表的 ✓：有宫格时看宫格目录里是否有任一成果。"""
        directory = self._tile_dir_for(image_path)
        if directory.is_dir():
            try:
                if any(directory.glob("*_mask.png")):
                    return True
                masks_dir = directory / "masks"
                if masks_dir.is_dir() and any(masks_dir.glob("*.png")):
                    return True
            except OSError:
                pass
        return self._mask_file_for(image_path).exists()

    def _load_existing_mask(self, document: AnnotationDocument, mask_path: Path) -> bool:
        if not mask_path.exists():
            return False
        image = QImage(str(mask_path))
        if image.isNull():
            logger.warning("已有掩膜无法读取：%s", mask_path)
            return False
        json_path = mask_path.with_suffix(".json")
        id_map: dict[int, int] = {}
        used: set[int] = set()
        try:
            used = {int(v) for v in np.unique(mask_ids_from_image(image)) if int(v) != 0}
        except Exception:  # noqa: BLE001 —— 掩膜读取异常时按空掩膜处理
            used = set()
        if json_path.exists():
            try:
                import json

                payload = json.loads(json_path.read_text(encoding="utf-8"))
                for item in payload.get("labels") or []:
                    disk_id = int(item.get("id") or 0)
                    name = str(item.get("name") or "").strip()
                    if not disk_id or not name:
                        continue
                    existing = self._labels.by_name(name)
                    label = existing or self._labels.add(name, item.get("color"))
                    id_map[disk_id] = label.id
            except Exception as exc:  # noqa: BLE001 —— 标注 JSON 损坏时继续用掩膜里的 id
                logger.warning("读取标注 JSON 失败 %s：%s", json_path, exc)
        for disk_id in sorted(used):
            if disk_id in id_map:
                continue
            name = f"类别 {disk_id}"
            existing = self._labels.by_name(name)
            try:
                label = existing or self._labels.add(name)
            except ValueError:
                label = existing
            if label is not None:
                id_map[disk_id] = label.id
        try:
            document.load_mask(image, id_map=id_map or None)
            document.set_clean()
        except Exception as exc:  # noqa: BLE001 —— 载入失败只回退，不中断打开影像
            logger.warning("载入已有掩膜失败 %s：%s", mask_path, exc)
            return False
        self._mask_override = mask_path
        return True

    # ------------------------------------------------------------------ 标签
    def _refresh_labels_panel(self) -> None:
        current = self.panel.current_label_id()
        ids = self._labels.ids()
        if current not in ids:
            current = ids[0] if ids else 0
        self.panel.set_labels(self._labels.labels, current)
        self._sync_canvas_label(current)

    def _on_labels_changed(self) -> None:
        self._refresh_labels_panel()
        self._refresh_actions()
        self._mark_project_dirty()

    def _on_label_selected(self, label_id: int) -> None:
        self._sync_canvas_label(label_id)
        self._mark_project_dirty()

    def _sync_canvas_label(self, label_id: int) -> None:
        label = self._labels.get(label_id)
        self.view.set_current_label(label_id, label.color if label is not None else None)

    def _add_label(self) -> None:
        name, ok = QInputDialog.getText(self, "添加标签", "标签名称：", QLineEdit.EchoMode.Normal, "")
        if not ok:
            return
        name = name.strip()
        if not name:
            return
        try:
            label = self._labels.add(name)
        except ValueError as exc:
            QMessageBox.warning(self, "添加标签", str(exc))
            return
        self.panel.set_labels(self._labels.labels, label.id)
        self._sync_canvas_label(label.id)

    def _rename_label(self, label_id: int) -> None:
        label = self._labels.get(label_id)
        if label is None:
            return
        name, ok = QInputDialog.getText(
            self, "重命名标签", "新名称：", QLineEdit.EchoMode.Normal, label.name
        )
        if not ok or not name.strip() or name.strip() == label.name:
            return
        try:
            self._labels.rename(label_id, name.strip())
        except ValueError as exc:
            QMessageBox.warning(self, "重命名标签", str(exc))

    def _color_label(self, label_id: int) -> None:
        label = self._labels.get(label_id)
        if label is None:
            return
        color = QColorDialog.getColor(QColor(label.color), self, "选择标签颜色")
        if color.isValid():
            self._labels.set_color(label_id, color.name())
            self._sync_canvas_label(label_id)

    def _delete_label(self, label_id: int) -> None:
        label = self._labels.get(label_id)
        if label is None:
            return
        used = self._document.used_label_ids() if self._document is not None else set()
        text = f"确定删除标签「{label.name}」？"
        if label_id in used:
            text += "\n\n该标签已有像素，删除后会一并清空为未标注。"
        reply = QMessageBox.question(
            self,
            "删除标签",
            text,
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if reply != QMessageBox.StandardButton.Yes:
            return
        if self._document is not None and label_id in used:
            self._document.clear_class(label_id)
        self._labels.remove(label_id)
        self._refresh_labels_panel()

    # ------------------------------------------------------------------ 保存
    def _save(self, save_as: bool = False) -> bool:
        if self._document is None or not self._current_image:
            self._show_status("没有可保存的标注")
            return False
        tile = self._current_tile_spec()
        if tile is not None:
            default_mask = self._tile_mask_file_for(self._current_image, tile)
        else:
            default_mask = self._mask_file_for(self._current_image)
        if save_as:
            default = default_mask if tile is not None else (self._mask_override or default_mask)
            chosen, _ = QFileDialog.getSaveFileName(
                self, "标注另存为", str(default), "PNG 掩膜 (*.png)"
            )
            if not chosen:
                return False
            mask_path = Path(chosen)
            if mask_path.suffix.lower() != ".png":
                mask_path = mask_path.with_suffix(".png")
        elif tile is not None:
            # 宫格成果固定落在 `<影像名>_grid/masks/` 下，不受整图另存路径影响
            mask_path = default_mask
        else:
            mask_path = self._mask_override or default_mask
        json_path = mask_path.with_suffix(".json")
        extra = {
            "bands": list(self.image_dock.bands()),
            "georeferenced": bool(self._raster_info and self._raster_info.georeferenced),
        }
        if tile is not None and self._grid_spec is not None:
            extra["grid"] = {
                "row": tile.row + 1,
                "col": tile.col + 1,
                "x": tile.x,
                "y": tile.y,
                "width": tile.width,
                "height": tile.height,
                "tile_size": self._grid_spec.tile_size,
                "overlap_px": self._grid_spec.overlap_px,
                "overlap_percent": self._grid_spec.overlap_percent,
                "source_width": self._grid_spec.image_width,
                "source_height": self._grid_spec.image_height,
            }
            if self._tile_layout(self._current_image) == "paired":
                extra["tile_image"] = (
                    f"images/{self._tile_image_file_for(self._current_image, tile).name}"
                )
        try:
            if tile is not None:
                # 结果目录固定为 images/ 原图切片 + masks/ 掩膜，两者同名一一对应
                self._write_tile_image(self._current_image, tile)
                source_size = (tile.width, tile.height)
            else:
                source_size = self._source_size or (self._document.width, self._document.height)
            self._document.save(
                mask_path,
                json_path=json_path,
                source_size=source_size,
                extra=extra,
            )
            if self.image_dock.geotiff_enabled() and self._raster_info is not None:
                info = self._raster_info
                if tile is not None:
                    scale_x = scale_y = 1.0
                    offset = (tile.x, tile.y)
                else:
                    scale_x = info.width / self._document.width if self._document.width else 1.0
                    scale_y = info.height / self._document.height if self._document.height else 1.0
                    offset = (0, 0)
                write_mask_geotiff(
                    self._document.label_id_array(),
                    mask_path.with_suffix(".tif"),
                    info,
                    class_names=[label.name for label in self._labels.labels],
                    class_colors=[label.color for label in self._labels.labels],
                    scale_x=scale_x,
                    scale_y=scale_y,
                    source_offset=offset,
                )
            if tile is not None:
                self._tile_annotated.add((tile.row, tile.col))
                self.image_dock.set_tile_annotated(tile.row, tile.col, True)
                self._tile_progress[self._current_image] = (
                    len(self._tile_annotated),
                    len(self._tiles),
                )
                if not save_as:
                    self._mask_override = None
            else:
                self._mask_override = mask_path
            self._document.set_clean()
            self.image_dock.set_annotated(self._current_image, self._image_annotated(self._current_image))
            self._update_title()
            self._mark_project_dirty()
            self._show_status(f"已保存：{mask_path}")
            return True
        except Exception as exc:
            logger.error("保存标注失败：%s", exc, exc_info=True)
            QMessageBox.critical(self, "保存标注", f"保存失败：\n{exc}")
            return False

    def _confirm_discard(self) -> bool:
        if self._document is None or not self._document.is_dirty:
            return True
        reply = QMessageBox.warning(
            self,
            "未保存的标注",
            "当前影像的标注尚未保存，是否保存？",
            QMessageBox.StandardButton.Save
            | QMessageBox.StandardButton.Discard
            | QMessageBox.StandardButton.Cancel,
            QMessageBox.StandardButton.Save,
        )
        if reply == QMessageBox.StandardButton.Save:
            return self._save()
        return reply == QMessageBox.StandardButton.Discard

    # ------------------------------------------------------------------ 标注
    def _set_mode(self, mode: str) -> None:
        self.view.set_mode(mode)
        for action in self._tool_group.actions():
            action.setChecked(action.data() == mode)
        self._show_status(_MODE_HINTS.get(mode, ""))

    def _on_polygon_finished(self, points) -> None:
        if self._document is None or not points:
            return
        label_id = self.panel.current_label_id()
        if not label_id:
            self._show_status("请先在右侧添加并选择标签")
            return
        if self._document.fill_polygon(points, label_id):
            self._show_status("已添加多边形")
        else:
            self._show_status("多边形为空，未写入")

    def _undo(self) -> None:
        if self._document is not None:
            self._document.undo()
            self.view.clear_preview()

    def _redo(self) -> None:
        if self._document is not None:
            self._document.redo()
            self.view.clear_preview()

    def _clear_current_label(self) -> None:
        if self._document is None:
            return
        label_id = self.panel.current_label_id()
        if label_id and self._document.clear_class(label_id):
            self._show_status("已清空当前标签")
        else:
            self._show_status("当前标签没有像素")

    def _clear_all(self) -> None:
        if self._document is None:
            return
        reply = QMessageBox.question(
            self,
            "清空全部标注",
            "确定清空当前影像的全部标注？该操作可以撤销。",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if reply == QMessageBox.StandardButton.Yes:
            self._document.clear_all()
            self.view.clear_preview()
            self._show_status("已清空全部标注")

    # ------------------------------------------------------------------ SAM 状态
    def _refresh_sam_availability(self) -> None:
        problem = self._sam.check_environment()
        self.panel.set_sam_available(problem is None, problem or "")
        if problem:
            self._sam_ready = False
            self._sam_family = ""
            self.panel.set_sam_family("")
        elif not self._sam.is_ready and not self._model_loading:
            self.panel.set_model_status("未加载模型")
        self.panel.set_checkpoint(self._config.sam.checkpoint)
        self._refresh_actions()

    def _choose_checkpoint(self) -> None:
        start = str(Path(self._config.sam.checkpoint).parent) if self._config.sam.checkpoint else ""
        path, _ = QFileDialog.getOpenFileName(
            self, "选择 SAM 权重", start, "PyTorch 权重 (*.pth *.pt);;所有文件 (*)"
        )
        if not path:
            return
        entry = model_for_checkpoint(path)
        family = entry.family if entry is not None else infer_family(path)
        sam2_config = entry.config if entry is not None else ""
        if family == "sam2" and not sam2_config:
            sam2_config = sam2_config_for_filename(path)
        model_type = entry.model_type if entry is not None and entry.family == "sam1" else "auto"
        save_checkpoint_override(path)
        save_sam_setting(SETTING_MODEL_ID, entry.id if entry is not None else "")
        save_sam_setting(SETTING_SAM2_CONFIG, sam2_config)
        save_sam_setting(SETTING_MODEL_TYPE, model_type)
        self._config = replace(
            self._config,
            sam=replace(
                self._config.sam,
                checkpoint=str(Path(path)),
                model_id=entry.id if entry is not None else "",
                sam2_config=sam2_config,
                model_type=model_type,
            ),
        )
        self._sam.config = self._config.sam
        self.panel.set_checkpoint(path)
        self.panel.set_model_type(model_type)
        self._refresh_sam_availability()
        self._show_status("SAM 权重已更新，点击「加载模型」生效")

    def _open_sam_settings(self) -> None:
        """模态打开 SAM 设置；控件已即时写 QSettings，关闭后统一应用。"""
        old = self._config.sam
        dialog = SamSettingsDialog(old, self)
        try:
            dialog.exec()
            new = dialog.config()
        finally:
            dialog.deleteLater()
        self._apply_sam_settings(old, new)

    def _apply_sam_settings(self, old: SamConfig, new: SamConfig) -> None:
        restart_keys = (
            "python",
            "sam_src",
            "checkpoint",
            "model_id",
            "sam2_config",
            "model_type",
            "device",
        )
        needs_restart = any(getattr(old, key) != getattr(new, key) for key in restart_keys)
        self._config = replace(self._config, sam=new)
        self._sam.config = new
        self.panel.set_checkpoint(new.checkpoint)
        self.panel.set_model_type(new.model_type)
        self.panel.set_device(new.device)
        self._refresh_sam_availability()
        if new.cache_enabled and self._sam.is_ready and self._document is not None:
            # 让新目录/开关在下一张影像时生效（当前影像的特征已编码）
            self._show_status("缓存设置已更新，将在下一次载入影像时生效")
        if needs_restart and (self._sam.is_ready or self._model_loading or self._sam.is_busy):
            self._model_loading = False
            try:
                self._sam.stop()
            except Exception:
                logger.exception("应用 SAM 设置时停止子进程失败")
            self._sam_ready = False
            self._refresh_sam_availability()
            if self._sam.check_environment() is None:
                self.panel.set_model_status("配置已更新，请重新加载模型")
            self._show_status("SAM 设置已更新，请重新加载模型")
        elif needs_restart:
            self._show_status("SAM 设置已更新，点击「加载模型」生效")
        else:
            self._show_status("SAM 设置已更新")

    def _on_model_config_changed(self, _index: int = -1) -> None:
        model_type = self.panel.model_type()
        device = self.panel.device()
        self._config = replace(
            self._config,
            sam=replace(
                self._config.sam,
                model_type=model_type,
                device=device,
            ),
        )
        self._sam.config = self._config.sam
        save_sam_setting(SETTING_MODEL_TYPE, model_type)
        save_sam_setting(SETTING_DEVICE, device)
        if self._sam.is_ready:
            self._show_status("模型配置已修改，点击「重新加载」生效")

    def _load_model(self) -> None:
        if self._model_loading:
            self._show_status("模型正在加载，请稍候")
            return
        problem = self._sam.check_environment()
        if problem:
            self.panel.set_sam_available(False, problem)
            self._show_status(problem)
            return
        self._model_loading = True
        self._sam_family = ""
        self.panel.set_sam_family("")
        self.panel.set_model_status("正在启动 SAM 子进程…")
        self._sam.start(self._on_model_loaded)
        self._refresh_actions()

    def _on_model_loaded(self, data: dict | None, error: str | None) -> None:
        self._model_loading = False
        if error:
            self._sam_ready = False
            self._sam_family = ""
            self.panel.set_sam_family("")
            self.panel.set_model_status(error)
            self.panel.set_sam_available(True)
            self._show_status(f"SAM 加载失败：{error}")
            self._refresh_actions()
            return
        self._sam_ready = True
        data = data or {}
        device = data.get("device") or ""
        family = str(data.get("family") or "").upper()
        model_label = data.get("model_id") or data.get("model_type") or ""
        detail = " / ".join(part for part in (family, str(model_label), str(device)) if part)
        self._sam_family = str(data.get("family") or "").lower()
        self.panel.set_sam_family(self._sam_family)
        self.panel.set_model_status(f"已就绪（{detail or '?'}）", ready=True)
        self.panel.set_sam_available(True)
        self._show_status("SAM 模型已就绪")
        if self._document is not None:
            self._send_sam_image()
        self._refresh_actions()

    def _on_sam_status(self, text: str) -> None:
        self.panel.set_model_status(text, ready=self._sam.is_ready)
        self._show_status(text)

    def _on_sam_ready(self, ready: bool) -> None:
        self._sam_ready = ready
        if not ready:
            self._sam_family = ""
            self.panel.set_sam_family("")
            self.panel.set_model_status(self._sam.status or "模型不可用")
        self._refresh_actions()

    def _on_sam_error(self, message: str) -> None:
        self._model_loading = False
        self.panel.set_model_status(message, ready=self._sam.is_ready)
        self.panel.set_sam_available(True)
        self._show_status(message)

    def _on_sam_progress(self, elapsed: float) -> None:
        self._show_status(f"自动分割中… 已用 {elapsed:.0f} 秒")

    def _on_sam_busy(self, busy: bool) -> None:
        self.panel.set_busy(busy)
        self._refresh_actions()

    def _refresh_actions(self) -> None:
        document = self._document
        sam_ready = self._sam.is_ready and not self._sam.is_busy
        self.act_sam_point.setEnabled(sam_ready)
        self.act_sam_box.setEnabled(sam_ready)
        self.act_sam_scribble.setEnabled(sam_ready)
        self.act_auto.setEnabled(sam_ready and document is not None)
        self.act_load_model.setEnabled(not self._sam.is_busy)
        self.act_save.setEnabled(document is not None)
        self.act_save_as.setEnabled(document is not None)
        self.act_clear_label.setEnabled(document is not None)
        self.act_clear_all.setEnabled(document is not None)
        if (
            document is not None
            and self.view.mode in (MODE_SAM_POINT, MODE_SAM_BOX, MODE_SAM_SCRIBBLE)
            and not self._sam.is_ready
        ):
            self._set_mode(MODE_PAN)

    # ------------------------------------------------------------------ SAM 推理
    def _send_sam_image(self) -> None:
        image = self._canvas_image or self._preview_image
        if self._document is None or image is None or not self._sam.is_ready:
            return
        try:
            self._temp_dir.mkdir(parents=True, exist_ok=True)
            path = self._temp_dir / "sam_input.png"
            if not image.save(str(path), "PNG"):
                raise OSError("预览图写出失败")
        except Exception as exc:  # noqa: BLE001 —— 临时图写失败要转成状态栏提示
            self._show_status(f"准备 SAM 输入失败：{exc}")
            return
        self._sam_image_token = self._sam_token
        self.panel.set_busy(True)
        self._sam.set_image(
            str(path),
            self._sam_token,
            lambda data, error: self._on_sam_image_ready(data, error),
        )

    def _on_sam_image_ready(self, data: dict | None, error: str | None) -> None:
        if error:
            # 影像没有送进 SAM，后续文字 / 自动分割不能误判为就绪
            self._sam_image_token = ""
            self._show_status(f"SAM 设置影像失败：{error}")
            return
        if (data or {}).get("token") != self._sam_token:
            return
        self._show_status("SAM 已就绪，可框选样本批量提取同类、文字分割或自动分割")

    def _on_sam_prompt_changed(self, points) -> None:
        self._sam_points = list(points)
        if not self._sam.is_ready:
            self._show_status("请先加载 SAM 模型")
            return
        if not points and self.view.sam_box() is None:
            self.view.clear_preview()
            return
        self._predict_timer.start()

    def _on_sam_box_changed(self, rect) -> None:
        self._sam_box = rect
        if rect is not None:
            # 应用样本后画布会清掉提示框，保留一份供「批量提取同类」复用
            self._last_sample_box = QRectF(rect)
        if rect is None:
            if not self._sam_points:
                self.view.clear_preview()
            return
        if not self._sam.is_ready:
            self._show_status("请先加载 SAM 模型")
            return
        self._predict_timer.start()

    def _predict_now(self) -> None:
        if self._document is None or not self._sam.is_ready:
            return
        box = self.view.sam_box()
        if not self._sam_points and box is None:
            return
        points = [[x, y] for x, y, _label in self._sam_points]
        labels = [label for _x, _y, label in self._sam_points]
        box_list = [box.left(), box.top(), box.right(), box.bottom()] if box is not None else None
        self.panel.set_busy(True)
        self._sam.predict(
            points=points,
            labels=labels,
            box=box_list,
            token=self._sam_token,
            callback=self._on_predict_result,
        )

    def _on_predict_result(self, data: dict | None, error: str | None) -> None:
        if error:
            self._show_status(f"SAM 推理失败：{error}")
            return
        if (data or {}).get("token") != self._sam_token:
            return
        masks = (data or {}).get("masks") or []
        self._set_sam_candidates(masks)
        if not masks:
            self.view.clear_preview()
            self._show_status("SAM 没有找到目标，试试调整提示点")
            return
        label_id = self.panel.current_label_id()
        label = self._labels.get(label_id)
        self.view.set_preview_mask(QImage(masks[0]["path"]), label.color if label else None)
        self._show_status(f"SAM 生成 {len(masks)} 个候选，Enter 应用最高分（得分 {masks[0]['score']:.2f}）")

    def _apply_sam_preview(self) -> None:
        if not self._sam_candidates:
            self._show_status("没有可应用的 SAM 候选")
            return
        label_id = self.panel.current_label_id()
        if not label_id:
            self._show_status("请先在右侧添加并选择标签")
            return
        if self._document is None:
            return
        best = self._sam_candidates[0]
        if self._document.apply_candidate_files(
            [best["path"]], label_id, only_unlabeled=False, text="SAM 掩膜"
        ):
            self._show_status("已应用 SAM 掩膜")
        else:
            self._show_status("SAM 掩膜为空，未写入")
        self._set_sam_candidates([])
        self.view.clear_preview()
        self.view.clear_prompts()

    def _run_auto(self) -> None:
        if self._document is None:
            self._show_status("请先打开影像")
            return
        if not self._sam.is_ready:
            self._show_status("请先加载 SAM 模型")
            return
        if self._sam_image_token != self._sam_token:
            self._show_status("影像尚未送入 SAM，请稍候重试")
            return
        params = self.panel.auto_params()
        params["max_side"] = self._config.auto.max_side
        self.panel.set_busy(True)
        self._show_status("正在自动分割，请稍候…")
        self._sam.auto(self._sam_token, params, self._on_auto_result)

    def _on_auto_result(self, data: dict | None, error: str | None) -> None:
        self.panel.set_busy(False)
        if error:
            self._show_status(f"自动分割失败：{error}")
            return
        if (data or {}).get("token") != self._sam_token:
            return
        self._set_sam_candidates((data or {}).get("masks") or [])
        self.view.clear_preview()
        if self._sam_candidates:
            self._show_status(f"自动分割完成：{len(self._sam_candidates)} 个候选，勾选后应用")
        else:
            self._show_status("自动分割没有生成候选，可降低 IoU / 稳定性阈值")

    def _run_exemplar(self, threshold: float) -> None:
        """SAM3 范例标注：框住 1 个样本，批量找出所有同类地物。"""
        if self._document is None:
            self._show_status("请先打开影像")
            return
        if not self._sam.is_ready:
            self._show_status("请先加载 SAM 模型")
            return
        if self._sam_family != "sam3":
            self._show_status("范例标注仅支持 SAM3 / SAM3.1 模型")
            return
        if self._sam_image_token != self._sam_token:
            self._show_status("影像尚未送入 SAM，请稍候重试")
            return
        box = self.view.sam_box() or self._last_sample_box
        if box is None:
            self._show_status("请先用框选工具框住 1 个样本，再点「批量提取同类」")
            return
        # 停掉框选触发的单实例防抖请求，避免抢占互斥队列
        self._predict_timer.stop()
        self.panel.set_busy(True)
        self._show_status("正在按范例提取所有同类地物…")
        self._sam.exemplar(
            self._sam_token,
            [box.left(), box.top(), box.right(), box.bottom()],
            threshold,
            self._on_exemplar_result,
        )

    def _on_exemplar_result(self, data: dict | None, error: str | None) -> None:
        self.panel.set_busy(False)
        if error:
            self._show_status(f"范例标注失败：{error}")
            return
        if (data or {}).get("token") != self._sam_token:
            return
        masks = (data or {}).get("masks") or []
        self._set_sam_candidates(masks)
        self.view.clear_preview()
        if not masks:
            self._show_status("没有找到与范例同类的地物，可降低置信度或重新框选")
            return
        # 批量提取默认全选；应用时仍只写入未标注像素，不会覆盖样本
        self.panel.check_all_candidates(True)
        label = self._labels.get(self.panel.current_label_id())
        self.view.set_preview_mask(QImage(masks[0]["path"]), label.color if label else None)
        self._show_status(
            f"范例标注找到 {len(masks)} 个同类地物，已全选；点「应用选中」批量写入"
        )

    def _run_text_prompt(self, text: str, threshold: float) -> None:
        text = (text or "").strip()
        if not text:
            return
        if self._document is None:
            self._show_status("请先打开影像")
            return
        if not self._sam.is_ready:
            self._show_status("请先加载 SAM 模型")
            return
        if self._sam_family != "sam3":
            self._show_status("文字提示仅支持 SAM3 / SAM3.1 模型")
            return
        if self._sam_image_token != self._sam_token:
            self._show_status("影像尚未送入 SAM，请稍候重试")
            return
        # SAM3 文本编码器只认英文，中文先走内置地物术语表
        translation = translate_prompt(text)
        if translation.unknown:
            self._show_status(
                f"未识别中文：{'、'.join(translation.unknown)}；请改用英文或简化描述"
            )
            return
        if not translation.english:
            self._show_status("未能识别提示中的地物，请改用英文描述（如 building / road）")
            return
        prompt = translation.english
        # 停掉点选/框选防抖，避免 120ms 后抢占互斥队列
        self._predict_timer.stop()
        self.panel.set_busy(True)
        if translation.has_chinese:
            self._show_status(f"正在按「{prompt}」（原文：{text}）分割…")
        else:
            self._show_status(f"正在按文字「{prompt}」分割…")
        self._sam.text(self._sam_token, prompt, threshold, self._on_text_result)

    def _on_text_result(self, data: dict | None, error: str | None) -> None:
        self.panel.set_busy(False)
        if error:
            self._show_status(f"文字分割失败：{error}")
            return
        if (data or {}).get("token") != self._sam_token:
            return
        text = str((data or {}).get("text") or "")
        masks = (data or {}).get("masks") or []
        self._set_sam_candidates(masks)
        self.view.clear_preview()
        if not masks:
            self._show_status(f"没有匹配「{text}」的目标，可换个说法或降低置信度阈值")
            return
        label = self._labels.get(self.panel.current_label_id())
        self.view.set_preview_mask(QImage(masks[0]["path"]), label.color if label else None)
        self._show_status(f"文字「{text}」生成 {len(masks)} 个候选，勾选后应用")

    def _preview_candidate(self, index: int) -> None:
        if not (0 <= index < len(self._sam_candidates)):
            return
        label = self._labels.get(self.panel.current_label_id())
        self.view.set_preview_mask(
            QImage(self._sam_candidates[index]["path"]), label.color if label else None
        )

    def _apply_candidates(self) -> None:
        if self._document is None or not self._sam_candidates:
            return
        indexes = self.panel.selected_candidate_indexes()
        if not indexes:
            self._show_status("请先勾选要应用的候选掩膜")
            return
        label_id = self.panel.current_label_id()
        if not label_id:
            self._show_status("请先在右侧添加并选择标签")
            return
        paths = [
            self._sam_candidates[index]["path"]
            for index in indexes
            if 0 <= index < len(self._sam_candidates)
        ]
        count = self._document.apply_candidate_files(
            paths, label_id, only_unlabeled=True, text="应用自动分割候选"
        )
        self.view.clear_preview()
        if count:
            self._show_status(f"已应用 {count} 个候选（只写入未标注像素）")
        else:
            self._show_status("选中的候选与已有标注重叠，未写入")

    def _clear_candidates(self) -> None:
        self._set_sam_candidates([])
        self.view.clear_preview()

    def _set_sam_candidates(self, candidates: list[dict]) -> None:
        """统一更新候选来源与面板列表，避免两处状态漂移。"""
        self._sam_candidates = list(candidates)
        self.panel.set_candidates(self._sam_candidates)

    # ------------------------------------------------------------------ 状态 / 标题
    def _show_status(self, text: str) -> None:
        if text:
            self.statusBar().showMessage(text, 8000)

    def _on_cursor_moved(self, point) -> None:
        if self._document is None:
            self._coord_label.setText("—")
            return
        self._coord_label.setText(f"x {point.x():.0f}  y {point.y():.0f}")

    def _on_zoom_changed(self, scale: float) -> None:
        if getattr(self, "_zoom_label", None) is not None:
            self._zoom_label.setText(f"{scale * 100:.0f}%")

    def _on_dirty_changed(self, _dirty: bool) -> None:
        self._update_title()

    def _update_title(self) -> None:
        name = Path(self._current_image).name if self._current_image else "未打开影像"
        if self._grid_spec is not None and self._current_tile is not None:
            row, col = self._current_tile
            name = (
                f"{name} · 宫格 {row + 1}-{col + 1}"
                f"／{self._grid_spec.rows}×{self._grid_spec.cols}"
            )
        if self._project_path is not None:
            project = self._project_path.stem
        elif self._project_dirty:
            project = "未命名工程"
        else:
            project = ""
        prefix = f"{project}{'●' if self._project_dirty else ''} — " if project else ""
        dirty = bool(self._document is not None and self._document.is_dirty)
        title = f"{prefix}{name}{' *' if dirty else ''} — 影像标注"
        self.setWindowTitle(title)
        label = getattr(self, "_title_label", None)
        if label is not None:
            label.setText(title)

    def _refresh_undo_actions(self) -> None:
        stack = self._document.undo_stack if self._document is not None else None
        self.act_undo.setEnabled(bool(stack and stack.canUndo()))
        self.act_redo.setEnabled(bool(stack and stack.canRedo()))

    # ------------------------------------------------------------------ 帮助
    def _show_help(self) -> None:
        QMessageBox.information(
            self,
            "使用说明",
            "1. 打开影像或文件夹，左侧切换影像；导入时可勾选「切分为宫格」，"
            "选择块大小与重叠度，在左侧「宫格」tab 逐块标注；\n"
            "2. 在右侧添加标签并选中，使用多边形 / 画笔 / 橡皮擦标注；\n"
            "3. Ctrl+Z / Ctrl+Y 撤销重做；\n"
            "4. 配置 SAM 权重后点击「加载模型」，可用点选（P）、框选（X）、"
            "涂鸦（S）、自动分割（F4）生成候选掩膜，Enter 应用；"
            "涂鸦时右键或 Shift+左键可画负笔画；\n"
            "5. Ctrl+S 保存为 `<影像名>_mask.png` + 同名 JSON；宫格模式每块保存到"
            " `<影像名>_grid/images/r01_c01.png`（原始切片）与"
            " `<影像名>_grid/masks/r01_c01.png`（标注掩膜），两个文件夹内文件名"
            "一一对应（行列从 1 开始，与宫格面板编号一致），勾选后可同时输出 GeoTIFF；\n"
            "6. 用「工程 → 保存标注工程」把影像清单、宫格参数、标签、进度和上次位置"
            "存到 `.qann` 文件；退出标注窗口时会提示保存。\n\n"
            "SAM 权重不随软件分发。请在「功能 → SAM 设置…」中安装依赖、下载 SAM1/SAM2/SAM3 "
            "模型（SAM3 权重需在 HuggingFace 申请授权），并在「模型选择」里加载权重。",
        )

    def _show_about(self) -> None:
        QMessageBox.about(
            self,
            "关于影像标注",
            "影像标注（EISeg 风格）\n\n"
            "手动标注：多边形 / 画笔 / 橡皮擦\n"
            "半自动与自动标注：Meta Segment Anything (SAM)\n"
            "SAM 推理在外部 Python 子进程中运行。",
        )

    # ------------------------------------------------------------------ 关闭
    def dataset_hint_dir(self) -> str:
        """训练页的默认扫描目录：标注输出目录 → 当前影像目录 → 工程目录。"""
        output = self.image_dock.output_dir()
        if output:
            return output
        if self._current_image:
            return str(Path(self._current_image).parent)
        if self._project_path is not None:
            return str(self._project_path.parent)
        return ""

    def confirm_leave(self) -> bool:
        """询问是否放弃未保存标注 / 保存标注工程；返回是否允许离开。"""
        return self._confirm_discard() and self._confirm_save_project()

    def _request_close(self) -> None:
        self.close_requested.emit()

    def closeEvent(self, event) -> None:
        # 页面由主窗口的 QStackedWidget 管理：不自行隐藏，交给宿主切回地图页。
        event.ignore()
        self.close_requested.emit()

    def shutdown(self, confirm: bool = True) -> bool:
        """应用退出前清理：确认未保存内容 → 停 SAM → 按配置清缓存 → 删临时目录。

        幂等：主窗口 ``closeEvent`` 与 ``aboutToQuit`` 都可能调用。用户取消时
        返回 False 且不做任何不可逆清理，页面仍可继续使用。

        ``confirm=False`` 供宿主在用户已通过 Ctrl+W 主动关闭页面（``confirm_leave``
        已跑过）后调用，避免退出时拿同一份未保存内容再问一次——尤其是用户当时
        选了「放弃」，再次弹「是否保存」会让人莫名其妙。
        """
        if self._shutdown_done:
            return True
        if self._shutting_down:
            return False
        if confirm and not self.confirm_leave():
            return False
        self._shutting_down = True
        try:
            try:
                self._sam.stop()
            except Exception:
                logger.exception("停止 SAM 子进程失败")
            if self._config.sam.cache_clear_on_close:
                try:
                    removed, freed = clear_cache(self._config.sam.cache_dir)
                    if removed:
                        logger.info("已清空 SAM 特征缓存：%s 个文件 / %s 字节", removed, freed)
                except Exception:
                    logger.exception("清空 SAM 特征缓存失败")
            shutil.rmtree(self._temp_dir, ignore_errors=True)
        finally:
            self._shutting_down = False
            self._shutdown_done = True
        return True
