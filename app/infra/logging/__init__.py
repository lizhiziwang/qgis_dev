"""项目级集中式日志。

对外 API（向后兼容旧 ``from component.Logger import *``）：

- :func:`get_logger` —— 推荐入口
- :func:`setup_logger` —— 旧名别名
- :func:`render_log_html` —— 日志行 → HTML 片段，供 ``LogDock`` 渲染
- :func:`log_to_html` —— 旧名别名
- :class:`LogConfig`、:class:`LogStyle`、:class:`HtmlLogFormatter`

实现拆到子模块：

- :mod:`app.infra.logging.config` —— ``LogConfig`` 数据类
- :mod:`app.infra.logging.styles` —— 颜色 / 加粗映射
- :mod:`app.infra.logging.formatter` —— ``HtmlLogFormatter``
- :mod:`app.infra.logging.sink` —— ``_RootSink`` 单例装配
- :mod:`app.infra.logging.renderer` —— ``render_log_html``

设计要点：

1. **单例 root + 子 logger 传播**：根 logger 只挂一次 handlers；子 logger 通过
   ``propagate`` 冒泡到根，杜绝"同一行日志写两次到文件"。
2. **错误日志分离**：`error.log` 只记录 ERROR 及以上，``app.log`` 记录全量。
3. **绝对路径**：以本模块所在目录为基准解析日志目录，跨工作目录运行与
   PyInstaller 打包后都可定位。
4. **文件预渲染 HTML**：file handler 把日志行直接写成带 ``<font>`` 的成品；
   用户控制的 ``message`` 字段用唯一 marker 包围，渲染器做一次正则替换做
   HTML 转义。
5. **样式与逻辑分层**：颜色映射、表单解析、HTML 渲染、根 logger 装配各自独立。
"""

from __future__ import annotations

from .config import LogConfig
from .formatter import HtmlLogFormatter
from .logger import get_logger, setup_logger
from .renderer import log_to_html, render_log_html
from .sink import _RootSink
from .styles import LogStyle

__all__ = [
    "HtmlLogFormatter",
    "LogConfig",
    "LogStyle",
    "_RootSink",
    "get_logger",
    "log_to_html",
    "render_log_html",
    "setup_logger",
]
