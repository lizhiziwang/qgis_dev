"""矢量数据格式表：界面显示名 ↔ OGR 驱动名 ↔ 默认文件扩展名。

创建 / 另存矢量数据时这三个名字必须一起给对，混着记最容易出错：

- **显示名**给界面下拉用（``GeoPackage``）；
- **OGR 驱动名**才是 ``QgsVectorFileWriter`` / processing 要的
  （``GPKG`` / ``ESRI Shapefile``，**不等于**文件后缀）；
- **默认扩展名**决定文件名的后缀（``.gpkg``）；
- **是否容器**（:attr:`VectorFormat.container`）区分「一个文件装多个图层」的库
  （GeoPackage / SQLite / FileGDB）与「一个文件就是一层」的单图层文件——两者的
  「文件名称 / 图层名」语义完全不同，界面要分开展示、流程也要分开（见
  :func:`formats_by_kind` / :func:`container_drivers`）。

历史上这段映射在各对话框里手抄（``{"shp": "ESRI Shapefile", "geojson":
"GeoJSON"}.get(ext, "GPKG")`` 散在 8 个文件里），新代码统一从这里取。
"""

from __future__ import annotations

import os
from dataclasses import dataclass, replace


@dataclass(frozen=True)
class VectorFormat:
    """一种可写出的矢量数据格式。"""

    #: 界面显示名（如 ``GeoPackage``）
    label: str
    #: 首选的 OGR 驱动名（如 ``GPKG`` / ``ESRI Shapefile``）
    driver: str
    #: 默认文件扩展名，含点（如 ``.gpkg``）
    extension: str
    #: 限制 / 注意事项（界面 tooltip 用；无特殊限制时为空）
    note: str = ""
    #: 多图层容器：一个文件里能装多个图层（库）。这类格式可以只建空库，
    #: 也可以往已存在的库里追加图层，回读时要带 ``|layername=``。
    container: bool = False
    #: 备选驱动名（首选驱动在本机构建里不存在时改用）。
    #: 同一格式在 GDAL 里可能注册成多个名字，且**可用性随构建而变**：
    #: FileGDB 在带 ESRI SDK 的构建里叫 ``FileGDB``，在只带 GDAL 自研实现的
    #: 构建里叫 ``OpenFileGDB``（GDAL ≥ 3.6 才可写）。见 :func:`resolve_format`。
    alt_drivers: tuple[str, ...] = ()


#: 常用矢量数据格式。**顺序即界面下拉顺序**，GeoPackage 排第一：
#: 单文件、无字段名长度与类型限制，是 QGIS 系的默认推荐。
VECTOR_FORMATS: tuple[VectorFormat, ...] = (
    VectorFormat("GeoPackage", "GPKG", ".gpkg", container=True),
    VectorFormat("Shapefile", "ESRI Shapefile", ".shp", "仅单几何类型；字段名 ≤10 字符、不支持中文字段别名"),
    VectorFormat("GeoJSON", "GeoJSON", ".geojson", "仅 WGS 84（EPSG:4326）"),
    VectorFormat("KML", "KML", ".kml", "仅 WGS 84；属性字段类型受限"),
    VectorFormat("GML", "GML", ".gml"),
    VectorFormat("File Geodatabase", "FileGDB", ".gdb", "目录型数据源：一个 .gdb 文件夹就是一个库", container=True, alt_drivers=("OpenFileGDB",)),
    VectorFormat("SQLite / SpatiaLite", "SQLite", ".sqlite", container=True),
    VectorFormat("MapInfo TAB", "MapInfo File", ".tab", "多文件格式：同名的 .dat/.map/.id 要一起带走"),
    VectorFormat("AutoCAD DXF", "DXF", ".dxf", "DXF 无属性表，只写几何"),
    VectorFormat("CSV", "CSV", ".csv", "几何写进 WKT 列；文件里不存坐标系"),
    VectorFormat("FlatGeobuf", "FlatGeobuf", ".fgb"),
    VectorFormat("GPX", "GPX", ".gpx", "只支持点 / 线；仅 WGS 84"),
)


def formats_by_kind(*, container: bool) -> tuple[VectorFormat, ...]:
    """按「是不是多图层容器」过滤格式表（界面按类分页 / 分下拉时用）。"""
    return tuple(fmt for fmt in VECTOR_FORMATS if fmt.container is container)


def container_drivers() -> frozenset[str]:
    """多图层容器格式的 OGR 驱动名（``GPKG`` / ``SQLite`` / ``FileGDB``）。

    :attr:`VectorFormat.container` 是这件事的**唯一真相**：创建服务判「是不是
    写进一个能装多图层的文件」（回读要拼 ``|layername=``、目标已存在时语义不同）
    只从这里取，别再另抄一份驱动名集合。
    """
    return frozenset(fmt.driver for fmt in VECTOR_FORMATS if fmt.container)


def resolve_format(fmt: VectorFormat, writers: frozenset[str]) -> VectorFormat | None:
    """选出本机能真正写出的驱动名，返回换好 ``driver`` 的那份格式；写不了返回 ``None``。

    ``writers`` 是 :func:`QgsVectorFileWriter.ogrDriverList` 那一份「可写驱动名」集合
    （见 ``create_data.service.available_drivers``）。**同一个格式在 GDAL 里可能有多个
    驱动名、可用性还随构建而变**，所以「本机能不能建这个格式」要在这里判：能就用
    解析出的驱动名建（写回，调用方别再自己去猜名字），一个都用不了就返回 ``None``，
    调用方把这一项从界面上**隐藏**掉（而不是灰着占位——点了也建不出来）。

    实测（本机 QGIS 4.2 / GDAL）：``FileGDB`` 驱动不存在，但 ``OpenFileGDB`` 在，
    且能正常建出 .gdb，所以 File Geodatabase 走的就是这条备选。
    """
    for driver in (fmt.driver, *fmt.alt_drivers):
        if driver in writers:
            return fmt if driver == fmt.driver else replace(fmt, driver=driver)
    return None


def format_for_extension(extension: str) -> VectorFormat | None:
    """按扩展名查格式（``shp`` / ``.shp`` / ``.SHP`` 都认）；查不到返回 ``None``。"""
    key = extension.strip().lower()
    if not key:
        return None
    if not key.startswith("."):
        key = f".{key}"
    for fmt in VECTOR_FORMATS:
        if key == fmt.extension:
            return fmt
    return None


def format_for_path(path: str) -> VectorFormat | None:
    """按路径扩展名查格式（:func:`format_for_extension` 的取扩展名版本）。"""
    return format_for_extension(os.path.splitext(str(path))[1])


__all__ = [
    "VECTOR_FORMATS",
    "VectorFormat",
    "container_drivers",
    "format_for_extension",
    "format_for_path",
    "formats_by_kind",
    "resolve_format",
]
