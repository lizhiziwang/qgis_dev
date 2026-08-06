"""值域检查。

旧 :class:`component.check.DataCheck.DataValueCheck` 行为：字段值不在给定的
值域集合内即视为不通过。值域用 ``,`` 拼接存到 ``p.where``。

新 :meth:`build_where` 输出形如::

    "field1 not in (a,b,c) and field2 not in (a,b,c)"

值域字符串由 :class:`CheckConfigDialog` 在 UI 层解析逗号得到；service 层
只负责生成 SQL，不解析值域。
"""

from __future__ import annotations

from app.core.domain.check.param import CheckParam
from app.core.domain.check.registry import BaseCheck, register


@register("value_range")
class DataValueCheck(BaseCheck):
    """字段值必须在给定值域内。"""

    def build_where(self, p: CheckParam) -> str:
        values = [v for v in p.where.split(",") if v]
        if not values or not p.field_name:
            return "1=0"   # 配置缺失视为空检
        joined = ",".join(values)
        return " and ".join(f'"{f}" not in ({joined})' for f in p.field_name)
