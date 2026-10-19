"""单例根 logger 装配。

维护 Python root logger 上的 handlers。所有 handler 在第一次装配时被加上，
之后保持幂等。这样既保证每个进程只有一组统一的输出目标，也允许单元测试中
通过 :func:`reset` 强制重新装配。
"""

from __future__ import annotations

import logging
import sys
from logging.handlers import RotatingFileHandler
from typing import ClassVar

from .config import LogConfig
from .formatter import HtmlLogFormatter
from .operation import attach_filter
from .redaction import attach_redaction
from .session import reset as reset_session


class _RootSink:
    """维护 Python root logger 上的 handlers。"""

    _configured: bool = False
    #: 当前生效的配置（:func:`configure` 时记下）。导入期 `__file__` 推导的
    #: 默认目录在打包后指向 `_internal/logs`，与真正写入的目录不同；需要
    #: "日志写到哪"的调用方（如日志面板）必须在**运行时**问这里，而不是在
    #: 模块导入时算一个常量。
    _active_config: ClassVar[LogConfig | None] = None
    #: 本类建出来的 handler。**只关这些**——root 上还可能有别人的 handler
    #: （pytest 的 caplog 就是），关掉别人正在用的流会直接破坏调用方。
    _handlers: ClassVar[list[logging.Handler]] = []

    @classmethod
    def active_config(cls) -> LogConfig:
        """当前生效的日志配置；尚未装配时按环境变量推导（不落盘）。"""
        return cls._active_config or LogConfig.from_env()

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
            # 编号注入（op=…）+ 凭据打码（tk=… / password=…）：见各自模块。
            # 打码挂在这里而不是"导出诊断包时再抹"，是因为用户不一定按我们想的走
            # ——他可能直接把 logs/app.log 拖给别人（见 app.infra.logging.redaction）。
            root.addHandler(attach_redaction(attach_filter(handler)))
        cls._handlers = [console, all_file, error_file]

        cls._active_config = config
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
        """测试用：解除装配标志并清空 handlers。

        连会话日志一起拆（:mod:`app.infra.logging.session` 的 handler 也挂在 root
        上）：root 上的东西被清空、而会话模块还记着"已装配"，它就会在下一次
        :func:`~app.infra.logging.session.install` 时直接返回、**无声地不再记录**。

        自己建的 handler 要 ``close()``（不关的话文件句柄悬着：Windows 上日志目录
        删不掉，测试里表现为"清理失败"这种跟断言无关的怪错）。别人的 handler 只摘
        不关——caplog 之类的记录器还要继续用那份流。
        """
        reset_session()

        root = logging.getLogger()
        for handler in cls._handlers:
            root.removeHandler(handler)
            handler.close()
        cls._handlers = []
        for handler in list(root.handlers):
            root.removeHandler(handler)
        cls._active_config = None
        cls._configured = False


def active_log_config() -> LogConfig:
    """当前生效的日志配置。

    给"运行时才知道日志写到哪"的调用方用（日志面板读文件、清空日志等）。
    不要在模块顶层缓存它——打包后导入期与 ``_configure_logging`` 之后的
    日志目录可能不是同一个（``__file__`` 在 ``_internal/`` 下）。
    """
    return _RootSink.active_config()
