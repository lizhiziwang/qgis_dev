"""分割 / 分解后要素属性的继承与刷新助手。

数据约定（zwps GIS 成果数据）：

- **面积 / 周长类字段**：几何变了数值就过期，按新几何重算（面图层：
  面积字段 → ``geometry.area()``、周长字段 → ``geometry.length()``
  （QGIS 4.2 已移除 ``QgsGeometry.perimeter()``，多边形的 ``length()``
  即周长）；线图层：一律 ``length()``）；
- **全局唯一标识字段**（FGUID / GUID / GlobalID）：新要素必须生成**新的
  唯一值**（字符串字段生成 ``uuid4``；其它类型无法安全生成 → 置 NULL）；
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

#: 面积类字段名关键字（小写子串匹配，含别名）
_AREA_TOKENS = ("面积", "area", "mj", "sqm", "shape_area", "tbmj")
#: 周长 / 长度类字段名关键字（小写子串匹配，含别名）
_LENGTH_TOKENS = ("周长", "perimeter", "zc", "length")
#: 全局唯一标识字段名关键字（小写全名匹配，含别名）
_UID_TOKENS = ("fguid", "guid", "globalid")


def _field_key(field: "QgsField") -> str:
    """字段名 + 别名的小写拼接，用于关键字匹配。"""
    parts = [field.name()]
    try:
        alias = field.alias()
        if alias:
            parts.append(alias)
    except Exception:  # noqa: BLE001
        pass
    return " ".join(parts).lower()


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
    """按图层几何类型算面积 ("area") / 周长长度 ("length") 值。"""
    if geometry is None or geometry.isNull() or geometry.isEmpty():
        return None
    try:
        if kind == "area":
            if layer.geometryType() == QgsWkbTypes.PolygonGeometry:
                return geometry.area()
            return geometry.length()
        return geometry.length()
    except Exception:  # noqa: BLE001 —— 几何运算失败不阻断编辑
        return None


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
            attributes[idx] = value
    return attributes


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
        attrs[uid_idx] = _new_uid_value(layer.fields().at(uid_idx))
    return attrs


def _new_uid_value(field: "QgsField"):
    """给唯一标识字段生成新值：字符串 → ``uuid4`` 文本；其它类型 → None。"""
    if QVariant is not None and field.type() == QVariant.String:
        return str(uuid.uuid4())
    return None


__all__ = [
    "new_feature_attributes",
    "refresh_area_fields",
    "uid_field_index",
]
