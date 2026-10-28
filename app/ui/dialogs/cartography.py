"""「制图出图」版面视图（交互对齐 ArcMap 的布局视图）。

打开就是一个**页面画布**，而不是填参数的三步向导：

- 进入时页面上已有地图框（ArcMap 的数据框），内容跟随主窗口画布的范围与图层；
- 顶部工具条按 ArcMap 的习惯分组：**页面**（页面设置 / 适应窗口）、
  **插入**（图名 / 文字 / 图例 / 比例尺 / 指北针 / 图片）、**地图**（同步当前视图 /
  缩放至图层范围）、**工具**（选择 / 平移 / 放大 / 缩小）、**输出**（导出地图 / 打印）；
- 插入的图元直接落在页面上，选中后可拖动、拉框缩放（QgsLayoutView 的选中工具），
  ``Delete`` 删除（地图框除外）；双击图元改属性，右键也有「属性 / 删除」菜单；
- 最后「导出地图」按格式与分辨率整版导出，「打印」走系统打印对话框。

窗口是懒建单实例：关掉再打开，版面内容与摆放都保留（与 ArcMap 的布局文档一致）。
"""

from __future__ import annotations

import contextlib
import os
import warnings
from dataclasses import replace
from pathlib import Path

from qgis.core import (
    QgsLayoutItem,
    QgsLayoutItemLabel,
    QgsLayoutItemLegend,
    QgsLayoutItemMap,
    QgsLayoutItemPage,
    QgsLayoutItemPicture,
    QgsLayoutItemScaleBar,
    QgsLayoutPoint,
    QgsLayoutSize,
    QgsMapLayer,
    QgsProject,
    QgsRectangle,
    QgsSvgCache,
    QgsUnitTypes,
)
from qgis.gui import QgsLayoutView, QgsLayoutViewToolPan, QgsLayoutViewToolSelect
from qgis.PyQt.QtCore import QPointF, QRectF, QSize, QSizeF, Qt, QUrl
from qgis.PyQt.QtGui import (
    QAction,
    QActionGroup,
    QColor,
    QDesktopServices,
    QFont,
    QIcon,
    QImage,
    QPageSize,
    QPainter,
    QPen,
    QPixmap,
    QTransform,
)
from qgis.PyQt.QtPrintSupport import QPrintDialog, QPrinter
from qgis.PyQt.QtWidgets import (
    QApplication,
    QButtonGroup,
    QCheckBox,
    QComboBox,
    QDialog,
    QDoubleSpinBox,
    QFileDialog,
    QFontComboBox,
    QFormLayout,
    QFrame,
    QGridLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMenu,
    QPlainTextEdit,
    QPushButton,
    QSpinBox,
    QTabWidget,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

from app.core.domain.cartography.layout import (
    DEFAULT_FONT_SIZE_PT,
    DEFAULT_NORTH_ARROW_MM,
    DEFAULT_PICTURE_WIDTH_MM,
    add_label,
    add_legend,
    add_north_arrow,
    add_picture,
    add_scale_bar,
    apply_label_properties,
    apply_legend_style,
    apply_map_frame_style,
    apply_north_arrow_style,
    apply_scale_bar_style,
    color_text,
    create_layout,
    create_map_frame,
    disable_grid,
    enable_grid,
    export_layout,
    fit_map_frame_to_page,
    grid_state,
    is_north_arrow,
    map_scale,
    north_arrow_svg_path,
    page_size,
    print_layout,
    read_legend_style,
    read_map_frame_style,
    read_north_arrow_style,
    read_scale_bar_style,
    resize_picture,
    set_map_extent,
    sync_map_frame,
)
from app.core.domain.cartography.settings import (
    CUSTOM_PAPER,
    DEFAULT_NORTH_ARROW_STYLE,
    DEFAULT_SCALE_BAR_STYLE,
    FRAME_JOIN_STYLES,
    MAP_FRAME_PRESETS,
    NORTH_ARROW_STYLES,
    OUTPUT_FORMATS,
    PAGE_SIZES,
    SCALE_BAR_LABEL_HORIZONTAL,
    SCALE_BAR_LABEL_VERTICAL,
    SCALE_BAR_STYLES,
    CartographySettings,
    LegendStyle,
    MapFramePreset,
    MapFrameStyle,
    NorthArrowStyle,
    ScaleBarStyle,
    apply_map_frame_preset,
    default_output_path,
    find_map_frame_preset,
    match_map_frame_preset,
    output_extension,
    output_filter,
    output_note,
    page_size_mm,
)
from app.infra.logging import get_logger
from app.infra.messagebar import send
from app.ui.common import tool_icons
from app.ui.common.base_dialog import BaseDialog
from app.ui.common.message_box import MessageBox
from app.ui.common.ribbon import RibbonToolBar
from app.ui.common.theme import ThemeManager, toolbar_qss
from app.ui.dialogs.layer_properties.widgets import ColorButton

logger = get_logger(__name__)

_MM = QgsUnitTypes.LayoutUnit.LayoutMillimeters

#: 页脚 / 图元默认摆放的页面内边距（毫米）。
_PAGE_PAD_MM = 6.0
#: 图名距页面上沿（毫米）。
_TITLE_TOP_MM = 4.0

#: 指北针缩略图缓存：(SVG 路径, ARGB 颜色, 宽度像素) → QImage。
_NORTH_ARROW_IMAGE_CACHE: dict[tuple[str, str, int], QImage] = {}
_svg_cache: QgsSvgCache | None = None


def _north_arrow_image(style: str, color: QColor, width_px: int) -> QImage | None:
    """把内置指北针 SVG 按指定填充色栅格化，用于样式下拉的图标与预览。

    直接 ``QSvgRenderer`` 渲染不会替换 SVG 里的 ``param(fill)``（箭头会透明），
    必须走 QGIS 的 SVG 缓存。返回 ``QImage``；找不到文件 / 渲染失败返回 ``None``。
    """
    path = north_arrow_svg_path(style)
    if not path:
        return None
    key = (path, color.name(QColor.NameFormat.HexArgb), int(width_px))
    cached = _NORTH_ARROW_IMAGE_CACHE.get(key)
    if cached is not None:
        return cached
    global _svg_cache
    try:
        if _svg_cache is None:
            _svg_cache = QgsSvgCache()
        result = _svg_cache.svgAsImage(path, float(width_px), color, color, 0.0, 1.0)
        # 4.2 返回 (QImage, 是否命中缓存)，3.40 只返回 QImage
        image = result[0] if isinstance(result, tuple) else result
    except Exception:
        logger.warning("指北针样式预览生成失败：%s", style, exc_info=True)
        return None
    if image.isNull():
        return None
    _NORTH_ARROW_IMAGE_CACHE[key] = image
    return image


def _north_arrow_icon(style: str, size_px: int = 48) -> QIcon:
    """样式下拉用的小图标（统一黑色，页面底色为白）。"""
    image = _north_arrow_image(style, QColor("#000000"), size_px)
    if image is None:
        return QIcon()
    return QIcon(QPixmap.fromImage(image))


def _draw_frame_ticks(
    painter: QPainter,
    rect: QRectF,
    length: float,
    color: QColor,
    key: str,
) -> None:
    """在缩略图上画内 / 外 / 内外刻度。"""
    painter.setPen(QPen(color, 0.9))
    inside = key in ("ticks_in", "ticks_both")
    outside = key in ("ticks_out", "ticks_both")
    step = 6.0
    x = rect.left() + 1.5
    while x < rect.right() - 0.5:
        if outside:
            painter.drawLine(QPointF(x, rect.top()), QPointF(x, rect.top() - length))
            painter.drawLine(QPointF(x, rect.bottom()), QPointF(x, rect.bottom() + length))
        if inside:
            painter.drawLine(QPointF(x, rect.top()), QPointF(x, rect.top() + length))
            painter.drawLine(QPointF(x, rect.bottom()), QPointF(x, rect.bottom() - length))
        x += step
    y = rect.top() + 1.5
    while y < rect.bottom() - 0.5:
        if outside:
            painter.drawLine(QPointF(rect.left(), y), QPointF(rect.left() - length, y))
            painter.drawLine(QPointF(rect.right(), y), QPointF(rect.right() + length, y))
        if inside:
            painter.drawLine(QPointF(rect.left(), y), QPointF(rect.left() + length, y))
            painter.drawLine(QPointF(rect.right(), y), QPointF(rect.right() - length, y))
        y += step


def _frame_preset_icon(preset: MapFramePreset) -> QIcon:
    """图框预设的缩略图（纯 QPainter 绘制，不依赖 QGIS 渲染）。

    白框在白纸上看不见，遇到浅色边框时缩略图改用深色底；这只影响预览，
    不影响实际出图。
    """
    color = QColor(preset.frame_color)
    dark_preview = color.lightness() > 215
    pixmap = QPixmap(80, 48)
    pixmap.fill(QColor("#3f4652") if dark_preview else QColor("#f2f4f7"))
    painter = QPainter(pixmap)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
    rect = QRectF(19, 12, 42, 24)
    painter.fillRect(rect, QColor("#7a8496") if dark_preview else QColor("#cfe0f2"))
    line_width = max(0.9, min(3.2, preset.frame_width_mm * 2.4))
    if preset.frame_enabled:
        painter.setPen(QPen(color, line_width))
        painter.setBrush(Qt.BrushStyle.NoBrush)
        painter.drawRect(rect)
    band = max(3.0, min(7.0, preset.decoration_width_mm * 3.0))
    if preset.decoration == "line":
        painter.setPen(QPen(color, max(0.9, min(2.0, line_width))))
        painter.setBrush(Qt.BrushStyle.NoBrush)
        painter.drawRect(rect.adjusted(-band, -band, band, band))
    elif preset.decoration.startswith("ticks"):
        _draw_frame_ticks(painter, rect, band, color, preset.decoration)
    elif preset.decoration == "zebra":
        pen = QPen(color, band)
        pen.setStyle(Qt.PenStyle.CustomDashLine)
        pen.setDashPattern([1.0, 1.0])
        pen.setCapStyle(Qt.PenCapStyle.FlatCap)
        pen.setJoinStyle(Qt.PenJoinStyle.MiterJoin)
        painter.setPen(pen)
        painter.setBrush(Qt.BrushStyle.NoBrush)
        painter.drawRect(rect.adjusted(-band / 2, -band / 2, band / 2, band / 2))
    painter.end()
    return QIcon(pixmap)



def _double_spin(
    minimum: float,
    maximum: float,
    value: float,
    suffix: str,
    *,
    decimals: int = 1,
    step: float = 1.0,
) -> QDoubleSpinBox:
    """范围 / 小数位 / 后缀一致的毫米或字号输入框。"""
    spin = QDoubleSpinBox()
    spin.setRange(float(minimum), float(maximum))
    spin.setDecimals(int(decimals))
    spin.setSingleStep(float(step))
    if suffix:
        spin.setSuffix(suffix)
    spin.setValue(max(float(minimum), min(float(maximum), float(value))))
    return spin


def _int_spin(minimum: int, maximum: int, value: int, suffix: str = "") -> QSpinBox:
    spin = QSpinBox()
    spin.setRange(int(minimum), int(maximum))
    if suffix:
        spin.setSuffix(suffix)
    spin.setValue(max(int(minimum), min(int(maximum), int(value))))
    return spin


def _item_size_mm(item) -> tuple[float, float]:
    size = item.sizeWithUnits()
    return float(size.width()), float(size.height())


def _is_spatial(layer: QgsMapLayer) -> bool:
    try:
        return bool(layer.isSpatial())
    except Exception:  # noqa: BLE001 —— 已删除的图层对象可能随时抛异常
        return False


class _LayoutView(QgsLayoutView):
    """版面画布：接管右键菜单、双击属性与 Delete 删除。"""

    def __init__(self, owner: CartographyDialog) -> None:
        super().__init__()
        self._owner = owner

    def contextMenuEvent(self, event) -> None:
        menu = self._owner._item_menu(self._layout_item_at(event.pos()))
        if menu is not None and not menu.isEmpty():
            menu.exec(event.globalPos())
        event.accept()

    def mouseDoubleClickEvent(self, event) -> None:
        item = self._layout_item_at(event.pos())
        if item is not None and not isinstance(item, QgsLayoutItemPage):
            self._owner._edit_item(item)
            event.accept()
            return
        super().mouseDoubleClickEvent(event)

    def _layout_item_at(self, pos):
        """鼠标位置下真正的版面图元。

        ``itemAt`` 命中的可能是 QGIS 内部图元：选中图元时是
        ``QgsLayoutMouseHandles`` 的 ``QGraphicsRectItem``（z=10000），
        纸面空白处是页面背景的 ``QGraphicsRectItem``。把它们交给按
        ``QgsLayoutItem`` 写的右键 / 双击逻辑，会让属性菜单与双击失效，
        删除还会在 ``removeLayoutItem`` 抛 TypeError。命中非版面图元时
        从场景栈往下找第一个 ``QgsLayoutItem``（页面也是其中一种）。
        """
        item = self.itemAt(pos)
        if item is None or isinstance(item, QgsLayoutItem):
            return item
        scene = self.scene()
        if scene is None:
            return None
        for candidate in scene.items(self.mapToScene(pos)):
            if isinstance(candidate, QgsLayoutItem):
                return candidate
        return None

    def keyPressEvent(self, event) -> None:
        if event.key() in (Qt.Key.Key_Delete, Qt.Key.Key_Backspace):
            self._owner._delete_selected()
            event.accept()
            return
        super().keyPressEvent(event)


class CartographyDialog(BaseDialog):
    """制图出图版面视图（懒建单实例，见主窗口 ``_show_cartography_dialog``）。"""

    def __init__(
        self,
        parent=None,
        title: str = "制图出图",
        *,
        canvas=None,
        project: QgsProject | None = None,
    ) -> None:
        super().__init__(parent, title)
        self.setMinimumSize(1080, 700)
        self.resize(1320, 860)
        self._canvas = canvas if canvas is not None else getattr(parent, "canvas", None)
        self._project = project if project is not None else QgsProject.instance()
        self._settings = CartographySettings()
        self._export_dpi = 300
        self._open_after_export = True
        self._first_shown = False
        self._built = False

        self._build_ui()
        self._init_layout()

        if self._canvas is not None:
            self._canvas.extentsChanged.connect(self._on_canvas_extent_changed)
            self._canvas.layersChanged.connect(self._on_canvas_layers_changed)

    # ============================================================ UI

    def _build_ui(self) -> None:
        outer = QVBoxLayout()
        outer.setContentsMargins(10, 8, 10, 8)
        outer.setSpacing(8)

        outer.addWidget(self._build_toolbar())

        self._view = _LayoutView(self)
        self._view.setFrameShape(QFrame.Shape.NoFrame)
        self._view.setMinimumSize(600, 380)
        outer.addWidget(self._view, 1)

        footer = QHBoxLayout()
        footer.setSpacing(8)
        self.status_label = QLabel("就绪")
        self.status_label.setWordWrap(True)
        footer.addWidget(self.status_label, 1)
        hint = QLabel("拖动移动图元，双击改属性，Delete 删除（地图框除外）")
        hint.setEnabled(False)
        footer.addWidget(hint)
        close_btn = QPushButton("关闭")
        close_btn.clicked.connect(self.close)
        footer.addWidget(close_btn)
        outer.addLayout(footer)
        self.setLayout(outer)

    def _build_toolbar(self) -> RibbonToolBar:
        bar = RibbonToolBar("制图工具", self)
        bar.setObjectName("CartographyLayoutToolbar")
        bar.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextUnderIcon)
        icon_size = 20
        bar.setIconSize(QSize(icon_size, icon_size))
        theme = ThemeManager.instance().current()

        def action(text: str, key: str, slot, *, tooltip: str = "", checkable: bool = False) -> QAction:
            act = QAction(text, self)
            act.setIcon(tool_icons.icon(key, theme.input_focus, icon_size))
            act.setToolTip(tooltip or text)
            act.setCheckable(checkable)
            act.triggered.connect(slot)
            return act

        # ---- 页面 ----
        self._act_page_setup = action("页面设置", "page_setup", self._on_page_setup, tooltip="纸张大小与方向")
        self._act_fit = action("适应窗口", "zoom_full", self._zoom_full, tooltip="整页显示")

        # ---- 插入（ArcMap 的「插入」菜单）----
        self._act_insert_title = action("图名", "heading", self._insert_title, tooltip="在页面顶部居中插入图名")
        self._act_insert_text = action("文字", "text", self._insert_text, tooltip="插入自由文字")
        self._act_insert_legend = action("图例", "legend", self._insert_legend, tooltip="插入图例（链接地图框）")
        self._act_insert_scale = action(
            "比例尺", "measure_line", self._insert_scale_bar, tooltip="插入比例尺（链接地图框）"
        )
        self._act_insert_north = action("指北针", "compass", self._insert_north_arrow, tooltip="插入指北针")
        self._act_insert_picture = action(
            "图片", "cat_image", self._insert_picture, tooltip="插入图片（PNG / JPEG / SVG）"
        )

        # ---- 地图 ----
        self._act_sync_canvas = action("当前视图", "refresh", self._sync_to_canvas, tooltip="地图框跟随主窗口当前视图")
        self._act_sync_layers = action(
            "图层范围", "zoom_full", self._sync_to_layers, tooltip="地图框缩放到全部出图图层范围"
        )

        # ---- 工具 ----
        self._tool_group = QActionGroup(self)
        self._tool_group.setExclusive(True)
        self._act_select = action(
            "选择", "select_point", self._activate_select, tooltip="选择 / 拖动 / 缩放图元", checkable=True
        )
        self._act_select.setChecked(True)
        self._act_pan = action("平移", "pan", self._activate_pan, tooltip="平移版面视图", checkable=True)
        for act in (self._act_select, self._act_pan):
            self._tool_group.addAction(act)
        self._act_zoom_in = action("放大", "zoom_in", self._view_zoom_in, tooltip="放大版面视图")
        self._act_zoom_out = action("缩小", "zoom_out", self._view_zoom_out, tooltip="缩小版面视图")

        # ---- 输出 ----
        self._act_export = action("导出地图", "export_csv", self._on_export, tooltip="按当前版面导出 PDF / 图片")
        self._act_print = action("打印", "print", self._on_print, tooltip="打印当前版面")

        groups = [
            ("页面", [self._act_page_setup, self._act_fit]),
            (
                "插入",
                [
                    self._act_insert_title,
                    self._act_insert_text,
                    self._act_insert_legend,
                    self._act_insert_scale,
                    self._act_insert_north,
                    self._act_insert_picture,
                ],
            ),
            ("地图", [self._act_sync_canvas, self._act_sync_layers]),
            ("工具", [self._act_select, self._act_pan, self._act_zoom_in, self._act_zoom_out]),
            ("输出", [self._act_export, self._act_print]),
        ]
        for idx, (_name, actions) in enumerate(groups):
            for act in actions:
                bar.addAction(act)
            if idx != len(groups) - 1:
                bar.addSeparator()
        bar.set_groups(groups, theme.text_secondary)
        bar.setStyleSheet(toolbar_qss(theme))
        return bar

    # ============================================================ 版面

    def _init_layout(self) -> None:
        self._layout = create_layout(self._project, *page_size_mm(self._settings))
        # 版面不注册进 project.layoutManager()（避免把版面写进工程），但必须
        # 挂到对话框名下：无主 QgsPrintLayout 会活过 QgsApplication.exitQgis()，
        # 地图图元内部的 100 ms 刷新定时器随后被事件循环投递到已拆掉的 QGIS，
        # 直接 access violation（pytest 收尾可复现）。挂父子后随窗口一起析构。
        self._layout.setParent(self)
        layers = self._spatial_layers()
        self._map_item = create_map_frame(self._layout, layers, self._current_extent(layers))
        self._map_item.extentChanged.connect(self._on_map_extent_changed)

        self._view.setCurrentLayout(self._layout)
        self._select_tool = QgsLayoutViewToolSelect(self._view)
        # 关键：选择工具的鼠标手柄（QgsLayoutMouseHandles）只在 setLayout()
        # 里创建。QgsLayoutView.setCurrentLayout 只认基类 QgsLayoutViewTool，
        # 不会替自建工具做这一步；漏掉后鼠标一移进版面就会在
        # QgsLayoutViewToolSelect::layoutMoveEvent 里解引用空指针
        # （0xC0000005，读 [0+0xd5]），整窗闪退且 Python 层无堆栈。
        self._select_tool.setLayout(self._layout)
        self._pan_tool = QgsLayoutViewToolPan(self._view)
        self._view.setTool(self._select_tool)
        self._view.zoomFull()
        self._apply_view_theme()
        self._built = True
        self._update_status()

    def _apply_view_theme(self) -> None:
        theme = ThemeManager.instance().current()
        scene = self._view.scene()
        if scene is not None:
            # 页面本身是白色，四周用窗体底色衬出「纸张」边界
            scene.setBackgroundBrush(QColor(theme.window_bg))

    # ---- 画布联动（ArcMap：布局里的数据框跟随数据视图） ----

    def _spatial_layers(self) -> list[QgsMapLayer]:
        """出图图层：有画布时用画布当前可见图层（与用户看到的一致），否则用全工程。"""
        if self._canvas is not None:
            return [layer for layer in self._canvas.layers() if layer is not None and _is_spatial(layer)]
        return [layer for layer in self._project.mapLayers().values() if layer is not None and _is_spatial(layer)]

    def _layers_extent(self, layers: list[QgsMapLayer]) -> QgsRectangle | None:
        extents = []
        for layer in layers:
            extent = layer.extent()
            if extent is not None and not extent.isEmpty():
                extents.append(extent)
        if not extents:
            return None
        union = QgsRectangle(extents[0])
        for extent in extents[1:]:
            union.combineExtentWith(extent)
        return union if not union.isEmpty() else None

    def _current_extent(self, layers: list[QgsMapLayer]) -> QgsRectangle | None:
        if self._canvas is not None:
            extent = self._canvas.extent()
            if extent is not None and not extent.isEmpty():
                return QgsRectangle(extent)
        if layers:
            return self._layers_extent(layers)
        return QgsRectangle(0.0, 0.0, 100.0, 100.0)

    def _sync_from_canvas(self, *, update_extent: bool = True) -> None:
        if self._canvas is None:
            return
        layers = self._spatial_layers()
        extent = self._current_extent(layers) if update_extent else None
        sync_map_frame(self._map_item, layers, extent)
        self._refresh_legends()
        self._update_status()

    def _on_canvas_extent_changed(self, *_args) -> None:
        if not self.isVisible():
            return
        set_map_extent(self._map_item, self._canvas.extent())
        self._update_status()

    def _on_canvas_layers_changed(self, *_args) -> None:
        if not self.isVisible():
            return
        self._map_item.setLayers(self._spatial_layers())
        self._refresh_legends()
        self._map_item.update()
        self._update_status()

    def _refresh_legends(self) -> None:
        for item in self._layout.items():
            if not isinstance(item, QgsLayoutItemLegend):
                continue
            with warnings.catch_warnings():
                warnings.simplefilter("ignore", DeprecationWarning)
                item.updateLegend()

    def _sync_to_canvas(self) -> None:
        if self._canvas is None:
            MessageBox.information(self, "制图出图", "当前窗口没有关联地图画布。")
            return
        self._sync_from_canvas(update_extent=True)
        self._view.setTool(self._select_tool)
        self._act_select.setChecked(True)
        self._update_status("已同步主窗口的当前视图")

    def _sync_to_layers(self) -> None:
        extent = self._layers_extent(self._spatial_layers())
        if extent is None:
            MessageBox.information(self, "制图出图", "工程里没有带范围的空间图层。")
            return
        set_map_extent(self._map_item, extent)
        self._update_status("已缩放到全部图层范围")

    def _on_map_extent_changed(self, *_args) -> None:
        self._update_status()

    # ============================================================ 工具

    def _activate_select(self) -> None:
        self._view.setTool(self._select_tool)
        self._act_select.setChecked(True)

    def _activate_pan(self) -> None:
        self._view.setTool(self._pan_tool)
        self._act_pan.setChecked(True)

    def _zoom_full(self) -> None:
        self._view.zoomFull()

    def _view_zoom_in(self) -> None:
        self._view.zoomIn()

    def _view_zoom_out(self) -> None:
        self._view.zoomOut()

    # ============================================================ 插入

    def _insert_title(self) -> None:
        default = self._project.title().strip()
        if not default and self._project.fileName():
            default = Path(self._project.fileName()).stem
        label = add_label(
            self._layout,
            default or "图名",
            point_size=DEFAULT_FONT_SIZE_PT,
            h_align=Qt.AlignmentFlag.AlignHCenter,
        )
        self._place_title(label)
        self._select_item(label)
        self._update_status("已插入图名（双击改文字 / 字体）")

    def _insert_text(self) -> None:
        label = add_label(
            self._layout,
            "文字",
            point_size=DEFAULT_FONT_SIZE_PT * 0.6,
            h_align=Qt.AlignmentFlag.AlignLeft,
            max_width_mm=60.0,
        )
        self._place_view_center(label)
        self._select_item(label)
        self._update_status("已插入文字（双击改内容）")

    def _insert_legend(self) -> None:
        legend = add_legend(self._layout, self._map_item, title="图例", font_family=self._default_font_family())
        self._place_bottom_left(legend)
        self._select_item(legend)
        self._update_status("已插入图例（双击改字体 / 边框 / 背景）")

    def _insert_scale_bar(self) -> None:
        bar = add_scale_bar(self._layout, self._map_item, font_family=self._default_font_family())
        self._place_bottom_right(bar)
        self._select_item(bar)
        self._update_status("已插入比例尺（双击改样式 / 颜色 / 文字）")

    def _insert_north_arrow(self) -> None:
        arrow = add_north_arrow(self._layout, size_mm=DEFAULT_NORTH_ARROW_MM)
        if arrow is None:
            MessageBox.warning(self, "制图出图", "QGIS 安装目录里没有内置指北针 SVG，无法插入。")
            return
        self._place_map_top_right(arrow)
        self._select_item(arrow)
        self._update_status("已插入指北针（双击改样式 / 颜色 / 旋转）")

    def _insert_picture(self) -> None:
        path, _selected = QFileDialog.getOpenFileName(
            self,
            "插入图片",
            "",
            "图片 (*.png *.jpg *.jpeg *.bmp *.tif *.tiff *.svg);;所有文件 (*)",
        )
        if not path:
            return
        picture = add_picture(self._layout, path, width_mm=DEFAULT_PICTURE_WIDTH_MM)
        self._place_view_center(picture)
        self._select_item(picture)
        self._update_status(f"已插入图片：{Path(path).name}")

    def _default_font_family(self) -> str:
        return self.font().family()

    # ---- 摆放 ----

    def _move_item(self, item, x: float, y: float) -> None:
        width, height = _item_size_mm(item)
        page_w, page_h = page_size(self._layout)
        x = min(max(0.0, float(x)), max(0.0, page_w - width))
        y = min(max(0.0, float(y)), max(0.0, page_h - height))
        item.attemptMove(QgsLayoutPoint(x, y, _MM))

    def _place_title(self, item) -> None:
        page_w, _page_h = page_size(self._layout)
        width, _height = _item_size_mm(item)
        self._move_item(item, (page_w - width) / 2.0, _TITLE_TOP_MM)

    def _place_view_center(self, item) -> None:
        center = self._view.mapToScene(self._view.viewport().rect().center())
        width, height = _item_size_mm(item)
        self._move_item(item, center.x() - width / 2.0, center.y() - height / 2.0)

    def _place_bottom_left(self, item) -> None:
        _page_w, page_h = page_size(self._layout)
        _width, height = _item_size_mm(item)
        self._move_item(item, _PAGE_PAD_MM, page_h - _PAGE_PAD_MM - height)

    def _place_bottom_right(self, item) -> None:
        page_w, page_h = page_size(self._layout)
        width, height = _item_size_mm(item)
        self._move_item(item, page_w - _PAGE_PAD_MM - width, page_h - _PAGE_PAD_MM - height)

    def _place_map_top_right(self, item) -> None:
        # QgsLayoutItem.rect() 是图元自身坐标（原点在左上角），页面位置要看
        # positionWithUnits()；用 rect().top() 会把图元摆到页面左上角。
        position = self._map_item.positionWithUnits()
        map_width, _map_height = _item_size_mm(self._map_item)
        width, _height = _item_size_mm(item)
        self._move_item(item, position.x() + map_width - width - 3.0, position.y() + 3.0)

    def _select_item(self, item) -> None:
        self._layout.setSelectedItem(item)
        self._view.setTool(self._select_tool)
        self._act_select.setChecked(True)
        self._view.setFocus()
        with contextlib.suppress(RuntimeError, TypeError):
            self._view.ensureVisible(item, 20, 20)

    # ============================================================ 右键 / 删除 / 属性

    def _item_menu(self, item) -> QMenu | None:
        menu = QMenu(self)
        if item is None or isinstance(item, QgsLayoutItemPage):
            menu.addAction("页面设置…", self._on_page_setup)
            return menu
        menu.addAction("属性…", lambda: self._edit_item(item))
        if item is self._map_item:
            menu.addSeparator()
            menu.addAction("同步当前视图", self._sync_to_canvas)
            menu.addAction("缩放至图层范围", self._sync_to_layers)
        else:
            menu.addSeparator()
            menu.addAction("删除", lambda: self._delete_item(item))
        return menu

    def _delete_item(self, item) -> None:
        if item is self._map_item or isinstance(item, QgsLayoutItemPage):
            return
        self._layout.removeLayoutItem(item)
        try:
            item.deleteLater()
        except AttributeError:
            from qgis.PyQt import sip  # noqa: PLC0415 —— 仅销毁图元时用到

            sip.delete(item)
        self._layout.update()
        self._update_status("已删除图元")

    def _delete_selected(self) -> None:
        items = [
            item
            for item in self._layout.selectedLayoutItems()
            if item is not self._map_item and not isinstance(item, QgsLayoutItemPage)
        ]
        if not items:
            self._update_status("没有可删除的图元（地图框不能删除）")
            return
        for item in items:
            self._delete_item(item)

    def _edit_item(self, item) -> None:
        if isinstance(item, QgsLayoutItemMap):
            self._edit_map(item)
        elif isinstance(item, QgsLayoutItemLegend):
            self._edit_legend(item)
        elif isinstance(item, QgsLayoutItemScaleBar):
            self._edit_scale_bar(item)
        elif isinstance(item, QgsLayoutItemPicture):
            # 内置指北针虽然也是 QgsLayoutItemPicture，但要按「指北针」改样式，
            # 不能落到图片对话框里只换文件
            if is_north_arrow(item):
                self._edit_north_arrow(item)
            else:
                self._edit_picture(item)
        elif isinstance(item, QgsLayoutItemLabel):
            self._edit_label(item)

    def _edit_label(self, item: QgsLayoutItemLabel) -> None:
        dialog = _LabelPropertiesDialog(self, item)
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return
        text, family, size, align = dialog.values()
        apply_label_properties(item, text, font_family=family, point_size=size, h_align=align)
        self._layout.update()
        self._update_status("图元属性已更新")

    def _edit_legend(self, item: QgsLayoutItemLegend) -> None:
        dialog = _LegendPropertiesDialog(self, item)
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return
        title, style = dialog.values()
        apply_legend_style(item, title, style)
        self._layout.update()
        self._update_status("图例属性已更新")

    def _edit_scale_bar(self, item: QgsLayoutItemScaleBar) -> None:
        dialog = _ScaleBarPropertiesDialog(self, item)
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return
        style = dialog.values()
        apply_scale_bar_style(item, style)
        self._layout.update()
        self._update_status("比例尺属性已更新")

    def _edit_north_arrow(self, item: QgsLayoutItemPicture) -> None:
        dialog = _NorthArrowPropertiesDialog(self, item)
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return
        apply_north_arrow_style(item, dialog.values())
        self._layout.update()
        self._update_status("指北针属性已更新")

    def _edit_picture(self, item: QgsLayoutItemPicture) -> None:
        dialog = _PicturePropertiesDialog(self, item)
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return
        path, width = dialog.values()
        resize_picture(item, width, path=path)
        self._layout.update()
        self._update_status("图片属性已更新")

    def _edit_map(self, item: QgsLayoutItemMap) -> None:
        dialog = _MapPropertiesDialog(self, item)
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return
        frame_style, grid_enabled, interval = dialog.values()
        apply_map_frame_style(item, frame_style)
        if grid_enabled:
            enable_grid(item, interval)
        else:
            disable_grid(item)
        self._layout.update()
        self._update_status("地图框属性已更新")

    # ============================================================ 页面设置

    def _on_page_setup(self) -> None:
        dialog = _PageSetupDialog(self, self._settings)
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return
        self._settings = dialog.settings()
        width, height = page_size_mm(self._settings)
        page = self._layout.pageCollection().page(0)
        page.setPageSize(QgsLayoutSize(width, height, _MM))
        self._layout.updateBounds()
        # 换纸张 / 方向后把地图框重新铺满页面，其他整饰图元保持原位（可自行拖动）
        fit_map_frame_to_page(self._map_item)
        self._layout.update()
        self._view.zoomFull()
        self._update_status(f"页面已改为 {self._settings.paper}")

    # ============================================================ 导出 / 打印

    def _export_default_path(self) -> str:
        project_file = self._project.fileName()
        if project_file:
            directory = str(Path(project_file).parent)
        else:
            documents = Path.home() / "Documents"
            directory = str(documents if documents.is_dir() else Path.home())
        title = self._project.title().strip()
        if not title and project_file:
            title = Path(project_file).stem
        return default_output_path("pdf", title, directory)

    def _on_export(self) -> None:
        dialog = _ExportMapDialog(
            self,
            default_path=self._export_default_path(),
            dpi=self._export_dpi,
            open_after=self._open_after_export,
        )
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return
        path, dpi, open_after = dialog.values()
        self._export_dpi = dpi
        self._open_after_export = open_after

        self._update_status("正在导出…")
        QApplication.setOverrideCursor(Qt.CursorShape.WaitCursor)
        try:
            ok, message = export_layout(self._layout, path, dpi=dpi)
        finally:
            QApplication.restoreOverrideCursor()

        if not ok:
            self._update_status("导出失败")
            MessageBox.warning(self, "导出失败", message)
            return

        self._update_status(f"已导出：{path}")
        send("success", f"地图已导出：{path}", duration=5)
        logger.info("制图导出成功：%s", path)
        if open_after:
            QDesktopServices.openUrl(QUrl.fromLocalFile(path))

    def _on_print(self) -> None:
        """打开系统打印对话框并打印整版。"""
        width, height = page_size(self._layout)
        printer = QPrinter(QPrinter.PrinterMode.HighResolution)
        printer.setPageSize(QPageSize(QSizeF(width, height), QPageSize.Unit.Millimeter))
        dialog = QPrintDialog(printer, self)
        dialog.setWindowTitle("打印地图")
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return
        QApplication.setOverrideCursor(Qt.CursorShape.WaitCursor)
        try:
            ok, message = print_layout(self._layout, printer, dpi=self._export_dpi)
        finally:
            QApplication.restoreOverrideCursor()
        if not ok:
            MessageBox.warning(self, "打印失败", message)
            return
        send("success", "地图已发送到打印机", duration=5)

    def open_print_dialog(self) -> None:
        """供顶部「打印地图」入口在窗口显示后直接打开打印对话框。"""
        self._on_print()

    # ============================================================ 状态 / 事件

    def _update_status(self, hint: str = "") -> None:
        page_w, page_h = page_size(self._layout)
        scale = map_scale(self._map_item)
        scale_text = f"1:{int(scale):,}" if scale and scale > 0 else "—"
        text = f"页面 {self._settings.paper}  {page_w:.0f} × {page_h:.0f} mm    地图比例尺 {scale_text}"
        if hint:
            text += f"    {hint}"
        self.status_label.setText(text)

    def showEvent(self, event) -> None:
        super().showEvent(event)
        self._apply_view_theme()
        if not self._first_shown:
            self._first_shown = True
            self._view.zoomFull()
        if self._built:
            self._sync_from_canvas(update_extent=True)


# ====================================================================== 属性对话框


class _LabelPropertiesDialog(BaseDialog):
    """图名 / 文字属性：文字、字体、字号、对齐。"""

    def __init__(self, parent, label: QgsLayoutItemLabel) -> None:
        super().__init__(parent, title="图名 / 文字属性", show_min_button=False, show_max_button=False)
        self.setMinimumSize(0, 0)
        self.resize(520, 340)
        layout = QVBoxLayout()
        layout.setSpacing(8)
        form = QFormLayout()
        form.setHorizontalSpacing(10)
        form.setVerticalSpacing(8)

        self.text_edit = QPlainTextEdit(label.text())
        self.text_edit.setFixedHeight(84)
        form.addRow("文字", self.text_edit)

        self.font_combo = QFontComboBox()
        font = QFont(label.textFormat().font())
        self.font_combo.setCurrentFont(font)
        form.addRow("字体", self.font_combo)

        self.size_spin = QSpinBox()
        self.size_spin.setRange(6, 200)
        self.size_spin.setSuffix(" pt")
        self.size_spin.setValue(max(6, int(font.pointSizeF() or DEFAULT_FONT_SIZE_PT)))
        form.addRow("字号", self.size_spin)

        self.align_combo = QComboBox()
        self.align_combo.addItem("左对齐", Qt.AlignmentFlag.AlignLeft)
        self.align_combo.addItem("居中", Qt.AlignmentFlag.AlignHCenter)
        self.align_combo.addItem("右对齐", Qt.AlignmentFlag.AlignRight)
        index = self.align_combo.findData(label.hAlign())
        if index >= 0:
            self.align_combo.setCurrentIndex(index)
        form.addRow("对齐", self.align_combo)
        layout.addLayout(form)
        layout.addStretch(1)
        layout.addLayout(_dialog_buttons(self))
        self.setLayout(layout)

    def values(self) -> tuple[str, str, int, Qt.AlignmentFlag]:
        align = self.align_combo.currentData()
        if align is None:
            align = Qt.AlignmentFlag.AlignLeft
        return (
            self.text_edit.toPlainText(),
            self.font_combo.currentFont().family(),
            self.size_spin.value(),
            align,
        )


class _NorthArrowPropertiesDialog(BaseDialog):
    """指北针属性：样式 / 宽度 / 旋转 / 填充色（对齐 ArcMap 的指北针选择器）。"""

    def __init__(self, parent, picture: QgsLayoutItemPicture) -> None:
        super().__init__(parent, title="指北针属性", show_min_button=False, show_max_button=False)
        self.setMinimumSize(0, 0)
        self.resize(580, 380)
        current = read_north_arrow_style(picture)

        form = QFormLayout()
        form.setHorizontalSpacing(10)
        form.setVerticalSpacing(8)

        self.style_combo = QComboBox()
        self.style_combo.setIconSize(QSize(28, 28))
        for label, style in NORTH_ARROW_STYLES:
            self.style_combo.addItem(_north_arrow_icon(style), label, style)
        index = self.style_combo.findData(current.style)
        self.style_combo.setCurrentIndex(max(0, index))
        form.addRow("样式", self.style_combo)

        self.width_spin = _double_spin(5.0, 200.0, current.width_mm, " mm")
        form.addRow("宽度", self.width_spin)

        self.rotation_spin = _double_spin(-180.0, 180.0, current.rotation, " °", decimals=1, step=5.0)
        form.addRow("旋转", self.rotation_spin)

        self.color_btn = ColorButton(QColor(current.fill_color))
        form.addRow("填充颜色", self.color_btn)

        form_widget = QWidget()
        form_widget.setLayout(form)

        self.preview = QLabel()
        self.preview.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.preview.setMinimumSize(150, 120)
        self.preview.setFrameShape(QFrame.Shape.StyledPanel)
        preview_box = QVBoxLayout()
        preview_box.setSpacing(6)
        preview_box.addWidget(QLabel("预览"))
        preview_box.addWidget(self.preview, 1)

        body = QHBoxLayout()
        body.setSpacing(12)
        body.addWidget(form_widget, 1)
        body.addLayout(preview_box)

        layout = QVBoxLayout()
        layout.setSpacing(8)
        layout.addLayout(body, 1)
        hint = QLabel("指北针按宽度等比缩放；旋转绕图元中心，拖动把手可继续缩放。")
        hint.setWordWrap(True)
        hint.setEnabled(False)
        layout.addWidget(hint)
        layout.addLayout(_dialog_buttons(self))
        self.setLayout(layout)

        self.style_combo.currentIndexChanged.connect(self._sync_preview)
        self.color_btn.color_changed.connect(self._sync_preview)
        self.rotation_spin.valueChanged.connect(self._sync_preview)
        self._sync_preview()

    def _sync_preview(self, *_args) -> None:
        style = str(self.style_combo.currentData() or DEFAULT_NORTH_ARROW_STYLE)
        image = _north_arrow_image(style, self.color_btn.color(), 240)
        if image is None:
            self.preview.clear()
            self.preview.setText("（无预览）")
            return
        pixmap = QPixmap.fromImage(image)
        rotation = self.rotation_spin.value()
        if rotation:
            # transformed 会自动扩大画布，转完再缩放到预览框
            pixmap = pixmap.transformed(
                QTransform().rotate(rotation),
                Qt.TransformationMode.SmoothTransformation,
            )
        self.preview.setPixmap(
            pixmap.scaled(
                QSize(130, 110),
                Qt.AspectRatioMode.KeepAspectRatio,
                Qt.TransformationMode.SmoothTransformation,
            )
        )

    def values(self) -> NorthArrowStyle:
        return NorthArrowStyle(
            style=str(self.style_combo.currentData() or DEFAULT_NORTH_ARROW_STYLE),
            width_mm=self.width_spin.value(),
            rotation=self.rotation_spin.value(),
            fill_color=color_text(self.color_btn.color()),
        )


class _LegendPropertiesDialog(BaseDialog):
    """图例属性：标题 / 字体 / 符号尺寸 / 列数 / 边框与背景。"""

    def __init__(self, parent, legend: QgsLayoutItemLegend) -> None:
        super().__init__(parent, title="图例属性", show_min_button=False, show_max_button=False)
        self.setMinimumSize(0, 0)
        self.resize(580, 460)
        self._base = read_legend_style(legend)

        tabs = QTabWidget()
        tabs.addTab(self._build_text_tab(legend.title()), "常规")
        tabs.addTab(self._build_frame_tab(), "边框与背景")

        layout = QVBoxLayout()
        layout.setSpacing(8)
        layout.addWidget(tabs, 1)
        hint = QLabel("图例内容跟随地图框的图层与范围自动更新；改字号 / 列数后图例框会自动重排。")
        hint.setWordWrap(True)
        hint.setEnabled(False)
        layout.addWidget(hint)
        layout.addLayout(_dialog_buttons(self))
        self.setLayout(layout)

    def _build_text_tab(self, title: str) -> QWidget:
        current = self._base
        page = QWidget()
        form = QFormLayout(page)
        form.setHorizontalSpacing(10)
        form.setVerticalSpacing(8)

        self.title_edit = QLineEdit(title)
        form.addRow("标题", self.title_edit)

        self.font_combo = QFontComboBox()
        if current.font_family:
            self.font_combo.setCurrentFont(QFont(current.font_family))
        form.addRow("字体", self.font_combo)

        self.title_size_spin = _double_spin(6.0, 72.0, current.title_size, " pt", decimals=0)
        form.addRow("标题字号", self.title_size_spin)

        self.item_size_spin = _double_spin(6.0, 72.0, current.item_size, " pt", decimals=0)
        form.addRow("内容字号", self.item_size_spin)

        self.font_color_btn = ColorButton(QColor(current.font_color))
        form.addRow("文字颜色", self.font_color_btn)

        self.columns_spin = _int_spin(1, 6, current.columns)
        form.addRow("列数", self.columns_spin)

        self.symbol_width_spin = _double_spin(1.0, 50.0, current.symbol_width_mm, " mm", decimals=1, step=0.5)
        form.addRow("符号宽度", self.symbol_width_spin)

        self.symbol_height_spin = _double_spin(1.0, 50.0, current.symbol_height_mm, " mm", decimals=1, step=0.5)
        form.addRow("符号高度", self.symbol_height_spin)

        self.line_spacing_spin = _double_spin(0.5, 5.0, current.line_spacing, "", decimals=1, step=0.1)
        form.addRow("行间距", self.line_spacing_spin)
        return page

    def _build_frame_tab(self) -> QWidget:
        current = self._base
        page = QWidget()
        form = QFormLayout(page)
        form.setHorizontalSpacing(10)
        form.setVerticalSpacing(8)

        self.background_check = QCheckBox("显示背景")
        self.background_check.setChecked(current.background_enabled)
        form.addRow("背景", self.background_check)
        self.background_color_btn = ColorButton(QColor(current.background_color))
        form.addRow("背景颜色", self.background_color_btn)

        self.frame_check = QCheckBox("显示边框")
        self.frame_check.setChecked(current.frame_enabled)
        form.addRow("边框", self.frame_check)
        self.frame_color_btn = ColorButton(QColor(current.frame_color))
        form.addRow("边框颜色", self.frame_color_btn)
        self.frame_width_spin = _double_spin(0.0, 5.0, current.frame_width_mm, " mm", decimals=2, step=0.1)
        form.addRow("边框宽度", self.frame_width_spin)

        self.background_check.toggled.connect(self.background_color_btn.setEnabled)
        self.frame_check.toggled.connect(self.frame_color_btn.setEnabled)
        self.frame_check.toggled.connect(self.frame_width_spin.setEnabled)
        self.background_color_btn.setEnabled(self.background_check.isChecked())
        self.frame_color_btn.setEnabled(self.frame_check.isChecked())
        self.frame_width_spin.setEnabled(self.frame_check.isChecked())
        return page

    def values(self) -> tuple[str, LegendStyle]:
        return self.title_edit.text(), replace(
            self._base,
            font_family=self.font_combo.currentFont().family(),
            title_size=float(self.title_size_spin.value()),
            item_size=float(self.item_size_spin.value()),
            font_color=color_text(self.font_color_btn.color()),
            columns=int(self.columns_spin.value()),
            symbol_width_mm=self.symbol_width_spin.value(),
            symbol_height_mm=self.symbol_height_spin.value(),
            line_spacing=self.line_spacing_spin.value(),
            background_enabled=self.background_check.isChecked(),
            background_color=color_text(self.background_color_btn.color()),
            frame_enabled=self.frame_check.isChecked(),
            frame_color=color_text(self.frame_color_btn.color()),
            frame_width_mm=self.frame_width_spin.value(),
        )


class _ScaleBarPropertiesDialog(BaseDialog):
    """比例尺属性：样式 / 单位 / 分段 / 符号颜色 / 文字（对齐 ArcMap）。"""

    def __init__(self, parent, bar: QgsLayoutItemScaleBar) -> None:
        super().__init__(parent, title="比例尺属性", show_min_button=False, show_max_button=False)
        self.setMinimumSize(0, 0)
        self.resize(580, 460)
        self._base = read_scale_bar_style(bar)

        tabs = QTabWidget()
        tabs.addTab(self._build_general_tab(), "常规")
        tabs.addTab(self._build_symbol_tab(), "符号")
        tabs.addTab(self._build_text_tab(), "文字")

        layout = QVBoxLayout()
        layout.setSpacing(8)
        layout.addWidget(tabs, 1)
        hint = QLabel("改样式 / 分段数 / 字号后比例尺按当前地图比例尺自动重算长度。")
        hint.setWordWrap(True)
        hint.setEnabled(False)
        layout.addWidget(hint)
        layout.addLayout(_dialog_buttons(self))
        self.setLayout(layout)

    def _build_general_tab(self) -> QWidget:
        current = self._base
        page = QWidget()
        form = QFormLayout(page)
        form.setHorizontalSpacing(10)
        form.setVerticalSpacing(8)

        self.style_combo = QComboBox()
        for label, style in SCALE_BAR_STYLES:
            self.style_combo.addItem(label, style)
        index = self.style_combo.findData(current.style)
        self.style_combo.setCurrentIndex(max(0, index))
        form.addRow("样式", self.style_combo)

        self.unit_combo = QComboBox()
        self.unit_combo.addItem("米（m）", "m")
        self.unit_combo.addItem("千米（km）", "km")
        self.unit_combo.setCurrentIndex(max(0, self.unit_combo.findData(current.unit)))
        form.addRow("单位", self.unit_combo)

        self.segments_spin = _int_spin(1, 20, current.segments)
        form.addRow("分段数", self.segments_spin)

        self.segments_left_spin = _int_spin(0, 10, current.segments_left)
        form.addRow("左侧分段", self.segments_left_spin)

        hint = QLabel("分段数是主刻度数量；左侧分段是零点左侧的细分数。")
        hint.setWordWrap(True)
        hint.setEnabled(False)
        form.addRow("", hint)
        return page

    def _build_symbol_tab(self) -> QWidget:
        current = self._base
        page = QWidget()
        form = QFormLayout(page)
        form.setHorizontalSpacing(10)
        form.setVerticalSpacing(8)

        self.height_spin = _double_spin(0.5, 20.0, current.height_mm, " mm", decimals=1, step=0.5)
        form.addRow("高度", self.height_spin)

        self.line_width_spin = _double_spin(0.0, 5.0, current.line_width_mm, " mm", decimals=2, step=0.1)
        form.addRow("线宽", self.line_width_spin)

        self.line_color_btn = ColorButton(QColor(current.line_color))
        form.addRow("线颜色", self.line_color_btn)
        self.fill_color_btn = ColorButton(QColor(current.fill_color))
        form.addRow("填充色 1", self.fill_color_btn)
        self.fill_color2_btn = ColorButton(QColor(current.fill_color2))
        form.addRow("填充色 2", self.fill_color2_btn)

        hint = QLabel("双线框 / 阶梯线会同时用到两种填充色；空心框只描边。")
        hint.setWordWrap(True)
        hint.setEnabled(False)
        form.addRow("", hint)
        return page

    def _build_text_tab(self) -> QWidget:
        current = self._base
        page = QWidget()
        form = QFormLayout(page)
        form.setHorizontalSpacing(10)
        form.setVerticalSpacing(8)

        self.font_combo = QFontComboBox()
        if current.font_family:
            self.font_combo.setCurrentFont(QFont(current.font_family))
        form.addRow("字体", self.font_combo)

        self.font_size_spin = _int_spin(6, 48, round(current.font_size), " pt")
        form.addRow("字号", self.font_size_spin)

        self.font_color_btn = ColorButton(QColor(current.font_color))
        form.addRow("文字颜色", self.font_color_btn)

        self.label_vertical_combo = QComboBox()
        for label, key in SCALE_BAR_LABEL_VERTICAL:
            self.label_vertical_combo.addItem(label, key)
        self.label_vertical_combo.setCurrentIndex(max(0, self.label_vertical_combo.findData(current.label_vertical)))
        form.addRow("数字位置", self.label_vertical_combo)

        self.label_horizontal_combo = QComboBox()
        for label, key in SCALE_BAR_LABEL_HORIZONTAL:
            self.label_horizontal_combo.addItem(label, key)
        self.label_horizontal_combo.setCurrentIndex(
            max(0, self.label_horizontal_combo.findData(current.label_horizontal))
        )
        form.addRow("数字对齐", self.label_horizontal_combo)
        return page

    def values(self) -> ScaleBarStyle:
        return replace(
            self._base,
            style=str(self.style_combo.currentData() or DEFAULT_SCALE_BAR_STYLE),
            unit=str(self.unit_combo.currentData() or "m"),
            font_family=self.font_combo.currentFont().family(),
            segments=int(self.segments_spin.value()),
            segments_left=int(self.segments_left_spin.value()),
            font_size=float(self.font_size_spin.value()),
            font_color=color_text(self.font_color_btn.color()),
            line_color=color_text(self.line_color_btn.color()),
            line_width_mm=self.line_width_spin.value(),
            fill_color=color_text(self.fill_color_btn.color()),
            fill_color2=color_text(self.fill_color2_btn.color()),
            height_mm=self.height_spin.value(),
            label_vertical=str(self.label_vertical_combo.currentData() or "above"),
            label_horizontal=str(self.label_horizontal_combo.currentData() or "edge"),
        )


class _MapPropertiesDialog(BaseDialog):
    """地图框属性：边框 / 背景 / 网格 / 当前比例尺。"""

    def __init__(self, parent, map_item: QgsLayoutItemMap) -> None:
        super().__init__(parent, title="地图框属性", show_min_button=False, show_max_button=False)
        self.setMinimumSize(0, 0)
        self.resize(700, 600)
        self._base = read_map_frame_style(map_item)
        enabled = bool(self._base.frame_enabled)
        self._decoration = self._base.decoration if enabled else "none"
        self._decoration_width_mm = float(self._base.decoration_width_mm) if enabled else 0.0
        self._preset_buttons: dict[str, QToolButton] = {}

        tabs = QTabWidget()
        tabs.addTab(self._build_frame_tab(), "边框与背景")
        tabs.addTab(self._build_grid_tab(map_item), "网格")

        layout = QVBoxLayout()
        layout.setSpacing(8)
        layout.addWidget(tabs, 1)
        hint = QLabel("地图内容跟随主窗口的当前视图与图层；拖动地图框四周的把手可改大小。")
        hint.setWordWrap(True)
        hint.setEnabled(False)
        layout.addWidget(hint)
        layout.addLayout(_dialog_buttons(self))
        self.setLayout(layout)
        self._sync_preset_selection()

    def _build_frame_presets(self) -> QWidget:
        """图框预设缩略图（ArcMap 数据框 Border 样式列表）。"""
        group = QGroupBox("图框样式预设")
        grid = QGridLayout(group)
        grid.setContentsMargins(8, 6, 8, 6)
        grid.setHorizontalSpacing(4)
        grid.setVerticalSpacing(4)
        self._preset_group = QButtonGroup(self)
        self._preset_group.setExclusive(True)
        for index, preset in enumerate(MAP_FRAME_PRESETS):
            button = QToolButton()
            button.setObjectName("FramePresetButton")
            button.setCheckable(True)
            button.setAutoRaise(True)
            button.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextUnderIcon)
            button.setIcon(_frame_preset_icon(preset))
            button.setIconSize(QSize(76, 46))
            button.setText(preset.label)
            button.setToolTip(f"点击套用预设：{preset.label}")
            button.setFixedSize(90, 74)
            button.clicked.connect(
                lambda _checked=False, key=preset.key: self._apply_preset(key)
            )
            self._preset_group.addButton(button)
            self._preset_buttons[preset.key] = button
            grid.addWidget(button, index // 6, index % 6)
        return group

    def _build_frame_tab(self) -> QWidget:
        current = self._base
        page = QWidget()
        outer = QVBoxLayout(page)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(8)
        outer.addWidget(self._build_frame_presets())

        form = QFormLayout()
        form.setHorizontalSpacing(10)
        form.setVerticalSpacing(8)

        self.frame_check = QCheckBox("显示图框边框（含装饰）")
        self.frame_check.setChecked(current.frame_enabled)
        form.addRow("边框", self.frame_check)

        self.frame_color_btn = ColorButton(QColor(current.frame_color))
        form.addRow("边框颜色", self.frame_color_btn)

        self.frame_width_spin = _double_spin(0.0, 5.0, current.frame_width_mm, " mm", decimals=2, step=0.1)
        form.addRow("线宽", self.frame_width_spin)

        self.frame_join_combo = QComboBox()
        for label, key in FRAME_JOIN_STYLES:
            self.frame_join_combo.addItem(label, key)
        self.frame_join_combo.setCurrentIndex(max(0, self.frame_join_combo.findData(current.frame_join)))
        form.addRow("拐角样式", self.frame_join_combo)

        self.background_check = QCheckBox("显示背景")
        self.background_check.setChecked(current.background_enabled)
        form.addRow("背景", self.background_check)

        self.background_color_btn = ColorButton(QColor(current.background_color))
        form.addRow("背景颜色", self.background_color_btn)
        outer.addLayout(form)
        outer.addStretch(1)

        self.frame_check.toggled.connect(self.frame_color_btn.setEnabled)
        self.frame_check.toggled.connect(self.frame_width_spin.setEnabled)
        self.frame_check.toggled.connect(self.frame_join_combo.setEnabled)
        self.frame_check.toggled.connect(self._on_frame_toggled)
        self.frame_color_btn.color_changed.connect(self._on_frame_edited)
        self.frame_width_spin.valueChanged.connect(self._on_frame_edited)
        self.frame_join_combo.currentIndexChanged.connect(self._on_frame_edited)
        self.background_check.toggled.connect(self.background_color_btn.setEnabled)
        self.frame_color_btn.setEnabled(self.frame_check.isChecked())
        self.frame_width_spin.setEnabled(self.frame_check.isChecked())
        self.frame_join_combo.setEnabled(self.frame_check.isChecked())
        self.background_color_btn.setEnabled(self.background_check.isChecked())
        return page

    def _frame_style(self) -> MapFrameStyle:
        """当前控件值 → 样式对象；关掉边框时装饰一并清空。"""
        enabled = self.frame_check.isChecked()
        return replace(
            self._base,
            frame_enabled=enabled,
            frame_color=color_text(self.frame_color_btn.color()),
            frame_width_mm=self.frame_width_spin.value(),
            frame_join=str(self.frame_join_combo.currentData() or "miter"),
            background_enabled=self.background_check.isChecked(),
            background_color=color_text(self.background_color_btn.color()),
            decoration=self._decoration if enabled else "none",
            decoration_width_mm=self._decoration_width_mm if enabled else 0.0,
        )

    def _apply_preset(self, key: str) -> None:
        preset = find_map_frame_preset(key)
        if preset is None:
            return
        style = apply_map_frame_preset(self._frame_style(), preset)
        self._decoration = style.decoration
        self._decoration_width_mm = style.decoration_width_mm
        self.frame_check.setChecked(style.frame_enabled)
        self.frame_color_btn.set_color(QColor(style.frame_color))
        self.frame_width_spin.setValue(style.frame_width_mm)
        self.frame_join_combo.setCurrentIndex(
            max(0, self.frame_join_combo.findData(style.frame_join))
        )
        self._sync_preset_selection()

    def _on_frame_toggled(self, checked: bool) -> None:
        if not checked:
            self._decoration = "none"
            self._decoration_width_mm = 0.0
        self._sync_preset_selection()

    def _on_frame_edited(self, *_args) -> None:
        self._sync_preset_selection()

    def _sync_preset_selection(self) -> None:
        """按当前控件值高亮匹配的预设；自定义时全部取消选中。"""
        if not hasattr(self, "_preset_group"):
            return
        key = match_map_frame_preset(self._frame_style())
        button = self._preset_buttons.get(key)
        if button is not None:
            if not button.isChecked():
                button.setChecked(True)
            return
        self._preset_group.setExclusive(False)
        for candidate in self._preset_group.buttons():
            candidate.setChecked(False)
        self._preset_group.setExclusive(True)


    def _build_grid_tab(self, map_item: QgsLayoutItemMap) -> QWidget:
        page = QWidget()
        form = QFormLayout(page)
        form.setHorizontalSpacing(10)
        form.setVerticalSpacing(8)

        grid_enabled, grid_interval = grid_state(map_item)
        self.grid_check = QCheckBox("显示网格（图廓刻度与标注）")
        self.grid_check.setChecked(grid_enabled)
        form.addRow("网格", self.grid_check)

        # 上限不能写死：全球范围 / 大坐标系的自动间隔可能远超 100 万，
        # QDoubleSpinBox 会把读到的值夹到上限，点确定后把网格间隔悄悄改小。
        interval_max = max(1_000_000.0, float(grid_interval) * 10.0)
        self.interval_spin = _double_spin(0.0, interval_max, grid_interval, "", decimals=4)
        self.interval_spin.setSingleStep(1.0)
        self.interval_spin.setSpecialValueText("自动")
        form.addRow("网格间隔", self.interval_spin)

        scale = map_scale(map_item)
        scale_text = f"1:{int(scale):,}" if scale > 0 else "—"
        form.addRow("当前比例尺", QLabel(scale_text))

        self.grid_check.toggled.connect(self.interval_spin.setEnabled)
        self.interval_spin.setEnabled(self.grid_check.isChecked())
        return page

    def values(self) -> tuple[MapFrameStyle, bool, float]:
        return (
            self._frame_style(),
            self.grid_check.isChecked(),
            self.interval_spin.value(),
        )


class _PicturePropertiesDialog(BaseDialog):
    """图片属性：图片文件、宽度（指北针走 :class:`_NorthArrowPropertiesDialog`）。"""

    def __init__(self, parent, picture: QgsLayoutItemPicture) -> None:
        super().__init__(parent, title="图片属性", show_min_button=False, show_max_button=False)
        self.setMinimumSize(0, 0)
        self.resize(540, 260)
        self._path = picture.picturePath()
        layout = QVBoxLayout()
        layout.setSpacing(8)
        form = QFormLayout()
        form.setHorizontalSpacing(10)
        form.setVerticalSpacing(8)

        path_row = QWidget()
        path_layout = QHBoxLayout(path_row)
        path_layout.setContentsMargins(0, 0, 0, 0)
        path_layout.setSpacing(6)
        self.path_label = QLabel(self._path)
        self.path_label.setWordWrap(True)
        path_layout.addWidget(self.path_label, 1)
        browse = QPushButton("更换图片…")
        browse.clicked.connect(self._on_browse)
        path_layout.addWidget(browse)
        form.addRow("图片", path_row)

        self.width_spin = QDoubleSpinBox()
        self.width_spin.setRange(5.0, 500.0)
        self.width_spin.setDecimals(1)
        self.width_spin.setSuffix(" mm")
        self.width_spin.setValue(max(5.0, _item_size_mm(picture)[0]))
        form.addRow("宽度", self.width_spin)
        layout.addLayout(form)
        layout.addStretch(1)
        layout.addLayout(_dialog_buttons(self))
        self.setLayout(layout)

    def _on_browse(self) -> None:
        path, _selected = QFileDialog.getOpenFileName(
            self,
            "选择图片",
            self._path,
            "图片 (*.png *.jpg *.jpeg *.bmp *.tif *.tiff *.svg);;所有文件 (*)",
        )
        if path:
            self._path = path
            self.path_label.setText(path)

    def values(self) -> tuple[str, float]:
        return self._path, self.width_spin.value()


class _PageSetupDialog(BaseDialog):
    """页面设置：纸张、方向、自定义尺寸（ArcMap 的 Page and Print Setup）。"""

    def __init__(self, parent, settings: CartographySettings) -> None:
        super().__init__(parent, title="页面设置", show_min_button=False, show_max_button=False)
        self.setMinimumSize(0, 0)
        self.resize(520, 360)
        layout = QVBoxLayout()
        layout.setSpacing(8)
        form = QFormLayout()
        form.setHorizontalSpacing(10)
        form.setVerticalSpacing(8)

        self.paper_combo = QComboBox()
        for name in PAGE_SIZES:
            self.paper_combo.addItem(name, name)
        self.paper_combo.addItem(CUSTOM_PAPER, CUSTOM_PAPER)
        index = self.paper_combo.findData(settings.paper)
        self.paper_combo.setCurrentIndex(max(0, index))
        form.addRow("纸张", self.paper_combo)

        self.orientation_combo = QComboBox()
        self.orientation_combo.addItem("纵向", "portrait")
        self.orientation_combo.addItem("横向", "landscape")
        index = self.orientation_combo.findData(settings.orientation)
        self.orientation_combo.setCurrentIndex(max(0, index))
        form.addRow("方向", self.orientation_combo)

        self.width_spin = QDoubleSpinBox()
        self.width_spin.setRange(10.0, 5000.0)
        self.width_spin.setDecimals(1)
        self.width_spin.setSuffix(" mm")
        self.width_spin.setValue(max(10.0, float(settings.custom_width_mm)))
        form.addRow("自定义宽", self.width_spin)

        self.height_spin = QDoubleSpinBox()
        self.height_spin.setRange(10.0, 5000.0)
        self.height_spin.setDecimals(1)
        self.height_spin.setSuffix(" mm")
        self.height_spin.setValue(max(10.0, float(settings.custom_height_mm)))
        form.addRow("自定义高", self.height_spin)

        self.size_label = QLabel()
        form.addRow("页面尺寸", self.size_label)
        layout.addLayout(form)
        hint = QLabel("改纸张后页面上的图元位置不变，需要时拖动重新摆放。")
        hint.setWordWrap(True)
        hint.setEnabled(False)
        layout.addWidget(hint)
        layout.addStretch(1)
        layout.addLayout(_dialog_buttons(self))
        self.setLayout(layout)

        self.paper_combo.currentIndexChanged.connect(self._sync_state)
        self.orientation_combo.currentIndexChanged.connect(self._sync_state)
        self.width_spin.valueChanged.connect(self._sync_size_label)
        self.height_spin.valueChanged.connect(self._sync_size_label)
        self._sync_state()

    def _sync_state(self, *_args) -> None:
        custom = self.paper_combo.currentData() == CUSTOM_PAPER
        self.width_spin.setEnabled(custom)
        self.height_spin.setEnabled(custom)
        self._sync_size_label()

    def _sync_size_label(self, *_args) -> None:
        width, height = page_size_mm(self.settings())
        self.size_label.setText(f"{width:.1f} × {height:.1f} mm")

    def settings(self) -> CartographySettings:
        return CartographySettings(
            paper=str(self.paper_combo.currentData() or "A4"),
            orientation=str(self.orientation_combo.currentData() or "portrait"),
            custom_width_mm=self.width_spin.value(),
            custom_height_mm=self.height_spin.value(),
        )


class _ExportMapDialog(BaseDialog):
    """导出地图（ArcMap 的 Export Map）：格式、路径、分辨率。"""

    def __init__(
        self,
        parent,
        *,
        default_path: str,
        dpi: int = 300,
        open_after: bool = True,
    ) -> None:
        super().__init__(parent, title="导出地图", show_min_button=False, show_max_button=False)
        self.setMinimumSize(0, 0)
        self.resize(620, 340)
        self._path = default_path

        layout = QVBoxLayout()
        layout.setSpacing(8)
        form = QFormLayout()
        form.setHorizontalSpacing(10)
        form.setVerticalSpacing(8)

        self.format_combo = QComboBox()
        for key, (_extension, _filter, note) in OUTPUT_FORMATS.items():
            self.format_combo.addItem(f"{key.upper()}  {note}", key)
        form.addRow("格式", self.format_combo)

        path_row = QWidget()
        path_layout = QHBoxLayout(path_row)
        path_layout.setContentsMargins(0, 0, 0, 0)
        path_layout.setSpacing(6)
        self.path_edit = QLineEdit(default_path)
        path_layout.addWidget(self.path_edit, 1)
        browse = QPushButton("浏览…")
        browse.clicked.connect(self._on_browse)
        path_layout.addWidget(browse)
        form.addRow("输出路径", path_row)

        self.dpi_spin = QSpinBox()
        self.dpi_spin.setRange(72, 1200)
        self.dpi_spin.setSingleStep(50)
        self.dpi_spin.setSuffix(" dpi")
        self.dpi_spin.setValue(int(dpi))
        form.addRow("分辨率", self.dpi_spin)

        self.open_check = QCheckBox("导出后打开文件")
        self.open_check.setChecked(bool(open_after))
        form.addRow("", self.open_check)

        self.note_label = QLabel(output_note(str(self.format_combo.currentData() or "pdf")))
        self.note_label.setWordWrap(True)
        self.note_label.setEnabled(False)
        form.addRow("", self.note_label)
        layout.addLayout(form)
        layout.addStretch(1)
        layout.addLayout(_dialog_buttons(self, ok_text="导出", on_ok=self._validate))
        self.setLayout(layout)

        self.format_combo.currentIndexChanged.connect(self._on_format_changed)

    def _on_format_changed(self, *_args) -> None:
        output_format = str(self.format_combo.currentData() or "pdf")
        expected = output_extension(output_format)
        current = self.path_edit.text().strip()
        if current:
            root, suffix = os.path.splitext(current)
            if suffix.lower() != expected:
                self.path_edit.setText(root + expected)
        self._path = self.path_edit.text().strip() or self._path
        self.note_label.setText(output_note(output_format))

    def _on_browse(self) -> None:
        output_format = str(self.format_combo.currentData() or "pdf")
        current = self.path_edit.text().strip()
        directory = str(Path(current).parent) if current else ""
        path, _selected = QFileDialog.getSaveFileName(
            self,
            "导出地图",
            current,
            output_filter(output_format),
        )
        if path:
            self.path_edit.setText(path)
        elif directory:
            logger.debug("未选择导出路径（默认目录 %s）", directory)

    def _validate(self) -> None:
        output_format = str(self.format_combo.currentData() or "pdf")
        path = self.path_edit.text().strip()
        if not path:
            MessageBox.warning(self, "导出地图", "请选择输出文件。")
            return
        expected = output_extension(output_format)
        if Path(path).suffix.lower() != expected:
            path = os.path.splitext(path)[0] + expected
            self.path_edit.setText(path)
        if Path(path).exists():
            answer = MessageBox.question(
                self,
                "覆盖文件",
                f"文件已存在，是否覆盖？\n{path}",
                MessageBox.StandardButton.Yes | MessageBox.StandardButton.No,
                MessageBox.StandardButton.No,
            )
            if answer != MessageBox.StandardButton.Yes:
                return
        self._path = path
        self.accept()

    def values(self) -> tuple[str, int, bool]:
        # 以输入框为准：_validate 里会把补全扩展名后的路径写回，直接调用 values
        # （如测试或后续复用）时也不能返回过期路径。
        return self.path_edit.text().strip() or self._path, self.dpi_spin.value(), self.open_check.isChecked()


def _dialog_buttons(
    dialog: BaseDialog,
    *,
    ok_text: str = "确定",
    on_ok=None,
) -> QHBoxLayout:
    """属性 / 设置对话框统一的「取消 · 确定」页脚。"""
    footer = QHBoxLayout()
    footer.addStretch(1)
    cancel = QPushButton("取消")
    cancel.clicked.connect(dialog.reject)
    footer.addWidget(cancel)
    ok = QPushButton(ok_text)
    ok.setDefault(True)
    ok.clicked.connect(on_ok if on_ok is not None else dialog.accept)
    footer.addWidget(ok)
    return footer


__all__ = ["CartographyDialog"]
