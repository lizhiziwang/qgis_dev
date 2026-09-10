"""主窗口构建工厂（阶段 4 起切到新 :class:`MainWindow`）。"""

from __future__ import annotations

from typing import Any, Optional

from app.ui.main_window import MainWindow


def build_window(app_name: Optional[str] = None) -> Any:
    """构建主窗口（新 :class:`MainWindow`）。

    :param app_name: 应用名（``app.yaml`` → ``app.name``）；主窗口用它做
        ``setWindowTitle`` 的前缀，传 ``None`` 时 :class:`MainWindow` 走默认
        "qgis_dev"。启动初始化流程里（见 :mod:`app.bootstrap`）由
        :func:`app.bootstrap.main` 从已加载的 :class:`AppConfig` 取出传入。
    """

    return MainWindow(app_name=app_name)