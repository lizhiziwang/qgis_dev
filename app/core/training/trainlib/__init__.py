"""训练子进程侧的纯 stdlib 训练库。

``train_sidecar.py`` 由外部 Python 直接执行，脚本目录会被加入 ``sys.path``，
所以这里只能依赖标准库；重依赖（torch / torchvision / SMP / cv2 / PIL）在各
模块函数内部延迟导入，便于界面侧复用目录定义而不拖入 torch。
"""

from __future__ import annotations

__all__: list[str] = []
