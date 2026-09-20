"""密码保护的统一入口（跨平台），给 :mod:`app.core.auth.saved_logins` 用。

Windows 有 DPAPI（:mod:`app.core.auth._dpapi`）：密钥由系统按当前登录用户托管，
程序里没有密钥，最省心，所以优先用。

Linux / macOS **没有等价 API**（系统钥匙串要走 D-Bus / CLI），本模块提供本地方案：

- 密钥文件 ``~/.config/qgis_dev/.secret_key``（``QGIS_DEV_SECRET_KEY`` 可覆盖），
  32 字节随机数、权限 ``0o600``，首次保存密码时自动生成；
- 实际加密密钥 = ``SHA256(密钥文件内容 ‖ /etc/machine-id ‖ uid)`` —— 把整个配置目录
  拷到别的机器 / 别的用户下**解不开**（machine-id、uid 不同），与 DPAPI 的行为对齐；
- 加密用 HMAC-SHA256 作伪随机流的计数器模式（16 字节随机 nonce）+ **先加密后认证**
  的 16 字节 HMAC 标签；落盘格式 ``magic ‖ nonce ‖ tag ‖ 密文``（调用方再 base64）。

边界（重要，别把它当 DPAPI）：

- 防的是「配置文件被拷走 / 被别的用户读到 / 明文进了备份」；
- **不防**「攻击者已能以同一用户身份执行代码」——那种情况下密钥文件同样可读；
- 想达到系统级强度要接系统钥匙串（Linux Secret Service / macOS Keychain），
  留作后续（入口就在 :func:`protect` / :func:`unprotect`，换掉本地分支即可）。

所有函数失败一律返回 ``None``、不抛异常（与 :mod:`app.core.auth._dpapi` 一致）：
解不开的账号由调用方跳过，绝不影响启动登录。
"""

from __future__ import annotations

import contextlib
import hashlib
import hmac
import os
import secrets
import sys
from pathlib import Path

from app.core.auth import _dpapi
from app.core.auth._user_paths import user_config_dir
from app.infra.logging import get_logger

logger = get_logger(__name__)

#: 密钥文件路径覆盖（测试 / 多实例用）
ENV_KEY_PATH = "QGIS_DEV_SECRET_KEY"

#: 密码保护方式（写进 json 的 ``prot`` 字段，逐条记录，便于跨平台共存）
PROT_DPAPI = "dpapi"
PROT_LOCAL_KEY = "local-key"
PROT_PLAIN = "plain"

_MAGIC = b"QDLK1"  # 格式版本；换算法时改它，老密文自然「解不开 → 跳过」
_KEY_FILENAME = ".secret_key"
_KEY_BYTES = 32
_NONCE_BYTES = 16
_TAG_BYTES = 16
_MACHINE_ID_FILES = ("/etc/machine-id", "/var/lib/dbus/machine-id")


# --------------------------------------------------------------- 路径 / 能力


def key_path() -> Path:
    """返回本地密钥文件路径（``QGIS_DEV_SECRET_KEY`` 可覆盖）。"""
    override = os.environ.get(ENV_KEY_PATH, "").strip()
    if override:
        return Path(override)
    return user_config_dir() / _KEY_FILENAME


def _is_windows() -> bool:
    """是否 Windows（单独提出来便于平台相关代码在测试里替换）。"""
    return sys.platform == "win32"


def protection_name() -> str:
    """当前平台保存密码用的方式。

    - ``"dpapi"``：Windows 系统加密（密钥由系统托管）
    - ``"local-key"``：非 Windows，密钥文件 + 机器 / 用户绑定
    - ``"plain"``：兜底（Windows 上 crypt32 都加载不了时才会出现，仅 base64）
    """
    if _dpapi.available():
        return PROT_DPAPI
    if _is_windows():
        # DPAPI 是 Windows 自带能力，加载失败属异常环境：退回原行为，别自作主张写密钥文件
        return PROT_PLAIN
    return PROT_LOCAL_KEY


# --------------------------------------------------------------- 对外加解密


def protect(data: bytes) -> tuple[str, bytes] | None:
    """加密 ``data``，返回 ``(prot, blob)``；失败返回 ``None``。"""
    if not data:
        return None
    if _dpapi.available():
        blob = _dpapi.protect(data)
        return None if blob is None else (PROT_DPAPI, blob)
    key = _load_key(create=True)
    if key is None:
        return None
    nonce = secrets.token_bytes(_NONCE_BYTES)
    ciphertext = _xor(data, _keystream(key, nonce, len(data)))
    return PROT_LOCAL_KEY, _MAGIC + nonce + _tag(key, nonce, ciphertext) + ciphertext


def unprotect(prot: object, blob: bytes) -> bytes | None:
    """按 ``prot`` 还原 ``blob``；解不开返回 ``None``。

    ``prot`` 为 ``None``（老文件缺字段）或 ``PROT_PLAIN`` 时按明文原样返回，
    由调用方决定怎么解码。
    """
    if prot in (None, PROT_PLAIN):
        return blob
    if prot == PROT_DPAPI:
        return _dpapi.unprotect(blob)
    if prot == PROT_LOCAL_KEY:
        return _unprotect_local(blob)
    return None


def _unprotect_local(blob: bytes) -> bytes | None:
    """还原本地密钥方案的密文；格式不对 / 校验失败 / 换机器了都返回 ``None``。"""
    head = len(_MAGIC) + _NONCE_BYTES + _TAG_BYTES
    if len(blob) <= head or not blob.startswith(_MAGIC):
        return None
    key = _load_key(create=False)
    if key is None:
        return None
    nonce = blob[len(_MAGIC) : len(_MAGIC) + _NONCE_BYTES]
    tag = blob[len(_MAGIC) + _NONCE_BYTES : head]
    ciphertext = blob[head:]
    if not hmac.compare_digest(tag, _tag(key, nonce, ciphertext)):
        return None
    return _xor(ciphertext, _keystream(key, nonce, len(ciphertext)))


# --------------------------------------------------------------- 密钥文件


def _load_key(*, create: bool) -> bytes | None:
    """取派生后的加密密钥；读不到（且在 ``create=False`` 时）返回 ``None``。"""
    raw = _read_key_file()
    if raw is None:
        if not create:
            return None
        raw = _create_key_file()
        if raw is None:
            return None
    return _derive_key(raw)


def _read_key_file() -> bytes | None:
    """读密钥文件；不存在 / 长度不对（被改坏）返回 ``None``（不覆盖，留作人工恢复）。"""
    path = key_path()
    try:
        raw = path.read_bytes()
    except FileNotFoundError:
        return None
    except OSError as exc:
        logger.warning("读取密钥文件失败（%s）：%s", path, exc)
        return None
    if len(raw) != _KEY_BYTES:
        logger.warning("密钥文件长度异常（%s，%d 字节），已忽略", path, len(raw))
        return None
    return raw


def _create_key_file() -> bytes | None:
    """生成密钥文件（``0o600``）；写不了返回 ``None``。"""
    path = key_path()
    raw = secrets.token_bytes(_KEY_BYTES)
    # O_BINARY：Windows 下 os.open 默认文本模式，会把随机字节里的 0x0A 改写成
    # 0x0D 0x0A → 密钥长度变 33，之后永远解不开（Linux 无此标志，取 0）
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_BINARY", 0)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        # O_EXCL：并发下谁先建成谁说了算，输的一方直接复用
        fd = os.open(path, flags, 0o600)
    except FileExistsError:
        return _read_key_file()
    except OSError as exc:
        logger.warning("创建密钥文件失败（%s）：%s", path, exc)
        return None
    try:
        os.write(fd, raw)
    except OSError as exc:
        logger.warning("写入密钥文件失败（%s）：%s", path, exc)
        return None
    finally:
        os.close(fd)
    with contextlib.suppress(OSError):
        os.chmod(path, 0o600)
    return raw


def _derive_key(raw: bytes) -> bytes:
    """密钥文件内容 + 机器标识 + uid → 32 字节密钥。"""
    material = b"\0".join((raw, _machine_id(), str(_uid()).encode("ascii")))
    return hashlib.sha256(material).digest()


def _machine_id() -> bytes:
    """机器标识（麒麟 / 常见 Linux 都有 ``/etc/machine-id``）；取不到用空串。"""
    for name in _MACHINE_ID_FILES:
        try:
            text = Path(name).read_text(encoding="utf-8").strip()
        except (OSError, UnicodeDecodeError):
            continue
        if text:
            return text.encode("utf-8")
    return b""


def _uid() -> int:
    getuid = getattr(os, "getuid", None)
    return int(getuid()) if callable(getuid) else 0


# --------------------------------------------------------------- 分组密码流


def _keystream(key: bytes, nonce: bytes, length: int) -> bytes:
    """HMAC-SHA256 计数器模式伪随机流（与明文等长）。"""
    out = bytearray()
    counter = 0
    while len(out) < length:
        out += hmac.new(key, nonce + counter.to_bytes(4, "big"), hashlib.sha256).digest()
        counter += 1
    return bytes(out[:length])


def _tag(key: bytes, nonce: bytes, ciphertext: bytes) -> bytes:
    """密文认证标签（先加密后认证）。"""
    mac = hmac.new(key, b"tag\0" + nonce + ciphertext, hashlib.sha256)
    return mac.digest()[:_TAG_BYTES]


def _xor(data: bytes, stream: bytes) -> bytes:
    return bytes(a ^ b for a, b in zip(data, stream, strict=True))


__all__ = [
    "ENV_KEY_PATH",
    "PROT_DPAPI",
    "PROT_LOCAL_KEY",
    "PROT_PLAIN",
    "key_path",
    "protect",
    "protection_name",
    "unprotect",
]
