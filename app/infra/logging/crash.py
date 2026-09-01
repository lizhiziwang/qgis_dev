"""崩溃日志：进程级未捕获异常 + 原生崩溃转储。

覆盖两类"程序崩溃"：

1. **未捕获 Python 异常**（含 Qt 槽 / C++ 回调里抛出的异常）——Qt 绑定（qgis.PyQt）会先调
   ``sys.excepthook`` 再决定是否终止进程，所以这里能拿到完整 traceback。
   写入 ``crash.log``（纯文本，便于事后 grep）。
2. **原生崩溃**（access violation / ``abort()`` / stack overflow）——
   用 :mod:`faulthandler` 在崩溃点 dump 全部线程栈，写入 ``crash_faulthandler.log``。
   通过文件描述符写入，绕过 Python 缓冲，硬崩溃也不丢数据。

另外把 ``sys.unraisablehook`` 也接到 ``crash.log``（析构等里抛出的不可上报异常）。

用法：启动早期（知道日志目录后）调一次 :func:`install`：

.. code-block:: python

    from app.infra.logging.crash import install as install_crash_logging
    install_crash_logging(paths.logs)

幂等：重复调用只装配一次。测试可用 :func:`reset` 还原。
"""

from __future__ import annotations

import faulthandler
import logging
import os
import sys
import time
import traceback
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

_installed: bool = False
_original_excepthook: Optional[object] = None
_original_unraisablehook: Optional[object] = None
_faulthandler_fd: Optional[int] = None
#: 本次安装是否真的调用了 faulthandler.enable（已被 pytest/PyInstaller 启用时不重复）
_fh_enabled_by_us: bool = False


def _default_log_dir() -> Path:
    """崩溃日志目录：优先环境变量，其次项目根 ``logs/``。"""
    env = os.environ.get("QGIS_DEV_LOG_DIR")
    if env:
        return Path(env)
    return Path(__file__).resolve().parents[3] / "logs"


def install(log_dir: Optional[Path] = None) -> None:
    """装配崩溃日志（幂等）。``log_dir`` 缺省用 :func:`_default_log_dir`。"""
    global _installed, _original_excepthook, _original_unraisablehook, _faulthandler_fd
    if _installed:
        return

    d = Path(log_dir) if log_dir is not None else _default_log_dir()
    d.mkdir(parents=True, exist_ok=True)
    crash_file = d / "crash.log"
    fh_file = d / "crash_faulthandler.log"

    # ---- 1) 未捕获 Python 异常 ----
    _original_excepthook = getattr(sys, "excepthook", None)
    crash_path = str(crash_file)

    def _excepthook(tp, val, tb) -> None:
        try:
            with open(crash_path, "a", encoding="utf-8") as f:
                f.write(f"\n{'=' * 70}\n")
                f.write(f"时间: {time.strftime('%Y-%m-%d %H:%M:%S')}  pid={os.getpid()}\n")
                f.write(f"未捕获异常: {tp.__name__}: {val}\n")
                f.write("".join(traceback.format_exception(tp, val, tb)))
                f.write("\n")
        except Exception:  # noqa: BLE001 —— 崩溃日志自身不能把事态搞得更糟
            pass
        # 保留默认行为（打印到 stderr；frozen 下 stderr 可能为 None，需保护）
        try:
            if _original_excepthook is not None:
                _original_excepthook(tp, val, tb)  # type: ignore[misc]
            else:
                sys.__excepthook__(tp, val, tb)
        except Exception:  # noqa: BLE001
            pass

    sys.excepthook = _excepthook  # type: ignore[assignment]

    # ---- 2) 不可上报异常（__del__ 等）----
    _original_unraisablehook = getattr(sys, "unraisablehook", None)

    def _unraisable(args) -> None:
        try:
            with open(crash_path, "a", encoding="utf-8") as f:
                f.write(f"\n--- unraisable: {args.exc_type.__name__}: {args.exc_value} ---\n")
                tb = getattr(args.exc_value, "__traceback__", None)
                if tb is not None:
                    f.write("".join(traceback.format_exception(args.exc_type, args.exc_value, tb)))
                else:
                    f.write(f"{args.exc_value!r}\n")
                f.write(f"位置: {args.object!r}\n\n")
        except Exception:  # noqa: BLE001
            pass
        if _original_unraisablehook is not None:
            try:
                _original_unraisablehook(args)  # type: ignore[misc]
            except Exception:  # noqa: BLE001
                pass

    sys.unraisablehook = _unraisable  # type: ignore[assignment]

    # ---- 3) 原生崩溃：faulthandler 走 fd，绕过缓冲 ----
    global _fh_enabled_by_us
    try:
        fd = os.open(str(fh_file), os.O_WRONLY | os.O_CREAT | os.O_APPEND)
        os.write(fd, f"\n{'=' * 70}\n{time.strftime('%Y-%m-%d %H:%M:%S')} "
                     f"faulthandler enabled pid={os.getpid()}\n".encode("utf-8"))
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
    if not _installed:
        return
    # 只有本次安装真正启用了 faulthandler 才 disable，避免关掉 pytest 等
    # 其它库已启用的 handler。
    if _fh_enabled_by_us:
        try:
            faulthandler.disable()
        except Exception:  # noqa: BLE001
            pass
        # disable 后 faulthandler 不再持有 fd，可以安全关闭
        if _faulthandler_fd is not None:
            try:
                os.close(_faulthandler_fd)
            except Exception:  # noqa: BLE001
                pass
            _faulthandler_fd = None
    _fh_enabled_by_us = False
    if _original_excepthook is not None:
        sys.excepthook = _original_excepthook  # type: ignore[assignment]
    if _original_unraisablehook is not None:
        sys.unraisablehook = _original_unraisablehook  # type: ignore[assignment]
    _installed = False


__all__ = ["install", "reset"]
