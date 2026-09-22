"""空间质检：面几何角（面内夹角过小）。

对每个面要素遍历其**所有环**（外环 + 内环）的顶点，计算「面内夹角」——即多边形
内部一侧的夹角（内环顶点取补角，因为多边形内部在洞的外侧）；只要**存在任一夹角
小于**「几何角度」阈值（``extra["angle"]``，默认 30°）就判为不合格。阈值 ≤ 0
表示不启用该判据。

夹角不在地理坐标系（经纬度）上直接算——那样会严重变形——而是先把要素转到
**江西区域 Albers 等积投影**（量算口径见 :mod:`app.core.domain.check.builtin.measure`）
再取平面夹角。重合点造成的退化边（零长度）无法定义夹角，直接跳过。
"""

from __future__ import annotations

import math
from typing import TYPE_CHECKING

from app.core.domain.check.builtin.measure import make_transform, measuring_geometry
from app.core.domain.check.param import CheckParam
from app.core.domain.check.registry import BaseCheck, CheckResult, CheckSeverity, CheckStatus, register

if TYPE_CHECKING:
    from qgis.core import QgsGeometry, QgsVectorLayer

#: 默认「几何角度」阈值（度）：面内夹角小于该值即判为不合格
DEFAULT_ANGLE = 30.0


@register("space_angle")
class SpaceAngleCheck(BaseCheck):
    """检查 layer 内是否存在面内夹角过小（尖角）的面要素。"""

    def build_where(self, p: CheckParam) -> str:
        return p.where or "1=1"

    # ------------------------------------------------------------ 参数解析

    @staticmethod
    def _limit(p: CheckParam) -> float:
        """取「几何角度」阈值（度）；非法值退回默认值。"""
        extra = p.extra if isinstance(p.extra, dict) else {}
        try:
            return float(extra.get("angle", DEFAULT_ANGLE))
        except (TypeError, ValueError):
            return DEFAULT_ANGLE

    # ------------------------------------------------------------ 夹角

    @staticmethod
    def _min_ring_angle(ring, is_hole: bool) -> float | None:
        """单个环内最小的「面内夹角」（度）；点数不足 / 全退化时返回 ``None``。

        ``is_hole`` 为真时取补角：洞的顶点处，多边形内部在环的外侧。
        """
        # ``asPolygon()`` / ``asMultiPolygon()`` 给出的是 ``QgsPointXY`` 列表；
        # 若直接传入 ``QgsCurve`` 环（如 ``exteriorRing()``）则取其点序列。
        points = ring.points() if hasattr(ring, "points") else ring
        # 坐标非有限值（投影域外会转成 inf）时整环放弃，避免算出假夹角
        if not all(math.isfinite(pt.x()) and math.isfinite(pt.y()) for pt in points):
            return None
        pts = [(pt.x(), pt.y()) for pt in points]
        if len(pts) >= 2 and pts[0] == pts[-1]:
            pts.pop()  # 去掉首尾重合点，下面按环形取前后顶点
        count = len(pts)
        if count < 3:
            return None

        # 有向面积定环的朝向：>0 为逆时针（QGIS 规范化后的外环朝向）
        twice_area = 0.0
        for i in range(count):
            x1, y1 = pts[i]
            x2, y2 = pts[(i + 1) % count]
            twice_area += x1 * y2 - x2 * y1
        ccw = twice_area > 0

        best: float | None = None
        for i in range(count):
            px, py = pts[i]
            x0, y0 = pts[i - 1]
            x1, y1 = pts[(i + 1) % count]
            in_x, in_y = px - x0, py - y0  # 入边方向
            out_x, out_y = x1 - px, y1 - py  # 出边方向
            if (in_x == 0 and in_y == 0) or (out_x == 0 and out_y == 0):
                continue  # 重合点造成的退化边，夹角无意义
            ax, ay = x0 - px, y0 - py  # 顶点 → 上一顶点
            bx, by = x1 - px, y1 - py  # 顶点 → 下一顶点
            cos_v = (ax * bx + ay * by) / (math.hypot(ax, ay) * math.hypot(bx, by))
            if not math.isfinite(cos_v):
                continue  # 坐标过大造成溢出，该顶点夹角不可信
            theta = math.degrees(math.acos(max(-1.0, min(1.0, cos_v))))
            # 左转 + 逆时针 = 凸顶点，夹角即面内夹角；否则取补角
            left_turn = (in_x * out_y - in_y * out_x) > 0
            inner = theta if left_turn == ccw else 360.0 - theta
            if is_hole:
                inner = 360.0 - inner
            if best is None or inner < best:
                best = inner
        return best

    @classmethod
    def _min_interior_angle(cls, geom: QgsGeometry) -> float | None:
        """面要素内最小的「面内夹角」（度）；非面 / 点数不足时返回 ``None``。"""
        from qgis.core import Qgis  # 延迟 import：纯 Python 单测无需 qgis

        # 只处理面（MultiPolygon 的 type() 同样是 Polygon）；线 / 点直接跳过
        if geom.type() != Qgis.GeometryType.Polygon:
            return None
        parts = geom.asMultiPolygon() if geom.isMultipart() else [geom.asPolygon()]
        best: float | None = None
        for rings in parts:
            if not rings:
                continue
            for index, ring in enumerate(rings):  # 第 0 个是外环，其余是内环
                angle = cls._min_ring_angle(ring, is_hole=index > 0)
                if angle is not None and (best is None or angle < best):
                    best = angle
        return best

    # ------------------------------------------------------------ 流式执行

    def iter_failed(self, p: CheckParam):
        """流式产出命中要素 ``(seen, fid, angle)``。

        ``seen`` 是累计已处理要素数（含未命中），供界面分批执行时更新进度；
        ``angle`` 是该面要素的最小面内夹角（度），供调用方参考。

        调用方需保证 ``p.layer`` 有效；无效图层直接不产出。
        """
        layer: QgsVectorLayer = p.layer
        if layer is None or not layer.isValid():
            return
        from qgis.core import QgsFeatureRequest  # 延迟 import：纯 Python 单测无需 qgis

        threshold = self._limit(p)
        transform = make_transform(layer.crs())
        req = QgsFeatureRequest().setFilterExpression(self.build_where(p))

        seen = 0
        for feat in layer.getFeatures(req):
            seen += 1
            geom = feat.geometry()
            if geom.isEmpty():
                continue
            angle = self._min_interior_angle(measuring_geometry(geom, transform))
            if angle is None:
                continue
            if threshold > 0 and angle < threshold:
                yield seen, feat.id(), angle

    def run(self, p: CheckParam) -> CheckResult:
        if p.layer is None or not p.layer.isValid():
            return CheckResult(
                status=CheckStatus.FAIL,
                severity=CheckSeverity.ERROR,
                info={"error": "layer 无效"},
            )
        threshold = self._limit(p)
        failed: list[int] = []
        angles: dict[int, float] = {}
        for _seen, fid, angle in self.iter_failed(p):
            failed.append(fid)
            angles[fid] = angle

        return CheckResult(
            status=CheckStatus.PASS if not failed else CheckStatus.FAIL,
            severity=CheckSeverity.WARNING if failed else CheckSeverity.INFO,
            failed_feature_ids=failed,
            info={"type_id": self.type_id, "angles": angles, "angle": threshold},
        )


__all__ = ["DEFAULT_ANGLE", "SpaceAngleCheck"]
