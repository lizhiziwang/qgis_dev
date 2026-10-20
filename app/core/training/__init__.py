"""深度学习训练客户端核心包（数据集扫描 / 子进程客户端 / 训练库）。

本包被 QGIS 内的界面导入；``trainlib`` 另外会被外部 Python 的
``train_sidecar.py`` 直接导入，因此 ``trainlib`` 内不允许 import ``app.*``。
"""

from __future__ import annotations

__all__: list[str] = []
