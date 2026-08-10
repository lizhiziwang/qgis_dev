"""测试 :func:`app.infra.messagebar.send` / :func:`sendMessageToTopParent`。"""

from __future__ import annotations

import pytest

pytest.importorskip("qgis", reason="messagebar 用 qgis._core.Qgis")


class _FakeBar:
    def __init__(self):
        self.calls = []

    def pushMessage(self, msg, level=None, duration=None):
        self.calls.append((msg, level, duration))


def test_send_unknown_level_raises():
    from app.infra.messagebar import send

    with pytest.raises(ValueError):
        send("bogus", "msg")


def test_send_returns_false_when_no_bar(monkeypatch):
    from app.infra.messagebar import qgis_bar

    monkeypatch.setattr(qgis_bar, "_get_bar", lambda: None)
    from app.infra.messagebar import send

    assert send("warning", "x") is False


def test_send_pushes_message(monkeypatch):
    from app.infra.messagebar import qgis_bar
    from qgis._core import Qgis

    bar = _FakeBar()
    monkeypatch.setattr(qgis_bar, "_get_bar", lambda: bar)
    from app.infra.messagebar import send

    assert send("warning", "操作失败", duration=5) is True
    assert bar.calls == [("操作失败", Qgis.Warning, 5)]


def test_legacy_send_message_to_top_parent_maps_numbers(monkeypatch):
    from app.infra.messagebar import qgis_bar
    from qgis._core import Qgis

    bar = _FakeBar()
    monkeypatch.setattr(qgis_bar, "_get_bar", lambda: bar)
    from app.infra.messagebar import sendMessageToTopParent

    sendMessageToTopParent(2, "msg", 7)
    assert bar.calls == [("msg", Qgis.Warning, 7)]
