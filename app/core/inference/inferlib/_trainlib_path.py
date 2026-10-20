"""把 ``app/core/training`` 加入 ``sys.path``，复用 ``trainlib``。

推理 sidecar 与训练 sidecar 的目录布局相同（``app/core/<name>``），因此
开发态和 PyInstaller 解包目录下都能按相对位置找到 ``trainlib``。
"""

from __future__ import annotations

import sys
from pathlib import Path


def add_training_path() -> Path:
    """返回 ``app/core/training`` 并保证它在 ``sys.path`` 里。"""
    training = Path(__file__).resolve().parents[2] / "training"
    entry = str(training)
    if training.is_dir() and entry not in sys.path:
        sys.path.insert(0, entry)
    return training


__all__ = ["add_training_path"]
