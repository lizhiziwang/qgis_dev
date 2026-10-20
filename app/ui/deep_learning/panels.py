"""标注窗口左右两侧的 Dock：影像列表 / 保存设置、模型 / 标签 / 候选掩膜 / 分割设置。"""

from __future__ import annotations

from dataclasses import dataclass

from qgis.PyQt.QtCore import QSize, Qt, pyqtSignal
from qgis.PyQt.QtGui import QBrush, QColor, QIcon, QImage, QPixmap
from qgis.PyQt.QtWidgets import (
    QAbstractItemView,
    QCheckBox,
    QComboBox,
    QDockWidget,
    QDoubleSpinBox,
    QFileDialog,
    QFormLayout,
    QFrame,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListView,
    QListWidget,
    QListWidgetItem,
    QPushButton,
    QScrollArea,
    QSpinBox,
    QTabWidget,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

from app.core.ai.prompt_translate import translate_prompt
from app.infra.deep_learning import AutoSegConfig
from app.infra.logging import get_logger
from app.ui.common import tool_icons
from app.ui.common.theme import ThemeManager
from app.ui.deep_learning.annotation_model import LabelClass
from app.ui.deep_learning.grid_slicing import GridSpec
from app.ui.deep_learning.raster_io import RasterInfo

logger = get_logger(__name__)

# 宫格块状态背景色：已完成 / 进行中（当前块） / 未标注
_TILE_STATUS_BACKGROUNDS = {
    "done": "#b7e1b5",
    "active": "#ffe082",
    "todo": "#e0e0e0",
}
_TILE_STATUS_FOREGROUNDS = {
    "done": "#1b5e20",
    "active": "#6d4c00",
    "todo": "#5f6368",
}


@dataclass(frozen=True)
class ImageEntry:
    """左侧数据列表的一项。"""

    path: str
    annotated: bool = False


def _scrollable(widget: QWidget) -> QScrollArea:
    area = QScrollArea()
    area.setWidgetResizable(True)
    area.setFrameShape(QFrame.Shape.NoFrame)
    area.setWidget(widget)
    return area


def _color_icon(color: str) -> QIcon:
    pixmap = QPixmap(14, 14)
    pixmap.fill(QColor(color))
    return QIcon(pixmap)


class ImageListDock(QDockWidget):
    """左侧：影像数据列表、波段设置、保存设置、地理信息。"""

    open_requested = pyqtSignal()
    folder_requested = pyqtSignal()
    remove_requested = pyqtSignal()
    image_activated = pyqtSignal(str)
    tile_activated = pyqtSignal(int, int)
    bands_changed = pyqtSignal(tuple)
    output_dir_changed = pyqtSignal(str)

    def __init__(self, parent=None) -> None:
        super().__init__("数据列表", parent)
        self.setObjectName("AnnotationImageDock")
        self.setAllowedAreas(Qt.DockWidgetArea.LeftDockWidgetArea | Qt.DockWidgetArea.RightDockWidgetArea)
        # 放进标注页的 QSplitter 后不能再允许拖动标题栏：QDockWidget 会
        # setFloating(True) 变成顶层窗口，而 splitter 仍占着槽位。
        self.setFeatures(QDockWidget.DockWidgetFeature.NoDockWidgetFeatures)

        content = QWidget()
        layout = QVBoxLayout(content)
        layout.setContentsMargins(8, 8, 8, 8)
        layout.setSpacing(8)

        # ---- 影像（列表 / 宫格两个 tab）
        list_group = QGroupBox("影像")
        list_layout = QVBoxLayout(list_group)
        self.tabs = QTabWidget()
        self.tabs.setObjectName("AnnotationViewTabs")

        list_page = QWidget()
        list_page_layout = QVBoxLayout(list_page)
        list_page_layout.setContentsMargins(0, 0, 0, 0)
        self.image_list = QListWidget()
        self.image_list.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.image_list.setAlternatingRowColors(True)
        self.image_list.setMinimumHeight(180)
        self.image_list.currentItemChanged.connect(self._on_current_item_changed)
        self.image_list.itemDoubleClicked.connect(self._on_item_double_clicked)
        list_page_layout.addWidget(self.image_list)
        self.tabs.addTab(list_page, "列表")

        grid_page = QWidget()
        grid_layout = QVBoxLayout(grid_page)
        grid_layout.setContentsMargins(0, 0, 0, 0)
        self.tile_info = QLabel("当前影像未启用宫格")
        self.tile_info.setWordWrap(True)
        self.tile_progress = QLabel("")
        self.tile_progress.setObjectName("AnnotationTileProgress")
        self.tile_progress.setAlignment(
            Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter
        )
        self.tile_progress.hide()
        grid_header = QHBoxLayout()
        grid_header.setContentsMargins(0, 0, 0, 0)
        grid_header.addWidget(self.tile_info, 1)
        grid_header.addWidget(self.tile_progress, 0)
        grid_layout.addLayout(grid_header)
        self.tile_legend = QLabel(
            '<span style="color:#2e7d32">■ 已完成</span>&nbsp;&nbsp;'
            '<span style="color:#a87900">■ 进行中</span>&nbsp;&nbsp;'
            '<span style="color:#757575">■ 未标注</span>'
        )
        self.tile_legend.setObjectName("AnnotationTileLegend")
        self.tile_legend.setTextFormat(Qt.TextFormat.RichText)
        self.tile_legend.hide()
        grid_layout.addWidget(self.tile_legend)
        self.tile_hint = QLabel("打开影像或文件夹时勾选「切分为宫格」，即可按块标注。")
        self.tile_hint.setWordWrap(True)
        self.tile_hint.setEnabled(False)
        grid_layout.addWidget(self.tile_hint)
        self.tile_list = QListWidget()
        self.tile_list.setObjectName("AnnotationTileGrid")
        self.tile_list.setViewMode(QListView.ViewMode.IconMode)
        self.tile_list.setResizeMode(QListView.ResizeMode.Adjust)
        self.tile_list.setMovement(QListView.Movement.Static)
        self.tile_list.setGridSize(QSize(104, 108))
        self.tile_list.setIconSize(QSize(88, 72))
        self.tile_list.setWordWrap(True)
        self.tile_list.setUniformItemSizes(True)
        self.tile_list.setMinimumHeight(180)
        self.tile_list.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.tile_list.itemClicked.connect(self._on_tile_clicked)
        self.tile_list.currentItemChanged.connect(self._on_tile_current_changed)
        grid_layout.addWidget(self.tile_list)
        self.tabs.addTab(grid_page, "宫格")

        list_layout.addWidget(self.tabs)
        buttons = QHBoxLayout()
        self.btn_open = QToolButton()
        self.btn_open.setText("打开影像")
        self.btn_open.clicked.connect(self.open_requested)
        self.btn_folder = QToolButton()
        self.btn_folder.setText("打开文件夹")
        self.btn_folder.clicked.connect(self.folder_requested)
        self.btn_remove = QToolButton()
        self.btn_remove.setText("移除")
        self.btn_remove.clicked.connect(self.remove_requested)
        self.btn_locate_tile = QToolButton()
        self.btn_locate_tile.setObjectName("AnnotationLocateTile")
        self.btn_locate_tile.setText("定位当前宫格")
        self.btn_locate_tile.setToolTip("切换到「宫格」页并定位到正在标注的块")
        self.btn_locate_tile.setEnabled(False)
        self.btn_locate_tile.clicked.connect(self.locate_current_tile)
        for button in (self.btn_open, self.btn_folder, self.btn_remove):
            buttons.addWidget(button)
        buttons.addStretch(1)
        buttons.addWidget(self.btn_locate_tile)
        list_layout.addLayout(buttons)
        layout.addWidget(list_group)

        # ---- 波段设置
        band_group = QGroupBox("波段设置")
        band_form = QFormLayout(band_group)
        self.band_combos: list[QComboBox] = []
        for name in ("R", "G", "B"):
            combo = QComboBox()
            combo.setEnabled(False)
            combo.currentIndexChanged.connect(self._emit_bands)
            band_form.addRow(f"{name} 波段", combo)
            self.band_combos.append(combo)
        layout.addWidget(band_group)

        # ---- 保存设置
        save_group = QGroupBox("保存设置")
        save_layout = QVBoxLayout(save_group)
        output_row = QHBoxLayout()
        output_row.addWidget(QLabel("输出目录"))
        self.output_edit = QLineEdit()
        self.output_edit.setPlaceholderText("默认与影像同目录")
        self.output_edit.editingFinished.connect(
            lambda: self.output_dir_changed.emit(self.output_edit.text().strip())
        )
        self.btn_output = QToolButton()
        self.btn_output.setText("…")
        self.btn_output.clicked.connect(self._choose_output_dir)
        output_row.addWidget(self.output_edit, 1)
        output_row.addWidget(self.btn_output)
        save_layout.addLayout(output_row)
        self.geotiff_check = QCheckBox("同时保存 GeoTIFF 掩膜（带地理参考）")
        save_layout.addWidget(self.geotiff_check)
        self.shp_check = QCheckBox("额外保存 ESRI Shapefile（暂未支持）")
        self.shp_check.setEnabled(False)
        self.shp_check.setToolTip("矢量成果尚未实现，掩膜请使用 PNG/GeoTIFF")
        save_layout.addWidget(self.shp_check)
        layout.addWidget(save_group)

        # ---- 地理信息
        geo_group = QGroupBox("地理信息")
        geo_form = QFormLayout(geo_group)
        self.geo_labels: dict[str, QLabel] = {}
        for key, title in (
            ("size", "尺寸"),
            ("bands", "波段"),
            ("dtype", "数据类型"),
            ("crs", "坐标系"),
            ("pixel", "像元大小"),
            ("nodata", "NoData"),
        ):
            value = QLabel("—")
            value.setWordWrap(True)
            value.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
            geo_form.addRow(title, value)
            self.geo_labels[key] = value
        layout.addWidget(geo_group)
        layout.addStretch(1)

        self.setWidget(_scrollable(content))
        self.apply_theme()

    # ------------------------------------------------------------------ 列表
    def set_images(self, entries: list[ImageEntry], current: str = "") -> None:
        self.image_list.blockSignals(True)
        self.image_list.clear()
        for entry in entries:
            item = QListWidgetItem(self._entry_text(entry))
            item.setData(Qt.ItemDataRole.UserRole, entry.path)
            item.setData(Qt.ItemDataRole.UserRole + 1, entry.annotated)
            item.setToolTip(entry.path)
            if entry.annotated:
                item.setForeground(QBrush(QColor("#3cb44b")))
            self.image_list.addItem(item)
        self.image_list.blockSignals(False)
        if current:
            self.set_current(current)

    def _entry_text(self, entry: ImageEntry) -> str:
        name = entry.path.replace("\\", "/").rsplit("/", 1)[-1]
        return f"{name}  ✓" if entry.annotated else name

    def current_path(self) -> str:
        item = self.image_list.currentItem()
        return str(item.data(Qt.ItemDataRole.UserRole) or "") if item is not None else ""

    def set_current(self, path: str) -> None:
        for row in range(self.image_list.count()):
            item = self.image_list.item(row)
            if item.data(Qt.ItemDataRole.UserRole) == path:
                self.image_list.blockSignals(True)
                self.image_list.setCurrentItem(item)
                self.image_list.blockSignals(False)
                return

    def set_annotated(self, path: str, annotated: bool) -> None:
        for row in range(self.image_list.count()):
            item = self.image_list.item(row)
            if item.data(Qt.ItemDataRole.UserRole) == path:
                item.setData(Qt.ItemDataRole.UserRole + 1, annotated)
                item.setText(self._entry_text(ImageEntry(path, annotated)))
                item.setData(
                    Qt.ItemDataRole.ForegroundRole,
                    QBrush(QColor("#3cb44b")) if annotated else None,
                )
                return

    def _on_current_item_changed(self, current: QListWidgetItem | None, _previous) -> None:
        if current is not None:
            self.image_activated.emit(str(current.data(Qt.ItemDataRole.UserRole) or ""))

    def _on_item_double_clicked(self, item: QListWidgetItem) -> None:
        self.image_activated.emit(str(item.data(Qt.ItemDataRole.UserRole) or ""))

    # ------------------------------------------------------------------ 宫格
    def set_tiles(
        self,
        spec: GridSpec | None,
        preview: QImage | None,
        current: tuple[int, int] | None = None,
        annotated: set[tuple[int, int]] | None = None,
        *,
        select_tab: bool = True,
    ) -> None:
        """刷新当前影像的宫格视图；``preview`` 用于生成块缩略图。"""
        annotated = annotated or set()
        self.tile_list.blockSignals(True)
        self.tile_list.clear()
        if spec is None:
            self.tile_info.setText("当前影像未启用宫格")
            self.tile_progress.clear()
            self.tile_progress.hide()
            self.tile_legend.hide()
            self.tile_hint.show()
            self.tile_list.hide()
            self.tile_list.blockSignals(False)
            self._update_locate_button()
            if select_tab:
                self.tabs.setCurrentIndex(0)
            return

        self.tile_hint.hide()
        self.tile_legend.show()
        self.tile_list.show()
        self.tile_progress.show()
        self.tile_info.setText(
            f"{spec.rows} 行 × {spec.cols} 列 · 块 {spec.tile_size} px · "
            f"重叠 {spec.overlap_px} px（{spec.overlap_percent:.0f}%）"
        )
        scale = preview.width() / spec.image_width if preview is not None and spec.image_width else 1.0
        icon_size = self.tile_list.iconSize()
        for tile in spec.tiles():
            item = QListWidgetItem()
            item.setData(Qt.ItemDataRole.UserRole, tile.row)
            item.setData(Qt.ItemDataRole.UserRole + 1, tile.col)
            self._update_tile_item(item, tile, (tile.row, tile.col) in annotated)
            item.setToolTip(
                f"第 {tile.row + 1} 行，第 {tile.col + 1} 列\n"
                f"源图区域 x={tile.x} y={tile.y}  {tile.width}×{tile.height} 像素"
            )
            if preview is not None and not preview.isNull():
                crop = preview.copy(
                    round(tile.x * scale),
                    round(tile.y * scale),
                    max(1, round(tile.width * scale)),
                    max(1, round(tile.height * scale)),
                )
                if not crop.isNull():
                    pixmap = QPixmap.fromImage(crop).scaled(
                        icon_size,
                        Qt.AspectRatioMode.KeepAspectRatio,
                        Qt.TransformationMode.SmoothTransformation,
                    )
                    item.setIcon(QIcon(pixmap))
            self.tile_list.addItem(item)
        self._refresh_tile_progress()
        self.tile_list.blockSignals(False)
        if current is not None:
            self.set_current_tile(current)
        self._update_locate_button()
        if select_tab:
            self.tabs.setCurrentIndex(1)

    def clear_tiles(self) -> None:
        self.set_tiles(None, None)

    def current_tile(self) -> tuple[int, int] | None:
        item = self.tile_list.currentItem()
        if item is None:
            return None
        return (
            int(item.data(Qt.ItemDataRole.UserRole) or 0),
            int(item.data(Qt.ItemDataRole.UserRole + 1) or 0),
        )

    def set_current_tile(self, tile: tuple[int, int] | None) -> None:
        previous = self.current_tile()
        self.tile_list.blockSignals(True)
        try:
            if tile is None:
                self.tile_list.setCurrentItem(None)
                return
            row, col = tile
            for index in range(self.tile_list.count()):
                item = self.tile_list.item(index)
                if (
                    int(item.data(Qt.ItemDataRole.UserRole) or 0) == row
                    and int(item.data(Qt.ItemDataRole.UserRole + 1) or 0) == col
                ):
                    self.tile_list.setCurrentItem(item)
                    self.tile_list.scrollToItem(item)
                    return
        finally:
            self.tile_list.blockSignals(False)
            if previous != tile:
                self._refresh_tile_status(previous)
                self._refresh_tile_status(tile)
            self._update_locate_button()

    def _on_tile_current_changed(self, current, _previous) -> None:
        self._update_locate_button()

    def _update_locate_button(self) -> None:
        self.btn_locate_tile.setEnabled(self.tile_list.currentItem() is not None)

    def locate_current_tile(self) -> bool:
        """切到「宫格」页并滚动到当前正在标注的块。"""
        item = self.tile_list.currentItem()
        if item is None:
            self._update_locate_button()
            return False
        self.tabs.setCurrentIndex(1)
        self.tile_list.scrollToItem(
            item, QAbstractItemView.ScrollHint.PositionAtCenter
        )
        self.tile_list.setFocus()
        return True

    def set_tile_annotated(self, row: int, col: int, annotated: bool) -> None:
        for index in range(self.tile_list.count()):
            item = self.tile_list.item(index)
            if (
                int(item.data(Qt.ItemDataRole.UserRole) or 0) == row
                and int(item.data(Qt.ItemDataRole.UserRole + 1) or 0) == col
            ):
                self._update_tile_item(item, None, annotated)
                self._refresh_tile_progress()
                return

    def _tile_status(self, item: QListWidgetItem) -> str:
        """已完成优先；当前打开但未完成的块为进行中，其余为未标注。"""
        if bool(item.data(Qt.ItemDataRole.UserRole + 2)):
            return "done"
        row = int(item.data(Qt.ItemDataRole.UserRole) or 0)
        col = int(item.data(Qt.ItemDataRole.UserRole + 1) or 0)
        return "active" if self.current_tile() == (row, col) else "todo"

    def _apply_tile_status(self, item: QListWidgetItem) -> None:
        status = self._tile_status(item)
        item.setBackground(QBrush(QColor(_TILE_STATUS_BACKGROUNDS[status])))
        item.setForeground(QBrush(QColor(_TILE_STATUS_FOREGROUNDS[status])))

    def _refresh_tile_status(self, tile: tuple[int, int] | None) -> None:
        if tile is None:
            return
        row, col = tile
        for index in range(self.tile_list.count()):
            item = self.tile_list.item(index)
            if (
                int(item.data(Qt.ItemDataRole.UserRole) or 0) == row
                and int(item.data(Qt.ItemDataRole.UserRole + 1) or 0) == col
            ):
                self._apply_tile_status(item)
                return

    def _update_tile_item(self, item: QListWidgetItem, tile, annotated: bool) -> None:
        row = tile.row if tile is not None else int(item.data(Qt.ItemDataRole.UserRole) or 0)
        col = tile.col if tile is not None else int(item.data(Qt.ItemDataRole.UserRole + 1) or 0)
        item.setData(Qt.ItemDataRole.UserRole + 2, bool(annotated))
        item.setText(f"{row + 1}-{col + 1}{' ✓' if annotated else ''}")
        self._apply_tile_status(item)

    def _refresh_tile_progress(self) -> None:
        total = self.tile_list.count()
        done = sum(
            1
            for index in range(total)
            if bool(self.tile_list.item(index).data(Qt.ItemDataRole.UserRole + 2))
        )
        percent = round(done * 100 / total) if total else 0
        self.tile_progress.setText(f"已完成 {done}/{total} · {percent}%")
        self.tile_progress.setToolTip(f"已保存 {done} 块，共 {total} 块")

    def _on_tile_clicked(self, item: QListWidgetItem) -> None:
        self.tile_activated.emit(
            int(item.data(Qt.ItemDataRole.UserRole) or 0),
            int(item.data(Qt.ItemDataRole.UserRole + 1) or 0),
        )

    # ------------------------------------------------------------------ 波段
    def set_bands(self, info: RasterInfo) -> None:
        names = info.band_names or tuple(f"band_{i}" for i in range(1, info.band_count + 1))
        for combo in self.band_combos:
            combo.blockSignals(True)
            combo.clear()
            for index, name in enumerate(names, start=1):
                combo.addItem(f"{index} · {name}", index)
            combo.setCurrentIndex(0)
            combo.setEnabled(info.band_count >= 3)
            combo.blockSignals(False)

    def set_band_selection(self, bands: tuple[int, int, int]) -> None:
        for combo, band in zip(self.band_combos, bands, strict=True):
            combo.blockSignals(True)
            combo.setCurrentIndex(max(0, combo.findData(band)))
            combo.blockSignals(False)

    def bands(self) -> tuple[int, int, int]:
        values = [int(combo.currentData() or 1) for combo in self.band_combos]
        return tuple(values[:3]) if values else (1, 1, 1)

    def _emit_bands(self, _index: int = -1) -> None:
        self.bands_changed.emit(self.bands())

    # ------------------------------------------------------------------ 保存设置
    def _choose_output_dir(self) -> None:
        directory = QFileDialog.getExistingDirectory(self, "选择输出目录", self.output_edit.text())
        if directory:
            self.output_edit.setText(directory)
            self.output_dir_changed.emit(directory)

    def output_dir(self) -> str:
        return self.output_edit.text().strip()

    def set_output_dir(self, path: str) -> None:
        self.output_edit.setText(path)

    def geotiff_enabled(self) -> bool:
        return self.geotiff_check.isChecked()

    # ------------------------------------------------------------------ 地理信息
    def set_geo_info(self, info: RasterInfo | None) -> None:
        if info is None:
            for label in self.geo_labels.values():
                label.setText("—")
            return
        self.geo_labels["size"].setText(f"{info.width} × {info.height} 像素")
        self.geo_labels["bands"].setText(f"{info.band_count}（{info.dtype_name}）")
        self.geo_labels["dtype"].setText(info.dtype_name)
        crs = info.projection_name or "无"
        if info.projection_wkt and crs not in ("无", "未知投影"):
            crs = f"{crs} ({info.projection_wkt[:30]}…)" if len(info.projection_wkt) > 30 else crs
        self.geo_labels["crs"].setText(crs)
        if info.pixel_size:
            unit = info.unit or "像素"
            self.geo_labels["pixel"].setText(f"{info.pixel_size[0]:g} × {info.pixel_size[1]:g} {unit}".strip())
        else:
            self.geo_labels["pixel"].setText("—")
        self.geo_labels["nodata"].setText("—" if info.nodata is None else f"{info.nodata:g}")

    # ------------------------------------------------------------------ 主题
    def apply_theme(self) -> None:
        theme = ThemeManager.instance().current()
        for button, key in (
            (self.btn_open, "folder_open"),
            (self.btn_folder, "folder_open"),
            (self.btn_remove, "trash"),
            (self.btn_locate_tile, "coord_locate"),
            (self.btn_output, "folder_open"),
        ):
            button.setIcon(tool_icons.icon(key, theme.menu_text))


class AnnotationPanelDock(QDockWidget):
    """右侧：模型、标签、候选掩膜、分割设置与保存。"""

    choose_checkpoint_requested = pyqtSignal()
    load_model_requested = pyqtSignal()
    sam_settings_requested = pyqtSignal()
    add_label_requested = pyqtSignal()
    rename_label_requested = pyqtSignal(int)
    color_label_requested = pyqtSignal(int)
    delete_label_requested = pyqtSignal(int)
    label_selected = pyqtSignal(int)
    candidate_preview_requested = pyqtSignal(int)
    apply_candidates_requested = pyqtSignal()
    clear_candidates_requested = pyqtSignal()
    text_prompt_requested = pyqtSignal(str, float)
    exemplar_requested = pyqtSignal(float)
    save_requested = pyqtSignal()
    brush_size_changed = pyqtSignal(int)
    opacity_changed = pyqtSignal(int)
    prompt_radius_changed = pyqtSignal(int)

    def __init__(self, parent=None) -> None:
        super().__init__("标注信息", parent)
        self.setObjectName("AnnotationPanelDock")
        self.setAllowedAreas(Qt.DockWidgetArea.LeftDockWidgetArea | Qt.DockWidgetArea.RightDockWidgetArea)
        # 放进标注页的 QSplitter 后不能再允许拖动标题栏：QDockWidget 会
        # setFloating(True) 变成顶层窗口，而 splitter 仍占着槽位。
        self.setFeatures(QDockWidget.DockWidgetFeature.NoDockWidgetFeatures)

        content = QWidget()
        layout = QVBoxLayout(content)
        layout.setContentsMargins(8, 8, 8, 8)
        layout.setSpacing(8)

        # ---- 模型选择
        model_group = QGroupBox("模型选择")
        model_layout = QVBoxLayout(model_group)
        checkpoint_row = QHBoxLayout()
        self.checkpoint_edit = QLineEdit()
        self.checkpoint_edit.setReadOnly(True)
        self.checkpoint_edit.setPlaceholderText("尚未选择 SAM 权重")
        self.btn_checkpoint = QToolButton()
        self.btn_checkpoint.setText("选择权重…")
        self.btn_checkpoint.clicked.connect(self.choose_checkpoint_requested)
        checkpoint_row.addWidget(self.checkpoint_edit, 1)
        checkpoint_row.addWidget(self.btn_checkpoint)
        model_layout.addLayout(checkpoint_row)
        model_form = QFormLayout()
        self.model_type_combo = QComboBox()
        for title, value in (("自动识别", "auto"), ("ViT-H", "vit_h"), ("ViT-L", "vit_l"), ("ViT-B", "vit_b")):
            self.model_type_combo.addItem(title, value)
        self.device_combo = QComboBox()
        for title, value in (("自动", "auto"), ("CUDA", "cuda"), ("CPU", "cpu")):
            self.device_combo.addItem(title, value)
        model_form.addRow("模型类型", self.model_type_combo)
        model_form.addRow("计算设备", self.device_combo)
        model_layout.addLayout(model_form)
        self.model_status = QLabel("未启动")
        self.model_status.setWordWrap(True)
        model_layout.addWidget(self.model_status)
        model_buttons = QHBoxLayout()
        self.btn_load = QPushButton("加载模型")
        self.btn_load.clicked.connect(self.load_model_requested)
        self.btn_restart = QPushButton("重新加载")
        self.btn_restart.clicked.connect(self.load_model_requested)
        self.btn_restart.setEnabled(False)
        model_buttons.addWidget(self.btn_load)
        model_buttons.addWidget(self.btn_restart)
        model_layout.addLayout(model_buttons)
        self.btn_sam_settings = QToolButton()
        self.btn_sam_settings.setText("SAM 设置…")
        self.btn_sam_settings.setToolTip("管理外部依赖、SAM1/SAM2/SAM3 模型与图像特征缓存")
        self.btn_sam_settings.clicked.connect(self.sam_settings_requested)
        model_layout.addWidget(self.btn_sam_settings)
        layout.addWidget(model_group)

        # ---- 标签列表
        label_group = QGroupBox("标签列表")
        label_layout = QVBoxLayout(label_group)
        self.label_list = QListWidget()
        self.label_list.setMaximumHeight(150)
        self.label_list.currentItemChanged.connect(self._on_label_changed)
        label_layout.addWidget(self.label_list)
        label_buttons = QHBoxLayout()
        self.btn_add_label = QToolButton()
        self.btn_add_label.setText("添加")
        self.btn_add_label.clicked.connect(self.add_label_requested)
        self.btn_rename_label = QToolButton()
        self.btn_rename_label.setText("改名")
        self.btn_rename_label.clicked.connect(self._emit_rename)
        self.btn_color_label = QToolButton()
        self.btn_color_label.setText("颜色")
        self.btn_color_label.clicked.connect(self._emit_color)
        self.btn_delete_label = QToolButton()
        self.btn_delete_label.setText("删除")
        self.btn_delete_label.clicked.connect(self._emit_delete)
        for button in (self.btn_add_label, self.btn_rename_label, self.btn_color_label, self.btn_delete_label):
            label_buttons.addWidget(button)
        label_layout.addLayout(label_buttons)
        layout.addWidget(label_group)

        # ---- 范例标注（SAM3）
        exemplar_group = QGroupBox("范例标注（SAM3）")
        exemplar_layout = QVBoxLayout(exemplar_group)
        exemplar_row = QHBoxLayout()
        self.exemplar_threshold_spin = QDoubleSpinBox()
        self.exemplar_threshold_spin.setRange(0.1, 0.95)
        self.exemplar_threshold_spin.setDecimals(2)
        # QDoubleSpinBox 默认步长是 1.0，必须显式设置
        self.exemplar_threshold_spin.setSingleStep(0.05)
        self.exemplar_threshold_spin.setValue(0.6)
        self.exemplar_threshold_spin.setToolTip("置信度阈值：越高越严格，默认 0.60")
        exemplar_row.addWidget(QLabel("置信度"))
        exemplar_row.addWidget(self.exemplar_threshold_spin, 1)
        self.btn_exemplar = QPushButton("批量提取同类")
        self.btn_exemplar.setToolTip("以当前框选的目标为范例，找出图中所有同类地物")
        self.btn_exemplar.clicked.connect(self._emit_exemplar)
        exemplar_row.addWidget(self.btn_exemplar)
        exemplar_layout.addLayout(exemplar_row)
        self.exemplar_hint = QLabel("先用框选工具框住 1 个样本，再点按钮")
        self.exemplar_hint.setWordWrap(True)
        self.exemplar_hint.setEnabled(False)
        exemplar_layout.addWidget(self.exemplar_hint)
        layout.addWidget(exemplar_group)

        # ---- 文字提示（SAM3）
        text_group = QGroupBox("文字提示（SAM3）")
        text_layout = QVBoxLayout(text_group)
        self.text_edit = QLineEdit()
        self.text_edit.setPlaceholderText("如 建筑物 / 道路 / 红色方块 / building")
        self.text_edit.setToolTip(
            "用自然语言描述要分割的地物，SAM3 会返回匹配的实例掩膜；"
            "中文会按内置地物术语表自动转换成英文"
        )
        self.text_edit.textChanged.connect(self._refresh_text_controls)
        text_layout.addWidget(self.text_edit)
        text_buttons = QHBoxLayout()
        self.text_threshold_spin = QDoubleSpinBox()
        self.text_threshold_spin.setRange(0.05, 0.95)
        self.text_threshold_spin.setDecimals(2)
        # QDoubleSpinBox 默认步长是 1.0，必须显式设置
        self.text_threshold_spin.setSingleStep(0.05)
        self.text_threshold_spin.setValue(0.5)
        self.text_threshold_spin.setToolTip("置信度阈值：越高候选越少、越严格")
        text_buttons.addWidget(QLabel("置信度"))
        text_buttons.addWidget(self.text_threshold_spin, 1)
        self.btn_text_prompt = QPushButton("文字分割")
        self.btn_text_prompt.clicked.connect(self._emit_text_prompt)
        self.text_edit.returnPressed.connect(self.btn_text_prompt.click)
        text_buttons.addWidget(self.btn_text_prompt)
        text_layout.addLayout(text_buttons)
        self.text_hint = QLabel("文字提示仅 SAM3 / SAM3.1 支持")
        self.text_hint.setWordWrap(True)
        self.text_hint.setEnabled(False)
        text_layout.addWidget(self.text_hint)
        layout.addWidget(text_group)

        # ---- 候选掩膜
        candidate_group = QGroupBox("候选掩膜")
        candidate_layout = QVBoxLayout(candidate_group)
        self.candidate_list = QListWidget()
        self.candidate_list.setMaximumHeight(160)
        self.candidate_list.currentItemChanged.connect(self._on_candidate_changed)
        candidate_layout.addWidget(self.candidate_list)
        candidate_buttons = QHBoxLayout()
        self.btn_apply_candidates = QPushButton("应用选中")
        self.btn_apply_candidates.clicked.connect(self.apply_candidates_requested)
        self.btn_clear_candidates = QToolButton()
        self.btn_clear_candidates.setText("清空")
        self.btn_clear_candidates.clicked.connect(self.clear_candidates_requested)
        candidate_buttons.addWidget(self.btn_apply_candidates, 1)
        candidate_buttons.addWidget(self.btn_clear_candidates)
        candidate_layout.addLayout(candidate_buttons)
        layout.addWidget(candidate_group)

        # ---- 分割设置
        seg_group = QGroupBox("分割设置")
        seg_form = QFormLayout(seg_group)
        self.brush_spin = QSpinBox()
        self.brush_spin.setRange(1, 500)
        self.brush_spin.setValue(20)
        self.brush_spin.setSuffix(" px")
        self.brush_spin.valueChanged.connect(self.brush_size_changed)
        self.opacity_spin = QSpinBox()
        self.opacity_spin.setRange(0, 100)
        self.opacity_spin.setValue(70)
        self.opacity_spin.setSuffix(" %")
        self.opacity_spin.valueChanged.connect(self.opacity_changed)
        self.prompt_spin = QSpinBox()
        self.prompt_spin.setRange(4, 30)
        self.prompt_spin.setValue(8)
        self.prompt_spin.setSuffix(" px")
        self.prompt_spin.valueChanged.connect(self.prompt_radius_changed)
        seg_form.addRow("笔刷大小", self.brush_spin)
        seg_form.addRow("标签透明度", self.opacity_spin)
        seg_form.addRow("提示点半径", self.prompt_spin)
        layout.addWidget(seg_group)

        auto_group = QGroupBox("自动分割参数")
        auto_form = QFormLayout(auto_group)
        self.points_spin = QSpinBox()
        self.points_spin.setRange(4, 64)
        self.points_spin.setValue(24)
        self.iou_spin = QDoubleSpinBox()
        self.iou_spin.setRange(0.5, 0.99)
        self.iou_spin.setSingleStep(0.01)
        self.iou_spin.setValue(0.86)
        self.stability_spin = QDoubleSpinBox()
        self.stability_spin.setRange(0.5, 0.99)
        self.stability_spin.setSingleStep(0.01)
        self.stability_spin.setValue(0.92)
        self.min_area_spin = QSpinBox()
        self.min_area_spin.setRange(0, 1_000_000)
        self.min_area_spin.setValue(0)
        self.min_area_spin.setSuffix(" px")
        self.max_candidates_spin = QSpinBox()
        self.max_candidates_spin.setRange(1, 1000)
        self.max_candidates_spin.setValue(200)
        auto_form.addRow("采样点密度", self.points_spin)
        auto_form.addRow("IoU 阈值", self.iou_spin)
        auto_form.addRow("稳定性阈值", self.stability_spin)
        auto_form.addRow("最小区域", self.min_area_spin)
        auto_form.addRow("最大候选数", self.max_candidates_spin)
        layout.addWidget(auto_group)

        self.btn_save = QPushButton("保存标注")
        self.btn_save.setMinimumHeight(32)
        self.btn_save.clicked.connect(self.save_requested)
        layout.addWidget(self.btn_save)
        layout.addStretch(1)

        self.setWidget(_scrollable(content))
        self._available = True
        self._ready = False
        self._busy = False
        self._family = ""
        self.apply_theme()
        self._refresh_text_controls()

    # ------------------------------------------------------------------ 模型
    def set_checkpoint(self, path: str) -> None:
        self.checkpoint_edit.setText(path)
        self.checkpoint_edit.setToolTip(path)

    def model_type(self) -> str:
        return str(self.model_type_combo.currentData() or "auto")

    def device(self) -> str:
        return str(self.device_combo.currentData() or "auto")

    def set_model_type(self, value: str) -> None:
        index = self.model_type_combo.findData(str(value or "auto"))
        if index >= 0:
            self.model_type_combo.blockSignals(True)
            self.model_type_combo.setCurrentIndex(index)
            self.model_type_combo.blockSignals(False)

    def set_device(self, value: str) -> None:
        index = self.device_combo.findData(str(value or "auto"))
        if index >= 0:
            self.device_combo.blockSignals(True)
            self.device_combo.setCurrentIndex(index)
            self.device_combo.blockSignals(False)

    def set_model_status(self, text: str, *, ready: bool = False) -> None:
        self._ready = bool(ready)
        self.model_status.setText(text)
        self.btn_load.setText("重新加载" if ready else "加载模型")
        self.btn_restart.setEnabled(ready and self._available and not self._busy)
        self._refresh_text_controls()

    def set_sam_available(self, available: bool, reason: str = "") -> None:
        self._available = bool(available)
        # 选择权重 / 模型类型 / 设备始终可用，缺失时也能先补齐配置
        self.btn_load.setEnabled(available and not self._busy)
        self.btn_restart.setEnabled(available and self._ready and not self._busy)
        if not available:
            self.model_status.setText(reason or "SAM 不可用")
        self._refresh_text_controls()

    def set_sam_family(self, family: str) -> None:
        """记录已加载的模型系列；文字提示只允许 SAM3 / SAM3.1。"""
        self._family = str(family or "").lower()
        self._refresh_text_controls()

    # ------------------------------------------------------------------ 标签
    def set_labels(self, labels: list[LabelClass], current_id: int = 0) -> None:
        self.label_list.blockSignals(True)
        self.label_list.clear()
        for label in labels:
            item = QListWidgetItem(_color_icon(label.color), label.name)
            item.setData(Qt.ItemDataRole.UserRole, label.id)
            self.label_list.addItem(item)
            if label.id == current_id:
                self.label_list.setCurrentItem(item)
        self.label_list.blockSignals(False)

    def current_label_id(self) -> int:
        item = self.label_list.currentItem()
        return int(item.data(Qt.ItemDataRole.UserRole) or 0) if item is not None else 0

    def _on_label_changed(self, current: QListWidgetItem | None, _previous) -> None:
        if current is not None:
            self.label_selected.emit(int(current.data(Qt.ItemDataRole.UserRole) or 0))

    def _emit_rename(self) -> None:
        label_id = self.current_label_id()
        if label_id:
            self.rename_label_requested.emit(label_id)

    def _emit_color(self) -> None:
        label_id = self.current_label_id()
        if label_id:
            self.color_label_requested.emit(label_id)

    def _emit_delete(self) -> None:
        label_id = self.current_label_id()
        if label_id:
            self.delete_label_requested.emit(label_id)

    # ------------------------------------------------------------------ 文字提示
    def _emit_exemplar(self) -> None:
        self.exemplar_requested.emit(float(self.exemplar_threshold_spin.value()))

    def _emit_text_prompt(self) -> None:
        text = self.text_edit.text().strip()
        if not text:
            return
        self.text_prompt_requested.emit(text, float(self.text_threshold_spin.value()))

    def _refresh_text_controls(self) -> None:
        base = self._available and self._ready and self._family == "sam3" and not self._busy
        translation = translate_prompt(self.text_edit.text())
        self.text_edit.setEnabled(base)
        self.text_threshold_spin.setEnabled(base)
        self.btn_text_prompt.setEnabled(base and translation.usable)
        self.exemplar_threshold_spin.setEnabled(base)
        self.btn_exemplar.setEnabled(base)
        if self._family != "sam3":
            hint = "文字提示仅 SAM3 / SAM3.1 支持"
            exemplar_hint = "范例标注仅 SAM3 / SAM3.1 支持"
        elif not self._ready:
            hint = "加载 SAM3 模型后可用"
            exemplar_hint = "加载 SAM3 模型后可用"
        elif self._busy:
            hint = "正在推理，请稍候…"
            exemplar_hint = "正在推理，请稍候…"
        else:
            exemplar_hint = "用框选工具框住 1 个样本，再点「批量提取同类」"
            if translation.unknown:
                hint = f"未识别中文：{'、'.join(translation.unknown)}；请改用英文或简化描述"
            elif translation.has_chinese and translation.english:
                hint = f"将发送英文提示：{translation.english}"
            else:
                hint = "可输入中文（按内置地物术语表转换）或英文；结果会出现在下方候选掩膜"
        self.text_hint.setText(hint)
        self.exemplar_hint.setText(exemplar_hint)

    # ------------------------------------------------------------------ 候选
    def set_candidates(self, candidates: list[dict]) -> None:
        self.candidate_list.blockSignals(True)
        self.candidate_list.clear()
        for index, candidate in enumerate(candidates):
            score = float(candidate.get("score") or 0.0)
            area = int(candidate.get("area") or 0)
            item = QListWidgetItem(f"掩膜 {index + 1} · 得分 {score:.2f} · {area:,} px")
            item.setData(Qt.ItemDataRole.UserRole, index)
            item.setFlags(item.flags() | Qt.ItemFlag.ItemIsUserCheckable)
            item.setCheckState(Qt.CheckState.Unchecked)
            item.setToolTip(str(candidate.get("path") or ""))
            self.candidate_list.addItem(item)
        self.candidate_list.blockSignals(False)
        has_candidates = bool(candidates)
        self.btn_apply_candidates.setEnabled(has_candidates and not self._busy)
        self.btn_clear_candidates.setEnabled(has_candidates and not self._busy)

    def selected_candidate_indexes(self) -> list[int]:
        indexes = []
        for row in range(self.candidate_list.count()):
            item = self.candidate_list.item(row)
            if item.checkState() == Qt.CheckState.Checked:
                indexes.append(int(item.data(Qt.ItemDataRole.UserRole) or 0))
        return indexes

    def current_candidate_index(self) -> int:
        item = self.candidate_list.currentItem()
        return int(item.data(Qt.ItemDataRole.UserRole) or 0) if item is not None else -1

    def check_all_candidates(self, checked: bool = True) -> None:
        state = Qt.CheckState.Checked if checked else Qt.CheckState.Unchecked
        for row in range(self.candidate_list.count()):
            self.candidate_list.item(row).setCheckState(state)

    def _on_candidate_changed(self, current: QListWidgetItem | None, _previous) -> None:
        if current is not None:
            self.candidate_preview_requested.emit(int(current.data(Qt.ItemDataRole.UserRole) or 0))

    # ------------------------------------------------------------------ 设置
    def brush_size(self) -> int:
        return self.brush_spin.value()

    def opacity(self) -> int:
        return self.opacity_spin.value()

    def prompt_radius(self) -> int:
        return self.prompt_spin.value()

    def auto_params(self) -> dict:
        return {
            "points_per_side": self.points_spin.value(),
            "pred_iou_thresh": self.iou_spin.value(),
            "stability_score_thresh": self.stability_spin.value(),
            "min_mask_region_area": self.min_area_spin.value(),
            "max_candidates": self.max_candidates_spin.value(),
        }

    def set_auto_params(self, config: AutoSegConfig) -> None:
        self.points_spin.setValue(config.points_per_side)
        self.iou_spin.setValue(config.pred_iou_thresh)
        self.stability_spin.setValue(config.stability_score_thresh)
        self.min_area_spin.setValue(config.min_mask_region_area)
        self.max_candidates_spin.setValue(config.max_candidates)

    def set_busy(self, busy: bool) -> None:
        self._busy = bool(busy)
        self.btn_load.setEnabled(self._available and not busy)
        self.btn_restart.setEnabled(self._available and self._ready and not busy)
        has_candidates = self.candidate_list.count() > 0
        self.btn_apply_candidates.setEnabled(not busy and has_candidates)
        self.btn_clear_candidates.setEnabled(not busy and has_candidates)
        self.model_type_combo.setEnabled(not busy)
        self.device_combo.setEnabled(not busy)
        self._refresh_text_controls()

    # ------------------------------------------------------------------ 主题
    def apply_theme(self) -> None:
        theme = ThemeManager.instance().current()
        for button, key in (
            (self.btn_checkpoint, "folder_open"),
            (self.btn_sam_settings, "edit_settings"),
            (self.btn_add_label, "plus_circle"),
            (self.btn_rename_label, "file"),
            (self.btn_color_label, "edit_settings"),
            (self.btn_delete_label, "trash"),
            (self.btn_clear_candidates, "trash"),
            (self.btn_save, "save"),
        ):
            button.setIcon(tool_icons.icon(key, theme.menu_text))
        self.btn_save.setIconSize(QSize(18, 18))
