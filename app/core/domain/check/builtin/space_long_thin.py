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

    def iter_failed(self, p: CheckParam):
        """流式产出 ``(seen, fid, ratio)``：**每个处理过的要素都产出一条**。

        ``fid is None`` 表示该要素通过（仅用于推进进度），``fid`` 非空表示命中；
        每个要素都 yield 一次，界面才能在一遍扫描一个都不命中时也刷新进度。
        供界面分批驱动（避免长循环阻塞 UI）的同时，也供 :meth:`run` 复用。

        调用方需保证 ``p.layer`` 有效；无效图层直接不产出。
        """
        layer: "QgsVectorLayer" = p.layer
        if layer is None or not layer.isValid():
            return
        from qgis.core import QgsFeatureRequest  # 延迟 import：纯 Python 单测无需 qgis

        ratio_limit = self._ratio(p)
        req = QgsFeatureRequest().setFilterExpression(self.build_where(p))

        seen = 0
        for feat in layer.getFeatures(req):
            seen += 1
            geom = feat.geometry()
            ratio = None if geom.isEmpty() else self._ratio_of(geom)
            if ratio is not None and ratio > ratio_limit:
                yield seen, feat.id(), ratio
            else:
                yield seen, None, None

    @staticmethod
    def _ratio_of(geom) -> float | None:
        """长宽比（长边 / 短边）；退化几何等算不出来时返回 ``None``。"""
        geom.convertToMultiType()
        obb = geom.orientedMinimumBoundingBox()
        if obb is None:
            return None
        poly = obb[0].asPolygon()
        if not poly or not poly[0]:
            return None
        pts = poly[0]
        if len(pts) < 2:
            return None
        e1 = math.hypot(pts[1].x() - pts[0].x(), pts[1].y() - pts[0].y())
        e2 = math.hypot(pts[2].x() - pts[1].x(), pts[2].y() - pts[1].y())
        if e2 == 0 or e1 == 0:
            return None
        return e1 / e2 if e1 > e2 else e2 / e1

    def run(self, p: CheckParam) -> CheckResult:
        if p.layer is None or not p.layer.isValid():
            return CheckResult(
                status=CheckStatus.FAIL,
                severity=CheckSeverity.ERROR,
                info={"error": "layer 无效"},
            )
        ratio_limit = self._ratio(p)
        failed: List[int] = []
        ratios: dict[int, float] = {}
        for _seen, fid, ratio in self.iter_failed(p):
            if fid is not None:
                failed.append(fid)
                ratios[fid] = ratio

        return CheckResult(
            status=CheckStatus.PASS if not failed else CheckStatus.FAIL,
            severity=CheckSeverity.WARNING if failed else CheckSeverity.INFO,
            failed_feature_ids=failed,
            info={"type_id": self.type_id, "ratios": ratios, "limit": ratio_limit},
        )
