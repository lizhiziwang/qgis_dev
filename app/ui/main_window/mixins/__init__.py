"""``MainWindow`` 的功能 mixin。

本包的模块只被 :mod:`app.ui.main_window.main_window` 引用；包 ``__init__``
刻意不做 re-export，避免上层（登录阶段等 QGIS 未初始化时）意外加载 QGIS。
"""

from __future__ import annotations
