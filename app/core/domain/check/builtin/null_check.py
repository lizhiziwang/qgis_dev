"""非空检查。

旧 :class:`NullValueCheck.build_where` 行为::

    where = " or ".join([
        f"({field} is null or {field} = '' or {field} = ' ')"
        for field in field_name
    ])

新代码保留等价语义，但 SQL 字符串里加上双引号包字段名，避免字段名含
特殊字符时崩。
"""

from __future__ import annotations

from app.core.domain.check.param import CheckParam
from app.core.domain.check.registry import BaseCheck, register


@register("null_value")
class NullValueCheck(BaseCheck):
    """字段值不允许为 null / 空字符串 / 空白。"""

    def build_where(self, p: CheckParam) -> str:
        if not p.field_name:
            return "1=0"
        parts: list[str] = []
        for f in p.field_name:
            parts.append(f'("{f}" is null OR "{f}" = \'\' OR "{f}" = \' \')')
        return " OR ".join(parts)
