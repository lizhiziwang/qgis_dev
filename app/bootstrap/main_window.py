"""主窗口构建工厂（阶段 4 起切到新 :class:`MainWindow`）。"""

from __future__ import annotations

from typing import Any, Optional

from app.core.auth import AuthResult
from app.ui.main_window import MainWindow


def build_window(app_name: Optional[str] = None, auth_result: AuthResult | None = None) -> Any:
    """构建主窗口（新 :class:`MainWindow`）。

    :param app_name: 应用名（``app.yaml`` → ``app.name``）；主窗口用它做
        ``setWindowTitle`` 的前缀，传 ``None`` 时 :class:`MainWindow` 走默认
        "qgis_dev"。启动初始化流程里（见 :mod:`app.bootstrap`）由
        :func:`app.bootstrap.main` 从已加载的 :class:`AppConfig` 取出传入。
    :param auth_result: 本次启动登录的结果（用户名 / token / 有效期）；
        主窗口标题栏的账号信息据此显示，传 ``None`` 时显示"未登录"。
    """

    return MainWindow(app_name=app_name, auth_result=auth_result)
