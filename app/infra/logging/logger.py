"""公共 API 入口。

- :func:`get_logger` —— 推荐入口
- :func:`setup_logger` —— 旧名别名，向后兼容既有调用点
"""

from __future__ import annotations

import logging
from typing import Optional

from .config import LogConfig
from .sink import _RootSink


def get_logger(name: Optional[str] = None) -> logging.Logger:
    """获取一个 logger 实例。

    首次调用时会装配项目级根 logger（写入 ``logs/app.log`` / ``logs/error.log``、
    输出到 stderr）。后续调用按 ``name`` 返回子 logger，子 logger 通过
    ``propagate`` 冒泡到根，从而保证每条日志只写一次文件、只输出一次控制台。

    Parameters
    ----------
    name:
        通常传模块级 ``__name__``；传入 ``None`` 或 ``"__main__"`` 时
        返回项目根 logger（名为 ``"Application"``）。
    """
    cfg = LogConfig.from_env()
    _RootSink.configure(cfg)

    if not name or name == "__main__":
        return logging.getLogger(cfg.root_logger_name)

    child = logging.getLogger(name)
    child.setLevel(cfg.root_level)
    child.propagate = True
    return child


# ---- 兼容旧 API ----
def setup_logger(name: Optional[str] = None) -> logging.Logger:
    """``setup_logger`` 旧版别名。"""
    return get_logger(name)
