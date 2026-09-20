"""记住的登录账号（用户名 + 密码）。

启动登录**成功之后**由 :class:`app.ui.dialogs.login.LoginDialog` 写入；下次启动
点击用户名输入框时，界面上会气泡列出已保存的账号，选中即**同时回填用户名与密码**
（气泡 UI 见 :class:`app.ui.dialogs.saved_login_popup.SavedLoginPopup`）。

存储位置（跨平台，与用户级设置同目录）：

- Windows: ``%APPDATA%\\qgis_dev\\saved_logins.json``
- macOS: ``~/Library/Application Support/qgis_dev/saved_logins.json``
- Linux: ``$XDG_CONFIG_HOME/qgis_dev/saved_logins.json``（默认 ``~/.config/...``）

密码怎么存的（**先看清风险再决定要不要开**）：

- Windows：用 **DPAPI**（:mod:`app.core.auth._dpapi`）加密，密钥由系统按当前
  Windows 登录用户托管 → 换个 Windows 用户 / 把文件拷到别的机器都解不开。
  文件里存 base64 密文，``"prot": "dpapi"``。
- Linux / macOS（麒麟等）：系统没有等价的 DPAPI，用 :mod:`app.core.auth._secret`
  的**本地方案** —— ``~/.config/qgis_dev/.secret_key``（``0o600``）里的随机密钥
  再揉上 ``/etc/machine-id`` 与 uid 派生，同样能做到「拷到别的机器 / 别的用户
  解不开」；``"prot": "local-key"``。
  ⚠️ 但它**不防**「攻击者已能以同一用户身份执行代码」（那时密钥文件也读得到）；
  要系统级强度得接系统钥匙串，见 ``app/auth.md`` §6。
- 兜底：上面两条都不可用（如 Windows 上 crypt32 加载失败）→ base64（**只是编码，
  不是加密**），``"prot": "plain"``。不接受就关掉下面的开关。

开关（环境变量）：

- ``QGIS_DEV_REMEMBER_PASSWORDS=0`` —— 不再保存新账号（**读取仍可用**，方便
  手工清理已保存的条目）。
- ``QGIS_DEV_SAVED_LOGINS`` —— 覆盖存储文件路径（测试 / 多环境隔离用）。
- ``QGIS_DEV_SECRET_KEY`` —— 覆盖本地密钥文件路径（非 Windows 平台）。

其它约定：

- 文件名 / 目录名跟随旧凭证约定（``qgis_dev``，见 :mod:`app.core.auth._user_paths`），
  从旧版本升级不会多出一份目录。
- 文件权限尽量收紧到 ``0o600``；Windows 下 ``chmod`` 只影响只读位，实际保护
  由 DPAPI 提供。
- ``prot`` 字段是**逐条**记录的：同一个文件里可以混合 Windows / Linux 写入的条目，
  换平台后解不开的那几条会被跳过（不会报错）。
- 任何读取失败（文件损坏 / 字段类型不对 / 密码解不开）都**只跳过该条**，不抛异常、
  不阻塞启动登录。
"""

from __future__ import annotations

import base64
import contextlib
import json
import os
import time
from dataclasses import dataclass
from pathlib import Path

from app.core.auth import _secret
from app.core.auth._user_paths import user_config_dir
from app.infra.logging import get_logger

logger = get_logger(__name__)

#: 最多记住几个账号（超出丢最旧的）
MAX_SAVED_LOGINS: int = 8

#: 存储文件结构版本
SCHEMA_VERSION: int = 1

#: 关闭保存的环境变量（``0`` / ``false`` / ``no`` 视为关闭）
ENV_ENABLED: str = "QGIS_DEV_REMEMBER_PASSWORDS"

#: 覆盖存储路径的环境变量
ENV_PATH: str = "QGIS_DEV_SAVED_LOGINS"

#: 文件名（放在用户级配置目录下）
_FILENAME: str = "saved_logins.json"

#: 保护方式标识（与 :mod:`app.core.auth._secret` 保持一致）
PROT_DPAPI: str = _secret.PROT_DPAPI
PROT_LOCAL_KEY: str = _secret.PROT_LOCAL_KEY
PROT_PLAIN: str = _secret.PROT_PLAIN

_FALSY = {"0", "false", "no", "off"}


# --------------------------------------------------------------- 数据类


@dataclass(frozen=True)
class SavedLogin:
    """一条已保存的账号。"""

    username: str
    password: str
    #: 最后保存时间（unix 秒）；0 表示未知（老文件）
    updated_at: int = 0

    def masked_password(self) -> str:
        """密码掩码（气泡里显示用，长度不暴露真实位数）。"""
        return "•" * 8


# --------------------------------------------------------------- 路径 / 开关


def saved_logins_path() -> Path:
    """返回存储文件路径（可用 ``QGIS_DEV_SAVED_LOGINS`` 覆盖）。"""
    override = os.environ.get(ENV_PATH, "").strip()
    if override:
        return Path(override)
    return user_config_dir() / _FILENAME


def remember_passwords_enabled() -> bool:
    """是否允许保存新账号（``QGIS_DEV_REMEMBER_PASSWORDS=0`` 关闭）。"""
    return os.environ.get(ENV_ENABLED, "").strip().lower() not in _FALSY


def password_protection() -> str:
    """当前平台保存密码用的方式：``"dpapi"`` / ``"local-key"`` / ``"plain"``。"""
    return _secret.protection_name()


# --------------------------------------------------------------- 读取


def load_saved_logins() -> list[SavedLogin]:
    """读取全部已保存账号，最近保存的在前；任何异常都返回 ``[]``。"""
    path = saved_logins_path()
    try:
        raw = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return []
    except (OSError, UnicodeDecodeError) as exc:
        logger.warning("读取已保存账号失败（%s）：%s", path, exc)
        return []
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        logger.warning("已保存账号文件不是合法 JSON（%s）：%s", path, exc)
        return []
    if not isinstance(data, dict):
        logger.warning("已保存账号文件顶层不是 JSON 对象（%s），已忽略", path)
        return []
    items = data.get("logins")
    if not isinstance(items, list):
        return []
    entries: list[SavedLogin] = []
    seen: set[str] = set()
    for item in items:
        entry = _entry_from_dict(item)
        if entry is None or entry.username in seen:
            continue
        seen.add(entry.username)
        entries.append(entry)
    return entries


def _entry_from_dict(item: object) -> SavedLogin | None:
    """把一条 JSON 记录还原成 :class:`SavedLogin`；无效返回 ``None``。"""
    if not isinstance(item, dict):
        return None
    username = item.get("username")
    if not isinstance(username, str) or not username.strip():
        return None
    password = _decode_password(item.get("password"), item.get("prot"))
    if password is None:
        logger.warning(
            "已保存账号 %s 的密码无法还原（换过机器 / 换过用户 / 密钥文件变了？），已忽略",
            username,
        )
        return None
    updated = item.get("updated_at")
    return SavedLogin(
        username=username.strip(),
        password=password,
        updated_at=int(updated) if isinstance(updated, int | float) else 0,
    )


def _decode_password(value: object, prot: object) -> str | None:
    """按 ``prot`` 还原密码；解不开返回 ``None``。"""
    if not isinstance(value, str):
        return None
    try:
        blob = base64.b64decode(value.encode("ascii"), validate=True)
    except (ValueError, UnicodeEncodeError):
        return None
    plain = _secret.unprotect(prot, blob)
    if plain is None:
        return None
    try:
        return plain.decode("utf-8")
    except UnicodeDecodeError:
        return None


# --------------------------------------------------------------- 写入


def save_login(username: str, password: str, *, now: int | None = None) -> SavedLogin | None:
    """保存（或更新）一条账号，返回保存后的条目。

    - 用户名 / 密码为空 → 返回 ``None`` 且不写文件；
    - :func:`remember_passwords_enabled` 为 ``False`` → 返回 ``None`` 且不写文件；
    - 同名账号 → 覆盖并挪到最前（不产生重复条目）；
    - 超过 :data:`MAX_SAVED_LOGINS` 条 → 丢掉最旧的。
    """
    name = (username or "").strip()
    if not name or not password:
        return None
    if not remember_passwords_enabled():
        logger.info("已关闭保存登录账号（%s=0），本次不保存 %s", ENV_ENABLED, name)
        return None
    stamp = int(time.time() if now is None else now)
    entry = SavedLogin(username=name, password=password, updated_at=stamp)
    others = [e for e in load_saved_logins() if e.username != name]
    entries = [entry, *others][:MAX_SAVED_LOGINS]
    if not _write_entries(entries):
        return None
    return entry


def remove_login(username: str) -> bool:
    """删除指定账号；返回是否真的删掉了（或文件是否发生了变化）。"""
    name = (username or "").strip()
    current = load_saved_logins()
    kept = [e for e in current if e.username != name]
    if len(kept) == len(current):
        return False
    _write_entries(kept)
    return True


def clear_saved_logins() -> bool:
    """删除全部已保存账号；返回是否真的有过文件。"""
    path = saved_logins_path()
    if not path.exists():
        return False
    try:
        path.unlink()
        return True
    except OSError as exc:
        logger.warning("删除已保存账号文件失败（%s）：%s", path, exc)
        return False


def _write_entries(entries: list[SavedLogin]) -> bool:
    """把条目写进文件（原子替换 + 收紧权限）；成功返回 ``True``。"""
    path = saved_logins_path()
    records: list[dict[str, object]] = []
    for entry in entries:
        record = _entry_to_dict(entry)
        if record is None:
            # 宁可不写，也不退回明文
            logger.warning("账号 %s 的密码加密失败，本次不写文件", entry.username)
            return False
        records.append(record)
    payload = {
        "version": SCHEMA_VERSION,
        "prot": password_protection(),
        "logins": records,
    }
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(path.suffix + ".tmp")
        tmp.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")
        os.replace(tmp, path)
    except OSError as exc:
        logger.warning("保存登录账号失败（%s）：%s", path, exc)
        return False
    with contextlib.suppress(OSError):
        # Windows 上 chmod 只影响只读位（真正保护来自 DPAPI）；Unix 上这才是防线
        os.chmod(path, 0o600)
    return True


def _entry_to_dict(entry: SavedLogin) -> dict[str, object] | None:
    """把一条账号转成 JSON 记录（密码按当前平台能力加密）；加密失败返回 ``None``。"""
    protected = _secret.protect(entry.password.encode("utf-8"))
    if protected is None:
        return None
    prot, blob = protected
    return {
        "username": entry.username,
        "password": base64.b64encode(blob).decode("ascii"),
        "prot": prot,
        "updated_at": entry.updated_at,
    }


__all__ = [
    "ENV_ENABLED",
    "ENV_PATH",
    "MAX_SAVED_LOGINS",
    "PROT_DPAPI",
    "PROT_LOCAL_KEY",
    "PROT_PLAIN",
    "SCHEMA_VERSION",
    "SavedLogin",
    "clear_saved_logins",
    "load_saved_logins",
    "password_protection",
    "remember_passwords_enabled",
    "remove_login",
    "save_login",
    "saved_logins_path",
]
