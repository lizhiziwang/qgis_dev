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
- :mod:`app.infra.logging.crash` —— 崩溃日志（``sys.excepthook`` +
  ``faulthandler``），启动早期调 :func:`install_crash_logging`
- :mod:`app.infra.logging.qgis_bridge` —— QGIS 消息日志 / Qt 消息处理器接进
  Python logging，``QgsApplication`` 建好后调 :func:`install_qgis_log_bridge`
- :mod:`app.infra.logging.operation` —— 矢量操作的 ``op`` 编号 + 入参快照，
  服务入口用 ``@log_operation("缓冲区生成")`` 加一行即可
- :mod:`app.infra.logging.session` —— 会话日志（一次运行一份纯文本 + JSONL），
  装配后调 :func:`install_session_logging` / :func:`current_session`
- :mod:`app.infra.logging.user_context` —— JSONL 里附带的当前登录用户 / 主机名 /
  操作系统，登录成功后调 :func:`set_log_user`
- :mod:`app.infra.logging.redaction` —— 回传前的凭据打码（``tk=`` / ``password=`` …），
  操作快照与诊断包共用同一份规则
- :mod:`app.infra.logging.environment` —— 构建 / 环境快照（``BUILD`` 日志行与诊断包里的
  ``environment.json`` 同源）
- :mod:`app.infra.logging.diagnostics` —— 诊断包导出：把会话 / 崩溃 / 配置 / 工程快照
  打成一个 zip（**导出前打码**），界面入口在日志面板
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
6. **三条采集线**：Python logging（本包）+ 崩溃 hook（crash）+ QGIS/Qt 自身
   日志（qgis_bridge）。缺任何一条，用户传上来的日志都定位不到根因。
7. **操作编号贯穿全局**：每条日志带 ``op=%(op_id)s``（filter 注入，见
   :mod:`app.infra.logging.operation`），把"用户点了什么"和随后刷出来的
   Python / QGIS / Qt / 崩溃日志串成一段。
8. **会话自包含**：一次运行另写一份 ``logs/session/<id>.log`` + ``<id>.jsonl``
   （见 :mod:`app.infra.logging.session`）。``app.log`` 是跨运行累积且会轮转的，
   用户回传时要的是"他这一次"的完整时间线；纯文本给人读，JSONL 给我用 ``jq`` 精确
   切段（按 ``op`` 捞整段、按 ``exc.type`` 归类）。JSONL 每条还带 ``user`` /
   ``user_id`` / ``host`` / ``os``，多人回传的日志靠它区分。
9. **回传的一律是打码过的**：凭据（XYZ 底图的 ``tk=``、目标库密码、``config/app.yaml``
   里的真实天地图 key）在**写日志时**（:mod:`app.infra.logging.redaction` 的
   ``RedactionFilter``）与**导出诊断包时**各过一道，规则只有一份。启动时还会把
   配置里的凭据真值登记进来（``register_secrets``），日志里**裸出现**的 key 同样
   打码；导出时再从包内那份配置现挖一遍。
"""

from __future__ import annotations

from .config import LogConfig
from .crash import install as install_crash_logging
from .diagnostics import DiagnosticsPackage
from .diagnostics import build_package as build_diagnostics_package
from .environment import build_snapshot
from .formatter import HtmlLogFormatter
from .logger import get_logger, setup_logger
from .operation import current_op_id, log_operation
from .qgis_bridge import install as install_qgis_log_bridge
from .redaction import (
    REDACTED,
    RedactionFilter,
    attach_redaction,
    collect_secret_values,
    known_secrets,
    redact,
    register_secrets,
)
from .renderer import log_to_html, render_log_html
from .session import SessionLog, current_session, list_sessions
from .session import install as install_session_logging
from .sink import _RootSink, active_log_config
from .styles import LogStyle
from .user_context import current_log_user, log_host, log_os, set_log_user

__all__ = [
    "REDACTED",
    "DiagnosticsPackage",
    "HtmlLogFormatter",
    "LogConfig",
    "LogStyle",
    "RedactionFilter",
    "SessionLog",
    "_RootSink",
    "active_log_config",
    "attach_redaction",
    "build_diagnostics_package",
    "build_snapshot",
    "collect_secret_values",
    "current_log_user",
    "current_op_id",
    "current_session",
    "get_logger",
    "install_crash_logging",
    "install_qgis_log_bridge",
    "install_session_logging",
    "known_secrets",
    "list_sessions",
    "log_host",
    "log_operation",
    "log_os",
    "log_to_html",
    "redact",
    "register_secrets",
    "render_log_html",
    "set_log_user",
    "setup_logger",
]
