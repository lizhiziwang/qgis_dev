"""主窗口包。

``MainWindow`` 改为**惰性导出**：包内子模块（如 :mod:`app.ui.main_window.title_bar`）
被 import 时**不能**触发 ``main_window.py`` 的加载——它顶层 ``from qgis.core import ...``，
而启动登录（:class:`app.ui.dialogs.login.LoginDialog`）在 :func:`QgsApplication.initQgis`
**之前**运行，无边框标题栏（:mod:`app.ui.common.frameless`）只需 TitleBar、不依赖 QGIS。
若这里 eager import MainWindow，登录阶段构造 LoginDialog 就会 ModuleNotFoundError。
"""

from __future__ import annotations

__all__ = ["MainWindow"]


def __getattr__(name: str):
    if name == "MainWindow":
        from .main_window import MainWindow

        return MainWindow
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
