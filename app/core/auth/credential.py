"""30 天有效凭证。

启动时若本地有**未过期 + 签名通过**的凭证，自动跳过登录直接进入主窗口；
否则弹登录对话框，登录成功后立即签发新凭证。

设计目标：

- **免登录体验**：用户每天上班打开就直接进工具，不被弹窗打断。
- **撤销可控**：删掉本地凭证文件即可下次启动重新登录。
- **轻量篡改检测**：HMAC-SHA256 签名，篡改后过期时间会被识别为无效。
  注意：这不是"安全"，因为密钥编译进二进制，懂反编译的人能伪造。
  只是挡住普通用户改时间戳续命。

模块导出：

- :class:`Credential` —— 数据类，含 ``verify_signature`` / ``is_valid`` 等方法
- :func:`load_credential` —— 从本地读 + 验签 + 查过期；无效返回 ``None``
- :func:`save_credential` —— 写凭证到用户配置目录
- :func:`clear_credential` —— 删本地凭证（主动登出 / 测试用）
- :func:`user_credential_path` —— 跨平台凭证路径
- :data:`VALIDITY_SECONDS` —— 30 天的秒数（其它模块可读不可改）
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

# --------------------------------------------------------------- 常量

# 30 天 = 30 * 24 * 3600 秒
VALIDITY_SECONDS: int = 30 * 24 * 3600

# HMAC 签名密钥（编译进二进制，仅用于篡改检测，不是真正的安全认证）。
# 生产部署时请改成一个随机长串 + 跟随版本号/构建号一起升级。
_SECRET: bytes = b"qgis_dev::auth::v1::please-change-in-prod"

# 凭证文件名
_CREDENTIAL_FILENAME: str = "credential.json"

# 用户级配置目录里的子目录名
_APP_DIR_NAME: str = "qgis_dev"


# --------------------------------------------------------------- 数据类


@dataclass(frozen=True)
class Credential:
    """30 天凭证。"""

    username: str
    issued_at: int            # unix timestamp (seconds)
    expires_at: int           # unix timestamp (seconds)
    signature: str            # hex(HMAC-SHA256)

    # ---------------------------------------------------------- 工厂

    @classmethod
    def issue(
        cls,
        username: str,
        *,
        validity_seconds: int = VALIDITY_SECONDS,
        now: Optional[int] = None,
    ) -> "Credential":
        """签发新凭证，默认 30 天有效。"""
        username = (username or "").strip()
        if not username:
            raise ValueError("username must be non-empty")
        iat = int(now if now is not None else time.time())
        exp = iat + int(validity_seconds)
        sig = _sign(username, iat, exp)
        return cls(username=username, issued_at=iat, expires_at=exp, signature=sig)

    @classmethod
    def from_dict(cls, d: dict) -> "Credential":
        """从 dict 还原（读 JSON 用）。"""
        return cls(
            username=str(d["username"]),
            issued_at=int(d["issued_at"]),
            expires_at=int(d["expires_at"]),
            signature=str(d["signature"]),
        )

    # ---------------------------------------------------------- 序列化

    def to_dict(self) -> dict:
        return {
            "username": self.username,
            "issued_at": self.issued_at,
            "expires_at": self.expires_at,
            "signature": self.signature,
        }

    def to_json(self, *, indent: int = 2) -> str:
        return json.dumps(self.to_dict(), indent=indent, ensure_ascii=False)

    # ---------------------------------------------------------- 校验

    def verify_signature(self) -> bool:
        """HMAC 签名校验（防篡改）。"""
        expected = _sign(self.username, self.issued_at, self.expires_at)
        return hmac.compare_digest(expected, self.signature)

    def is_valid(self, *, now: Optional[int] = None) -> bool:
        """是否在有效期内。"""
        cur = int(now if now is not None else time.time())
        return cur < self.expires_at

    def remaining_seconds(self, *, now: Optional[int] = None) -> int:
        """剩余有效秒数；负数表示已过期。"""
        cur = int(now if now is not None else time.time())
        return self.expires_at - cur

    # ---------------------------------------------------------- 展示

    def __str__(self) -> str:
        return (
            f"Credential(username={self.username!r}, "
            f"expires_in={self.remaining_seconds()}s)"
        )


# --------------------------------------------------------------- 签名


def _sign(username: str, issued_at: int, expires_at: int) -> str:
    """计算 HMAC-SHA256（hex 字符串）。"""
    msg = f"{username}|{issued_at}|{expires_at}".encode("utf-8")
    return hmac.new(_SECRET, msg, hashlib.sha256).hexdigest()


# --------------------------------------------------------------- 路径


def user_credential_path() -> Path:
    """返回凭证文件路径（跨平台）。

    - Windows: ``%APPDATA%\\qgis_dev\\credential.json``
    - macOS: ``~/Library/Application Support/qgis_dev/credential.json``
    - Linux: ``$XDG_CONFIG_HOME/qgis_dev/credential.json`` 或 ``~/.config/qgis_dev/credential.json``
    """
    if sys.platform == "win32":
        base = Path(os.environ.get("APPDATA") or (Path.home() / "AppData" / "Roaming"))
    elif sys.platform == "darwin":
        base = Path.home() / "Library" / "Application Support"
    else:
        base = Path(os.environ.get("XDG_CONFIG_HOME") or (Path.home() / ".config"))
    return base / _APP_DIR_NAME / _CREDENTIAL_FILENAME


# --------------------------------------------------------------- 加载 / 保存


def load_credential() -> Optional[Credential]:
    """加载本地凭证并校验；返回 ``None`` 表示无有效凭证（需要登录）。

    任一步失败都返回 ``None``，不抛异常——调用方据此决定要不要弹登录。

    校验顺序：

    1. 文件是否存在
    2. JSON 解析是否成功 + 字段齐全
    3. HMAC 签名是否通过（防篡改）
    4. 是否在有效期内
    """
    path = user_credential_path()
    if not path.exists():
        return None
    try:
        raw = path.read_text(encoding="utf-8")
        data = json.loads(raw)
        cred = Credential.from_dict(data)
    except Exception:  # noqa: BLE001
        return None
    if not cred.verify_signature():
        return None
    if not cred.is_valid():
        return None
    return cred


def save_credential(cred: Credential) -> Path:
    """写入凭证到用户配置目录。返回写入路径。

    自动创建父目录，并把文件权限收紧为 ``0o600``（仅 owner 可读写）。
    Windows 下 ``chmod`` 仅设置只读标志，会被忽略——这不是问题，
    因为 Windows 上同用户账号下的其它进程本来就共享权限。
    """
    path = user_credential_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(cred.to_json(), encoding="utf-8")
    try:
        os.chmod(path, 0o600)
    except Exception:  # noqa: BLE001
        pass
    return path


def clear_credential() -> bool:
    """删除本地凭证。返回是否真的删了文件。

    主要用于：

    - "登出"按钮（当前 UI 未实现，预留）
    - 测试 / 调试时强制重新登录
    """
    path = user_credential_path()
    if path.exists():
        try:
            path.unlink()
            return True
        except Exception:  # noqa: BLE001
            return False
    return False


__all__ = [
    "VALIDITY_SECONDS",
    "Credential",
    "clear_credential",
    "load_credential",
    "save_credential",
    "user_credential_path",
]
