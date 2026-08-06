"""空间质检：长宽比超限（"狭长图斑"）。

旧 :func:`component.check.SpaceDataCheck.layer_long_surface` 行为：对每个
要素求 oriented minimum bounding box，若长边 / 短边 > ``param["ratio"]``
（默认 5）即视为失败。
"""

from __future__ import annotations

import math
from typing import TYPE_CHECKING, List

from app.core.domain.check.param import CheckParam
from app.core.domain.check.registry import BaseCheck, CheckResult, CheckSeverity, CheckStatus, register

if TYPE_CHECKING:
    from qgis.core import QgsVectorLayer


@register("space_long_thin")
class SpaceLongThinCheck(BaseCheck):
    """检查 layer 内是否存在长宽比超限的要素。"""

    def build_where(self, p: CheckParam) -> str:
        return p.where or "1=1"

    def _ratio(self, p: CheckParam) -> float:
        v = p.extra.get("ratio", 5) if isinstance(p.extra, dict) else 5
        try:
            return float(v)
        except (TypeError, ValueError):
            return 5.0

    def run(self, p: CheckParam) -> CheckResult:
        if p.layer is None or not p.layer.isValid():
            return CheckResult(
                status=CheckStatus.FAIL,
                severity=CheckSeverity.ERROR,
                info={"error": "layer 无效"},
            )
        from qgis.core import QgsFeatureRequest  # 延迟 import：纯 Python 单测无需 qgis

        layer: "QgsVectorLayer" = p.layer
        ratio_limit = self._ratio(p)
        req = QgsFeatureRequest().setFilterExpression(self.build_where(p))

        failed: List[int] = []
        ratios: dict[int, float] = {}
        for feat in layer.getFeatures(req):
            geom = feat.geometry()
            if geom.isEmpty():
                continue
            geom.convertToMultiType()
            obb = geom.orientedMinimumBoundingBox()
            if obb is None:
                continue
            poly = obb[0].asPolygon()
            if not poly or not poly[0]:
                continue
            pts = poly[0]
            if len(pts) < 2:
                continue
            e1 = math.hypot(pts[1].x() - pts[0].x(), pts[1].y() - pts[0].y())
            e2 = math.hypot(pts[2].x() - pts[1].x(), pts[2].y() - pts[1].y())
            if e2 == 0 or e1 == 0:
                continue
            ratio = e1 / e2 if e1 > e2 else e2 / e1
            if ratio > ratio_limit:
                failed.append(feat.id())
                ratios[feat.id()] = ratio

        return CheckResult(
            status=CheckStatus.PASS if not failed else CheckStatus.FAIL,
            severity=CheckSeverity.WARNING if failed else CheckSeverity.INFO,
            failed_feature_ids=failed,
            info={"type_id": self.type_id, "ratios": ratios, "limit": ratio_limit},
        )
