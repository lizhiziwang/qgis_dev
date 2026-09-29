"""合并：把选中的多个要素并成一个，属性保留「要并入的那个图斑」。

**流程**：先用选择工具选中 **2 个及以上**要素 → 点编辑工具条「合并」→ 弹
:class:`~app.ui.dialogs.merge_geometry.MergeGeometryDialog`：左侧列出选中的
要素、右侧显示当前高亮要素的全部属性（点列表项会在图上高亮闪烁，便于在密集
数据里定位到底选的是哪一块）→ 选定「要并入哪个图斑」后确认，一次性写回。

几何规则：

- **只合并相邻接的图形**——共享一段边或面积重叠（见 :func:`_adjacent`）。仅角
  对角相接、中间隔一条矢量化细缝、以及完全分离的，都**不算相邻**：前一版会
  用微膨胀缝合 / 凹包把它们硬填成一块，现在改为直接提示不能合并（细缝属于
  「补缝」要处理的情形，不该被合并顺手吞掉）；
- 选中图形可能分成好几组互不相邻的（例如 A B C 连成一片、D 在别处），只取
  **最大**的那一组来合并，其余在弹窗里标注「与其余不相邻，已排除」；
- 求并用 ``unaryUnion``（合并公共边界，**保留全部原始顶点、不做简化**）；相邻
  分组用同一套判据，保证「判为相邻」的输入一定并得成单块。

属性规则：

- 结果要素保留**目标图斑**的全部属性（含 ``mzguid`` 等唯一标识，不重新生成——
  合并出来的图斑在业务上仍是「要并入的那个」）；
- 面积 / 周长类字段按合并后的几何重算（:func:`~app.ui.tools.feature_attrs.refresh_area_fields`），
  目前按图层坐标单位直接取值（约定为**平方米**）；以后若要按配置换算单位，
  改 ``feature_attrs._geometry_value`` 一处即可。

安全约束（仓库既定结论，见 ``swipe.py`` docstring）：本模块不在 Qt 事件栈内调
``canvas.refresh()``，画布刷新由调用方（工具条）用 ``QTimer.singleShot(0, ...)``
推迟出栈后执行。

全部改动包进**一个** ``beginEditCommand("合并")``，Ctrl+Z 一次撤销整个合并。
"""

from __future__ import annotations

from collections.abc import Callable, Sequence

from qgis.core import Qgis, QgsGeometry, QgsVectorLayer
from qgis.PyQt.QtWidgets import QDialog

from app.core.domain.check.space_index import clear_spatial_index
from app.infra.logging import get_logger
from app.ui.tools.feature_attrs import refresh_area_fields

logger = get_logger(__name__)


# ==================================================================== 邻接判定


def _adjacent(a: QgsGeometry, b: QgsGeometry) -> bool:
    """两几何是否**相邻接**：共享一段边或面积重叠（并集仍是一块）。

    只在一个点上相接（角对角）不算相邻——那样的并集是多部件，合并出来还是
    两块；隔一条细缝同理（``distance > 0``）。判据直接用 ``unaryUnion`` 的结果
    形态，与合并本身用的是同一个运算，不用「边界求交长度 > 0」那一套：实测重
    结点化后的相邻边界 ``distance`` 为 0，但求交会被 GEOS 塌成点、``length()``
    算出 0，按共享边长判定会全军覆没。
    """
    if a is None or b is None:
        return False
    try:
        if a.isNull() or b.isNull() or a.isEmpty() or b.isEmpty():
            return False
        # 快筛：包围盒不相交 / 有间距 → 必然不相邻，省掉一次 union
        if not a.boundingBox().intersects(b.boundingBox()):
            return False
        if a.distance(b) > 0:
            return False
        merged = QgsGeometry.unaryUnion([a, b])
        return merged is not None and not merged.isEmpty() and not merged.isMultipart()
    except Exception:  # noqa: BLE001 —— 几何运算异常一律按不相邻处理
        logger.warning("邻接判定失败，按不相邻处理", exc_info=True)
        return False


def adjacency_groups(layer: QgsVectorLayer | None, fids: Sequence[int]) -> list[list[int]]:
    """把 ``fids`` 按「相邻接」关系分成连通分量（分量内两两可达）。

    单元素分量 = 与其余选中图形都不相接的孤立要素。几何读不出来（已删除 / 为空）
    的 fid 直接不出现在结果里。返回顺序沿用 ``fids`` 顺序，保证结果稳定。
    """
    if layer is None:
        return []
    order = list(dict.fromkeys(int(fid) for fid in fids))
    geometries: dict[int, QgsGeometry] = {}
    kept: list[int] = []
    for fid in order:
        feature = layer.getFeature(fid)
        if not feature.isValid():
            continue
        geom = feature.geometry()
        if geom is None or geom.isNull() or geom.isEmpty():
            continue
        geometries[fid] = geom
        kept.append(fid)
    if not kept:
        return []

    # 并查集：两两判定相邻，同组的直接合并
    parent = {fid: fid for fid in kept}

    def find(fid: int) -> int:
        while parent[fid] != fid:
            parent[fid] = parent[parent[fid]]  # 路径压缩
            fid = parent[fid]
        return fid

    for i, fid_a in enumerate(kept):
        for fid_b in kept[i + 1 :]:
            if find(fid_a) == find(fid_b):
                continue  # 已经连通，不必再算一次 union
            if _adjacent(geometries[fid_a], geometries[fid_b]):
                parent[find(fid_b)] = find(fid_a)

    groups: dict[int, list[int]] = {}
    for fid in kept:
        groups.setdefault(find(fid), []).append(fid)
    return list(groups.values())


def mergeable_group(
    layer: QgsVectorLayer | None, fids: Sequence[int]
) -> tuple[list[int], list[int]]:
    """挑出可合并的**一组**相邻要素，以及被排除的其余选中要素。

    选中图形分成多组互不相邻时取**最大**的那组（并列取先选中的那组，结果稳定
    可预期），其余作为「与其余不相邻」被排除，由调用方告知用户。

    :return: ``(可合并的相邻要素 fid, 被排除的 fid)``；没有任何一对相邻时返回
        ``([], 全部选中)``
    """
    order = list(dict.fromkeys(int(fid) for fid in fids))
    eligible = [g for g in adjacency_groups(layer, order) if len(g) >= 2]
    if not eligible:
        return ([], order)
    best = max(eligible, key=len)
    chosen = set(best)
    return (best, [fid for fid in order if fid not in chosen])


# ==================================================================== 几何


def merge_geometries(geometries: Sequence[QgsGeometry]) -> QgsGeometry | None:
    """多个几何求并成一块；无效几何忽略，全部无效返回 ``None``。

    ``unaryUnion`` 保形：相邻 / 重叠要素只合并公共边界、不简化顶点。调用方须
    保证输入两两相邻（见 :func:`_adjacent`），否则结果会是多部件——由
    :func:`merge_features` 拒绝写回，不会把「两块」当成一次合并存下来。
    """
    geoms = [
        g for g in geometries if g is not None and not g.isNull() and not g.isEmpty()
    ]
    if not geoms:
        return None
    merged = QgsGeometry.unaryUnion(geoms)
    if merged is None or merged.isEmpty():
        # 兜底：并集失败时退化为多几何收集，形状 100% 保留
        merged = QgsGeometry.collectGeometry(geoms)
    if merged is None or merged.isEmpty():
        return None
    return merged


# ==================================================================== 写回


def merge_features(
    layer: QgsVectorLayer | None,
    target_fid: int,
    source_fids: Sequence[int],
    *,
    hint: Callable[[str], None] | None = None,
) -> int | None:
    """把 ``source_fids`` 的几何并进 ``target_fid``，并删掉这些源要素。

    **要求传入的要素两两相邻接**（共享边 / 重叠）：并集是多部件时直接拒绝写回，
    不会把「互不相接的几块」当成一次合并存下来。正常入口是
    :func:`merge_features_interactive`（先分组再调用本函数），直接调用本函数请
    自行保证这一点。

    :param layer: 目标矢量图层（须处于编辑态）
    :param target_fid: 「要并入的图斑」——合并结果保留它的**全部属性**
    :param source_fids: 参与合并的要素（可含 ``target_fid``，会自动去重）
    :param hint: 可选状态提示回调
    :return: 参与合并的要素个数（``>= 2``）；无编辑态 / 几何不可用 / 非相邻 /
        写回失败返回 ``None``。**不会开空的编辑命令**（``beginEditCommand`` 会
        截断 redo 历史，无操作也开会白白破坏撤销历史）。
    """
    say = hint or (lambda text: None)
    if layer is None or layer.type() != layer.VectorLayer:
        return None
    if not layer.isEditable():
        say("图层未处于编辑态，无法合并")
        return None

    # 目标排最前并去重；几何读不出来（要素已删 / 几何为空）的直接跳过
    ordered = list(dict.fromkeys([int(target_fid), *(int(fid) for fid in source_fids)]))
    used_fids: list[int] = []
    geoms: list[QgsGeometry] = []
    for fid in ordered:
        feature = layer.getFeature(fid)
        if not feature.isValid():
            logger.warning("合并：要素 %s 已失效，跳过", fid)
            continue
        geom = feature.geometry()
        if geom is None or geom.isNull() or geom.isEmpty():
            logger.warning("合并：要素 %s 几何为空，跳过", fid)
            continue
        used_fids.append(fid)
        geoms.append(geom)
    delete_fids = [fid for fid in used_fids if fid != target_fid]
    if len(geoms) < 2 or not delete_fids:
        say("合并失败：可用要素不足两个")
        return None

    merged = merge_geometries(geoms)
    if merged is None:
        logger.warning("合并：几何求并失败，参与要素 %s", used_fids)
        say("合并失败：要素几何无法求并（详见日志）")
        return None
    if merged.isMultipart():
        # 相邻校验（mergeable_group）通过后仍多部件：调用方绕过了校验，或 GEOS
        # 拓扑异常。这里拒绝而不是硬填间隙——填出来的形状不是用户选的那些图斑。
        logger.warning("合并：要素并非相邻接（并集为多部件），已拒绝: %s", used_fids)
        say("合并失败：所选要素之间并非相邻接（不共享边界）")
        return None

    # 结果要素属性继承目标图斑：只把面积 / 周长类字段按新几何重算
    target_attrs = list(layer.getFeature(target_fid).attributes())
    refreshed = refresh_area_fields(layer, list(target_attrs), merged)

    layer.beginEditCommand("合并")
    try:
        # 换几何 + 删其余要素：合成一个撤销步骤（目标 fid 不参与删除，两者互不影响）
        layer.changeGeometry(target_fid, merged)
        deleted = layer.deleteFeatures(delete_fids)
        if not deleted:
            # 源要素一个都没删掉（被外部删过 / 数据源拒绝）→ 提交下去就是
            # 「几何并了、源要素还在」的叠图半成品，宁可整体放弃
            layer.destroyEditCommand()
            logger.warning("合并：源要素未能删除，已放弃本次合并: %s", delete_fids)
            say("合并失败：要并入的要素已不存在（详见日志）")
            return None
        for idx, value in enumerate(refreshed):
            if idx < len(target_attrs) and target_attrs[idx] != value:
                layer.changeAttributeValue(target_fid, idx, value)
    except Exception:  # noqa: BLE001 —— 收宽：任何异常都必须关掉编辑命令
        # destroyEditCommand = 丢弃本次改动并关闭命令（PyQGIS 没有
        # cancelEditCommand，写了会在 except 里再抛 AttributeError，
        # 命令悬着 → 图层撤销栈永久失效）
        layer.destroyEditCommand()
        logger.exception("合并失败: layer=%s target=%s", layer.name(), target_fid)
        say("合并失败：图层编辑 API 异常")
        return None
    layer.endEditCommand()

    # 几何已变、要素已删，空间索引缓存失效 → 清掉让下次命中重建
    clear_spatial_index(layer)
    try:
        # 显式 SetSelection：本构建 select() 是追加语义，不能用来替换选中
        layer.selectByIds([target_fid], Qgis.SelectBehavior.SetSelection)
    except (RuntimeError, AttributeError):
        logger.warning("合并后选中结果要素失败（不影响合并结果）", exc_info=True)
    return len(used_fids)


def merge_features_interactive(
    layer: QgsVectorLayer | None,
    canvas=None,
    parent=None,
    *,
    hint: Callable[[str], None] | None = None,
) -> int | None:
    """弹窗选「要并入的图斑」→ 执行合并。

    只有**相邻接**的要素才谈得上合并（见 :func:`mergeable_group`）：选中图形里
    最大的那组相邻要素作为可合并的一组，其余在弹窗里标注「与其余不相邻，已排除」。

    :param canvas: 画布；传入后弹窗会在用户点选要素时于地图上高亮闪烁该要素
    :return: 参与合并的要素个数；选中的图形之间没有相邻接的（**不弹窗**）返回
        ``0``；用户取消 / 执行失败返回 ``None``
    """
    say = hint or (lambda text: None)
    if layer is None or layer.type() != layer.VectorLayer:
        return None
    if not layer.isEditable():
        say("图层未处于编辑态，无法合并")
        return None
    fids = list(layer.selectedFeatureIds())
    if len(fids) < 2:
        # 按钮可用性已挡住（选中不足即置灰），这里兜底防直接调用
        say("请先用选择工具选中两个及以上要合并的要素")
        return None

    group, excluded = mergeable_group(layer, fids)
    if len(group) < 2:
        # 一组相邻的都没有 → 不弹窗，由调用方给出明确提示
        say("选中的图形之间没有相邻接的，无法合并")
        return 0

    # 延迟 import：对话框只在真正要合并时才需要，也避免 UI 模块间的循环引用
    from app.ui.dialogs.merge_geometry import MergeGeometryDialog

    dialog = MergeGeometryDialog(
        layer, group, excluded=excluded, canvas=canvas, parent=parent
    )
    if dialog.exec() != QDialog.DialogCode.Accepted:
        return None
    target_fid = dialog.target_fid()
    if target_fid is None:
        return None
    return merge_features(layer, target_fid, group, hint=hint)


__all__ = [
    "adjacency_groups",
    "merge_features",
    "merge_features_interactive",
    "merge_geometries",
    "mergeable_group",
]
