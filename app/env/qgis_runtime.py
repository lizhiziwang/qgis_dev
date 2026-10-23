"""QGIS 运行时配置。

负责：

1. 把 QGIS 4.2 所需的 ``QGIS_PREFIX_PATH`` / ``PROJ_DATA`` / ``GDAL_DATA`` /
   ``GDAL_DRIVER_PATH`` / ``QT_PLUGIN_PATH`` / ``PATH`` 等环境变量设置好。
2. 把 ``apps/qgis/python`` 加进 ``sys.path``，使得 ``import qgis._core``
   不会 ImportError。
3. 可选地初始化 ``processing`` 插件（迁自旧 ``setting.py:init_plugs``）。

设计：

- :class:`QgisRuntime` 数据类承载路径集合，配置来源是 :class:`AppConfig`
  （``config/app.yaml``）。
- :meth:`apply_env` 幂等，重复调用只生效一次。
- :class:`for_dev` 用本地 ``D:\\pf\\qgis4.2`` 安装；:class:`for_prod`
  走 ``QGIS_PREFIX`` 环境变量（PyInstaller 打包后由 launcher 注入）。

QGIS 4.2 目录布局（OSGeo4W）：

- ``apps/qgis`` —— QGIS 主程序 + python 绑定（``apps/qgis/python``）
- ``apps/qt6``  —— Qt6 运行时（``plugins`` / ``bin``）
- ``apps/gdal`` —— GDAL 数据（``share/gdal``）与驱动（``lib/gdalplugins``）
- ``share/proj`` —— PROJ 数据（``proj.db``）
- ``bin``       —— 依赖 DLL（gdal313.dll / proj_9.dll / Qt6 依赖等）
"""

from __future__ import annotations

import hashlib
import os
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

from app.infra.logging import get_logger

logger = get_logger(__name__)

#: 非 ASCII 安装路径 → ASCII 跳转链接的缓存（一次进程内只解析一次）
_ASCII_PREFIX_CACHE: dict[str, Path] = {}


def _ascii_link_base() -> Path | None:
    """找一个可写、路径全 ASCII 的目录，用来放指向 QGIS 运行时的跳转链接。

    优先 ``%LOCALAPPDATA%``；用户名是中文时退到 ``%PUBLIC%``（Windows 固定
    ``C:\\Users\\Public``），最后才考虑 ``%TEMP%``。
    """
    for candidate in (
        os.environ.get("LOCALAPPDATA"),
        os.environ.get("PUBLIC"),
        os.environ.get("TEMP"),
    ):
        if not candidate or not candidate.isascii():
            continue
        base = Path(candidate)
        try:
            if base.is_dir() and os.access(base, os.W_OK):
                return base
        except OSError:
            continue
    return None


def _junction_points_to(link: Path, target: Path) -> bool:
    try:
        if not link.is_dir() or not (link / "proj.db").is_file():
            return False
        return os.path.realpath(link) == os.path.realpath(target)
    except OSError:
        return False


def _ensure_ascii_junction(prefix: Path) -> Path | None:
    """创建/复用一个指向 ``prefix`` 的 ASCII 目录跳转（junction）。

    Windows 上 ``mklink /J`` 不需要管理员权限；目标路径可以含中文。
    """
    base = _ascii_link_base()
    if base is None:
        return None
    digest = hashlib.sha1(str(prefix).casefold().encode("utf-8")).hexdigest()[:10]
    link = base / "qgis_dev_runtime" / f"rt_{digest}"
    try:
        if _junction_points_to(link, prefix):
            return link
        if link.is_symlink() or (hasattr(os.path, "isjunction") and os.path.isjunction(link)):
            os.rmdir(link)  # 只删跳转本身，绝不动目标目录
        link.parent.mkdir(parents=True, exist_ok=True)
        proc = subprocess.run(
            ["cmd", "/c", "mklink", "/J", str(link), str(prefix)],
            capture_output=True,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        if proc.returncode != 0 or not (link / "proj.db").is_file():
            # 不按文本解码：cmd 输出是系统 ANSI(GBK)，解码会抛 UnicodeDecodeError
            logger.debug("创建 QGIS 运行时 ASCII 链接失败: rc=%s", proc.returncode)
            return None
        return link
    except OSError as exc:
        logger.debug("创建 QGIS 运行时 ASCII 链接异常: %s", exc)
        return None


def _ascii_prefix_for_qgis(prefix: Path) -> Path:
    """给 QGIS 用的前缀：含非 ASCII 字符时换成 ASCII 跳转路径。

    实测（QGIS 4.2 / Windows 10）：安装目录含中文时，QGIS 只能加载内建的
    gdal/ogr 等少量 provider，``provider_wms.dll`` 等外部插件全部加载失败，
    表现为底图打不开、``Invalid data provider wms``、空间库不可用；把
    ``QGIS_PREFIX_PATH`` 换成指向同一目录的 ASCII junction 后全部恢复。
    Python 侧路径本身支持 Unicode，所以只替换 QGIS/GDAL/Qt 用的环境变量。
    """
    text = str(prefix)
    if os.name != "nt" or text.isascii():
        return prefix
    cached = _ASCII_PREFIX_CACHE.get(text)
    if cached is not None and cached.is_dir():
        return cached
    link = _ensure_ascii_junction(prefix)
    if link is not None:
        logger.info("安装路径含非 ASCII 字符，QGIS 运行时改用 ASCII 链接: %s -> %s", link, prefix)
        _ASCII_PREFIX_CACHE[text] = link
        return link
    logger.warning(
        "安装路径含非 ASCII 字符且无法创建 ASCII 链接，QGIS 数据提供者可能加载不全: %s",
        text,
    )
    return prefix



@dataclass(frozen=True)
class QgisRuntime:
    """QGIS 运行时路径集合。"""

    qgis_base: Path        # 根目录，例如 D:/zsh/app/qgis4.2
    qgis_prefix: Path      # apps/qgis（QGIS 主目录，含 python 绑定）
    qt6: Path              # apps/qt6
    gdal: Path             # apps/gdal
    proj: Path             # share/proj（proj.db 所在）
    bin: Path              # bin（依赖 DLL）
    plugins: Path          # apps/qgis/python/plugins
    #: True = 便携布局（QGIS 运行时打进 dist/app/runtime，前缀即该目录）
    portable: bool = False
    #: True = 标准 Linux 布局（conda / apt 安装：share/qgis、lib/qgis/plugins）
    system: bool = False

    # -------------------------------------------------------------- 工厂

    @classmethod
    def for_dev(cls, qgis_base: Path) -> "QgisRuntime":
        """本地开发用：硬编码 ``qgis_base``（来自 ``config/app.yaml``）。"""
        qgis_base = Path(qgis_base)
        return cls(
            qgis_base=qgis_base,
            qgis_prefix=qgis_base / "apps" / "qgis",
            qt6=qgis_base / "apps" / "qt6",
            gdal=qgis_base / "apps" / "gdal",
            proj=qgis_base / "share" / "proj",
            bin=qgis_base / "bin",
            plugins=qgis_base / "apps" / "qgis" / "python" / "plugins",
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
        """Windows 便携打包：QGIS 运行时数据集中在 ``dist/app/runtime``，前缀即该目录。

        调用方传进来的 ``prefix`` 就是 ``runtime/``：``proj.db``、``etc/``、
        ``plugins/``、``i18n/``、``platforms/`` 都在它下面。（``qgis`` python
        包本身在 ``_internal/``，由 PyInstaller 放进 sys.path。）

        Linux 便携包的 ``runtime/`` 是 conda 风格前缀（``share/qgis`` +
        ``lib/qgis/plugins``），由 :meth:`for_system` 处理，不走这里。
        """
        prefix = Path(prefix)
        return cls(
            qgis_base=prefix,
            qgis_prefix=prefix,
            qt6=prefix,
            gdal=prefix,
            proj=prefix,
            bin=prefix,
            plugins=prefix / "plugins",
            portable=True,
        )

    @classmethod
    def for_system(cls, prefix: Path) -> "QgisRuntime":
        """标准 Linux 布局（conda / apt 安装）：
        ``share/qgis``、``lib/qgis/plugins``、``share/proj``、``share/gdal``
        都在前缀根目录下（``QGIS_PREFIX_PATH`` 即环境根）。

        Linux 真机直接跑源码（``python app/main.py``）时用，
        ``export QGIS_PREFIX=$CONDA_PREFIX`` 即生效。

        Linux 便携包的 ``runtime/`` 也按这个布局组装（迷你 conda 前缀），
        因此打包产物同样走这里：``QGIS_PREFIX=dist/app/runtime``。
        """
        prefix = Path(prefix)
        return cls(
            qgis_base=prefix,
            qgis_prefix=prefix,
            qt6=prefix,
            gdal=prefix,
            proj=prefix / "share" / "proj",
            bin=prefix / "bin",
            plugins=prefix / "share" / "qgis" / "python" / "plugins",
            system=True,
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
        if self.system:
            self._apply_system_env()
            return

        # 1) 环境变量
        os.environ["QGIS_PREFIX_PATH"] = str(self.qgis_prefix)
        os.environ["PROJ_DATA"] = str(self.proj)
        os.environ["PROJ_LIB"] = str(self.proj)  # PROJ 9+ 兼容写法
        os.environ["GDAL_DATA"] = str(self.gdal / "share" / "gdal")
        os.environ["GDAL_DRIVER_PATH"] = str(self.gdal / "lib" / "gdalplugins")
        os.environ["QT_PLUGIN_PATH"] = (
            f"{self.qgis_prefix / 'qtplugins'}{os.pathsep}{self.qt6 / 'plugins'}"
        )
        os.environ["QT_QPA_PLATFORM_PLUGIN_PATH"] = str(
            self.qt6 / "plugins" / "platforms"
        )
        # PATH 优先 QGIS 自带 dll，避免系统 dll 冲突
        os.environ["PATH"] = (
            f"{self.bin}{os.pathsep}"
            f"{self.qgis_prefix / 'bin'}{os.pathsep}"
            f"{self.qt6 / 'bin'}{os.pathsep}"
            f"{os.environ.get('PATH', '')}"
        )

        # 2) sys.path：QGIS 自身
        for p in (
            self.qgis_prefix / "python",
            self.proj,
            self.gdal / "share" / "gdal",
            self.gdal / "lib" / "gdalplugins",
            self.plugins,
        ):
            self._append_syspath(p)

    def _apply_portable_env(self) -> None:
        """便携布局：proj.db / etc / plugins / i18n / platforms 都在前缀（runtime/）下。

        安装目录含中文时 QGIS 的 C++ provider 插件加载会失败，
        :func:`_ascii_prefix_for_qgis` 会改用 ASCII junction，见其文档。
        """
        p = _ascii_prefix_for_qgis(self.qgis_base)  # 即 dist/app/runtime（或其 ASCII 链接）
        os.environ["QGIS_PREFIX_PATH"] = str(p)
        os.environ["PROJ_DATA"] = str(p)                     # proj.db 在根
        os.environ["PROJ_LIB"] = str(p)
        os.environ["GDAL_DATA"] = str(p / "gdal_data")
        os.environ["GDAL_DRIVER_PATH"] = str(p / "gdalplugins")
        os.environ["QT_PLUGIN_PATH"] = f"{p}{os.pathsep}{p / 'plugins'}"
        os.environ["QT_QPA_PLATFORM_PLUGIN_PATH"] = str(p / "platforms")
        # 包内 bin/ 是 GDAL 命令行工具（gdal_translate / gdalwarp ...）：
        # processing 的 gdal provider 通过 PATH 找它们（GdalUtils.runGdal）。
        os.environ["PATH"] = (
            f"{p / 'bin'}{os.pathsep}{p}{os.pathsep}{os.environ.get('PATH', '')}"
        )
        for sp in (p, p / "plugins"):
            self._append_syspath(sp)

    def _apply_system_env(self) -> None:
        """标准 Linux 布局（conda / apt）：share/qgis、lib/qgis/plugins。

        conda 的 QGIS python 包在 ``share/qgis/python``（``import qgis`` 时
        conda 已把该路径放进 sys.path，这里再补一次幂等）。

        Qt 插件目录按布局选：conda Qt6 在 ``lib/qt6/plugins``，conda Qt4/5 与
        apt 在 ``plugins``。写死 ``$PREFIX/plugins`` 会覆盖掉 conda 激活脚本的
        正确值，Qt 只能靠 QLibraryInfo 的编译期前缀兜底，兜底不成立时
        QApplication 会在登录框之前因找不到 xcb 平台插件退出。
        """
        p = self.qgis_base  # 即环境根（$CONDA_PREFIX 或 /usr）
        os.environ["QGIS_PREFIX_PATH"] = str(p)
        os.environ["PROJ_DATA"] = str(p / "share" / "proj")
        os.environ["PROJ_LIB"] = str(p / "share" / "proj")
        os.environ["GDAL_DATA"] = str(p / "share" / "gdal")
        # 便携包搬到别处后，libgdal 编译期烧进去的绝对路径
        # （构建机 $CONDA_PREFIX/lib/gdalplugins）已经失效；不显式指定时 GDAL
        # 仍能启动，但会静默丢掉 21 个插件驱动（GRIB/HDF5/netCDF/PDF/JP2 等）。
        # 目录不存在时保留 libgdal 的默认查找，避免把驱动力全关掉。
        gdal_drivers = p / "lib" / "gdalplugins"
        if gdal_drivers.is_dir():
            os.environ["GDAL_DRIVER_PATH"] = str(gdal_drivers)
        qt_plugins: Path | None = None
        for candidate in (
            p / "lib" / "qt6" / "plugins",
            p / "lib" / "qt5" / "plugins",
            p / "plugins",
            p / "qtplugins",
        ):
            if (candidate / "platforms").is_dir():
                qt_plugins = candidate
                break
        if qt_plugins is None:
            qt_plugins = p / "plugins"  # 保持旧值，至少不让路径变成空
        existing_qt_plugins = os.environ.get("QT_PLUGIN_PATH", "")
        os.environ["QT_PLUGIN_PATH"] = (
            f"{qt_plugins}{os.pathsep}{existing_qt_plugins}"
            if existing_qt_plugins
            and str(qt_plugins) not in existing_qt_plugins.split(os.pathsep)
            else str(qt_plugins)
        )
        os.environ["QT_QPA_PLATFORM_PLUGIN_PATH"] = str(qt_plugins / "platforms")
        os.environ["PATH"] = f"{p / 'bin'}{os.pathsep}{os.environ.get('PATH', '')}"
        for sp in (p / "share" / "qgis" / "python", self.plugins):
            self._append_syspath(sp)

    def init_plugins(self) -> None:
        """初始化 processing 插件。

        ``processing`` 是 Python 插件（位于 ``python/plugins/processing``），
        打包时必须把它复制进 ``plugins/`` 目录；否则 ``qgis.processing.run``
        始终是 stub，调用即抛 ``QgsNotSupportedException: Processing plugin
        has not been loaded``（瓦片 / 缓冲工具不可用）。
        失败不抛（初始化失败不影响主流程），但会记录日志以便排查。
        """
        try:
            from processing.core.Processing import Processing

            Processing.initialize()
        except Exception:  # noqa: BLE001
            logger.warning(
                "Processing 插件初始化失败，瓦片 / 缓冲等依赖 processing.run 的工具将不可用",
                exc_info=True,
            )

    @staticmethod
    def _append_syspath(p: Path) -> None:
        s = str(p)
        if s and s not in sys.path:
            sys.path.append(s)
