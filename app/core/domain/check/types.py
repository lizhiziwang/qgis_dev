"""质检公共类型。"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Sequence


class CheckSeverity(str, Enum):
    """质检结果严重程度。"""

    INFO = "info"
    WARNING = "warning"
    ERROR = "error"


class CheckStatus(str, Enum):
    """质检结论。"""

    PASS = "pass"
    FAIL = "fail"


@dataclass(frozen=True)
class CheckResult:
    """单次质检执行结果。"""

    status: CheckStatus
    severity: CheckSeverity
    failed_feature_ids: Sequence[int] = field(default_factory=tuple)
    info: dict = field(default_factory=dict)

    @property
    def failed_count(self) -> int:
        return len(self.failed_feature_ids)

    @property
    def is_pass(self) -> bool:
        return self.status is CheckStatus.PASS


@dataclass(frozen=True)
class CheckRow:
    """质检条件载体：单字段值。

    旧 ``CheckVo`` 用 ``**kwargs`` 构造，易漏字段；这里走 dataclass。
    """

    field_name: str
    value: object = None
