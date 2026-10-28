"""制图出图参数与纯计算工具。

本模块刻意**不 import QGIS**：纸张尺寸、方向、文件名、范围比例适配等逻辑
都是纯函数，便于在无 QGIS 的环境里单测。真正依赖 ``qgis.core`` 的版面组装
在 :mod:`app.core.domain.cartography.layout`。

术语对齐 ArcMap 的出图流程：

- 页面设置：纸张 / 方向 / 页边距 / 输出分辨率；
- 图面整饰：图名 / 图例 / 比例尺 / 指北针 / 图框 / 网格；
- 输出：PDF（矢量）或 PNG / JPEG / TIFF（栅格）。
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass, replace
from datetime import datetime
from pathlib import Path

#: 常用纸张（纵向，宽 × 高，单位毫米）。键同时作为界面下拉项文本。
PAGE_SIZES: dict[str, tuple[float, float]] = {
    "A4": (210.0, 297.0),
    "A3": (297.0, 420.0),
    "A2": (420.0, 594.0),
    "A1": (594.0, 841.0),
    "A0": (841.0, 1189.0),
    "B5": (176.0, 250.0),
    "Letter": (215.9, 279.4),
}

#: 自定义纸张在下拉框里的键。
CUSTOM_PAPER = "自定义"

#: 方向键：``portrait`` 纵向 / ``landscape`` 横向。
ORIENTATIONS = ("portrait", "landscape")

#: 输出格式：键 → (扩展名, 文件选择器过滤器, 界面说明)。
OUTPUT_FORMATS: dict[str, tuple[str, str, str]] = {
    "pdf": (".pdf", "PDF 文档 (*.pdf)", "矢量 PDF，适合打印与归档"),
    "png": (".png", "PNG 图片 (*.png)", "无损栅格，适合插入文档/网页"),
    "jpg": (".jpg", "JPEG 图片 (*.jpg)", "有损压缩栅格，文件较小"),
    "tif": (".tif", "TIFF 图片 (*.tif)", "高质量栅格，适合后续制版"),
}

#: 比例尺候选：(界面名, QGIS 渲染器注册名)。
#: 注册名大小写并不统一（``stepped`` / ``hollow`` 全小写），必须逐字照抄；
#: 名字写错时 QGIS 不报错，只会退回默认的 Single Box。
SCALE_BAR_STYLES: tuple[tuple[str, str], ...] = (
    ("单线框", "Single Box"),
    ("双线框", "Double Box"),
    ("阶梯线", "stepped"),
    ("空心框", "hollow"),
    ("数值标注", "Numeric"),
    ("上刻度线", "Line Ticks Up"),
    ("中刻度线", "Line Ticks Middle"),
    ("下刻度线", "Line Ticks Down"),
)

#: 新建比例尺的默认样式。
DEFAULT_SCALE_BAR_STYLE = "Single Box"

#: 指北针候选：(界面名, QGIS 内置 SVG 文件名)，对应 <QGIS>/svg/arrows/。
NORTH_ARROW_STYLES: tuple[tuple[str, str], ...] = (
    ("圆圈指针", "NorthArrow_01.svg"),
    ("圆环箭头", "NorthArrow_02.svg"),
    ("菱形指针", "NorthArrow_03.svg"),
    ("方框箭头", "NorthArrow_04.svg"),
    ("八角星", "NorthArrow_05.svg"),
    ("四角星", "NorthArrow_06.svg"),
    ("十字罗盘", "NorthArrow_07.svg"),
    ("星杆（右）", "NorthArrow_08.svg"),
    ("星杆（左）", "NorthArrow_09.svg"),
    ("圆环双箭头", "NorthArrow_10.svg"),
    ("人字箭头", "NorthArrow_11.svg"),
)

#: 新建指北针的默认样式（与旧版固定使用 04 保持一致）。
DEFAULT_NORTH_ARROW_STYLE = "NorthArrow_04.svg"

#: 比例尺数字标注的位置：(界面名, 内部键)。
SCALE_BAR_LABEL_VERTICAL: tuple[tuple[str, str], ...] = (
    ("分段上方", "above"),
    ("分段下方", "below"),
)
SCALE_BAR_LABEL_HORIZONTAL: tuple[tuple[str, str], ...] = (
    ("居中于边缘", "edge"),
    ("居中于分段", "segment"),
)

#: 图框 / 图例边框的连接样式：(界面名, 内部键)。
FRAME_JOIN_STYLES: tuple[tuple[str, str], ...] = (
    ("斜接（尖角）", "miter"),
    ("圆角", "round"),
    ("平角", "bevel"),
)

#: 图框装饰：内部键 → 说明。装饰由地图网格的边框层绘制，跟着地图框自动
#: 移动 / 缩放；``none`` 表示只用地图框自身的描边。
MAP_FRAME_DECORATIONS: tuple[tuple[str, str], ...] = (
    ("none", "无装饰"),
    ("line", "外框线（双线）"),
    ("ticks_out", "外刻度线"),
    ("ticks_in", "内刻度线"),
    ("ticks_both", "内外刻度线"),
    ("zebra", "斑马纹"),
)

MAP_FRAME_DECORATION_KEYS = frozenset(key for key, _label in MAP_FRAME_DECORATIONS)


@dataclass(frozen=True)
class MapFramePreset:
    """ArcMap 数据框边框样式的预设（只描述边框，背景沿用当前设置）。"""

    key: str
    label: str
    frame_enabled: bool = True
    frame_color: str = "#000000"
    frame_width_mm: float = 0.3
    frame_join: str = "miter"
    decoration: str = "none"
    decoration_width_mm: float = 0.0


#: 图框预设（界面按 6 列缩略图展示）。默认的细线框与 :class:`MapFrameStyle`
#: 的出厂值一致，因此新建地图框打开属性对话框时会自动选中它。
MAP_FRAME_PRESETS: tuple[MapFramePreset, ...] = (
    MapFramePreset(
        "none",
        "无边框",
        frame_enabled=False,
        frame_color="#333333",
        frame_width_mm=0.35,
    ),
    MapFramePreset(
        "thin",
        "细线框",
        frame_color="#333333",
        frame_width_mm=0.35,
    ),
    MapFramePreset(
        "black",
        "黑色细线",
        frame_color="#000000",
        frame_width_mm=0.3,
    ),
    MapFramePreset(
        "medium",
        "黑色中线",
        frame_color="#000000",
        frame_width_mm=0.6,
    ),
    MapFramePreset(
        "thick",
        "黑色粗线",
        frame_color="#000000",
        frame_width_mm=1.0,
    ),
    MapFramePreset(
        "gray",
        "灰色细线",
        frame_color="#808080",
        frame_width_mm=0.3,
    ),
    MapFramePreset(
        "white",
        "白色粗线",
        frame_color="#ffffff",
        frame_width_mm=0.8,
    ),
    MapFramePreset(
        "double",
        "双线框",
        frame_color="#000000",
        frame_width_mm=0.25,
        decoration="line",
        decoration_width_mm=1.2,
    ),
    MapFramePreset(
        "ticks_out",
        "外刻度线",
        frame_color="#000000",
        frame_width_mm=0.3,
        decoration="ticks_out",
        decoration_width_mm=1.5,
    ),
    MapFramePreset(
        "ticks_in",
        "内刻度线",
        frame_color="#000000",
        frame_width_mm=0.3,
        decoration="ticks_in",
        decoration_width_mm=1.2,
    ),
    MapFramePreset(
        "ticks_both",
        "内外刻度",
        frame_color="#000000",
        frame_width_mm=0.3,
        decoration="ticks_both",
        decoration_width_mm=1.5,
    ),
    MapFramePreset(
        "zebra",
        "斑马纹框",
        frame_color="#000000",
        frame_width_mm=0.2,
        decoration="zebra",
        decoration_width_mm=2.0,
    ),
)


def find_map_frame_preset(key: str) -> MapFramePreset | None:
    """按内部键取图框预设；键不存在返回 ``None``。"""
    wanted = str(key or "").strip()
    for preset in MAP_FRAME_PRESETS:
        if preset.key == wanted:
            return preset
    return None


def match_map_frame_preset(style: MapFrameStyle) -> str:
    """返回与 ``style`` 完全匹配的预设键；自定义样式返回空串。

    只比较边框相关字段，背景不参与匹配：用户单独改背景颜色不应把图框
    预设切成「自定义」。线宽用 0.02 mm 容差，抵消控件两位小数与 QGIS
    内部浮点读回的微小误差。
    """
    for preset in MAP_FRAME_PRESETS:
        if style.frame_enabled != preset.frame_enabled:
            continue
        if not style.frame_enabled:
            # 关掉边框后颜色 / 线宽 / 拐角都不再显示，无边框只看装饰是否也关掉。
            if style.decoration == preset.decoration:
                return preset.key
            continue
        if (
            style.frame_color.lower() == preset.frame_color.lower()
            and abs(float(style.frame_width_mm) - preset.frame_width_mm) <= 0.02
            and style.frame_join == preset.frame_join
            and style.decoration == preset.decoration
            and abs(float(style.decoration_width_mm) - preset.decoration_width_mm) <= 0.05
        ):
            return preset.key
    return ""


def apply_map_frame_preset(style: MapFrameStyle, preset: MapFramePreset) -> MapFrameStyle:
    """把预设的边框字段套到 ``style`` 上，保留它的背景设置。"""
    return replace(
        style,
        frame_enabled=preset.frame_enabled,
        frame_color=preset.frame_color,
        frame_width_mm=preset.frame_width_mm,
        frame_join=preset.frame_join,
        decoration=preset.decoration if preset.frame_enabled else "none",
        decoration_width_mm=preset.decoration_width_mm if preset.frame_enabled else 0.0,
    )



@dataclass(frozen=True)
class NorthArrowStyle:
    """指北针样式参数（颜色用 ``#RRGGBB`` / ``#AARRGGBB`` 字符串，与 QGIS 解耦）。"""

    style: str = DEFAULT_NORTH_ARROW_STYLE
    width_mm: float = 14.0
    rotation: float = 0.0
    fill_color: str = "#000000"


@dataclass(frozen=True)
class ScaleBarStyle:
    """比例尺样式参数。

    字段与 QGIS 的 ``QgsLayoutItemScaleBar`` 一一对应；颜色用字符串表示，
    便于在无 QGIS 的单测里构造（只有 ``QColor(...)`` 时才需要 Qt）。
    """

    style: str = DEFAULT_SCALE_BAR_STYLE
    unit: str = "m"
    font_family: str = ""
    segments: int = 2
    segments_left: int = 0
    font_size: float = 8.0
    font_color: str = "#000000"
    line_color: str = "#000000"
    line_width_mm: float = 0.3
    fill_color: str = "#000000"
    fill_color2: str = "#ffffff"
    height_mm: float = 3.0
    label_vertical: str = "above"
    label_horizontal: str = "edge"


@dataclass(frozen=True)
class LegendStyle:
    """图例样式参数（标题内容不在这里，单独传给 apply 函数）。"""

    font_family: str = ""
    title_size: float = 10.0
    item_size: float = 9.0
    font_color: str = "#000000"
    columns: int = 1
    symbol_width_mm: float = 7.0
    symbol_height_mm: float = 4.0
    line_spacing: float = 1.0
    background_enabled: bool = True
    background_color: str = "#ffffff"
    frame_enabled: bool = False
    frame_color: str = "#000000"
    frame_width_mm: float = 0.3


@dataclass(frozen=True)
class MapFrameStyle:
    """地图框的边框 / 背景样式（ArcMap 数据框的 Frame 设置）。

    ``decoration`` 对应依附在地图框上的装饰边框（双线 / 刻度 / 斑马纹），
    由版面层的 ``QgsLayoutItemMapGrid`` 绘制；``decoration_width_mm`` 是装饰
    距离地图框边缘的宽度 / 刻度长度。取值为 :data:`MAP_FRAME_DECORATIONS` 的键。
    """

    frame_enabled: bool = True
    frame_color: str = "#333333"
    frame_width_mm: float = 0.35
    frame_join: str = "miter"
    background_enabled: bool = True
    background_color: str = "#ffffff"
    decoration: str = "none"
    decoration_width_mm: float = 0.0


@dataclass(frozen=True)
class CartographySettings:
    """版面的纸张参数。

    图面内容（图名 / 图例 / 比例尺 …）不在这里预置开关，而是在交互式版面视图里
    按 ArcMap 的「插入」流程逐个添加、拖动摆放，见
    :mod:`app.core.domain.cartography.layout`。
    """

    paper: str = "A4"
    orientation: str = "portrait"
    custom_width_mm: float = 210.0
    custom_height_mm: float = 297.0


def page_size_mm(settings: CartographySettings) -> tuple[float, float]:
    """按纸张 + 方向返回页面宽高（毫米）。

    自定义纸张取 ``custom_width_mm`` / ``custom_height_mm``；横向时交换宽高。
    任何非法尺寸都回退到 A4 纵向，避免生成 0 宽版面。
    """
    if settings.paper == CUSTOM_PAPER:
        width, height = settings.custom_width_mm, settings.custom_height_mm
    else:
        width, height = PAGE_SIZES.get(settings.paper, PAGE_SIZES["A4"])
    width = float(width)
    height = float(height)
    if not _is_positive_finite(width) or not _is_positive_finite(height):
        width, height = PAGE_SIZES["A4"]
    if settings.orientation == "landscape":
        width, height = height, width
    return width, height


def _is_positive_finite(value: float) -> bool:
    return math.isfinite(value) and value > 0


def output_extension(output_format: str) -> str:
    """输出格式对应的扩展名（含点）；未知格式回退 ``.pdf``。"""
    return OUTPUT_FORMATS.get(output_format, OUTPUT_FORMATS["pdf"])[0]


def output_filter(output_format: str) -> str:
    """文件选择器过滤器；未知格式回退 PDF。"""
    return OUTPUT_FORMATS.get(output_format, OUTPUT_FORMATS["pdf"])[1]


def output_note(output_format: str) -> str:
    """输出格式的一行说明。"""
    return OUTPUT_FORMATS.get(output_format, OUTPUT_FORMATS["pdf"])[2]


#: 文件名里不能出现的字符（Windows 全量 + 常见分隔符）。
_INVALID_FILENAME_CHARS = re.compile(r'[\\/:*?"<>|\r\n\t]+')


def safe_stem(text: str, fallback: str = "地图") -> str:
    """把图名压成安全的文件名主干（去非法字符、首尾空白/点）。"""
    cleaned = _INVALID_FILENAME_CHARS.sub("_", (text or "").strip())
    cleaned = cleaned.strip(" ._")
    return cleaned[:80] or fallback


def default_output_path(
    output_format: str,
    title: str = "",
    directory: str | Path | None = None,
    *,
    now: datetime | None = None,
) -> str:
    """生成默认输出路径：``<目录>/<图名>_<时间戳><扩展名>``。

    目录优先级：传入目录 → 用户文档目录 → 当前目录。
    """
    if directory is None:
        documents = Path.home() / "Documents"
        directory = documents if documents.is_dir() else Path.home()
    stamp = (now or datetime.now()).strftime("%Y%m%d_%H%M%S")
    stem = safe_stem(title)
    return str(Path(directory) / f"{stem}_{stamp}{output_extension(output_format)}")


def fit_extent_to_aspect(
    extent: tuple[float, float, float, float],
    aspect: float,
) -> tuple[float, float, float, float]:
    """把范围按目标宽高比（宽/高）居中扩展，避免地图拉伸变形。

    返回 ``(xmin, ymin, xmax, ymax)``。范围非法或 ``aspect`` 非法时原样返回。
    """
    xmin, ymin, xmax, ymax = (float(v) for v in extent)
    width = xmax - xmin
    height = ymax - ymin
    if not all(math.isfinite(v) for v in (width, height, aspect)):
        return xmin, ymin, xmax, ymax
    if width <= 0 or height <= 0 or aspect <= 0:
        return xmin, ymin, xmax, ymax
    center_x = (xmin + xmax) / 2.0
    center_y = (ymin + ymax) / 2.0
    current = width / height
    if current < aspect:
        width = height * aspect
    else:
        height = width / aspect
    return (
        center_x - width / 2.0,
        center_y - height / 2.0,
        center_x + width / 2.0,
        center_y + height / 2.0,
    )


def nice_grid_interval(
    width: float,
    height: float,
    *,
    target_divisions: int = 8,
) -> float:
    """按范围取一个「好看」的网格间隔（1/2/5 × 10^n）。

    接近 ``max(width, height) / target_divisions``，用于公里网 / 地图单位网格。
    """
    long_side = max(float(width), float(height))
    if not math.isfinite(long_side) or long_side <= 0:
        return 0.0
    target = long_side / max(1, target_divisions)
    exponent = math.floor(math.log10(target))
    base = 10.0**exponent
    for factor in (1.0, 2.0, 5.0, 10.0):
        if target <= factor * base:
            return factor * base
    return 10.0 * base


__all__ = [
    "CUSTOM_PAPER",
    "DEFAULT_NORTH_ARROW_STYLE",
    "DEFAULT_SCALE_BAR_STYLE",
    "FRAME_JOIN_STYLES",
    "MAP_FRAME_DECORATIONS",
    "MAP_FRAME_DECORATION_KEYS",
    "MAP_FRAME_PRESETS",
    "NORTH_ARROW_STYLES",
    "ORIENTATIONS",
    "OUTPUT_FORMATS",
    "PAGE_SIZES",
    "SCALE_BAR_LABEL_HORIZONTAL",
    "SCALE_BAR_LABEL_VERTICAL",
    "SCALE_BAR_STYLES",
    "CartographySettings",
    "LegendStyle",
    "MapFramePreset",
    "MapFrameStyle",
    "NorthArrowStyle",
    "ScaleBarStyle",
    "apply_map_frame_preset",
    "default_output_path",
    "find_map_frame_preset",
    "fit_extent_to_aspect",
    "match_map_frame_preset",
    "nice_grid_interval",
    "output_extension",
    "output_filter",
    "output_note",
    "page_size_mm",
    "safe_stem",
]
