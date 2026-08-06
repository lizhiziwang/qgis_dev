"""日志系统配置项。

全部字段都是只读快照，构建后不可变。运行时如需修改请重新构造实例并
调用 :func:`app.infra.logging.get_logger`，或在调用前修改环境变量。

环境变量：

- ``QGIS_DEV_LOG_DIR`` —— 覆盖 ``log_dir``
- ``QGIS_DEV_LOG_LEVEL`` —— 覆盖 ``root_level`` 与 ``console_level``，字符串级别名
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, Optional

# 以本文件所在目录为基准解析日志目录，跨工作目录运行与 PyInstaller 打包后都可定位。
_MODULE_DIR = Path(__file__).resolve().parent            # .../app/infra/logging
_INFRA_DIR = _MODULE_DIR.parent                          # .../app/infra
_APP_DIR = _INFRA_DIR.parent                             # .../app
_PROJECT_ROOT = _APP_DIR.parent                          # 项目根目录
_DEFAULT_LOG_DIR = _PROJECT_ROOT / "logs"

_ENV_LOG_DIR = "QGIS_DEV_LOG_DIR"
_ENV_LOG_LEVEL = "QGIS_DEV_LOG_LEVEL"


@dataclass(frozen=True)
class LogConfig:
    """日志系统配置。"""

    log_dir: Path = _DEFAULT_LOG_DIR
    console_level: int = logging.INFO
    root_level: int = logging.DEBUG
    file_max_bytes: int = 10 * 1024 * 1024   # 10 MB
    file_backup_count: int = 5
    file_encoding: str = "utf-8"
    root_logger_name: str = "Application"
    all_log_filename: str = "app.log"
    error_log_filename: str = "error.log"
    log_format: str = (
        "%(asctime)s - %(name)s - %(process)d:%(thread)d - "
        "__%(levelname)s__ - %(message)s"
    )
    date_format: str = "%Y-%m-%d %H:%M:%S"

    @classmethod
    def from_env(cls) -> "LogConfig":
        """根据环境变量生成配置覆盖项。"""
        overrides: Dict[str, object] = {}
        env_dir = os.environ.get(_ENV_LOG_DIR)
        if env_dir:
            overrides["log_dir"] = Path(env_dir)
        env_level = os.environ.get(_ENV_LOG_LEVEL)
        if env_level:
            numeric = logging.getLevelName(env_level.upper())
            if isinstance(numeric, int):
                overrides["root_level"] = numeric
                overrides["console_level"] = numeric
        if overrides:
            return cls(**overrides)  # type: ignore[arg-type]
        return cls()

    def ensure(self) -> None:
        """确保 ``log_dir`` 存在。"""
        self.log_dir.mkdir(parents=True, exist_ok=True)
