"""深度学习推理：外部 Python 子进程 + 大图滑动窗口。

GUI 侧只负责写 manifest、启动子进程和展示事件；torch / rasterio 的导入与
窗口读写全部在 ``infer_sidecar.py`` 里完成（见 ``inferlib``）。
"""

from __future__ import annotations

__all__: list[str] = []
