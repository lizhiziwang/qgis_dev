"""每用户更新状态（原子读写 ``state.json``）。

保存的不是“业务数据”，而是让客户端**不重复打扰用户、也不被回滚包降级**的
判断依据：

- ``lastPublishSeq``：已接受过的最大发布序号。服务端每次 prepare 分配单调
  递增序号；收到更小的序号说明是重放的旧清单，直接拒绝（防回滚 / 防中间人
  拿旧清单重放）。
- ``skippedVersionCode``：用户点过“跳过此版本”的版本；非强制更新不再自动
  弹窗，但手动“检查更新”仍会显示。
- ``lastPromptedVersionCode``：已提示过的版本，用于避免同一版本在一次会话里
  重复弹窗。
- ``pending``：已下载完成、等待安装的包（下次启动可继续提示安装）。

写入采用“同目录临时文件 + ``os.replace``”，避免断电 / 崩溃留下半截 JSON；
读取失败一律退回默认值（更新状态丢了只会多提示一次，不能让启动失败）。
"""

from __future__ import annotations

import contextlib
import json
import os
import tempfile
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from app.infra.logging import get_logger
from app.update import paths as update_paths

logger = get_logger(__name__)

STATE_SCHEMA = 1


# --------------------------------------------------------------------- 数据类


@dataclass
class PendingDownload:
    """已下载完成、等待安装的安装包。"""

    version_code: int = 0
    publish_seq: int = 0
    package_id: int = 0
    file_name: str = ""
    sha256: str = ""
    path: str = ""
    downloaded_at: str = ""
    #: 已验签的清单原文 / 签名 / 公钥 ID（重启后可离线恢复“待安装”提示；
    #: Linux 提权更新器还要用它重新验签，不能只信任任务文件）
    manifest: str = ""
    signature: str = ""
    key_id: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "versionCode": self.version_code,
            "publishSeq": self.publish_seq,
            "packageId": self.package_id,
            "fileName": self.file_name,
            "sha256": self.sha256,
            "path": self.path,
            "downloadedAt": self.downloaded_at,
            "manifest": self.manifest,
            "signature": self.signature,
            "keyId": self.key_id,
        }

    @classmethod
    def from_dict(cls, data: Any) -> PendingDownload | None:
        if not isinstance(data, dict):
            return None
        return cls(
            version_code=_safe_int(data.get("versionCode")),
            publish_seq=_safe_int(data.get("publishSeq")),
            package_id=_safe_int(data.get("packageId")),
            file_name=_safe_str(data.get("fileName")),
            sha256=_safe_str(data.get("sha256")),
            path=_safe_str(data.get("path")),
            downloaded_at=_safe_str(data.get("downloadedAt")),
            manifest=_safe_str(data.get("manifest")),
            signature=_safe_str(data.get("signature")),
            key_id=_safe_str(data.get("keyId")),
        )


@dataclass
class ChannelState:
    """某个发布通道的本地状态。"""

    last_check_at: str = ""
    last_publish_seq: int = 0
    skipped_version_code: int = 0
    last_prompted_version_code: int = 0
    pending: PendingDownload | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "lastCheckAt": self.last_check_at,
            "lastPublishSeq": self.last_publish_seq,
            "skippedVersionCode": self.skipped_version_code,
            "lastPromptedVersionCode": self.last_prompted_version_code,
            "pending": self.pending.to_dict() if self.pending else None,
        }

    @classmethod
    def from_dict(cls, data: Any) -> ChannelState:
        if not isinstance(data, dict):
            return cls()
        return cls(
            last_check_at=_safe_str(data.get("lastCheckAt")),
            last_publish_seq=_safe_int(data.get("lastPublishSeq")),
            skipped_version_code=_safe_int(data.get("skippedVersionCode")),
            last_prompted_version_code=_safe_int(data.get("lastPromptedVersionCode")),
            pending=PendingDownload.from_dict(data.get("pending")),
        )


@dataclass
class UpdateState:
    """整个 ``state.json``。"""

    schema: int = STATE_SCHEMA
    channels: dict[str, ChannelState] = field(default_factory=dict)

    def channel(self, name: str) -> ChannelState:
        """取通道状态（不存在则创建，调用方负责 :meth:`StateStore.save`）。"""
        key = (name or "").strip().lower() or "internal"
        state = self.channels.get(key)
        if state is None:
            state = ChannelState()
            self.channels[key] = state
        return state

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema": self.schema,
            "channels": {name: state.to_dict() for name, state in self.channels.items()},
        }

    @classmethod
    def from_dict(cls, data: Any) -> UpdateState:
        if not isinstance(data, dict):
            return cls()
        raw_channels = data.get("channels")
        channels: dict[str, ChannelState] = {}
        if isinstance(raw_channels, dict):
            for name, value in raw_channels.items():
                if isinstance(name, str):
                    channels[name.lower()] = ChannelState.from_dict(value)
        return cls(schema=_safe_int(data.get("schema")) or STATE_SCHEMA, channels=channels)


# --------------------------------------------------------------------- 存储


class StateStore:
    """``state.json`` 的原子读写。"""

    def __init__(self, path: Path | None = None) -> None:
        self.path = Path(path) if path is not None else update_paths.state_file()

    def load(self) -> UpdateState:
        try:
            raw = self.path.read_text(encoding="utf-8")
        except FileNotFoundError:
            return UpdateState()
        except OSError:
            logger.warning("读取更新状态失败：%s", self.path, exc_info=True)
            return UpdateState()
        try:
            state = UpdateState.from_dict(json.loads(raw))
        except ValueError:
            # 损坏的状态文件只影响“是否提示”，不能阻断更新检查
            logger.warning("更新状态文件损坏，按空状态处理：%s", self.path)
            return UpdateState()
        self._prune_pending(state)
        return state

    def save(self, state: UpdateState) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = json.dumps(state.to_dict(), ensure_ascii=False, indent=2)
        fd, temp_name = tempfile.mkstemp(
            prefix=".state-", suffix=".tmp", dir=str(self.path.parent)
        )
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                handle.write(payload)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temp_name, self.path)
        except OSError:
            logger.warning("写入更新状态失败：%s", self.path, exc_info=True)
            with contextlib.suppress(OSError):
                os.unlink(temp_name)

    def _prune_pending(self, state: UpdateState) -> None:
        """丢弃越界 / 已不存在的待安装记录（防 state.json 被手工篡改成任意路径）。"""
        for name in list(state.channels):
            pending = state.channels[name].pending
            if pending is None:
                continue
            path = Path(pending.path) if pending.path else None
            if (
                path is None
                or not path.is_file()
                or not update_paths.is_under_update_root(path)
            ):
                logger.info("丢弃失效的待安装包记录 channel=%s path=%s", name, pending.path)
                state.channels[name].pending = None


def utc_now_iso() -> str:
    """UTC 时间戳（秒级 ISO 8601，带 ``Z``）。"""
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


# ---------------------------------------------------------------------  helpers


def _safe_int(value: Any) -> int:
    if isinstance(value, bool):
        return 0
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


def _safe_str(value: Any) -> str:
    return value if isinstance(value, str) else ""


__all__ = [
    "ChannelState",
    "PendingDownload",
    "StateStore",
    "UpdateState",
    "utc_now_iso",
]
