# -*- coding: utf-8 -*-
"""项目级集中式日志模块。

本模块对外提供：

- ``get_logger(name)`` —— 项目推荐的统一入口。
- ``setup_logger(name)`` —— 旧版 API 别名，向后兼容既有调用点。
- ``render_log_html(line)`` / ``log_to_html(line)`` —— 日志行 → HTML 片段，
  供 :class:`component.LogDock.LogDock` 在 ``QTextBrowser`` 中渲染使用。
- :class:`HtmlLogFormatter` —— 文件 handler 专用，把日志行预渲染为
  带 ``<font>`` 的成品写入文件。

设计要点：

1. **单例 root + 子 logger 传播**：根 logger（Python 的 root logger）只挂一次
   handlers，模块通过 ``get_logger(__name__)`` 拿到的子 logger 通过 ``propagate``
   冒泡到根，杜绝"同一行日志写两次到文件"。
2. **错误日志分离**：`error.log` 只记录 ERROR 及以上，``app.log`` 记录全量。
   原实现两个 file handler 都写 ``app.log``，现已修复。
3. **绝对路径**：以本文件所在目录为基准解析日志目录，跨工作目录运行与
   PyInstaller 打包后都可定位。
4. **文件预渲染 HTML**：file handler 用 :class:`HtmlLogFormatter`，把日志行
   直接写成带 ``<font>`` 的成品；用户控制的 message 字段用唯一 marker 包围，
   渲染器做一次正则替换做 HTML 转义，不再 split / 拆字段。
5. **样式与逻辑分层**：颜色映射、表单解析、HTML 渲染、根 logger 装配各自独立。
"""
from __future__ import annotations

import html as _std_html
import logging
import os
import re
import sys
from dataclasses import dataclass
from logging.handlers import RotatingFileHandler
from typing import ClassVar, Dict, Optional, Tuple


# ---------------------------------------------------------------------------
# 路径定位
# ---------------------------------------------------------------------------
_MODULE_DIR = os.path.dirname(os.path.abspath(__file__))          # .../component
_PROJECT_ROOT = os.path.dirname(_MODULE_DIR)                      # 项目根目录
_DEFAULT_LOG_DIR = os.path.join(_PROJECT_ROOT, "logs")

# 环境变量覆盖（部署 / 打包时可临时切换）
_ENV_LOG_DIR = "QGIS_DEV_LOG_DIR"
_ENV_LOG_LEVEL = "QGIS_DEV_LOG_LEVEL"


# ---------------------------------------------------------------------------
# 样式定义（颜色、加粗集中在一处，HTML / 控制台两套配色）
# ---------------------------------------------------------------------------
class LogStyle:
    """日志级别 → 颜色 / 加粗 的映射。

    在 HTML 与 ANSI 控制台两种渲染目标上各保留一份。
    颜色取值按级别向上递进（DEBUG → CRITICAL 视觉权重依次变重）。
    """

    HTML_COLORS: ClassVar[Dict[str, str]] = {
        "DEBUG":    "#4169e1",   # 蓝色
        "INFO":     "#008000",   # 绿色
        "WARNING":  "#ff8c00",   # 橙色
        "ERROR":    "#ff0000",   # 红色
        "CRITICAL": "#8b0000",   # 深红色
    }
    HTML_BOLD: ClassVar[Dict[str, bool]] = {
        "ERROR":    True,
        "CRITICAL": True,
    }
    ANSI_COLORS: ClassVar[Dict[str, str]] = {
        "DEBUG":    "\033[34m",
        "INFO":     "\033[32m",
        "WARNING":  "\033[33m",
        "ERROR":    "\033[31m",
        "CRITICAL": "\033[1;31m",
    }
    ANSI_RESET: ClassVar[str] = "\033[0m"

    @classmethod
    def html(cls, level: str) -> Tuple[str, bool]:
        """返回 ``(颜色字符串, 是否加粗)``；未知级别回退到 INFO。"""
        level = (level or "INFO").upper()
        return (
            cls.HTML_COLORS.get(level, cls.HTML_COLORS["INFO"]),
            cls.HTML_BOLD.get(level, False),
        )


# ---------------------------------------------------------------------------
# 配置数据类
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class LogConfig:
    """日志系统配置项。

    全部字段都是只读快照，构建后不可变。运行时如需修改请重新构造实例并
    调用 :func:`get_logger`，或在调用前修改环境变量。
    """

    log_dir: str = _DEFAULT_LOG_DIR
    console_level: int = logging.INFO
    root_level: int = logging.DEBUG
    file_max_bytes: int = 10 * 1024 * 1024   # 10 MB
    file_backup_count: int = 5
    file_encoding: str = "utf-8"
    root_logger_name: str = "Application"
    all_log_filename: str = "app.log"
    error_log_filename: str = "error.log"
    log_format: str = (
        "%(asctime)s - %(name)s - %(process)d:%(thread)d - "
        "__%(levelname)s__ - %(message)s"
    )
    date_format: str = "%Y-%m-%d %H:%M:%S"

    @classmethod
    def from_env(cls) -> "LogConfig":
        """根据环境变量生成配置覆盖项。"""
        overrides: Dict[str, object] = {}
        env_dir = os.environ.get(_ENV_LOG_DIR)
        if env_dir:
            overrides["log_dir"] = env_dir
        env_level = os.environ.get(_ENV_LOG_LEVEL)
        if env_level:
            numeric = logging.getLevelName(env_level.upper())
            if isinstance(numeric, int):
                overrides["root_level"] = numeric
                overrides["console_level"] = numeric
        if overrides:
            return cls(**overrides)  # type: ignore[arg-type]
        return cls()


# ---------------------------------------------------------------------------
# 预渲染 formatter
# ---------------------------------------------------------------------------
class HtmlLogFormatter(logging.Formatter):
    """把日志行直接格式化为 HTML 片段，专供 file handler 使用。

    文件里每行长这样（示例）::

        2026-08-03 12:00:00 - component.SpaceQuery - 1234:5678 - \\
        <font color="#ff0000"><b>ERROR</b></font> - \\
        <font color="#000000">__LOGDOCK::MSG_OPEN__SQL 失败 <script>__LOGDOCK::MSG_CLOSE__</font>

    - 可信字段（时间戳 / 模块名 / PID-Thread / 级别）由本 formatter 直接套
      上 ``<font>`` 标签写入文件，渲染时无须再着色。
    - 用户控制的 ``message`` 字段被 ``__LOGDOCK::MSG_OPEN__`` /
      ``__LOGDOCK::MSG_CLOSE__`` 这对唯一 marker 包住。marker 之间是原始
      用户文本，由 :func:`render_log_html` 用一次 ``re.sub`` + ``html.escape``
      完成转义，避免 ``&<>"`` 之类字符破坏 QTextBrowser 显示。
    - 控制台 handler **不**使用本 formatter，仍输出纯文本，方便 grep。
    """

    LEVEL_STYLES: ClassVar[Dict[int, Tuple[str, bool]]] = {
        logging.DEBUG:    ("#4169e1", False),
        logging.INFO:     ("#008000", False),
        logging.WARNING:  ("#ff8c00", False),
        logging.ERROR:    ("#ff0000", True),
        logging.CRITICAL: ("#8b0000", True),
    }

    # 唯一且足够长的 marker；用户消息里出现完全相同字符串的概率近似 0
    MESSAGE_OPEN: ClassVar[str] = "__LOGDOCK::MSG_OPEN__"
    MESSAGE_CLOSE: ClassVar[str] = "__LOGDOCK::MSG_CLOSE__"

    LOG_FORMAT: ClassVar[str] = (
        "%(asctime)s - %(name)s - %(process)d:%(thread)d - "
        "%(levelname_html)s - %(message_html)s"
    )
    DATE_FORMAT: ClassVar[str] = "%Y-%m-%d %H:%M:%S"

    def __init__(self) -> None:
        super().__init__(self.LOG_FORMAT, datefmt=self.DATE_FORMAT)

    def format(self, record: logging.LogRecord) -> str:
        # 1) 级别：套上颜色 / 加粗的 <font> 标签
        color, bold = self.LEVEL_STYLES.get(
            record.levelno, self.LEVEL_STYLES[logging.INFO]
        )
        bold_open, bold_close = ("<b>", "</b>") if bold else ("", "")
        record.levelname_html = (
            f'<font color="{color}">{bold_open}{record.levelname}{bold_close}</font>'
        )

        # 2) 消息：保持原始文本，外面套 marker，让 renderer 决定要不要 escape
        raw_msg = record.getMessage()
        record.message_html = f"{self.MESSAGE_OPEN}{raw_msg}{self.MESSAGE_CLOSE}"

        # 3) 用父类做时间戳格式化 + 模板字段替换
        return super().format(record)


# ---------------------------------------------------------------------------
# 单例根 logger 装配
# ---------------------------------------------------------------------------
class _RootSink:
    """维护 Python root logger 上的 handlers。

    所有 handler 在第一次装配时被加上，之后保持幂等。这样既保证每个进程只
    有一组统一的输出目标，也允许单元测试中通过 ``reset`` 强制重新装配。
    """

    _configured: bool = False

    @classmethod
    def configure(cls, config: LogConfig) -> None:
        if cls._configured:
            return

        os.makedirs(config.log_dir, exist_ok=True)

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
        )
        # 文件：预渲染 HTML，所有样式直接由 formatter 写进文件
        html_formatter = HtmlLogFormatter()

        # 1) 控制台：默认 INFO 及以上写到 stderr
        console = logging.StreamHandler(stream=sys.stderr)
        console.setLevel(config.console_level)
        console.setFormatter(plain_formatter)

        # 2) 全量日志：rotating file，DEBUG 及以上，写预渲染 HTML
        all_file = cls._make_rotating(
            os.path.join(config.log_dir, config.all_log_filename),
            config,
            html_formatter,
            level=config.root_level,
        )

        # 3) 错误日志：单独文件，仅 ERROR / CRITICAL
        error_file = cls._make_rotating(
            os.path.join(config.log_dir, config.error_log_filename),
            config,
            html_formatter,
            level=logging.ERROR,
        )

        root.addHandler(console)
        root.addHandler(all_file)
        root.addHandler(error_file)

        cls._configured = True

    @staticmethod
    def _make_rotating(
        path: str,
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
        """测试用：解除装配标志。"""
        cls._configured = False


# ---------------------------------------------------------------------------
# 公共 API
# ---------------------------------------------------------------------------
def get_logger(name: Optional[str] = None) -> logging.Logger:
    """获取一个 logger 实例。

    首次调用时会装配项目级根 logger（写入 ``logs/app.log`` / ``logs/error.log``、
    输出到 stderr）。后续调用按 ``name`` 返回子 logger，子 logger 通过
    ``propagate`` 冒泡到根，从而保证每条日志只写一次文件、只输出一次控制台。

    Parameters
    ----------
    name:
        通常传模块级 ``__name__``；传入 ``None`` 或 ``"__main__"`` 时
        返回项目根 logger（名为 ``"Application"``）。

    Returns
    -------
    logging.Logger
        已经接入全局 handler 链的 logger，可以直接调用 ``.info`` / ``.error``。
    """
    cfg = LogConfig.from_env()
    _RootSink.configure(cfg)

    if not name or name == "__main__":
        return logging.getLogger(cfg.root_logger_name)

    child = logging.getLogger(name)
    child.setLevel(cfg.root_level)
    child.propagate = True
    return child


# ---- 兼容旧 API ----
def setup_logger(name: Optional[str] = None) -> logging.Logger:
    """``setup_logger`` 旧版别名。"""
    return get_logger(name)


# ---------------------------------------------------------------------------
# HTML 渲染
# ---------------------------------------------------------------------------
# 文件里 message 被 HtmlLogFormatter 写出的唯一 marker 包住，渲染器只对
# marker 之间（即用户原始消息）做一次 html.escape。marker 通过 re.escape
# 转义后做正则编译，避免任何字符被误解。
_MSG_PATTERN = re.compile(
    re.escape(HtmlLogFormatter.MESSAGE_OPEN)
    + r"([\s\S]*?)"
    + re.escape(HtmlLogFormatter.MESSAGE_CLOSE)
)


def render_log_html(line: str) -> str:
    """对日志行做最后一步：HTML 转义 marker 之间（即用户 message）的字符。

    :class:`HtmlLogFormatter` 已经把可信字段（时间、模块、PID、级别）按
    颜色 / 加粗套好了 ``<font>`` 标签写进文件，渲染时无须再着色。本函数
    只做一次正则替换 —— 把 ``__LOGDOCK::MSG_OPEN__...__LOGDOCK::MSG_CLOSE__``
    之间可能含 ``&<>`` 的原始用户文本做 HTML 转义。

    对未识别 marker 的旧格式行（升级前残留）原样返回即可，由 QTextBrowser
    当作纯文本显示。
    """
    return _MSG_PATTERN.sub(lambda m: _std_html.escape(m.group(1)), line)


# ---- 兼容旧 API ----
def log_to_html(line: str) -> str:
    """``log_to_html`` 旧版别名。"""
    return render_log_html(line)


__all__ = [
    "LogConfig",
    "LogStyle",
    "HtmlLogFormatter",
    "get_logger",
    "setup_logger",
    "render_log_html",
    "log_to_html",
]  # noqa: E501