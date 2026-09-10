"""质检服务门面。

外部调用方只面对 :class:`CheckService`，不直接接触注册表。新代码把
JSON 配置 → :class:`CheckParam` 的解析也收拢到 ``from_dict`` 里，业务
模块不用关心 ``checkType`` 是 int 还是 str。
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any, List, Mapping, Optional, Sequence

from app.core.domain.check.param import CheckParam
from app.core.domain.check.registry import BaseCheck, get_check_cls
from app.core.domain.check.types import CheckResult

from .registry import available_types

if TYPE_CHECKING:
    from qgis.core import QgsVectorLayer


def _ensure_builtin_loaded() -> None:
    """触发 builtin 子模块 ``import *`` 完成注册。"""
    # 重复 import 是 no-op，import 副作用是注册本身
    from app.core.domain.check import builtin  # noqa: F401, PLC0415 - 循环依赖：上提会与 check 包 __init__ 成环


class CheckService:
    """质检服务门面。"""

    def __init__(self) -> None:
        _ensure_builtin_loaded()

    def available_types(self) -> List[str]:
        """返回所有已注册的 check ``type_id`` 列表。"""

        return available_types()

    # ---------------------------------------------------------- 单条 / 批量

    def run(self, p: CheckParam, *, type_id: Optional[str] = None) -> CheckResult:
        """执行一条质检。

        :param p: 质检参数
        :param type_id: 强制指定 check 类型；``None`` 时用 ``p.extra["type_id"]``，
            缺省时用 ``"value_range"``（兼容旧 ``checkType=0`` 行为）。
        """
        cls = self._resolve_cls(type_id, p)
        instance = cls()
        return instance.run(p)

    def run_batch(
        self,
        params: Sequence[CheckParam],
        *,
        type_ids: Optional[Sequence[Optional[str]]] = None,
    ) -> List[CheckResult]:
        """批量执行。``type_ids`` 与 ``params`` 等长，缺省走 ``run`` 的 fallback。"""
        results: List[CheckResult] = []
        ids = list(type_ids) if type_ids else [None] * len(params)
        for p, tid in zip(params, ids):
            results.append(self.run(p, type_id=tid))
        return results

    # ---------------------------------------------------------- JSON 适配

    def from_dict(self, dto: Mapping[str, Any], layer: "Optional[QgsVectorLayer]" = None) -> CheckParam:
        """从 JSON dict 构造 :class:`CheckParam`（兼容旧 ``resource/质检.json``）。"""
        check_name = dto.get("checkItemName")
        check_id = dto.get("id")
        try:
            check_type = dto.get("checkType", 0)
        except (TypeError, ValueError):
            check_type = 0

        # 旧 ``checkType`` 0/1/2 数字 → 字符串 ``type_id`` 映射
        legacy_type_map = {0: "value_range", 1: "null_value", 2: "logic"}
        type_id = legacy_type_map.get(int(check_type), "value_range")

        field_name: List[str] = []
        where = "1=1"
        extra: dict = {"type_id": type_id}
        try:
            param_str = dto.get("checkParam")
            if param_str:
                if isinstance(param_str, str):
                    parsed = json.loads(param_str)
                else:
                    parsed = param_str
                if isinstance(parsed, dict):
                    raw = parsed.get("fieldName", [])
                    if isinstance(raw, list):
                        field_name = [str(x) for x in raw if x is not None]
                    elif isinstance(raw, str):
                        field_name = [raw] if raw else []
                    where = str(parsed.get("where", "1=1"))
                    if "domainValues" in parsed:
                        where = ",".join(str(v) for v in parsed["domainValues"])
                    if "logicalSql" in parsed:
                        where = str(parsed["logicalSql"])
        except (json.JSONDecodeError, TypeError, ValueError):
            where = "1=1"

        # layer：JSON 里没存，从调用方传进来
        return CheckParam(
            layer=layer,
            check_name=str(check_name) if check_name is not None else None,
            field_name=tuple(field_name),
            where=where,
            check_id=str(check_id) if check_id is not None else None,
            extra=extra,
        )

    def run_from_json(
        self,
        dto: Mapping[str, Any],
        layer: "Optional[QgsVectorLayer]" = None,
    ) -> CheckResult:
        """一步走完：JSON → :class:`CheckParam` → :class:`CheckResult`。"""
        p = self.from_dict(dto, layer=layer)
        type_id = p.extra.get("type_id", "value_range")
        return self.run(p, type_id=str(type_id))

    # ---------------------------------------------------------- 内部

    def _resolve_cls(
        self, type_id: Optional[str], p: CheckParam
    ) -> type[BaseCheck]:
        tid = type_id or (p.extra.get("type_id") if isinstance(p.extra, dict) else None)
        if not tid:
            # 兼容旧调用方式（不传 type_id 也跑得了）：默认 value_range
            tid = "value_range"
        return get_check_cls(str(tid))


__all__ = ["CheckService"]
