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
    def _file_based(cls) -> "Paths":
        """仅基于 ``__file__`` 推导（不含环境变量覆盖）。"""
        return cls(
            project_root=_PROJECT_ROOT,
            resources=_PROJECT_ROOT / "resource",
            logs=_PROJECT_ROOT / "logs",
            tmp=_PROJECT_ROOT / ".tmp",
            config=_PROJECT_ROOT / "config",
        )

    @classmethod
    def from_env(cls, base: Optional["Paths"] = None) -> "Paths":
        """在 ``base``（缺省 :meth:`_file_based`）之上应用 ``QGIS_DEV_PATHS_*``。

        只要设了 ``QGIS_DEV_PATHS_PROJECT_ROOT``，其余子目录默认相对它推导
        （各自仍可用对应环境变量单独覆盖）——因为打包后只有 ``PROJECT_ROOT``
        是可靠的，``__file__`` 推出的兄弟目录都在 ``_internal/`` 下。
        """
        default = base if base is not None else cls._file_based()
        if _ENV_PROJECT_ROOT in os.environ:
            root = Path(os.environ[_ENV_PROJECT_ROOT])
            fb_resources, fb_logs = root / "resource", root / "logs"
            fb_tmp, fb_config = root / ".tmp", root / "config"
        else:
            root = default.project_root
            fb_resources, fb_logs = default.resources, default.logs
            fb_tmp, fb_config = default.tmp, default.config
        return cls(
            project_root=root,
            resources=Path(os.environ[_ENV_RESOURCE_DIR])
            if _ENV_RESOURCE_DIR in os.environ
            else fb_resources,
            logs=Path(os.environ[_ENV_LOG_DIR])
            if _ENV_LOG_DIR in os.environ
            else fb_logs,
            tmp=Path(os.environ[_ENV_TMP_DIR])
            if _ENV_TMP_DIR in os.environ
            else fb_tmp,
            config=Path(os.environ[_ENV_CONFIG_DIR])
            if _ENV_CONFIG_DIR in os.environ
            else fb_config,
        )

    @classmethod
    def default(cls) -> "Paths":
        """进程当前生效的路径：``__file__`` 推导 + ``QGIS_DEV_PATHS_*`` 环境变量覆盖。

        打包后（PyInstaller ``_internal/``）``__file__`` 落在 ``_internal/`` 下，
        单靠它会得到 ``_internal/resource`` 这种**不存在**的路径；必须用 launcher /
        :func:`app.bootstrap._ensure_portable_env` 注入的环境变量纠正。

        因此 ``default()`` 本身就应用环境变量，调用方（如
        :class:`app.ui.common.icons.Icons`）不必再套一层 :meth:`from_env`。
        在开发态（无环境变量）行为与以前一致，仍是仓库根。
        """
        return cls.from_env(cls._file_based())

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
