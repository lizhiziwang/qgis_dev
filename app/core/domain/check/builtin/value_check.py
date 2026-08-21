"""值域检查。

旧 :class:`component.check.DataCheck.DataValueCheck` 行为：字段值不在给定的
值域集合内即视为不通过。值域用 ``,`` 拼接存到 ``p.where``。

新 :meth:`build_where` 输出形如::

    "field1 not in ('a','b') and field2 not in ('a','b')"

值域字符串由 :class:`CheckConfigDialog` 在 UI 层解析逗号得到；service 层
只负责生成 SQL，不解析值域。

**值必须按字段类型加引号**：质检数据里地类/林种等字段是字符串编码（如
``030100`` 带前导零）。不加引号会被 OGR SQL 当成数值/列名，GDAL 报 ``Type
mismatch or improper type of arguments to IN operator.``，且前导零被丢弃
导致错配。这里按 :class:`QgsField` 的真实类型决定：字符串字段一律加单引号
（内嵌单引号翻倍转义），数值字段保持裸字面量（非数值值对该字段无比较意义，
跳过）。
"""

from __future__ import annotations

from PyQt6.QtCore import QMetaType

from app.core.domain.check.param import CheckParam
from app.core.domain.check.registry import BaseCheck, register

#: 数值型字段类型：值域保持裸字面量（不引号）
_NUMERIC_TYPES = frozenset(
    {
        QMetaType.Type.Bool,
        QMetaType.Type.Int,
        QMetaType.Type.UInt,
        QMetaType.Type.LongLong,
        QMetaType.Type.ULongLong,
        QMetaType.Type.Double,
        QMetaType.Type.Float,
    }
)


def _quote_value(value: str, field_type) -> str:
    """按字段类型把单个值域字面量转成 SQL 片段。

    - 数值字段：能解析成数值的保持裸字面量；否则返回 ``None``（该值对这个
      字段不可能匹配，跳过）。
    - 其余（字符串/日期等）：加单引号，内嵌单引号用两个单引号转义
      （SQL 字符串转义，QGIS 表达式同样接受）。
    """
    if field_type in _NUMERIC_TYPES:
        try:
            float(value)
            return value
        except ValueError:
            return None
    return "'" + value.replace("'", "''") + "'"


def _field_type(layer, name) -> QMetaType.Type:
    """取字段类型；拿不到字段时按字符串处理（保守加引号）。"""
    try:
        if layer is not None and name:
            idx = layer.fields().indexOf(name)
            if idx >= 0:
                return layer.fields().at(idx).type()
    except Exception:  # noqa: BLE001
        pass
    return QMetaType.Type.QString


@register("value_range")
class DataValueCheck(BaseCheck):
    """字段值必须在给定值域内。"""

    def build_where(self, p: CheckParam) -> str:
        values = [v for v in p.where.split(",") if v]
        if not values or not p.field_name:
            return "1=0"   # 配置缺失视为空检
        predicates: list[str] = []
        for f in p.field_name:
            field_type = _field_type(p.layer, f)
            literals = [
                lit
                for v in values
                if (lit := _quote_value(v, field_type)) is not None
            ]
            if not literals:
                # 数值字段全为不可比较的值 → 该字段不参与过滤
                continue
            predicates.append(f'"{f}" not in ({",".join(literals)})')
        if not predicates:
            return "1=0"
        return " and ".join(predicates)
