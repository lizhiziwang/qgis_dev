"""崩溃日志：进程级未捕获异常 + 原生崩溃转储。

覆盖两类"程序崩溃"：

1. **未捕获 Python 异常**（含 Qt 槽 / C++ 回调里抛出的异常）——Qt 绑定（qgis.PyQt）会先调
   ``sys.excepthook`` 再决定是否终止进程，所以这里能拿到完整 traceback。
   写入 ``crash.log``（滚动，见下）。
2. **原生崩溃**（access violation / ``abort()`` / stack overflow）——
   用 :mod:`faulthandler` 在崩溃点 dump 全部线程栈，写入 ``crash_faulthandler.log``。
   通过文件描述符写入，绕过 Python 缓冲，硬崩溃也不丢数据。

另外把 ``sys.unraisablehook`` 也接到 ``crash.log``（析构等里抛出的不可上报异常）。

**为什么要滚动 + 折叠重复**：真实见过一次事件循环抖动——属性表模型在图层销毁后
每帧抛同一个 ``RuntimeError: wrapped C/C++ object ... has been deleted``——把
``crash.log`` 撑到 10 MB / 21000 条，而里面没有一条能看出"用户当时在干什么"。
所以这里：

- ``crash.log`` 走 :class:`~logging.handlers.RotatingFileHandler`，上限
  :attr:`~app.infra.logging.LogConfig.crash_max_bytes`；
- **同一异常连续重复时折叠计数**：首次写完整堆栈，之后每
  :data:`_REPEAT_EMIT_EVERY` 次补一条提示；异常换成另一种时，把"上一个重复了
  多少次"补记一条，重复量不丢、体积可控；
- 记录走 :data:`_CRASH_LOGGER_NAME` 子 logger（``propagate=True``），所以同一份
  堆栈也进 ``app.log``——崩溃前后的业务日志与堆栈落在同一条时间线上，便于还原现场。

用法：启动早期（知道日志目录后）调一次 :func:`install`：

.. code-block:: python

    from app.infra.logging.crash import install as install_crash_logging
    install_crash_logging(paths.logs)

幂等：重复调用只装配一次。测试可用 :func:`reset` 还原。
"""

from __future__ import annotations

import contextlib
import faulthandler
import logging
import os
import sys
import threading
import time
import traceback
from logging.handlers import RotatingFileHandler
from pathlib import Path

from .config import LogConfig
from .operation import attach_filter

logger = logging.getLogger(__name__)

#: 崩溃日志专用子 logger。挂在 ``Application`` 下，``propagate=True`` 让它同时
#: 进 app.log（全量日志）——崩溃堆栈与它前后的业务日志在同一条时间线上。
_CRASH_LOGGER_NAME = "Application.Crash"

#: ``op=``：崩溃多半发生在某个矢量操作里（见 operation 模块）。excepthook 跑的时候
#: 操作上下文通常已经退出，所以这里多半是 ``-``；真正带编号的是操作自己那条
#: ``OP … 失败 …`` 记录。留着这个字段是为了崩溃恰好落在操作内时能直接对上。
_CRASH_FORMAT = "%(asctime)s - CRASH - pid=%(process)d - op=%(op_id)s - %(message)s"
_CRASH_DATE_FORMAT = "%Y-%m-%d %H:%M:%S"

#: 同一异常连续重复时，每这么多次补写一条提示（其余折叠）
_REPEAT_EMIT_EVERY = 100

_installed: bool = False
_original_excepthook: object | None = None
_original_unraisablehook: object | None = None
_faulthandler_fd: int | None = None
#: 本次安装是否真的调用了 faulthandler.enable（已被 pytest/PyInstaller 启用时不重复）
_fh_enabled_by_us: bool = False

#: 去重状态（``_last_signature`` + ``_repeat_count``）的锁。用 RLock：写日志的
#: 过程中若又抛异常（会走回 excepthook），普通 Lock 会自锁死。
_repeat_lock = threading.RLock()
_last_signature: tuple | None = None
_repeat_count: int = 0


def _default_log_dir() -> Path:
    """崩溃日志目录：优先环境变量，其次项目根 ``logs/``。"""
    env = os.environ.get("QGIS_DEV_LOG_DIR")
    if env:
        return Path(env)
    return Path(__file__).resolve().parents[3] / "logs"


def install(
    log_dir: Path | None = None,
    *,
    max_bytes: int | None = None,
    backup_count: int | None = None,
) -> None:
    """装配崩溃日志（幂等）。

    :param log_dir: 缺省用 :func:`_default_log_dir`。
    :param max_bytes: ``crash.log`` 轮转上限，缺省用
        :attr:`~app.infra.logging.LogConfig.crash_max_bytes`。
    :param backup_count: ``crash.log`` 备份份数，缺省用
        :attr:`~app.infra.logging.LogConfig.crash_backup_count`。
    """
    global _installed, _original_excepthook, _original_unraisablehook
    global _faulthandler_fd, _fh_enabled_by_us
    if _installed:
        return

    cfg = LogConfig.from_env()
    limit = cfg.crash_max_bytes if max_bytes is None else max_bytes
    backups = cfg.crash_backup_count if backup_count is None else backup_count

    d = Path(log_dir) if log_dir is not None else _default_log_dir()
    d.mkdir(parents=True, exist_ok=True)
    crash_file = d / "crash.log"
    fh_file = d / "crash_faulthandler.log"

    crash_log = _configure_crash_logger(crash_file, limit, backups)
    # faulthandler 一旦 enable 就长期持有 fd、运行期轮转不了自己那个文件，
    # 所以进程序时先按大小收一次（把上一次运行留下的巨大 dump 挪走）。
    _rotate_oversize(fh_file, limit)

    # ---- 1) 未捕获 Python 异常 ----
    # 保留原 hook 只为了 :func:`reset` 能还原（含第三方如 IPython 装的 hook）；
    # 这里不再显式回链：记录经 ``propagate`` 到根 logger，控制台 handler / 无
    # 配置时的 ``logging.lastResort`` 都会把它写到 stderr，默认行为不丢，
    # 但不会因为"回链 + 控制台"把同一份堆栈打成两份。
    _original_excepthook = getattr(sys, "excepthook", None)

    def _excepthook(tp, val, tb) -> None:
        # 崩溃日志自身不能把事态搞得更糟：任何写入失败都吞掉
        with contextlib.suppress(Exception):
            _record_exception(crash_log, tp, val, tb)

    sys.excepthook = _excepthook  # type: ignore[assignment]

    # ---- 2) 不可上报异常（__del__ 等）----
    _original_unraisablehook = getattr(sys, "unraisablehook", None)

    def _unraisable(args) -> None:
        with contextlib.suppress(Exception):
            crash_log.error(_format_unraisable(args))

    sys.unraisablehook = _unraisable  # type: ignore[assignment]

    # ---- 3) 原生崩溃：faulthandler 走 fd，绕过缓冲 ----
    global _fh_enabled_by_us
    try:
        fd = os.open(str(fh_file), os.O_WRONLY | os.O_CREAT | os.O_APPEND)
        os.write(fd, f"\n{'=' * 70}\n{time.strftime('%Y-%m-%d %H:%M:%S')} "
                     f"faulthandler enabled pid={os.getpid()}\n".encode())
        try:
            faulthandler.enable(file=fd, all_threads=True)
            _faulthandler_fd = fd
            _fh_enabled_by_us = True
        except RuntimeError:
            # 已被其它库（pytest / PyInstaller）启用：不动它，关掉自己的 fd
            os.close(fd)
    except Exception:  # noqa: BLE001
        try:
            if not faulthandler.is_enabled():
                faulthandler.enable(all_threads=True)
                _fh_enabled_by_us = True
        except Exception:  # noqa: BLE001
            pass

    _installed = True
    logger.info("崩溃日志已装配 crash=%s faulthandler=%s", crash_file, fh_file)


def reset() -> None:
    """测试用：还原 hooks / 关闭 faulthandler。"""
    global _installed, _faulthandler_fd, _fh_enabled_by_us
    global _last_signature, _repeat_count
    if not _installed:
        return
    # 只有本次安装真正启用了 faulthandler 才 disable，避免关掉 pytest 等
    # 其它库已启用的 handler。
    if _fh_enabled_by_us:
        with contextlib.suppress(Exception):
            faulthandler.disable()
        # disable 后 faulthandler 不再持有 fd，可以安全关闭
        if _faulthandler_fd is not None:
            with contextlib.suppress(Exception):
                os.close(_faulthandler_fd)
            _faulthandler_fd = None
    _fh_enabled_by_us = False
    if _original_excepthook is not None:
        sys.excepthook = _original_excepthook  # type: ignore[assignment]
    if _original_unraisablehook is not None:
        sys.unraisablehook = _original_unraisablehook  # type: ignore[assignment]
    # 关掉崩溃日志自己的 handler：不清会一直占着文件句柄（Windows 上删不掉日志
    # 目录，测试的 tmp_path 收尾会报 PermissionError）
    crash_log = logging.getLogger(_CRASH_LOGGER_NAME)
    for handler in list(crash_log.handlers):
        crash_log.removeHandler(handler)
        handler.close()
    with _repeat_lock:
        _last_signature = None
        _repeat_count = 0
    _installed = False


# --------------------------------------------------------------------- 内部


def _configure_crash_logger(path: Path, max_bytes: int, backup_count: int) -> logging.Logger:
    """给崩溃日志专用 logger 挂上滚动文件 handler（重复安装时先摘掉旧的）。

    ``propagate=True``：同一条记录也冒泡到根 logger，落进 app.log / error.log。
    """
    log = logging.getLogger(_CRASH_LOGGER_NAME)
    log.setLevel(logging.DEBUG)
    log.propagate = True
    for handler in list(log.handlers):
        log.removeHandler(handler)
        handler.close()
    handler = RotatingFileHandler(
        path,
        maxBytes=max_bytes,
        backupCount=backup_count,
        encoding="utf-8",
    )
    handler.setFormatter(
        logging.Formatter(
            _CRASH_FORMAT, datefmt=_CRASH_DATE_FORMAT, defaults={"op_id": "-"}
        )
    )
    log.addHandler(attach_filter(handler))
    return log


def _rotate_oversize(path: Path, max_bytes: int) -> None:
    """文件超过 ``max_bytes`` 就挪成 ``<名>.1``（覆盖旧的备份）。

    仅用于 faulthandler 那个 dump 文件——它被 faulthandler 持有的 fd 写，运行期
    轮转不了，只能在装配前收一次。
    """
    try:
        if path.is_file() and path.stat().st_size >= max_bytes:
            backup = path.with_name(path.name + ".1")
            backup.unlink(missing_ok=True)
            path.rename(backup)
    except OSError:
        pass


def _exception_signature(tp, val, tb) -> tuple:
    """去重用的异常指纹：类型 + 文本 + **最内层帧**的文件名:行号。

    取最内层帧（而不是入口帧）是为了把"同一个异常类型、抛在不同代码位置"区分开——
    抖动型刷屏往往就是某一行被反复执行；不同行不同指纹，各自计数。
    只取文件名与行号两个字符串，不持有 frame 对象。
    """
    inner = tb
    while inner is not None and inner.tb_next is not None:
        inner = inner.tb_next
    where = (inner.tb_frame.f_code.co_filename, inner.tb_lineno) if inner is not None else ("", 0)
    return (getattr(tp, "__name__", str(tp)), str(val), where)


def _next_record(signature: tuple, tp, val, tb) -> list:
    """推进去重状态，返回本次该写的一条或多条日志文本（空列表＝折叠掉）。

    状态迁移：

    - 与上一条同指纹 → 计数 +1；只在每 :data:`_REPEAT_EMIT_EVERY` 次补一条提示；
    - 换了指纹 → 先把上一个的重复次数补记一条，再写本次完整堆栈。
    """
    global _last_signature, _repeat_count
    out: list = []
    if signature == _last_signature:
        _repeat_count += 1
        if _repeat_count % _REPEAT_EMIT_EVERY:
            return out
        out.append(f"（同一异常已重复 {_repeat_count} 次，中间记录已折叠）")
        return out
    if _last_signature is not None and _repeat_count > 1:
        out.append(f"（上一个异常累计重复 {_repeat_count} 次）")
    _last_signature = signature
    _repeat_count = 1
    out.append(_format_exception(tp, val, tb))
    return out


def _record_exception(crash_log: logging.Logger, tp, val, tb) -> None:
    """记一条未捕获异常（同一异常连续重复时折叠）。"""
    signature = _exception_signature(tp, val, tb)
    with _repeat_lock:
        texts = _next_record(signature, tp, val, tb)
    for text in texts:
        crash_log.error(text)


def _format_exception(tp, val, tb) -> str:
    """未捕获异常条目：分隔线 + 类型/消息/pid/线程 + 完整堆栈。"""
    header = (
        f"\n{'=' * 70}\n"
        f"未捕获异常: {getattr(tp, '__name__', tp)}: {val}"
        f"   pid={os.getpid()} 线程={threading.current_thread().name}\n"
    )
    return header + "".join(traceback.format_exception(tp, val, tb)).rstrip()


def _format_unraisable(args) -> str:
    """不可上报异常条目（``__del__`` / 弱引用回调里抛出的异常）。"""
    head = f"\n--- unraisable: {args.exc_type.__name__}: {args.exc_value} ---\n"
    tb = getattr(args.exc_value, "__traceback__", None)
    if tb is not None:
        body = "".join(traceback.format_exception(args.exc_type, args.exc_value, tb))
    else:
        body = f"{args.exc_value!r}\n"
    return f"{head}{body}位置: {args.object!r}"


__all__ = ["install", "reset"]
