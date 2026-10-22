"""本地数据格式的**唯一分类口径**（矢量 / 栅格 / 表格 / 容器数据库 / GDB 目录）。

「添加图层」文件框、修复数据源时的过滤器、以及新的「添加数据」弹窗都要判断
“这个路径是什么数据”，以前这些扩展名各写一份，漏改一处就会出现“弹窗里看得到、
加载器不认”的分歧。这里集中定义扩展名与分类函数，UI 与加载逻辑都从这里取。

本模块**不 import qgis**：它是纯路径判定，能在无 QGIS 的测试 / 服务端环境里用。
"""

from __future__ import annotations

import os
from typing import Final

#: 矢量数据扩展名（单文件格式 + 本地容器库）。``.gdb`` 是目录，见 :data:`GDB_SUFFIX`
VECTOR_EXTS: Final[frozenset[str]] = frozenset(
    {
        ".shp",
        ".geojson",
        ".json",
        ".gpkg",
        ".gml",
        ".kml",
        ".sqlite",
        ".sqlite3",
        ".db",
        ".tab",
        ".mif",
        ".fgb",
        ".gpx",
        ".dxf",
    }
)

#: 栅格数据扩展名（按扩展名走 GDAL 栅格 provider）
RASTER_EXTS: Final[frozenset[str]] = frozenset(
    {
        ".tif",
        ".tiff",
        ".img",
        ".jpg",
        ".jpeg",
        ".png",
        ".bmp",
        ".gif",
        ".dem",
        ".asc",
        ".vrt",
        ".jp2",
        ".ecw",
        ".hdf",
        ".nc",
        ".mrf",
    }
)

#: 表格数据扩展名（CSV / Excel / ODS）：QGIS 用 OGR 的文本 / 表格驱动打开
TABLE_EXTS: Final[frozenset[str]] = frozenset(
    {".csv", ".xls", ".xlsx", ".ods"}
)

#: Excel / ODS 一个文件里可能有多张表（工作表），弹窗里可以展开挑选
EXCEL_EXTS: Final[frozenset[str]] = frozenset({".xls", ".xlsx", ".ods"})

#: 一个文件里装多个图层的本地数据库（加载前要列子图层让用户挑）
CONTAINER_EXTS: Final[frozenset[str]] = frozenset(
    {".gpkg", ".sqlite", ".sqlite3", ".db"}
)

#: 可以“就地展开成多层”的扩展名：本地数据库 + Excel 工作簿
MULTILAYER_EXTS: Final[frozenset[str]] = CONTAINER_EXTS | EXCEL_EXTS

#: File Geodatabase 的目录后缀（``.gdb`` 是**目录**，不是文件）
GDB_SUFFIX: Final[str] = ".gdb"

#: 文本 / XML 矢量格式：文件大时加载慢、可能吃内存，加载前值得提示
LARGE_TEXT_VECTOR_EXTS: Final[frozenset[str]] = frozenset(
    {".geojson", ".json", ".kml", ".gml"}
)

#: 弹窗「类型」列用的友好名称；没登记的扩展名回退成大写后缀
FORMAT_LABELS: Final[dict[str, str]] = {
    ".shp": "Shapefile",
    ".geojson": "GeoJSON",
    ".json": "GeoJSON",
    ".gml": "GML",
    ".kml": "KML",
    ".gpkg": "GeoPackage 数据库",
    ".sqlite": "SQLite 数据库",
    ".sqlite3": "SQLite 数据库",
    ".db": "SQLite 数据库",
    ".csv": "CSV 表格",
    ".xls": "Excel 表格",
    ".xlsx": "Excel 表格",
    ".ods": "ODS 表格",
    ".tab": "MapInfo TAB",
    ".mif": "MapInfo MIF",
    ".fgb": "FlatGeobuf",
    ".gpx": "GPX",
    ".dxf": "DXF",
    ".tif": "GeoTIFF 栅格",
    ".tiff": "GeoTIFF 栅格",
    ".img": "ERDAS IMG 栅格",
    ".jpg": "JPEG 影像",
    ".jpeg": "JPEG 影像",
    ".png": "PNG 影像",
    ".bmp": "BMP 影像",
    ".gif": "GIF 影像",
    ".dem": "DEM 高程",
    ".asc": "ASCII Grid 栅格",
    ".vrt": "VRT 虚拟栅格",
    ".jp2": "JPEG2000 栅格",
    ".ecw": "ECW 影像",
    ".hdf": "HDF 栅格",
    ".nc": "NetCDF 栅格",
    ".mrf": "MRF 栅格",
}

#: 分类结果（字符串常量，避免 UI 各处再维护一套枚举）
KIND_DIR = "dir"
KIND_GDB = "gdb"
KIND_VECTOR = "vector"
KIND_RASTER = "raster"
KIND_TABLE = "table"
KIND_CONTAINER = "container"
KIND_UNKNOWN = ""


def suffix_of(path: str) -> str:
    """路径扩展名（小写，含点）；``.gdb`` 目录也能取到 ``.gdb``。"""
    return os.path.splitext(str(path or "").rstrip("\\/"))[1].lower()


def is_gdb_dir(path: str) -> bool:
    """是否是 File Geodatabase 目录（磁盘上真实存在且以 ``.gdb`` 结尾）。"""
    return os.path.isdir(path) and suffix_of(path) == GDB_SUFFIX


def classify_path(path: str) -> str:
    """把路径分成 :data:`KIND_DIR` / ``gdb`` / ``vector`` / ``raster`` / ``container``。

    目录优先判断：``.gdb`` 虽然是目录，但要按“可展开的数据库”展示；普通目录返回
    ``dir``。扩展名不在任何清单里返回空串（调用方据此决定是否展示）。
    """
    if not path:
        return KIND_UNKNOWN
    return classify_entry(path, is_dir=os.path.isdir(path))


def classify_entry(path: str, *, is_dir: bool) -> str:
    """已知“是不是目录”时的分类（列目录时用，省掉对每个条目的额外 ``stat``）。"""
    if not path:
        return KIND_UNKNOWN
    if is_dir:
        return KIND_GDB if suffix_of(path) == GDB_SUFFIX else KIND_DIR
    ext = suffix_of(path)
    if ext in CONTAINER_EXTS:
        return KIND_CONTAINER
    if ext in TABLE_EXTS:
        return KIND_TABLE
    if ext in RASTER_EXTS:
        return KIND_RASTER
    if ext in VECTOR_EXTS:
        return KIND_VECTOR
    return KIND_UNKNOWN


def is_container_source(path: str) -> bool:
    """是否需要先列子图层再加载（GDB 目录 / GeoPackage / SQLite）。"""
    return classify_path(path) in (KIND_GDB, KIND_CONTAINER)


def is_multi_layer_source(path: str) -> bool:
    """是否可能在文件内部装多个图层（本地库 + Excel 工作簿）：弹窗里可展开挑选。

    与 :func:`is_container_source` 的区别：Excel 在弹窗里能展开工作表，但加载器
    直接选中它时仍按普通表格文件打开，不强制走“先挑图层”的容器流程。
    """
    kind = classify_path(path)
    if kind == KIND_GDB:
        return True
    return kind == KIND_CONTAINER or suffix_of(path) in EXCEL_EXTS



def format_label(path: str, kind: str | None = None) -> str:
    """「类型」列显示名；目录返回“文件夹” / “GDB 数据库”。

    ``kind`` 已知时传入可省掉一次 ``os.path.isdir``（列目录时每个条目都算一次）。
    """
    if kind is None:
        kind = classify_path(path)
    if kind == KIND_DIR:
        return "文件夹"
    if kind == KIND_GDB:
        return "GDB 数据库"
    ext = suffix_of(path)
    return FORMAT_LABELS.get(ext, ext.lstrip(".").upper() + " 数据" if ext else "未知数据")


__all__ = [
    "CONTAINER_EXTS",
    "EXCEL_EXTS",
    "FORMAT_LABELS",
    "GDB_SUFFIX",
    "KIND_CONTAINER",
    "KIND_DIR",
    "KIND_GDB",
    "KIND_RASTER",
    "KIND_TABLE",
    "KIND_UNKNOWN",
    "KIND_VECTOR",
    "LARGE_TEXT_VECTOR_EXTS",
    "MULTILAYER_EXTS",
    "RASTER_EXTS",
    "TABLE_EXTS",
    "VECTOR_EXTS",
    "classify_entry",
    "classify_path",
    "format_label",
    "is_container_source",
    "is_gdb_dir",
    "is_multi_layer_source",
    "suffix_of",
]
