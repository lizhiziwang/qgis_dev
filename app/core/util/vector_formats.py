"""矢量数据格式表：界面显示名 ↔ OGR 驱动名 ↔ 默认文件扩展名。

创建 / 另存矢量数据时这三个名字必须一起给对，混着记最容易出错：

- **显示名**给界面下拉用（``GeoPackage``）；
- **OGR 驱动名**才是 ``QgsVectorFileWriter`` / processing 要的
  （``GPKG`` / ``ESRI Shapefile``，**不等于**文件后缀）；
- **默认扩展名**决定文件名的后缀（``.gpkg``）。

历史上这段映射在各对话框里手抄（``{"shp": "ESRI Shapefile", "geojson":
"GeoJSON"}.get(ext, "GPKG")`` 散在 8 个文件里），新代码统一从这里取。
"""

from __future__ import annotations

import os
from dataclasses import dataclass


@dataclass(frozen=True)
class VectorFormat:
    """一种可写出的矢量数据格式。"""

    #: 界面显示名（如 ``GeoPackage``）
    label: str
    #: OGR 驱动名（如 ``GPKG`` / ``ESRI Shapefile``）
    driver: str
    #: 默认文件扩展名，含点（如 ``.gpkg``）
    extension: str
    #: 限制 / 注意事项（界面 tooltip 用；无特殊限制时为空）
    note: str = ""


#: 常用矢量数据格式。**顺序即界面下拉顺序**，GeoPackage 排第一：
#: 单文件、无字段名长度与类型限制，是 QGIS 系的默认推荐。
VECTOR_FORMATS: tuple[VectorFormat, ...] = (
    VectorFormat("GeoPackage", "GPKG", ".gpkg"),
    VectorFormat("Shapefile", "ESRI Shapefile", ".shp", "仅单几何类型；字段名 ≤10 字符、不支持中文字段别名"),
    VectorFormat("GeoJSON", "GeoJSON", ".geojson", "仅 WGS 84（EPSG:4326）"),
    VectorFormat("KML", "KML", ".kml", "仅 WGS 84；属性字段类型受限"),
    VectorFormat("GML", "GML", ".gml"),
    VectorFormat("File Geodatabase", "FileGDB", ".gdb", "目录型数据源；需 FileGDB 驱动支持"),
    VectorFormat("SQLite / SpatiaLite", "SQLite", ".sqlite"),
    VectorFormat("MapInfo TAB", "MapInfo File", ".tab", "多文件格式：同名的 .dat/.map/.id 要一起带走"),
    VectorFormat("AutoCAD DXF", "DXF", ".dxf", "DXF 无属性表，只写几何"),
    VectorFormat("CSV", "CSV", ".csv", "几何写进 WKT 列；文件里不存坐标系"),
    VectorFormat("FlatGeobuf", "FlatGeobuf", ".fgb"),
    VectorFormat("GPX", "GPX", ".gpx", "只支持点 / 线；仅 WGS 84"),
)


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


__all__ = ["VECTOR_FORMATS", "VectorFormat", "format_for_extension", "format_for_path"]
