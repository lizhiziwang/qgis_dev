"""空间质检：自相交。

旧 :func:`component.check.SpaceDataCheck.layer_self_intersect` 行为：遍历
layer 要素，对每个 ``geom`` 调 :meth:`QgsGeometry.validateGeometry`，
命中 ``intersect / self-intersect / self intersection`` 即视为失败。
"""

from __future__ import annotations

from typing import TYPE_CHECKING, ClassVar

from app.core.domain.check.param import CheckParam
from app.core.domain.check.registry import BaseCheck, CheckResult, CheckSeverity, CheckStatus, register

if TYPE_CHECKING:
    from qgis.core import QgsGeometry, QgsVectorLayer


@register("space_self_intersect")
class SpaceSelfIntersectCheck(BaseCheck):
    """检查 layer 内要素是否自相交。"""

    #: ``validateGeometry`` 报文里判定"自相交"的关键字（沿用旧 ``component.check`` 口径）
    _KEYWORDS: ClassVar[tuple[str, ...]] = ("intersect", "self-intersect", "self intersection")

    def build_where(self, p: CheckParam) -> str:
        return p.where or "1=1"

    def _self_intersect_error(self, geom: QgsGeometry) -> str | None:
        """返回该几何第一条"自相交"类校验报文；不是自相交返回 ``None``。"""
        for err in geom.validateGeometry():
            msg = err.what()
            if any(k in msg for k in self._KEYWORDS):
                return msg
        return None

    def iter_failed(self, p: CheckParam):
        """流式产出命中要素 ``(seen, fid, msg)``。

        ``seen`` 是累计已处理要素数（含未命中），供界面分批执行时更新进度；
        ``msg`` 是该要素命中的几何校验报文（:meth:`QgsGeometry.validateGeometry` 原文）。

        调用方需保证 ``p.layer`` 有效；无效图层直接不产出。
        """
        layer: QgsVectorLayer = p.layer
        if layer is None or not layer.isValid():
            return
        from qgis.core import QgsFeatureRequest  # 延迟 import：纯 Python 单测无需 qgis

        req = QgsFeatureRequest().setFilterExpression(self.build_where(p))

        seen = 0
        for feat in layer.getFeatures(req):
            seen += 1
            geom: QgsGeometry = feat.geometry()
            if geom.isEmpty():
                continue
            msg = self._self_intersect_error(geom)
            if msg is not None:
                yield seen, feat.id(), msg

    def run(self, p: CheckParam) -> CheckResult:
        if p.layer is None or not p.layer.isValid():
            return CheckResult(
                status=CheckStatus.FAIL,
                severity=CheckSeverity.ERROR,
                info={"error": "layer 无效"},
            )
        failed: list[int] = []
        for _seen, fid, _msg in self.iter_failed(p):
            failed.append(fid)
        return CheckResult(
            status=CheckStatus.PASS if not failed else CheckStatus.FAIL,
            severity=CheckSeverity.ERROR if failed else CheckSeverity.INFO,
            failed_feature_ids=failed,
            info={"type_id": self.type_id, "count": len(failed)},
        )
