"""要素分割：用**其它图层**上选中的要素，分割当前编辑图层的要素。

**用法**（两步，与需求一致）：

1. 在**非当前编辑图层**的其它图层上，用点选 / 框选选中与目标图斑相交的要素；
2. 点工具条「要素分割」→ 当前编辑图层中与这些要素相交的要素被切开。

覆盖四种组合（线切面 / 线切线 / 面切面 / 面切线）靠一个统一原语：把切割要素
一律化成**边界曲线**——线要素取其本身，面要素取**全部环**（外环 + 内环）——
再逐条喂给 :meth:`QgsGeometry.splitGeometry`。面切面时"切割面的边界"正是把
目标切成"面内 / 面外"两块的那条线，与 QGIS 的 Split With Lines 同源。

关键实现约束（都已实测）：

- ``splitGeometry`` 必须传 ``list[QgsPoint]``。传 ``QgsLineString`` 会抛
  ``TypeError: First argument is not a list of points``——仓库里 split_feature.py
  的注释写的是「要传 QgsLineString」，但与它自己的可用代码相反，以代码为准。
- 曲线在**图层 CRS** 下构建（与目标几何同 CRS），画布 CRS 不同时先换算。
- 新块走 ``addFeature`` + :func:`app.ui.tools.feature_attrs.new_feature_attributes`
  （主键置空交给数据源分配、FGUID 重生成、面积重算），与
  :mod:`app.ui.tools.split_polygon`「面分割」同一套写法。
- 全部改动包进**一个** ``beginEditCommand("要素分割")``，Ctrl+Z 一次撤销。
"""

from __future__ import annotations

from collections.abc import Callable, Sequence

from qgis.core import (
    Qgis,
    QgsCoordinateTransform,
    QgsFeature,
    QgsGeometry,
    QgsPoint,
    QgsProject,
    QgsVectorLayer,
)

from app.core.domain.check.space_index import clear_spatial_index
from app.infra.logging import get_logger
from app.ui.tools.feature_attrs import new_feature_attributes, refresh_area_fields

logger = get_logger(__name__)


def cutter_layers(edit_layer: QgsVectorLayer | None) -> list[QgsVectorLayer]:
    """工程里**除编辑图层外**、当前有选中要素的矢量图层（切割要素的来源）。"""
    project = QgsProject.instance()
    out: list[QgsVectorLayer] = []
    for layer in project.mapLayers().values():
        if not isinstance(layer, QgsVectorLayer) or not layer.isValid():
            continue
        if edit_layer is not None and layer.id() == edit_layer.id():
            continue
        if layer.selectedFeatureIds():
            out.append(layer)
    return out


def has_cutter_selection(edit_layer: QgsVectorLayer | None) -> bool:
    """「要素分割」的可用前提：其它图层上有选中的切割要素。"""
    return bool(cutter_layers(edit_layer))


def _curves_from_geometry(geometry: QgsGeometry) -> list[list[QgsPoint]]:
    """把几何化成可喂给 ``splitGeometry`` 的点列表集合。

    面 → 外环 + 各内环（各成一条闭合曲线）；线 → 其本身。多点 / 多部件逐个展开。
    """
    curves: list[list[QgsPoint]] = []
    for part in geometry.constParts():
        if hasattr(part, "exteriorRing"):
            ring = part.exteriorRing()
            if ring is not None:
                curves.append([QgsPoint(p.x(), p.y()) for p in ring.points()])
            for i in range(part.numInteriorRings()):
                inner = part.interiorRing(i)
                if inner is not None:
                    curves.append([QgsPoint(p.x(), p.y()) for p in inner.points()])
        elif hasattr(part, "points"):
            curves.append([QgsPoint(p.x(), p.y()) for p in part.points()])
    return [curve for curve in curves if len(curve) >= 2]


def collect_cutter_curves(
    edit_layer: QgsVectorLayer | None,
) -> tuple[list[list[QgsPoint]], list[QgsGeometry]]:
    """收集其它图层上选中要素的切割曲线，并换算到**编辑图层 CRS**。

    :return: ``(曲线点列表, 转换后的几何列表)``；后者用于判交，前者用于切分。
    """
    if edit_layer is None:
        return ([], [])
    target_crs = edit_layer.crs()
    project = QgsProject.instance()
    curves: list[list[QgsPoint]] = []
    geometries: list[QgsGeometry] = []

    for layer in cutter_layers(edit_layer):
        source_crs = layer.crs()
        needs_transform = (
            source_crs.isValid() and target_crs.isValid() and source_crs != target_crs
        )
        transform = None
        if needs_transform:
            try:
                transform = QgsCoordinateTransform(source_crs, target_crs, project)
            except Exception:  # noqa: BLE001 —— 转换建不起来就跳过该图层
                logger.warning("要素分割：构建 CRS 转换失败: %s", layer.name(), exc_info=True)
                continue

        for fid in layer.selectedFeatureIds():
            feature = layer.getFeature(fid)
            if not feature.isValid():
                continue
            geometry = feature.geometry()
            if geometry is None or geometry.isNull() or geometry.isEmpty():
                continue
            if transform is not None:
                geometry = QgsGeometry.fromWkt(geometry.asWkt())
                try:
                    geometry.transform(transform)
                except Exception:  # noqa: BLE001
                    logger.warning("要素分割：切割要素坐标转换失败 fid=%s", fid, exc_info=True)
                    continue
                if geometry.isEmpty():
                    continue
            found = _curves_from_geometry(geometry)
            if not found:
                continue
            curves.extend(found)
            geometries.append(geometry)
    return (curves, geometries)


def _split_geometry(
    geometry: QgsGeometry, curves: Sequence[Sequence[QgsPoint]]
) -> list[QgsGeometry]:
    """用 ``curves`` 逐条切分 ``geometry``，返回切出的块（未切动时长度为 1）。"""
    parts = [QgsGeometry.fromWkt(geometry.asWkt())]
    for curve in curves:
        next_parts: list[QgsGeometry] = []
        for part in parts:
            try:
                code, new_geoms, _pts = part.splitGeometry(curve, False, True)
            except (TypeError, RuntimeError):
                logger.warning("要素分割：单条曲线切分失败，跳过", exc_info=True)
                next_parts.append(part)
                continue
            next_parts.append(part)
            if code == Qgis.GeometryOperationResult.Success:
                for piece in new_geoms:
                    if piece is not None and not piece.isNull() and not piece.isEmpty():
                        next_parts.append(piece)
        parts = next_parts
    return [
        part for part in parts if part is not None and not part.isNull() and not part.isEmpty()
    ]


def _intersecting_fids(
    layer: QgsVectorLayer, geometries: Sequence[QgsGeometry]
) -> list[int]:
    """编辑图层里与任一 ``geometries`` 相交的要素 id（包围盒粗筛 + 精确判交）。"""
    if not geometries:
        return []
    boxes = [g.boundingBox() for g in geometries]
    out: list[int] = []
    for feature in layer.getFeatures():
        geometry = feature.geometry()
        if geometry is None or geometry.isNull() or geometry.isEmpty():
            continue
        bbox = geometry.boundingBox()
        for i, cutter in enumerate(geometries):
            if not bbox.intersects(boxes[i]):
                continue
            if geometry.intersects(cutter):
                out.append(feature.id())
                break
    return out


def split_by_selected_features(
    layer: QgsVectorLayer | None,
    hint: Callable[[str], None] | None = None,
) -> tuple[int, int] | None:
    """用其它图层选中的要素分割 ``layer`` 中与它们相交的要素。

    :return: ``(被切开的原要素数, 新产生的块数)``；未编辑 / 无切割要素 /
        全部失败返回 ``None``；有切割要素但没切开任何东西返回 ``(0, 0)``。
        **不会开空的编辑命令**（``beginEditCommand`` 会截断 redo 历史）。
    """
    say = hint or (lambda text: None)
    if layer is None or layer.type() != layer.VectorLayer:
        return None
    if not layer.isEditable():
        say("图层未处于编辑态，无法分割要素")
        return None

    curves, cutter_geoms = collect_cutter_curves(layer)
    if not curves:
        say("请先在**其它图层**上选中用于分割的要素（当前编辑图层自身不算）")
        return None

    targets = _intersecting_fids(layer, cutter_geoms)
    if not targets:
        say("编辑图层中没有与所选要素相交的图形，无需分割")
        return (0, 0)

    # 几何先算完再开编辑命令：一个都切不开时不开命令，免得白白截断 redo 历史
    plans: list[tuple[int, list, list[QgsGeometry]]] = []
    for fid in targets:
        feature = layer.getFeature(fid)
        if not feature.isValid():
            continue
        geometry = feature.geometry()
        if geometry is None or geometry.isNull() or geometry.isEmpty():
            continue
        parts = _split_geometry(geometry, curves)
        if len(parts) < 2:
            continue
        plans.append((fid, list(feature.attributes()), parts))

    if not plans:
        logger.info("要素分割：%d 个相交要素都没被切开", len(targets))
        say("所选要素没能切开任何图形（切割要素需真正穿过目标边界）")
        return (0, 0)

    changed = 0
    added = 0
    result_fids: list[int] = []
    layer.beginEditCommand("要素分割")
    try:
        for fid, attrs, parts in plans:
            # 第一块留在原要素上，其余拆成新要素
            layer.changeGeometry(fid, parts[0])
            before = list(attrs)
            refreshed = refresh_area_fields(layer, list(attrs), parts[0])
            for idx, value in enumerate(refreshed):
                if before[idx] != value:
                    layer.changeAttributeValue(fid, idx, value)
            result_fids.append(fid)
            for part in parts[1:]:
                new_feature = QgsFeature(layer.fields())
                new_feature.setGeometry(part)
                new_feature.setAttributes(new_feature_attributes(layer, refreshed, part))
                layer.addFeature(new_feature)
                result_fids.append(new_feature.id())
                added += 1
            changed += 1
    except RuntimeError:
        layer.destroyEditCommand()
        logger.exception("要素分割失败: layer=%s", layer.name())
        say("要素分割失败：图层编辑 API 异常")
        return None
    layer.endEditCommand()

    # 要素增改，空间索引缓存失效 → 清掉让下次命中重建
    clear_spatial_index(layer)
    try:
        # 显式 SetSelection：本构建 select() 是追加语义
        layer.selectByIds(result_fids, Qgis.SelectBehavior.SetSelection)
    except (RuntimeError, AttributeError):
        logger.warning("要素分割后选中结果要素失败（不影响分割结果）", exc_info=True)
    return (changed, added)


__all__ = [
    "collect_cutter_curves",
    "cutter_layers",
    "has_cutter_selection",
    "split_by_selected_features",
]
