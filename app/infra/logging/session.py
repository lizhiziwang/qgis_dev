"""会话日志：一次运行 = 一份自包含的「纯文本 + JSONL」。

**为什么还要一个会话日志**（``app.log`` / ``error.log`` 已经在记了）：

- ``app.log`` 是**跨运行累积**的：轮转按大小走（10 MB × 5），用户跑了一天之后，他
  出问题那一刻的行可能已经被挤进 ``app.log.3``，甚至滚出去了。会话日志按**进程**
  切分，一次运行一对文件，用户传上来的就是他这一次的完整记录。
- ``app.log`` 的文件内容是**预渲染 HTML**（``<font>`` + ``__LOGDOCK::`` 标记），指望
  用户直接双击打开来读是不现实的，我这边 grep 也得先扒标签。纯文本会话日志是给人看
  的**成品**，也是唯一一个能被随手 ``grep`` 的文件。
- JSONL 是**同一份记录的机器可读形态**：一条记录一行 JSON，``op`` / ``level`` /
  ``line`` / ``exc.stack`` 都是独立字段。要"把 op7 那一段整段捞出来"、
  "按耗时排队"、"数一数某类报错出现几次"，用 ``jq`` 比在 HTML 里做正则可靠得多。

三条采集线（Python logging、崩溃 hook、QGIS/Qt 日志）都冒泡到 root logger，所以会话
文件里天然是**同一份完整时间线**——崩溃那几行、QGIS 自己吐的 OGR 报错、以及
"用户点了什么"的 ``OP`` 行，都在同一个文件里按时间排好。这是现场排查最需要的东西。

保留策略：每启动一次清一次旧的，只留最近 :attr:`LogConfig.session_keep` 次会话
（含各自轮转出来的备份）；单个会话本身也有大小上限，避免长跑一整天把便携包塞满。
"""

from __future__ import annotations

import contextlib
import json
import logging
import os
import re
import time
import traceback
from dataclasses import dataclass
from datetime import datetime
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Any

from .config import LogConfig
from .operation import attach_filter
from .redaction import attach_redaction
from .user_context import current_log_user, log_host, log_os

logger = logging.getLogger(__name__)

#: 会话文件所在的子目录（在 ``log_dir`` 下；第三步的诊断包就按这个目录收）
SESSION_DIR_NAME = "session"

#: 会话 id：``20261017-091533-p12345``。本地时间 + 进程号——同一个人开两个实例时
#: 两份文件不会互相覆盖；时间在前让目录按名字排就是时间序。
_ID_FORMAT = "%Y%m%d-%H%M%S"
_ID_PATTERN = re.compile(r"^\d{8}-\d{6}-p\d+$")

_TEXT_SUFFIX = ".log"
_JSONL_SUFFIX = ".jsonl"

#: 纯文本会话的格式。**故意与 ``LogConfig.log_format`` 不同**：那边是用给控制台的
#: ``__ERROR__`` 标记（历史遗留的着色约定），这里是给人读 / 给 grep 的成品文本，
#: 级别就写 ``ERROR``，不加下划线。
_TEXT_FORMAT = (
    "%(asctime)s - %(name)s.%(funcName)s:%(lineno)d - "
    "%(process)d:%(thread)d - op=%(op_id)s - %(levelname)s - %(message)s"
)

#: 当前会话与它装上的 handler（模块级单例：整个进程只该有一对会话文件）
_current: SessionLog | None = None
_handlers: list[logging.Handler] = []


@dataclass(frozen=True)
class SessionLog:
    """一次运行的会话文件集合。"""

    #: 会话 id（同时是文件名前缀），如 ``20261017-091533-p12345``
    session_id: str
    #: 会话目录（``<log_dir>/session``）
    directory: Path
    #: 纯文本会话（``<id>.log``）
    text_path: Path
    #: 结构化会话（``<id>.jsonl``）
    jsonl_path: Path

    def files(self) -> list[Path]:
        """本会话已落地的全部文件（含轮转出来的 ``.1`` 备份）。

        第三步导出诊断包时按这个列表收文件，所以**别按文件名硬拼**——轮转过的会话
        是 ``<id>.log`` + ``<id>.log.1`` 两个文件，漏了就把发现问题的那段丢了。
        """
        return sorted(p for p in self.directory.glob(f"{self.session_id}.*") if p.is_file())


def install(log_dir: Path, config: LogConfig) -> SessionLog:
    """在当前进程的 root logger 上装一对会话 handler（幂等），返回本次会话。

    :param log_dir: 日志目录（会话文件写进它下面的 ``session/``）。
    :param config: 取大小上限 / 备份数 / 保留次数等。

    装在 root 上是因为三条采集线都冒泡到这里。root 的级别若比 ``config.root_level``
    更严（如默认的 ``WARNING``）就调低——装了会话日志却只收到 WARNING 以上，等于白装。
    """
    global _current
    if _current is not None:
        if _still_attached():
            return _current
        # handler 被外部摘掉了（如 :meth:`_RootSink.reset` 清空 root）——这时若直接
        # 返回，会话日志会**无声地**不记了：装好的东西不见了比没装更危险。清掉重装。
        logger.debug("会话 handler 已不在 root 上，重新装配")
        reset()

    session_dir = Path(log_dir) / SESSION_DIR_NAME
    session_dir.mkdir(parents=True, exist_ok=True)
    session_id = _new_session_id()
    session = SessionLog(
        session_id=session_id,
        directory=session_dir,
        text_path=session_dir / f"{session_id}{_TEXT_SUFFIX}",
        jsonl_path=session_dir / f"{session_id}{_JSONL_SUFFIX}",
    )

    root = logging.getLogger()
    level = _as_level(config.root_level)
    if root.level > level:
        root.setLevel(level)

    text = _make_rotating(
        session.text_path,
        config,
        # op=%(op_id)s 由 handler 上的 filter 注入；漏装时退化成 ``-``
        logging.Formatter(_TEXT_FORMAT, datefmt=config.date_format, defaults={"op_id": "-"}),
        level,
    )
    jsonl = _make_rotating(session.jsonl_path, config, _JsonlFormatter(), level)

    for handler in (text, jsonl):
        # 打码与编号一起装：会话文件是**用户回传的那个文件**，凭据不能从这里出去
        # （JSONL 那边走 record.getMessage()，所以打码必须发生在 filter 阶段）。
        root.addHandler(attach_redaction(attach_filter(handler)))
        _handlers.append(handler)

    _current = session
    _prune(session_dir, config.session_keep, current_id=session_id)
    logger.info(
        "会话日志已装配 session=%s 级别=%s 文件=%s（%s）",
        session_id,
        logging.getLevelName(level),
        session.text_path.name,
        session.jsonl_path.name,
    )
    return session


def current_session() -> SessionLog | None:
    """当前会话；还没装（或已 :func:`reset`）时返回 ``None``。"""
    return _current


def list_sessions(directory: Path) -> list[SessionLog]:
    """列出目录里已有的会话，按会话 id（即启动时间）从新到旧。

    会话 id 自带时间前缀，按名字倒序就是时间倒序。只认 ``<id>.*`` 里 id 符合
    命名规则的文件——日志目录里可能有用户自己放的东西，不能一起当成会话。
    纯文本 / JSONL 缺一个也照样列出（轮转或手工清理后可能只剩一种）。
    """
    directory = Path(directory)
    ids: set[str] = set()
    with contextlib.suppress(OSError):
        for path in directory.iterdir():
            if not path.is_file():
                continue
            session_id = path.name.partition(".")[0]
            if _ID_PATTERN.match(session_id):
                ids.add(session_id)
    return [
        SessionLog(
            session_id=session_id,
            directory=directory,
            text_path=directory / f"{session_id}{_TEXT_SUFFIX}",
            jsonl_path=directory / f"{session_id}{_JSONL_SUFFIX}",
        )
        for session_id in sorted(ids, reverse=True)
    ]


def reset() -> None:
    """测试 / 重装用：摘掉会话 handler 并关文件。

    必须 ``close()``：Windows 上没关的文件句柄让 ``shutil.rmtree`` 删不掉目录，
    测试里会表现为"临时目录清理失败"这种跟断言无关的怪错。
    """
    global _current
    root = logging.getLogger()
    for handler in _handlers:
        root.removeHandler(handler)
        handler.close()
    _handlers.clear()
    _current = None


# --------------------------------------------------------------------- 内部


def _as_level(level: int | str) -> int:
    """级别既可能是 ``10`` 也可能是 ``"DEBUG"``（``logging.getLevelName`` 双向都吃）。

    :class:`LogConfig` 把级别声明成 ``int``，但 ``handler.setLevel`` / ``root.setLevel``
    是**能收级别名**的，所以 ``LogConfig(root_level="DEBUG")`` 这种写法一路都不会报错，
    直到这里跟 ``root.level``（一定是 int）比较时炸掉——装配崩溃等于日志整个没了，
    所以在入口处统一收成 int。
    """
    if isinstance(level, str):
        numeric = logging.getLevelName(level.upper())
        # 认不出的名字 → DEBUG：会话日志本来就要全量，宁可多记也不要静默变少
        return numeric if isinstance(numeric, int) else logging.DEBUG
    return int(level)


def _new_session_id() -> str:
    """本次运行的会话 id：``20261017-091533-p12345``。

    时间在前，所以 ``session/`` 目录按文件名排序就是时间序；带进程号是因为
    同一个人可能同时开着两个实例，两份文件不能互相覆盖。
    """
    return f"{time.strftime(_ID_FORMAT)}-p{os.getpid()}"


def _still_attached() -> bool:
    """我们装的 handler 是否都还挂在 root 上。"""
    root_handlers = logging.getLogger().handlers
    return bool(_handlers) and all(h in root_handlers for h in _handlers)


class _JsonlFormatter(logging.Formatter):
    """一条记录 = 一行 JSON（多行 traceback 也压进同一个对象里）。

    字段名短但明确，方便 ``jq`` 直接读：
    ``{"ts":"…","level":"ERROR","user":"…","user_id":"…","host":"…","os":"…",
    "logger":"app…","func":"run","file":"service.py","line":122,"pid":1,
    "thread":2,"op":"op3","msg":"…","exc":{"type":"…","msg":"…","stack":"…"}}``

    ``user`` / ``user_id`` / ``host`` / ``os`` 用来区分多人回传的同名日志，也是结构化
    排查时"这条是谁在什么机器上触发的"的依据。它们取自进程级上下文
    （:mod:`app.infra.logging.user_context`），登录前那几行为空，这是正常的。
    """

    def format(self, record: logging.LogRecord) -> str:
        user_id, user_name = current_log_user()
        payload: dict[str, Any] = {
            "ts": _timestamp(record.created),
            "level": record.levelname,
            # 登录失败 / 尚未登录时为空串；展示层统一显示为 ``-``
            "user": user_name,
            "user_id": user_id,
            "host": log_host(),
            "os": log_os(),
            "logger": record.name,
            "func": record.funcName,
            "file": os.path.basename(record.pathname),
            "line": record.lineno,
            "pid": record.process,
            "thread": record.thread,
            # 由 handler 上的 filter 注入；万一漏装（旧 handler）也别让整条记录写不出去
            "op": getattr(record, "op_id", "-"),
            "msg": record.getMessage(),
        }
        if record.exc_info:
            payload["exc"] = _exception(record.exc_info)
        # 紧凑分隔符 + ensure_ascii=False：中文在文件里就是中文，直接 readable，
        # 而 ``json.dumps`` 会把换行转义成 ``\n``，多行 traceback 也不会撑破"一行一条"。
        return json.dumps(payload, ensure_ascii=False, separators=(",", ":"))


def _timestamp(created: float) -> str:
    """``2026-10-17T09:15:33.123+08:00``——带时区，跨机对比时不至于全靠猜。"""
    return datetime.fromtimestamp(created).astimezone().isoformat(timespec="milliseconds")


def _exception(exc_info: tuple) -> dict[str, str]:
    """异常类型 / 消息 / 完整堆栈；取不到的项退化成 ``?``。"""
    kind, value, tb = exc_info
    type_name = getattr(kind, "__name__", None) or "?"
    try:
        message = str(value)
        stack = "".join(traceback.format_exception(kind, value, tb))
    except Exception:  # noqa: BLE001 —— 异常自己的 __str__ 也可能炸
        return {"type": type_name, "msg": "?", "stack": ""}
    return {"type": type_name, "msg": message, "stack": stack}


def _make_rotating(
    path: Path, config: LogConfig, formatter: logging.Formatter, level: int
) -> RotatingFileHandler:
    """会话 handler。

    :param level: 已归一成 int 的级别（会话日志要的就是全量）。

    - ``errors="backslashreplace"``：QGIS/GDAL 那边偶尔会递来含孤立代理对的字符串，
      严格 utf-8 编码会抛 ``UnicodeEncodeError``；一条畸形消息不该让**整个会话文件**
      从那一刻起停止记录（``handleError`` 只是打一行 stderr，用户看不到）。
    - 按大小轮转也安全：轮转发生在整条记录写完之后，JSONL 切开仍然是合法的 JSONL。
    """
    handler = RotatingFileHandler(
        path,
        maxBytes=config.session_max_bytes,
        backupCount=config.session_backup_count,
        encoding=config.file_encoding,
        errors="backslashreplace",
    )
    handler.setLevel(level)
    handler.setFormatter(formatter)
    return handler


def _prune(session_dir: Path, keep: int, current_id: str) -> None:
    """只留最近 ``keep`` 次会话，删掉更早的（每次启动清一次）。

    绝不碰当前会话，也**只认自己写出来的名字**（``<id>.log`` / ``<id>.log.1`` /
    ``<id>.jsonl`` 这类带会话 id 前缀的）——日志目录里可能有用户放进去的东西，
    清旧日志顺手删掉别的文件是不可接受的。
    """
    if keep <= 0:
        return
    newest: dict[str, int] = {}
    for path in session_dir.iterdir():
        if not path.is_file():
            continue
        session_id, sep, _rest = path.name.partition(".")  # 轮转备份的 id 前缀不变
        if not sep or session_id == current_id or not _ID_PATTERN.match(session_id):
            continue
        with contextlib.suppress(OSError):
            newest[session_id] = max(newest.get(session_id, 0), path.stat().st_mtime_ns)

    doomed = sorted(newest, key=lambda sid: newest[sid], reverse=True)[keep:]
    for session_id in doomed:
        for old in session_dir.glob(f"{session_id}.*"):
            with contextlib.suppress(OSError):
                old.unlink()


__all__ = [
    "SESSION_DIR_NAME",
    "SessionLog",
    "current_session",
    "install",
    "list_sessions",
    "reset",
]
