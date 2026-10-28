"""把当前地图导出为图片 / 矢量文件的 Dialog（无模态）。

- 输出范围：当前视图 / 全部图层范围 / 指定某个图层的范围；下方实时显示
  四至坐标（按工程坐标系）。对话框无模态，打开后仍可平移 / 缩放地图，
  「当前视图」的四至与输出尺寸会跟着刷新。
- 比例尺、分辨率、输出像素**全部可编辑且双向联动**（与 QGIS「地图另存为
  图像」一致）：改比例尺 → 宽高重算；改宽 / 高 → 比例尺重算；改 DPI →
  同比例尺下宽高重算。比例尺可从常用值下拉选择，也可直接输入。
- 格式：PNG / JPEG / TIFF / WebP / BMP / PDF / SVG。
- 导出后可按勾选把位图加载回地图：同时按输出范围写 world file 与 CRS
  sidecar，导出「当前视图」时加载的图层正好覆盖当前视图范围。PDF / SVG
  不能作为栅格图层加载。
"""

from __future__ import annotations

import contextlib
import os
from typing import TYPE_CHECKING

from qgis.PyQt import sip
from qgis.core import (
    QgsCoordinateTransform,
    QgsMapLayer,
    QgsMapSettings,
    QgsProject,
    QgsRasterLayer,
    QgsRectangle,
    QgsUnitTypes,
)
from qgis.PyQt.QtCore import QDateTime, QEventLoop, QStandardPaths, Qt, QTimer
from qgis.PyQt.QtGui import QColor
from qgis.PyQt.QtWidgets import (
    QApplication,
    QCheckBox,
    QComboBox,
    QGridLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPushButton,
    QSpinBox,
    QVBoxLayout,
    QWidget,
)

from app.core.domain.export_map_image import (
    DEFAULT_MAP_IMAGE_FORMAT,
    MAP_IMAGE_FORMATS,
    MAX_IMAGE_PIXELS,
    MAX_IMAGE_SIDE,
    PDF_MAX_PAGE_MM,
    ExportMapImageError,
    ExportMapSpec,
    MapImageFormat,
    ensure_extension,
    export_map_image,
    format_from_path,
    pdf_page_size_mm,
    scale_for_width,
    size_for_scale,
)
from app.infra.logging import get_logger
from app.infra.messagebar import send
from app.ui.common.base_dialog import BaseDialog
from app.ui.common.layer_order import layers_in_tree_order
from app.ui.common.message_box import MessageBox
from app.ui.common.validation import focus_and_highlight
from app.ui.common.widgets import save_file

logger = get_logger(__name__)

if TYPE_CHECKING:
    from qgis.core import QgsCoordinateReferenceSystem, QgsMapLayer
    from qgis.gui import QgsMapCanvas

#: 背景选项：下拉显示名 → 颜色。``transparent`` 只对支持 alpha 的格式可用。
_BACKGROUNDS: tuple[tuple[str, str, QColor], ...] = (
    ("白色", "white", QColor(255, 255, 255)),
    ("透明（仅 PNG / TIFF / WebP / SVG）", "transparent", QColor(0, 0, 0, 0)),
    ("黑色", "black", QColor(0, 0, 0)),
)

#: 常用比例尺（1:N 的 N）。下拉里可选，也可在编辑框里直接输入任意值。
_SCALE_PRESETS: tuple[int, ...] = (
    100,
    200,
    500,
    1000,
    2000,
    2500,
    5000,
    10000,
    25000,
    50000,
    100000,
    200000,
    250000,
    500000,
    1000000,
    2000000,
    5000000,
    10000000,
    25000000,
    50000000,
    100000000,
)

#: 输出像素 / 比例尺显示的上限（真正导出上限由服务校验，这里只保证输入框
#: 不会把比例尺换算出的超大值悄悄截断）。
_DISPLAY_MAX = 1_000_000_000

#: 输出范围下拉项的稳定键
_EXTENT_VIEW = "view"
_EXTENT_FULL = "full"
_EXTENT_LAYER = "layer"


class ExportMapImageDialog(BaseDialog):
    """导出图片 / 矢量文件 Dialog。"""

    def __init__(
        self,
        parent=None,
        title: str = "",
        *,
        canvas: QgsMapCanvas,
        project_path: str | None = None,
    ) -> None:
        super().__init__(
            parent,
            title or "导出图片",
            show_min_button=False,
            show_max_button=False,
        )
        if canvas is None:
            raise ValueError("导出图片需要地图画布")
        self._canvas = canvas
        self.project = QgsProject.instance()
        self._project_path = project_path
        self._confirmed_overwrite_path = ""
        self._syncing = False
        self._exporting = False
        self._scale_value = 1.0

        self._view_extent = QgsRectangle()
        #: 全部图层范围 / 单个图层范围都**按需计算**：首次打开不遍历所有图层
        #: 的 extent / 坐标变换（重数据源第一次取 extent 很慢），选中时才算。
        self._full_extent: QgsRectangle | None = None
        self._layers: list = []
        self._layer_by_id: dict[str, object] = {}
        self._layer_extents: dict[str, QgsRectangle] = {}
        self._refresh_view_extent()
        self._refresh_layer_list()

        self._aspect = self._aspect_for(self._default_extent())
        #: 比例尺输入防抖：用户敲 "1:5000" 时别在 "1" / "1:5" 这些中间态
        #: 反复重算宽高（会闪一下几十万像素）。
        self._scale_timer = QTimer(self)
        self._scale_timer.setSingleShot(True)
        self._scale_timer.setInterval(250)
        self._scale_timer.timeout.connect(self._apply_scale_text)
        #: 显示「指定图层」行后，布局要等下一次事件循环才把 sizeHint 算高；
        #: 立刻取 sizeHint 还是旧值，所以延后一拍再把窗口撑高。
        self._grow_timer = QTimer(self)
        self._grow_timer.setSingleShot(True)
        self._grow_timer.setInterval(0)
        self._grow_timer.timeout.connect(self._grow_to_fit)
        self._init_ui()
        self._connect_canvas_signals()

    # ----------------------------------------------------------- 数据刷新

    def _canvas_map_settings(self) -> QgsMapSettings:
        return QgsMapSettings(self._canvas.mapSettings())

    def _refresh_view_extent(self) -> None:
        try:
            extent = self._canvas_map_settings().extent()
        except Exception:  # noqa: BLE001 —— 画布 / 工程正在销毁
            return
        if self._is_valid_extent(extent):
            self._view_extent = QgsRectangle(extent)

    def _refresh_full_extent(self) -> QgsRectangle:
        """计算全部图层范围（按需 + 缓存；图层 / 坐标系变化时清空缓存）。"""
        if self._full_extent is not None:
            return self._full_extent
        try:
            extent = self._canvas.fullExtent()
        except Exception:  # noqa: BLE001 —— 画布销毁 / 工程正在切换
            extent = None
        self._full_extent = (
            QgsRectangle(extent) if self._is_valid_extent(extent) else QgsRectangle()
        )
        return self._full_extent

    def _refresh_layer_list(self) -> None:
        """只收集图层对象（不取 extent）：首次打开要尽量快。"""
        try:
            candidates = layers_in_tree_order(self.project)
        except Exception:  # noqa: BLE001 —— 极端情况下工程正在清空
            logger.warning("读取图层列表失败", exc_info=True)
            candidates = []
        self._layers = [
            layer
            for layer in candidates
            if layer is not None and not sip.isdeleted(layer)
        ]
        self._layer_by_id = {layer.id(): layer for layer in self._layers}
        self._layer_extents.clear()
        self._full_extent = None

    def _layer_extent(self, layer_id: str) -> QgsRectangle:
        """按需计算并缓存某个图层的范围（变换到画布坐标系）。"""
        if layer_id in self._layer_extents:
            return self._layer_extents[layer_id]
        layer = self._layer_by_id.get(layer_id)
        extent = self._extent_of_layer(layer) if layer is not None else QgsRectangle()
        self._layer_extents[layer_id] = extent
        return extent

    def _extent_of_layer(self, layer: QgsMapLayer) -> QgsRectangle:
        """图层范围变换到画布坐标系；失败返回空矩形。"""
        try:
            if layer is None or sip.isdeleted(layer) or not layer.isValid():
                return QgsRectangle()
            extent = layer.extent()
            if not self._is_valid_extent(extent):
                return QgsRectangle()
            source_crs = layer.crs()
            target_crs = self._destination_crs()
            if (
                source_crs.isValid()
                and target_crs.isValid()
                and source_crs != target_crs
            ):
                transform = QgsCoordinateTransform(
                    source_crs, target_crs, self.project
                )
                extent = transform.transformBoundingBox(extent)
            if not self._is_valid_extent(extent):
                return QgsRectangle()
            return QgsRectangle(extent)
        except Exception:  # noqa: BLE001 —— 单图层范围算不出来不影响其它图层
            logger.warning(
                "计算图层范围失败：%s",
                getattr(layer, "name", lambda: "?")() if layer is not None else "?",
                exc_info=True,
            )
            return QgsRectangle()

    @staticmethod
    def _is_valid_extent(extent: QgsRectangle | None) -> bool:
        return (
            extent is not None
            and not extent.isNull()
            and extent.width() > 0
            and extent.height() > 0
        )

    @staticmethod
    def _aspect_for(extent: QgsRectangle | None) -> float:
        if (
            extent is not None
            and not extent.isNull()
            and extent.height() > 0
            and extent.width() > 0
        ):
            return extent.width() / extent.height()
        return 1.0

    def _destination_crs(self) -> QgsCoordinateReferenceSystem:
        try:
            crs = self._canvas_map_settings().destinationCrs()
        except Exception:  # noqa: BLE001
            crs = None
        if crs is not None and crs.isValid():
            return crs
        return self.project.crs()

    def _default_extent(self) -> QgsRectangle:
        if self._is_valid_extent(self._view_extent):
            return self._view_extent
        return QgsRectangle()

    def _default_dir(self) -> str:
        if self._project_path:
            folder = os.path.dirname(os.path.abspath(self._project_path))
            if os.path.isdir(folder):
                return folder
        pictures = QStandardPaths.writableLocation(
            QStandardPaths.StandardLocation.PicturesLocation
        )
        if pictures and os.path.isdir(pictures):
            return pictures
        return os.path.expanduser("~")

    def _default_path(self, fmt: MapImageFormat) -> str:
        stamp = QDateTime.currentDateTime().toString("yyyyMMdd_HHmmss")
        stem = "地图"
        if self._project_path:
            project_stem = os.path.splitext(os.path.basename(self._project_path))[0]
            if project_stem:
                stem = f"{project_stem}_地图"
        return os.path.join(self._default_dir(), f"{stem}_{stamp}{fmt.extension}")

    # --------------------------------------------------------------- 构建

    @staticmethod
    def _field_label(text: str) -> QLabel:
        """参数区左侧标签：右对齐、垂直居中，保持各行列对齐。"""
        label = QLabel(text)
        label.setAlignment(
            Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter
        )
        return label

    @staticmethod
    def _range_value_label(alignment) -> QLabel:
        label = QLabel()
        label.setAlignment(alignment | Qt.AlignmentFlag.AlignVCenter)
        label.setTextInteractionFlags(
            Qt.TextInteractionFlag.TextSelectableByMouse
        )
        return label

    def _init_ui(self) -> None:
        root = QVBoxLayout()
        root.setContentsMargins(14, 12, 14, 12)
        root.setSpacing(10)

        # 4 列网格：左标签 | 字段 | 右标签 | 字段。宽字段跨 1-3 列，
        # 比例尺 / 分辨率、格式 / 背景各自对半排，上下文本框中线对齐。
        grid = QGridLayout()
        grid.setHorizontalSpacing(8)
        grid.setVerticalSpacing(8)
        grid.setColumnStretch(1, 1)
        grid.setColumnStretch(3, 1)
        row = 0

        # ---- 输出范围 ----
        self.extent_combo = QComboBox()
        for label, key in self._extent_options():
            self.extent_combo.addItem(label, key)
        grid.addWidget(self._field_label("输出范围"), row, 0)
        grid.addWidget(self.extent_combo, row, 1, 1, 3)
        row += 1

        self.layer_combo = QComboBox()
        for layer in self._layers:
            self.layer_combo.addItem(layer.name(), layer.id())
        self._layer_label = self._field_label("指定图层")
        grid.addWidget(self._layer_label, row, 0)
        grid.addWidget(self.layer_combo, row, 1, 1, 3)
        row += 1

        # ---- 范围（ArcGIS 风格：上 / 左 右 / 下）----
        self.range_box = QGroupBox("范围")
        range_layout = QGridLayout(self.range_box)
        range_layout.setContentsMargins(12, 8, 12, 8)
        range_layout.setHorizontalSpacing(16)
        range_layout.setVerticalSpacing(2)
        range_layout.setColumnStretch(0, 1)
        range_layout.setColumnStretch(1, 1)
        range_layout.setColumnStretch(2, 1)
        self.north_label = self._range_value_label(Qt.AlignmentFlag.AlignHCenter)
        self.south_label = self._range_value_label(Qt.AlignmentFlag.AlignHCenter)
        self.west_label = self._range_value_label(Qt.AlignmentFlag.AlignLeft)
        self.east_label = self._range_value_label(Qt.AlignmentFlag.AlignRight)
        range_layout.addWidget(self.north_label, 0, 1)
        range_layout.addWidget(self.west_label, 1, 0)
        range_layout.addWidget(self.east_label, 1, 2)
        range_layout.addWidget(self.south_label, 2, 1)
        grid.addWidget(self.range_box, row, 0, 1, 4)
        row += 1

        # ---- 比例尺 / 分辨率（各自标签 + 文本框）----
        self.scale_combo = QComboBox()
        self.scale_combo.setEditable(True)
        self.scale_combo.setInsertPolicy(QComboBox.InsertPolicy.NoInsert)
        for value in _SCALE_PRESETS:
            self.scale_combo.addItem(f"1:{value}", value)
        self.scale_combo.setMinimumWidth(150)
        self.scale_combo.lineEdit().setPlaceholderText("1:5000 或 5000")
        self.scale_combo.setToolTip(
            "可从常用比例尺中选择，也可直接输入（如 1:5000 或 5000）"
        )
        self.dpi_spin = QSpinBox()
        self.dpi_spin.setRange(1, 2400)
        self.dpi_spin.setSuffix(" dpi")
        self.dpi_spin.setSingleStep(10)
        self.dpi_spin.setMinimumWidth(90)
        self.dpi_spin.setToolTip("输出分辨率；同样比例尺下 DPI 越高像素越多、越清晰")
        grid.addWidget(self._field_label("比例尺"), row, 0)
        grid.addWidget(self.scale_combo, row, 1)
        grid.addWidget(self._field_label("分辨率"), row, 2)
        grid.addWidget(self.dpi_spin, row, 3)
        row += 1

        # ---- 输出宽度 / 高度（上下两行，文本框同宽对齐）----
        self.width_spin = QSpinBox()
        self.width_spin.setRange(1, _DISPLAY_MAX)
        self.width_spin.setSingleStep(100)
        self.width_spin.setSuffix(" px")
        self.width_spin.setGroupSeparatorShown(True)
        self.height_spin = QSpinBox()
        self.height_spin.setRange(1, _DISPLAY_MAX)
        self.height_spin.setSingleStep(100)
        self.height_spin.setSuffix(" px")
        self.height_spin.setGroupSeparatorShown(True)
        grid.addWidget(self._field_label("输出宽度"), row, 0)
        grid.addWidget(self.width_spin, row, 1, 1, 3)
        row += 1
        grid.addWidget(self._field_label("输出高度"), row, 0)
        grid.addWidget(self.height_spin, row, 1, 1, 3)
        row += 1
        self.lock_aspect_check = QCheckBox("锁定宽高比")
        self.lock_aspect_check.setChecked(True)
        self.lock_aspect_check.setToolTip(
            "锁定宽高比：改宽度或高度时另一边按输出范围比例自动换算，"
            "地图不变形，加载回地图后与所选范围一致。\n"
            "不锁定：宽高可分别设置，图片像素比例与范围比例不一致时"
            "地图会被横向 / 纵向拉伸（适合固定模板尺寸）。"
        )
        grid.addWidget(self.lock_aspect_check, row, 1, 1, 3)
        row += 1

        # ---- 导出格式 / 背景（同一行）----
        self.format_combo = QComboBox()
        for fmt in MAP_IMAGE_FORMATS:
            self.format_combo.addItem(fmt.label, fmt)
        self.background_combo = QComboBox()
        grid.addWidget(self._field_label("导出格式"), row, 0)
        grid.addWidget(self.format_combo, row, 1)
        grid.addWidget(self._field_label("背景"), row, 2)
        grid.addWidget(self.background_combo, row, 3)
        row += 1

        # ---- 保存位置 ----
        path_widget = QWidget()
        path_row = QHBoxLayout(path_widget)
        path_row.setContentsMargins(0, 0, 0, 0)
        path_row.setSpacing(6)
        self.path_edit = QLineEdit(self._default_path(self._current_format()))
        self.path_edit.setClearButtonEnabled(True)
        self.path_edit.textEdited.connect(self._on_path_edited)
        browse = QPushButton("浏览…")
        browse.clicked.connect(self._on_browse)
        path_row.addWidget(self.path_edit, 1)
        path_row.addWidget(browse)
        grid.addWidget(self._field_label("保存位置"), row, 0)
        grid.addWidget(path_widget, row, 1, 1, 3)
        row += 1

        # ---- 导出后加载到地图（仅位图；按输出范围写世界文件后加入工程）----
        self.load_to_map_check = QCheckBox("导出后加载到地图")
        self.load_to_map_check.setChecked(True)
        grid.addWidget(self.load_to_map_check, row, 1, 1, 3)
        self._update_load_to_map_state(self._current_format())
        row += 1

        root.addLayout(grid)

        self.hint = QLabel()
        self.hint.setObjectName("PropHint")
        self.hint.setWordWrap(True)
        root.addWidget(self.hint)

        self.status_label = QLabel()
        self.status_label.setObjectName("PropHint")
        self.status_label.setWordWrap(True)
        self.status_label.setTextInteractionFlags(
            Qt.TextInteractionFlag.TextSelectableByMouse
        )
        root.addWidget(self.status_label)

        buttons = QHBoxLayout()
        buttons.addStretch(1)
        self.export_btn = QPushButton("导出")
        self.export_btn.setObjectName("DialogPrimary")
        self.export_btn.clicked.connect(self._on_export)
        close = QPushButton("关闭")
        close.clicked.connect(self.reject)
        buttons.addWidget(self.export_btn)
        buttons.addWidget(close)
        root.addLayout(buttons)

        self.setLayout(root)
        self.setMinimumSize(600, 0)

        self._init_values()
        self._connect_signals()
        self._show_layer_row(self._extent_mode() == _EXTENT_LAYER)
        self._refresh_backgrounds(self._current_format())
        self._on_extent_changed()
        self.adjustSize()

    def _extent_options(self) -> list[tuple[str, str]]:
        options: list[tuple[str, str]] = []
        if self._is_valid_extent(self._view_extent):
            options.append(("当前视图", _EXTENT_VIEW))
        # 全部 / 单图层范围按需计算：这里只判断有没有图层，避免打开时
        # 为每个图层取 extent / 做坐标变换。
        if self._layers:
            options.append(("全部图层范围", _EXTENT_FULL))
            options.append(("指定图层范围", _EXTENT_LAYER))
        if not options:
            # 没有任何有效范围也占住一行，导出时统一报「范围无效」，
            # 比打开对话框就崩掉或空下拉好排查。
            options.append(("当前视图", _EXTENT_VIEW))
        return options

    def _init_values(self) -> None:
        """设置首屏默认值（尚未连接信号，不会触发联动）。"""
        view_valid = self._is_valid_extent(self._view_extent)
        if view_valid:
            default_key = _EXTENT_VIEW
        elif self._layers:
            default_key = _EXTENT_FULL
        else:
            default_key = _EXTENT_VIEW
        index = self.extent_combo.findData(default_key)
        if index >= 0:
            self.extent_combo.setCurrentIndex(index)
        if self._layers:
            self.layer_combo.setCurrentIndex(0)

        # 画布设置只拷贝一次：QgsMapSettings 复制会带上整份图层列表
        settings = self._canvas_map_settings()
        dpi = int(round(float(settings.outputDpi() or 96.0)))
        self.dpi_spin.setValue(max(1, min(2400, dpi)))

        default_extent = self._default_extent()
        scale = float(settings.scale() or 0.0)
        if scale <= 0:
            canvas_width = int(settings.outputSize().width())
            scale = scale_for_width(
                default_extent,
                canvas_width,
                self.dpi_spin.value(),
                self._destination_crs(),
                self.project,
            )
        if scale <= 0:
            scale = 5000.0
        self._scale_value = max(0.01, min(scale, 1_000_000_000.0))
        self._set_scale_display(self._scale_value)

        width, height = size_for_scale(
            default_extent,
            self._scale_value,
            self.dpi_spin.value(),
            self._destination_crs(),
            self.project,
        )
        if width <= 0 or height <= 0:
            canvas_size = settings.outputSize()
            width = max(1, int(canvas_size.width()) or 1600)
            height = max(1, int(canvas_size.height()) or 1200)
        self._syncing = True
        try:
            self.width_spin.setValue(min(width, _DISPLAY_MAX))
            self.height_spin.setValue(min(height, _DISPLAY_MAX))
        finally:
            self._syncing = False

    def _connect_signals(self) -> None:
        self.extent_combo.currentIndexChanged.connect(self._on_extent_changed)
        self.layer_combo.currentIndexChanged.connect(self._on_layer_changed)
        self.scale_combo.currentTextChanged.connect(self._on_scale_text_changed)
        self.scale_combo.lineEdit().editingFinished.connect(
            self._on_scale_editing_finished
        )
        self.dpi_spin.valueChanged.connect(self._on_dpi_changed)
        self.width_spin.valueChanged.connect(self._on_width_changed)
        self.height_spin.valueChanged.connect(self._on_height_changed)
        self.lock_aspect_check.toggled.connect(self._on_lock_toggled)
        self.format_combo.currentIndexChanged.connect(self._on_format_changed)

    def _connect_canvas_signals(self) -> None:
        """无模态：地图被平移 / 缩放、图层增删、坐标系变化时同步对话框。"""
        for signal_name, handler in (
            ("extentsChanged", self._on_canvas_extents_changed),
            ("destinationCrsChanged", self._on_canvas_crs_changed),
        ):
            _safe_connect(getattr(self._canvas, signal_name, None), handler, signal_name)
        for signal_name, signal in (
            ("layersAdded", getattr(self.project, "layersAdded", None)),
            ("layersRemoved", getattr(self.project, "layersRemoved", None)),
            ("readProject", getattr(self.project, "readProject", None)),
        ):
            _safe_connect(signal, self._on_project_layers_changed, signal_name)

    # ------------------------------------------------------------- 当前值

    def _current_format(self) -> MapImageFormat:
        fmt = self.format_combo.currentData()
        return fmt if isinstance(fmt, MapImageFormat) else DEFAULT_MAP_IMAGE_FORMAT

    def _extent_mode(self) -> str:
        return str(self.extent_combo.currentData() or _EXTENT_VIEW)

    def _extent_for_mode(self, mode: str) -> QgsRectangle:
        if mode == _EXTENT_VIEW:
            return QgsRectangle(self._view_extent)
        if mode == _EXTENT_FULL:
            return QgsRectangle(self._refresh_full_extent())
        if mode == _EXTENT_LAYER:
            layer_id = str(self.layer_combo.currentData() or "")
            return QgsRectangle(self._layer_extent(layer_id))
        return QgsRectangle()

    def _current_extent(self) -> QgsRectangle:
        return self._extent_for_mode(self._extent_mode())

    def _current_background(self) -> QColor:
        key = self.background_combo.currentData()
        for _label, option_key, color in _BACKGROUNDS:
            if option_key == key:
                return QColor(color)
        return QColor(255, 255, 255)

    def _output_dpi(self) -> float:
        return float(self.dpi_spin.value())

    def _current_output_size(self) -> tuple[int, int]:
        return self.width_spin.value(), self.height_spin.value()

    def _current_scale(self) -> float:
        return float(self._scale_value)

    def _height_for(self, width: int) -> int:
        height = round(width / self._aspect) if self._aspect > 0 else width
        return max(1, min(_DISPLAY_MAX, height))

    # --------------------------------------------------------- 比例尺输入

    @staticmethod
    def _parse_scale_text(text: str) -> float:
        """解析比例尺输入：``1:5000`` / ``5000`` / ``1 : 5,000`` 都接受。"""
        raw = str(text or "").strip().replace(",", "").replace(" ", "")
        if raw.lower().startswith("1:"):
            raw = raw[2:]
        raw = raw.lstrip(":")
        try:
            value = float(raw)
        except ValueError:
            return 0.0
        return value if value > 0 else 0.0

    @staticmethod
    def _format_scale(value: float) -> str:
        if value >= 1:
            return f"1:{value:.0f}"
        return f"1:{value:.4g}"

    def _set_scale_display(self, value: float) -> None:
        self._syncing = True
        try:
            self.scale_combo.setEditText(self._format_scale(value))
        finally:
            self._syncing = False

    def _on_scale_text_changed(self, _text: str) -> None:
        if self._syncing:
            return
        self._scale_timer.start()

    def _apply_scale_text(self) -> None:
        """防抖后应用比例尺输入（非法中间态直接忽略）。"""
        if self._syncing:
            return
        value = self._parse_scale_text(self.scale_combo.currentText())
        if value <= 0:
            return
        self._scale_value = value
        self._sync_size_from_scale()
        self._update_hint()

    def _on_scale_editing_finished(self) -> None:
        """焦点离开比例尺输入框：规范化显示，非法输入还原上一个有效值。

        输入框只显示整数比例尺，而“改宽高反算”得到的是小数比例尺：失焦时
        解析出的整数与内部值相差 <1，属于显示取整，不能反过来按整数重算像素，
        否则用户刚输入的宽度会被悄悄改掉。
        """
        if self._syncing:
            return
        self._scale_timer.stop()
        value = self._parse_scale_text(self.scale_combo.currentText())
        if value <= 0:
            self._set_scale_display(self._scale_value)
            return
        if abs(value - self._scale_value) < 1.0:
            self._set_scale_display(self._scale_value)
            return
        self._scale_value = value
        self._set_scale_display(value)
        self._sync_size_from_scale()
        self._update_hint()

    def _sync_size_from_scale(self) -> None:
        width, height = size_for_scale(
            self._current_extent(),
            self._scale_value,
            self._output_dpi(),
            self._destination_crs(),
            self.project,
        )
        if width <= 0 or height <= 0:
            return
        self._syncing = True
        try:
            self.width_spin.setValue(min(width, _DISPLAY_MAX))
            self.height_spin.setValue(min(height, _DISPLAY_MAX))
        finally:
            self._syncing = False

    def _sync_scale_from_pixels(self) -> None:
        scale = scale_for_width(
            self._current_extent(),
            self.width_spin.value(),
            self._output_dpi(),
            self._destination_crs(),
            self.project,
        )
        if scale <= 0:
            return
        self._scale_value = min(scale, 1_000_000_000.0)
        self._set_scale_display(self._scale_value)

    # --------------------------------------------------------------- 联动

    def _on_extent_changed(self, *_args) -> None:
        extent = self._current_extent()
        if self._is_valid_extent(extent):
            self._aspect = self._aspect_for(extent)
        self._show_layer_row(self._extent_mode() == _EXTENT_LAYER)
        # 换范围时比例尺保持不变，宽高按新范围重算（与 QGIS 一致）
        self._sync_size_from_scale()
        self._update_extent_display()
        self._update_hint()

    def _on_layer_changed(self, *_args) -> None:
        self._on_extent_changed()

    def _on_dpi_changed(self, *_args) -> None:
        if self._syncing:
            return
        self._sync_size_from_scale()
        self._update_hint()

    def _on_width_changed(self, value: int) -> None:
        if self._syncing:
            return
        if self.lock_aspect_check.isChecked():
            self._set_size(value, self._height_for(value))
        self._sync_scale_from_pixels()
        self._update_hint()

    def _on_height_changed(self, value: int) -> None:
        if self._syncing:
            return
        if self.lock_aspect_check.isChecked():
            width = round(value * self._aspect) if self._aspect > 0 else value
            width = max(1, min(_DISPLAY_MAX, width))
            self._set_size(width, value)
        self._sync_scale_from_pixels()
        self._update_hint()

    def _on_lock_toggled(self, checked: bool) -> None:
        if self._syncing:
            return
        if checked:
            self._set_size(
                self.width_spin.value(), self._height_for(self.width_spin.value())
            )
        self._update_hint()

    def _set_size(self, width: int, height: int) -> None:
        """同时设置宽高（阻断两个 valueChanged 的互相递归）。"""
        self._syncing = True
        try:
            self.width_spin.setValue(width)
            self.height_spin.setValue(height)
        finally:
            self._syncing = False

    def _show_layer_row(self, visible: bool) -> None:
        self.layer_combo.setVisible(visible)
        if self._layer_label is not None:
            self._layer_label.setVisible(visible)
        if visible:
            # 显示「指定图层」行后内容变高；Qt 不一定把窗口跟着撑高，
            # 窗口按旧高度会把下面的"范围"框裁掉。这里只增高、不动宽度。
            self._grow_to_fit()
            self._grow_timer.start()

    def _grow_to_fit(self) -> None:
        content = getattr(self, "_content", None)
        if content is not None and content.layout() is not None:
            content.layout().activate()
        if self.layout() is not None:
            self.layout().activate()
        hint = self.sizeHint()
        if hint.height() > self.height():
            self.resize(self.width(), hint.height())

    # ------------------------------------------------------- 画布实时同步

    def _on_canvas_extents_changed(self, *_args) -> None:
        if self._exporting:
            return
        try:
            self._refresh_view_extent()
            if self._extent_mode() != _EXTENT_VIEW:
                return
            extent = self._current_extent()
            if self._is_valid_extent(extent):
                self._aspect = self._aspect_for(extent)
            self._sync_size_from_scale()
            self._update_extent_display()
            self._update_hint()
        except Exception:  # noqa: BLE001 —— 槽内异常会原生崩溃
            logger.exception("同步画布范围失败")

    def _on_canvas_crs_changed(self, *_args) -> None:
        if self._exporting:
            return
        try:
            # 坐标系变了：图层 / 全图范围需要重新变换，先清缓存
            self._refresh_view_extent()
            self._full_extent = None
            self._layer_extents.clear()
            self._on_extent_changed()
        except Exception:  # noqa: BLE001
            logger.exception("同步画布坐标系失败")

    def _on_project_layers_changed(self, *_args) -> None:
        if self._exporting:
            return
        try:
            self._reload_layers_and_extents()
        except Exception:  # noqa: BLE001
            logger.exception("同步工程图层失败")

    def _reload_layers_and_extents(self) -> None:
        current_mode = self._extent_mode()
        current_layer_id = str(self.layer_combo.currentData() or "")
        # 图层增删：重建列表并让全部 / 单图层范围缓存失效
        self._refresh_layer_list()

        self.layer_combo.blockSignals(True)
        try:
            self.layer_combo.clear()
            for layer in self._layers:
                self.layer_combo.addItem(layer.name(), layer.id())
            index = self.layer_combo.findData(current_layer_id)
            if index < 0 and self.layer_combo.count() > 0:
                index = 0
            if index >= 0:
                self.layer_combo.setCurrentIndex(index)
        finally:
            self.layer_combo.blockSignals(False)

        self.extent_combo.blockSignals(True)
        try:
            options = self._extent_options()
            keys = [key for _label, key in options]
            target = current_mode if current_mode in keys else keys[0]
            self.extent_combo.clear()
            for label, key in options:
                self.extent_combo.addItem(label, key)
            index = self.extent_combo.findData(target)
            if index >= 0:
                self.extent_combo.setCurrentIndex(index)
        finally:
            self.extent_combo.blockSignals(False)

        self._show_layer_row(self._extent_mode() == _EXTENT_LAYER)
        self._on_extent_changed()

    # ----------------------------------------------------------- 四至 / 提示

    @staticmethod
    def _unit_suffix(crs: QgsCoordinateReferenceSystem) -> str:
        if crs is not None and crs.isValid():
            if crs.isGeographic():
                return "°"
            with contextlib.suppress(Exception):
                return QgsUnitTypes.toAbbreviatedString(crs.mapUnits())
        return ""

    def _update_extent_display(self) -> None:
        extent = self._current_extent()
        crs = self._destination_crs()
        crs_name = (
            crs.authid() if crs is not None and crs.isValid() else ""
        ) or "工程坐标系"
        if not self._is_valid_extent(extent):
            self.range_box.setTitle("范围")
            for label in (
                self.north_label,
                self.south_label,
                self.west_label,
                self.east_label,
            ):
                label.setText("—")
            return
        self.range_box.setTitle(f"范围（{crs_name}）")
        unit = self._unit_suffix(crs)
        suffix = f" {unit}" if unit else ""
        # 与 ArcGIS 一致保留 6 位小数；四种方位按罗盘布局摆放
        self.north_label.setText(f"上：{extent.yMaximum():.6f}{suffix}")
        self.south_label.setText(f"下：{extent.yMinimum():.6f}{suffix}")
        self.west_label.setText(f"左：{extent.xMinimum():.6f}{suffix}")
        self.east_label.setText(f"右：{extent.xMaximum():.6f}{suffix}")

    def _update_hint(self) -> None:
        extent = self._current_extent()
        if not self._is_valid_extent(extent):
            self.hint.setText("输出范围无效，请选择有效的输出范围。")
            self.export_btn.setEnabled(False)
            return

        width, height = self._current_output_size()
        fmt = self._current_format()
        dpi = max(1.0, self._output_dpi())
        scale = self._current_scale()

        if width <= 0 or height <= 0:
            self.hint.setText("无法计算输出尺寸，请检查比例尺 / 分辨率。")
            self.export_btn.setEnabled(False)
            return

        megapixels = width * height / 1_000_000
        cm_width = width / dpi * 2.54
        cm_height = height / dpi * 2.54
        text = (
            f"比例尺 1:{scale:,.0f}；输出 {width:,} × {height:,} 像素"
            f"（{megapixels:.1f} 百万像素，约 {cm_width:.1f} × {cm_height:.1f} cm）。"
        )

        valid = True
        if width > MAX_IMAGE_SIDE or height > MAX_IMAGE_SIDE:
            text += f" 单边超过 {MAX_IMAGE_SIDE} 像素上限，请调小尺寸或比例尺。"
            valid = False
        elif megapixels > MAX_IMAGE_PIXELS / 1_000_000:
            text += (
                f" 超过约 {MAX_IMAGE_PIXELS / 1_000_000:.0f} 百万像素上限，"
                "请调小尺寸或比例尺。"
            )
            valid = False

        if fmt.kind == "pdf":
            page_width, page_height = pdf_page_size_mm(width, height, dpi)
            if max(page_width, page_height) > PDF_MAX_PAGE_MM:
                text += (
                    f" PDF 页面 {page_width:.0f} × {page_height:.0f} mm 超出"
                    f"单边 {PDF_MAX_PAGE_MM:.0f} mm 上限，请提高分辨率或调小尺寸。"
                )
                valid = False

        self.hint.setText(text)
        self.export_btn.setEnabled(valid)

    def _update_load_to_map_state(self, fmt: MapImageFormat) -> None:
        """「导出后加载到地图」只对位图有效；PDF / SVG 不能当栅格图层加载。"""
        raster = fmt.kind == "raster"
        self.load_to_map_check.setEnabled(raster)
        self.load_to_map_check.setToolTip(
            "导出完成后按输出范围写世界文件，并把图片作为栅格图层加入工程；"
            "输出范围选「当前视图」时会正好覆盖当前视图范围。"
            if raster
            else "PDF / SVG 不是栅格图片，不能加载到地图。"
        )

    def _on_format_changed(self, *_args) -> None:
        fmt = self._current_format()
        path = self.path_edit.text().strip()
        if path:
            new_path = ensure_extension(path, fmt)
            if new_path != path:
                self.path_edit.setText(new_path)
        self._refresh_backgrounds(fmt)
        self._update_load_to_map_state(fmt)
        self._update_hint()

    def _refresh_backgrounds(self, fmt: MapImageFormat) -> None:
        """按格式重建背景下拉：透明项只在支持 alpha 的格式下出现。"""
        previous = self.background_combo.currentData()
        if previous is None:
            previous = "white"
        self.background_combo.blockSignals(True)
        try:
            self.background_combo.clear()
            for label, key, _color in _BACKGROUNDS:
                if key == "transparent" and not fmt.supports_transparency:
                    continue
                self.background_combo.addItem(label, key)
            index = self.background_combo.findData(previous)
            if index < 0:
                index = self.background_combo.findData("white")
            self.background_combo.setCurrentIndex(max(0, index))
        finally:
            self.background_combo.blockSignals(False)

    def _on_path_edited(self, _text: str) -> None:
        # 用户手动改过路径后，覆盖确认要重新弹（浏览对话框自己已经确认过一次）
        self._confirmed_overwrite_path = ""

    # --------------------------------------------------------------- 浏览

    def _on_browse(self) -> None:
        fmt = self._current_format()
        filters = [fmt.filter]
        filters.extend(
            other.filter for other in MAP_IMAGE_FORMATS if other is not fmt
        )
        filters.append("所有文件 (*)")
        chosen = save_file(
            self,
            "导出图片",
            self.path_edit.text().strip() or self._default_path(fmt),
            ";;".join(filters),
        )
        if not chosen:
            return
        detected = format_from_path(chosen)
        if detected is not None:
            # 用户在保存对话框里换了扩展名 → 格式跟着换
            index = self.format_combo.findData(detected)
            if index >= 0:
                self.format_combo.setCurrentIndex(index)
            chosen = ensure_extension(chosen, detected)
        else:
            chosen = ensure_extension(chosen, self._current_format())
        self.path_edit.setText(chosen)
        # 保存对话框（原生）已经确认过覆盖；导出时不再二次询问
        self._confirmed_overwrite_path = chosen

    # --------------------------------------------------------------- 导出

    def _on_export(self) -> None:
        try:
            self._do_export()
        except Exception:  # noqa: BLE001 —— 槽内异常会原生崩溃，必须兜住
            logger.exception("导出图片失败")
            MessageBox.critical(
                self, "导出图片", "导出过程中发生未知错误，详见日志。"
            )

    def _do_export(self) -> None:
        # 无模态：导出前先刷新一次「当前视图」，保证用的是画布最新范围
        self._refresh_view_extent()
        # 全图 / 单图层范围可能因编辑而变：导出前让缓存失效重算
        mode = self._extent_mode()
        if mode == _EXTENT_FULL:
            self._full_extent = None
        elif mode == _EXTENT_LAYER:
            self._layer_extents.pop(str(self.layer_combo.currentData() or ""), None)
        # 比例尺输入框可能还没失焦（用户直接点导出）：先应用 / 还原
        self._on_scale_editing_finished()

        path = self.path_edit.text().strip()
        if not path:
            focus_and_highlight(self.path_edit, "请选择输出文件路径")
            return
        fmt = self._current_format()
        path = ensure_extension(path, fmt)
        if path != self.path_edit.text().strip():
            self.path_edit.setText(path)

        extent = self._current_extent()
        if not self._is_valid_extent(extent):
            MessageBox.warning(
                self, "导出图片", "输出范围无效，请选择有效的输出范围。"
            )
            return

        if os.path.exists(path) and path != self._confirmed_overwrite_path:
            answer = MessageBox.question(
                self,
                "覆盖文件",
                f"文件已存在：\n{path}\n\n是否覆盖？",
                default_button=QMessageBox.StandardButton.No,
            )
            if answer != QMessageBox.StandardButton.Yes:
                return

        if self._canvas is None or sip.isdeleted(self._canvas):
            MessageBox.warning(self, "导出图片", "地图画布已关闭，无法导出。")
            return

        width, height = self._current_output_size()
        spec = ExportMapSpec(
            path=path,
            width=width,
            height=height,
            extent=extent,
            background=self._current_background(),
            dpi=self._output_dpi(),
        )
        try:
            spec.validate()
        except ExportMapImageError as exc:
            MessageBox.warning(self, "导出图片", str(exc))
            return

        self._exporting = True
        self.export_btn.setEnabled(False)
        self.status_label.setText("正在导出…")
        QApplication.setOverrideCursor(Qt.CursorShape.WaitCursor)
        # 让「正在导出…」先落到界面上再开始同步渲染；只处理重绘，不处理用户
        # 输入 —— 渲染前这一小段里窗口被关掉会让后续写盘 / 更新控件落空。
        QApplication.processEvents(
            QEventLoop.ProcessEventsFlag.ExcludeUserInputEvents
        )
        load_to_map = self.load_to_map_check.isChecked() and fmt.kind == "raster"
        error = ""
        unexpected = False
        try:
            canvas_settings = QgsMapSettings(self._canvas.mapSettings())
            export_map_image(
                canvas_settings,
                spec,
                self.project,
                georeference=load_to_map,
            )
        except ExportMapImageError as exc:
            error = str(exc)
        except Exception:  # noqa: BLE001 —— 渲染 / 写盘的意外错误
            logger.exception("导出图片失败")
            unexpected = True
        finally:
            self._exporting = False
            # 先恢复光标再弹提示：否则提示框会顶着「忙碌」光标出现
            QApplication.restoreOverrideCursor()
            self.export_btn.setEnabled(True)

        if unexpected:
            self.status_label.setText("")
            MessageBox.critical(self, "导出图片", "导出失败，未知错误详见日志。")
            return
        if error:
            self.status_label.setText("")
            MessageBox.warning(self, "导出图片", error)
            return

        loaded = self._load_exported_layer(path) if load_to_map else False
        self.status_label.setText(
            f"已导出：{path}" + ("（已加载到地图）" if loaded else "")
        )
        # 这次文件由自己写出，下次再导出同一路径要重新确认覆盖
        self._confirmed_overwrite_path = ""
        send(
            "success",
            f"地图已导出{'并加载到地图' if loaded else ''}：{path}",
            duration=6,
        )

    def _load_exported_layer(self, path: str) -> bool:
        """把刚导出的位图作为栅格图层加入工程（依赖刚写出的 world file 定位）。"""
        for existing in self.project.mapLayers().values():
            try:
                if (
                    existing.type() == QgsMapLayer.RasterLayer
                    and existing.source() == path
                ):
                    # 覆盖导出到同一路径：复用已有图层并重新读盘，别叠加同名副本
                    existing.reload()
                    return True
            except RuntimeError:  # C++ 对象已销毁
                continue
        name = os.path.splitext(os.path.basename(path))[0]
        layer = QgsRasterLayer(path, name)
        if not layer.isValid():
            logger.warning("导出图片无法作为栅格图层加载：%s", path)
            MessageBox.warning(
                self, "导出图片", f"导出成功，但无法加载到地图：\n{path}"
            )
            return False
        if not layer.crs().isValid():
            layer.setCrs(self._destination_crs())
        self.project.addMapLayer(layer)
        return True


def _safe_connect(signal, handler, name: str) -> None:
    """连接画布 / 工程信号；不同 QGIS 版本信号名可能不存在，失败只记日志。"""
    if signal is None:
        return
    try:
        signal.connect(handler)
    except (AttributeError, TypeError, RuntimeError):
        logger.warning("连接信号失败：%s", name, exc_info=True)
