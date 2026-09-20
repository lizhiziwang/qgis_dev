"""响应 JSON 的通用取值工具（登录、当前用户信息共用）。

后端各家字段名不一（``userName`` / ``realName``、``data`` 有时还包一层），这里只做
「按顺序找第一个非空字段，字段被 ``data`` / ``result`` / ``payload`` 包着也能找到」，
**不抛异常、不碰网络、不含业务语义**，方便单独测与复用。
"""

from __future__ import annotations

from typing import Any

__all__ = ["WRAPPER_KEYS", "pick", "pick_any", "pick_str"]

#: 字段可能被包一层的外层键（按顺序找）
WRAPPER_KEYS: tuple[str, ...] = ("data", "result", "payload")


def pick(payload: dict[str, Any], key: str) -> Any:
    """按「顶层 → ``data`` / ``result`` / ``payload``」顺序取第一个非空 ``key``。"""
    value = payload.get(key)
    if value not in (None, ""):
        return value
    for wrapper in WRAPPER_KEYS:
        nested = payload.get(wrapper)
        if isinstance(nested, dict):
            value = nested.get(key)
            if value not in (None, ""):
                return value
    return None


def pick_any(payload: dict[str, Any], keys: tuple[str, ...]) -> Any:
    """按顺序在 ``keys`` 里取第一个命中的字段（值可能被包一层）。"""
    for key in keys:
        value = pick(payload, key)
        if value not in (None, ""):
            return value
    return None


def pick_str(payload: dict[str, Any], keys: tuple[str, ...]) -> str:
    """按顺序取第一个非空文本字段（值不是字符串就 ``str()`` 一下）。"""
    value = pick_any(payload, keys)
    return str(value).strip() if value not in (None, "") else ""
