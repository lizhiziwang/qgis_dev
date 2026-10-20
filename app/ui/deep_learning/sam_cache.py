"""SAM 图像特征缓存的目录统计与清空（UI 侧）。

缓存文件由 sidecar 以 ``<sha256>.pt`` 写入；这里只做只读扫描与删除，不加载 torch。
"""

from __future__ import annotations

from pathlib import Path

from app.core.ai.sam_models import default_cache_dir

_CACHE_SUFFIXES = (".pt", ".tmp")


def cache_files(directory: str | Path) -> list[Path]:
    """缓存目录下的文件（不递归）。目录不存在时返回空列表。"""
    path = Path(directory)
    if not path.is_dir():
        return []
    try:
        return [
            item
            for item in path.iterdir()
            if item.is_file() and item.suffix.lower() in _CACHE_SUFFIXES
        ]
    except OSError:
        return []


def cache_usage(directory: str | Path) -> tuple[int, int]:
    """返回 ``(文件数, 总字节)``。单个文件 stat 失败时跳过。"""
    count = 0
    total = 0
    for item in cache_files(directory):
        try:
            total += item.stat().st_size
        except OSError:
            continue
        count += 1
    return count, total


def clear_cache(directory: str | Path) -> tuple[int, int]:
    """删除缓存文件，返回 ``(删除数, 释放字节)``。"""
    removed = 0
    freed = 0
    for item in cache_files(directory):
        try:
            size = item.stat().st_size
            item.unlink()
        except OSError:
            continue
        removed += 1
        freed += size
    return removed, freed


def format_bytes(value: int) -> str:
    size = float(max(0, int(value)))
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if size < 1024.0 or unit == "TB":
            return f"{size:.0f} {unit}" if unit == "B" else f"{size:.1f} {unit}"
        size /= 1024.0
    return f"{size:.1f} TB"


__all__ = [
    "cache_files",
    "cache_usage",
    "clear_cache",
    "default_cache_dir",
    "format_bytes",
]
