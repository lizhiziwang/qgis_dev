"""测试 :class:`app.core.domain.check.param.CheckParam`。"""

from __future__ import annotations

import pytest

from app.core.domain.check.param import CheckParam, NullValueParam


def test_default_param():
    p = CheckParam()
    assert p.layer is None
    assert p.check_name is None
    assert p.field_name == ()
    assert p.where == "1=1"
    assert p.meta_source_layer_name is None
    assert p.check_id is None
    assert p.extra == {}


def test_param_is_frozen():
    p = CheckParam(check_name="x")
    with pytest.raises((AttributeError, Exception)):
        p.check_name = "y"  # type: ignore[misc]


def test_field_name_tuple_cannot_be_mutated():
    p = CheckParam(field_name=("a", "b"))
    with pytest.raises((AttributeError, TypeError)):
        p.field_name.append("c")  # type: ignore[attr-defined]


def test_null_value_param_subclass():
    n = NullValueParam(check_name="n", field_name=("a",))
    assert n.check_name == "n"
    assert n.field_name == ("a",)
    # 是 CheckParam 子类
    assert isinstance(n, CheckParam)
