"""制图出图领域模块。

- :mod:`app.core.domain.cartography.settings` —— 与 QGIS 解耦的纯参数/计算；
- :mod:`app.core.domain.cartography.layout` —— QGIS 打印版面组装与输出。

子模块按需导入，包本身不 import QGIS：无 QGIS 环境也能单测 settings。
"""
