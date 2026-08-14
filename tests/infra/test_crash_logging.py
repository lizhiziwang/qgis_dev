"""测试崩溃日志装配 :mod:`app.infra.logging.crash`。

不需要 QGIS：纯 Python hooks。
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

from app.infra.logging import crash


@pytest.fixture(autouse=True)
def _isolated_crash_logging():
    """每个测试前都复位，避免互相污染全局 hooks。"""
    crash.reset()
    yield
    crash.reset()


def test_install_is_idempotent(tmp_path: Path):
    crash.install(tmp_path)
    crash.install(tmp_path)
    assert crash._installed is True
    assert crash._original_excepthook is not None


def test_crash_log_captures_uncaught_exception(tmp_path: Path):
    crash.install(tmp_path)
    assert (tmp_path / "crash_faulthandler.log").exists()

    def _boom():
        raise ValueError("boom from slot")

    try:
        _boom()
    except ValueError:
        sys.excepthook(*sys.exc_info())

    content = (tmp_path / "crash.log").read_text(encoding="utf-8")
    assert "未捕获异常: ValueError: boom from slot" in content
    assert "_boom" in content
    assert "test_crash_log_captures_uncaught_exception" in content


def test_crash_log_captures_custom_exception(tmp_path: Path):
    crash.install(tmp_path)

    class MyErr(Exception):
        pass

    def _slot():
        raise MyErr("qgis slot TypeError 之类")

    try:
        _slot()
    except MyErr:
        sys.excepthook(*sys.exc_info())

    content = (tmp_path / "crash.log").read_text(encoding="utf-8")
    assert "MyErr: qgis slot TypeError 之类" in content


def test_unraisable_hook_writes_log(tmp_path: Path, monkeypatch):
    crash.install(tmp_path)

    from types import SimpleNamespace

    try:
        raise RuntimeError("del boom")
    except RuntimeError as exc:
        args = SimpleNamespace(
            exc_type=RuntimeError,
            exc_value=exc,
            exc_traceback=exc.__traceback__,
            object="BadDel.__del__",
        )
        # 不链到 pytest 自带的 unraisable hook（它会把 unraisable 当成测试失败）
        saved = crash._original_unraisablehook
        monkeypatch.setattr(crash, "_original_unraisablehook", lambda args: None)
        try:
            sys.unraisablehook(args)
        finally:
            monkeypatch.setattr(crash, "_original_unraisablehook", saved)

    content = (tmp_path / "crash.log").read_text(encoding="utf-8")
    assert "unraisable" in content
    assert "del boom" in content


def test_reset_restores_original_hooks(tmp_path: Path):
    orig_exc = sys.excepthook
    orig_unr = sys.unraisablehook

    crash.install(tmp_path)
    assert sys.excepthook is not orig_exc

    crash.reset()
    assert sys.excepthook is orig_exc
    assert sys.unraisablehook is orig_unr
    assert crash._installed is False
