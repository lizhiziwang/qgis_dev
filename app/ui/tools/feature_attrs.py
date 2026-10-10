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
    if not alias:
        return ""
    try:
        repaired = alias.encode("latin-1").decode("utf-8")
    except (UnicodeEncodeError, UnicodeDecodeError):
        return alias
    return repaired or alias


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


def uid_field_index(fields: "QgsFields") -> int | None:
    """全局唯一标识字段（FGUID / GUID / GlobalID）的下标；无则 ``None``。"""
    for idx, field in enumerate(fields):
        if field.name().strip().lower() in _UID_TOKENS:
            return idx
    return None


def _geometry_value(
    layer: "QgsVectorLayer", geometry: "QgsGeometry", kind: str
) -> float | None:
    """按图层几何类型算面积 ("area") / 周长长度 ("length") 值。

    **地理坐标系（EPSG:4490 这类经纬度）必须走椭球量算**：直接
    ``geometry.area()`` 拿到的是**平方度**——实测一块正常图斑只有 ``1.28e-08``，
    完全是另一个量级；长度同理是"度"。这里用 :class:`QgsDistanceArea` 按椭球算，
    面积给**平方米**、长度给**米**，也是 QGIS 自己「计算几何 / 字段计算器」的口径。

    投影坐标系保持 ``geometry.area()``（本来就是 CRS 单位，常规投影即平方米）——
    不去动它的好处是投影数据的面积口径不会因为这次修复而变化。
    """
    if geometry is None or geometry.isNull() or geometry.isEmpty():
        return None
    try:
        if layer.geometryType() == QgsWkbTypes.PolygonGeometry:
            if kind == "area":
                return (
                    _ellipsoidal_measure(layer, geometry, area=True)
                    if _needs_ellipsoidal(layer)
                    else geometry.area()
                )
            return (
                _ellipsoidal_measure(layer, geometry, area=False)
                if _needs_ellipsoidal(layer)
                else geometry.length()
            )
        return (
            _ellipsoidal_measure(layer, geometry, area=False)
            if _needs_ellipsoidal(layer)
            else geometry.length()
        )
    except Exception:  # noqa: BLE001 —— 几何运算失败不阻断编辑
        return None


def _needs_ellipsoidal(layer: "QgsVectorLayer") -> bool:
    """该图层是否需要椭球量算（地理坐标系才需要）。"""
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
        return d.measureArea(geometry) if area else d.measureLength(geometry)
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
    "feature_label",
    "field_alias",
    "new_feature_attributes",
    "refresh_area_fields",
    "uid_field_index",
    "uid_value_for",
]
