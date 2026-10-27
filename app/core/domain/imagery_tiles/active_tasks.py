"""正在运行的影像切片任务登记表。

XYZ 目录切片与 MBTiles 切片可以同时多个在跑（对话框可重复打开），任务本身
不经过 ``QgsTaskManager``（见 :mod:`.task`），没有现成的全局列表。关闭程序
时主窗口靠这里判断「还有切片在进行」，先提示用户再终止，避免悄无声息地
退出、留下半套瓦片。

任务在 :meth:`~app.core.domain.imagery_tiles.task.CreateImageryTileTask.start`
时登记，线程退出（成功 / 失败 / 取消）时注销；登记表跨线程访问，用锁保护。
"""

from __future__ import annotations

import threading
from typing import Any

from app.infra.logging import get_logger

logger = get_logger(__name__)

_LOCK = threading.Lock()
#: 任务对象 → 提示文字（如「影像名 → 输出目录」）
_ACTIVE: dict[Any, str] = {}


def register(task: Any, description: str) -> None:
    """登记一个刚开始运行的切片任务。"""
    with _LOCK:
        _ACTIVE[task] = str(description or "影像切片")


def unregister(task: Any) -> None:
    """任务结束（成功 / 失败 / 取消）后注销。"""
    with _LOCK:
        _ACTIVE.pop(task, None)


def active_descriptions() -> list[str]:
    """当前仍在运行的切片任务描述（按登记顺序）。"""
    with _LOCK:
        return list(_ACTIVE.values())


def has_active() -> bool:
    """是否有切片任务在跑。"""
    with _LOCK:
        return bool(_ACTIVE)


def cancel_all() -> int:
    """取消所有在跑的切片任务，返回要求取消的个数。

    只发取消请求（子进程会被立即终止，回退线程在切片间隙退出），不等待线程
    真正结束——关闭流程不能被一个慢任务拖住；子进程另有 ``atexit`` 兜底。
    """
    with _LOCK:
        tasks = list(_ACTIVE)
    for task in tasks:
        try:
            task.cancel()
        except Exception:  # noqa: BLE001 - 关闭流程不能因单个任务失败而中断
            logger.debug("取消影像切片任务失败", exc_info=True)
    return len(tasks)


__all__ = [
    "active_descriptions",
    "cancel_all",
    "has_active",
    "register",
    "unregister",
]
