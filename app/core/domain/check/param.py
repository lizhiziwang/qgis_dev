"""质检参数 DTO。

旧 ``CheckParam`` / ``NullValueParam`` 走 ``__init__(**kwargs)``，调用方极
易漏字段；新代码走 :func:`dataclasses.dataclass`，字段必填/可选都通过
类型注解显式表达。

设计原则：

- 全部字段都是只读快照，构造后不可变。
- ``field_name`` 用 :class:`Sequence[str]`，避免可变 list 误共享。
- ``where`` 默认 ``"1=1"``，调用方可省略。
- ``extra`` 留给 JSON 配置里特有的扩展字段。

类型注解里 :class:`QgsVectorLayer` 用 ``TYPE_CHECKING`` 引入，让本模块在
**无 QGIS** 的纯 Python 环境（CI 单测、FastAPI 进程）也能 import；运行时
真正传入 layer 才会需要 qgis。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Optional, Sequence

if TYPE_CHECKING:
    from qgis.core import QgsVectorLayer


@dataclass(frozen=True)
class CheckParam:
    """质检参数。"""

    layer: Optional["QgsVectorLayer"] = None
    check_name: Optional[str] = None
    field_name: Sequence[str] = field(default_factory=tuple)
    where: str = "1=1"
    meta_source_layer_name: Optional[str] = None
    check_id: Optional[str] = None
    extra: dict = field(default_factory=dict)


# 旧 ``NullValueParam`` 跟 ``CheckParam`` 完全一致，保留同名以兼容旧 import
@dataclass(frozen=True)
class NullValueParam(CheckParam):
    """兼容旧 :class:`component.check.CheckParam.NullValueParam`。"""
