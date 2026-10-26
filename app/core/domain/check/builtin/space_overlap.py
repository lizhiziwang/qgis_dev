"""空间质检：要素重叠（两两相交但非邻接）。

旧 :func:`component.check.SpaceDataCheck.layer_feature_cover` 行为：建空间
索引，按 fid 升序两两比对，``geom.intersects(t) and not geom.touches(t)``
即视为**重叠**（共用一条边、只碰一个点都算邻接，不算重叠）。

:meth:`SpaceOverlapCheck.iter_failed` 供「质检 → 面重叠」窗口分批执行：逐个源
要素扫空间索引候选，只要与任一其他要素重叠就产出该要素（同一要素重叠多块也只算
一次），消息给出重叠的要素个数；:meth:`SpaceOverlapCheck.run` 复用同一套判定，
批量跑的命中集合与流式跑一致。

注意：空间索引里的候选**不按** ``where`` 过滤 —— 过滤条件只决定"查哪些源要素"，
被判定的每个源要素仍会与图层里全部要素比对（沿用旧批量口径）。
"""

from __future__ import annotations

from collections.abc import Iterator
from typing import TYPE_CHECKING

from app.core.domain.check.param import CheckParam
from app.core.domain.check.registry import BaseCheck, CheckResult, CheckSeverity, CheckStatus, register
from app.core.domain.check.space_index import build_spatial_index

if TYPE_CHECKING:
    from qgis.core import QgsGeometry, QgsVectorLayer


@register("space_overlap")
class SpaceOverlapCheck(BaseCheck):
    """检查 layer 内是否存在"相交但非邻接"的要素对。"""

    def build_where(self, p: CheckParam) -> str:
        return p.where or "1=1"

    @staticmethod
    def _index(layer: QgsVectorLayer):
        """取（或建）图层空间索引；建不出来返回 ``None``。"""
        index = getattr(layer, "__spatial_index__", None)
        if index is None:
            index = build_spatial_index(layer)
        return index

    def _overlaps(self, layer: QgsVectorLayer, geom: QgsGeometry, fid: int) -> list[int]:
        """返回与 ``geom`` 重叠（相交且非邻接）的其他要素 fid（升序）。"""
        index = self._index(layer)
        if index is None:
            return []
        found: list[int] = []
        for other_fid in index.intersects(geom.boundingBox()):
            if other_fid == fid:
                continue
            target = layer.getFeature(other_fid).geometry()
            if target is None or target.isEmpty():
                continue
            if geom.intersects(target) and not geom.touches(target):
                found.append(other_fid)
        return sorted(found)

    def iter_failed(self, p: CheckParam) -> Iterator[tuple[int, int | None, str]]:
        """流式产出 ``(seen, fid, msg)``：**每个处理过的要素都产出一条**。

        ``fid is None`` 表示该要素通过（仅用于推进进度），``fid`` 非空表示命中；
        界面按 ``seen`` 与总数算百分比。每个要素都 yield 一次，界面才能在长时间
        扫描（一遍下来一个都不命中）时照样刷新进度，而不是卡在 1%。
        调用方需保证 ``p.layer`` 有效；无效图层直接不产出。
        """
        layer: QgsVectorLayer = p.layer
        if layer is None or not layer.isValid():
            return
        from qgis.core import QgsFeatureRequest  # 延迟 import：纯 Python 单测无需 qgis

        build_spatial_index(layer)
        req = QgsFeatureRequest().setFilterExpression(self.build_where(p))

        seen = 0
        for feat in layer.getFeatures(req):
            seen += 1
            geom = feat.geometry()
            if geom.isEmpty():
                yield seen, None, ""
                continue
            others = self._overlaps(layer, geom, feat.id())
            if others:
                yield seen, feat.id(), f"与 {len(others)} 个要素重叠"
            else:
                yield seen, None, ""

    def run(self, p: CheckParam) -> CheckResult:
        if p.layer is None or not p.layer.isValid():
            return CheckResult(
                status=CheckStatus.FAIL,
                severity=CheckSeverity.ERROR,
                info={"error": "layer 无效"},
            )
        failed: list[int] = []
        for _seen, fid, _msg in self.iter_failed(p):
            if fid is not None:
                failed.append(fid)
        failed = sorted(failed)
        return CheckResult(
            status=CheckStatus.PASS if not failed else CheckStatus.FAIL,
            severity=CheckSeverity.ERROR if failed else CheckSeverity.INFO,
            failed_feature_ids=failed,
            info={"type_id": self.type_id, "count": len(failed)},
        )


__all__ = ["SpaceOverlapCheck"]
