"""预渲染 formatter，把日志行直接格式化为 HTML 片段，专供 file handler 使用。

文件里每行长这样（示例）::

    2026-08-03 12:00:00 - component.SpaceQuery - 1234:5678 - \\
    <font color="#ff0000"><b>ERROR</b></font> - \\
    <font color="#000000">__LOGDOCK::MSG_OPEN__SQL 失败 <script>__LOGDOCK::MSG_CLOSE__</font>

- 可信字段（时间戳 / 模块名 / PID-Thread / 级别）由本 formatter 直接套
  上 ``<font>`` 标签写入文件，渲染时无须再着色。
- 用户控制的 ``message`` 字段被 ``__LOGDOCK::MSG_OPEN__`` /
  ``__LOGDOCK::MSG_CLOSE__`` 这对唯一 marker 包住。marker 之间是原始
  用户文本，由 :func:`app.infra.logging.renderer.render_log_html` 用一次
  ``re.sub`` + ``html.escape`` 完成转义，避免 ``&<>"`` 之类字符破坏
  QTextBrowser 显示。
- 控制台 handler **不**使用本 formatter，仍输出纯文本，方便 grep。
"""

from __future__ import annotations

import logging
from typing import ClassVar, Dict, Tuple


class HtmlLogFormatter(logging.Formatter):
    """把日志行格式化为带 ``<font>`` 标签的 HTML 片段。"""

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
        "%(asctime)s - %(name)s.%(funcName)s:%(lineno)d - "
        "%(process)d:%(thread)d - op=%(op_id)s - "
        "%(levelname_html)s - %(message_html)s"
    )
    DATE_FORMAT: ClassVar[str] = "%Y-%m-%d %H:%M:%S"

    def __init__(self) -> None:
        # ``defaults``：op_id 由 handler 上的 filter 注入（见 operation.attach_filter）。
        # 万一有 handler 漏装 filter，退化成 ``-`` 而不是让整条记录格式化失败。
        super().__init__(self.LOG_FORMAT, datefmt=self.DATE_FORMAT, defaults={"op_id": "-"})

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
