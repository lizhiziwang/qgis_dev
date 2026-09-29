"""空间质检：多部件（面要素几何含多个部分）。

设计稿 ``resource/reeee/面多部件检查.png`` 的说明文案："检查源图层有多个部分的面要素"。
判定口径：要素几何的部分数大于 1 即判为**多部件**（一个要素被 2 个及以上部分拼成，
通常需要拆成单部件要素）；部分数走 :meth:`QgsGeometry.constGet` 拿到的抽象几何的
``partCount()``（QGIS 3/4 都有；``QgsGeometry.numGeometries`` 在 4.x 已移除）。

注意这里**不用** :meth:`QgsGeometry.isMultipart`：它判的是 WKB 类型（``MultiPolygon``
即使只存 1 个部分也返回 ``True``），会把单部分的 MultiPolygon 误报成"多个部分"；
本检查要的是"真的由多块拼成"，所以按部分数算。
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from app.core.domain.check.param import CheckParam
from app.core.domain.check.registry import BaseCheck, CheckResult, CheckSeverity, CheckStatus, register

if TYPE_CHECKING:
    from qgis.core import QgsGeometry, QgsVectorLayer


@register("space_multipart")
class SpaceMultipartCheck(BaseCheck):
    """检查 layer 内要素几何是否由多个部分拼成（多部件）。"""

    def build_where(self, p: CheckParam) -> str:
        return p.where or "1=1"

    @staticmethod
    def _part_count(geom: QgsGeometry) -> int:
        """几何的部分数；非集合类型（单面 / 单线 / 单点）为 1。

        :meth:`QgsGeometry.constGet` 对空 / 空值几何返回 ``None``，此时算 0 部分。
        """
        abstract = geom.constGet()
        return int(abstract.partCount()) if abstract is not None else 0

    def iter_failed(self, p: CheckParam):
        """流式产出命中要素 ``(seen, fid, msg)``。

        ``seen`` 是累计已处理要素数（含未命中），供界面分批执行时更新进度；
        ``msg`` 是命中原因（``"3 个部分"`` 这样的部分数说明）。

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
            parts = self._part_count(geom)
            if parts > 1:
                yield seen, feat.id(), f"{parts} 个部分"

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
