"""HTML 渲染：把 :class:`HtmlLogFormatter` 写在文件里的 marker 之间内容做 HTML 转义。

文件里 ``message`` 字段被 :class:`HtmlLogFormatter` 写出的唯一 marker 包住，
渲染器只对 marker 之间（即用户原始消息）做一次 ``html.escape``。marker 通过
``re.escape`` 转义后做正则编译，避免任何字符被误解。
"""

from __future__ import annotations

import html as _std_html
import re

from .formatter import HtmlLogFormatter

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
