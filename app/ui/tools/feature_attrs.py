"""分割 / 分解后要素属性的继承与刷新助手。

数据约定（zwps GIS 成果数据）：

- **面积 / 周长类字段**：几何变了数值就过期，按新几何重算（面图层：
  面积字段 → ``geometry.area()``、周长字段 → ``geometry.length()``
  （QGIS 4.2 已移除 ``QgsGeometry.perimeter()``，多边形的 ``length()``
  即周长）；线图层：一律 ``length()``）；
- **全局唯一标识字段**（FGUID / GUID / GlobalID / **mzguid**）：新要素必须生成
  **新的唯一值**（字符串字段生成 ``uuid4``；其它类型无法安全生成 → 置 NULL）；
- **主键字段**（``layer.primaryKeyAttributes()``）：新要素置 NULL，交给
  数据源自动分配。实测手工 ``addFeature`` 时若不把主键字段清空，提交到
  GPKG 会把原要素的主键值复制过去 → ``UNIQUE constraint failed``；
- 其余普通字段**一律继承**原始要素的值。

字段识别按**名称 / 别名**做关键字匹配（成果数据的字段命名约定），匹配不到
任何面积类字段时不会改任何属性（数据无这些字段也能正常用）。
"""

from __future__ import annotations

import uuid
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from qgis.core import QgsField, QgsFields, QgsGeometry, QgsVectorLayer

try:  # 单元测试 / offscreen 环境也能 import
    from qgis.core import QgsWkbTypes
    from qgis.PyQt.QtCore import QVariant
except Exception:  # noqa: BLE001
    QgsWkbTypes = None  # type: ignore[assignment]
    QVariant = None  # type: ignore[assignment]

from app.infra.edit_settings import (
    BACKEND_ELLIPSOID,
    BACKEND_GAUSS,
    MU_IN_SQUARE_METERS,
    EditSettingsManager,
)

#: 面积类字段名关键字（小写子串匹配，含别名）。成果数据里面积字段既有中文
#: （``面积``）也有拼音缩写（``mj`` / ``txmj`` / ``xbmj`` / ``tb_mj``）与全拼
#: （``bh_mian_ji``），三种写法都要能命中
_AREA_TOKENS = ("面积", "area", "mj", "sqm", "shape_area", "tbmj", "mian_ji", "mianji")
#: 周长 / 长度类字段名关键字（小写子串匹配，含别名）
_LENGTH_TOKENS = ("周长", "perimeter", "zc", "length")
#: 全局唯一标识字段名关键字（小写全名匹配，含别名）。``mzguid`` 是成果数据里
#: 图斑的唯一标识，和 FGUID / GUID / GlobalID 一样属于「新要素必须重新生成」的列
_UID_TOKENS = ("fguid", "guid", "globalid", "mzguid")


def _field_key(field: "QgsField") -> str:
    """字段名 + 别名的小写拼接，用于关键字匹配。"""
    parts = [field.name()]
    alias = field_alias(field)
    if alias:
        parts.append(alias)
    return " ".join(parts).lower()


def field_alias(field: "QgsField") -> str:
    """字段别名的显示文本（**修掉可能的乱码**）；没有别名返回空串。

    某些 GDB（实测 FileGDB 的中文别名）取回来的别名是
    ``'æ\\x9d\\x83å±\\x9eæ\\x80§è´¨'`` 这个样子——UTF-8 字节被当成 Latin-1 解了一遍。
    按 ``latin-1 → utf-8`` 转回去就还原成「权属性质」。转不过去（本来就不是这种
    乱码，比如正常的 ``Café``）就原样返回，不会误伤。
    """
    try:
        alias = field.alias() or ""
    except Exception:  # noqa: BLE001
        return ""
    from app.core.util.text_encoding import repair_mojibake

    return repair_mojibake(alias)


def _field_kind(field: "QgsField") -> str | None:
    """字段是否是面积 / 周长类字段（"area"/"length"），否则 ``None``。"""
    key = _field_key(field)
    for token in _AREA_TOKENS:
        if token in key:
            return "area"
    for token in _LENGTH_TOKENS:
        if token in key:
            return "length"
    return None


#: 由**数据源自动赋值**的标识字段（保存时自己会填 / 重算，用户不该手改）。
#: 与 :data:`_UID_TOKENS` 分开：那些是"新要素必须重新生成 uuid"的列，分割 / 分解时
#: 要赋新值；objectid 这类是自增 / 自维护的，赋 uuid 反而是错的。
#: 普通 ``id`` 也按自维护字段处理（用户要求它和 OBJECTID 一样不可编辑）。
_AUTO_ID_TOKENS = ("objectid", "object_id", "ogc_fid", "fid", "gid", "rowid", "id")


def auto_id_field_index(fields: "QgsFields") -> int | None:
    """数据源自动赋值的标识字段下标（objectid / ogc_fid / fid…）；无则 ``None``。"""
    for idx, field in enumerate(fields):
        if field.name().strip().lower() in _AUTO_ID_TOKENS:
            return idx
    return None


def uid_field_index(fields: "QgsFields") -> int | None:
    """全局唯一标识字段（FGUID / GUID / GlobalID）的下标；无则 ``None``。"""
    for idx, field in enumerate(fields):
        if field.name().strip().lower() in _UID_TOKENS:
            return idx
    return None


def readonly_field_indices(fields: "QgsFields") -> set[int]:
    """**只读**字段下标的集合（系统自维护主键 + 全局唯一标识）。

    **所有**命中 ``_UID_TOKENS`` / ``_AUTO_ID_TOKENS`` 的字段都算（以前只取第一个
    命中项，图层里同时有 OBJECTID 和 id 这类字段时后一个仍可编辑）。只读字段
    不允许手工改值（见属性录入弹窗），也不允许改名 / 删除 / 参与字段计算器与
    几何计算（属性表列头、图层属性「字段」页统一按本函数判定）。
    """
    tokens = set(_UID_TOKENS) | set(_AUTO_ID_TOKENS)
    return {
        index
        for index, field in enumerate(fields)
        if field.name().strip().lower() in tokens
    }


def _geometry_value(
    layer: "QgsVectorLayer", geometry: "QgsGeometry", kind: str
) -> float | None:
    """按图层几何类型算面积 ("area") / 周长长度 ("length") 值（**写进字段的那个值**）。

    分两种口径，由「编辑设置 → 应用于系统长度与面积字段值的自动计算」决定：

    - **勾选** → 按「默认长度 / 面积计算方法」算，并按「默认长度 / 面积单位」写入
      （所以面积字段可能存的是**亩 / 公顷**而不是平方米 —— 这是用户在编辑设置里
      明确选的单位）；
    - **不勾选** → 保持系统原来的方式：地理坐标系（EPSG:4490 这类）必须走椭球
      量算（直接 ``geometry.area()`` 拿到的是**平方度**，实测一块正常图斑只有
      ``1.28e-08``），投影坐标系用它自己 CRS 的平面量算；单位固定平方米 / 米。

    两种口径都是**照算不误**，差别只在"用哪套方法、写什么单位"。
    """
    if geometry is None or geometry.isNull() or geometry.isEmpty():
        return None
    is_area = kind == "area" and layer.geometryType() == QgsWkbTypes.PolygonGeometry
    settings = EditSettingsManager.instance().current()
    try:
        if settings.auto_calc:
            backend = settings.area_backend if is_area else settings.length_backend
            meters = _measure_with(layer, geometry, is_area, backend)
            return None if meters is None else _to_unit(kind, meters, settings)
        return (
            _ellipsoidal_measure(layer, geometry, area=is_area)
            if _needs_ellipsoidal(layer)
            else (geometry.area() if is_area else geometry.length())
        )
    except Exception:  # noqa: BLE001 —— 几何运算失败不阻断编辑
        return None


def measure_geometry_value(
    layer: "QgsVectorLayer",
    geometry: "QgsGeometry",
    *,
    is_area: bool,
    backend: str,
) -> float | None:
    """按指定后端把几何量成（米 / 平方米）；失败返回 ``None``。

    与自动计算系统字段同一套量算口径（高斯投影 / 等积投影 / 椭球），供属性表
    「几何计算」对话框复用，避免另抄一份投影与椭球处理。
    """
    try:
        return _measure_with(layer, geometry, is_area, backend)
    except Exception:  # noqa: BLE001 —— 单要素算不出来不影响其它要素
        logger.warning("几何计算量算失败", exc_info=True)
        return None


def _measure_with(layer, geometry, is_area: bool, backend: str) -> float | None:
    """按指定后端量出（米² / 米）：椭球 / 高斯-克吕格 / 等积，逐级兜底。"""
    if backend == BACKEND_ELLIPSOID:
        value = _ellipsoidal_measure(layer, geometry, area=is_area)
        if value is not None:
            return value
    else:
        projected = _projected_measure(layer, geometry, backend)
        if projected is not None:
            return projected.area() if is_area else projected.length()
    return geometry.area() if is_area else geometry.length()


def _projected_measure(layer, geometry, backend: str):
    """把几何投到对应投影（高斯-克吕格 / 江西 Albers）平面；不可用返回 ``None``。"""
    from app.core.domain.check.builtin.measure import (
        make_gauss_kruger_transform,
        make_transform,
        project_geometry,
    )

    try:
        transform = (
            make_gauss_kruger_transform(layer.crs(), geometry)
            if backend == BACKEND_GAUSS
            else make_transform(layer.crs())
        )
    except Exception:  # noqa: BLE001 —— 建不出转换就落回下一级兜底
        return None
    return project_geometry(geometry, transform)


def _to_unit(kind: str, meters: float, settings) -> float:
    """米² / 米 → 「编辑设置」里选的单位。"""
    if kind == "area":
        if settings.area_unit == "公顷":
            return meters / 10_000.0
        if settings.area_unit == "平方千米":
            return meters / 1_000_000.0
        return meters / MU_IN_SQUARE_METERS          # 亩（QGIS 枚举里没有，手工换）
    if settings.len_unit == "千米":
        return meters / 1000.0
    if settings.len_unit == "厘米":
        return meters * 100.0
    return meters


def _needs_ellipsoidal(layer: "QgsVectorLayer") -> bool:
    """该图层是否需要椭球量算（地理坐标系才需要）——系统原来的判定。"""
    try:
        return bool(layer.crs().isGeographic())
    except Exception:  # noqa: BLE001
        return False


def _ellipsoidal_measure(
    layer: "QgsVectorLayer", geometry: "QgsGeometry", *, area: bool
) -> float | None:
    """按椭球算面积（平方米）/ 长度（米）；算不出来返回 ``None``。"""
    from qgis.core import QgsDistanceArea, QgsProject

    try:
        d = QgsDistanceArea()
        d.setSourceCrs(layer.crs(), QgsProject.instance().transformContext())
        ellipsoid = layer.crs().ellipsoidAcronym()
        if ellipsoid:
            d.setEllipsoid(ellipsoid)
        if area:
            return d.measureArea(geometry)
        # **面必须用 measurePerimeter**：实测 QGIS 4.2 下 ``measureLength`` 对多边形
        # 几何直接返回 0 —— 周长字段会被写成 0（安静地把数据改坏）。测量工具那边
        # 记着同一个坑（见 app/ui/tools/measure.py 的 _perimeter_meters）。
        if layer.geometryType() == QgsWkbTypes.PolygonGeometry:
            return d.measurePerimeter(geometry)
        return d.measureLength(geometry)
    except Exception:  # noqa: BLE001
        return None


def _coerce_to_field(field: "QgsField", value):
    """把算出来的数值塞进字段：**整型字段取整**。

    不取整的话，面积字段如果是 ``lbmj_bl`` 这种整型列，就会写进 ``1.28e-08`` 这类
    小数——界面上读回来是 ``1.2824370712073707e-08``，用户一点确定就报
    「不是有效整数」，直接卡住保存。
    """
    if QVariant is None or value is None:
        return value
    if field.type() in (
        QVariant.Int, QVariant.LongLong, QVariant.UInt, QVariant.ULongLong,
    ):
        try:
            return int(round(float(value)))
        except (TypeError, ValueError):
            return value
    return value


def refresh_area_fields(
    layer: "QgsVectorLayer", attributes: list, geometry: "QgsGeometry"
) -> list:
    """按新几何重算 ``attributes`` 里的面积 / 周长类字段（就地改写并返回）。

    原要素几何被改小（面分割留下剩余部分 / 分解留下首个部件）后调本函数，
    把过期的面积 / 周长值更新为当前几何的值；找不到面积类字段时为无操作。

    **"自动计算"那个勾选框不控制算不算**——不勾只是换成系统原来的算法与单位
    （见 :func:`_geometry_value`），几何变了照样重算，否则成果字段会悄悄变陈旧。
    """
    for idx, field in enumerate(layer.fields()):
        kind = _field_kind(field)
        if kind is None or idx >= len(attributes):
            continue
        value = _geometry_value(layer, geometry, kind)
        if value is not None:
            attributes[idx] = _coerce_to_field(field, value)
    return attributes


def uid_value_for(field: "QgsField"):
    """给唯一标识字段生成新值：字符串 → ``uuid4`` 文本；其它类型 → None。

    需要「保留已有值、只在空的时候才生成」的调用方（如「复制到」把源图斑的
    唯一标识映射过来时）自己判空后再调本函数。
    """
    if QVariant is not None and field.type() == QVariant.String:
        return str(uuid.uuid4())
    return None


def new_feature_attributes(
    layer: "QgsVectorLayer", attributes: list, geometry: "QgsGeometry"
) -> list:
    """分割 / 分解产出的**新要素**属性。

    - 主键字段置 NULL（数据源自动分配，防 UNIQUE 冲突）；
    - 面积 / 周长类字段按新几何重算；
    - 唯一标识字段（FGUID 等）重新生成唯一值（主键列除外——交给数据源）；
    - 其余字段继承原始要素的值。
    """
    attrs = list(attributes)
    pk = set(layer.primaryKeyAttributes())
    for idx in pk:
        if idx < len(attrs):
            attrs[idx] = None
    refresh_area_fields(layer, attrs, geometry)
    uid_idx = uid_field_index(layer.fields())
    if uid_idx is not None and uid_idx not in pk and uid_idx < len(attrs):
        attrs[uid_idx] = uid_value_for(layer.fields().at(uid_idx))
    return attrs


#: 「编号 / 名称」类字段名关键字（小写子串匹配，含别名），没有显示表达式时的兜底
_LABEL_TOKENS = ("编号", "编码", "图斑号", "code", "tbbh", "bh", "名称", "name")


def _field_text(feature, layer: "QgsVectorLayer", name: str) -> str:
    """要素在 ``name`` 字段上的可展示文本；字段不存在 / 无值返回空串。"""
    try:
        idx = layer.fields().indexOf(name)
        if idx < 0:
            return ""
        value = feature.attributes()[idx]
    except Exception:  # noqa: BLE001 —— 取不到就当没值，别把调用方带崩
        return ""
    if value is None:
        return ""
    text = str(value).strip()
    return "" if text.lower() in ("", "null", "none", "<null>") else text


def feature_label(feature, layer: "QgsVectorLayer") -> str:
    """要素对外的**显示名**（弹窗标题之类）；取不到就退回要素 ID 字符串。

    优先级（第一个取到值即返回）：

    1. 图层自己配置的**显示表达式 / 显示字段** —— 全应用其它面板展示要素名也用它，
       成果数据里通常就是图斑编号；
    2. 名称 / 别名命中 :data:`_LABEL_TOKENS` 的字段的**值**；
    3. ``str(feature.id())``。

    GUID 类字段（``mzguid`` / ``FGUID``…）永不参与第 2 步：那是一串 uuid，认不出来。
    """
    if feature is None:
        return ""
    label = _label_from_display(layer, feature)
    if label:
        return label

    try:
        fields = layer.fields()
        attrs = feature.attributes()
    except Exception:  # noqa: BLE001
        fields, attrs = None, None
    if fields is not None:
        for idx in range(min(fields.count(), len(attrs))):
            field = fields.at(idx)
            key = f"{field.name()} {field_alias(field)}".strip().lower()
            if key in _UID_TOKENS:
                continue  # uuid 类字段认不出要素，跳过
            if not any(token in key for token in _LABEL_TOKENS):
                continue
            text = _field_text(feature, layer, field.name())
            if text:
                return text
    try:
        return str(feature.id())
    except Exception:  # noqa: BLE001 —— 销毁流程中
        return ""


def _label_from_display(layer: "QgsVectorLayer", feature) -> str:
    """图层显示字段 / 显示表达式的值（取不到返回空串）。"""
    try:
        display = (layer.displayField() or "").strip()
    except Exception:  # noqa: BLE001
        display = ""
    if display:
        text = _field_text(feature, layer, display)
        if text:
            return text
    try:
        raw = (layer.displayExpression() or "").strip()
    except Exception:  # noqa: BLE001
        return ""
    if not raw:
        return ""
    try:
        from qgis.core import QgsExpression

        expression = QgsExpression(raw)
        if expression.hasParserError():
            return ""
        value = expression.evaluate(feature, layer.createExpressionContext())
    except Exception:  # noqa: BLE001 —— 表达式脏了不该挡住标题
        return ""
    if value is None:
        return ""
    text = str(value).strip()
    return "" if text.lower() in ("", "null", "none", "<null>") else text


__all__ = [
    "auto_id_field_index",
    "feature_label",
    "field_alias",
    "new_feature_attributes",
    "readonly_field_indices",
    "refresh_area_fields",
    "uid_field_index",
    "uid_value_for",
]
