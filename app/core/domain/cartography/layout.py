"""基于 :class:`QgsPrintLayout` 的版面组装、整饰元素插入与输出。

交互式「制图出图」窗口（:mod:`app.ui.dialogs.cartography`）在
:class:`QgsLayoutView` 里直接编辑这里创建的版面，交互对齐 ArcMap 的布局视图：

- 打开时先有「页面 + 地图框」，地图框跟随主窗口画布的范围与图层；
- 图名 / 文字 / 图例 / 比例尺 / 指北针 / 图片按需插入，插入后由用户在版面上
  拖动、缩放（选中工具由窗口负责），双击改属性；
- 最后整版导出（PDF / PNG / JPEG / TIFF）或打印。

本模块只负责「造图元 + 量尺寸 + 输出」，不碰 Qt 窗口控件，便于无头测试。
版面不注册进工程的 layoutManager，避免每次开窗口往工程里塞一个「制图出图」版面。
"""

from __future__ import annotations

import contextlib
import math
import os
import warnings
from dataclasses import replace
from pathlib import Path
from typing import TYPE_CHECKING

from qgis.core import (
    Qgis,
    QgsApplication,
    QgsLayoutExporter,
    QgsLayoutItem,
    QgsLayoutItemLabel,
    QgsLayoutItemLegend,
    QgsLayoutItemMap,
    QgsLayoutItemMapGrid,
    QgsLayoutItemPicture,
    QgsLayoutItemScaleBar,
    QgsLayoutMeasurement,
    QgsLayoutPoint,
    QgsLayoutSize,
    QgsPrintLayout,
    QgsRectangle,
    QgsScaleBarSettings,
    QgsTextFormat,
    QgsUnitTypes,
)
from qgis.PyQt.QtCore import QSize, Qt
from qgis.PyQt.QtGui import QColor, QFont, QImageReader

from app.core.domain.cartography.settings import (
    DEFAULT_NORTH_ARROW_STYLE,
    DEFAULT_SCALE_BAR_STYLE,
    NORTH_ARROW_STYLES,
    LegendStyle,
    MapFrameStyle,
    NorthArrowStyle,
    ScaleBarStyle,
    fit_extent_to_aspect,
    nice_grid_interval,
)
from app.infra.logging import get_logger

if TYPE_CHECKING:
    from qgis.core import QgsMapLayer, QgsProject
    from qgis.PyQt.QtPrintSupport import QPrinter

logger = get_logger(__name__)

_MM = QgsUnitTypes.LayoutUnit.LayoutMillimeters

#: 初始地图框与页面边缘的间距（毫米）。
DEFAULT_INSET_MM = 10.0
#: 插入图名 / 文字的默认字号（pt）。
DEFAULT_FONT_SIZE_PT = 22
#: 插入指北针的默认宽度（毫米）。
DEFAULT_NORTH_ARROW_MM = NorthArrowStyle().width_mm
#: 插入图片的默认宽度（毫米，高度按图片比例）。
DEFAULT_PICTURE_WIDTH_MM = 40.0

#: 图例首次绘制前量不出尺寸时的兜底大小（毫米）。
_FALLBACK_LEGEND_MM = (50.0, 30.0)
#: 比例尺量不出尺寸时的兜底大小（毫米）。
_FALLBACK_SCALEBAR_MM = (50.0, 12.0)

#: 输出结果码 → 中文说明。
_EXPORT_ERRORS: dict[int, str] = {
    1: "导出已取消",
    2: "可用内存不足，请降低输出分辨率",
    3: "无法写入目标文件，请检查路径与权限",
    4: "打印失败",
    5: "部分图层（SVG）渲染失败",
    6: "逐要素导出失败",
}


# ============================================================ 字体 / 尺寸小工具


def _font(family: str, point_size: float) -> QFont:
    font = QFont(family) if family else QFont()
    font.setPointSizeF(max(4.0, float(point_size)))
    return font


def _text_format(family: str, point_size: float) -> QgsTextFormat:
    fmt = QgsTextFormat()
    fmt.setFont(_font(family, point_size))
    fmt.setSize(float(point_size))
    fmt.setSizeUnit(QgsUnitTypes.RenderUnit.Points)
    return fmt


def _item_size_mm(item) -> tuple[float, float]:
    size = item.sizeWithUnits()
    return float(size.width()), float(size.height())


def color_text(color: QColor) -> str:
    """``QColor`` → 样式字符串；不透明用 ``#RRGGBB``，带透明度用 ``#AARRGGBB``。

    样式参数（:class:`ScaleBarStyle` 等）用纯字符串保存，颜色按钮里选的
    「无颜色」（alpha=0）必须原样带过去，否则透明背景会被当成不透明黑色。
    """
    if color.alpha() == 255:
        return color.name()
    return color.name(QColor.NameFormat.HexArgb)


def page_size(layout: QgsPrintLayout) -> tuple[float, float]:
    """版面第 1 页的宽高（毫米）。"""
    size = layout.pageCollection().page(0).sizeWithUnits()
    return float(size.width()), float(size.height())


def north_arrow_svg_path(style: str) -> str:
    """在 QGIS 安装目录里找指定样式的内置指北针 SVG；找不到返回空串。"""
    name = Path(str(style or DEFAULT_NORTH_ARROW_STYLE)).name
    for base in QgsApplication.svgPaths():
        candidate = Path(base) / "arrows" / name
        if candidate.is_file():
            return str(candidate)
    return ""


def is_north_arrow(picture: QgsLayoutItemPicture) -> bool:
    """图元是不是内置指北针（插入图片也可能用了 SVG，属性对话框要区分）。"""
    name = Path(picture.picturePath()).name
    return any(name == style for _label, style in NORTH_ARROW_STYLES)


# ============================================================ 版面 / 地图框


def create_layout(project: QgsProject, width_mm: float, height_mm: float) -> QgsPrintLayout:
    """建一个只有一页、指定纸张大小的空版面。"""
    layout = QgsPrintLayout(project)
    layout.initializeDefaults()
    layout.pageCollection().page(0).setPageSize(QgsLayoutSize(float(width_mm), float(height_mm), _MM))
    return layout


def set_map_extent(map_item: QgsLayoutItemMap, extent: QgsRectangle) -> None:
    """把地图范围按地图框宽高比居中扩展后应用，避免地物被拉伸。"""
    if extent is None or extent.isEmpty():
        return
    width, height = _item_size_mm(map_item)
    if width <= 0 or height <= 0:
        return
    xmin, ymin, xmax, ymax = fit_extent_to_aspect(
        (extent.xMinimum(), extent.yMinimum(), extent.xMaximum(), extent.yMaximum()),
        width / height,
    )
    map_item.setExtent(QgsRectangle(xmin, ymin, xmax, ymax))
    # 装饰刻度 / 斑马纹的间隔按地图单位换算，范围变了必须重算，
    # 否则缩放后刻度会越画越密（斑马纹最后糊成黑边）。
    _refresh_decoration_intervals(map_item)


def _sync_map_crs(map_item: QgsLayoutItemMap, layers: list[QgsMapLayer]) -> None:
    """把地图框 CRS 设为第一个有效图层的 CRS。

    QgsLayoutItemMap 新建后的 CRS 是 Unknown，``scale()`` 会按未知单位换算，
    实测 1:52 万的地图读成 1:324 亿；属性对话框的「当前比例尺」也跟着错。
    与 QGIS 版面设计器一致，显式跟随图层 CRS。
    """
    for layer in layers:
        if layer is None or not layer.isValid():
            continue
        crs = layer.crs()
        if crs.isValid():
            map_item.setCrs(crs)
            return


def create_map_frame(
    layout: QgsPrintLayout,
    layers: list[QgsMapLayer],
    extent: QgsRectangle | None,
    *,
    inset_mm: float = DEFAULT_INSET_MM,
) -> QgsLayoutItemMap:
    """在页面上建地图框（ArcMap 的数据框）：铺满页边距内区域并套用当前范围。"""
    page_w, page_h = page_size(layout)
    inset = max(0.0, float(inset_mm))
    map_item = QgsLayoutItemMap(layout)
    # 装饰网格的间隔按页面毫米换算成地图单位，范围或框尺寸一变（画布联动、
    # 拖手柄改大小）就得重算。连接放在建图元处只连一次；没有装饰网格时
    # 回调是空操作。个别构建没有该信号时静默跳过。
    with contextlib.suppress(AttributeError):  # pragma: no cover - 仅旧构建兜底
        map_item.extentChanged.connect(lambda item=map_item: _refresh_decoration_intervals(item))
    # 注意：只 setRect 不会刷新 QgsLayoutItem 的尺寸缓存（sizeWithUnits 仍为 0），
    # 后续按宽高比套范围会直接返回；必须走 attemptMove/attemptResize。
    layout.addLayoutItem(map_item)
    map_item.attemptMove(QgsLayoutPoint(inset, inset, _MM))
    map_item.attemptResize(
        QgsLayoutSize(
            max(10.0, page_w - 2 * inset),
            max(10.0, page_h - 2 * inset),
            _MM,
        )
    )
    layer_list = list(layers)
    map_item.setLayers(layer_list)
    _sync_map_crs(map_item, layer_list)
    apply_map_frame_style(map_item, MapFrameStyle())
    if extent is not None:
        set_map_extent(map_item, extent)
    return map_item


def sync_map_frame(map_item: QgsLayoutItemMap, layers: list[QgsMapLayer], extent: QgsRectangle | None) -> None:
    """让地图框跟随主画布：图层集合 + 地图范围。"""
    layer_list = list(layers)
    map_item.setLayers(layer_list)
    _sync_map_crs(map_item, layer_list)
    if extent is not None:
        set_map_extent(map_item, extent)
    map_item.update()


def fit_map_frame_to_page(map_item: QgsLayoutItemMap, *, inset_mm: float = DEFAULT_INSET_MM) -> None:
    """把地图框重新铺满当前页面的页边距内区域（换纸张 / 方向后调用）。

    尺寸变化会让 QGIS 按原比例尺自动伸缩范围，这里再把原范围的宽高比套回去，
    保证内容不被拉伸。
    """
    layout = map_item.layout()
    if layout is None:
        return
    extent = QgsRectangle(map_item.extent())
    page_w, page_h = page_size(layout)
    inset = max(0.0, float(inset_mm))
    map_item.attemptMove(QgsLayoutPoint(inset, inset, _MM))
    map_item.attemptResize(
        QgsLayoutSize(
            max(10.0, page_w - 2 * inset),
            max(10.0, page_h - 2 * inset),
            _MM,
        )
    )
    if not extent.isEmpty():
        set_map_extent(map_item, extent)
    else:
        _refresh_decoration_intervals(map_item)
    map_item.update()


def map_scale(map_item: QgsLayoutItemMap) -> float:
    """地图框当前比例尺分母（1:N 的 N）；量不出时返回 0。"""
    try:
        return float(map_item.scale())
    except Exception:  # noqa: BLE001 —— 图元已随版面销毁时只关心「量不出」
        return 0.0


#: 边框连接样式键 → Qt 枚举。Qt 的取值是位标志（Miter=0 / Bevel=0x40 /
#: Round=0x80），不能按下标猜，必须显式映射。PyQt6 的 Qt 枚举是 enum.Enum，
#: 不能用 ``int()`` 转（会 TypeError），要取 ``.value``；PyQt5 直接就是 int。
_FRAME_JOIN_TO_QT = {
    "miter": Qt.PenJoinStyle.MiterJoin,
    "round": Qt.PenJoinStyle.RoundJoin,
    "bevel": Qt.PenJoinStyle.BevelJoin,
}


def _qt_enum_value(value) -> int:
    return int(getattr(value, "value", value))


_FRAME_JOIN_KEYS = {_qt_enum_value(value): key for key, value in _FRAME_JOIN_TO_QT.items()}

#: 网格栈里的两种内部网格：图廓网格（用户开关的网格 / 标注）与图框装饰。
_ANNOTATION_GRID_NAME = "图廓网格"
_DECORATION_GRID_NAME = "图框装饰"

#: 图框装饰键 → QgsLayoutItemMapGrid 的边框样式。
#: 「双线框」没有原生样式：QGIS 的 LineBorder 只有一条线、且紧贴地图框，
#: 会与地图框自身的描边完全重合（4.2 实测）。这里用 Zebra + 全透明填充 +
#: 整边一个间隔，只留下斑马纹的内外两条边框线，效果就是双线框。
_DECORATION_FRAME_STYLES = {
    "line": QgsLayoutItemMapGrid.Zebra,
    "ticks_out": QgsLayoutItemMapGrid.ExteriorTicks,
    "ticks_in": QgsLayoutItemMapGrid.InteriorTicks,
    "ticks_both": QgsLayoutItemMapGrid.InteriorExteriorTicks,
    "zebra": QgsLayoutItemMapGrid.Zebra,
}
_FRAME_STYLE_TO_DECORATION = {
    _qt_enum_value(style): key for key, style in _DECORATION_FRAME_STYLES.items() if key not in ("line", "zebra")
}

#: 双线框的识别约定：Zebra 且两个填充色全透明。见 :func:`_decoration_from_grid`。
_TRANSPARENT_FILL = QColor(0, 0, 0, 0)


def _decoration_from_grid(grid: QgsLayoutItemMapGrid) -> str:
    """从网格边框层反推装饰键；Zebra 按填充色区分双线框与斑马纹。"""
    style = _qt_enum_value(grid.frameStyle())
    if style == _qt_enum_value(QgsLayoutItemMapGrid.Zebra):
        if grid.frameFillColor1().alpha() == 0 and grid.frameFillColor2().alpha() == 0:
            return "line"
        return "zebra"
    return _FRAME_STYLE_TO_DECORATION.get(style, "none")


def _grid_by_name(map_item: QgsLayoutItemMap, name: str) -> QgsLayoutItemMapGrid | None:
    """按名字找地图框网格（网格栈没有按名字索引的公开接口）。"""
    stack = map_item.grids()
    for index in range(stack.size()):
        grid = stack.grid(index)
        if grid is not None and grid.name() == name:
            return grid
    return None


def _remove_grid(map_item: QgsLayoutItemMap, grid: QgsLayoutItemMapGrid) -> None:
    """从网格栈移除并销毁网格。

    QGIS 4.2 的 ``removeGrid`` 改成按网格 id（字符串）移除，3.40 仍按对象；
    两种签名都试一次，调用方无需关心运行版本。
    """
    stack = map_item.grids()
    try:
        stack.removeGrid(grid)
    except TypeError:
        stack.removeGrid(grid.id())
    map_item.updateBoundingRect()


def _decoration_grid(
    map_item: QgsLayoutItemMap,
    *,
    create: bool = False,
) -> QgsLayoutItemMapGrid | None:
    """取地图框的装饰网格；``create=True`` 时没有就新建。"""
    grid = _grid_by_name(map_item, _DECORATION_GRID_NAME)
    if grid is not None or not create:
        return grid
    grid = QgsLayoutItemMapGrid(_DECORATION_GRID_NAME, map_item)
    map_item.grids().addGrid(grid)
    map_item.updateBoundingRect()
    return grid


def _decoration_interval(value: float, length_mm: float) -> float:
    """装饰刻度 / 斑马纹在一条边上的间距：约每 10 mm 一格，取整到 1/2/5。"""
    if value <= 0 or length_mm <= 0:
        return 0.0
    divisions = max(2, min(24, round(length_mm / 10.0)))
    return nice_grid_interval(value, 0.0, target_divisions=divisions)


def _refresh_decoration_intervals(map_item: QgsLayoutItemMap, key: str | None = None) -> None:
    """重算装饰网格的间隔，保持刻度 / 斑马纹在页面上的密度。

    间隔以地图单位计，套用范围后地图范围一变就得跟着重算，否则缩放后
    刻度会越来越密、斑马纹最后糊成黑边。``key`` 省略时从网格自身反推。
    """
    grid = _decoration_grid(map_item)
    if grid is None or not grid.enabled():
        return
    extent = map_item.extent()
    if extent.width() <= 0 or extent.height() <= 0:
        return
    if key is None:
        key = _decoration_from_grid(grid)
    if key == "line":
        # 双线框靠「整边一个分段」只留下内外两条线，间隔必须与范围一致。
        grid.setIntervalX(extent.width())
        grid.setIntervalY(extent.height())
    elif key in ("ticks_out", "ticks_in", "ticks_both", "zebra"):
        size = map_item.sizeWithUnits()
        grid.setIntervalX(_decoration_interval(extent.width(), size.width()))
        grid.setIntervalY(_decoration_interval(extent.height(), size.height()))


def _apply_decoration_grid(
    map_item: QgsLayoutItemMap,
    options: MapFrameStyle,
    key: str,
) -> None:
    """把双线 / 刻度 / 斑马纹装饰写到隐藏网格的边框层上。

    装饰沿用地图框自己的颜色与线宽，``decoration_width_mm`` 决定装饰带宽度
    （双线外框距离、刻度长度、斑马纹带宽）。网格本体只画边框，不画网格线：
    ``FrameAnnotationsOnly`` + 关闭标注。
    """
    grid = _decoration_grid(map_item, create=True)
    if grid is None:
        return
    width = max(0.2, float(options.decoration_width_mm))
    grid.setEnabled(True)
    grid.setCrs(map_item.crs())
    grid.setUnits(QgsLayoutItemMapGrid.MapUnit)
    grid.setStyle(QgsLayoutItemMapGrid.FrameAnnotationsOnly)
    grid.setAnnotationEnabled(False)
    grid.setFrameStyle(_DECORATION_FRAME_STYLES[key])
    grid.setFrameWidth(width)
    grid.setFramePenSize(max(0.1, float(options.frame_width_mm)))
    grid.setFramePenColor(QColor(options.frame_color))
    if key == "line":
        # 双线框：透明填充 + 整边一个分段，斑马纹只剩内外两条边框线。
        grid.setFrameFillColor1(_TRANSPARENT_FILL)
        grid.setFrameFillColor2(_TRANSPARENT_FILL)
    elif key == "zebra":
        # Zebra 的两个填充色交替出现；第二个用纸面白，黑/白相间最清楚。
        grid.setFrameFillColor1(QColor(options.frame_color))
        grid.setFrameFillColor2(QColor("#ffffff"))
    # 间隔按当前范围换算；key 显式传入，免得靠填充色反推。
    _refresh_decoration_intervals(map_item, key)
    map_item.updateBoundingRect()


def read_map_frame_style(map_item: QgsLayoutItemMap) -> MapFrameStyle:
    """读取地图框当前的边框 / 背景样式（含装饰边框）。"""
    grid = _decoration_grid(map_item)
    decoration = "none"
    decoration_width = 0.0
    if grid is not None and bool(map_item.frameEnabled()) and grid.enabled():
        decoration = _decoration_from_grid(grid)
        if decoration != "none":
            decoration_width = float(grid.frameWidth())
    return MapFrameStyle(
        frame_enabled=bool(map_item.frameEnabled()),
        frame_color=color_text(map_item.frameStrokeColor()),
        frame_width_mm=float(map_item.frameStrokeWidth().length()),
        frame_join=_FRAME_JOIN_KEYS.get(_qt_enum_value(map_item.frameJoinStyle()), "miter"),
        background_enabled=bool(map_item.hasBackground()),
        background_color=color_text(map_item.backgroundColor()),
        decoration=decoration,
        decoration_width_mm=decoration_width,
    )


def apply_map_frame_style(map_item: QgsLayoutItemMap, options: MapFrameStyle) -> None:
    """套用地图框边框 / 背景样式（ArcMap 数据框的 Frame 设置）。"""
    map_item.setFrameEnabled(bool(options.frame_enabled))
    map_item.setFrameStrokeColor(QColor(options.frame_color))
    map_item.setFrameStrokeWidth(QgsLayoutMeasurement(max(0.0, float(options.frame_width_mm)), _MM))
    map_item.setFrameJoinStyle(_FRAME_JOIN_TO_QT.get(options.frame_join, Qt.PenJoinStyle.MiterJoin))
    map_item.setBackgroundEnabled(bool(options.background_enabled))
    map_item.setBackgroundColor(QColor(options.background_color))

    decoration = options.decoration if options.frame_enabled else "none"
    if decoration in _DECORATION_FRAME_STYLES:
        _apply_decoration_grid(map_item, options, decoration)
    else:
        grid = _decoration_grid(map_item)
        if grid is not None:
            _remove_grid(map_item, grid)
    map_item.update()


# ============================================================ 整饰元素


def add_label(
    layout: QgsPrintLayout,
    text: str,
    *,
    font_family: str = "",
    point_size: float = DEFAULT_FONT_SIZE_PT,
    h_align: Qt.AlignmentFlag = Qt.AlignmentFlag.AlignLeft,
    max_width_mm: float | None = None,
) -> QgsLayoutItemLabel:
    """插入图名 / 文字标签（尺寸先按文字内容量出，之后用户可拖动缩放）。"""
    label = QgsLayoutItemLabel(layout)
    label.setText(text)
    label.setTextFormat(_text_format(font_family, point_size))
    label.setHAlign(h_align)
    label.setVAlign(Qt.AlignmentFlag.AlignVCenter)
    layout.addLayoutItem(label)
    label.adjustSizeToText()
    width, height = _item_size_mm(label)
    width = max(12.0, width + 2.0)
    if max_width_mm is not None:
        width = min(width, float(max_width_mm))
    label.attemptResize(QgsLayoutSize(width, max(6.0, height), _MM))
    return label


def add_legend(
    layout: QgsPrintLayout,
    map_item: QgsLayoutItemMap,
    *,
    title: str = "图例",
    font_family: str = "",
) -> QgsLayoutItemLegend:
    """插入图例并链接到地图框。"""
    legend = QgsLayoutItemLegend(layout)
    legend.setLinkedMap(map_item)
    if hasattr(legend, "setLegendFilterByMapEnabled"):
        legend.setLegendFilterByMapEnabled(True)
    layout.addLayoutItem(legend)
    apply_legend_style(legend, title, LegendStyle(font_family=font_family))
    _ensure_legend_size(layout, legend)
    return legend


def _ensure_legend_size(layout: QgsPrintLayout, legend: QgsLayoutItemLegend) -> None:
    """图例在首次绘制前尺寸为 0：先低分辨率渲染一次版面，再量真实尺寸。"""
    width, height = _item_size_mm(legend)
    if width > 0 and height > 0:
        return
    page_w, page_h = page_size(layout)
    try:
        QgsLayoutExporter(layout).renderPageToImage(0, QSize(320, max(120, round(320 * page_h / page_w))))
    except Exception:  # 预渲染失败不阻断插入，按兜底尺寸继续
        logger.warning("图例预渲染失败，按默认尺寸插入", exc_info=True)
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", DeprecationWarning)
        legend.updateLegend()
    legend.adjustBoxSize()
    width, height = _item_size_mm(legend)
    if width <= 0 or height <= 0:
        legend.attemptResize(QgsLayoutSize(_FALLBACK_LEGEND_MM[0], _FALLBACK_LEGEND_MM[1], _MM))


def add_scale_bar(
    layout: QgsPrintLayout,
    map_item: QgsLayoutItemMap,
    *,
    style: str = DEFAULT_SCALE_BAR_STYLE,
    unit: str = "m",
    font_family: str = "",
) -> QgsLayoutItemScaleBar:
    """插入比例尺并链接到地图框。"""
    bar = QgsLayoutItemScaleBar(layout)
    bar.setLinkedMap(map_item)
    layout.addLayoutItem(bar)
    apply_scale_bar_style(
        bar,
        replace(ScaleBarStyle(), style=style, unit=unit, font_family=font_family),
    )
    width, height = _item_size_mm(bar)
    if width <= 0 or height <= 0:
        bar.attemptResize(QgsLayoutSize(_FALLBACK_SCALEBAR_MM[0], _FALLBACK_SCALEBAR_MM[1], _MM))
    return bar


def add_north_arrow(
    layout: QgsPrintLayout,
    *,
    style: str = DEFAULT_NORTH_ARROW_STYLE,
    size_mm: float = DEFAULT_NORTH_ARROW_MM,
    fill_color: str = "#000000",
    rotation: float = 0.0,
) -> QgsLayoutItemPicture | None:
    """插入内置指北针；QGIS 安装目录里没有对应 SVG 时返回 ``None``。"""
    svg_path = north_arrow_svg_path(style)
    if not svg_path:
        logger.warning("未找到内置指北针 SVG（%s），跳过指北针", style)
        return None
    picture = QgsLayoutItemPicture(layout)
    picture.setPicturePath(svg_path)
    picture.setResizeMode(QgsLayoutItemPicture.Zoom)
    layout.addLayoutItem(picture)
    apply_north_arrow_style(
        picture,
        NorthArrowStyle(
            style=style,
            width_mm=size_mm,
            rotation=rotation,
            fill_color=fill_color,
        ),
    )
    return picture


def add_picture(
    layout: QgsPrintLayout,
    path: str,
    *,
    width_mm: float = DEFAULT_PICTURE_WIDTH_MM,
) -> QgsLayoutItemPicture:
    """插入图片（SVG / 位图），按原图比例给出默认高度。"""
    picture = QgsLayoutItemPicture(layout)
    picture.setPicturePath(str(path))
    picture.setResizeMode(QgsLayoutItemPicture.Zoom)
    layout.addLayoutItem(picture)
    width = max(5.0, float(width_mm))
    aspect = _picture_aspect(path)
    picture.attemptResize(QgsLayoutSize(width, max(5.0, width / aspect), _MM))
    return picture


def _picture_aspect(path: str) -> float:
    """图片宽高比（宽/高）；读不出来按 1:1。"""
    suffix = Path(path).suffix.lower()
    try:
        if suffix == ".svg":
            from qgis.PyQt.QtSvg import QSvgRenderer  # noqa: PLC0415

            size = QSvgRenderer(path).defaultSize()
            if size.width() > 0 and size.height() > 0:
                return size.width() / size.height()
        else:
            # 只读文件头拿宽高；QImage(path) 会整幅解码，几十兆的扫描图
            # 每次改宽度都要在 GUI 线程卡住好几秒。
            size = QImageReader(path).size()
            if size.isValid() and size.height() > 0:
                return size.width() / size.height()
    except Exception:  # 读不到尺寸就用方形，图元仍可插入
        logger.debug("读取图片尺寸失败：%s", path, exc_info=True)
    return 1.0


def resize_picture(picture: QgsLayoutItemPicture, width_mm: float, path: str = "") -> None:
    """按图片原始比例把图元设成指定宽度（编辑图片属性时用）。"""
    if path:
        picture.setPicturePath(path)
        picture.setResizeMode(QgsLayoutItemPicture.Zoom)
    width = max(5.0, float(width_mm))
    aspect = _picture_aspect(picture.picturePath())
    picture.attemptResize(QgsLayoutSize(width, max(5.0, width / aspect), _MM))


def read_north_arrow_style(picture: QgsLayoutItemPicture) -> NorthArrowStyle:
    """读取指北针图元的样式参数（非内置指北针按默认样式返回）。"""
    name = Path(picture.picturePath()).name
    known = any(name == style for _label, style in NORTH_ARROW_STYLES)
    width, _height = _item_size_mm(picture)
    return NorthArrowStyle(
        style=name if known else DEFAULT_NORTH_ARROW_STYLE,
        width_mm=width if width > 0 else DEFAULT_NORTH_ARROW_MM,
        rotation=float(picture.pictureRotation()),
        fill_color=color_text(picture.svgFillColor()),
    )


def apply_north_arrow_style(picture: QgsLayoutItemPicture, options: NorthArrowStyle) -> None:
    """套用指北针样式：SVG、填充色、旋转与宽度（高度按原图比例）。"""
    path = north_arrow_svg_path(options.style)
    if path:
        picture.setPicturePath(path)
        picture.setResizeMode(QgsLayoutItemPicture.Zoom)
    # 旋转绕图元中心，拖动把手缩放时指针不会甩出框外
    picture.setPictureAnchor(QgsLayoutItem.ReferencePoint.Middle)
    picture.setSvgFillColor(QColor(options.fill_color))
    picture.setPictureRotation(float(options.rotation))
    width = max(6.0, float(options.width_mm))
    aspect = _picture_aspect(path or picture.picturePath())
    picture.attemptResize(QgsLayoutSize(width, max(6.0, width / aspect), _MM))
    picture.update()


# ============================================================ 属性编辑


def apply_label_properties(
    label: QgsLayoutItemLabel,
    text: str,
    *,
    font_family: str = "",
    point_size: float = DEFAULT_FONT_SIZE_PT,
    h_align: Qt.AlignmentFlag = Qt.AlignmentFlag.AlignLeft,
) -> None:
    """改图名 / 文字的内容与字体，宽度沿用用户拖出来的宽度，高度按新排版重算。"""
    width, _height = _item_size_mm(label)
    label.setText(text)
    label.setTextFormat(_text_format(font_family, point_size))
    label.setHAlign(h_align)
    label.adjustSizeToText()
    measured_width, measured_height = _item_size_mm(label)
    # 保留用户拖宽的宽度，但文字变长时自动加宽，避免内容被裁掉
    label.attemptResize(QgsLayoutSize(max(12.0, width, measured_width), max(6.0, measured_height), _MM))
    label.update()


def read_legend_style(legend: QgsLayoutItemLegend) -> LegendStyle:
    """读取图例当前的字体 / 符号 / 边框 / 背景样式。"""
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", DeprecationWarning)
        title_font = legend.styleFont(Qgis.LegendComponent.Title)
        item_font = legend.styleFont(Qgis.LegendComponent.SymbolLabel)
        return LegendStyle(
            font_family=title_font.family(),
            title_size=float(title_font.pointSizeF()) if title_font.pointSizeF() > 0 else 10.0,
            item_size=float(item_font.pointSizeF()) if item_font.pointSizeF() > 0 else 9.0,
            font_color=color_text(legend.fontColor()),
            columns=max(1, int(legend.columnCount())),
            symbol_width_mm=float(legend.symbolWidth()),
            symbol_height_mm=float(legend.symbolHeight()),
            line_spacing=float(legend.lineSpacing()),
            background_enabled=bool(legend.hasBackground()),
            background_color=color_text(legend.backgroundColor()),
            frame_enabled=bool(legend.frameEnabled()),
            frame_color=color_text(legend.frameStrokeColor()),
            frame_width_mm=float(legend.frameStrokeWidth().length()),
        )


def apply_legend_style(legend: QgsLayoutItemLegend, title: str, options: LegendStyle) -> None:
    """套用图例的标题、字体、符号尺寸、列数、边框与背景。"""
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", DeprecationWarning)
        legend.setTitle(title)
        legend.setStyleFont(Qgis.LegendComponent.Title, _font(options.font_family, options.title_size))
        legend.setStyleFont(Qgis.LegendComponent.SymbolLabel, _font(options.font_family, options.item_size))
        # 4.2 里 setFontColor 只接受一个全局 QColor；分组件颜色没有 Python API
        legend.setFontColor(QColor(options.font_color))
        legend.setColumnCount(max(1, int(options.columns)))
        legend.setSymbolWidth(max(0.0, float(options.symbol_width_mm)))
        legend.setSymbolHeight(max(0.0, float(options.symbol_height_mm)))
        legend.setLineSpacing(max(0.0, float(options.line_spacing)))
        legend.setBackgroundEnabled(bool(options.background_enabled))
        legend.setBackgroundColor(QColor(options.background_color))
        legend.setFrameEnabled(bool(options.frame_enabled))
        legend.setFrameStrokeColor(QColor(options.frame_color))
        legend.setFrameStrokeWidth(QgsLayoutMeasurement(max(0.0, float(options.frame_width_mm)), _MM))
        legend.updateLegend()
    legend.adjustBoxSize()
    legend.update()


def apply_legend_properties(
    legend: QgsLayoutItemLegend,
    title: str,
    *,
    font_family: str = "",
    title_size: float = 10.0,
) -> None:
    """改图例标题与字号（兼容旧调用），并按新内容重新量尺寸。"""
    current = read_legend_style(legend)
    apply_legend_style(
        legend,
        title,
        replace(
            current,
            font_family=font_family or current.font_family,
            title_size=float(title_size),
            item_size=max(6.0, float(title_size) - 1.0),
        ),
    )


def _apply_scale_bar_unit(bar: QgsLayoutItemScaleBar, unit: str) -> None:
    """设置比例尺显示单位（段长仍是地图单位，渲染时换算）。

    ``setUnits`` 只改枚举、不会同步 ``mapUnitsPerScaleBarUnit``（4.2 实测
    仍为 1），换算系数必须手动给，否则 20 km 的比例尺会标成 20,000 km。
    """
    if unit == "km":
        bar.setUnits(QgsUnitTypes.DistanceUnit.DistanceKilometers)
        bar.setMapUnitsPerScaleBarUnit(1000.0)
        bar.setUnitLabel("km")
    else:
        bar.setUnits(QgsUnitTypes.DistanceUnit.DistanceMeters)
        bar.setMapUnitsPerScaleBarUnit(1.0)
        bar.setUnitLabel("m")


def _scale_bar_unit(bar: QgsLayoutItemScaleBar) -> str:
    if int(bar.units()) == int(QgsUnitTypes.DistanceUnit.DistanceKilometers):
        return "km"
    return "m"


def _label_vertical_key(value) -> str:
    # QgsScaleBarSettings.LabelVerticalPlacement：AboveSegment=0 / BelowSegment=1
    return "below" if int(value) == 1 else "above"


def _label_horizontal_key(value) -> str:
    # CenteredEdge=0 / CenteredSegment=1
    return "segment" if int(value) == 1 else "edge"


def _apply_scale_bar_label_placement(
    bar: QgsLayoutItemScaleBar,
    vertical: str,
    horizontal: str,
) -> None:
    """设置数字标注在分段的上/下方、居中于边缘还是分段。

    用 ``QgsScaleBarSettings`` 上的枚举（3.40 / 4.2 都认）；4.2 的
    ``Qgis.ScaleBarDistanceLabel*`` 是另一套别名，传错类型会直接 TypeError。
    """
    try:
        bar.setLabelVerticalPlacement(
            QgsScaleBarSettings.LabelVerticalPlacement.BelowSegment
            if vertical == "below"
            else QgsScaleBarSettings.LabelVerticalPlacement.AboveSegment
        )
        bar.setLabelHorizontalPlacement(
            QgsScaleBarSettings.LabelHorizontalPlacement.CenteredSegment
            if horizontal == "segment"
            else QgsScaleBarSettings.LabelHorizontalPlacement.CenteredEdge
        )
    except (AttributeError, TypeError):  # 标签位置属于可选整饰，失败不影响出图
        logger.warning("当前 QGIS 不支持比例尺标签位置设置，已跳过", exc_info=True)


def read_scale_bar_style(bar: QgsLayoutItemScaleBar) -> ScaleBarStyle:
    """读取比例尺当前的样式参数（颜色 / 分段 / 字号 / 标签位置）。"""
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", DeprecationWarning)
        font = bar.font()
        return ScaleBarStyle(
            style=str(bar.style() or DEFAULT_SCALE_BAR_STYLE),
            unit=_scale_bar_unit(bar),
            font_family=font.family(),
            segments=int(bar.numberOfSegments()),
            segments_left=int(bar.numberOfSegmentsLeft()),
            font_size=float(font.pointSizeF()) if font.pointSizeF() > 0 else 8.0,
            font_color=color_text(bar.fontColor()),
            line_color=color_text(bar.lineColor()),
            line_width_mm=float(bar.lineWidth()),
            fill_color=color_text(bar.fillColor()),
            fill_color2=color_text(bar.fillColor2()),
            height_mm=float(bar.height()),
            label_vertical=_label_vertical_key(bar.labelVerticalPlacement()),
            label_horizontal=_label_horizontal_key(bar.labelHorizontalPlacement()),
        )


def apply_scale_bar_style(
    bar: QgsLayoutItemScaleBar,
    options: ScaleBarStyle,
) -> None:
    """套用比例尺样式。

    顺序有讲究：``setHeight`` / 字号本身不会刷新图元方框，
    ``applyDefaultSize()`` 会按当前字号与高度重算方框，同时把分段数复位；
    所以先设字号 / 高度，再 applyDefaultSize，最后补分段数与单位。
    """
    bar.setStyle(options.style or DEFAULT_SCALE_BAR_STYLE)
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", DeprecationWarning)
        bar.setFont(_font(options.font_family or bar.font().family(), options.font_size))
        bar.setFontColor(QColor(options.font_color))
        bar.setFillColor(QColor(options.fill_color))
        bar.setFillColor2(QColor(options.fill_color2))
        bar.setLineColor(QColor(options.line_color))
        bar.setLineWidth(max(0.0, float(options.line_width_mm)))
        bar.setHeight(max(0.0, float(options.height_mm)))
    bar.applyDefaultSize()
    segments = max(1, int(options.segments))
    if int(bar.numberOfSegments()) != segments:
        bar.setNumberOfSegments(segments)
    segments_left = max(0, int(options.segments_left))
    if int(bar.numberOfSegmentsLeft()) != segments_left:
        bar.setNumberOfSegmentsLeft(segments_left)
    # 同上：applyDefaultSize 之后设单位，避免被重置
    _apply_scale_bar_unit(bar, options.unit)
    _apply_scale_bar_label_placement(bar, options.label_vertical, options.label_horizontal)
    bar.update()


def apply_scale_bar_properties(
    bar: QgsLayoutItemScaleBar,
    *,
    style: str = DEFAULT_SCALE_BAR_STYLE,
    unit: str = "m",
    font_family: str = "",
) -> None:
    """改比例尺样式 / 单位（兼容旧调用），并重新套用默认长度。"""
    current = read_scale_bar_style(bar)
    apply_scale_bar_style(
        bar,
        replace(
            current,
            style=style,
            unit=unit,
            font_family=font_family or current.font_family,
        ),
    )


# ============================================================ 网格


def _grid_decimals(interval: float) -> int:
    """按网格间隔量级取标注小数位：度坐标间隔 0.05 → 2 位，米坐标 500 → 0 位。"""
    if interval <= 0 or interval >= 1:
        return 0
    return min(6, max(0, -math.floor(math.log10(interval))))


def _annotation_grid(
    map_item: QgsLayoutItemMap,
    *,
    create: bool = False,
) -> QgsLayoutItemMapGrid | None:
    """取用户开关的图廓网格（标注网格）。

    不能再用 ``map_item.grid()`` 探测 / 取第一个网格：图框装饰也是网格，
    装饰先建、网格后开时第一个网格是装饰，会把标注写到装饰上。这里按名字
    找；本功能之前创建的网格没有名字，则把第一个非装饰网格认作标注网格。
    没有网格时**不要**调用 ``map_item.grid()``（它会新建并启用一个网格）。
    """
    grid = _grid_by_name(map_item, _ANNOTATION_GRID_NAME)
    if grid is not None:
        return grid
    stack = map_item.grids()
    for index in range(stack.size()):
        candidate = stack.grid(index)
        if candidate is None or candidate.name() == _DECORATION_GRID_NAME:
            continue
        candidate.setName(_ANNOTATION_GRID_NAME)
        return candidate
    if not create:
        return None
    if stack.size() == 0:
        grid = map_item.grid()
        map_item.updateBoundingRect()
    else:
        grid = QgsLayoutItemMapGrid(_ANNOTATION_GRID_NAME, map_item)
        stack.addGrid(grid)
        map_item.updateBoundingRect()
    if grid is not None:
        grid.setName(_ANNOTATION_GRID_NAME)
    return grid


def grid_state(map_item: QgsLayoutItemMap) -> tuple[bool, float]:
    """地图框网格状态：``(是否启用, 间隔)``；没有网格时返回 ``(False, 0.0)``。"""
    grid = _annotation_grid(map_item)
    if grid is None:
        return False, 0.0
    return bool(grid.enabled()), float(grid.intervalX())


def enable_grid(map_item: QgsLayoutItemMap, interval: float = 0.0) -> None:
    """打开地图框网格（公里网 / 地图单位网格）；``interval<=0`` 按范围自动取整。"""
    grid = _annotation_grid(map_item, create=True)
    if grid is None:
        return
    if interval <= 0:
        extent = map_item.extent()
        interval = nice_grid_interval(extent.width(), extent.height())
    if interval <= 0:
        return
    try:
        grid.setName(_ANNOTATION_GRID_NAME)
        grid.setEnabled(True)
        # QGIS 3.40 的网格枚举是 QgsLayoutItemMapGrid 上的扁平常量，4.2 才
        # 迁到 Qgis.*；扁平常量在两版都保留，统一用它以兼容双环境。
        grid.setUnits(QgsLayoutItemMapGrid.MapUnit)
        grid.setStyle(QgsLayoutItemMapGrid.FrameAnnotationsOnly)
        grid.setIntervalX(interval)
        grid.setIntervalY(interval)
        grid.setAnnotationEnabled(True)
        grid.setAnnotationFormat(QgsLayoutItemMapGrid.Decimal)
        grid.setAnnotationPrecision(_grid_decimals(interval))
        for side in (
            QgsLayoutItemMapGrid.Top,
            QgsLayoutItemMapGrid.Bottom,
            QgsLayoutItemMapGrid.Left,
            QgsLayoutItemMapGrid.Right,
        ):
            grid.setAnnotationPosition(QgsLayoutItemMapGrid.OutsideMapFrame, side)
        grid.setFrameStyle(QgsLayoutItemMapGrid.LineBorder)
        grid.setFramePenColor(QColor("#555555"))
        grid.setFramePenSize(0.2)
    except Exception:  # 网格属于可选整饰，失败不影响出图
        logger.warning("网格配置失败，已跳过", exc_info=True)
    map_item.updateBoundingRect()
    map_item.update()


def disable_grid(map_item: QgsLayoutItemMap) -> None:
    grid = _annotation_grid(map_item)
    if grid is None:
        return
    grid.setEnabled(False)
    map_item.updateBoundingRect()
    map_item.update()


# ============================================================ 输出


def export_layout(layout: QgsPrintLayout, path: str, *, dpi: int = 300) -> tuple[bool, str]:
    """按扩展名导出整版；返回 ``(是否成功, 面向用户的说明)``。

    先写同目录的临时文件、成功后再替换目标文件：导出失败（分辨率过高
    内存不足、磁盘写满等）时，用户之前导出的同名成果不会被提前删掉。
    顺带绕开 GDAL 的 PNG 驱动不支持「更新已有文件」的问题（直接写已有
    PNG 会打印 ERROR 6，虽然结果仍成功）。
    """
    target = Path(path)
    partial = target.with_name(f".{target.stem}.{os.getpid()}.part{target.suffix}")
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        partial.unlink(missing_ok=True)
    except OSError as exc:
        return False, f"无法创建输出目录：{exc}"

    exporter = QgsLayoutExporter(layout)
    try:
        if partial.suffix.lower() == ".pdf":
            settings = QgsLayoutExporter.PdfExportSettings()
            settings.dpi = int(dpi)
            result = exporter.exportToPdf(str(partial), settings)
        else:
            settings = QgsLayoutExporter.ImageExportSettings()
            settings.dpi = int(dpi)
            result = exporter.exportToImage(str(partial), settings)
    except Exception as exc:  # 导出失败要回传给界面，不能崩窗口
        logger.error("地图导出异常 path=%s", target, exc_info=True)
        _discard_partial(partial)
        return False, f"导出失败：{exc}"

    code = int(result)
    if code == int(QgsLayoutExporter.ExportResult.Success):
        try:
            os.replace(partial, target)
        except OSError as exc:
            _discard_partial(partial)
            return False, f"导出成功但无法覆盖目标文件（可能被其他程序占用）：{exc}"
        return True, str(target)
    _discard_partial(partial)
    detail = _EXPORT_ERRORS.get(code, f"未知错误（代码 {code}）")
    error_file = ""
    try:
        error_file = exporter.errorFile()
    except Exception:  # noqa: BLE001 —— 仅用于补充错误说明
        error_file = ""
    if error_file:
        detail = f"{detail}\n{error_file}"
    logger.warning("地图导出失败 code=%s path=%s", code, target)
    return False, detail


def _discard_partial(path: Path) -> None:
    """删掉导出失败留下的临时文件（删不掉也不影响主流程）。"""
    with contextlib.suppress(OSError):
        path.unlink()


def print_layout(layout: QgsPrintLayout, printer: QPrinter, *, dpi: int = 300) -> tuple[bool, str]:
    """把整版送到打印机。"""
    settings = QgsLayoutExporter.PrintExportSettings()
    settings.dpi = int(dpi)
    try:
        result = QgsLayoutExporter(layout).print(printer, settings)
    except Exception as exc:  # 打印失败回传界面
        logger.error("地图打印异常", exc_info=True)
        return False, f"打印失败：{exc}"
    code = int(result)
    if code == int(QgsLayoutExporter.ExportResult.Success):
        return True, ""
    return False, _EXPORT_ERRORS.get(code, f"打印失败（代码 {code}）")


__all__ = [
    "DEFAULT_FONT_SIZE_PT",
    "DEFAULT_INSET_MM",
    "DEFAULT_NORTH_ARROW_MM",
    "DEFAULT_PICTURE_WIDTH_MM",
    "add_label",
    "add_legend",
    "add_north_arrow",
    "add_picture",
    "add_scale_bar",
    "apply_label_properties",
    "apply_legend_properties",
    "apply_legend_style",
    "apply_map_frame_style",
    "apply_north_arrow_style",
    "apply_scale_bar_properties",
    "apply_scale_bar_style",
    "color_text",
    "create_layout",
    "create_map_frame",
    "disable_grid",
    "enable_grid",
    "export_layout",
    "fit_map_frame_to_page",
    "grid_state",
    "is_north_arrow",
    "map_scale",
    "north_arrow_svg_path",
    "page_size",
    "print_layout",
    "read_legend_style",
    "read_map_frame_style",
    "read_north_arrow_style",
    "read_scale_bar_style",
    "resize_picture",
    "set_map_extent",
    "sync_map_frame",
]
