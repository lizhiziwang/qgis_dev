"""空间质检：要素覆盖（两两相交但非邻接）。

旧 :func:`component.check.SpaceDataCheck.layer_feature_cover` 行为：建空间
索引，按 fid 升序两两比对，``geom.intersects(t) and not geom.touches(t)``
即视为覆盖。
"""

from __future__ import annotations

from typing import TYPE_CHECKING, List, Set, Tuple

from app.core.domain.check.param import CheckParam
from app.core.domain.check.registry import BaseCheck, CheckResult, CheckSeverity, CheckStatus, register
from app.core.domain.check.space_index import build_spatial_index

if TYPE_CHECKING:
    from qgis.core import QgsVectorLayer


@register("space_overlap")
class SpaceOverlapCheck(BaseCheck):
    """检查 layer 内是否存在"相交但非邻接"的要素对。"""

    def build_where(self, p: CheckParam) -> str:
        return p.where or "1=1"

    def run(self, p: CheckParam) -> CheckResult:
        if p.layer is None or not p.layer.isValid():
            return CheckResult(
                status=CheckStatus.FAIL,
                severity=CheckSeverity.ERROR,
                info={"error": "layer 无效"},
            )
        from qgis.core import QgsFeatureRequest  # 延迟 import

        layer: "QgsVectorLayer" = p.layer
        build_spatial_index(layer)
        req = QgsFeatureRequest().setFilterExpression(self.build_where(p))

        recorded: Set[Tuple[int, int]] = set()
        for feat in layer.getFeatures(req):
            fid = feat.id()
            geom = feat.geometry()
            if geom.isEmpty():
                continue
            candidates = layer.__spatial_index__.intersects(geom.boundingBox())
            for other_fid in candidates:
                if other_fid <= fid:
                    continue
                target = layer.getFeature(other_fid).geometry()
                if geom.intersects(target) and not geom.touches(target):
                    recorded.add((fid, other_fid))
        failed: List[int] = sorted({i for pair in recorded for i in pair})
        return CheckResult(
            status=CheckStatus.PASS if not recorded else CheckStatus.FAIL,
            severity=CheckSeverity.ERROR if recorded else CheckSeverity.INFO,
            failed_feature_ids=failed,
            info={"type_id": self.type_id, "pairs": sorted(recorded)},
        )
