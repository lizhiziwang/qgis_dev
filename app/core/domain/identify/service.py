"""点击识别服务。

迁自 :class:`component.map_tool.FindFeatureTool`，去掉对 :class:`ModernFeatureInfoDialog`
的依赖，业务层只负责"识别 + 高亮"。

UI 层：调 :meth:`identify` 拿 :class:`IdentifyResult` 后，弹自己的 dialog。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, List, Optional

if TYPE_CHECKING:
    from qgis.core import QgsFeature, QgsMapLayer, QgsVectorLayer
    from qgis.gui import QgsHighlight, QgsMapCanvas, QgsMapToolIdentify


@dataclass(frozen=True)
class IdentifyResult:
    """点击识别的结果：单条矢量要素，或栅格图层某像元的波段值。"""

    layer: "QgsMapLayer"
    feature: Optional["QgsFeature"] = None
    geometry_wkt: str = ""
    attributes: dict = field(default_factory=dict)
    #: 栅格图层时有效：波段名 → 该像元值（如 {"Band 1": "137"}）
    raster_values: dict = field(default_factory=dict)

    @property
    def is_raster(self) -> bool:
        from qgis.core import QgsRasterLayer

        return isinstance(self.layer, QgsRasterLayer)

    @property
    def fid(self) -> int:
        return int(self.feature.id()) if self.feature is not None else -1


class FeatureIdentifyService:
    """点击识别服务。"""

    def identify(
        self,
        canvas: "QgsMapCanvas",
        x: float,
        y: float,
        mode: Optional["QgsMapToolIdentify.IdentifyMode"] = None,
    ) -> List[IdentifyResult]:
        """点击画布坐标 ``(x, y)`` 识别要素。

        ``mode`` 默认 ``TopDownAll``（所有图层里所有可识别要素）。
        """
        from qgis.gui import QgsMapToolIdentify
        from qgis.core import QgsRasterLayer

        if mode is None:
            mode = QgsMapToolIdentify.IdentifyMode.TopDownAll
        tool = QgsMapToolIdentify(canvas)
        # QGIS 4.x 的 identify(x, y, mode) 要求 int 坐标 + IdentifyMode 枚举，
        # 传 float / int 都会 TypeError（int 在 PyQt6 不再自动转枚举）
        results = tool.identify(int(x), int(y), mode)
        out: List[IdentifyResult] = []
        for r in results:
            layer = r.mLayer
            feature = r.mFeature
            if isinstance(layer, QgsRasterLayer):
                # 栅格像元：波段值在 QgsMapToolIdentify 结果的 mAttributes
                # （如 {"Band 1": "137"}），要素几何是点击处的像元格子
                geom_wkt = ""
                try:
                    if feature is not None:
                        geom_wkt = feature.geometry().asWkt()
                except Exception:  # noqa: BLE001
                    pass
                out.append(
                    IdentifyResult(
                        layer=layer,
                        feature=feature,
                        geometry_wkt=geom_wkt,
                        raster_values={k: str(v) for k, v in r.mAttributes.items()},
                    )
                )
                continue
            if feature is None:
                continue
            attrs = {f.name(): feature.attribute(f.name()) for f in layer.fields()}
            geom_wkt = ""
            try:
                geom_wkt = feature.geometry().asWkt()
            except Exception:  # noqa: BLE001
                pass
            out.append(IdentifyResult(layer=layer, feature=feature, geometry_wkt=geom_wkt, attributes=attrs))
        return out

    def highlight(
        self,
        canvas: "QgsMapCanvas",
        layer: "QgsVectorLayer",
        feature: "QgsFeature",
    ) -> "QgsHighlight":
        """创建高亮对象（红色边框），调用方负责 ``deleteLater()``。"""
        from PyQt6.QtCore import Qt
        from PyQt6.QtGui import QColor
        from qgis.gui import QgsHighlight

        h = QgsHighlight(canvas, feature, layer)
        h.setColor(QColor(255, 0, 0))
        h.setFillColor(QColor(255, 0, 0, 50))
        h.setWidth(1)
        h.show()
        return h
