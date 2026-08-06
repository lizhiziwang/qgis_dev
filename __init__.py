"""qgis_dev 项目根包标记。

pytest 推断 rootdir 时若根没 ``__init__.py``，会创建 namespace package，
把同目录的 ``app`` 当作 ``qgis_dev.app``，导致 ``import app`` 找不到。

加这个空文件让 pytest 把项目根当作 regular package。
"""
