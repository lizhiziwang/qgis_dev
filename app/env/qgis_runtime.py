"""QGIS 运行时配置。

负责：

1. 把 QGIS 3.x 所需的 ``QGIS_PREFIX_PATH`` / ``PROJ_LIB`` / ``GDAL_DATA`` /
   ``GDAL_DRIVER_PATH`` / ``QT_PLUGIN_PATH`` / ``PATH`` 等环境变量设置好。
2. 把 ``apps/qgis-ltr/python`` 加进 ``sys.path``，使得 ``import qgis._core``
   不会 ImportError。
3. 可选地初始化 ``processing`` 插件（迁自旧 ``setting.py:init_plugs``）。

设计：

- :class:`QgisRuntime` 数据类承载路径集合，配置来源是 :class:`AppConfig`
  （``config/app.yaml``）。
- :meth:`apply_env` 幂等，重复调用只生效一次。
- :class:`for_dev` 用本地 ``D:\\zsh\\app\\qgis_3.40`` 安装；:class:`for_prod`
  走 ``QGIS_PREFIX`` 环境变量（PyInstaller 打包后由 launcher 注入）。
"""

from __future__ import annotations

import os
import sys
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class QgisRuntime:
    """QGIS 运行时路径集合。"""

    qgis_base: Path        # 根目录，例如 D:/zsh/app/qgis_3.40
    qgis_ltr: Path         # apps/qgis-ltr
    gdal: Path             # apps/gdal
    proj: Path             # apps/proj
    bin: Path              # bin
    plugins: Path          # apps/qgis-ltr/python/plugins
    #: True = 便携扁平布局（QGIS 运行时打进 dist/app，前缀即 dist/app）
    portable: bool = False

    # -------------------------------------------------------------- 工厂

    @classmethod
    def for_dev(cls, qgis_base: Path) -> "QgisRuntime":
        """本地开发用：硬编码 ``qgis_base``（来自 ``config/app.yaml``）。"""
        qgis_base = Path(qgis_base)
        return cls(
            qgis_base=qgis_base,
            qgis_ltr=qgis_base / "apps" / "qgis-ltr",
            gdal=qgis_base / "apps" / "gdal",
            proj=qgis_base / "apps" / "proj",
            bin=qgis_base / "bin",
            plugins=qgis_base / "apps" / "qgis-ltr" / "python" / "plugins",
        )

    @classmethod
    def for_prod(cls) -> "QgisRuntime":
        """生产环境：路径走 ``QGIS_PREFIX`` 等环境变量。"""
        prefix = os.environ.get("QGIS_PREFIX")
        if not prefix:
            raise RuntimeError(
                "生产模式需要设置 QGIS_PREFIX 环境变量"
                "（PyInstaller 打包时由 launcher 注入）"
            )
        return cls.for_dev(Path(prefix))

    @classmethod
    def for_portable(cls, prefix: Path) -> "QgisRuntime":
        """便携打包：QGIS 运行时按**扁平布局**打进 ``dist/app``，前缀即 ``dist/app``。

        ``proj.db``、``qgis`` python 包、``etc/``、``plugins/``、``i18n/``、
        各种 DLL 都在前缀根目录下，环境变量直接指向前缀。
        """
        prefix = Path(prefix)
        return cls(
            qgis_base=prefix,
            qgis_ltr=prefix,
            gdal=prefix,
            proj=prefix,
            bin=prefix,
            plugins=prefix / "plugins",
            portable=True,
        )

    # -------------------------------------------------------------- 应用

    def apply_env(self) -> None:
        """写入所有 QGIS 所需的环境变量，并把路径加进 ``sys.path``。

        必须先调用此函数再 ``import qgis._core``，否则 ImportError。
        重复调用是幂等的（重复 ``sys.path.append`` 由 Python 自身去重）。
        """
        if self.portable:
            self._apply_portable_env()
            return

        # 1) 环境变量
        os.environ["QGIS_PREFIX_PATH"] = str(self.qgis_ltr)
        os.environ["PROJ_LIB"] = str(self.proj / "share")
        os.environ["GDAL_DATA"] = str(self.gdal / "data")
        os.environ["GDAL_DRIVER_PATH"] = str(self.gdal / "gdalplugins")
        os.environ["QT_PLUGIN_PATH"] = (
            f"{self.qgis_ltr / 'plugins'}{os.pathsep}{self.bin}"
        )
        os.environ["QT_QPA_PLATFORM_PLUGIN_PATH"] = str(self.bin / "platforms")
        # PATH 优先 QGIS 自带 dll，避免系统 dll 冲突
        os.environ["PATH"] = (
            f"{self.bin}{os.pathsep}"
            f"{self.qgis_ltr / 'bin'}{os.pathsep}"
            f"{self.gdal / 'bin'}{os.pathsep}"
            f"{os.environ.get('PATH', '')}"
        )

        # 2) sys.path：QGIS 自身
        for p in (
            self.qgis_ltr,
            self.proj / "share",
            self.gdal / "data",
            self.gdal / "gdalplugins",
            self.plugins,
        ):
            self._append_syspath(p)

    def _apply_portable_env(self) -> None:
        """便携布局：proj.db / qgis / etc / plugins / i18n / DLL 都在前缀根目录。"""
        p = self.qgis_base  # 即 dist/app
        os.environ["QGIS_PREFIX_PATH"] = str(p)
        os.environ["PROJ_LIB"] = str(p)                     # proj.db 在根
        os.environ["GDAL_DATA"] = str(p / "gdal_data")
        os.environ["GDAL_DRIVER_PATH"] = str(p / "gdalplugins")
        os.environ["QT_PLUGIN_PATH"] = f"{p}{os.pathsep}{p / 'plugins'}"
        os.environ["QT_QPA_PLATFORM_PLUGIN_PATH"] = str(p / "platforms")
        os.environ["PATH"] = f"{p}{os.pathsep}{os.environ.get('PATH', '')}"
        for sp in (p, p / "plugins"):
            self._append_syspath(sp)

    def init_plugins(self) -> None:
        """初始化 processing 插件。失败不抛（开发期不一定要装 processing）。"""
        try:
            from processing.core.Processing import Processing  # type: ignore[import-not-found]

            Processing.initialize()
        except Exception:  # noqa: BLE001 —— 初始化失败不影响主流程
            pass

    @staticmethod
    def _append_syspath(p: Path) -> None:
        s = str(p)
        if s and s not in sys.path:
            sys.path.append(s)
