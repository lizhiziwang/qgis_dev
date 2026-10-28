"""栅格属性表（RAT）构建。

QGIS 自带的 ``QgsRasterAttributeTable.createFromRaster`` 对伪彩色渲染会输出
``Min`` / ``Max`` 区间（每个色阶一行），与 ArcMap 的「Value + Count」逐像元值
属性表不同；调色板渲染虽然有一行一个值，但没有像元计数。

本模块对整型波段直接统计每个像元值出现的次数，生成 Value / Count / 分类 /
颜色字段（字段用法与 QGIS 的 RAT 一致，能正常写回 GeoTIFF / .img / VRT）。
浮点波段、值域过大或读不到像元时退回 QGIS 自带实现，保证仍能创建属性表。
"""

from __future__ import annotations

import math

from qgis.core import (
    Qgis,
    QgsPalettedRasterRenderer,
    QgsRasterAttributeTable,
    QgsRasterLayer,
    QgsSingleBandPseudoColorRenderer,
)
from qgis.PyQt.QtCore import QMetaType
from qgis.PyQt.QtGui import QColor

from app.infra.logging import get_logger

logger = get_logger(__name__)

#: 整型波段最多枚举多少个取值；再大就退回 QGIS 的区间属性表，避免一次
#: 读几 GB 像元只为数 40 亿个可能的编码。
_MAX_BUCKETS = 1_000_000

_INTEGER_TYPES = {"Byte", "UInt16", "Int16", "UInt32", "Int32"}

#: RAT 字段（名称、用法、存储类型）。名称沿用 QGIS / ArcMap 的习惯，
#: 中文展示由属性表对话框负责。
_FIELDS = (
    ("Value", Qgis.RasterAttributeTableFieldUsage.MinMax, QMetaType.Type.Double),
    ("Count", Qgis.RasterAttributeTableFieldUsage.PixelCount, QMetaType.Type.LongLong),
    ("Class", Qgis.RasterAttributeTableFieldUsage.Name, QMetaType.Type.QString),
    ("Red", Qgis.RasterAttributeTableFieldUsage.Red, QMetaType.Type.Int),
    ("Green", Qgis.RasterAttributeTableFieldUsage.Green, QMetaType.Type.Int),
    ("Blue", Qgis.RasterAttributeTableFieldUsage.Blue, QMetaType.Type.Int),
    ("Alpha", Qgis.RasterAttributeTableFieldUsage.Alpha, QMetaType.Type.Int),
)


def build_raster_attribute_table(
    layer: QgsRasterLayer,
) -> tuple[QgsRasterAttributeTable | None, int]:
    """按当前渲染器生成栅格属性表，返回 ``(table, band)``。

    整型波段生成逐像元值的 Value + Count 表；其余情况（浮点波段、超大值域、
    远程数据源等）退回 ``createFromRaster``。调用方仍应检查返回值并回读验证
    数据源是否真的支持 RAT。
    """
    if not isinstance(layer, QgsRasterLayer):
        return None, 0
    renderer = layer.renderer()
    band = _renderer_band(renderer)
    if band > 0:
        counts = _band_value_counts(layer, band)
        if counts:
            table = _build_discrete_table(counts, renderer, band)
            if table is not None:
                return table, band
    return QgsRasterAttributeTable.createFromRaster(layer)


def _renderer_band(renderer) -> int:
    """渲染器对应的波段；不是可建 RAT 的渲染方式返回 0。"""
    if not isinstance(
        renderer, (QgsPalettedRasterRenderer, QgsSingleBandPseudoColorRenderer)
    ):
        return 0
    try:
        return int(renderer.inputBand())
    except (RuntimeError, AttributeError):
        return 0


def _band_value_counts(layer: QgsRasterLayer, band_no: int) -> list[tuple[int, int]] | None:
    """统计整型波段每个像元值的出现次数；不适用时返回 ``None``。

    用 GDAL 的精确直方图（``approx_ok=False``）一次扫描完成。Bin 边界取
    ``[value - 0.5, value + 0.5)``，整数像元正好落进各自的值桶。过程中关闭
    PAM，避免为了统计在数据目录旁写 ``.aux.xml``。
    """
    source = str(layer.source() or "")
    if not source:
        return None
    try:
        from osgeo import gdal
    except ImportError:  # pragma: no cover —— 正常 QGIS 环境都有
        logger.debug("没有 GDAL Python 绑定，退回 QGIS 属性表实现")
        return None

    dataset = None
    previous_pam = gdal.GetConfigOption("GDAL_PAM_ENABLED", None)
    gdal.SetConfigOption("GDAL_PAM_ENABLED", "NO")
    try:
        dataset = gdal.Open(source, gdal.GA_ReadOnly)
        if dataset is None:
            return None
        band = dataset.GetRasterBand(int(band_no))
        if band is None:
            return None
        if gdal.GetDataTypeName(band.DataType) not in _INTEGER_TYPES:
            return None
        minimum, maximum = band.ComputeRasterMinMax(False)
        if minimum is None or maximum is None:
            return None
        if not (math.isfinite(minimum) and math.isfinite(maximum)):
            return None
        first, last = math.floor(minimum), math.ceil(maximum)
        span = int(last - first + 1)
        if span <= 0 or span > _MAX_BUCKETS:
            return None
        histogram = band.GetHistogram(
            first - 0.5, last + 0.5, span, False, False, None
        )
        if not histogram:
            return None
        return [
            (first + index, int(count))
            for index, count in enumerate(histogram)
            if count
        ]
    except Exception as exc:  # 数据源类型 / GDAL 版本差异都按“统计不了”处理
        logger.debug("统计栅格像元值失败：%s", exc, exc_info=True)
        return None
    finally:
        dataset = None
        gdal.SetConfigOption("GDAL_PAM_ENABLED", previous_pam)


def _build_discrete_table(
    counts: list[tuple[int, int]], renderer, band: int
) -> QgsRasterAttributeTable | None:
    """按 ``counts`` 生成一行一个像元值的 RAT；字段不合法时返回 ``None``。"""
    table = QgsRasterAttributeTable()
    usage = Qgis.RasterAttributeTableFieldUsage
    for name, field_usage, field_type in _FIELDS:
        ok, error = table.appendField(name, field_usage, field_type)
        if not ok:
            logger.debug("创建 RAT 字段 %s 失败：%s", name, error)
            return None

    palette: dict[float, tuple[QColor, str]] = {}
    shader = None
    try:
        if isinstance(renderer, QgsPalettedRasterRenderer) and int(
            renderer.inputBand()
        ) == band:
            for cls in renderer.classes():
                color = QColor(cls.color)
                if not color.isValid():
                    color = QColor(0, 0, 0, 0)
                palette[float(cls.value)] = (color, str(cls.label or ""))
        elif isinstance(renderer, QgsSingleBandPseudoColorRenderer) and int(
            renderer.inputBand()
        ) == band:
            shader = renderer.shader()
    except (RuntimeError, AttributeError):
        palette, shader = {}, None

    nodata = QColor(0, 0, 0, 0)
    if isinstance(renderer, QgsPalettedRasterRenderer):
        try:
            candidate = QColor(renderer.nodataColor())
            if candidate.isValid():
                nodata = candidate
        except (RuntimeError, AttributeError):
            pass

    for value, count in counts:
        color, label = palette.get(float(value), (nodata, ""))
        if shader is not None:
            try:
                ok, red, green, blue, alpha = shader.shade(float(value))
                color = QColor(red, green, blue, alpha) if ok else QColor(0, 0, 0, 0)
            except (RuntimeError, TypeError):
                color = QColor(0, 0, 0, 0)
        if not label:
            label = _format_value(value)
        ok, error = table.appendRow(
            [
                float(value),
                int(count),
                label,
                color.red(),
                color.green(),
                color.blue(),
                color.alpha(),
            ]
        )
        if not ok:
            logger.debug("写入 RAT 行失败：value=%s，%s", value, error)
            return None

    valid = table.isValid()
    if isinstance(valid, tuple):
        valid, error = valid
    else:
        error = ""
    if not valid:
        logger.debug("生成的 RAT 无效：%s", error)
        return None
    return table


def _format_value(value) -> str:
    """整数去掉 ``.0``，浮点保留可读精度。"""
    try:
        number = float(value)
    except (TypeError, ValueError):
        return str(value)
    if number.is_integer() and abs(number) < 1e15:
        return str(int(number))
    return f"{number:g}"
