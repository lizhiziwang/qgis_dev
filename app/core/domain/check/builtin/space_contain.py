"""空间质检：跨边界面不相交（源图层要素须被参考图层完全包含）。

对应设计稿 ``resource/reeee/跨边界面不相交.png``：检查**源图层**（``p.layer``）
里的面要素是否都落在**参考图层**给出的范围之内。只要某个要素有一部分伸到参考
图层范围之外（跨过参考图层的边界），该要素就判为失败，否则视为「被参考图层完全
包含」。

参考图层的来源（``p.extra``）：

- ``reference_layer``：直接给 :class:`QgsVectorLayer` 对象（界面走这条）；
- ``reference_layer_id``：给工程内图层 id，运行时从
  :class:`QgsProject` 取（配置文件 / 后端任务走这条）。

两个图层各有自己的过滤条件：源图层用 :attr:`CheckParam.where`，参考图层用
``extra["reference_where"]``（为空 / ``"1=1"`` 表示不筛）。参考图层的过滤条件
在**取参考要素时就生效**，也就是“拿哪些参考要素拼参考范围”。

「参考图层范围」按**参考图层（筛选后）所有要素几何的并集**理解：源要素落在若干
参考要素的拼接范围里也算完全包含，不要求被单个参考要素包住。判定用
:meth:`QgsGeometry.contains`（源要素**贴边**不算跨边界，只有伸出边界才算），
超出部分面积用 :mod:`app.core.domain.check.builtin.measure` 的统一量算口径
（江西 Albers 等积投影 → 椭球）取米²，随流式结果一起产出。
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from app.core.domain.check.builtin.measure import make_ellipsoid_measure, make_transform, measure_area_perimeter
from app.core.domain.check.param import CheckParam
from app.core.domain.check.registry import BaseCheck, CheckResult, CheckSeverity, CheckStatus, register
from app.core.domain.check.space_index import build_spatial_index

if TYPE_CHECKING:
    from qgis.core import QgsGeometry, QgsSpatialIndex, QgsVectorLayer


@register("space_contain")
class SpaceContainCheck(BaseCheck):
    """检查 layer 内要素是否都被参考图层完全包含（无跨边界）。"""

    def build_where(self, p: CheckParam) -> str:
        return p.where or "1=1"

    @staticmethod
    def _reference_layer(p: CheckParam) -> QgsVectorLayer | None:
        """取参考图层：先 ``extra["reference_layer"]``，再按 id 到工程里找。"""
        extra = p.extra if isinstance(p.extra, dict) else {}
        ref = extra.get("reference_layer")
        if ref is None:
            ref_id = extra.get("reference_layer_id")
            if ref_id:
                from qgis.core import QgsProject  # 延迟 import：纯 Python 单测无需 qgis

                ref = QgsProject.instance().mapLayer(str(ref_id))
        return ref

    @staticmethod
    def _reference_where(p: CheckParam) -> str:
        """参考图层自己的过滤条件（``extra["reference_where"]``）：空则不筛。"""
        extra = p.extra if isinstance(p.extra, dict) else {}
        return str(extra.get("reference_where") or "").strip() or "1=1"

    @staticmethod
    def _reference_index(ref: QgsVectorLayer, where: str) -> QgsSpatialIndex | None:
        """取参考图层的空间索引（只含满足 ``where`` 的参考要素）。

        没过滤条件时复用工程缓存的整层索引（:func:`build_spatial_index`）；带过滤
        条件时得按表达式现建一份 —— 那份缓存只按图层存，装不下“不同表达式”。
        """
        if where == "1=1":
            return build_spatial_index(ref)
        from qgis.core import QgsFeatureRequest, QgsSpatialIndex  # 延迟 import

        index = QgsSpatialIndex()
        request = QgsFeatureRequest().setFilterExpression(where).setNoAttributes()
        index.addFeatures(
            [f for f in ref.getFeatures(request) if f.hasGeometry()]
        )
        return index

    @staticmethod
    def _reference_geometry_cache(ref: QgsVectorLayer, where: str) -> dict[int, "QgsGeometry"]:
        """一次性把参考要素的**几何**读进内存（只取几何、不取属性）。

        逐个 ``getFeature`` 回读会带上 100+ 个属性列；14 万参考要素实测要几十秒。
        这里用 ``setNoAttributes()`` 批量读一遍（同规模 ~1s），后续按 fid 直接取几何。
        """
        from qgis.core import QgsFeatureRequest  # 延迟 import

        request = QgsFeatureRequest().setNoAttributes()
        if where and where != "1=1":
            request.setFilterExpression(where)
        cache: dict[int, QgsGeometry] = {}
        for feat in ref.getFeatures(request):
            geom = feat.geometry()
            if geom is not None and not geom.isEmpty():
                cache[feat.id()] = geom
        return cache

    @staticmethod
    def _reference_union(
        index: QgsSpatialIndex,
        cache: dict[int, QgsGeometry],
        bbox,
        geom: QgsGeometry | None = None,
    ) -> QgsGeometry | None:
        """取参考图层中与该范围相交的要素并集；范围内没有参考要素返回 ``None``。

        ``cache`` 一次性预载参考几何（见 :meth:`_reference_geometry_cache`），
        不再逐 fid 回 provider 读要素。

        ``geom`` 非空时先试"单个参考面包含源面"：两个相同图层（或同一版本数据）
        里每个要素一定能被它自己那个参考面包含，但把相邻的多块并成一个几何做
        ``contains`` 时，GEOS 会在公共边处产生零面积毛刺，导致 identical 几何也被
        判成不包含（用户报过"两个相同图层仍提示有结果"）。单面判定精确且更快，
        命中就直接返回。
        """
        from qgis.core import QgsGeometry  # 延迟 import

        parts = []
        for fid in index.intersects(bbox):
            geom_part = cache.get(fid)
            if geom_part is not None and not geom_part.isEmpty():
                parts.append(geom_part)
        if not parts:
            return None
        if geom is not None:
            for part in parts:
                if part.contains(geom):
                    return part
        return parts[0] if len(parts) == 1 else QgsGeometry.unaryUnion(parts)

    def iter_failed(self, p: CheckParam):
        """流式产出 ``(seen, fid, excess_area)``：**每个处理过的要素都产出一条**。

        ``fid is None`` 表示该要素通过（仅用于推进进度），``fid`` 非空表示命中；
        ``excess_area`` 是**超出参考图层范围**的面积（米²）。每个要素都 yield 一次，
        界面才能在一遍扫描一个都不越界时也刷新进度。
        调用方需保证 ``p.layer`` 与参考图层都有效；任一无效直接不产出。
        """
        layer: QgsVectorLayer = p.layer
        ref = self._reference_layer(p)
        if layer is None or not layer.isValid() or ref is None or not ref.isValid():
            return
        from qgis.core import QgsFeatureRequest  # 延迟 import：纯 Python 单测无需 qgis

        index = self._reference_index(ref, self._reference_where(p))
        if index is None:
            return
        # 参考几何一次批量读进内存（只取几何）：后面每个源要素都要按 fid 取候选，
        # 逐个 getFeature 会带着 180+ 属性列反复回读 provider，是大头开销。
        cache = self._reference_geometry_cache(ref, self._reference_where(p))
        transform = make_transform(layer.crs())
        measure = make_ellipsoid_measure(layer.crs())
        req = QgsFeatureRequest().setFilterExpression(self.build_where(p))

        seen = 0
        for feat in layer.getFeatures(req):
            seen += 1
            geom: QgsGeometry = feat.geometry()
            if geom.isEmpty():
                yield seen, None, 0.0
                continue

            covered = self._reference_union(index, cache, geom.boundingBox(), geom)
            if covered is not None and covered.contains(geom):
                yield seen, None, 0.0
                continue
            # 参考范围内一个要素都没有时，整个要素都算超出
            outside = geom if covered is None else geom.difference(covered)
            excess = 0.0
            if not outside.isEmpty():
                excess = measure_area_perimeter(outside, transform, measure)[0]
            yield seen, feat.id(), excess

    def run(self, p: CheckParam) -> CheckResult:
        if p.layer is None or not p.layer.isValid():
            return CheckResult(
                status=CheckStatus.FAIL,
                severity=CheckSeverity.ERROR,
                info={"error": "layer 无效"},
            )
        if self._reference_layer(p) is None:
            return CheckResult(
                status=CheckStatus.FAIL,
                severity=CheckSeverity.ERROR,
                info={"error": "未指定参考图层"},
            )
        failed: list[int] = []
        for _seen, fid, _excess in self.iter_failed(p):
            if fid is not None:
                failed.append(fid)
        return CheckResult(
            status=CheckStatus.PASS if not failed else CheckStatus.FAIL,
            severity=CheckSeverity.ERROR if failed else CheckSeverity.INFO,
            failed_feature_ids=failed,
            info={"type_id": self.type_id, "count": len(failed)},
        )
