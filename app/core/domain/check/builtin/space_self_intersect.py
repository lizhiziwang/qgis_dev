"""空间质检：自相交。

旧 :func:`component.check.SpaceDataCheck.layer_self_intersect` 行为：遍历
layer 要素，对每个 ``geom`` 调 :meth:`QgsGeometry.validateGeometry`，
命中 ``intersect / self-intersect / self intersection`` 即视为失败。
"""

from __future__ import annotations

from typing import TYPE_CHECKING, List

from app.core.domain.check.param import CheckParam
from app.core.domain.check.registry import BaseCheck, CheckResult, CheckSeverity, CheckStatus, register

if TYPE_CHECKING:
    from qgis.core import QgsGeometry, QgsVectorLayer


@register("space_self_intersect")
class SpaceSelfIntersectCheck(BaseCheck):
    """检查 layer 内要素是否自相交。"""

    def build_where(self, p: CheckParam) -> str:
        return p.where or "1=1"

    def run(self, p: CheckParam) -> CheckResult:
        if p.layer is None or not p.layer.isValid():
            return CheckResult(
                status=CheckStatus.FAIL,
                severity=CheckSeverity.ERROR,
                info={"error": "layer 无效"},
            )
        from qgis.core import QgsFeatureRequest, QgsGeometry  # 延迟 import

        req = QgsFeatureRequest().setFilterExpression(self.build_where(p))
        failed: List[int] = []
        for feat in p.layer.getFeatures(req):
            geom: "QgsGeometry" = feat.geometry()
            if geom.isEmpty():
                continue
            for err in geom.validateGeometry():
                msg = err.what()
                if any(k in msg for k in ("intersect", "self-intersect", "self intersection")):
                    failed.append(feat.id())
                    break
        return CheckResult(
            status=CheckStatus.PASS if not failed else CheckStatus.FAIL,
            severity=CheckSeverity.ERROR if failed else CheckSeverity.INFO,
            failed_feature_ids=failed,
            info={"type_id": self.type_id, "count": len(failed)},
        )
