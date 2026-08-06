"""质检类型注册表。

旧实现是模块级字典 ``check_service = {0: DataValueCheck(), ...}``，新增
类型时必须改两处（字典 + 数组下标），且 ``0/1/2`` 数字下标易被误改。
新实现改用 :func:`register` 装饰器，按字符串 ``type_id`` 注册，新增
类型只需在 builtin 里写一个类并 ``@register("value_range")`` 即可。

调用方通过 :func:`get_check_cls` 拿到类，实例化后用 :meth:`BaseCheck.run`
执行。
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Callable, Dict, List, Type

from .param import CheckParam
from .types import CheckResult, CheckSeverity, CheckStatus

_REGISTRY: Dict[str, Type["BaseCheck"]] = {}


def register(type_id: str) -> Callable[[Type["BaseCheck"]], Type["BaseCheck"]]:
    """装饰器：把 :class:`BaseCheck` 子类注册到全局表。

    >>> @register("value_range")
    ... class DataValueCheck(BaseCheck):
    ...     def build_where(self, p: CheckParam) -> str: ...
    """

    def deco(cls: Type["BaseCheck"]) -> Type["BaseCheck"]:
        if not isinstance(cls, type) or not issubclass(cls, BaseCheck):
            raise TypeError(f"{cls!r} must subclass BaseCheck")
        if type_id in _REGISTRY:
            raise ValueError(f"check type {type_id!r} already registered")
        _REGISTRY[type_id] = cls
        cls.type_id = type_id  # type: ignore[attr-defined]
        return cls

    return deco


def get_check_cls(type_id: str) -> Type["BaseCheck"]:
    """按 ``type_id`` 拿 check 类；找不到抛 :class:`KeyError`。"""
    if type_id not in _REGISTRY:
        raise KeyError(f"unknown check type: {type_id!r}")
    return _REGISTRY[type_id]


def available_types() -> List[str]:
    """返回所有已注册的 ``type_id`` 列表（按字母序）。"""
    return sorted(_REGISTRY)


def reset_registry() -> None:
    """测试用：清空注册表。"""
    _REGISTRY.clear()


class BaseCheck(ABC):
    """质检抽象基类。"""

    type_id: str = ""

    @abstractmethod
    def build_where(self, p: CheckParam) -> str:
        """根据 ``p`` 构造 SQL where 子串。子类必须实现。"""
        raise NotImplementedError

    def run(self, p: CheckParam) -> CheckResult:
        """执行质检：先 build_where → 拿 where → 跑 :meth:`_execute` 拿 features。

        子类若要自定义执行流程（空间检查、复合检查），可重写本方法。
        """
        try:
            where = self.build_where(p)
            p_for_run = CheckParam(
                layer=p.layer,
                check_name=p.check_name,
                field_name=p.field_name,
                where=where,
                meta_source_layer_name=p.meta_source_layer_name,
                check_id=p.check_id,
                extra=p.extra,
            )
        except Exception as e:  # noqa: BLE001
            return CheckResult(
                status=CheckStatus.FAIL,
                severity=CheckSeverity.ERROR,
                info={"error": f"build_where 失败: {e}"},
            )

        if p_for_run.layer is None or not p_for_run.layer.isValid():
            return CheckResult(
                status=CheckStatus.FAIL,
                severity=CheckSeverity.ERROR,
                info={"error": "layer 无效"},
            )

        try:
            from qgis.core import QgsFeatureRequest  # 延迟 import：纯 Python 单测无需 qgis

            req = QgsFeatureRequest().setFilterExpression(p_for_run.where)
            ids = [f.id() for f in p_for_run.layer.getFeatures(req)]
        except Exception as e:  # noqa: BLE001
            return CheckResult(
                status=CheckStatus.FAIL,
                severity=CheckSeverity.ERROR,
                info={"error": f"SQL 执行失败: {e}", "where": p_for_run.where},
            )

        return CheckResult(
            status=CheckStatus.PASS if not ids else CheckStatus.FAIL,
            severity=CheckSeverity.ERROR if ids else CheckSeverity.INFO,
            failed_feature_ids=ids,
            info={
                "type_id": self.type_id,
                "where": p_for_run.where,
                "check_name": p_for_run.check_name,
            },
        )


__all__ = [
    "BaseCheck",
    "available_types",
    "get_check_cls",
    "register",
    "reset_registry",
]
