"""空间查询服务。

迁自旧 :class:`component.SpaceQuery.SpaceQuery.query`，去掉 UI 耦合。
调用方传入 layer / geometry / crs，本服务返回 fid 列表。
"""

from __future__ import annotations

from typing import TYPE_CHECKING, List, Optional

if TYPE_CHECKING:
    from qgis.core import (
        QgsCoordinateReferenceSystem,
        QgsGeometry,
        QgsVectorLayer,
    )

from app.core.domain.check.query import intersect_features


class SpaceQueryService:
    """空间查询服务。"""

    def query_by_geometry(
        self,
        layer: "Optional[QgsVectorLayer]",
        geometry: "Optional[QgsGeometry]",
        source_crs: "Optional[QgsCoordinateReferenceSystem]" = None,
    ) -> List[int]:
        """返回与 ``geometry`` 相交的要素 ID 列表。

        ``source_crs`` 缺省时用 ``layer.crs()``。
        """
        if layer is None or geometry is None:
            return []
        crs = source_crs or layer.crs()
        return intersect_features(layer, geometry, crs)
