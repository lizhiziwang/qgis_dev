"""日志记录附带的当前用户与系统标识。

日志系统在登录之前就装配好了（见 :func:`app.bootstrap.main`），所以不能在
装配时把用户写死；这里用进程级模块变量保存当前登录用户，JSONL formatter
在每条记录格式化时读取。

**不用 ``ContextVar``**：后台线程有独立的 context，登录状态必须对整个进程
（包括工作线程、QGIS 消息线程）生效。
"""

from __future__ import annotations

import platform

_user_id = ""
_user_name = ""

_host = (platform.node() or "").strip() or "-"
_os = " ".join(
    part for part in ((platform.system() or "").strip(), (platform.release() or "").strip()) if part
) or "-"


def set_log_user(user_id: str = "", user_name: str = "") -> None:
    """登记当前登录用户（登录成功后调用；失败 / 未登录时传空串）。"""
    global _user_id, _user_name
    _user_id = (user_id or "").strip()
    _user_name = (user_name or "").strip()


def current_log_user() -> tuple[str, str]:
    """当前登录用户 ``(user_id, user_name)``；未登录时都是空串。"""
    return _user_id, _user_name


def log_host() -> str:
    """主机名（进程生命周期内不变）。"""
    return _host


def log_os() -> str:
    """操作系统型号，如 ``Windows 11`` / ``Kylin V10``（缺项退化成 ``-``）。"""
    return _os


__all__ = ["current_log_user", "log_host", "log_os", "set_log_user"]
