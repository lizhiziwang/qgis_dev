"""质检领域。

import 本模块即触发内置 6 个 check 的注册，调用 :func:`available_types`
会立即得到非空列表。
"""

from __future__ import annotations

from . import builtin  # noqa: F401  触发 builtin ``import *`` → 注册
from .param import CheckParam, NullValueParam
from .registry import BaseCheck, available_types, get_check_cls, register
from .service import CheckService
from .types import CheckResult, CheckRow, CheckSeverity, CheckStatus

__all__ = [
    "BaseCheck",
    "CheckParam",
    "CheckResult",
    "CheckRow",
    "CheckService",
    "CheckSeverity",
    "CheckStatus",
    "NullValueParam",
    "available_types",
    "get_check_cls",
    "register",
]
