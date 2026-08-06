"""逻辑检查（占位实现）。

旧 :class:`component.check.DataCheck.LogicCheck._build_sql` 是 ``pass``，没有
真正生成 SQL。新代码保留为占位但显式返回 ``"1=0"``（永远 0 行），并把
``info["note"]`` 写明"待实现"，避免静默 0 行的歧义。
"""

from __future__ import annotations

from app.core.domain.check.param import CheckParam
from app.core.domain.check.registry import BaseCheck, CheckResult, CheckSeverity, CheckStatus, register


@register("logic")
class LogicCheck(BaseCheck):
    """逻辑检查（待实现）。"""

    def build_where(self, p: CheckParam) -> str:
        # 旧实现是 pass；这里显式返回 1=0 让"无 SQL"语义可观察
        return "1=0"

    def run(self, p: CheckParam) -> CheckResult:
        return CheckResult(
            status=CheckStatus.FAIL,
            severity=CheckSeverity.WARNING,
            info={"note": "logic check 尚未实现，sql=1=0 永远 0 行；待业务方提供自定义 where"},
        )
