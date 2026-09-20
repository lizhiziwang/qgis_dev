"""用户级配置目录（跨平台）。

只解决一件事：把「当前用户的配置目录」按平台算出来，供同目录下的
:mod:`app.core.auth.saved_logins`（账号文件）与 :mod:`app.core.auth._secret`
（本地密钥文件）共用，避免两处各写一份平台判断。
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

#: 目录名。**别改**：历史实现（已删除的 ``credential.py``）就用它，
#: 改了老用户的配置 / 记住的账号会「凭空消失」。
APP_DIR_NAME: str = "qgis_dev"


def user_config_dir() -> Path:
    """当前用户的配置目录（不保证存在）。

    - Windows：``%APPDATA%\\qgis_dev``
    - macOS：``~/Library/Application Support/qgis_dev``
    - 其它（Linux / 麒麟）：``$XDG_CONFIG_HOME/qgis_dev``（默认 ``~/.config/qgis_dev``）
    """
    if sys.platform == "win32":
        base = Path(os.environ.get("APPDATA") or (Path.home() / "AppData" / "Roaming"))
    elif sys.platform == "darwin":
        base = Path.home() / "Library" / "Application Support"
    else:
        base = Path(os.environ.get("XDG_CONFIG_HOME") or (Path.home() / ".config"))
    return base / APP_DIR_NAME


__all__ = ["APP_DIR_NAME", "user_config_dir"]
