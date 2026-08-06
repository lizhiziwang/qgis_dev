"""内置质检类型集合。

按 :func:`app.core.domain.check.registry.register` 装饰器注册到全局表。
``import *`` 此模块即触发全部注册；新 ``CheckService`` 启动时会自动 import
本模块以保证 ``available_types()`` 返回非空。
"""

from __future__ import annotations

from . import logic_check, null_check, space_long_thin, space_overlap, space_self_intersect, value_check  # noqa: F401

__all__ = [
    "logic_check",
    "null_check",
    "space_long_thin",
    "space_overlap",
    "space_self_intersect",
    "value_check",
]
