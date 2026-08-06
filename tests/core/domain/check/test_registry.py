"""测试 :func:`app.core.domain.check.registry` 装饰器。"""

from __future__ import annotations

import pytest

from app.core.domain.check import registry
from app.core.domain.check.param import CheckParam
from app.core.domain.check.registry import (
    BaseCheck,
    available_types,
    get_check_cls,
    register,
    reset_registry,
)


@pytest.fixture(autouse=True)
def _reset():
    reset_registry()
    yield
    reset_registry()


def test_register_decorator_sets_type_id():
    @register("demo_type")
    class DemoCheck(BaseCheck):
        def build_where(self, p: CheckParam) -> str:
            return "1=1"

    assert DemoCheck.type_id == "demo_type"
    assert "demo_type" in available_types()
    assert get_check_cls("demo_type") is DemoCheck


def test_register_duplicate_raises():
    @register("dup")
    class A(BaseCheck):
        def build_where(self, p: CheckParam) -> str:
            return "1=1"

    with pytest.raises(ValueError):

        @register("dup")
        class B(BaseCheck):
            def build_where(self, p: CheckParam) -> str:
                return "1=1"


def test_register_non_basecheck_raises():
    with pytest.raises(TypeError):

        @register("x")
        class NotABaseCheck:
            pass


def test_get_check_cls_unknown_raises():
    with pytest.raises(KeyError):
        get_check_cls("nope")


def test_available_types_sorted():
    @register("z_type")
    class Z(BaseCheck):
        def build_where(self, p: CheckParam) -> str:
            return "1=1"

    @register("a_type")
    class A(BaseCheck):
        def build_where(self, p: CheckParam) -> str:
            return "1=1"

    types = available_types()
    assert types.index("a_type") < types.index("z_type")


def test_basecheck_run_with_invalid_layer():
    class Dummy(BaseCheck):
        def build_where(self, p: CheckParam) -> str:
            return "1=1"

    Dummy.type_id = "dummy"
    res = Dummy().run(CheckParam(layer=None))
    assert res.is_pass is False
    assert res.failed_feature_ids == ()
    assert "error" in res.info
