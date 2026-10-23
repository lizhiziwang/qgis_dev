"""更新状态目录（每用户可写，与安装目录无关）。

布局::

    <base>/                      # Windows: %LOCALAPPDATA%/qgis_dev/updates
                                 # Linux:   ${XDG_DATA_HOME:-~/.local/share}/qgis_dev/updates
      <install-key>/             # 按安装位置隔离（Linux 安装版不追加，见 update_root）
        state.json               # 检查/跳过/待安装记录
        markers/                 # 启动健康标记（更新器据此判断新版本是否正常）
        downloads/               # 安装包（.part 为未完成分片，可断点续传）
        jobs/                     # 交给更新器的任务 JSON
        updater/                 # 更新器脚本 / 日志
        backup/                  # Windows 更新前的整目录备份

``QGIS_DEV_UPDATE_DIR`` 可整体覆盖（测试 / 运维排查用）；此时不再追加
install-key，直接把它当作最终目录。
"""

from __future__ import annotations

import hashlib
import os
import sys
from pathlib import Path

ENV_UPDATE_DIR = "QGIS_DEV_UPDATE_DIR"

_APP_DIR_NAME = "qgis_dev"


def _user_data_root() -> Path:
    """每用户数据根（不含应用名）。"""
    if sys.platform.startswith("win"):
        local = os.environ.get("LOCALAPPDATA")
        if local:
            return Path(local)
        return Path.home() / "AppData" / "Local"
    xdg = os.environ.get("XDG_DATA_HOME")
    if xdg:
        return Path(xdg)
    return Path.home() / ".local" / "share"


def install_key() -> str:
    """当前安装位置的短指纹（同一路径升级后保持不变）。"""
    if getattr(sys, "frozen", False):
        base = Path(sys.executable).resolve().parent
    else:
        base = Path(__file__).resolve().parents[2]
    text = str(base).replace("\\", "/").lower().encode("utf-8")
    return hashlib.sha256(text).hexdigest()[:12]


def update_root() -> Path:
    """当前安装的更新状态根目录（不保证存在）。"""
    override = os.environ.get(ENV_UPDATE_DIR)
    if override:
        return Path(override).expanduser()
    if sys.platform.startswith("linux") and getattr(sys, "frozen", False):
        # 与 tools/runtime/launch_qgis.sh 的默认值保持一致，不追加 install-key：
        # 更新器用脚本拉起新版本，若直接运行 qgis_dev 时用 keyed 目录、脚本启动
        # 时用非 keyed 目录，健康标记就会写到两个地方，更新器永远等不到。
        return _user_data_root() / _APP_DIR_NAME / "updates"
    return _user_data_root() / _APP_DIR_NAME / "updates" / install_key()


def state_file() -> Path:
    return update_root() / "state.json"


def markers_dir() -> Path:
    return update_root() / "markers"


def downloads_dir() -> Path:
    return update_root() / "downloads"


def jobs_dir() -> Path:
    return update_root() / "jobs"


def updater_dir() -> Path:
    return update_root() / "updater"


def backup_dir() -> Path:
    return update_root() / "backup"


def updater_log_file() -> Path:
    return updater_dir() / "updater.log"


def updater_heartbeat_file() -> Path:
    """更新器启动心跳。

    更新器进程会在读任务之前先写这个文件；主程序据此确认“新进程真的跑起来
    了”再退出——否则被安全软件拦下时会静默退出、用户什么也看不到。
    """
    return updater_dir() / ".updater-started"


def ensure_dirs() -> Path:
    """创建并返回更新根目录。"""
    root = update_root()
    for path in (root, markers_dir(), downloads_dir(), jobs_dir(), updater_dir()):
        path.mkdir(parents=True, exist_ok=True)
    return root


def is_under_update_root(path: Path) -> bool:
    """``path`` 是否落在更新根目录内（读取 state.json 里的路径前必须校验）。"""
    try:
        resolved = Path(path).expanduser().resolve()
        root = update_root().resolve()
        return resolved == root or root in resolved.parents
    except OSError:
        return False


__all__ = [
    "ENV_UPDATE_DIR",
    "backup_dir",
    "downloads_dir",
    "ensure_dirs",
    "install_key",
    "is_under_update_root",
    "jobs_dir",
    "markers_dir",
    "state_file",
    "update_root",
    "updater_dir",
    "updater_heartbeat_file",
    "updater_log_file",
]
