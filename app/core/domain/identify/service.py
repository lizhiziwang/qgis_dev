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
    """点击识别的结果（单条要素）。"""

    layer: "QgsVectorLayer"
    feature: "QgsFeature"
    geometry_wkt: str = ""
    attributes: dict = field(default_factory=dict)

    @property
    def fid(self) -> int:
        return int(self.feature.id())


class FeatureIdentifyService:
    """点击识别服务。"""

    #: 默认用 QGIS 自带的 :class:`QgsMapToolIdentify`，模式 ``TopDownAll``
    DEFAULT_MODE = 1   # TopDownAll

    def identify(
        self,
        canvas: "QgsMapCanvas",
        x: float,
        y: float,
        mode: int = 1,
    ) -> List[IdentifyResult]:
        """点击画布坐标 ``(x, y)`` 识别要素。"""
        from qgis.gui import QgsMapToolIdentify

        tool = QgsMapToolIdentify(canvas)
        results = tool.identify(x, y, mode)
        out: List[IdentifyResult] = []
        for r in results:
            layer = r.mLayer
            feature = r.mFeature
            if feature is None or not hasattr(layer, "fields"):
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
        from PyQt5.QtCore import Qt
        from PyQt5.QtGui import QColor
        from qgis.gui import QgsHighlight

        h = QgsHighlight(canvas, feature, layer)
        h.setColor(QColor(255, 0, 0))
        h.setFillColor(QColor(255, 0, 0, 50))
        h.setWidth(1)
        h.show()
        return h
