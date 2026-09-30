"""复制到：把源图层上选中的要素复制到另一个图层（字段属性可按映射表搬运）。

**流程**（对齐 B/S 端 ``CopyToModal.vue`` + ``copyFeaturesUtil.ts``）：

1. 先用选择工具选中要素 → 点编辑工具条「复制到」→ 弹
   :class:`~app.ui.dialogs.copy_to_layer.CopyToLayerDialog`：选目标图层、勾选
   「复制要素的同时复制指定字段属性」、逐字段指定「源字段 → 目标字段」映射
   （或点「自动匹配同名字段」）；
2. 确认后按**固定顺序**校验（顺序与 B/S 一致，先把最该拦的拦下来）：

   - **重叠校验**：源选中要素之间自重叠、或与目标图层已有图斑面积相交 → 拒绝；
   - **字段值规则**：字符串超目标字段长度、含 ``<`` ``>`` 危险字符 → 拒绝；
   - 【预留】必填 / 字典值校验 —— 见 :func:`_validate_business_fields`；
   - 【预留】操作范围 / 权限校验 —— 见 :func:`_validate_permission`。

3. 校验过了写进**目标图层的编辑缓冲**：自动 ``startEditing``（没开的话），全部
   改动包进**一个** ``beginEditCommand("复制到")``，Ctrl+Z 一次撤销整批。**不自动
   提交** —— 用户看过没问题再点「保存编辑」。

**属性规则**：**映射表管到的字段按映射写，没管到的走自动赋值规则**。

- 主键字段一律置空（交给数据源分配，防 UNIQUE 冲突）；
- 唯一标识字段（MZGUID / FGUID…）：**映射了源图层的这个字段且源值非空 → 用源值**，
  否则生成新 ``uuid4``（"复制到"常常就是要延续源图斑的身份，此时不该换号）；
- 面积 / 周长类字段：映射了就用映射值，没映射才按新几何重算；
- 其余未映射字段留空。

跨坐标系时几何会显式换算到**目标图层 CRS**（B/S 由 ArcGIS 代劳，桌面端得自己做）。
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass, field

from qgis.core import (
    QgsCoordinateTransform,
    QgsFeature,
    QgsGeometry,
    QgsProject,
    QgsVectorLayer,
    QgsWkbTypes,
)
from qgis.PyQt.QtCore import QVariant

from app.core.domain.check.space_index import clear_spatial_index
from app.infra.logging import get_logger
from app.ui.tools.feature_attrs import (
    field_alias,
    refresh_area_fields,
    uid_field_index,
    uid_value_for,
)

logger = get_logger(__name__)

#: 目标字段里不参与映射的字段名（小写）——审核字段由目标图层的业务规则负责，
#: 不从别的图层搬过来（对齐 B/S 的 status / ck_status）
_EXCLUDED_TARGET_FIELDS = ("status", "ck_status")

#: 主键的常见列名（小写精确匹配）。排除主键主用 ``primaryKeyAttributes()`` 的下标，
#: 但那套**内存图层报不出来**（主键列压根不在 ``fields()`` 里，实测 ``[]``），
#: 所以再按列名补一道——B/S 也是按 ``objectid`` / ``fid`` 的名字排的。
_PK_FIELD_NAMES = ("fid", "objectid")

#: 面积 / 周长类字段名（小写精确匹配）：只用于给弹窗做提示，**不排除** —— 用户可以
#: 显式映射（比如把源图层的「林草面积」搬到目标图层的「面积」），映射了就按映射写
AREA_LIKE_FIELDS = ("mj", "bh_mian_ji", "txmj", "xbmj", "xb_mj", "tb_mj")


@dataclass(frozen=True)
class CopyToResult:
    """复制到的执行结果。"""

    ok: bool
    #: 失败原因（给用户看的一句话）；成功时为空
    reason: str = ""
    #: 实际写进目标图层的要素数
    added: int = 0
    #: 因校验失败被拦下的目标字段显示名（供调用方拼「【因子名】」提示）
    fields: list[str] = field(default_factory=list)


# ==================================================================== 字段筛选


def _is_pk_field(layer: QgsVectorLayer, idx: int, name: str) -> bool:
    """字段是不是主键：按下标（真实数据源）+ 按列名（内存图层兜底）。"""
    if idx in set(layer.primaryKeyAttributes()):
        return True
    return name.strip().lower() in _PK_FIELD_NAMES


def mappable_target_fields(layer: QgsVectorLayer) -> list:
    """目标图层里**可映射**的字段：排除主键与审核字段（对齐 B/S）。

    主键要交给数据源分配，映射过去没有意义（还可能撞 UNIQUE）；``status`` /
    ``ck_status`` 属于目标图层自己的审核流程，不该从别的图层搬。
    """
    out = []
    for idx, fld in enumerate(layer.fields()):
        if _is_pk_field(layer, idx, fld.name()):
            continue
        if fld.name().strip().lower() in _EXCLUDED_TARGET_FIELDS:
            continue
        out.append(fld)
    return out


def mappable_source_fields(layer: QgsVectorLayer) -> list:
    """源图层里可作**来源**的字段：只排除主键。

    唯一标识（MZGUID / FGUID）**保留可选** —— 复制到常常就是要延续源图斑的身份，
    映射过来且源值非空时按源值写（见 :func:`_finalize_attributes`）。
    """
    return [
        fld
        for idx, fld in enumerate(layer.fields())
        if not _is_pk_field(layer, idx, fld.name())
    ]


def types_compatible(source_field, target_field) -> bool:
    """两个字段的类型能不能直接搬值（对齐 B/S 的 `FIELD_TYPE_MAPPING` 思路）。

    ArcGIS 那边是把 ``esriFieldTypeXxx`` 与客户端类型归一后再比；这里同理，
    把 QVariant 类型归到「数值 / 文本 / 日期时间 / 布尔」几个族里比，族内互通
    （``small-integer`` 与 ``integer`` 在 B/S 里也当同一种），跨族不兼容。
    """
    return _type_family(source_field.type()) == _type_family(target_field.type())


def _type_family(qvariant_type) -> str:
    if qvariant_type in (
        QVariant.Int, QVariant.LongLong, QVariant.UInt, QVariant.ULongLong, QVariant.Double,
    ):
        return "number"
    if qvariant_type == QVariant.String:
        return "string"
    if qvariant_type in (QVariant.Date, QVariant.Time, QVariant.DateTime):
        return "datetime"
    if qvariant_type == QVariant.Bool:
        return "bool"
    return f"other:{qvariant_type}"


# ==================================================================== 校验（纯几何 / 纯字段）


def _polygon_overlap(a: QgsGeometry, b: QgsGeometry) -> bool:
    """两个面是否**面积相交**。

    只共享一条边（相邻）不算重叠：求交得到的是线、面积为 0，天然被排除——这也
    正是 B/S 里"先 intersects 查询、再用 touches 排除边界接触"在做的事，这里
    用面积一并判掉，少一次几何运算。
    """
    if a is None or b is None or a.isEmpty() or b.isEmpty():
        return False
    try:
        if not a.intersects(b):
            return False
        inter = a.intersection(b)
    except Exception:  # noqa: BLE001 —— 几何运算异常按"不重叠"放行，不误拦用户
        logger.warning("复制到：重叠判定几何运算失败", exc_info=True)
        return False
    if inter is None or inter.isEmpty():
        return False
    return inter.area() > 0.0


def check_self_overlap(geometries: Sequence[QgsGeometry]) -> bool:
    """源选中要素之间是否存在自重叠（只对面要素生效，与 B/S 一致）。"""
    polys = [g for g in geometries if g is not None and g.type() == QgsWkbTypes.PolygonGeometry]
    for i in range(len(polys)):
        for j in range(i + 1, len(polys)):
            if _polygon_overlap(polys[i], polys[j]):
                return True
    return False


def check_target_overlap(target_layer: QgsVectorLayer, geometries: Sequence[QgsGeometry]) -> bool:
    """待复制的面要素是否与目标图层**已有图斑**面积相交。"""
    from app.core.domain.check.query import intersect_features

    project = QgsProject.instance()
    for geom in geometries:
        if geom is None or geom.type() != QgsWkbTypes.PolygonGeometry:
            continue
        fids = intersect_features(target_layer, geom, target_layer.crs(), project)
        for fid in fids:
            feature = target_layer.getFeature(fid)
            if not feature.isValid():
                continue
            if _polygon_overlap(feature.geometry(), geom):
                return True
    return False


def validate_field_rules(
    source_layer: QgsVectorLayer,
    target_layer: QgsVectorLayer,
    features: Sequence[QgsFeature],
    field_mappings: Sequence[tuple[str, str]],
) -> tuple[list[str], list[str]]:
    """字段值规则校验：``(长度超限的字段名, 含危险字符的字段名)``。

    只校验本次映射涉及的字段（对齐 B/S：不映射的字段不参与）。字段名用**目标字段
    的显示名**（别名优先），方便用户对上界面里的那一列。
    """
    target_by_name = {fld.name(): fld for fld in target_layer.fields()}
    over_length: list[str] = []
    danger: list[str] = []
    for source_name, target_name in field_mappings:
        tgt = target_by_name.get(target_name)
        if tgt is None:
            continue
        display = field_alias(tgt) or tgt.name()
        limit = tgt.length()
        for feature in features:
            try:
                value = feature[source_name]
            except KeyError:
                continue
            if not isinstance(value, str) or value == "":
                continue
            if limit and len(value) > limit:
                if display not in over_length:
                    over_length.append(display)
                continue
            if "<" in value or ">" in value:
                if display not in danger:
                    danger.append(display)
    return over_length, danger


def _validate_business_fields(*_args, **_kwargs) -> list[str]:
    """【预留】**必填 / 字典值**校验（对应 B/S ``CopyFeaturesUtil.validateCopyToAttributes``）。

    桌面端暂时**没有对应数据源**：B/S 读的是服务端下发的目标图层字段业务配置
    （``editFieldsetOption.busAttributeFields`` 的 ``required`` / ``attribute``
    字典绑定 + ``busDictOption`` 字典表）。本项目的配置体系里没有这套东西，所以
    恒不拦截。

    后面要补时需要两样输入：
    1. 目标图层每个字段的 **required 标记**；
    2. 字段绑定的**字典表**（值 → 是否合法）与用例字典的加载方式。

    接入后行为（照 B/S）：``required`` 且任一待复制值为空 → 拦住，提示
    「存在未填写的必填项【字段名】」；配了字典且值非空但不在字典里 → 拦住，提示
    「存在错误字典值」。
    """
    return []


def _validate_permission(*_args, **_kwargs) -> str:
    """【预留】**操作范围 / 权限**校验（对应 B/S ``validateLimitRegionPermission``）。

    桌面端暂时**没有对应数据源**：B/S 要一个「操作范围」多边形
    （``layerOption.limitRegion``）+ 行政区服务（按县编码查县名，查不到还要用图形
    反查村、取村编码前 6 位当县码）。本项目里没有行政区数据，所以恒通过。

    后面要补时需要：操作范围几何 + 行政区/村级数据源。

    接入后行为（照 B/S）：源要素不在操作范围内 → 拦住，提示「无"XX县"的操作权限」。
    """
    return ""


# ==================================================================== 写回


def _finalize_attributes(
    target_layer: QgsVectorLayer,
    attributes: list,
    geometry: QgsGeometry,
    mapped_indices: set[int],
) -> list:
    """复制到的新要素属性收尾 —— **自动赋值只作用于映射表没管的字段**。

    - 主键：一律置空（无条件，映射表里也拿不到它）；
    - 唯一标识：映射了且值非空 → 保留源值；否则生成新 ``uuid4``；
    - 面积 / 周长：映射了就用映射值，没映射才按新几何重算。
    """
    attrs = list(attributes)
    pk = set(target_layer.primaryKeyAttributes())
    for idx in pk:
        if idx < len(attrs):
            attrs[idx] = None

    # 面积 / 周长：只在"没被映射管到"时才按新几何重算
    if not (mapped_indices & _area_indices(target_layer)):
        refresh_area_fields(target_layer, attrs, geometry)

    uid_idx = uid_field_index(target_layer.fields())
    if uid_idx is not None and uid_idx not in pk and uid_idx < len(attrs):
        existing = attrs[uid_idx]
        if existing is None or (isinstance(existing, str) and existing.strip() == ""):
            attrs[uid_idx] = uid_value_for(target_layer.fields().at(uid_idx))
    return attrs


def _area_indices(layer: QgsVectorLayer) -> set[int]:
    """面积 / 周长类字段的下标集合（复用 :func:`refresh_area_fields` 的同一套判定）。"""
    from app.ui.tools.feature_attrs import _field_kind

    return {
        idx
        for idx, fld in enumerate(layer.fields())
        if _field_kind(fld) is not None
    }


def copy_features_to(
    source_layer: QgsVectorLayer | None,
    target_layer: QgsVectorLayer | None,
    field_mappings: Sequence[tuple[str, str]] = (),
    copy_attributes: bool = False,
    *,
    hint: Callable[[str], None] | None = None,
) -> CopyToResult:
    """把 ``source_layer`` 上**选中的**要素复制到 ``target_layer``。

    :param field_mappings: ``[(源字段名, 目标字段名), ...]``；``copy_attributes``
        为 False 时忽略。
    :return: :class:`CopyToResult`；**不会**开空的编辑命令（无要素可复制时直接返回）。
    """
    say = hint or (lambda text: None)
    if source_layer is None or target_layer is None:
        return CopyToResult(False, "源图层或目标图层无效")
    if not source_layer.isValid() or not target_layer.isValid():
        return CopyToResult(False, "源图层或目标图层无效")
    if source_layer.id() == target_layer.id():
        return CopyToResult(False, "源图层与目标图层不能是同一个")
    if source_layer.geometryType() != target_layer.geometryType():
        return CopyToResult(False, "源图层与目标图层的几何类型不一致")

    fids = source_layer.selectedFeatureIds()
    if not fids:
        return CopyToResult(False, "请先选中要复制的要素")

    features: list[QgsFeature] = []
    source_geoms: list[QgsGeometry] = []
    for fid in fids:
        feature = source_layer.getFeature(fid)
        if not feature.isValid():
            continue
        geom = feature.geometry()
        if geom is None or geom.isNull() or geom.isEmpty():
            continue
        features.append(feature)
        source_geoms.append(geom)
    if not features:
        return CopyToResult(False, "选中的要素没有可用几何")

    mappings = list(field_mappings) if copy_attributes else []

    # ---- 1. 重叠校验（源自身 + 与目标图层已有图斑）----
    if check_self_overlap(source_geoms):
        return CopyToResult(False, "复制到操作失败：源图层选中的数据存在自重叠图斑")
    # 目标图层的几何要换算到它的 CRS 再比
    target_geoms = [_to_layer_geometry(source_layer, target_layer, g) for g in source_geoms]
    if check_target_overlap(target_layer, target_geoms):
        return CopyToResult(False, "复制到操作失败：待复制的图斑与目标图层中已有图斑重叠")

    # ---- 2. 字段值规则（长度 / 危险字符）----
    over, danger = validate_field_rules(source_layer, target_layer, features, mappings)
    if over:
        return CopyToResult(False, "因子超出系统配置数据长度，请修改数据或联系管理员处理",
                            fields=over)
    if danger:
        return CopyToResult(False, '因子中存在"<、>"等危险字符，请删除该类字符数据',
                            fields=danger)

    # ---- 3/4. 【预留】必填 / 字典值、操作范围 / 权限校验 ----
    missing = _validate_business_fields(source_layer, target_layer, features, mappings)
    if missing:
        return CopyToResult(False, "存在未填写的必填项", fields=missing)
    county = _validate_permission(source_layer, features)
    if county:
        return CopyToResult(False, "无操作权限", fields=[county])

    # ---- 5. 写进目标图层的编辑缓冲（一个撤销步骤，不自动提交）----
    source_names = [fld.name() for fld in source_layer.fields()]
    target_indices: list[tuple[int, int]] = []   # (目标字段下标, 源字段下标)
    for src_name, tgt_name in mappings:
        if src_name not in source_names:
            logger.warning("复制到：源字段不存在，已忽略 %s", src_name)
            continue
        tgt_idx = target_layer.fields().indexOf(tgt_name)
        if tgt_idx < 0:
            logger.warning("复制到：目标字段不存在，已忽略 %s", tgt_name)
            continue
        target_indices.append((tgt_idx, source_names.index(src_name)))
    mapped_indices = {idx for idx, _src in target_indices}

    if not target_layer.isEditable() and not target_layer.startEditing():
        return CopyToResult(False, f"目标图层「{target_layer.name()}」无法进入编辑态")

    added = 0
    target_layer.beginEditCommand("复制到")
    try:
        for feature, geom in zip(features, target_geoms):
            attrs = [None] * target_layer.fields().count()
            for tgt_idx, src_idx in target_indices:
                attrs[tgt_idx] = feature.attributes()[src_idx] if src_idx < len(feature.attributes()) else None
            attrs = _finalize_attributes(target_layer, attrs, geom, mapped_indices)
            new_feature = QgsFeature(target_layer.fields())
            new_feature.setGeometry(geom)
            new_feature.setAttributes(attrs)
            target_layer.addFeature(new_feature)
            added += 1
    except Exception:  # noqa: BLE001 —— 收宽：任何异常都必须关掉编辑命令
        target_layer.destroyEditCommand()
        logger.exception("复制到失败: source=%s target=%s", source_layer.name(), target_layer.name())
        return CopyToResult(False, "复制到失败：图层编辑 API 异常")
    target_layer.endEditCommand()

    # 要素新增，空间索引缓存失效 → 清掉让下次命中重建
    clear_spatial_index(target_layer)
    target_layer.triggerRepaint()
    say(f"已复制到「{target_layer.name()}」：新增 {added} 个要素（可 Ctrl+Z 撤销）")
    return CopyToResult(True, added=added)


def _to_layer_geometry(
    source_layer: QgsVectorLayer, target_layer: QgsVectorLayer, geometry: QgsGeometry
) -> QgsGeometry:
    """源图层几何 → **目标图层 CRS**（同 CRS / 无效时原样深拷贝）。"""
    cloned = QgsGeometry.fromWkt(geometry.asWkt())
    src_crs, dst_crs = source_layer.crs(), target_layer.crs()
    if not src_crs.isValid() or not dst_crs.isValid() or src_crs == dst_crs:
        return cloned
    try:
        cloned.transform(
            QgsCoordinateTransform(src_crs, dst_crs, QgsProject.instance())
        )
    except Exception:  # noqa: BLE001 —— 转换失败就用原坐标，让后续校验/写入暴露问题
        logger.warning("复制到：几何坐标转换失败，已按原坐标继续", exc_info=True)
    return cloned


__all__ = [
    "AREA_LIKE_FIELDS",
    "CopyToResult",
    "check_self_overlap",
    "check_target_overlap",
    "copy_features_to",
    "mappable_source_fields",
    "mappable_target_fields",
    "types_compatible",
    "validate_field_rules",
]
