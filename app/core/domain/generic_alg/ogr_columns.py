"""解析矢量图层在 OGR 数据源里的几何列名 / FID 列名。

**几何列**：QGIS 4.2 的 GDAL 算法（融合 / 矢量缓冲区 / 偏移线 / 单边缓冲区 /
沿线生成点）把几何列名直接拼进 ``-sql``（``SELECT ST_Union(geometry) ...``）。
算法只在输入被导出成临时文件时才使用真实列名；直接读 OGR / SpatiaLite /
PostgreSQL 图层时 ``geometry_column_name`` 为空，于是退回参数默认值
``"geometry"``。

GDAL 3.13 的 SQLite 方言不再把 ``geometry`` 当几何列别名，而各数据源的真实
列名并不相同：GeoPackage 是 ``geom``、FileGDB 是 ``SHAPE``、Shapefile 才是
``geometry``。memory 图层由 QGIS 先导出成临时 GeoPackage，几何列同样是
``geom``（「坐标转换」的临时结果直接做偏移线就会踩到）。输入不匹配时报：

    In ExecuteSQL(): sqlite3_prepare_v2(): no such column: geometry

**FID 列**：QGIS 的 OGR provider 会把 FID 列当普通字段暴露（FileGDB 的
``OBJECTID``、SQLite 的 ``rowid`` 等），但 GDAL 命令行工具按真实属性字段
查找（``gdal_rasterize -a OBJECTID``），会报：

    Failed to find field OBJECTID on layer ...

这里统一按输入图层解析这些列名，供 service（执行参数修正 / 校验）和 dialog
（表单默认值 / 字段下拉过滤）共用。
"""

from __future__ import annotations

from functools import lru_cache

#: 算法里拼进 SQL 的几何列名参数
GEOMETRY_PARAMETER = "GEOMETRY"

#: 非文件 provider → GDAL 算法内部实际读取的几何列名。
#: QGIS 在调用 GDAL 命令行工具前会把这类图层导出成临时 GeoPackage
#: （日志里能看到 ``INPUT.gpkg``），而 GeoPackage 的几何列固定是 ``geom``。
#: 典型场景：「坐标转换」的 memory 结果直接拿去「偏移线 / 融合」，默认的
#: ``geometry`` 会报 ``sqlite3_prepare_v2(): no such column: geometry``。
_CONVERTED_SOURCE_GEOMETRY = {
    "memory": "geom",
    "virtual": "geom",
}


def has_geometry_parameter(algorithm) -> bool:
    """算法是否有需要修正的 ``GEOMETRY`` 字符串参数。"""
    from qgis.core import QgsProcessingParameterString

    for pdef in algorithm.parameterDefinitions():
        if pdef.name() == GEOMETRY_PARAMETER and isinstance(
            pdef, QgsProcessingParameterString
        ):
            return True
    return False


def geometry_column_name(layer) -> str:
    """返回图层在数据源中的几何列名；查不到时返回空串。

    解析顺序：

    1. ``QgsDataSourceUri(source).geometryColumn()`` —— PostGIS / SpatiaLite
       的 source 自带 ``(geom)``，最直接；
    2. provider 的 ``querySublayers`` —— GeoPackage（``geom``）、FileGDB
       （``SHAPE``）的几何列名只在子图层元数据里；
    3. 数据源只有一个子图层时按它兜底；
    4. memory / virtual 等无文件数据源 —— QGIS 会先导出成临时 GeoPackage，
       几何列按 ``geom`` 处理。
    """
    from qgis.core import QgsDataSourceUri, QgsProviderRegistry

    provider = layer.providerType().lower()
    converted = _CONVERTED_SOURCE_GEOMETRY.get(provider)
    if converted:
        return converted

    try:
        column = QgsDataSourceUri(layer.source()).geometryColumn()
    except Exception:  # noqa: BLE001 —— 非 QgsDataSourceUri 能解析的 source
        column = ""
    if column:
        return column

    try:
        metadata = QgsProviderRegistry.instance().providerMetadata(
            layer.providerType()
        )
        if metadata is None:
            return ""
        sublayers = list(metadata.querySublayers(layer.source()) or [])
    except Exception:  # noqa: BLE001 —— memory / virtual 等 provider 不支持子图层
        return ""
    if not sublayers:
        return ""

    source = layer.source()
    for details in sublayers:
        if details.uri() == source:
            return details.geometryColumnName() or ""

    layer_name = ""
    try:
        layer_name = (metadata.decodeUri(source) or {}).get("layerName") or ""
    except Exception:  # noqa: BLE001
        layer_name = ""
    if layer_name:
        for details in sublayers:
            if details.name() == layer_name:
                return details.geometryColumnName() or ""

    if len(sublayers) == 1:
        return sublayers[0].geometryColumnName() or ""
    return ""


def fid_column_name(layer) -> str:
    """返回图层在 OGR 里的 FID 列名；非 OGR 图层或查不到时返回空串。

    FID 列由数据源自动维护（OpenFileGDB 是 ``OBJECTID``、SQLite 是
    ``rowid`` 等）。QGIS 会把它当普通字段显示，但它不是 OGR 层的属性字段，
    以 ``-a`` / ``-zfield`` 等命令行参数引用会失败。
    """
    if layer is None or layer.providerType().lower() != "ogr":
        return ""
    try:
        return _ogr_fid_column(layer.providerType(), layer.source())
    except Exception:  # noqa: BLE001 —— 打不开的数据源按「未知」处理
        return ""


@lru_cache(maxsize=64)
def _ogr_fid_column(provider: str, source: str) -> str:
    """按 ``(provider, source)`` 缓存 FID 列名，避免图层切换时反复开数据源。"""
    from osgeo import gdal
    from qgis.core import QgsProviderRegistry

    metadata = QgsProviderRegistry.instance().providerMetadata(provider)
    if metadata is None:
        return ""
    parts = metadata.decodeUri(source) or {}
    path = parts.get("path") or ""
    if not path:
        return ""
    if parts.get("vsiPrefix"):
        path = parts["vsiPrefix"] + path

    dataset = gdal.OpenEx(path, gdal.OF_VECTOR)
    if dataset is None:
        return ""
    try:
        layer_name = parts.get("layerName") or ""
        if layer_name:
            ogr_layer = dataset.GetLayerByName(layer_name)
        else:
            layer_id = parts.get("layerId")
            ogr_layer = dataset.GetLayer(int(layer_id or 0))
        return (ogr_layer.GetFIDColumn() if ogr_layer is not None else "") or ""
    finally:
        dataset = None
