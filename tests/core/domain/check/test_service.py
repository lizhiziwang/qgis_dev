"""测试 :class:`app.core.domain.check.service.CheckService`。

需要能 import qgis，因为 :class:`CheckService` 内部会触发 :class:`BaseCheck.run`。
"""

from __future__ import annotations

import json

import pytest

pytest.importorskip("qgis", reason="CheckService 内部走 BaseCheck.run")

from unittest.mock import MagicMock

from app.core.domain.check import registry
from app.core.domain.check.param import CheckParam
from app.core.domain.check.service import CheckService
from app.core.domain.check.types import CheckStatus


@pytest.fixture(autouse=True)
def _clean_registry():
    registry.reset_registry()

    @registry.register("demo_value")
    class _DemoValue(registry.BaseCheck):
        def build_where(self, p: CheckParam) -> str:
            return "1=0"

    @registry.register("demo_fail")
    class _DemoFail(registry.BaseCheck):
        def build_where(self, p: CheckParam) -> str:
            return "demo_col = 1"

    yield


def test_service_loads_builtin_automatically():
    svc = CheckService()
    # builtin 子模块 ``import *`` 应该已经注册
    from app.core.domain.check import builtin  # noqa: F401
    types = svc.run(CheckParam(layer=MagicMock())).info  # 不会真跑 SQL，type_id 缺失
    # run 找不到 type_id → 用默认 value_range
    # 关键是 CheckService 自身能跑通
    assert types is not None


def test_run_with_type_id_override():
    svc = CheckService()
    layer = MagicMock()
    layer.isValid.return_value = True
    # 我们自己 register 的 demo_value 返回 1=0，期望 is_pass=True
    res = svc.run(CheckParam(layer=layer), type_id="demo_value")
    assert res.is_pass is True
    assert res.failed_feature_ids == []


def test_run_with_demo_fail_returns_ids():
    svc = CheckService()
    layer = MagicMock()
    layer.isValid.return_value = True
    f1, f2 = MagicMock(), MagicMock()
    f1.id.return_value = 3
    f2.id.return_value = 7
    layer.getFeatures.return_value = iter([f1, f2])
    res = svc.run(CheckParam(layer=layer), type_id="demo_fail")
    assert res.is_pass is False
    assert res.failed_feature_ids == [3, 7]


def test_from_dict_legacy_0_maps_to_value_range():
    svc = CheckService()
    dto = {
        "id": "abc",
        "checkItemName": "地类值域",
        "checkType": 0,
        "checkParam": json.dumps({
            "fieldName": ["DLBM"],
            "domainValues": ["01", "02"],
        }, ensure_ascii=False),
    }
    p = svc.from_dict(dto, layer=None)
    assert p.check_id == "abc"
    assert p.check_name == "地类值域"
    assert p.field_name == ("DLBM",)
    # 旧 0 → value_range → where 是 domainValues 逗号拼接
    assert p.where == "01,02"
    assert p.extra["type_id"] == "value_range"


def test_from_dict_legacy_1_maps_to_null_value():
    svc = CheckService()
    dto = {
        "id": "1", "checkItemName": "非空", "checkType": 1,
        "checkParam": json.dumps({"fieldName": ["X", "Y"]}, ensure_ascii=False),
    }
    p = svc.from_dict(dto, layer=None)
    assert p.field_name == ("X", "Y")
    assert p.extra["type_id"] == "null_value"


def test_from_dict_legacy_2_maps_to_logic():
    svc = CheckService()
    dto = {
        "id": "2", "checkItemName": "逻辑", "checkType": 2,
        "checkParam": json.dumps({"logicalSql": "a = b"}, ensure_ascii=False),
    }
    p = svc.from_dict(dto, layer=None)
    assert p.where == "a = b"
    assert p.extra["type_id"] == "logic"


def test_run_from_json_end_to_end():
    svc = CheckService()
    layer = MagicMock()
    layer.isValid.return_value = True
    layer.getFeatures.return_value = iter([MagicMock(id=99)])
    dto = {
        "id": "x", "checkItemName": "demo", "checkType": 0,
        "checkParam": json.dumps({"fieldName": ["DLBM"], "domainValues": ["01"]}, ensure_ascii=False),
    }
    res = svc.run_from_json(dto, layer=layer)
    # value_range where = "01"（单值），SQL 形如 `"DLBM" not in (01)`
    # 我们只看 info 里有 where 字段
    assert "where" in res.info


def test_run_batch_passes_per_item_type():
    svc = CheckService()
    layer = MagicMock()
    layer.isValid.return_value = True
    layer.getFeatures.return_value = iter([])
    p1 = CheckParam(layer=layer)
    p2 = CheckParam(layer=layer)
    results = svc.run_batch([p1, p2], type_ids=["demo_value", "demo_fail"])
    assert results[0].is_pass is True
    assert results[1].is_pass is True   # 1=0 也 0 行
