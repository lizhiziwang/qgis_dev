"""文本/二进制文件读写基础设施。

迁移自旧 ``component/file.py``，但移除了对 QFileDialog 的依赖（file dialog
属于 UI 范畴，迁到 :mod:`app.ui.common.widgets`）。本模块只保留
纯 IO 操作。
"""

from __future__ import annotations

from pathlib import Path
from typing import Union

PathLike = Union[str, Path]


def write_text(path: PathLike, content: str, *, encoding: str = "utf-8") -> bool:
    """把字符串写到文本文件。失败返回 False。"""
    try:
        Path(path).write_text(content, encoding=encoding)
        return True
    except OSError:  # 权限、路径不存在等
        return False


def write_bytes(path: PathLike, content: bytes) -> bool:
    """把 bytes 写到二进制文件。失败返回 False。"""
    try:
        Path(path).write_bytes(content)
        return True
    except OSError:
        return False


def read_text(path: PathLike, *, encoding: str = "utf-8") -> str:
    """读文本文件，文件不存在返回空串。"""
    try:
        return Path(path).read_text(encoding=encoding)
    except (OSError, UnicodeDecodeError):
        return ""


# 向后兼容旧 API
def write_data(path: PathLike, data: str) -> bool:
    """``component.file.write_data`` 旧名别名。"""
    return write_text(path, data)
