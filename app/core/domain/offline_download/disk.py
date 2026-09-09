"""磁盘容量工具（纯逻辑）：剩余空间、估算合计与下载前预检（方案 §7）。"""

from __future__ import annotations

import os
import shutil
from typing import Optional

#: 估算冗余系数：估算 × 系数 后与剩余空间比较
REDUNDANCY_FACTOR = 1.3


def free_bytes(path: str) -> Optional[int]:
    """目标目录所在盘的剩余空间（字节）；目录不存在/不可用返回 None。

    只读探测，**不创建目录**（否则“看剩余空间”会多建一层空文件夹）：
    沿路径向上找最近已存在目录取所在盘空间。
    """
    try:
        cur = str(path or "").strip() or os.getcwd()
        while cur and not os.path.isdir(cur):
            nxt = os.path.dirname(cur)
            if nxt == cur:
                return None
            cur = nxt
        return shutil.disk_usage(cur).free
    except OSError:
        return None


def estimate_needed(total_bytes: Optional[int], factor: float = REDUNDANCY_FACTOR) -> Optional[int]:
    """把已选数据集估算合计乘以冗余系数；无任何已知估算 → None（无法预检）。"""
    if not total_bytes:
        return None
    return int(total_bytes * factor)


def precheck_message(
    target_dir: str, total_est_bytes: Optional[int]
) -> Optional[str]:
    """下载前预检。空间不足/无法估算返回中文提示，否则返回 None（放行）。"""
    free = free_bytes(target_dir)
    if free is None:
        return f"下载位置不可用或磁盘不可读：{target_dir}"
    needed = estimate_needed(total_est_bytes)
    if needed is None:
        return None
    if needed > free:
        short = needed - free
        return (
            f"目标盘剩余 {format_bytes(free)}，估算需要 {format_bytes(needed)}"
            f"（含冗余系数 {REDUNDANCY_FACTOR}），至少还需 {format_bytes(short)}。"
            "请更换下载位置或减少勾选图层。"
        )
    return None


def format_bytes(num: Optional[int]) -> str:
    """字节 → 人类可读 ``123.4 GB``。None → ``未知``。"""
    if num is None:
        return "未知"
    value = float(num)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if value < 1024.0 or unit == "TB":
            if unit == "B":
                return f"{int(value)} {unit}"
            return f"{value:.1f} {unit}"
        value /= 1024.0
    return f"{num} B"
