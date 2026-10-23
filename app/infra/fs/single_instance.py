"""单实例互斥体（Qt-free）。

自动更新要替换 ``app.exe`` / ``qgis_dev``，必须先确保旧进程已退出。Windows
安装器通过 Inno Setup 的 ``AppMutex`` 等这个互斥体；应用启动时创建同名互斥体，
第二个实例拿不到就提示后退出。

- Windows：命名互斥体（与 Inno Setup ``AppMutex`` 同名即可互认）；
- Linux：``flock`` 每用户锁文件（``/tmp`` 下按 uid 区分，不影响其他用户）。

进程退出（含崩溃）时内核自动释放，不需要清理文件。
"""

from __future__ import annotations

import contextlib
import os
import sys
import tempfile
from pathlib import Path
from typing import Any, TextIO

_ERROR_ALREADY_EXISTS = 183


class SingleInstance:
    """一次 ``acquire()`` 对应一个进程生命周期。

    调用方必须让实例活到进程结束（Windows 上句柄随对象关闭；Linux 上 fd 随
    对象关闭），否则互斥体会提前释放。
    """

    def __init__(self, name: str) -> None:
        self.name = name
        self.acquired = False
        self._handle: Any = None
        self._file: TextIO | None = None

    def acquire(self) -> bool:
        if self.acquired:
            return True
        if sys.platform == "win32":
            self.acquired = self._acquire_windows()
        else:
            self.acquired = self._acquire_posix()
        return self.acquired

    def release(self) -> None:
        if sys.platform == "win32" and self._handle is not None:
            with contextlib.suppress(Exception):
                _kernel32().CloseHandle(self._handle)
            self._handle = None
        if self._file is not None:
            with contextlib.suppress(Exception):
                import fcntl

                fcntl.flock(self._file.fileno(), fcntl.LOCK_UN)
            with contextlib.suppress(OSError):
                self._file.close()
            self._file = None
        self.acquired = False

    # ------------------------------------------------------------------ 平台

    def _acquire_windows(self) -> bool:
        import ctypes
        from ctypes import wintypes

        kernel32 = _kernel32()
        kernel32.CreateMutexW.argtypes = [wintypes.LPVOID, wintypes.BOOL, wintypes.LPCWSTR]
        kernel32.CreateMutexW.restype = wintypes.HANDLE
        ctypes.set_last_error(0)
        handle = kernel32.CreateMutexW(None, False, self.name)
        if not handle:
            raise OSError(ctypes.get_last_error(), "CreateMutexW 失败")
        if ctypes.get_last_error() == _ERROR_ALREADY_EXISTS:
            kernel32.CloseHandle(handle)
            return False
        self._handle = handle
        return True

    def _acquire_posix(self) -> bool:
        import fcntl

        uid = os.getuid() if hasattr(os, "getuid") else 0
        path = Path(tempfile.gettempdir()) / f"{self.name}-{uid}.lock"
        try:
            fh = open(path, "a+", encoding="utf-8")  # noqa: SIM115 —— 生命周期由本对象持有
        except OSError:
            return False
        try:
            fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            fh.close()
            return False
        try:
            fh.seek(0)
            fh.truncate()
            fh.write(str(os.getpid()))
            fh.flush()
        except OSError:
            pass
        self._file = fh
        return True


def notify_already_running(name: str) -> None:
    """第二个实例的提示：Windows 弹系统消息框，其他平台写 stderr。

    这里刻意不导入 Qt：调用点早于 ``QApplication``，用 Qt 会崩。
    """
    message = "图斑核实平台已经在运行。"
    if sys.platform == "win32":
        try:
            import ctypes

            ctypes.windll.user32.MessageBoxW(0, message, "提示", 0x40)
            return
        except Exception:  # noqa: BLE001
            pass
    print(f"{message}（mutex={name}）", file=sys.stderr)


_kernel32_cache: Any = None


def _kernel32() -> Any:
    global _kernel32_cache
    if _kernel32_cache is None:
        import ctypes

        _kernel32_cache = ctypes.WinDLL("kernel32", use_last_error=True)
    return _kernel32_cache
