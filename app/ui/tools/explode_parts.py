"""分解要素：多部件要素 → 多个单部件要素。

一个要素由多个（几何上分离的）部分组成的情况很常见（合并过要素、分割过的
要素等），多数数据场景下需要把它们拆开分别管理。**分解流程**：先用选择工具
选中多部件要素 → 点编辑工具条「分解」。

实现：对每个选中要素的多部件几何，第一个部件**保留在原要素上**
（``changeGeometry``），其余部件逐个 ``addFeature`` 成新要素。新要素属性：
主键清空（交给数据源分配，防 GPKG ``UNIQUE constraint failed``）、FGUID
重新生成唯一值、面积 / 周长字段按部件几何重算，其余属性继承原始要素
（见 :mod:`app.ui.tools.feature_attrs`）；原要素只重算其过期面积类字段。

全部改动包进**一个** ``beginEditCommand("分解")``，Ctrl+Z 一次撤销整批。
按仓库惯例：分割后清空间索引缓存（``clear_spatial_index``），画布刷新由
调用方（工具条）用 ``QTimer.singleShot(0, ...)`` 推迟执行。
"""

from __future__ import annotations

from collections.abc import Callable

from qgis.core import QgsFeature, QgsGeometry, QgsVectorLayer

from app.core.domain.check.space_index import clear_spatial_index
from app.infra.logging import get_logger
from app.ui.tools.feature_attrs import new_feature_attributes, refresh_area_fields

logger = get_logger(__name__)


def explode_features(
    layer: QgsVectorLayer | None,
    hint: Callable[[str], None] | None = None,
) -> tuple[int, int] | None:
    """把 ``layer`` 当前选中的**多部件要素**分解为单部件要素。

    :param layer: 目标矢量图层（须处于编辑态；未编辑会直接返回 None）
    :param hint: 可选状态提示回调（失败原因 / 进度文案）
    :return: ``(被分解的要素数, 新增的要素数)``；无可分解对象（无选中 / 未编辑 /
        被选中者都不是多部件 / 执行失败）时返回 ``None``。任意场景都不会产生
        空的编辑命令（无可拆对象时完全不动编辑缓冲——``beginEditCommand`` 会
        截断 redo 历史，无操作也开命令会白白破坏撤销历史）。
    """
    say = hint or (lambda text: None)
    if layer is None or layer.type() != layer.VectorLayer:
        return None
    fids = list(layer.selectedFeatureIds())
    if not fids:
        say("请先用选择工具选中要分解的多部件要素")
        return None
    if not layer.isEditable():
        say("图层未处于编辑态，无法分解要素")
        return None

    # 先探测真正可拆的多部件要素：一个都没有就不开编辑命令（保持 redo 历史）
    candidates: list[int] = []
    for fid in fids:
        feature = layer.getFeature(fid)
        if not feature.isValid():
            continue
        geom = feature.geometry()
        if geom is None or geom.isNull() or geom.isEmpty() or not geom.isMultipart():
            continue
        if sum(1 for _ in geom.constParts()) < 2:
            continue
        candidates.append(fid)
    if not candidates:
        say("所选要素中没有多部件要素，无需分解")
        return (0, 0)

    changed_fids: list[int] = []
    new_fids: list[int] = []
    layer.beginEditCommand("分解")
    try:
        for fid in candidates:
            feature = layer.getFeature(fid)
            if not feature.isValid():
                continue
            geom = feature.geometry()
            if geom is None or geom.isNull() or geom.isEmpty():
                continue
            # 部件深拷贝：QgsGeometry(part) 会转移部件所有权导致 double-free
            # 段错误（仓库既定结论，见 merge_geometry.py），一律 fromWkt。
            parts = [QgsGeometry.fromWkt(p.asWkt()) for p in geom.constParts()]
            if len(parts) < 2:
                continue
            # 第一个部件留原要素，其余拆成新要素
            layer.changeGeometry(fid, parts[0])
            attrs = list(feature.attributes())
            before = list(attrs)
            refreshed = refresh_area_fields(layer, attrs, parts[0])
            for idx, value in enumerate(refreshed):
                if before[idx] != value:
                    layer.changeAttributeValue(fid, idx, value)
            for part in parts[1:]:
                new_feature = QgsFeature(layer.fields())
                new_feature.setGeometry(part)
                new_feature.setAttributes(new_feature_attributes(layer, refreshed, part))
                layer.addFeature(new_feature)
                new_fids.append(new_feature.id())
            changed_fids.append(fid)
    except RuntimeError:
        layer.destroyEditCommand()
        logger.exception("分解要素失败: layer=%s", layer.name())
        say("分解失败：图层编辑 API 异常")
        return None
    layer.endEditCommand()

    # 要素增改，空间索引缓存失效 → 清掉让下次命中重建
    clear_spatial_index(layer)
    # 分解产出（保留的原要素 + 新拆出的要素）选中高亮，便于确认
    try:
        layer.select([*changed_fids, *new_fids])
    except RuntimeError:
        logger.warning("分解后选中结果要素失败（不影响分解结果）", exc_info=True)
    return (len(changed_fids), len(new_fids))


__all__ = ["explode_features"]
