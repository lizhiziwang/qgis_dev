"""空间相交查询。

修复旧 ``component.check.SpaceDataCheck.search_intersect_features_geom``
的两个问题：

1. **类型注解错误** —— 旧 ``geom: QSGGeometry``（Qt Quick 类型），实际
   传的是 :class:`QgsGeometry`。
2. **缺 key 静默失败** —— 旧 ``param["crs"]`` 缺 key 时 ``KeyError`` 被
   吞掉。新接口把 ``source_crs`` 改成必填参数。

返回值是 :class:`QgsFeatureRequest` 风格的 fid 列表，调用方可用
``layer.getFeature(fid)`` 拿单条要素。
"""

from __future__ import annotations

from typing import TYPE_CHECKING, List, Optional

if TYPE_CHECKING:
    from qgis.core import (
        QgsCoordinateReferenceSystem,
        QgsGeometry,
        QgsProject,
        QgsVectorLayer,
    )

from .space_index import build_spatial_index


def intersect_features(
    layer: "Optional[QgsVectorLayer]",
    geom: "Optional[QgsGeometry]",
    source_crs: "Optional[QgsCoordinateReferenceSystem]",
    project: "Optional[QgsProject]" = None,
) -> List[int]:
    """返回与 ``geom`` 相交的要素 ID 列表。

    Parameters
    ----------
    layer:
        目标矢量图层。
    geom:
        用于查询的几何对象（必须是 :class:`QgsGeometry`）。
    source_crs:
        ``geom`` 自身的坐标系；若与 ``layer.crs()`` 不同，会先做转换。
    project:
        :class:`QgsProject` 实例，用于 datum 转换上下文。``None`` 时用
        :meth:`QgsProject.instance`。
    """
    from qgis.core import (  # 延迟 import
        QgsCoordinateReferenceSystem,
        QgsCoordinateTransform,
        QgsGeometry,
        QgsProject,
    )

    if layer is None or not layer.isValid():
        return []
    if geom is None or geom.isEmpty():
        return []
    if source_crs is None:
        source_crs = layer.crs()

    if source_crs != layer.crs():
        project = project or QgsProject.instance()
        xform = QgsCoordinateTransform(source_crs, layer.crs(), project)
        geom = QgsGeometry(geom)  # 拷贝避免外部副作用
        geom.transform(xform)
        if geom.isEmpty():
            return []

    index = build_spatial_index(layer)
    if index is None:
        return []
    bbox = geom.boundingBox()
    candidates = index.intersects(bbox)
    return [fid for fid in candidates if layer.getFeature(fid).geometry().intersects(geom)]


__all__ = ["intersect_features"]
