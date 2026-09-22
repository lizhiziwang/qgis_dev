"""空间质检的公共量算口径：优先把几何转到江西区域 Albers 等积投影再量。

空间质检的面积 / 周长 / 夹角统一用 :data:`JIANGXI_ALBERS_WKT`
（``CGCS2000 / Jiangxi_Albers_Equal_Area``）先把要素转到等积投影，再在投影平面上
取值，好处有三个：

- 等积投影的平面**面积**就是真实面积（米²），与国土 / 林业面积统计口径一致；
- **夹角**在地理坐标系（经纬度）上直接算会严重变形，投影之后才有意义；
- 比逐要素 :class:`QgsDistanceArea` 椭球量算快得多（实测 2 万要素 2.28s → 0.84s）。

投影不可用（或坐标转换失败）时才退回 :class:`QgsDistanceArea` 椭球量算，最后退回
原始平面量算。注意 Albers **只保面积不保长度**：量周长会有 ~2e-5 的偏差。
"""

from __future__ import annotations

from functools import lru_cache
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from qgis.core import QgsCoordinateReferenceSystem, QgsCoordinateTransform, QgsGeometry

#: 江西区域等积投影（CGCS2000 / Jiangxi_Albers_Equal_Area，中央经线 116°、
#: 双标准纬线 25.5°/28.5°）。等积投影下平面面积 = 真实面积（米²）。
JIANGXI_ALBERS_WKT = (
    'PROJCS["CGCS2000 / Jiangxi_Albers_Equal_Area",'
    'GEOGCS["China_Geodetic_Coordinate_System_2000",'
    '  DATUM["China_2000",'
    '    SPHEROID["CGCS2000",6378137,298.257222101]],'
    '  PRIMEM["Greenwich",0],'
    '  UNIT["Degree",0.0174532925199433]],'
    'PROJECTION["Albers_Conic_Equal_Area"],'
    'PARAMETER["standard_parallel_1",25.5],'
    'PARAMETER["standard_parallel_2",28.5],'
    'PARAMETER["latitude_of_origin",27.0],'
    'PARAMETER["central_meridian",116.0],'
    'PARAMETER["false_easting",0],'
    'PARAMETER["false_northing",0],'
    'UNIT["Meter",1]]'
)


@lru_cache(maxsize=1)
def jiangxi_albers_crs() -> QgsCoordinateReferenceSystem | None:
    """江西 Albers 等积投影 CRS；解析失败返回 ``None``（调用方退回椭球量算）。"""
    from qgis.core import QgsCoordinateReferenceSystem  # 延迟 import：纯 Python 单测无需 qgis

    crs = QgsCoordinateReferenceSystem()
    if not crs.createFromWkt(JIANGXI_ALBERS_WKT) or not crs.isValid():
        return None
    return crs


def make_transform(crs) -> QgsCoordinateTransform | None:
    """``crs`` → 江西 Albers 的坐标转换；``crs`` 无效 / 投影不可用时返回 ``None``。"""
    target = jiangxi_albers_crs()
    if target is None or crs is None or not crs.isValid():
        return None
    from qgis.core import QgsCoordinateTransform, QgsProject  # 延迟 import

    return QgsCoordinateTransform(crs, target, QgsProject.instance().transformContext())


def make_ellipsoid_measure(crs):
    """椭球量算器（投影不可用时的退路）；``crs`` 无效时返回 ``None``。"""
    if crs is None or not crs.isValid():
        return None
    from qgis.core import QgsDistanceArea, QgsProject  # 延迟 import

    measure = QgsDistanceArea()
    measure.setSourceCrs(crs, QgsProject.instance().transformContext())
    ellipsoid = crs.ellipsoidAcronym()
    if ellipsoid:
        measure.setEllipsoid(ellipsoid)
    return measure


def project_geometry(geom: QgsGeometry, transform) -> QgsGeometry | None:
    """把几何转到目标投影；``transform`` 为 ``None`` 或转换失败时返回 ``None``。"""
    if transform is None:
        return None
    from qgis.core import Qgis, QgsGeometry  # 延迟 import

    projected = QgsGeometry(geom)
    if projected.transform(transform) == Qgis.GeometryOperationResult.Success:
        return projected
    return None


def measuring_geometry(geom: QgsGeometry, transform) -> QgsGeometry:
    """量算用的几何：能转到 Albers 就用投影后的副本，否则用原几何。"""
    projected = project_geometry(geom, transform)
    return geom if projected is None else projected


def measure_area_perimeter(geom: QgsGeometry, transform, measure) -> tuple[float, float]:
    """量出几何的 ``(面积, 周长)``（米² / 米）：投影优先 → 椭球量算 → 平面量算。"""
    projected = project_geometry(geom, transform)
    if projected is not None:
        return projected.area(), projected.length()
    if measure is not None:
        return measure.measureArea(geom), measure.measurePerimeter(geom)
    return geom.area(), geom.length()


__all__ = [
    "JIANGXI_ALBERS_WKT",
    "jiangxi_albers_crs",
    "make_ellipsoid_measure",
    "make_transform",
    "measure_area_perimeter",
    "measuring_geometry",
    "project_geometry",
]
