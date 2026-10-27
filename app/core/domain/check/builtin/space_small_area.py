"""空间质检：细碎面（面积过小 / 形状细碎）。

对每个面要素按其「面积」判定，满足**任一**条件即视为细碎面：

- 面积小于「最小面积」阈值（默认 20 平方米）；
- 周长 / 面积比大于「周长面积比」阈值（默认 10 米/平方米），即形状过于细碎；
- 开启「多部件检查」时，几何为多部件。

面积来源由 ``extra["area_type"]`` 决定：``"geom"``（默认，几何面积）或
``"attr"``（取 ``extra["area_field"]`` 指定的数值字段值）。几何面积 / 周长
不直接在地理坐标系上量，而是先把要素转到**江西区域 Albers 等积投影**再取平面值，
统一的量算口径见 :mod:`app.core.domain.check.builtin.measure`。
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from app.core.domain.check.builtin.measure import (
    JIANGXI_ALBERS_WKT,
    jiangxi_albers_crs,
    make_ellipsoid_measure,
    make_transform,
    measure_area_perimeter,
)
from app.core.domain.check.param import CheckParam
from app.core.domain.check.registry import BaseCheck, CheckResult, CheckSeverity, CheckStatus, register

if TYPE_CHECKING:
    from qgis.core import QgsGeometry, QgsVectorLayer

#: 默认最小面积（平方米）
DEFAULT_MIN_AREA = 20.0

#: 默认周长面积比阈值（米/平方米）
DEFAULT_PA_RATIO = 10.0


@register("space_small_area")
class SpaceSmallAreaCheck(BaseCheck):
    """检查 layer 内是否存在面积过小 / 形状细碎的面要素。"""

    def build_where(self, p: CheckParam) -> str:
        return p.where or "1=1"

    # ------------------------------------------------------------ 参数解析

    @staticmethod
    def _num(p: CheckParam, key: str, default: float) -> float:
        v = p.extra.get(key, default) if isinstance(p.extra, dict) else default
        try:
            return float(v)
        except (TypeError, ValueError):
            return default

    def _limit(self, p: CheckParam) -> tuple[float, float, bool, str, str | None]:
        """拆出 ``(min_area, pa_ratio, check_multipart, area_type, area_field)``。"""
        extra = p.extra if isinstance(p.extra, dict) else {}
        min_area = self._num(p, "min_area", DEFAULT_MIN_AREA)
        pa_ratio = self._num(p, "pa_ratio", DEFAULT_PA_RATIO)
        check_multipart = bool(extra.get("check_multipart", False))
        area_type = str(extra.get("area_type", "geom") or "geom")
        area_field = extra.get("area_field")
        return min_area, pa_ratio, check_multipart, area_type, (str(area_field) if area_field else None)

    # ------------------------------------------------------------ 量算

    @staticmethod
    def _make_transform(crs):
        """源 CRS → 江西 Albers 的坐标转换（见 :mod:`...builtin.measure`）。"""
        return make_transform(crs)

    @staticmethod
    def _make_measure(crs):
        """椭球量算器（等积投影不可用时的退路）。"""
        return make_ellipsoid_measure(crs)

    @staticmethod
    def _measure_pair(geom: QgsGeometry, transform, measure) -> tuple[float, float]:
        """返回几何的 ``(面积, 周长)``（米² / 米）。

        优先转到江西 Albers 等积投影后取平面值（面积 = 真实面积）；转换失败 /
        投影不可用时退回椭球量算，最后再退回平面量算。
        """
        return measure_area_perimeter(geom, transform, measure)

    # ------------------------------------------------------------ 流式执行

    def iter_failed(self, p: CheckParam):
        """流式产出 ``(seen, fid, area)``：**每个处理过的要素都产出一条**。

        ``fid is None`` 表示该要素通过（仅用于推进进度），``fid`` 非空表示命中；
        ``area`` 是判定所用的面积（几何面积或属性值），通过记录里为 ``None``。
        每个要素都 yield 一次，界面才能在一遍扫描一个都不命中时也刷新进度。
        调用方需保证 ``p.layer`` 有效；无效图层直接不产出。
        """
        layer: QgsVectorLayer = p.layer
        if layer is None or not layer.isValid():
            return
        from qgis.core import QgsFeatureRequest  # 延迟 import：纯 Python 单测无需 qgis

        min_area, pa_ratio, check_multipart, area_type, area_field = self._limit(p)
        use_attr = area_type == "attr" and bool(area_field)
        transform = self._make_transform(layer.crs())
        measure = self._make_measure(layer.crs())
        req = QgsFeatureRequest().setFilterExpression(self.build_where(p))

        seen = 0
        for feat in layer.getFeatures(req):
            seen += 1
            geom = feat.geometry()
            if geom.isEmpty():
                yield seen, None, None
                continue

            # 几何面积 / 周长一次算好：面积按 ``area_type`` 取舍，周长只用于周长面积比
            geom_area, geom_perimeter = self._measure_pair(geom, transform, measure)
            area = 0.0
            if use_attr:
                try:
                    area = float(feat[area_field])
                except (TypeError, ValueError):
                    area = 0.0
            else:
                area = geom_area

            pa = 0.0
            if geom_area > 0:
                pa = geom_perimeter / geom_area

            failed = min_area > 0 and area < min_area
            if not failed and pa_ratio > 0 and pa > pa_ratio:
                failed = True
            if not failed and check_multipart and geom.isMultipart():
                failed = True
            if failed:
                yield seen, feat.id(), area
            else:
                yield seen, None, None

    def run(self, p: CheckParam) -> CheckResult:
        if p.layer is None or not p.layer.isValid():
            return CheckResult(
                status=CheckStatus.FAIL,
                severity=CheckSeverity.ERROR,
                info={"error": "layer 无效"},
            )
        min_area, pa_ratio, check_multipart, area_type, area_field = self._limit(p)
        failed: list[int] = []
        areas: dict[int, float] = {}
        for _seen, fid, area in self.iter_failed(p):
            if fid is not None:
                failed.append(fid)
                areas[fid] = area

        return CheckResult(
            status=CheckStatus.PASS if not failed else CheckStatus.FAIL,
            severity=CheckSeverity.WARNING if failed else CheckSeverity.INFO,
            failed_feature_ids=failed,
            info={
                "type_id": self.type_id,
                "areas": areas,
                "min_area": min_area,
                "pa_ratio": pa_ratio,
                "check_multipart": check_multipart,
                "area_type": area_type,
                "area_field": area_field,
            },
        )


# ``JIANGXI_ALBERS_WKT`` / ``jiangxi_albers_crs`` 已搬到 :mod:`...builtin.measure`，
# 这里继续对外导出，保持既有引用（含单测）不变。
__all__ = ["DEFAULT_MIN_AREA", "DEFAULT_PA_RATIO", "JIANGXI_ALBERS_WKT", "SpaceSmallAreaCheck", "jiangxi_albers_crs"]
