"""把地图经离屏渲染导出为图片 / 矢量文件。

支持 **7 种格式**：

- 位图：PNG / JPEG / TIFF / WebP / BMP（``QImage`` + Qt 图片插件）
- 矢量：PDF / SVG（同一个地图渲染器直接画到 ``QPdfWriter`` / ``QSvgGenerator``）

导出不截屏：取画布当前的 :class:`QgsMapSettings`（图层、可见性、样式、工程
坐标系、标注 / 表达式变量都在里面），只替换范围、像素尺寸、背景色与 DPI，
画布上盖着的面板、浮动提示条、橡皮筋都不会被画进去。

**尺寸 / 比例尺 / 分辨率**：三者相互推算统一走 :class:`QgsScaleCalculator`
（地理坐标系按纬度换算），与 QGIS 自己算出的比例尺一致。比例尺指 1:N 的 N：
固定范围与 N 时，DPI 越高输出像素越多；固定像素尺寸与 DPI 时，像素越多比例尺
分母越小（图越"放大"）。

**背景透明**：只对支持 alpha 的格式开放（PNG / TIFF / WebP / SVG）；不支持的
格式传透明背景会直接报错，避免存出来一片黑底。

**地理参考**：位图格式本身不携带范围。勾选「导出后加载到地图」时会额外写
world file（像素大小 + 左上角坐标）与 ``.aux.xml``（CRS），GDAL / QGIS
重新打开图片即可按导出范围精确对位。
"""

from __future__ import annotations

import contextlib
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING
from xml.sax.saxutils import escape

from qgis.PyQt.QtCore import QSize
from qgis.PyQt.QtGui import QColor, QImage, QPainter

from app.infra.logging import get_logger

if TYPE_CHECKING:
    from qgis.core import (
        QgsCoordinateReferenceSystem,
        QgsMapRendererJob,
        QgsMapSettings,
        QgsProject,
        QgsRectangle,
        QgsScaleCalculator,
    )

logger = get_logger(__name__)

#: 单边像素上限（QImage / 显卡纹理的常见安全范围）
MAX_IMAGE_SIDE = 20000
#: 总像素上限：约 80MP，ARGB32 占内存约 320MB，再大容易渲染失败 / 卡死
MAX_IMAGE_PIXELS = 80_000_000
#: PDF 单页边长上限（PDF 规范 14400pt ≈ 5080mm），超出后写出的页面会失败
PDF_MAX_PAGE_MM = 5080.0


class ExportMapImageError(RuntimeError):
    """导出失败；消息可直接展示给用户。"""


@dataclass(frozen=True)
class MapImageFormat:
    """一种可保存的导出格式。"""

    key: str
    label: str
    #: 保存对话框里的扩展名（一个主扩展名）
    extension: str
    #: 渲染方式：``raster``（QImage）/ ``pdf``（QPdfWriter）/ ``svg``（QSvgGenerator）
    kind: str = "raster"
    #: ``QImage.save`` 的 format 参数（仅 raster）
    save_format: str = ""
    #: ``QFileDialog`` 过滤器里的通配（可含多个扩展名）
    patterns: tuple[str, ...] = ()
    #: 是否支持透明背景
    supports_transparency: bool = False
    #: JPEG / WebP 质量（1~100）；-1 表示用 Qt 默认
    quality: int = -1

    @property
    def filter(self) -> str:
        """文件对话框过滤器，如 ``PNG 图片 (*.png)``。"""
        patterns = self.patterns or (self.extension,)
        return f"{self.label} ({' '.join(f'*{p}' for p in patterns)})"


#: 导出对话框 / 过滤器共用的一份格式清单（顺序即下拉顺序）
MAP_IMAGE_FORMATS: tuple[MapImageFormat, ...] = (
    MapImageFormat(
        key="png",
        label="PNG 图片",
        extension=".png",
        save_format="PNG",
        supports_transparency=True,
    ),
    MapImageFormat(
        key="jpeg",
        label="JPEG 图片",
        extension=".jpg",
        save_format="JPEG",
        patterns=(".jpg", ".jpeg"),
        quality=92,
    ),
    MapImageFormat(
        key="tiff",
        label="TIFF 图片",
        extension=".tif",
        save_format="TIFF",
        patterns=(".tif", ".tiff"),
        supports_transparency=True,
    ),
    MapImageFormat(
        key="webp",
        label="WebP 图片",
        extension=".webp",
        save_format="WEBP",
        supports_transparency=True,
        quality=90,
    ),
    MapImageFormat(
        key="bmp",
        label="BMP 图片",
        extension=".bmp",
        save_format="BMP",
    ),
    MapImageFormat(
        key="pdf",
        label="PDF 文档",
        extension=".pdf",
        kind="pdf",
    ),
    MapImageFormat(
        key="svg",
        label="SVG 矢量图",
        extension=".svg",
        kind="svg",
        supports_transparency=True,
    ),
)

DEFAULT_MAP_IMAGE_FORMAT = MAP_IMAGE_FORMATS[0]


def format_from_path(path: str) -> MapImageFormat | None:
    """按扩展名找格式；不认识的扩展名返回 ``None``。"""
    extension = os.path.splitext(str(path or ""))[1].lower()
    if not extension:
        return None
    for fmt in MAP_IMAGE_FORMATS:
        if extension == fmt.extension or extension in fmt.patterns:
            return fmt
    return None


def ensure_extension(path: str, fmt: MapImageFormat) -> str:
    """把 ``path`` 的扩展名对齐到 ``fmt``。

    已有**本格式**扩展名时原样返回（``.jpeg`` 对 JPEG、``.tiff`` 对 TIFF 也算
    对）；已有其它导出格式的扩展名时替换；完全没扩展名（或不是已知导出扩展名）
    时追加。
    """
    path = str(path or "").strip()
    if not path:
        return path
    root, extension = os.path.splitext(path)
    extension = extension.lower()
    if extension == fmt.extension or extension in fmt.patterns:
        return path
    known = {f.extension for f in MAP_IMAGE_FORMATS}
    for other in MAP_IMAGE_FORMATS:
        known.update(other.patterns)
    if extension in known:
        return root + fmt.extension
    return path + fmt.extension


def pdf_page_size_mm(width: int, height: int, dpi: float) -> tuple[float, float]:
    """输出像素 + DPI → PDF 页面的物理尺寸（毫米）。"""
    dpi = max(1.0, float(dpi))
    return width / dpi * 25.4, height / dpi * 25.4


#: 栅格格式 → world file 扩展名（GDAL 按扩展名读取；不认识的用通用的 .wld）
_WORLD_FILE_EXTENSIONS = {
    ".png": ".pgw",
    ".jpg": ".jgw",
    ".jpeg": ".jgw",
    ".tif": ".tfw",
    ".tiff": ".tfw",
    ".webp": ".wld",
    ".bmp": ".wld",
}


def world_file_path(path: str) -> str:
    """导出图片对应的 world file 路径（保留原目录 / 文件名）。"""
    root, extension = os.path.splitext(str(path))
    return root + _WORLD_FILE_EXTENSIONS.get(extension.lower(), ".wld")


def write_georeference(
    path: str,
    extent: "QgsRectangle",
    width: int,
    height: int,
    crs: "QgsCoordinateReferenceSystem | None" = None,
) -> str:
    """给导出的位图写 world file（及 CRS 的 ``.aux.xml``），返回 world 文件路径。

    ``QImage.save`` 不会把范围写进 PNG / JPEG / BMP / WebP 等格式，缺少
    地理参考的图片再加载到地图时无法定位，看起来就像「导出范围不对」。
    world file 记录像素大小与左上角坐标（允许横纵像素不等，未锁定宽高比
    时也能精确覆盖输出范围）；``.aux.xml`` 只补坐标系（GDAL PAM）。
    """
    if width <= 0 or height <= 0:
        raise ExportMapImageError("写世界文件失败：输出像素尺寸无效。")
    if (
        extent is None
        or extent.isNull()
        or extent.width() <= 0
        or extent.height() <= 0
    ):
        raise ExportMapImageError("写世界文件失败：输出范围无效。")
    pixel_width = extent.width() / width
    pixel_height = extent.height() / height
    # 世界文件记录的是左上角像素的中心点
    center_x = extent.xMinimum() + pixel_width / 2.0
    center_y = extent.yMaximum() - pixel_height / 2.0
    target = world_file_path(path)
    Path(target).write_text(
        "\n".join(
            (
                f"{pixel_width:.12f}",
                "0.000000000000",
                "0.000000000000",
                f"{-pixel_height:.12f}",
                f"{center_x:.12f}",
                f"{center_y:.12f}",
                "",
            )
        ),
        encoding="ascii",
    )
    if crs is not None and crs.isValid():
        Path(f"{path}.aux.xml").write_text(
            "<PAMDataset>\n  <SRS>"
            + escape(crs.toWkt())
            + "</SRS>\n</PAMDataset>\n",
            encoding="utf-8",
        )
    return target


@dataclass(frozen=True)
class ExportMapSpec:
    """一次地图导出的参数（对话框收集，服务消费）。"""

    #: 输出文件完整路径（扩展名决定保存格式）
    path: str
    width: int
    height: int
    #: 输出范围（工程坐标系）
    extent: QgsRectangle
    #: 背景色；``alpha < 255`` 视作透明底
    background: QColor = field(default_factory=lambda: QColor(255, 255, 255))
    #: 输出 DPI（同时决定比例尺与符号 / 标注的物理尺寸）
    dpi: float = 96.0

    def validate(self) -> None:
        """校验参数；不合法时抛 :class:`ExportMapImageError`。"""
        if not str(self.path or "").strip():
            raise ExportMapImageError("请选择输出文件路径。")
        if self.width < 1 or self.height < 1:
            raise ExportMapImageError("输出宽度和高度都必须大于 0 像素。")
        if self.width > MAX_IMAGE_SIDE or self.height > MAX_IMAGE_SIDE:
            raise ExportMapImageError(
                f"输出尺寸过大：单边不能超过 {MAX_IMAGE_SIDE} 像素。"
            )
        pixels = self.width * self.height
        if pixels > MAX_IMAGE_PIXELS:
            raise ExportMapImageError(
                f"输出尺寸过大：{self.width} × {self.height} 约 "
                f"{pixels / 1_000_000:.1f} 百万像素，"
                f"上限约 {MAX_IMAGE_PIXELS / 1_000_000:.0f} 百万像素。"
            )
        if self.extent is None or self.extent.isNull():
            raise ExportMapImageError("输出范围无效，请先在地图上定位到要导出的区域。")
        if self.extent.width() <= 0 or self.extent.height() <= 0:
            raise ExportMapImageError("输出范围无效，请先在地图上定位到要导出的区域。")
        if self.dpi <= 0:
            raise ExportMapImageError("输出分辨率必须大于 0。")
        parent = os.path.dirname(os.path.abspath(self.path))
        if not os.path.isdir(parent):
            raise ExportMapImageError(f"输出目录不存在：\n{parent}")
        fmt = format_from_path(self.path)
        if fmt is None:
            raise ExportMapImageError(
                "不支持的导出格式，请使用 PNG、JPEG、TIFF、WebP、BMP、PDF 或 SVG。"
            )
        if self.background.alpha() < 255 and not fmt.supports_transparency:
            raise ExportMapImageError(
                f"{fmt.label} 不支持透明背景，请改用 PNG / TIFF / WebP / SVG "
                "或选择不透明背景。"
            )
        if fmt.kind == "pdf":
            page_width, page_height = pdf_page_size_mm(
                self.width, self.height, self.dpi
            )
            if max(page_width, page_height) > PDF_MAX_PAGE_MM:
                raise ExportMapImageError(
                    f"PDF 页面过大：{page_width:.0f} × {page_height:.0f} mm，"
                    f"单边不能超过 {PDF_MAX_PAGE_MM:.0f} mm。\n"
                    "请调小像素尺寸或提高分辨率。"
                )


# ---------------------------------------------------------------------------
# 比例尺 / 输出尺寸
# ---------------------------------------------------------------------------
def _scale_calculator(
    dpi: float, crs: QgsCoordinateReferenceSystem, project: QgsProject
) -> QgsScaleCalculator:
    """按工程坐标系单位创建比例尺计算器（地理坐标系会自动按纬度换算）。"""
    from qgis.core import QgsScaleCalculator, QgsUnitTypes

    valid_crs = crs is not None and crs.isValid()
    map_units = (
        crs.mapUnits() if valid_crs else QgsUnitTypes.DistanceUnit.Meters
    )
    calculator = QgsScaleCalculator(max(1.0, float(dpi)), map_units)
    with contextlib.suppress(Exception):
        ellipsoid = project.ellipsoid()
        if ellipsoid:
            calculator.setEllipsoid(ellipsoid)
    return calculator


def size_for_scale(
    extent: QgsRectangle,
    scale: float,
    dpi: float,
    crs: QgsCoordinateReferenceSystem,
    project: QgsProject | None = None,
) -> tuple[int, int]:
    """固定范围与比例尺 1:``scale``，按 ``dpi`` 算出输出像素尺寸。"""
    from qgis.core import QgsProject

    if (
        extent is None
        or extent.isNull()
        or extent.width() <= 0
        or extent.height() <= 0
        or scale <= 0
        or dpi <= 0
    ):
        return 0, 0
    try:
        calculator = _scale_calculator(dpi, crs, project or QgsProject.instance())
        size = calculator.calculateImageSize(extent, float(scale))
        width = max(1, int(round(size.width())))
        height = max(1, int(round(size.height())))
        return width, height
    except Exception as exc:  # noqa: BLE001 —— 比例尺计算失败不阻塞界面
        logger.warning("计算输出像素尺寸失败：%s", exc)
        return 0, 0


def scale_for_width(
    extent: QgsRectangle,
    width: int,
    dpi: float,
    crs: QgsCoordinateReferenceSystem,
    project: QgsProject | None = None,
) -> float:
    """固定范围与输出像素宽度，按 ``dpi`` 反算比例尺分母 N。"""
    from qgis.core import QgsProject

    if (
        extent is None
        or extent.isNull()
        or extent.width() <= 0
        or width <= 0
        or dpi <= 0
    ):
        return 0.0
    try:
        calculator = _scale_calculator(dpi, crs, project or QgsProject.instance())
        return float(calculator.calculate(extent, int(width)))
    except Exception as exc:  # noqa: BLE001 —— 比例尺计算失败不阻塞界面
        logger.warning("计算比例尺失败：%s", exc)
        return 0.0


# ---------------------------------------------------------------------------
# 渲染
# ---------------------------------------------------------------------------
def _format_errors(job: QgsMapRendererJob) -> str:
    """把渲染任务收集到的错误拼成一句可展示的话。"""
    messages = []
    for error in job.errors():
        text = getattr(error, "message", None) or str(error)
        if text:
            messages.append(str(text))
    return "；".join(messages)


def _prepare_map_settings(
    settings: QgsMapSettings,
    spec: ExportMapSpec,
    project: QgsProject | None,
    *,
    vector: bool = False,
) -> QgsMapSettings:
    """从画布设置拷贝一份导出用的地图设置（范围 / 尺寸 / DPI / 表达式变量）。"""
    from qgis.core import (
        QgsExpressionContext,
        QgsExpressionContextUtils,
        QgsMapSettings,
        QgsProject,
    )

    map_settings = QgsMapSettings(settings)
    map_settings.setExtent(spec.extent)
    map_settings.setOutputSize(QSize(int(spec.width), int(spec.height)))
    map_settings.setOutputDpi(float(spec.dpi))
    map_settings.setBackgroundColor(spec.background)
    # 导出要一次画完，不能让渲染器按"部分输出"提前返回（瓦片缓存命中时
    # 画布会这么优化，离屏导出必须关掉，否则图片可能缺一块）。
    with contextlib.suppress(Exception):
        map_settings.setFlag(QgsMapSettings.Flag.RenderPartialOutput, False)
    if vector:
        # PDF / SVG 尽量保留矢量图层，不因 painter 设备类型退化成整张位图
        with contextlib.suppress(Exception):
            map_settings.setFlag(QgsMapSettings.Flag.ForceVectorOutput, True)

    # 4.2 的 QgsRenderContext.fromMapSettings 不再自动填 @map_extent_* /
    # @project_* 变量，离屏渲染必须自己挂 global + project + mapSettings 作用域；
    # 缺了以后所有依赖表达式变量的样式 / 标注都会悄悄空掉。
    context = QgsExpressionContext()
    context.appendScope(QgsExpressionContextUtils.globalScope())
    context.appendScope(
        QgsExpressionContextUtils.projectScope(project or QgsProject.instance())
    )
    context.appendScope(QgsExpressionContextUtils.mapSettingsScope(map_settings))
    map_settings.setExpressionContext(context)
    return map_settings


def _render_with_painter(
    map_settings: QgsMapSettings, painter: QPainter
) -> None:
    """把地图画到已激活的 painter 上（位图 / PDF / SVG 共用）。"""
    from qgis.core import QgsMapRendererCustomPainterJob

    job = QgsMapRendererCustomPainterJob(map_settings, painter)
    job.start()
    job.waitForFinished()
    error_text = _format_errors(job)
    if error_text:
        raise ExportMapImageError(f"地图渲染失败：{error_text}")


def render_map_image(
    settings: QgsMapSettings,
    spec: ExportMapSpec,
    project: QgsProject | None = None,
) -> QImage:
    """按 ``spec`` 离屏渲染一张位图（不写盘）。"""
    map_settings = _prepare_map_settings(settings, spec, project)

    transparent = spec.background.alpha() < 255
    image_format = (
        QImage.Format.Format_ARGB32_Premultiplied
        if transparent
        else QImage.Format.Format_RGB32
    )
    image = QImage(QSize(int(spec.width), int(spec.height)), image_format)
    image.fill(spec.background)
    if image.isNull():
        raise ExportMapImageError(
            f"无法创建 {spec.width} × {spec.height} 的图片，内存不足。"
        )
    # 把 DPI 写进图片元数据（PNG pHYs / JPEG JFIF / TIFF 分辨率）：
    # 不写的话图片默认按 96dpi 解释，300dpi 导出的图打印出来会小 3 倍。
    dots_per_meter = int(round(float(spec.dpi) / 0.0254))
    image.setDotsPerMeterX(dots_per_meter)
    image.setDotsPerMeterY(dots_per_meter)

    painter = QPainter(image)
    try:
        _render_with_painter(map_settings, painter)
    finally:
        painter.end()
    return image


def _export_svg(
    settings: QgsMapSettings,
    spec: ExportMapSpec,
    project: QgsProject | None,
) -> str:
    from qgis.PyQt.QtCore import QRect
    from qgis.PyQt.QtSvg import QSvgGenerator

    map_settings = _prepare_map_settings(settings, spec, project, vector=True)
    generator = QSvgGenerator()
    generator.setFileName(spec.path)
    generator.setSize(QSize(int(spec.width), int(spec.height)))
    generator.setViewBox(QRect(0, 0, int(spec.width), int(spec.height)))
    with contextlib.suppress(Exception):
        generator.setResolution(int(round(spec.dpi)))
    generator.setTitle("地图")
    generator.setDescription(
        f"{spec.width} × {spec.height} 像素，{spec.dpi:.0f} dpi"
    )

    painter = QPainter()
    if not painter.begin(generator):
        raise ExportMapImageError(f"SVG 写入失败：\n{spec.path}")
    try:
        if spec.background.alpha() >= 255:
            painter.fillRect(
                0, 0, int(spec.width), int(spec.height), spec.background
            )
        _render_with_painter(map_settings, painter)
    finally:
        painter.end()
    return spec.path


def _export_pdf(
    settings: QgsMapSettings,
    spec: ExportMapSpec,
    project: QgsProject | None,
) -> str:
    from qgis.PyQt.QtCore import QMarginsF, QSizeF
    from qgis.PyQt.QtGui import QPageSize, QPdfWriter

    map_settings = _prepare_map_settings(settings, spec, project, vector=True)
    dpi = max(1, int(round(spec.dpi)))
    page_width, page_height = pdf_page_size_mm(spec.width, spec.height, dpi)

    writer = QPdfWriter(spec.path)
    writer.setResolution(dpi)
    writer.setPageSize(
        QPageSize(
            QSizeF(page_width, page_height),
            QPageSize.Unit.Millimeter,
            "Map",
            QPageSize.SizeMatchPolicy.ExactMatch,
        )
    )
    writer.setPageMargins(QMarginsF(0, 0, 0, 0))

    painter = QPainter()
    if not painter.begin(writer):
        raise ExportMapImageError(f"PDF 写入失败：\n{spec.path}")
    try:
        painter.fillRect(
            0, 0, int(spec.width), int(spec.height), spec.background
        )
        _render_with_painter(map_settings, painter)
    finally:
        painter.end()
    return spec.path


def export_map_image(
    settings: QgsMapSettings,
    spec: ExportMapSpec,
    project: QgsProject | None = None,
    *,
    georeference: bool = False,
) -> str:
    """渲染并保存地图，返回保存路径（格式由 ``spec.path`` 扩展名决定）。

    :param georeference: 位图导出后是否写 world file / CRS sidecar。对话框勾选
        「导出后加载到地图」时传 ``True``，否则重新加载图片会没有地理参考。
    """
    spec.validate()
    fmt = format_from_path(spec.path)
    if fmt is None:  # validate 已拦过，这里只做类型收窄
        raise ExportMapImageError(
            "不支持的导出格式，请使用 PNG、JPEG、TIFF、WebP、BMP、PDF 或 SVG。"
        )

    if fmt.kind == "pdf":
        _export_pdf(settings, spec, project)
    elif fmt.kind == "svg":
        _export_svg(settings, spec, project)
    else:
        image = render_map_image(settings, spec, project)
        quality = fmt.quality if fmt.quality >= 0 else -1
        if not image.save(spec.path, fmt.save_format, quality):
            raise ExportMapImageError(
                f"图片写入失败：\n{spec.path}\n请检查磁盘空间或文件是否被占用。"
            )
        if georeference:
            try:
                write_georeference(
                    spec.path,
                    spec.extent,
                    spec.width,
                    spec.height,
                    settings.destinationCrs(),
                )
            except Exception as exc:  # noqa: BLE001 —— sidecar 失败不影响图片本身
                logger.warning("写世界文件失败（图片已正常导出）：%s", exc)

    logger.info(
        "地图已导出：%s（%s×%s, %s, dpi=%.1f）",
        spec.path,
        spec.width,
        spec.height,
        fmt.key,
        spec.dpi,
    )
    return spec.path
