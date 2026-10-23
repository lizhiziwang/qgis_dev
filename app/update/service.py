"""更新服务编排（检查 → 下载 → 启动更新器），Qt-free。

UI 层（:mod:`app.ui.update`）只调用这里的同步方法，并把它们放到后台线程里执行；
本模块不碰 Qt，方便单测与命令行排查。

过滤器语义：

- 只有 ``versionCode`` 大于当前版本才叫“有新版本”；回滚包不会自动安装。
- ``mandatory``（或当前版本低于 ``minVersionCode``）只影响**提示频率**：跳过
  按钮不可用、每次启动 / 周期检查都会再弹；**永远不阻断程序使用**。
- 用户选择“跳过此版本”只抑制自动弹窗，手动“检查更新”仍会显示。
"""

from __future__ import annotations

import base64
import shutil
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from app import APP_ID, CHANNEL, VERSION_CODE
from app.infra.logging import get_logger
from app.update import paths as update_paths
from app.update.checker import (
    CheckResult,
    UpdateChecker,
    arch_name,
    os_name,
    runtime_id,
    verify_signed_manifest,
)
from app.update.downloader import ProgressCallback, download_package
from app.update.errors import UpdateConfigError, UpdateError, UpdateInstallError
from app.update.launcher import InstallLaunch, UpdaterLauncher
from app.update.manifest import UpdatePackage, join_url
from app.update.state_store import PendingDownload, StateStore, utc_now_iso

if TYPE_CHECKING:  # pragma: no cover - 仅类型
    from app.env.config import AppConfig

logger = get_logger(__name__)


@dataclass(frozen=True)
class UpdateOutcome:
    """一次检查的最终结论（UI 据此决定弹不弹窗、显示什么按钮）。"""

    result: CheckResult
    update_available: bool
    mandatory: bool
    skipped: bool
    pending: bool
    pending_path: Path | None
    reason: str = ""

    @property
    def version(self) -> str:
        return self.result.manifest.version

    @property
    def version_code(self) -> int:
        return self.result.manifest.version_code

    @property
    def release_notes(self) -> str:
        return self.result.manifest.release_notes

    @property
    def package(self) -> UpdatePackage | None:
        return self.result.package

    @property
    def should_prompt(self) -> bool:
        """自动检查时是否应该弹窗（手动检查由 UI 自行决定）。"""
        if self.pending:
            return not self.skipped or self.mandatory
        return self.update_available and (not self.skipped or self.mandatory)


class UpdateService:
    """更新业务入口。"""

    def __init__(
        self,
        config: AppConfig,
        *,
        state_store: StateStore | None = None,
        current_version_code: int = VERSION_CODE,
        launcher: UpdaterLauncher | None = None,
    ) -> None:
        self._config = config
        self._store = state_store or StateStore()
        self._current_version_code = int(current_version_code)
        self._launcher = launcher or UpdaterLauncher()

    # ------------------------------------------------------------------ 属性

    @property
    def enabled(self) -> bool:
        return bool(self._config.update.enabled)

    @property
    def channel(self) -> str:
        return (self._config.update.channel or CHANNEL).strip().lower()

    @property
    def base_url(self) -> str:
        return (self._config.update.base_url or self._config.services.feedback_url or "").strip()

    @property
    def current_version_code(self) -> int:
        return self._current_version_code

    @property
    def state_store(self) -> StateStore:
        return self._store

    # ------------------------------------------------------------------ 检查

    def check(self, *, manual: bool = False) -> UpdateOutcome | None:
        """检查更新；没有已发布版本返回 ``None``。

        :param manual: 手动检查时不受“跳过此版本”影响（但 ``skipped`` 字段仍如实返回）
        """
        if not self.enabled:
            raise UpdateConfigError("自动更新已在配置中关闭（update.enabled=false）")
        checker = UpdateChecker(
            base_url=self.base_url,
            channel=self.channel,
            timeout=self._config.update.request_timeout_seconds,
            current_version_code=self._current_version_code,
        )
        state = self._store.load()
        channel_state = state.channel(self.channel)
        if (
            channel_state.pending is not None
            and channel_state.pending.version_code <= self._current_version_code
        ):
            # 旧版本留下的待安装记录：升级成功后清掉，避免长期占用磁盘
            channel_state.pending = None
        result = checker.check(last_publish_seq=channel_state.last_publish_seq)

        channel_state.last_check_at = utc_now_iso()
        if result is not None:
            channel_state.last_publish_seq = max(
                channel_state.last_publish_seq, result.manifest.publish_seq
            )
        self._store.save(state)
        if result is None:
            return None

        manifest = result.manifest
        newer = manifest.version_code > self._current_version_code
        mandatory = newer and (
            manifest.mandatory or self._current_version_code < manifest.min_version_code
        )
        skipped = (
            newer
            and not mandatory
            and channel_state.skipped_version_code == manifest.version_code
        )
        pending = newer and _pending_matches(channel_state.pending, result)
        reason = ""
        if result.package is None:
            reason = "no_package"
        elif not newer:
            reason = "not_newer"
        elif skipped:
            reason = "skipped"

        if mandatory and skipped:
            logger.info("强制更新版本 %s 不受“跳过此版本”影响，将再次提示", manifest.version)
        if not manual and reason == "no_package" and newer:
            logger.warning(
                "发现新版本 %s，但没有匹配当前平台的安装包，暂不提示", manifest.version
            )

        return UpdateOutcome(
            result=result,
            update_available=newer and result.package is not None,
            mandatory=mandatory,
            skipped=skipped,
            pending=pending,
            pending_path=Path(channel_state.pending.path) if pending and channel_state.pending else None,
            reason=reason,
        )

    # ------------------------------------------------------------------ 下载

    def download(
        self,
        outcome: UpdateOutcome,
        *,
        progress: ProgressCallback | None = None,
        cancel: threading.Event | None = None,
    ) -> Path:
        """下载 ``outcome`` 对应的包，并记录“待安装”。"""
        package = outcome.package
        if package is None:
            raise UpdateInstallError("清单中没有可安装的包")
        channel_state = self._state_channel()
        pending = channel_state.pending
        if _pending_matches(pending, outcome.result) and pending is not None:
            path = Path(pending.path)
            if path.is_file():
                logger.info("复用已下载的安装包：%s", path)
                if progress is not None:
                    progress(package.size_bytes, package.size_bytes)
                return path

        version_dir = update_paths.downloads_dir() / str(outcome.version_code)
        _cleanup_old_downloads(keep=version_dir)
        path = download_package(
            package=package,
            base_url=self.base_url,
            dest_dir=version_dir,
            progress=progress,
            cancel=cancel,
            timeout=30.0,
        )
        state = self._store.load()
        state.channel(self.channel).pending = PendingDownload(
            version_code=outcome.version_code,
            publish_seq=outcome.result.manifest.publish_seq,
            package_id=package.id,
            file_name=package.file_name,
            sha256=package.sha256,
            path=str(path),
            downloaded_at=utc_now_iso(),
            manifest=_b64(outcome.result.manifest_raw),
            signature=_b64(outcome.result.signature),
            key_id=outcome.result.key_id,
        )
        self._store.save(state)
        logger.info("安装包已就绪，等待安装：%s", path)
        return path

    # ------------------------------------------------------------------ 交互记录

    def mark_prompted(self, outcome: UpdateOutcome) -> None:
        state = self._store.load()
        state.channel(self.channel).last_prompted_version_code = outcome.version_code
        self._store.save(state)

    def skip_version(self, outcome: UpdateOutcome) -> None:
        """用户选择“跳过此版本”（强制更新不写跳过记录）。"""
        if outcome.mandatory:
            logger.info("强制更新：忽略跳过请求 version=%s", outcome.version)
            return
        state = self._store.load()
        state.channel(self.channel).skipped_version_code = outcome.version_code
        self._store.save(state)
        logger.info("用户跳过版本 %s", outcome.version)

    def clear_skip(self) -> None:
        state = self._store.load()
        state.channel(self.channel).skipped_version_code = 0
        self._store.save(state)

    def clear_pending(self) -> None:
        state = self._store.load()
        state.channel(self.channel).pending = None
        self._store.save(state)

    def pending_outcome(self) -> UpdateOutcome | None:
        """把“已下载待安装”恢复成完整结论（离线也能提示安装）。

        清单材料来自本地状态文件，但**仍按签名验证**：状态文件被改过只会导致
        验签失败，不会让客户端安装一个未经签名的包。
        """
        pending = self.pending_install()
        if pending is None:
            return None
        raw = _unb64(pending.manifest)
        signature = _unb64(pending.signature)
        if not raw or not signature:
            return None
        try:
            manifest = verify_signed_manifest(
                raw,
                signature,
                pending.key_id,
                app_id=APP_ID,
                channel=self.channel,
                last_publish_seq=0,
            )
        except UpdateError as exc:
            logger.warning("待安装包清单验签失败，已忽略：%s", exc)
            return None
        package = manifest.select_package(
            runtime_id=runtime_id(), os_name=os_name(), arch=arch_name()
        )
        if package is None or package.id != pending.package_id:
            logger.warning("待安装包与清单不匹配，已忽略")
            return None
        mandatory = manifest.mandatory or self._current_version_code < manifest.min_version_code
        state = self._store.load()
        skipped = (
            not mandatory
            and state.channel(self.channel).skipped_version_code == manifest.version_code
        )
        return UpdateOutcome(
            result=CheckResult(
                manifest=manifest,
                package=package,
                manifest_raw=raw,
                signature=signature,
                key_id=pending.key_id,
            ),
            update_available=True,
            mandatory=mandatory,
            skipped=skipped,
            pending=True,
            pending_path=Path(pending.path),
            reason="pending" if not skipped else "skipped",
        )

    def pending_install(self) -> PendingDownload | None:
        """已下载待安装的包（下次启动继续提示安装）。"""
        state = self._store.load()
        pending = state.channel(self.channel).pending
        if pending is None or not pending.path:
            return None
        if not Path(pending.path).is_file():
            return None
        if pending.version_code <= self._current_version_code:
            # 已经手动装过 / 状态过旧
            return None
        return pending

    # ------------------------------------------------------------------ 安装

    def download_url(self, package: UpdatePackage) -> str:
        """手工下载地址（自动安装不可用时的“打开下载页”）。"""
        return join_url(self.base_url, package.url)

    def can_auto_install(self, package_path: Path) -> bool:
        return self._launcher.can_auto_install(package_path)

    def manual_install_hint(self, package_path: Path) -> str:
        return self._launcher.manual_hint(package_path)

    def launch_install(self, outcome: UpdateOutcome, package_path: Path) -> InstallLaunch:
        # 不在启动更新器时清 pending：更新失败会回滚到旧版本，旧版本仍应能
        # 看到“已下载待安装”；新版本启动后 versionCode 已不小于 pending，
        # pending_install() 会自动忽略它。
        return self._launcher.launch(outcome.result, package_path)

    # ------------------------------------------------------------------ 内部

    def _state_channel(self):
        return self._store.load().channel(self.channel)


def _pending_matches(pending: PendingDownload | None, result: CheckResult) -> bool:
    if pending is None or result.package is None:
        return False
    return (
        pending.version_code == result.manifest.version_code
        and pending.package_id == result.package.id
        and pending.sha256 == result.package.sha256
    )


def _cleanup_old_downloads(*, keep: Path) -> None:
    """删除其它版本的下载目录（只保留本次要用的版本）。"""
    root = update_paths.downloads_dir()
    if not root.is_dir():
        return
    for child in root.iterdir():
        if not child.is_dir() or child.resolve() == keep.resolve():
            continue
        logger.info("清理旧版本安装包：%s", child)
        shutil.rmtree(child, ignore_errors=True)


def _b64(value: bytes) -> str:
    return base64.b64encode(value).decode("ascii") if value else ""


def _unb64(value: str) -> bytes:
    try:
        return base64.b64decode(value, validate=True) if value else b""
    except ValueError:
        return b""


__all__ = ["UpdateOutcome", "UpdateService"]
