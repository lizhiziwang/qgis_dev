"""按位置选择：目标图层要素与源图层要素之间的空间关系判定。

与 :class:`~app.core.domain.query.SpaceQueryService` 的分工：

- ``SpaceQueryService`` 是「用户画一个几何（点 / 线 / 面 / 框）→ 查哪几个要素与
  它相交」，只做相交；
- 本模块是 QGIS「按位置选择（Select by Location）」的语义：拿**源图层的要素**
  （全部或选中的）去和**目标图层**的每个要素做空间关系判断，支持相交 / 包含 /
  被包含 / 重叠 / 穿越 / 接触 / 相等 / 不相交 / 距离范围内。

``source_geometries`` 按源图层 CRS 传入；内部会先变换到目标图层 CRS，再建
:class:`QgsSpatialIndex` 缩小候选集，最后做精确判断。返回命中的目标要素 fid 列表，
选择集的实际写入由调用方（界面）完成。
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Iterable

    from qgis.core import QgsCoordinateReferenceSystem, QgsGeometry, QgsVectorLayer

#: 支持的空间关系（与界面下拉的 ``data`` 对应）
PREDICATES: tuple[str, ...] = (
    "intersects",
    "contains",
    "within",
    "overlaps",
    "crosses",
    "touches",
    "equals",
    "disjoint",
    "distance",
)

#: 距离类关系：``QgsGeometry.distance`` 的阈值（目标图层 CRS 单位）
DISTANCE_PREDICATE = "distance"


def geometry_matches(
    target: "QgsGeometry",
    source: "QgsGeometry",
    predicate: str,
    distance: float = 0.0,
) -> bool:
    """判断目标几何与源几何是否满足空间关系 ``predicate``。

    ``contains`` 表示「目标包含源」、``within`` 表示「目标被源包含」，与 QGIS
    「目标图层要素的空间选择方法」下拉的语义一致。
    """
    if predicate == "intersects":
        return target.intersects(source)
    if predicate == "contains":
        return target.contains(source)
    if predicate == "within":
        return target.within(source)
    if predicate == "overlaps":
        return target.overlaps(source)
    if predicate == "crosses":
        return target.crosses(source)
    if predicate == "touches":
        return target.touches(source)
    if predicate == "equals":
        return target.equals(source)
    if predicate == "disjoint":
        return target.disjoint(source)
    if predicate == DISTANCE_PREDICATE:
        return target.distance(source) <= max(float(distance), 0.0)
    return False


def _prepare_sources(
    source_geometries: "Iterable[QgsGeometry]",
    source_crs: "QgsCoordinateReferenceSystem | None",
    target_crs: "QgsCoordinateReferenceSystem",
) -> list["QgsGeometry"]:
    """源几何 → 目标图层 CRS 的副本（空几何丢弃）。"""
    from qgis.core import (
        QgsCoordinateTransform,
        QgsGeometry,
        QgsProject,
    )

    transform = None
    if (
        source_crs is not None
        and source_crs.isValid()
        and target_crs.isValid()
        and source_crs != target_crs
    ):
        transform = QgsCoordinateTransform(
            source_crs, target_crs, QgsProject.instance().transformContext()
        )

    prepared: list[QgsGeometry] = []
    for geometry in source_geometries:
        if geometry is None or geometry.isEmpty():
            continue
        copy = QgsGeometry(geometry)
        if transform is not None:
            copy.transform(transform)
        if not copy.isEmpty():
            prepared.append(copy)
    return prepared


def match_feature_ids(
    target_layer: "QgsVectorLayer",
    source_geometries: "Iterable[QgsGeometry]",
    source_crs: "QgsCoordinateReferenceSystem | None",
    predicate: str,
    distance: float = 0.0,
) -> list[int]:
    """返回 ``target_layer`` 中与任一源几何满足空间关系的要素 ID。

    使用 :class:`QgsSpatialIndex` 按包围盒先筛候选，再逐个做精确判定；源几何与目标
    图层 CRS 不同时先做坐标变换。``distance`` 只在 ``predicate="distance"`` 时生效，
    单位为**目标图层 CRS 的单位**。
    """
    from qgis.core import (
        QgsFeature,
        QgsFeatureRequest,
        QgsRectangle,
        QgsSpatialIndex,
    )

    if target_layer is None or not target_layer.isValid():
        return []

    prepared = _prepare_sources(source_geometries, source_crs, target_layer.crs())
    if not prepared:
        return []

    index = QgsSpatialIndex()
    for position, geometry in enumerate(prepared):
        feature = QgsFeature(position)
        feature.setGeometry(geometry)
        index.addFeature(feature)

    grow = max(float(distance), 0.0) if predicate == DISTANCE_PREDICATE else 0.0
    matches: list[int] = []
    request = QgsFeatureRequest().setNoAttributes()
    for feature in target_layer.getFeatures(request):
        geometry = feature.geometry()
        if geometry is None or geometry.isEmpty():
            continue
        bbox = QgsRectangle(geometry.boundingBox())
        if grow > 0.0:
            bbox.grow(grow)
        candidates = index.intersects(bbox)
        if predicate == "disjoint":
            # 与目标相交的源几何包围盒一定在候选里：候选全部不相交 ⇒ 与所有源不相交
            matched = all(not geometry.intersects(prepared[i]) for i in candidates)
        else:
            matched = any(
                geometry_matches(geometry, prepared[i], predicate, grow) for i in candidates
            )
        if matched:
            matches.append(feature.id())
    return matches


__all__ = [
    "DISTANCE_PREDICATE",
    "PREDICATES",
    "geometry_matches",
    "match_feature_ids",
]
