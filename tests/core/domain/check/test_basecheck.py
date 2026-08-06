"""测试 :class:`BaseCheck.run` 在 :class:`QgsVectorLayer` mock 场景下的行为。

需要能 import qgis（即使 layer 是 mock）—— 因为 :class:`BaseCheck.run`
内部要 ``from qgis.core import QgsFeatureRequest``。
"""

from __future__ import annotations

import pytest

pytest.importorskip("qgis", reason="BaseCheck.run 内部要 import qgis.core")

from unittest.mock import MagicMock

from app.core.domain.check.param import CheckParam
from app.core.domain.check.registry import BaseCheck, get_check_cls, register


def test_run_returns_pass_when_no_features():
    @register("dummy_pass")
    class Dummy(BaseCheck):
        def build_where(self, p: CheckParam) -> str:
            return "1=0"

    layer = MagicMock()
    layer.isValid.return_value = True
    layer.getFeatures.return_value = iter([])

    res = Dummy().run(CheckParam(layer=layer))
    assert res.is_pass is True
    assert res.failed_feature_ids == []


def test_run_returns_fail_when_features_match():
    @register("dummy_fail")
    class Dummy(BaseCheck):
        def build_where(self, p: CheckParam) -> str:
            return "n = 'x'"

    layer = MagicMock()
    layer.isValid.return_value = True
    f1, f2 = MagicMock(), MagicMock()
    f1.id.return_value = 42
    f2.id.return_value = 43
    layer.getFeatures.return_value = iter([f1, f2])

    res = Dummy().run(CheckParam(layer=layer))
    assert res.is_pass is False
    assert res.failed_feature_ids == [42, 43]


def test_run_catches_build_where_exception():
    @register("dummy_broken")
    class Broken(BaseCheck):
        def build_where(self, p: CheckParam) -> str:
            raise RuntimeError("oops")

    res = Broken().run(CheckParam(layer=MagicMock()))
    assert res.is_pass is False
    assert "build_where" in res.info.get("error", "")


def test_run_catches_sql_exception():
    @register("dummy_broken_sql")
    class BrokenSql(BaseCheck):
        def build_where(self, p: CheckParam) -> str:
            return "INVALID SQL"

    layer = MagicMock()
    layer.isValid.return_value = True
    layer.getFeatures.side_effect = Exception("parse error")

    res = BrokenSql().run(CheckParam(layer=layer))
    assert res.is_pass is False
    assert "SQL 执行失败" in res.info.get("error", "")


def test_get_check_cls_returns_registered_class():
    @register("zzz_unique_xyz")
    class Z(BaseCheck):
        def build_where(self, p: CheckParam) -> str:
            return "1=1"

    assert get_check_cls("zzz_unique_xyz") is Z
