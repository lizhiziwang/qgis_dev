"""补缝：把多选相邻面之间的**封闭**缝隙并入指定的目标面。

**流程**：先用选择工具选中若干相邻面 → 点编辑工具条「补缝」→ 本模块检出这些
面之间被完全包围的缝隙 → 弹 :class:`~app.ui.dialogs.gap_repair.GapRepairDialog`
让用户为每处缝隙指定并入哪个参与面 → 一次性写回。

纯几何判定在 :mod:`app.core.domain.gap_repair`（域层不得依赖 UI，而面积字段
重算 :func:`app.ui.tools.feature_attrs.refresh_area_fields` 在 UI 层，故图层
编排放这里，与 :mod:`app.ui.tools.explode_parts` 同样的分工）。

全部改动包进**一个** ``beginEditCommand("补缝")``，Ctrl+Z 一次撤销整批。按仓库
惯例：改完清空间索引缓存（``clear_spatial_index``），画布刷新由调用方（工具条）
用 ``QTimer.singleShot(0, ...)`` 推迟执行。
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass

from qgis.core import Qgis, QgsGeometry, QgsVectorLayer, QgsWkbTypes
from qgis.PyQt.QtWidgets import QDialog

from app.core.domain.check.space_index import clear_spatial_index
from app.core.domain.gap_repair import Gap, detect_gaps, merge_gap_into
from app.infra.logging import get_logger
from app.ui.tools.feature_attrs import refresh_area_fields

logger = get_logger(__name__)


@dataclass(frozen=True)
class GapTarget:
    """一处检出缝隙，及可作目标的要素 fid（即围成它的参与面）。"""

    gap: Gap
    #: 参与围成该缝隙的要素 fid（升序）——只有它们能作为并入目标
    target_fids: tuple[int, ...]


def collect_gap_targets(
    layer: QgsVectorLayer,
    fids: Sequence[int],
) -> list[GapTarget]:
    """读取 ``fids`` 的几何并检出其中的封闭缝隙。

    贡献者下标由 :func:`~app.core.domain.gap_repair.detect_gaps` 按**传入序列**
    给出，这里保持传参顺序不变，直接映射回 fid。
    """
    ordered = [fid for fid in fids if layer.getFeature(fid).isValid()]
    geometries: list[QgsGeometry] = []
    kept_fids: list[int] = []
    for fid in ordered:
        geom = layer.getFeature(fid).geometry()
        if geom is None or geom.isNull() or geom.isEmpty():
            continue
        geometries.append(geom)
        kept_fids.append(fid)
    if len(geometries) < 2:
        return []

    gaps = detect_gaps(geometries, crs=layer.crs())
    return [
        GapTarget(
            gap=gap,
            target_fids=tuple(kept_fids[i] for i in gap.contributor_indices),
        )
        for gap in gaps
    ]


def apply_gap_repair(
    layer: QgsVectorLayer | None,
    targets: Sequence[GapTarget],
    assignments: Sequence[tuple[int, int]],
    *,
    hint: Callable[[str], None] | None = None,
) -> tuple[int, int] | None:
    """按 ``assignments`` 执行补缝。

    :param layer: 目标矢量图层（须处于编辑态）
    :param targets: :func:`collect_gap_targets` 的检出结果
    :param assignments: ``[(缺口序号, 目标 fid), ...]``；未列入的缝隙不处理
    :param hint: 可选状态提示回调
    :return: ``(被改动的要素数, 填补的缝隙数)``；无编辑态 / 全部失败返回
        ``None``，无可做之事返回 ``(0, 0)``。**不会开空的编辑命令**
        （``beginEditCommand`` 会截断 redo 历史，无操作也开会白白破坏撤销历史）。
    """
    say = hint or (lambda text: None)
    if layer is None or layer.type() != layer.VectorLayer:
        return None
    if not layer.isEditable():
        say("图层未处于编辑态，无法补缝")
        return None
    if not assignments:
        return (0, 0)

    by_index = {item.gap.index: item for item in targets}
    # 目标 fid → 要并入它的缺口几何（同一目标的多处缝隙一次求并、写一次）
    plan: dict[int, list[QgsGeometry]] = {}
    for gap_index, target_fid in assignments:
        item = by_index.get(gap_index)
        if item is None:
            logger.warning("补缝：忽略未知缺口序号 %s", gap_index)
            continue
        if target_fid not in item.target_fids:
            logger.warning("补缝：要素 %s 未参与围成缺口 %s，已忽略", target_fid, gap_index)
            continue
        plan.setdefault(target_fid, []).append(item.gap.geometry)
    if not plan:
        return (0, 0)

    # 几何先算完再开编辑命令：算不出来的直接跳过，避免开了命令却没内容
    multi = QgsWkbTypes.isMultiType(layer.wkbType())
    work: list[tuple[int, QgsGeometry, int]] = []
    skipped = 0
    for fid, gap_geoms in plan.items():
        feature = layer.getFeature(fid)
        if not feature.isValid():
            logger.warning("补缝：要素 %s 已失效，跳过", fid)
            skipped += len(gap_geoms)
            continue
        merged = merge_gap_into(feature.geometry(), gap_geoms)
        if merged is None:
            skipped += len(gap_geoms)
            continue
        if multi and not merged.isMultipart():
            # convertToMultiType() 就地转换、返回 bool 成功标志——不能拿返回值赋值
            merged.convertToMultiType()
        work.append((fid, merged, len(gap_geoms)))

    if not work:
        logger.warning("补缝：%d 处缝隙均无法并入目标面", skipped)
        say("补缝失败：缝隙与目标面未能合并（详见日志）")
        return None

    changed = 0
    filled = 0
    layer.beginEditCommand("补缝")
    try:
        for fid, merged, count in work:
            feature = layer.getFeature(fid)
            layer.changeGeometry(fid, merged)
            if feature.isValid():
                attrs = list(feature.attributes())
                before = list(attrs)
                refreshed = refresh_area_fields(layer, attrs, merged)
                for idx, value in enumerate(refreshed):
                    if before[idx] != value:
                        layer.changeAttributeValue(fid, idx, value)
            changed += 1
            filled += count
    except RuntimeError:
        layer.destroyEditCommand()
        logger.exception("补缝失败: layer=%s", layer.name())
        say("补缝失败：图层编辑 API 异常")
        return None
    layer.endEditCommand()

    # 几何已变，空间索引缓存失效 → 清掉让下次命中重建
    clear_spatial_index(layer)
    try:
        # 显式 SetSelection：本构建 select() 是追加语义，不能用来替换选中
        layer.selectByIds(
            [fid for fid, _, _ in work], Qgis.SelectBehavior.SetSelection
        )
    except (RuntimeError, AttributeError):
        logger.warning("补缝后选中结果要素失败（不影响补缝结果）", exc_info=True)
    if skipped:
        logger.info("补缝：跳过 %d 处无法并入的缝隙", skipped)
    return (changed, filled)


def repair_gaps_interactive(
    layer: QgsVectorLayer | None,
    canvas=None,
    parent=None,
    *,
    hint: Callable[[str], None] | None = None,
) -> tuple[int, int] | None:
    """检出 → 弹窗分配目标面 → 执行。

    :param canvas: 画布；传入后弹窗会在用户选目标面时于地图上高亮闪烁该要素
    :return: ``(改动要素数, 填补缝隙数)``；用户取消 / 执行失败返回 ``None``；
        没检出缝隙返回 ``(0, 0)``（不弹窗）。
    """
    say = hint or (lambda text: None)
    if layer is None or layer.type() != layer.VectorLayer:
        return None
    if not layer.isEditable():
        say("图层未处于编辑态，无法补缝")
        return None

    targets = collect_gap_targets(layer, list(layer.selectedFeatureIds()))
    if not targets:
        return (0, 0)

    # 延迟 import：对话框只在真正有缝隙时才需要，也避免 UI 模块间的循环引用
    from app.ui.dialogs.gap_repair import GapRepairDialog

    dialog = GapRepairDialog(layer, targets, canvas=canvas, parent=parent)
    if dialog.exec() != QDialog.DialogCode.Accepted:
        return None
    return apply_gap_repair(layer, targets, dialog.assignments(), hint=hint)


__all__ = [
    "GapTarget",
    "apply_gap_repair",
    "collect_gap_targets",
    "repair_gaps_interactive",
]
