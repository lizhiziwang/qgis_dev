"""测试 :class:`app.infra.logging`。"""

from __future__ import annotations

import logging
import os
import re
from pathlib import Path

import pytest

from app.infra.logging import (
    HtmlLogFormatter,
    LogConfig,
    LogStyle,
    _RootSink,
    get_logger,
    render_log_html,
)
from app.infra.logging.renderer import _MSG_PATTERN


def test_log_config_from_env_overrides_dir(monkeypatch, tmp_path: Path):
    d = tmp_path / "x"
    monkeypatch.setenv("QGIS_DEV_LOG_DIR", str(d))
    monkeypatch.delenv("QGIS_DEV_LOG_LEVEL", raising=False)

    cfg = LogConfig.from_env()
    assert cfg.log_dir == d


def test_log_config_from_env_overrides_level(monkeypatch):
    monkeypatch.setenv("QGIS_DEV_LOG_LEVEL", "WARNING")
    monkeypatch.delenv("QGIS_DEV_LOG_DIR", raising=False)
    cfg = LogConfig.from_env()
    assert cfg.console_level == logging.WARNING
    assert cfg.root_level == logging.WARNING


def test_log_style_html_default_level():
    color, bold = LogStyle.html("INFO")
    assert color == "#008000"
    assert bold is False


def test_log_style_html_unknown_falls_back_to_info():
    color, bold = LogStyle.html("BOGUS")
    assert color == "#008000"
    assert bold is False


def test_log_style_html_error_is_bold():
    color, bold = LogStyle.html("ERROR")
    assert color == "#ff0000"
    assert bold is True


def test_html_log_formatter_wraps_message_with_marker():
    fmt = HtmlLogFormatter()
    record = logging.LogRecord(
        name="x", level=logging.INFO, pathname=__file__, lineno=1,
        msg="hello <script>", args=(), exc_info=None,
    )
    out = fmt.format(record)
    assert HtmlLogFormatter.MESSAGE_OPEN in out
    assert HtmlLogFormatter.MESSAGE_CLOSE in out
    assert "hello <script>" in out
    # level 套了 font
    assert '<font color="#008000">INFO</font>' in out


def test_render_log_html_escapes_message_only():
    line = (
        f'2026 - x - 1:1 - '
        f'<font color="#008000">INFO</font> - '
        f'{HtmlLogFormatter.MESSAGE_OPEN}hello <script>{HtmlLogFormatter.MESSAGE_CLOSE}'
    )
    out = render_log_html(line)
    # 可信字段不被转义
    assert '<font color="#008000">INFO</font>' in out
    # 用户消息里的 < 被转义
    assert "hello &lt;script&gt;" in out
    # marker 被剥掉
    assert HtmlLogFormatter.MESSAGE_OPEN not in out
    assert HtmlLogFormatter.MESSAGE_CLOSE not in out


def test_render_log_html_preserves_lines_without_marker():
    raw = "old format line"
    assert render_log_html(raw) == raw


def test_sink_idempotent(monkeypatch, tmp_log_dir):
    monkeypatch.setenv("QGIS_DEV_LOG_DIR", str(tmp_log_dir))
    cfg = LogConfig.from_env()
    _RootSink.configure(cfg)
    n1 = len(logging.getLogger().handlers)
    _RootSink.configure(cfg)   # 二次调用不应增加 handler
    n2 = len(logging.getLogger().handlers)
    assert n1 == n2


def test_get_logger_returns_named_logger():
    log = get_logger("tests.demo")
    assert log.name == "tests.demo"
    assert log.propagate is True


def test_get_logger_none_returns_root():
    log = get_logger(None)
    assert log.name == "Application"


def test_msg_pattern_compiles():
    pat = re.compile(re.escape(HtmlLogFormatter.MESSAGE_OPEN) + r".+?" + re.escape(HtmlLogFormatter.MESSAGE_CLOSE))
    assert pat is not None
    assert _MSG_PATTERN is not None
