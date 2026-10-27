"""把安装任务交给独立更新器进程。

主程序不能在“替换自己所在的目录”这件事上自己动手：Windows 上正在运行的
exe/dll 被占用，Linux 上 ``/opt`` 需要 root。做法统一为——主程序只负责写一份
任务 JSON，然后启动一个**独立进程**，自己退出：

- Windows：``powershell.exe -STA -File update.ps1 -JobFile job.json``，没有控制台
  窗口，脚本自己弹一个 WinForms 进度窗口（主程序退出后用户能看到更新进度 /
  失败原因 / 回滚结果）。脚本的第一个动作是写“启动心跳”，主程序轮询到心跳
  才真正退出；没等到心跳就保持运行并提示用户，避免更新器被拦截时静默退出。
- Linux：``pkexec /usr/local/lib/qgis_dev/qgis-dev-updater <job.json>``；
  提权程序是 root 所有、固定路径，**自己重新验签清单**后才动手——任务文件在
  普通用户可写目录里，不能作为信任来源。

源码运行 / 不支持的包格式 → 不启动更新器，返回手工安装提示（UI 打开所在
文件夹），绝不在开发机上覆盖仓库目录。
"""

from __future__ import annotations

import base64
import json
import os
import re
import shutil
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from app.infra.logging import get_logger
from app.update import paths as update_paths
from app.update.checker import CheckResult
from app.update.errors import UpdateInstallError
from app.update.updater_scripts import WINDOWS_UPDATER_PS1

logger = get_logger(__name__)

#: Linux 提权更新器（由 .deb 安装，root:root 0755）
LINUX_HELPER = Path("/usr/local/lib/qgis_dev/qgis-dev-updater")

#: Linux 自动更新的唯一安装目录（.deb 布局；提权更新器只认这个路径）
LINUX_APP_DIR = Path("/opt/qgis_app")

#: Linux 更新器日志（root 可写）
LINUX_HELPER_LOG = Path("/var/log/qgis_dev-updater.log")

#: 更新器脚本里的能力版本（HELPER_VERSION）；>=2 才会写安装进度文件
_HELPER_PROGRESS_VERSION = 2

#: 安装进度文件（与任务 JSON 同目录，由 root 更新器原子写入）
_LINUX_STATUS_FILE = "install-status.json"

_WINDOWS_INSTALLER_SUFFIXES = (".exe", ".msi")
_LINUX_INSTALLER_SUFFIXES = (".tar.gz", ".tgz")

#: 传给新进程的桌面会话环境（pkexec 会清空环境变量）。更新目录 / 配置目录
#: 必须一并带过去，否则新进程会落到不同的每用户路径，更新器等不到健康标记。
_DESKTOP_ENV_KEYS = (
    "DISPLAY",
    "WAYLAND_DISPLAY",
    "XAUTHORITY",
    "XDG_RUNTIME_DIR",
    "DBUS_SESSION_BUS_ADDRESS",
    "XDG_DATA_HOME",
    "XDG_CONFIG_HOME",
    "XDG_CACHE_HOME",
    "QGIS_DEV_UPDATE_DIR",
    "QGIS_DEV_CONFIG",
    "LANG",
    "LC_ALL",
    "PATH",
)


@dataclass(frozen=True)
class InstallLaunch:
    """启动结果。``started=False`` 时 UI 应展示 ``reason`` 并提供手工安装入口。"""

    started: bool
    reason: str = ""
    package_path: Path | None = None
    #: 更新器启动心跳（主程序轮询到它才退出）；无心跳的平台为 ``None``
    heartbeat_path: Path | None = None
    #: Linux 安装进度文件（新 helper 才会写；None = 退回“启动后直接退出”）
    status_path: Path | None = None
    #: pkexec 进程 pid（日志 / 兜底判断；存活判断优先用 ``process.poll()``）
    process_pid: int | None = None
    #: pkexec 进程对象：子进程退出后必须 poll() 回收，否则僵尸进程让存活判断永远为真
    process: subprocess.Popen | None = None
    #: 本次任务 id（进度文件里也带，防止读到上一次的残留）
    job_id: str = ""
    #: 更新器日志路径（启动失败时展示给用户）
    log_path: Path | None = None

    @property
    def can_install(self) -> bool:
        return self.started


class UpdaterLauncher:
    """按当前平台构造任务并启动更新器。"""

    def __init__(self, *, app_dir: Path | None = None, exe_path: Path | None = None) -> None:
        self._app_dir = app_dir or (Path(sys.executable).resolve().parent if getattr(sys, "frozen", False) else None)
        self._exe_path = exe_path or (Path(sys.executable).resolve() if getattr(sys, "frozen", False) else None)

    # ------------------------------------------------------------------ 能力

    @property
    def frozen(self) -> bool:
        return bool(getattr(sys, "frozen", False))

    def can_auto_install(self, package_path: Path) -> bool:
        if not self.frozen:
            return False
        if sys.platform.startswith("win"):
            # 只有 Inno 安装版才能自动装：便携目录里运行安装包会把程序装到
            # %LOCALAPPDATA%，却更新不到当前目录，最终只能回滚。
            return (
                package_path.suffix.lower() in _WINDOWS_INSTALLER_SUFFIXES
                and self._installed_by_inno()
            )
        if sys.platform.startswith("linux"):
            return (
                self._installed_under_linux_app_dir()
                and _has_suffix(package_path, _LINUX_INSTALLER_SUFFIXES)
                and LINUX_HELPER.is_file()
                and os.access(LINUX_HELPER, os.X_OK)
                and shutil.which("pkexec") is not None
            )
        return False

    def _installed_by_inno(self) -> bool:
        """安装目录里是否有 Inno 卸载程序（便携目录没有）。"""
        if self._app_dir is None:
            return False
        try:
            return any(self._app_dir.glob("unins*.exe"))
        except OSError:
            return False

    def _installed_under_linux_app_dir(self) -> bool:
        """只有 .deb 安装（/opt/qgis_app）才能走提权更新器。

        提权程序把安装目录写死成 ``/opt/qgis_app``（任务文件里的 ``appDir``
        不可信）；便携版 / 开发目录传进去只会被拒绝，所以先在客户端挡住，
        直接退化成手工安装提示。
        """
        if self._app_dir is None:
            return False
        try:
            return self._app_dir.resolve() == LINUX_APP_DIR.resolve()
        except OSError:
            return False

    def manual_hint(self, package_path: Path) -> str:
        if not self.frozen:
            return "当前为源码运行，不支持自动安装；请在打包后的程序中更新。"
        if sys.platform.startswith("win"):
            if package_path.suffix.lower() not in _WINDOWS_INSTALLER_SUFFIXES:
                return "该安装包格式不支持自动安装，请手动运行安装程序。"
            if not self._installed_by_inno():
                return (
                    "当前程序不是安装版（目录中没有卸载程序），不支持自动安装；"
                    "请手动运行安装包升级，或改用安装版。"
                )
            return "无法启动自动更新，请手动运行安装程序。"
        if sys.platform.startswith("linux"):
            if _has_suffix(package_path, (".deb",)):
                return "deb 包请使用系统包管理器安装。"
            if not self._installed_under_linux_app_dir():
                return (
                    "当前程序不是 deb 安装版（不在 /opt/qgis_app），"
                    "不支持自动安装；请手动解压覆盖或重新安装 deb 包。"
                )
            if not LINUX_HELPER.is_file():
                return "系统未安装提权更新器（qgis-dev-updater），请重新安装完整安装包。"
            if shutil.which("pkexec") is None:
                return "系统缺少 pkexec，无法自动安装，请手动解压覆盖。"
        return "当前平台不支持自动安装，请手动安装。"

    # ------------------------------------------------------------------ 启动

    def launch(self, result: CheckResult, package_path: Path) -> InstallLaunch:
        """写任务并启动更新器；不支持自动安装时返回 ``started=False``。"""
        package_path = Path(package_path)
        if not package_path.is_file():
            raise UpdateInstallError("安装包不存在，请重新下载")
        if not self.can_auto_install(package_path):
            return InstallLaunch(False, self.manual_hint(package_path), package_path)
        update_paths.ensure_dirs()
        if sys.platform.startswith("win"):
            heartbeat = self._launch_windows(result, package_path)
            return InstallLaunch(
                True,
                "",
                package_path,
                heartbeat_path=heartbeat,
                log_path=update_paths.updater_log_file(),
            )
        status_path, process, job_id = self._launch_linux(result, package_path)
        return InstallLaunch(
            True,
            "",
            package_path,
            status_path=status_path,
            process_pid=process.pid if process is not None else None,
            process=process,
            job_id=job_id,
            # Linux 更新器写的是 /var/log/qgis_dev-updater.log；updater_dir 里的
            # updater.log 只有 Windows 脚本会写。
            log_path=LINUX_HELPER_LOG,
        )

    # ------------------------------------------------------------------ Windows

    def _launch_windows(self, result: CheckResult, package_path: Path) -> Path:
        job = self._base_job(result, package_path)
        job.update(
            {
                "platform": "windows",
                "backup": _backup_enabled(),
                "backupDir": str(update_paths.backup_dir()),
                "installerLog": str(update_paths.updater_dir() / "installer.log"),
                "waitSeconds": 600,
                "healthTimeoutSeconds": 240,
            }
        )
        job_path = _write_job(job)
        script_path = update_paths.updater_dir() / "update.ps1"
        script_path.write_text(WINDOWS_UPDATER_PS1, encoding="utf-8-sig")

        # 上一次的日志 / 心跳不能冒充这一次：启动前先清掉。
        log_path = update_paths.updater_log_file()
        heartbeat = update_paths.updater_heartbeat_file()
        for stale in (log_path, heartbeat):
            try:
                stale.unlink()
            except FileNotFoundError:
                pass
            except OSError as exc:
                logger.warning("无法清理旧的更新器文件 %s：%s", stale, exc)

        powershell = _powershell()
        if powershell is None:
            raise UpdateInstallError("找不到 PowerShell，无法启动自动更新")
        command = [
            powershell,
            "-NoProfile",
            "-NonInteractive",
            "-STA",  # WinForms 进度窗口需要 STA
            "-ExecutionPolicy",
            "Bypass",
            "-File",
            str(script_path),
            "-JobFile",
            str(job_path),
            "-LogFile",
            str(log_path),
            "-HeartbeatFile",
            str(heartbeat),
        ]
        flags = 0
        if os.name == "nt":
            # 不能用 DETACHED_PROCESS：实测 Windows 10 上 powershell.exe 以
            # detached 方式启动会立即退出（退出码 0，脚本完全不执行，日志也没有）。
            # CREATE_NO_WINDOW 不创建控制台窗口，但不影响脚本自己弹的 WinForms
            # 窗口；子进程也不会随主程序退出而结束。不要再传 -WindowStyle Hidden：
            # 隐式隐藏的 PowerShell 容易被安全软件（如火绒 HIPS）拦截。
            flags = subprocess.CREATE_NO_WINDOW | subprocess.CREATE_NEW_PROCESS_GROUP
        try:
            subprocess.Popen(
                command,
                close_fds=True,
                cwd=str(update_paths.updater_dir()),
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                creationflags=flags,
            )
        except OSError as exc:
            raise UpdateInstallError(f"启动更新器失败：{exc}") from exc
        logger.info(
            "Windows 更新器已启动：job=%s package=%s heartbeat=%s",
            job_path,
            package_path,
            heartbeat,
        )
        return heartbeat

    # ------------------------------------------------------------------ Linux

    def _launch_linux(
        self, result: CheckResult, package_path: Path
    ) -> tuple[Path | None, subprocess.Popen | None, str]:
        job = self._base_job(result, package_path)
        job.update(
            {
                "platform": "linux",
                "backupDir": str(update_paths.backup_dir()),
                "uid": os.getuid(),
                "gid": os.getgid(),
                "user": _current_user(),
                "home": str(Path.home()),
                "env": {key: os.environ[key] for key in _DESKTOP_ENV_KEYS if key in os.environ},
                "helperLog": str(LINUX_HELPER_LOG),
                "waitSeconds": 600,
                "healthTimeoutSeconds": 240,
            }
        )
        # 自动更新只替换 /opt/qgis_app，不替换 helper：新客户端配旧 helper 时
        # 不能等一个永远不出现的进度文件，直接退回旧行为。
        status_path: Path | None = None
        if linux_helper_supports_progress():
            status_path = update_paths.updater_dir() / _LINUX_STATUS_FILE
            job["statusFile"] = str(status_path)
            try:
                status_path.unlink()
            except FileNotFoundError:
                pass
            except OSError as exc:
                logger.warning("无法清理旧的安装进度文件 %s：%s", status_path, exc)
        job_path = _write_job(job)
        try:
            process = subprocess.Popen(
                ["pkexec", str(LINUX_HELPER), str(job_path)],
                close_fds=True,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                start_new_session=True,
                cwd=str(Path.home()),
            )
        except OSError as exc:
            raise UpdateInstallError(f"启动提权更新器失败：{exc}") from exc
        logger.info(
            "Linux 更新器已启动：job=%s package=%s status=%s pid=%s",
            job_path,
            package_path,
            status_path,
            process.pid,
        )
        if status_path is None:
            return None, process, ""
        return status_path, process, str(job.get("jobId") or "")

    # ------------------------------------------------------------------ 任务

    def _base_job(self, result: CheckResult, package_path: Path) -> dict[str, Any]:
        if self._app_dir is None or self._exe_path is None:
            raise UpdateInstallError("无法确定当前安装目录，不能自动更新")
        package = result.package
        if package is None:
            raise UpdateInstallError("清单中没有可安装的包")
        return {
            "jobId": f"{result.manifest.version_code}-{int(time.time())}-{os.getpid()}",
            "pid": os.getpid(),
            "appDir": str(self._app_dir),
            "exePath": str(self._exe_path),
            "packagePath": str(package_path),
            "packageId": package.id,
            "packageSha256": package.sha256,
            "expectedVersion": result.manifest.version,
            "expectedVersionCode": result.manifest.version_code,
            "channel": result.manifest.channel,
            "markerDir": str(update_paths.markers_dir()),
            "updaterDir": str(update_paths.updater_dir()),
            "logFile": str(update_paths.updater_log_file()),
            "manifest": _b64(result.manifest_raw),
            "signature": _b64(result.signature),
            "keyId": result.key_id,
        }


def _write_job(job: dict[str, Any]) -> Path:
    path = update_paths.jobs_dir() / f"update-{job['jobId']}.json"
    temp = path.with_suffix(".json.tmp")
    temp.write_text(json.dumps(job, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(temp, path)
    return path


def _b64(value: bytes) -> str:
    return base64.b64encode(value).decode("ascii")


def _powershell() -> str | None:
    if os.name == "nt":
        system_root = os.environ.get("SYSTEMROOT", r"C:\Windows")
        candidate = Path(system_root) / "System32" / "WindowsPowerShell" / "v1.0" / "powershell.exe"
        if candidate.is_file():
            return str(candidate)
    return shutil.which("powershell") or shutil.which("pwsh")


def _backup_enabled() -> bool:
    value = os.environ.get("QGIS_DEV_UPDATE_BACKUP", "1").strip().lower()
    return value not in ("0", "false", "no", "off")


def _has_suffix(path: Path, suffixes: tuple[str, ...]) -> bool:
    name = path.name.lower()
    return any(name.endswith(suffix) for suffix in suffixes)


def linux_helper_supports_progress(helper: Path | None = None) -> bool:
    """提权更新器是否支持安装进度（脚本里有 ``HELPER_VERSION >= 2``）。

    读取 helper 本体而不是额外版本文件：helper 由 deb 安装、root:root 0755，
    普通用户可读；这样旧 deb（1.0.8 及以前）配新客户端时会自动退回老流程，
    不会卡在“等待进度文件”。
    """
    helper = LINUX_HELPER if helper is None else helper
    try:
        text = helper.read_text(encoding="utf-8", errors="ignore")
    except OSError:
        return False
    match = re.search(r"^HELPER_VERSION\s*=\s*(\d+)\s*$", text, re.MULTILINE)
    if match is None:
        return False
    try:
        return int(match.group(1)) >= _HELPER_PROGRESS_VERSION
    except ValueError:
        return False


def _current_user() -> str:
    try:
        import getpass

        return getpass.getuser()
    except Exception:  # noqa: BLE001
        return ""


__all__ = ["LINUX_APP_DIR", "LINUX_HELPER", "InstallLaunch", "UpdaterLauncher", "linux_helper_supports_progress"]
