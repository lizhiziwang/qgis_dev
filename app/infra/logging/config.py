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
    # 含 ``funcName`` / ``lineno``：定位问题的第一步是知道"哪一行"。打包后源码被
    # 编译成 .pyd（``config/app.yaml`` 的 ``build.protect.cython``），产物里没有
    # .py，只能靠日志里的文件名:行号去对同一 commit 的源码，所以这两项必须有。
    # ``op_id``：当前矢量操作编号（见 :mod:`app.infra.logging.operation`），
    # 不在操作里是 ``-``；有它才能把"用户点了什么"与"报错那几行"对上。
    log_format: str = (
        "%(asctime)s - %(name)s.%(funcName)s:%(lineno)d - "
        "%(process)d:%(thread)d - op=%(op_id)s - __%(levelname)s__ - %(message)s"
    )
    date_format: str = "%Y-%m-%d %H:%M:%S"

    #: 崩溃日志（``crash.log``）的大小上限与备份数。崩溃日志由
    #: :mod:`app.infra.logging.crash` 单独管理，不跟着上面的轮转配置走——
    #: 它可能被"事件循环里每帧抛的同一个异常"灌满，需要更小的上限。
    crash_max_bytes: int = 5 * 1024 * 1024
    crash_backup_count: int = 3

    #: 会话日志（``logs/session/``，见 :mod:`app.infra.logging.session`）：一次运行
    #: 一对文件（纯文本 + JSONL）。上限比 ``app.log`` 小——它是给用户回传用的，
    #: 长跑一整天也不该在便携包里堆出几百 MB；``session_keep`` 是**保留几次会话**
    #: （每次启动清一次旧的，轮转出来的 ``.1`` 备份跟着主文件一起算一次会话）。
    session_max_bytes: int = 5 * 1024 * 1024
    session_backup_count: int = 1
    session_keep: int = 5

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
