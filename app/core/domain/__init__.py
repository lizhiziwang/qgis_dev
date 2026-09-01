"""领域模型层。

只允许依赖 QGIS / GDAL 几何对象，**不允许直接 import Qt 绑定（统一走 qgis.PyQt）**。
所有 UI / Qt 相关的逻辑放在 :mod:`app.ui`。
"""

from __future__ import annotations
