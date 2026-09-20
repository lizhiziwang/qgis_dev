"""授权服务器 HTTP 客户端——**只负责登录**（标准库 ``urllib``，无第三方依赖、无 Qt）。

登录时 :class:`app.ui.dialogs.login.LoginDialog` 在后台线程调用
:meth:`AuthClient.login`；本模块是**同步阻塞**的，自己不管线程与事件循环——
这样 ``app/core`` 保持"无 GUI 依赖、可脱离 QGIS 单测"。

接口契约为本项目后端（forestry-api）实测结果，服务器 IP / 路径 / 字段名都在
:mod:`app.core.auth.config` 里配::

    POST http://59.55.120.111:7008/forestry-api/system/unit/unified
    Content-Type: application/json
    {"username": "ayxlyj", "password": "<md5 小写 hex>", "isCode": false}

    # 成功（HTTP 200）
    {"code": 5000, "message": "操作成功", "success": true,
     "data": {"access_token": "...", "token_type": "bearer",
              "refresh_token": "...", "expires_in": 21599, "scope": "..."}}

    # 失败（HTTP 也还是 200）
    {"code": 5001, "message": "密码错误，请检查后重试", "success": false}

**除登录以外的业务服务器访问一律走** :mod:`app.core.util.service_client`
（当前用户信息在 :mod:`app.core.auth.current_user`）——别在本模块再加业务接口。

失败一律抛 :class:`AuthError`，``str(exc)`` 已是从业中文文案，可直接显示在
登录框的错误行上（不需要调用方再翻译）。
"""

from __future__ import annotations

import hashlib
import json
import ssl
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from functools import lru_cache
from typing import TYPE_CHECKING, Any

from app.core.auth._payload import pick, pick_any
from app.core.auth.config import (
    DEFAULT_PASSWORD_ENCODING,
    AuthServerConfig,
    load_auth_server_config,
)
from app.core.auth.errors import AuthError

if TYPE_CHECKING:  # 只在类型注解里用，运行时不必 import（避免 current_user ↔ client 互引）
    from app.core.auth.current_user import CurrentUser

#: 响应里表示成功 / 失败的布尔字段（本项目后端用 ``success``）。**布尔优先于 code**：
#: 后端明确给了 bool 就以它为准，避免各家用 5000 / 0 / 200 表示成功时误判。
_SUCCESS_KEYS: tuple[str, ...] = ("success", "ok")
#: 响应里表示业务码的字段
_CODE_KEYS: tuple[str, ...] = ("code", "status", "statusCode", "errcode", "errCode", "retCode")
#: 响应里错误文案的字段
_MESSAGE_KEYS: tuple[str, ...] = (
    "message",
    "msg",
    "error",
    "errorMsg",
    "errmsg",
    "errMsg",
    "detail",
    "description",
)
#: 成功响应里可能带的过期秒数字段
_EXPIRES_KEYS: tuple[str, ...] = ("expires_in", "expiresIn", "expire_in", "expireIn", "expire", "expires")


@dataclass(frozen=True)
class AuthResult:
    """登录成功的结果（只存在于内存，不落盘）。"""

    username: str
    #: 服务端签发的 token；接口不返 token 时为空串
    token: str = ""
    #: token 有效期（秒）；服务端没给则为 ``None``
    expires_in: int | None = None
    #: 原始响应 JSON（排查问题时看）
    raw: dict[str, Any] = field(default_factory=dict)
    #: 当前用户信息（``currentUserByToken`` 的结果）；没拉到时为 ``None``，
    #: 界面回退显示登录账号。**不阻塞登录**：拉取失败只记 warning。
    user: CurrentUser | None = None

    def expires_at(self, now: float | None = None) -> int | None:
        """token 过期的绝对时间（epoch 秒）；服务端没给有效期时为 ``None``。

        :param now: 基准时间（epoch 秒），默认取当前时间；仅便于测试传入。
        """
        if not self.expires_in:
            return None
        return int(time.time() if now is None else now) + int(self.expires_in)


def md5_hex(text: str) -> str:
    """``text`` 的 md5 32 位小写 hex（与 Java / C# 的 ``MD5`` 结果一致）。"""
    return hashlib.md5(text.encode("utf-8")).hexdigest()


def encode_password(password: str, encoding: str = DEFAULT_PASSWORD_ENCODING) -> str:
    """按配置把密码转成提交值（``md5`` 小写 hex / ``plain`` 明文）。"""
    if encoding.strip().lower() == "plain":
        return password
    return md5_hex(password)


@lru_cache(maxsize=2)
def _ssl_context(verify: bool) -> ssl.SSLContext:
    """按 ``verify`` 缓存 SSL 上下文；``verify=False`` 时跳过证书校验。"""
    ctx = ssl.create_default_context()
    if not verify:
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
    return ctx


class AuthClient:
    """同步（阻塞）登录客户端；GUI 侧请放到后台线程调用。

    ``config`` 为 ``None`` 时用 :func:`app.core.auth.config.load_auth_server_config`
    读默认值 + 环境变量。测试可注入自定义 :class:`AuthServerConfig`。
    """

    def __init__(self, config: AuthServerConfig | None = None) -> None:
        self._config = config if config is not None else load_auth_server_config()

    @property
    def config(self) -> AuthServerConfig:
        """当前生效的服务器配置（登录框展示 / 日志用）。"""
        return self._config

    def login(self, username: str, password: str) -> AuthResult:
        """提交用户名密码换取 token。

        :raises AuthError: 账号密码为空 / 网络不通 / 超时 / 服务端拒绝 / 响应格式异常。
        """
        username = (username or "").strip()
        if not username or not password:
            raise AuthError("用户名和密码不能为空")

        cfg = self._config
        body = json.dumps(
            {
                **dict(cfg.extra_body),
                cfg.username_field: username,
                cfg.password_field: encode_password(password, cfg.password_encoding),
            },
            ensure_ascii=False,
        ).encode("utf-8")

        request = urllib.request.Request(
            cfg.login_url,
            data=body,
            method="POST",
            headers={
                "Content-Type": "application/json; charset=utf-8",
                "Accept": "application/json",
            },
        )
        return _parse_login_response(self._post(request, cfg), cfg, fallback_username=username)

    # ------------------------------------------------------------ 网络

    @staticmethod
    def _post(request: urllib.request.Request, cfg: AuthServerConfig) -> bytes:
        """发请求并返回响应体；把各类网络异常翻译成 :class:`AuthError`。"""
        try:
            with urllib.request.urlopen(request, timeout=cfg.timeout, context=_ssl_context(cfg.verify_ssl)) as resp:
                raw: bytes = resp.read()
                return raw
        except urllib.error.HTTPError as exc:
            raise AuthError(_http_error_message(exc)) from exc
        except urllib.error.URLError as exc:
            if isinstance(exc.reason, TimeoutError):
                raise AuthError(f"连接授权服务器超时（{cfg.timeout:g} 秒）：{cfg.base_url}") from exc
            raise AuthError(f"无法连接授权服务器 {cfg.base_url}：{exc.reason}") from exc
        except TimeoutError as exc:
            raise AuthError(f"连接授权服务器超时（{cfg.timeout:g} 秒）：{cfg.base_url}") from exc
        except OSError as exc:
            raise AuthError(f"连接授权服务器失败：{exc}") from exc


# ------------------------------------------------------------------ 响应解析


def _http_error_message(exc: urllib.error.HTTPError) -> str:
    """HTTP 非 2xx：优先用响应体里的 message，其次按状态码给兜底文案。"""
    detail = ""
    try:
        raw = exc.read()
        if raw:
            detail = _message_from_payload(json.loads(raw.decode("utf-8")))
    except (UnicodeDecodeError, ValueError, OSError):
        detail = ""
    if detail:
        return detail
    if exc.code in (401, 403):
        return "用户名或密码错误"
    return f"授权服务器返回错误（HTTP {exc.code} {exc.reason}）"


def _parse_login_response(raw: bytes, cfg: AuthServerConfig, *, fallback_username: str) -> AuthResult:
    payload = _load_json_object(raw)
    _check_business_error(payload, cfg.success_codes)

    token = pick_any(payload, cfg.token_fields)
    username = pick(payload, cfg.resp_username_field)
    return AuthResult(
        username=str(username) if username else fallback_username,
        token=str(token) if token else "",
        expires_in=_pick_int(payload, _EXPIRES_KEYS),
        raw=payload,
    )


def _check_business_error(payload: dict[str, Any], success_codes: tuple[int | str, ...]) -> None:
    """业务层失败判定；失败抛 :class:`AuthError`（带服务端 message）。

    容忍两种后端风格：``{"success": false}`` 布尔标志、``{"code": 5001}`` 业务码。
    **布尔存在时以布尔为准**（本项目后端两种都给，且成功码是自定义的 5000）；
    两者都没有（直接返回 token 的接口）按成功处理。
    """
    flags = [flag for flag in (pick(payload, key) for key in _SUCCESS_KEYS) if isinstance(flag, bool)]
    if flags:
        if not all(flags):
            raise AuthError(_message_from_payload(payload) or "登录被拒绝")
        return
    for key in _CODE_KEYS:
        value = pick(payload, key)
        if value is None or isinstance(value, bool):
            continue
        if value not in success_codes:
            raise AuthError(_message_from_payload(payload) or f"登录被拒绝（code={value}）")


def _load_json_object(raw: bytes) -> dict[str, Any]:
    """登录响应体 → JSON 对象；空 / 非 JSON / 顶层不是对象都抛 :class:`AuthError`。"""
    if not raw:
        raise AuthError("授权服务器返回空响应")
    try:
        payload = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, ValueError) as exc:
        raise AuthError("授权服务器返回的不是合法 JSON，请确认接口地址是否正确") from exc
    if not isinstance(payload, dict):
        raise AuthError("授权服务器返回的数据格式异常（顶层不是 JSON 对象）")
    return payload


def _pick_int(payload: dict[str, Any], keys: tuple[str, ...]) -> int | None:
    for key in keys:
        value = pick(payload, key)
        if value is None:
            continue
        try:
            return int(value)
        except (TypeError, ValueError):
            continue
    return None


def _message_from_payload(payload: Any) -> str:
    """从响应 JSON 里取错误文案（顶层不是对象时返回空串，不报错）。"""
    if not isinstance(payload, dict):
        return ""
    for key in _MESSAGE_KEYS:
        value = pick(payload, key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return ""


__all__ = [
    "AuthClient",
    "AuthError",
    "AuthResult",
    "encode_password",
    "md5_hex",
]
