"""更新控制器：定时检查 / 后台下载 / 启动更新器。

线程模型与登录框一致（:mod:`app.ui.dialogs.login`）：普通
:class:`threading.Thread` 跑网络与哈希，结果经 Qt 信号回到主线程；Qt 控件只在
主线程碰。

触发点：

- 启动后 ``update.startup_delay_ms`` 延迟一次（避开启动高峰）；
- 之后每 ``update.check_interval_hours`` 小时一次；
- 「帮助」页签的「检查更新」按钮（手动，永远显示结果）。

强制更新只影响提示频率：每次启动 / 周期检查都会再弹，但用户始终可以“稍后”，
不阻断程序使用。
"""

from __future__ import annotations

import json
import os
import threading
import time
from pathlib import Path

from qgis.PyQt.QtCore import QObject, Qt, QTimer, QUrl, pyqtSignal
from qgis.PyQt.QtGui import QDesktopServices
from qgis.PyQt.QtWidgets import QApplication, QMessageBox

from app import __version__
from app.env.config import AppConfig
from app.infra.logging import get_logger
from app.ui.update.dialog import UpdateDialog
from app.update.errors import UpdateCancelledError, UpdateError
from app.update.launcher import InstallLaunch
from app.update.service import UpdateOutcome, UpdateService

logger = get_logger(__name__)

#: 更新器最多等旧进程 ``waitSeconds`` 秒（任务里固定 600s），再加一点余量。
#: 过了这个窗口它已自行退出，应允许重新发起安装——否则用户取消过一次退出，
#: 整场会话都无法再更新。
_INSTALLER_WAIT_WINDOW_SECONDS = 660

#: 等待更新器“启动心跳”的轮询间隔 / 超时。更新器进程一起来就写心跳（早于
#: 读任务和弹窗），正常冷启动 1~2 秒；超过这个时间还没心跳，基本可以判定
#: 被安全软件拦下了——此时绝不能退出主程序，否则用户什么都看不到。
_HEARTBEAT_POLL_MS = 250
_HEARTBEAT_TIMEOUT_SECONDS = 8.0


class UpdateController(QObject):
    """主窗口级更新控制器（实例必须由主窗口持有一个引用）。"""

    _checked = pyqtSignal(object, bool, str)   # outcome | None, manual, error
    _progress = pyqtSignal(int, int)           # done, total
    _downloaded = pyqtSignal(object, str)      # outcome, path
    _download_failed = pyqtSignal(str)
    _download_cancelled = pyqtSignal()

    def __init__(self, window, config: AppConfig) -> None:
        super().__init__(window)
        self._window = window
        self._service = UpdateService(config)
        self._config = config
        self._dialog: UpdateDialog | None = None
        self._thread: threading.Thread | None = None
        self._cancel: threading.Event | None = None
        self._checking = False
        self._downloading = False
        self._installer_launched = False
        self._installer_launched_at = 0.0
        self._launch: InstallLaunch | None = None
        self._heartbeat_deadline = 0.0
        self._heartbeat_timer = QTimer(self)
        self._heartbeat_timer.setInterval(_HEARTBEAT_POLL_MS)
        self._heartbeat_timer.timeout.connect(self._poll_updater_heartbeat)
        self._prompted: set[int] = set()
        self._timer = QTimer(self)
        self._timer.setTimerType(Qt.TimerType.CoarseTimer)
        self._timer.timeout.connect(self._on_periodic_check)
        self._checked.connect(self._on_checked)
        self._progress.connect(self._on_progress)
        self._downloaded.connect(self._on_downloaded)
        self._download_failed.connect(self._on_download_failed)
        self._download_cancelled.connect(self._on_download_cancelled)
        self._started = False

    # ------------------------------------------------------------------ 生命周期

    def start(self) -> None:
        """主窗口显示后调用：登记启动检查与周期检查。"""
        if self._started:
            return
        self._started = True
        update_cfg = self._config.update
        if not self._service.enabled:
            logger.info("自动更新已关闭（update.enabled=false）")
            return
        if update_cfg.check_on_startup:
            QTimer.singleShot(update_cfg.startup_delay_ms, self._on_startup_check)
            logger.info(
                "自动检查更新已排程：启动后 %d ms（channel=%s）",
                update_cfg.startup_delay_ms,
                self._service.channel,
            )
        interval_ms = max(1, int(update_cfg.check_interval_hours)) * 3600 * 1000
        self._timer.start(interval_ms)

    def shutdown(self) -> None:
        """窗口关闭时停止定时器并取消下载（进程即将退出）。"""
        self._timer.stop()
        self._heartbeat_timer.stop()
        if self._cancel is not None:
            self._cancel.set()

    # ------------------------------------------------------------------ 入口

    def check_now(self) -> None:
        """「帮助」页签的「检查更新」。"""
        self.check(manual=True)

    def check(self, *, manual: bool) -> None:
        if self._checking:
            if manual:
                QMessageBox.information(self._window, "检查更新", "正在检查更新，请稍候…")
            return
        self._checking = True
        self._thread = threading.Thread(
            target=self._run_check, args=(manual,), name="update-check", daemon=True
        )
        self._thread.start()

    # ------------------------------------------------------------------ 后台

    def _run_check(self, manual: bool) -> None:
        try:
            outcome = self._service.check(manual=manual)
            self._checked.emit(outcome, manual, "")
        except UpdateError as exc:
            self._checked.emit(None, manual, str(exc))
        except Exception as exc:
            logger.exception("检查更新时发生未预期异常")
            self._checked.emit(None, manual, f"检查更新失败：{exc}")

    def _run_download(self, outcome: UpdateOutcome) -> None:
        cancel = self._cancel or threading.Event()
        try:
            path = self._service.download(
                outcome,
                progress=lambda done, total: self._progress.emit(done, total),
                cancel=cancel,
            )
            self._downloaded.emit(outcome, str(path))
        except UpdateCancelledError:
            self._download_cancelled.emit()
        except UpdateError as exc:
            self._download_failed.emit(str(exc))
        except Exception as exc:
            logger.exception("下载安装包时发生未预期异常")
            self._download_failed.emit(f"下载失败：{exc}")

    # ------------------------------------------------------------------ 主线程槽

    def _on_startup_check(self) -> None:
        pending = self._service.pending_outcome()
        if pending is not None and self._should_present(pending, manual=False):
            # 已下载的包优先：不重复走网络；用户选择跳过后仍会继续联网检查
            self._present(pending, manual=False)
            return
        self.check(manual=False)

    def _on_periodic_check(self) -> None:
        if self._dialog is not None and self._dialog.isVisible():
            return
        self._on_startup_check()

    def _on_checked(self, outcome, manual: bool, error: str) -> None:
        self._checking = False
        if error:
            if manual:
                QMessageBox.warning(self._window, "检查更新", error)
            else:
                logger.warning("自动检查更新失败：%s", error)
            return
        if outcome is None:
            if manual:
                QMessageBox.information(
                    self._window, "检查更新", "服务端还没有发布可用的版本。"
                )
            return
        if not outcome.update_available and not outcome.pending:
            if manual:
                reason = (
                    "发现新版本，但当前平台没有匹配的安装包。"
                    if outcome.reason == "no_package"
                    else f"当前已是最新版本（{__version__}）。"
                )
                QMessageBox.information(self._window, "检查更新", reason)
            return
        self._present(outcome, manual=manual)

    def _should_present(self, outcome: UpdateOutcome, *, manual: bool) -> bool:
        if manual:
            return True
        if outcome.skipped and not outcome.mandatory:
            logger.debug("用户已跳过版本 %s，自动检查不再提示", outcome.version)
            return False
        return outcome.mandatory or outcome.version_code not in self._prompted

    def _present(self, outcome: UpdateOutcome, *, manual: bool) -> None:
        if not self._should_present(outcome, manual=manual):
            return
        if not manual:
            self._service.mark_prompted(outcome)
        self._prompted.add(outcome.version_code)
        self._show_dialog(outcome)

    def _show_dialog(self, outcome: UpdateOutcome) -> None:
        if self._dialog is not None:
            if self._downloading:
                self._dialog.raise_()
                self._dialog.activateWindow()
                return
            self._dialog.set_outcome(outcome)
            self._dialog.show()
            self._dialog.raise_()
            self._dialog.activateWindow()
            return
        dialog = UpdateDialog(self._window, outcome)
        dialog.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose, True)
        dialog.install_requested.connect(self._on_install_requested)
        dialog.skip_requested.connect(self._on_skip_requested)
        dialog.cancel_requested.connect(self._cancel_download)
        dialog.open_download_requested.connect(self._on_open_download_requested)
        dialog.open_folder_requested.connect(self._on_open_folder_requested)
        dialog.destroyed.connect(self._on_dialog_destroyed)
        self._dialog = dialog
        dialog.show()

    def _on_dialog_destroyed(self, *_args) -> None:
        self._dialog = None

    # ------------------------------------------------------------------ 对话框动作

    def _on_install_requested(self) -> None:
        if self._dialog is not None and self._dialog.outcome is not None:
            self._begin_install(self._dialog.outcome)

    def _on_skip_requested(self) -> None:
        if self._dialog is not None and self._dialog.outcome is not None:
            self._service.skip_version(self._dialog.outcome)

    def _on_open_download_requested(self) -> None:
        if self._dialog is not None and self._dialog.outcome is not None:
            self._open_download_page(self._dialog.outcome)

    def _on_open_folder_requested(self) -> None:
        if self._dialog is not None and self._dialog.folder_path is not None:
            QDesktopServices.openUrl(
                QUrl.fromLocalFile(str(self._dialog.folder_path))
            )

    # ------------------------------------------------------------------ 下载 / 安装

    def _installer_is_waiting(self) -> bool:
        """是否已有更新器在等本进程退出（超过等待窗口视为已放弃）。"""
        if not self._installer_launched:
            return False
        if time.monotonic() - self._installer_launched_at < _INSTALLER_WAIT_WINDOW_SECONDS:
            return True
        logger.info("上一次更新器已超过等待窗口，允许重新发起安装")
        self._installer_launched = False
        return False

    def _begin_install(self, outcome: UpdateOutcome) -> None:
        if self._installer_is_waiting():
            # 已有一个更新器在等本进程退出：再启动一个会并发替换安装目录
            QMessageBox.information(
                self._window, "检查更新", "更新程序已启动，请退出程序完成安装。"
            )
            return
        if self._downloading:
            return
        if outcome.pending_path is not None and outcome.pending_path.is_file():
            self._install_now(outcome, outcome.pending_path)
            return
        if outcome.package is None:
            QMessageBox.warning(self._window, "检查更新", "当前平台没有匹配的安装包。")
            return
        self._downloading = True
        self._cancel = threading.Event()
        if self._dialog is not None:
            self._dialog.set_downloading()
        threading.Thread(
            target=self._run_download,
            args=(outcome,),
            name="update-download",
            daemon=True,
        ).start()

    def _cancel_download(self) -> None:
        if self._cancel is not None:
            self._cancel.set()

    def _on_progress(self, done: int, total: int) -> None:
        if self._dialog is not None:
            self._dialog.set_progress(done, total)

    def _on_downloaded(self, outcome: UpdateOutcome, path: str) -> None:
        self._downloading = False
        if self._dialog is None or not self._dialog.isVisible():
            # 用户已关闭对话框：保留待安装状态，下次再提示
            logger.info("安装包已下载，但更新对话框已关闭：%s", path)
            return
        self._install_now(outcome, Path(path))

    def _on_download_failed(self, message: str) -> None:
        self._downloading = False
        if self._dialog is not None:
            self._dialog.set_download_error(message)

    def _on_download_cancelled(self) -> None:
        self._downloading = False
        if self._dialog is not None:
            self._dialog.set_download_cancelled()

    def _install_now(self, outcome: UpdateOutcome, package_path: Path) -> None:
        try:
            launch = self._service.launch_install(outcome, package_path)
        except UpdateError as exc:
            logger.error("启动更新器失败：%s", exc)
            if self._dialog is not None:
                self._dialog.set_manual(str(exc), package_path)
            return
        if not launch.started:
            logger.info("自动安装不可用：%s", launch.reason)
            if self._dialog is not None:
                self._dialog.set_manual(launch.reason, launch.package_path)
            return
        self._installer_launched = True
        self._installer_launched_at = time.monotonic()
        self._launch = launch
        if self._dialog is not None:
            self._dialog.set_starting_updater()
        if launch.heartbeat_path is None:
            # 非 Windows（提权更新器）：没有心跳文件可等，按原逻辑直接退出
            logger.info("更新器已启动，准备退出主程序")
            QTimer.singleShot(600, self._request_exit)
            return
        logger.info("更新器进程已创建，等待启动心跳：%s", launch.heartbeat_path)
        self._heartbeat_deadline = time.monotonic() + _HEARTBEAT_TIMEOUT_SECONDS
        self._heartbeat_timer.start()

    def _poll_updater_heartbeat(self) -> None:
        """轮询更新器心跳：确认新进程真的跑起来了，主程序才退出。"""
        launch = self._launch
        path = launch.heartbeat_path if launch is not None else None
        if path is None:
            self._heartbeat_timer.stop()
            return
        data = _read_heartbeat(path)
        if data is not None:
            pid = data.get("pid")
            if isinstance(pid, int) and not _process_alive(pid):
                self._heartbeat_timer.stop()
                self._on_updater_blocked("更新程序启动后立即退出")
                return
            self._heartbeat_timer.stop()
            logger.info("已确认更新器进程启动（pid=%s），退出主程序", pid)
            if self._dialog is not None:
                self._dialog.set_installing()
            QTimer.singleShot(600, self._request_exit)
            return
        if time.monotonic() >= self._heartbeat_deadline:
            self._heartbeat_timer.stop()
            self._on_updater_blocked(
                f"等待更新程序启动超时（{int(_HEARTBEAT_TIMEOUT_SECONDS)} 秒）"
            )

    def _on_updater_blocked(self, reason: str) -> None:
        """更新器没能确认启动：保持主程序运行，把原因和日志位置告诉用户。

        不把 ``_installer_launched`` 清掉：万一是启动极慢，更新器随后仍会等
        本进程退出；清掉会让用户再点一次“立即更新”，导致两个更新器同时装。
        """
        log_path = self._launch.log_path if self._launch is not None else None
        logger.warning("未能确认更新程序已启动：%s（日志：%s）", reason, log_path)
        if self._dialog is not None:
            self._dialog.set_install_blocked(reason, log_path)

    def _request_exit(self) -> None:
        # 走正常关闭流程：有未保存工程时用户还能取消（更新器会等进程退出，
        # 程序不退出就不会安装，超时自行放弃）。
        closed = self._window.close()
        if closed:
            app = QApplication.instance()
            if app is not None:
                app.quit()
            return
        logger.info("用户取消退出，更新未安装（更新器会等待超时后放弃）")
        if self._dialog is not None:
            self._dialog.set_install_deferred(
                "已取消退出程序，本次更新尚未安装。"
                "更新窗口会继续等待约 10 分钟，期间退出程序仍会自动安装；"
                "继续使用则更新窗口会自行关闭，不会修改任何文件。"
            )

    # ------------------------------------------------------------------ 手工安装

    def _open_download_page(self, outcome: UpdateOutcome) -> None:
        package = outcome.package
        if package is None:
            return
        url = QUrl(self._service.download_url(package))
        logger.info("打开安装包下载地址：%s", url.toString())
        QDesktopServices.openUrl(url)


def _read_heartbeat(path: Path) -> dict | None:
    """读更新器心跳；文件正在写入（JSON 不完整）时返回 ``None``，下轮再读。"""
    try:
        data = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def _process_alive(pid: int) -> bool:
    """进程是否仍存活（打不开句柄视为存活：可能是提权的更新器）。"""
    if pid <= 0:
        return False
    if os.name != "nt":
        try:
            os.kill(pid, 0)
        except OSError:
            return False
        return True
    import ctypes
    from ctypes import wintypes

    process_query_limited_information = 0x1000
    still_active = 259
    error_access_denied = 5
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.OpenProcess.restype = wintypes.HANDLE
    kernel32.OpenProcess.argtypes = (wintypes.DWORD, wintypes.BOOL, wintypes.DWORD)
    kernel32.GetExitCodeProcess.argtypes = (
        wintypes.HANDLE,
        ctypes.POINTER(wintypes.DWORD),
    )
    kernel32.CloseHandle.argtypes = (wintypes.HANDLE,)
    handle = kernel32.OpenProcess(
        process_query_limited_information, False, int(pid)
    )
    if not handle:
        return ctypes.get_last_error() == error_access_denied
    try:
        code = wintypes.DWORD()
        if not kernel32.GetExitCodeProcess(handle, ctypes.byref(code)):
            return True
        return code.value == still_active
    finally:
        kernel32.CloseHandle(handle)


__all__ = ["UpdateController"]
