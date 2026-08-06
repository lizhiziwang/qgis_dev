"""日志级别 → 颜色 / 加粗 的映射。

HTML 与 ANSI 控制台两种渲染目标各保留一份。
颜色取值按级别向上递进（DEBUG → CRITICAL 视觉权重依次变重）。
"""

from __future__ import annotations

from typing import ClassVar, Dict, Tuple


class LogStyle:
    """日志级别 → 颜色 / 加粗 的映射。"""

    HTML_COLORS: ClassVar[Dict[str, str]] = {
        "DEBUG":    "#4169e1",   # 蓝色
        "INFO":     "#008000",   # 绿色
        "WARNING":  "#ff8c00",   # 橙色
        "ERROR":    "#ff0000",   # 红色
        "CRITICAL": "#8b0000",   # 深红色
    }
    HTML_BOLD: ClassVar[Dict[str, bool]] = {
        "ERROR":    True,
        "CRITICAL": True,
    }
    ANSI_COLORS: ClassVar[Dict[str, str]] = {
        "DEBUG":    "\033[34m",
        "INFO":     "\033[32m",
        "WARNING":  "\033[33m",
        "ERROR":    "\033[31m",
        "CRITICAL": "\033[1;31m",
    }
    ANSI_RESET: ClassVar[str] = "\033[0m"

    @classmethod
    def html(cls, level: str) -> Tuple[str, bool]:
        """返回 ``(颜色字符串, 是否加粗)``；未知级别回退到 INFO。"""
        level = (level or "INFO").upper()
        return (
            cls.HTML_COLORS.get(level, cls.HTML_COLORS["INFO"]),
            cls.HTML_BOLD.get(level, False),
        )
