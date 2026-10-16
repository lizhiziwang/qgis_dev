"""单例根 logger 装配。

维护 Python root logger 上的 handlers。所有 handler 在第一次装配时被加上，
之后保持幂等。这样既保证每个进程只有一组统一的输出目标，也允许单元测试中
通过 :func:`reset` 强制重新装配。
"""

from __future__ import annotations

import logging
import sys
from logging.handlers import RotatingFileHandler

from .config import LogConfig
from .formatter import HtmlLogFormatter
from .operation import attach_filter


class _RootSink:
    """维护 Python root logger 上的 handlers。"""

    _configured: bool = False

    @classmethod
    def configure(cls, config: LogConfig) -> None:
        """幂等装配。多次调用只在第一次生效。"""
        if cls._configured:
            return

        config.ensure()

        root = logging.getLogger()                     # Python 的 root logger
        root.setLevel(config.root_level)
        # 清掉可能由 logging.basicConfig 或其它库安装的 handler，避免重复输出
        for h in list(root.handlers):
            root.removeHandler(h)
        root.propagate = True

        # 控制台：纯文本，仍走 LogConfig.log_format，方便终端 grep
        plain_formatter = logging.Formatter(
            config.log_format,
            datefmt=config.date_format,
            # op=%(op_id)s 由 handler 上的 filter 注入；漏装时退化成 ``-``
            defaults={"op_id": "-"},
        )
        # 文件：预渲染 HTML，所有样式直接由 formatter 写进文件
        html_formatter = HtmlLogFormatter()

        # 1) 控制台：默认 INFO 及以上写到 stderr
        console = logging.StreamHandler(stream=sys.stderr)
        console.setLevel(config.console_level)
        console.setFormatter(plain_formatter)

        # 2) 全量日志：rotating file，DEBUG 及以上，写预渲染 HTML
        all_file = cls._make_rotating(
            config.log_dir / config.all_log_filename,
            config,
            html_formatter,
            level=config.root_level,
        )

        # 3) 错误日志：单独文件，仅 ERROR / CRITICAL
        error_file = cls._make_rotating(
            config.log_dir / config.error_log_filename,
            config,
            html_formatter,
            level=logging.ERROR,
        )

        for handler in (console, all_file, error_file):
            root.addHandler(attach_filter(handler))

        cls._configured = True

    @staticmethod
    def _make_rotating(
        path,
        config: LogConfig,
        formatter: logging.Formatter,
        level: int,
    ) -> RotatingFileHandler:
        handler = RotatingFileHandler(
            path,
            maxBytes=config.file_max_bytes,
            backupCount=config.file_backup_count,
            encoding=config.file_encoding,
        )
        handler.setLevel(level)
        handler.setFormatter(formatter)
        return handler

    @classmethod
    def reset(cls) -> None:
        """测试用：解除装配标志并清空 handlers。"""
        root = logging.getLogger()
        for h in list(root.handlers):
            root.removeHandler(h)
        cls._configured = False
