"""把 QGIS / Qt 自身的日志接进 Python logging。

**为什么需要**：矢量操作失败的原因——OGR/GDAL 报错、坐标系不匹配、图层只读、
几何非法——大多只出现在 QGIS 的消息日志里，Python 侧往往只看到"操作没成功"。
不接这两条线，用户传上来的日志就缺最关键的一块。

两个来源：

1. ``QgsApplication.messageLog()`` 的消息信号 —— QGIS 消息日志（provider、图层加载、
   processing 算法、GDAL/OGR 的报错都在这里）。**QGIS 4.0 起有两个信号**：带格式的
   ``messageReceivedWithFormat(message, tag, Qgis::MessageLevel, Qgis::StringFormat)``
   和 3 参数的老 ``messageReceived(message, tag, Qgis::MessageLevel)``。实测 4.2 里
   ``QgsMessageLog.logMessage`` **只发带格式的那个**——老信号依然存在、连上却永远收不到
   消息（这正是"桥装好了却一条 QGIS 日志都没有"的原因）。所以两个都接：槽用 ``*args``
   兼容两种签名，真有版本同时发两个信号也由折叠限流器收成一条。Html 格式的消息
   （QGIS 侧 GUI 排版用的）去掉标签再记，日志要的是文本。
2. ``qInstallMessageHandler`` —— Qt 自己的 qDebug/qWarning/qCritical。注意它会
   **替换** PyQt 的默认处理器，所以槽里必须保证绝不抛异常；顺带一个好处：
   PyQt 遇到槽内未捕获异常时最终会走 ``qFatal``，那条信息也会被这里收进日志。

**本模块必须保持"不参与 Cython 原生编译"**：``tools/protect_cython.py`` 的
``qt_free_only`` 按"文件里有没有 import qgis/PyQt"判断（函数体内的 import 也算），
所以这里的 qgis/PyQt import 全部写在函数体内——既是必须的延迟导入（logging 装配
时 QGIS 运行时环境还没就绪，顶层 import 会失败），也恰好让保护工具跳过本文件。
**不要**把 ``from qgis...`` 提到模块顶层，也不要让本文件被编译成 .pyd：Cython
编译后的函数与 PyQt 信号槽不兼容（``.connect(编译函数)`` 会 TypeError 甚至硬崩溃）。

**刷屏保护**：canvas 重绘 / 属性表模型抖动期间，同一条 QGIS 消息每秒能刷几十条
（真实见过一次抖动把 crash.log 写到 10 MB）。这里按"同一来源 + 级别 + 消息前缀"
在一个时间窗口内只放行第一条，其余折叠计数，窗口滚动时补一条汇总——既保住
"发生过什么"，又不让体积和 IO 失控。

用法（**必须在 QgsApplication 创建之后**，见 :func:`app.bootstrap.main`）：

.. code-block:: python

    from app.infra.logging import install_qgis_log_bridge
    install_qgis_log_bridge()

幂等；失败只记一条 warning，不阻断启动。测试可用 :func:`reset` 还原。
"""

from __future__ import annotations

import contextlib
import html
import logging
import re
import threading
import time
from collections.abc import Callable
from typing import Any

logger = logging.getLogger(__name__)

#: 转发到 Python logging 的两个子 logger（模块级取一次，避免每条消息都查 registry）
_logger_qgis = logging.getLogger("Application.QGIS")
_logger_qt = logging.getLogger("Application.Qt")

#: 依次尝试连接的 QGIS 消息日志信号（QGIS 4.2 实测：只有第一个会收到消息，
#: 详见模块 docstring——两个都接是为了同时兼容 3.x / 4.x）。
_QGIS_LOG_SIGNALS: tuple[str, ...] = ("messageReceivedWithFormat", "messageReceived")

#: 折叠窗口（秒）与每窗口最多放行的条数
_WINDOW_SECONDS = 1.0
_MAX_PER_WINDOW = 200
#: 判重只看消息前这么多个字符（尾部常带坐标 / 计数等每次都变的部分）
_KEY_PREFIX = 120

#: ``Qgis.StringFormat.Html`` 的整数值（4.2 实测：PlainText=0 / Html=1，只有这两个）
_FORMAT_HTML = 1

#: 剥 HTML 标签用；只在消息标记为 Html 时生效
_HTML_TAG_RE = re.compile(r"<[^>]+>")

#: QGIS ``Qgis.MessageLevel`` 的整数值 → Python 级别（枚举身份比较失效时的回退）
_QGIS_INT_LEVELS: dict[int, int] = {
    0: logging.DEBUG,     # Info
    1: logging.WARNING,   # Warning
    2: logging.ERROR,     # Critical
    3: logging.INFO,      # Success
    4: logging.DEBUG,     # NoLevel
}

#: 枚举成员名 → Python 级别，按这个顺序查
_QGIS_LEVEL_BY_NAME: tuple[tuple[str, int], ...] = (
    ("Critical", logging.ERROR),
    ("Warning", logging.WARNING),
    ("Success", logging.INFO),
    ("Info", logging.DEBUG),
    ("NoLevel", logging.DEBUG),
)

#: Qt ``QtMsgType`` 的整数值 → Python 级别（QtDebugMsg=0 … QtInfoMsg=4）
_QT_INT_LEVELS: dict[int, int] = {
    0: logging.DEBUG,     # QtDebugMsg
    1: logging.WARNING,   # QtWarningMsg
    2: logging.ERROR,     # QtCriticalMsg
    3: logging.CRITICAL,  # QtFatalMsg
    4: logging.INFO,      # QtInfoMsg
}

_installed: bool = False
_previous_qt_handler: Callable | None = None
_limiter: _MessageLimiter | None = None
#: 本次装配真正连上的信号名（``reset`` 据此原样断开，不去猜连了哪些）
_connected_signals: list[str] = []


class _ReentrancyGuard(threading.local):
    """防递归：handler 内部若再触发 Qt / QGIS 消息，直接丢弃。

    例如转发的消息进了控制台 handler，而控制台写入本身又产生 Qt 警告——
    没有这道闸就会无限递归。
    """

    active: bool = False


_reentrant = _ReentrancyGuard()


class _MessageLimiter:
    """按时间窗口折叠重复 / 高频消息。

    语义：同一 ``key`` 在一个窗口内只放行第一条（后续计入折叠数）；窗口内放行
    总数超过 ``max_per_window`` 后全部丢弃。窗口滚动时，如果上一窗口折叠过消息，
    补一条 debug 汇总，保证"被折叠了多少"有据可查。
    """

    def __init__(
        self,
        window: float = _WINDOW_SECONDS,
        max_per_window: int = _MAX_PER_WINDOW,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._window = window
        self._max = max_per_window
        self._clock = clock
        self._start = 0.0
        self._allowed = 0
        self._seen: dict[tuple, int] = {}
        self._folded = 0

    def offer(self, key: tuple) -> tuple[bool, int]:
        """返回 ``(是否放行, 本窗口内该 key 的累计条数)``。"""
        now = self._clock()
        if now - self._start >= self._window:
            self._roll(now)
        count = self._seen.get(key, 0) + 1
        self._seen[key] = count
        if count > 1:
            self._folded += 1
            return False, count
        if self._allowed >= self._max:
            self._folded += 1
            return False, count
        self._allowed += 1
        return True, count

    def _roll(self, now: float) -> None:
        folded = self._folded
        self._start = now
        self._allowed = 0
        self._seen = {}
        self._folded = 0
        if folded:
            logger.debug("QGIS/Qt 日志刷屏：上一窗口折叠 %d 条重复消息", folded)


# --------------------------------------------------------------------- 级别映射


def _qgis_level_to_logging(level: Any, message_level: Any = None) -> int:
    """``Qgis.MessageLevel`` → Python logging 级别。

    :param message_level: ``Qgis.MessageLevel`` 枚举类；不传则从 ``qgis.core`` 取。
        允许显式传入，便于脱离 QGIS 单测。
    """
    if message_level is None:
        message_level = _load_message_level_enum()
    # 优先按枚举成员**身份**定位：QGIS 若调整枚举取值也不会错配。
    # 用 ``is`` 而不是 ``==``：PyQt6 的枚举不是 IntEnum，与其他枚举成员比 ``==``
    # 会退化成"按值比较"（实测 ``IntEnum(3) == Qgis.MessageLevel.Success`` 为真），
    # 拿别处的枚举值来比就可能张冠李戴。PyQt5 下级别是纯 int，身份比较不成立，
    # 自然落到下面的整数值回退。
    if message_level is not None:
        for name, py_level in _QGIS_LEVEL_BY_NAME:
            member = getattr(message_level, name, None)
            if member is not None and level is member:
                return py_level
    key = _as_int(level)
    return _QGIS_INT_LEVELS.get(key, logging.DEBUG) if key is not None else logging.DEBUG


def _qt_level_to_logging(mode: Any) -> int:
    """Qt ``QtMsgType`` → Python logging 级别。"""
    key = _as_int(mode)
    if key is None:
        return logging.DEBUG
    return _QT_INT_LEVELS.get(key, logging.DEBUG)


def _as_int(value: Any) -> int | None:
    """把 Qt / QGIS 的级别值统一成 int。

    PyQt6 的枚举成员不是 int，要取 ``.value``；PyQt5 直接就是 int。
    """
    if isinstance(value, bool):  # bool 是 int 子类，别把 True 当级别 1
        return None
    if isinstance(value, int):
        return int(value)
    raw = getattr(value, "value", None)
    return int(raw) if isinstance(raw, int) else None


def _load_message_level_enum() -> Any:
    """延迟取 ``Qgis.MessageLevel``（拿不到返回 None，调用方走整数回退）。"""
    try:
        from qgis.core import Qgis
    except Exception:  # noqa: BLE001 —— 没装 QGIS 也要能 import 本模块
        return None
    return getattr(Qgis, "MessageLevel", None)


# --------------------------------------------------------------------- 槽函数


def _qgis_args(args: tuple) -> tuple[str, str, Any, Any]:
    """从信号参数取 ``(message, tag, level, format)``，容忍两个版本的签名差异。

    3 参数的老信号没有 ``format``，取到 ``None``（按纯文本处理）。
    """
    message = str(args[0]) if args else ""
    tag = str(args[1]) if len(args) > 1 and args[1] else "QGIS"
    level = args[2] if len(args) > 2 else 0
    fmt = args[3] if len(args) > 3 else None
    return message, tag, level, fmt


def _plain_text(message: str, fmt: Any) -> str:
    """Html 格式的消息剥掉标签与实体：诊断日志里要的是文本。

    只有 QGIS 4.0 起带格式的信号才会给出 ``fmt``；GUI 侧（消息日志面板、
    processing 结果）会用 HTML 排版，``<a href=...>`` 之类对排查毫无意义。
    """
    if _as_int(fmt) != _FORMAT_HTML:
        return message
    return html.unescape(_HTML_TAG_RE.sub("", message)).strip()


def _qt_location(context: Any) -> str:
    """Qt 消息的来源位置 ``文件:行号 函数()``；取不到就空串。"""
    try:
        file = context.file or ""
        func = context.function or ""
        line = context.line or 0
    except Exception:  # noqa: BLE001
        return ""
    if not file and not func:
        return ""
    return f"{file}:{line} {func}()"


def _allow(key: tuple) -> bool:
    """过一遍折叠窗口；限流器没装配时一律放行。"""
    if _limiter is None:
        return True
    allowed, _count = _limiter.offer(key)
    return allowed


def _on_qgis_message(*args: Any) -> None:
    """QGIS 消息日志信号槽（``messageReceivedWithFormat`` / ``messageReceived``）。"""
    if _reentrant.active:
        return
    _reentrant.active = True
    try:
        message, tag, level, fmt = _qgis_args(args)
        message = _plain_text(message, fmt)
        if not _allow(("qgis", tag, _as_int(level), message[:_KEY_PREFIX])):
            return
        _logger_qgis.log(_qgis_level_to_logging(level), "[QGIS:%s] %s", tag, message)
    except Exception:  # noqa: BLE001 —— 日志桥绝不能把主流程带崩
        pass
    finally:
        _reentrant.active = False


def _on_qt_message(mode: Any, context: Any, message: Any) -> None:
    """``qInstallMessageHandler`` 槽：``(QtMsgType, QMessageLogContext, str)``。"""
    if _reentrant.active:
        return
    _reentrant.active = True
    try:
        where = _qt_location(context)
        text = str(message)
        if not _allow(("qt", _as_int(mode), where, text[:_KEY_PREFIX])):
            return
        _logger_qt.log(_qt_level_to_logging(mode), "[Qt] %s %s", where, text)
    except Exception:  # noqa: BLE001
        pass
    finally:
        _reentrant.active = False


# --------------------------------------------------------------------- 装配


def install() -> bool:
    """装配 QGIS / Qt 日志桥（幂等），返回是否装配成功。

    必须在 QGIS 运行时环境就绪（``QgisRuntime.apply_env``）且 ``QgsApplication``
    已创建之后调用；任何一步失败只记 warning 并返回 ``False``，不抛异常——
    日志桥装不上不该拦住程序启动。
    """
    global _installed, _previous_qt_handler, _limiter, _connected_signals
    if _installed:
        return True
    try:
        from qgis.core import Qgis, QgsApplication
        from qgis.PyQt.QtCore import qInstallMessageHandler
    except Exception as exc:  # noqa: BLE001
        logger.warning("QGIS/Qt 日志桥装配失败（导入 qgis 失败）：%s", exc)
        return False

    _limiter = _MessageLimiter()

    # 两个信号都接（见模块 docstring）：4.2 只有带格式的那个真会发消息。哪条通路
    # 是活的写进日志——用户传上来的日志里能直接看出"QGIS 侧报错到底有没有被收到"。
    _connected_signals = []
    message_log = QgsApplication.messageLog()
    for name in _QGIS_LOG_SIGNALS:
        signal = getattr(message_log, name, None)
        if signal is None:
            continue
        try:
            signal.connect(_on_qgis_message)
        except Exception as exc:  # noqa: BLE001
            logger.warning("接入 QGIS 消息日志失败（%s）：%s", name, exc)
            continue
        _connected_signals.append(name)
    if _connected_signals:
        logger.info("已接入 QGIS 消息日志：%s", "、".join(_connected_signals))
    else:
        logger.warning("没有可用的 QGIS 消息日志信号，QGIS 侧报错不会进日志文件")

    try:
        # 返回值是上一个处理器（PyQt 默认的 C++ 处理器时为 None），reset 时还原
        _previous_qt_handler = qInstallMessageHandler(_on_qt_message)
        logger.debug("已接入 Qt 消息处理器")
    except Exception as exc:  # noqa: BLE001
        logger.warning("接入 Qt 消息处理器失败：%s", exc)

    _installed = True
    logger.info("QGIS/Qt 日志桥已装配（QGIS %s）", getattr(Qgis, "QGIS_VERSION", "?"))
    return True


def reset() -> None:
    """测试用：断开信号、还原上一个 Qt 消息处理器。"""
    global _installed, _previous_qt_handler, _limiter
    if not _installed:
        return
    try:
        from qgis.PyQt.QtCore import qInstallMessageHandler

        qInstallMessageHandler(_previous_qt_handler)  # None ＝ 还原 Qt 默认处理器
    except Exception:  # noqa: BLE001
        pass
    try:
        from qgis.core import QgsApplication

        message_log = QgsApplication.messageLog()
        for name in _connected_signals:
            signal = getattr(message_log, name, None)
            if signal is not None:
                with contextlib.suppress(Exception):
                    signal.disconnect(_on_qgis_message)
    except Exception:  # noqa: BLE001
        pass
    _connected_signals.clear()
    _previous_qt_handler = None
    _limiter = None
    _installed = False


__all__ = ["install", "reset"]
