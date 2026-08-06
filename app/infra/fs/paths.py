"""项目路径服务。

集中管理项目根、资源、日志、临时、配置等目录。所有 UI / 业务模块
**只通过** :class:`Paths` 解析资源路径，禁止再写 ``"resource/png/..."``
之类的硬编码字符串。

设计要点：

1. **以本文件所在目录为基准**解析项目根。``paths.py`` 位于
   ``<root>/app/infra/fs/``，所以 ``project_root = paths.py 的上 3 级``。
2. **frozen dataclass**：创建后不可变，避免误改。
3. **可被环境变量覆盖**（``QGIS_DEV_PATHS_PROJECT_ROOT`` 等），便于
   打包后由 launcher 注入。
4. **资源路径统一**通过 :meth:`Paths.resource` 拼接，避免散落的字符串拼接。
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

_MODULE_DIR = Path(__file__).resolve().parent          # .../app/infra/fs
_INFRA_DIR = _MODULE_DIR.parent                        # .../app/infra
_APP_DIR = _INFRA_DIR.parent                           # .../app
_PROJECT_ROOT = _APP_DIR.parent                        # 项目根

_ENV_PROJECT_ROOT = "QGIS_DEV_PATHS_PROJECT_ROOT"
_ENV_RESOURCE_DIR = "QGIS_DEV_PATHS_RESOURCE"
_ENV_LOG_DIR = "QGIS_DEV_PATHS_LOG"
_ENV_TMP_DIR = "QGIS_DEV_PATHS_TMP"
_ENV_CONFIG_DIR = "QGIS_DEV_PATHS_CONFIG"


@dataclass(frozen=True)
class Paths:
    """项目路径集合。"""

    project_root: Path
    resources: Path
    logs: Path
    tmp: Path
    config: Path

    # ------------------------------------------------------------------ 工厂

    @classmethod
    def default(cls) -> "Paths":
        """基于 ``__file__`` 推导的默认路径。"""
        return cls(
            project_root=_PROJECT_ROOT,
            resources=_PROJECT_ROOT / "resource",
            logs=_PROJECT_ROOT / "logs",
            tmp=_PROJECT_ROOT / ".tmp",
            config=_PROJECT_ROOT / "config",
        )

    @classmethod
    def from_env(cls, base: Optional["Paths"] = None) -> "Paths":
        """结合环境变量覆盖默认路径。"""
        default = base or cls.default()
        return cls(
            project_root=Path(os.environ[_ENV_PROJECT_ROOT])
            if _ENV_PROJECT_ROOT in os.environ
            else default.project_root,
            resources=Path(os.environ[_ENV_RESOURCE_DIR])
            if _ENV_RESOURCE_DIR in os.environ
            else default.resources,
            logs=Path(os.environ[_ENV_LOG_DIR])
            if _ENV_LOG_DIR in os.environ
            else default.logs,
            tmp=Path(os.environ[_ENV_TMP_DIR])
            if _ENV_TMP_DIR in os.environ
            else default.tmp,
            config=Path(os.environ[_ENV_CONFIG_DIR])
            if _ENV_CONFIG_DIR in os.environ
            else default.config,
        )

    # ------------------------------------------------------------------ 行为

    def ensure(self) -> None:
        """确保 logs/tmp/config 目录存在（resources 通常由用户管理）。"""
        for p in (self.logs, self.tmp, self.config):
            p.mkdir(parents=True, exist_ok=True)

    def resource(self, *parts: str) -> Path:
        """拼接资源路径。

        >>> paths.resource("png", "app_icon.png")
        PosixPath('.../resource/png/app_icon.png')
        """
        return self.resources.joinpath(*parts)

    def config_file(self, name: str) -> Path:
        """拼接配置文件路径。"""
        return self.config / name

    def log_file(self, name: str) -> Path:
        """拼接日志文件路径。"""
        return self.logs / name

    # ------------------------------------------------------------------ 协议

    def __post_init__(self) -> None:
        # 把所有路径转成 Path，避免字符串误用
        object.__setattr__(self, "project_root", Path(self.project_root))
        object.__setattr__(self, "resources", Path(self.resources))
        object.__setattr__(self, "logs", Path(self.logs))
        object.__setattr__(self, "tmp", Path(self.tmp))
        object.__setattr__(self, "config", Path(self.config))
