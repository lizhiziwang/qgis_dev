"""领域模型层。

只允许依赖 QGIS / GDAL 几何对象，**不允许 import PyQt6**。
所有 UI / Qt 相关的逻辑放在 :mod:`app.ui`。
"""

from __future__ import annotations
