"""Windows DPAPI 加解密小封装（``CryptProtectData`` / ``CryptUnprotectData``）。

只做一件事：把一段字节用**当前 Windows 登录用户**的密钥加密 / 解密。用途见
:mod:`app.core.auth.saved_logins`（记住的登录密码）。

要点：

- **只在 Windows 可用**；其它平台 :func:`available` 返回 ``False``，调用方自行
  决定降级策略（本模块不抛异常，失败一律返回 ``None``）。
- 全程 ``ctypes`` 调系统库，**无第三方依赖**，也不需要任何本地密钥文件——
  密钥由系统按当前用户托管，换 Windows 用户 / 换机器都解不开。
- 用 ``CRYPTPROTECT_UI_FORBIDDEN``，任何需要用户交互的情况直接失败而不是弹窗
  （启动阶段不该被系统对话框打断）。
- ``entropy`` 是附加熵：这里编译进程序，**不构成额外安全性**，只用来把 blob
  限定在本项目（别的程序即使拿到密文也不会用同一 entropy 去解）。
"""

from __future__ import annotations

import ctypes
import sys
from typing import Any

#: 附加熵（本项目专用；换掉它会让已保存的密文失效）
ENTROPY: bytes = b"qgis_dev::saved_logins::v1"

#: 不弹系统 UI（无交互）
_CRYPTPROTECT_UI_FORBIDDEN = 0x01

_IS_WINDOWS = sys.platform == "win32"

#: 懒加载的 (crypt32, kernel32)；``None`` 表示不可用
_LIBS: tuple[Any, Any] | None = None
_LIBS_FAILED = False


class _Blob(ctypes.Structure):
    """``DATA_BLOB``。"""

    _fields_ = [("cbData", ctypes.c_uint32), ("pbData", ctypes.c_void_p)]


def _blob(data: bytes) -> tuple[_Blob, Any]:
    """构造 ``DATA_BLOB``；第 2 个返回值是必须保活的缓冲区。"""
    if not data:
        return _Blob(0, None), None
    buf = ctypes.create_string_buffer(data, len(data))
    return _Blob(len(data), ctypes.cast(buf, ctypes.c_void_p)), buf


def _libs() -> tuple[Any, Any] | None:
    """加载 crypt32 / kernel32 并声明签名；失败返回 ``None``（只尝试一次）。"""
    global _LIBS, _LIBS_FAILED
    if _LIBS is not None or _LIBS_FAILED:
        return _LIBS
    win_dll = getattr(ctypes, "WinDLL", None)  # 非 Windows 平台没有 WinDLL
    if win_dll is None:
        _LIBS_FAILED = True
        return None
    try:
        crypt32 = win_dll("crypt32", use_last_error=True)
        kernel32 = win_dll("kernel32", use_last_error=True)
        # 不声明签名的话 ctypes 会把指针当 int 传，64 位下直接 OverflowError
        crypt32.CryptProtectData.argtypes = [
            ctypes.POINTER(_Blob),  # pDataIn
            ctypes.c_wchar_p,  # szDataDescr
            ctypes.POINTER(_Blob),  # pOptionalEntropy
            ctypes.c_void_p,  # pvReserved
            ctypes.c_void_p,  # pPromptStruct
            ctypes.c_uint32,  # dwFlags
            ctypes.POINTER(_Blob),  # pDataOut
        ]
        crypt32.CryptProtectData.restype = ctypes.c_int
        crypt32.CryptUnprotectData.argtypes = [
            ctypes.POINTER(_Blob),  # pDataIn
            ctypes.POINTER(ctypes.c_wchar_p),  # ppszDataDescr
            ctypes.POINTER(_Blob),  # pOptionalEntropy
            ctypes.c_void_p,  # pvReserved
            ctypes.c_void_p,  # pPromptStruct
            ctypes.c_uint32,  # dwFlags
            ctypes.POINTER(_Blob),  # pDataOut
        ]
        crypt32.CryptUnprotectData.restype = ctypes.c_int
        kernel32.LocalFree.argtypes = [ctypes.c_void_p]
        kernel32.LocalFree.restype = ctypes.c_void_p
    except Exception:  # noqa: BLE001 - 加载失败一律视为不可用
        _LIBS_FAILED = True
        return None
    _LIBS = (crypt32, kernel32)
    return _LIBS


def available() -> bool:
    """本机是否可用 DPAPI（Windows 且系统库加载成功）。"""
    return _IS_WINDOWS and _libs() is not None


def protect(data: bytes, entropy: bytes = ENTROPY) -> bytes | None:
    """加密；失败返回 ``None``（调用方决定降级）。"""
    libs = _libs()
    if libs is None:
        return None
    crypt32, kernel32 = libs
    try:
        in_blob, _keep_in = _blob(data)
        ent_blob, _keep_ent = _blob(entropy)
        out_blob = _Blob()
        ok = crypt32.CryptProtectData(
            ctypes.byref(in_blob),
            "qgis_dev",
            ctypes.byref(ent_blob),
            None,
            None,
            _CRYPTPROTECT_UI_FORBIDDEN,
            ctypes.byref(out_blob),
        )
        if not ok:
            return None
        try:
            return ctypes.string_at(out_blob.pbData, out_blob.cbData)
        finally:
            kernel32.LocalFree(out_blob.pbData)
    except Exception:  # noqa: BLE001 - ctypes 出错也不该让调用方崩
        return None


def unprotect(data: bytes, entropy: bytes = ENTROPY) -> bytes | None:
    """解密；失败返回 ``None``（密文损坏 / 换了 Windows 用户都会失败）。"""
    libs = _libs()
    if libs is None or not data:
        return None
    crypt32, kernel32 = libs
    try:
        in_blob, _keep_in = _blob(data)
        ent_blob, _keep_ent = _blob(entropy)
        out_blob = _Blob()
        ok = crypt32.CryptUnprotectData(
            ctypes.byref(in_blob),
            None,
            ctypes.byref(ent_blob),
            None,
            None,
            _CRYPTPROTECT_UI_FORBIDDEN,
            ctypes.byref(out_blob),
        )
        if not ok:
            return None
        try:
            return ctypes.string_at(out_blob.pbData, out_blob.cbData)
        finally:
            kernel32.LocalFree(out_blob.pbData)
    except Exception:  # noqa: BLE001
        return None


__all__ = ["ENTROPY", "available", "protect", "unprotect"]
