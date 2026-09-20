"""业务接口请求工具（HTTP 统一入口，只用标准库 ``urllib``）。

**入参** = 接口路径 + 参数 + 请求方式；**服务器地址** 按 ``config/app.yaml``
的 ``env:`` 从 :data:`app.core.auth.config.PROFILES` 取 ``scheme/ip/port``
（业务接口与登录走**同一台网关**，所以共用那份配置——不会出现"登录打到新机、
业务还在老机"的错位）；**请求头** 自动带上登录接口换来的 token::

    unitkey: Bearer <登录返回的 access_token>

token 来自启动登录（bootstrap 成功后已调 :func:`set_session_token`），也可以
逐次用 ``token=`` 显式传或覆盖；``token=""`` 表示这次请求**不带** token。

参数放哪（``params`` 是"业务参数"，按请求方式自动分流）::

    request("/forestry-api/xxx/list", {"pageNum": 1}, "GET")    # → /list?pageNum=1
    request("/forestry-api/xxx/save", {"name": "A"}, "POST")    # → body {"name":"A"}

    ┌───────────────┬────────────────────────────────────────┐
    │ GET / HEAD    │ params 拼进 query string               │
    │ DELETE / OPT  │ params 拼进 query string               │
    │ POST/PUT/PATCH│ params 作 JSON body                    │
    └───────────────┴────────────────────────────────────────┘

query 里值为 ``None`` 的键丢弃、布尔型写成小写 ``true`` / ``false``（Java 后端认识），
列表值按同名重复拼；要精确控制就显式给 ``query=`` / ``json_body=``（给了以后
``params`` 不再参与），表单提交用 ``data={"a": 1}``，原始字节用 ``data=b"..."``。

用法::

    from app.core.util.service_client import ServiceError, request

    try:
        resp = request("/forestry-api/system/unit/list", {"pageNum": 1}, "POST")
    except ServiceError as exc:
        logger.warning("取用户列表失败：%s", exc)
    else:
        logger.info("共 %s 条", len(resp.data["records"]))

命令行走一次（调接口 / 排障用，token 用 ``--token`` 给）::

    python -m app.core.util.service_client GET /forestry-api/system/unit/currentUserByToken \
        --token <token> --token-in-query
    python -m app.core.util.service_client POST /forestry-api/xxx/list '{"pageNum":1}'

（Windows Git-Bash 下 ``/xxx`` 会被 MSYS 当成盘符路径改写，加 ``MSYS_NO_PATHCONV=1``
前缀或写成 ``//forestry-api/...``。）

注意：

- **同步阻塞**，GUI 里调要自己放后台线程（项目约定 ``threading.Thread``）。
- 默认 **HTTP 非 2xx / 业务失败（``success: false``，如 ``code: 401``）都抛**
  :class:`ServiceError`；不想抛就 ``raise_on_error=False``，然后看
  :attr:`ServiceResponse.ok` / :attr:`ServiceResponse.message`。
- 日志里的 URL 会把 ``access_token`` 之类的 query 值打码（token 不进日志）。
- ``app.core`` 层不引 Qt，本模块同理。
"""

from __future__ import annotations

import json
import logging
import re
import ssl
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from app.core.auth.config import DEFAULT_SUCCESS_CODES, AuthServerConfig, load_auth_server_config
from app.infra.logging import get_logger

logger = get_logger(__name__)

__all__ = [
    "ServiceError",
    "ServiceResponse",
    "clear_session_token",
    "get_session_token",
    "load_service_config",
    "request",
    "service_base_url",
    "set_session_token",
]

#: ``params`` 走 query string 的请求方式（其余方法当 JSON body）
_QUERY_METHODS = ("GET", "HEAD", "DELETE", "OPTIONS")
#: 允许的请求方式（写错直接报错，别到服务端才发现）
_ALLOWED_METHODS = ("GET", "POST", "PUT", "DELETE", "PATCH", "HEAD", "OPTIONS")
#: query 里名字像密钥的参数，日志打码（token 不进日志文件）
_SENSITIVE_QUERY_RE = re.compile(r"(?i)([?&][^=&#]*(?:token|passw|pwd|secret|sign|key)[^=&#]*=)([^&#]*)")

#: 本次会话的登录 token（bootstrap 登录成功后 :func:`set_session_token` 写入）
_session_token: str = ""
#: 按 ``verify_ssl`` 缓存的 SSL 上下文
_ssl_contexts: dict[bool, ssl.SSLContext] = {}


# ============================================================ 会话 token


def set_session_token(token: str | None) -> None:
    """登记本次登录的 token（``Bearer`` 前缀可有可无），此后请求自动带上。

    bootstrap 在登录成功后调一次即可；传 ``None`` / 空串 ＝ 清除。
    """
    global _session_token
    _session_token = (token or "").strip()
    if _session_token:
        logger.debug("业务请求已登记登录 token（长度 %d）", len(_session_token))


def get_session_token() -> str:
    """当前登记的 token（空串 ＝ 还没登录 / 已清除）。"""
    return _session_token


def clear_session_token() -> None:
    """清除登记的 token（登出 / 测试清理用）。"""
    set_session_token(None)


# ============================================================ 配置 / 地址


def load_service_config(env: str | None = None) -> AuthServerConfig:
    """取当前环境的服务器配置（与登录共用 ``PROFILES`` / ``QGIS_DEV_AUTH_*``）。"""
    return load_auth_server_config(env)


def service_base_url(env: str | None = None) -> str:
    """当前环境的业务接口根地址 ``scheme://ip:port``（不含末尾斜杠）。"""
    return load_service_config(env).base_url


# ============================================================ 响应对象


@dataclass(frozen=True)
class ServiceResponse:
    """一次请求的结果（HTTP 层 + 业务层）。

    :attr:`payload` 是解析后的 JSON（dict / list / 标量），解析不了时为 ``None``、
    原始文本看 :attr:`text`；:attr:`ok` 同时要求 HTTP 2xx 与业务成功。
    """

    url: str
    method: str
    status_code: int
    payload: Any = None
    text: str = ""
    elapsed_ms: float = 0.0

    @property
    def data(self) -> Any:
        """业务数据：``payload["data"]``（没有 ``data`` 键时就是整个 payload）。"""
        if isinstance(self.payload, dict) and "data" in self.payload:
            return self.payload["data"]
        return self.payload

    @property
    def code(self) -> Any:
        """业务码（``code`` / ``status``；裸 JSON 或无该字段时为 ``None``）。"""
        if not isinstance(self.payload, dict):
            return None
        return self.payload.get("code") if "code" in self.payload else self.payload.get("status")

    @property
    def success(self) -> bool:
        """业务是否成功（有 ``success`` / ``ok`` 布尔时以它为准，否则看 :attr:`code`）。"""
        return _business_state(self.payload)[0]

    @property
    def message(self) -> str:
        """业务提示文案（``message`` / ``msg`` / ``error``…；没有则空串）。"""
        return _business_state(self.payload)[1]

    @property
    def ok(self) -> bool:
        """HTTP 2xx 且业务成功。"""
        return 200 <= self.status_code < 300 and self.success

    def raise_for_status(self) -> ServiceResponse:
        """业务失败就抛 :class:`ServiceError`，否则返回自己（便于链式调用）。"""
        if not self.ok:
            raise ServiceError(self.message or f"请求失败（HTTP {self.status_code}）", response=self)
        return self

    def __str__(self) -> str:
        return f"{self.method} {_mask_url(self.url)} → HTTP {self.status_code}（{self.elapsed_ms:.0f} ms）"


class ServiceError(RuntimeError):
    """请求失败：网络不通 / 超时 / HTTP 非 2xx / 业务失败 / 参数不合法。

    :attr:`response` 在"服务端有响应"时不为空（网络错误时为 ``None``）。
    """

    def __init__(self, message: str, *, response: ServiceResponse | None = None) -> None:
        super().__init__(message)
        self.response = response
        self.status_code = response.status_code if response is not None else 0
        self.code = response.code if response is not None else None


# ============================================================ 请求入口


def request(
    path: str,
    params: Mapping[str, Any] | None = None,
    method: str = "GET",
    *,
    query: Mapping[str, Any] | None = None,
    json_body: Any = None,
    data: bytes | str | Mapping[str, Any] | None = None,
    headers: Mapping[str, str] | None = None,
    token: str | None = None,
    token_in_query: bool = False,
    config: AuthServerConfig | None = None,
    base_url: str | None = None,
    env: str | None = None,
    timeout: float | None = None,
    verify_ssl: bool | None = None,
    raise_on_error: bool = True,
) -> ServiceResponse:
    """按当前环境配置发一次业务请求并返回 :class:`ServiceResponse`。

    :param path: 接口路径（``/forestry-api/...``，前导斜杠可省）；传完整
        ``http(s)://`` URL 时直接用它、忽略 ``base_url`` / 环境配置
    :param params: 业务参数；GET/HEAD/DELETE/OPTIONS 拼 query，POST/PUT/PATCH 作
        JSON body（显式给了 ``query`` / ``json_body`` 时本参数不参与）。query 里
        值为 ``None`` 的键丢弃、布尔型写成小写 ``true`` / ``false``（Java 后端认识），
        列表值按同名重复拼（``doseq``）
    :param method: 请求方式（``GET`` / ``POST`` / ``PUT`` / ``DELETE`` / ``PATCH`` /
        ``HEAD`` / ``OPTIONS``，大小写不敏感）
    :param query: 显式 query 参数（会与 ``path`` 里自带的 query 合并）
    :param json_body: 显式 JSON body（任意可序列化对象）
    :param data: 表单 / 原始 body：dict 走 ``urlencode``，str / bytes 原样发送
    :param headers: 额外请求头，**同名时覆盖**默认头（含 token 头，便于个别接口换法）
    :param token: 本次请求用的 token；``None`` ＝ 用 :func:`get_session_token`，
        ``""`` ＝ 这次不带 token 头
    :param token_in_query: 同时也把 token（**不带** ``Bearer`` 前缀）放到 query 的
        ``access_token`` 里——服务端只认 query 的接口（如 ``currentUserByToken``）
        必须打开它
    :param base_url: 覆盖根地址（如指向测试机）；``None`` ＝ 按 ``env`` 取配置
    :param config: 直接注入服务器配置（根地址 / 头名 / 超时都看它，不再读环境变量）；
        鉴权包内部复用本工具、或测试想固定配置时用
    :param env: 环境名（``dev`` / ``prod``…）；``None`` ＝ 读 ``config/app.yaml`` 的 ``env:``
    :param timeout: 超时秒数；``None`` ＝ 环境配置里的值
    :param verify_ssl: 是否校验证书；``None`` ＝ 环境配置里的值
    :param raise_on_error: ``True``（默认）时 HTTP 非 2xx / 业务失败直接抛
        :class:`ServiceError`，``False`` 时返回响应由调用方自己判断

    :raises ServiceError: 请求方式 / 参数不合法、网络不通、超时、服务端报错或
        业务失败（``raise_on_error=True`` 时）。
    """
    cfg = config if config is not None else load_service_config(env)
    method = (method or "GET").strip().upper()
    if method not in _ALLOWED_METHODS:
        raise ServiceError(f"不支持的请求方式：{method}（可用：{'/'.join(_ALLOWED_METHODS)}）")

    query_params = dict(_resolve_query(method, params, query) or {})
    if token_in_query:
        query_params[cfg.token_query_field] = _token_for_query(cfg, token)
    url = _build_url(path, cfg, base_url=base_url, query=query_params)
    body, content_type = _build_body(method, params, json_body=json_body, data=data)
    request_headers = _build_headers(cfg, token=token, content_type=content_type, extra=headers)
    http_request = urllib.request.Request(url, data=body, method=method, headers=request_headers)

    started = time.perf_counter()
    status_code, raw = _send(http_request, cfg, timeout=timeout, verify_ssl=verify_ssl)
    elapsed_ms = (time.perf_counter() - started) * 1000

    payload, text = _decode_body(raw)
    response = ServiceResponse(
        url=url,
        method=method,
        status_code=status_code,
        payload=payload,
        text=text,
        elapsed_ms=elapsed_ms,
    )
    logger.log(
        logging.DEBUG if response.ok else logging.WARNING,
        "业务请求 %s（body %d 字节，带 token：%s）",
        response,
        len(body) if body else 0,
        "是" if cfg.token_header in request_headers else "否",
    )
    if raise_on_error:
        response.raise_for_status()
    return response


# ============================================================ 组包辅助


def _effective_token(token: str | None) -> str:
    """本次请求实际要用的 token（``None`` → 会话 token；其余（含空串）原样用）。"""
    return get_session_token() if token is None else (token or "").strip()


def _token_for_query(cfg: AuthServerConfig, token: str | None) -> str:
    """query 里要用的 token 值（去掉 ``Bearer`` 前缀；没有 token 则报错）。"""
    effective = _effective_token(token)
    if not effective:
        raise ServiceError(f"token_in_query=True 但没有可用 token（无法拼 {cfg.token_query_field}）")
    prefix = cfg.token_prefix
    if prefix and effective.startswith(prefix):
        return effective[len(prefix) :].strip()
    return effective


def _resolve_query(
    method: str,
    params: Mapping[str, Any] | None,
    query: Mapping[str, Any] | None,
) -> Mapping[str, Any] | None:
    """决定 query：显式 ``query`` 优先，其次 GET 类方法的 ``params``。"""
    if query is not None:
        return query
    return params if method in _QUERY_METHODS else None


def _build_url(
    path: str,
    cfg: AuthServerConfig,
    *,
    base_url: str | None,
    query: Mapping[str, Any] | None,
) -> str:
    """拼完整 URL（前导斜杠可省；``path`` 是完整 URL 时原样用）。"""
    text = (path or "").strip()
    if not text:
        raise ServiceError("接口路径不能为空")
    if text.startswith(("http://", "https://")):
        url = text
    else:
        root = (base_url or cfg.base_url).rstrip("/")
        # 归一化重复斜杠（Git-Bash 下 ``//path`` 是常用写法，别真发出去）
        url = f"{root}/{text.lstrip('/')}"
    encoded = _encode_query(query) if query else ""
    if not encoded:
        return url
    return f"{url}{'&' if '?' in url else '?'}{encoded}"


def _encode_query(query: Mapping[str, Any]) -> str:
    """拼 query string：``None`` 丢掉、布尔转小写 ``true`` / ``false``。

    后端（Java）只认小写布尔；``None`` 通常是"这个条件不传"，拼进去会变成
    字符串 ``None`` 反而当成了过滤条件。
    """
    pairs: list[tuple[str, Any]] = []
    for key, value in query.items():
        if value is None:
            continue
        if isinstance(value, (list, tuple, set)):
            items = [_query_scalar(item) for item in value]
            pairs.extend((key, item) for item in items if item is not None)
            continue
        scalar = _query_scalar(value)
        if scalar is not None:
            pairs.append((key, scalar))
    return urllib.parse.urlencode(pairs, doseq=True)


def _query_scalar(value: Any) -> Any:
    """单值归一化（布尔 → 小写字符串，``None`` → 跳过）。"""
    if value is None:
        return None
    if isinstance(value, bool):
        return "true" if value else "false"
    return value


def _build_body(
    method: str,
    params: Mapping[str, Any] | None,
    *,
    json_body: Any,
    data: bytes | str | Mapping[str, Any] | None,
) -> tuple[bytes | None, str]:
    """构造 body 与对应 ``Content-Type``（没 body 时返回 ``(None, "")``）。"""
    if json_body is not None and data is not None:
        raise ServiceError("json_body 与 data 不能同时给（前者发 JSON，后者发表单/原始字节）")

    source: Any = json_body
    if source is None and data is None and method not in _QUERY_METHODS:
        source = params
    if data is not None:
        if isinstance(data, Mapping):
            return _encode_query(data).encode("utf-8"), "application/x-www-form-urlencoded"
        return (data.encode("utf-8") if isinstance(data, str) else bytes(data)), ""
    if source is None:
        return None, ""
    try:
        return json.dumps(source, ensure_ascii=False, default=str).encode("utf-8"), "application/json; charset=utf-8"
    except (TypeError, ValueError) as exc:  # default=str 兜底后基本不会到
        raise ServiceError(f"请求参数无法序列化成 JSON：{exc}") from exc


def _build_headers(
    cfg: AuthServerConfig,
    *,
    token: str | None,
    content_type: str,
    extra: Mapping[str, str] | None,
) -> dict[str, str]:
    """默认头（Accept + Content-Type + 登录 token）叠加调用方头（同名覆盖）。"""
    headers: dict[str, str] = {"Accept": "application/json"}
    if content_type:
        headers["Content-Type"] = content_type
    effective = _effective_token(token)
    if effective:
        headers[cfg.token_header] = cfg.token_header_value(effective)
    elif token is None:
        logger.debug("未登记登录 token，本次请求不带 %s 头", cfg.token_header)
    if extra:
        headers.update(extra)
    return headers


# ============================================================ 网络


def _send(
    http_request: urllib.request.Request,
    cfg: AuthServerConfig,
    *,
    timeout: float | None,
    verify_ssl: bool | None,
) -> tuple[int, bytes]:
    """发请求，返回 ``(状态码, 响应体)``；网络异常翻成中文 :class:`ServiceError`。

    HTTP 非 2xx 也算"拿到响应"（回状态码+错误体，由 :meth:`ServiceResponse.raise_for_status`
    在用完成文后再决定抛不抛）。
    """
    seconds = cfg.timeout if timeout is None else timeout
    verify = cfg.verify_ssl if verify_ssl is None else verify_ssl
    try:
        with urllib.request.urlopen(
            http_request,
            timeout=seconds,
            context=_ssl_context(verify),
        ) as resp:
            return int(resp.status), bytes(resp.read())
    except urllib.error.HTTPError as exc:
        return int(exc.code), _read_error_body(exc)
    except urllib.error.URLError as exc:
        if isinstance(exc.reason, TimeoutError):
            raise ServiceError(f"请求超时（{seconds:g} 秒）：{_mask_url(http_request.full_url)}") from exc
        raise ServiceError(f"无法连接服务器：{exc.reason}") from exc
    except TimeoutError as exc:
        raise ServiceError(f"请求超时（{seconds:g} 秒）：{_mask_url(http_request.full_url)}") from exc
    except OSError as exc:
        raise ServiceError(f"请求失败：{exc}") from exc


def _ssl_context(verify: bool) -> ssl.SSLContext:
    """按 ``verify`` 缓存 SSL 上下文；``verify=False`` 跳过证书校验。"""
    ctx = _ssl_contexts.get(verify)
    if ctx is None:
        ctx = ssl.create_default_context()
        if not verify:
            ctx.check_hostname = False
            ctx.verify_mode = ssl.CERT_NONE
        _ssl_contexts[verify] = ctx
    return ctx


def _read_error_body(exc: urllib.error.HTTPError) -> bytes:
    """读 HTTP 错误响应的 body（读不到就给空）。"""
    try:
        return bytes(exc.read())
    except Exception:  # noqa: BLE001 - 错误响应读不出来不该盖掉原始错误
        return b""


# ============================================================ 响应解析


def _decode_body(raw: bytes) -> tuple[Any, str]:
    """解码响应体：返回 ``(解析后的 JSON 或 None, 文本)``。"""
    text = raw.decode("utf-8", errors="replace") if raw else ""
    if not text.strip():
        return None, text
    try:
        return json.loads(text), text
    except json.JSONDecodeError:
        return None, text


def _business_state(payload: Any) -> tuple[bool, str]:
    """判断业务成功 + 取提示文案（``success`` 布尔优先，其次看 ``code``）。"""
    if not isinstance(payload, dict):
        return True, ""  # 裸 JSON（数组 / 标量）：没有业务码可判，按成功
    message = _first_text(payload, ("message", "msg", "errorMsg", "error", "detail", "info"))
    flag = payload.get("success")
    if isinstance(flag, bool):
        return flag, message
    flag = payload.get("ok")
    if isinstance(flag, bool):
        return flag, message
    code = payload.get("code") if "code" in payload else payload.get("status")
    if code is None:
        return True, message
    return code in DEFAULT_SUCCESS_CODES, message


def _first_text(payload: Mapping[str, Any], keys: tuple[str, ...]) -> str:
    """按顺序找第一个非空字符串字段。"""
    for key in keys:
        value = payload.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return ""


def _mask_url(url: str) -> str:
    """日志用：把 query 里像 token / 密码 / 签名 的值打码。"""
    return _SENSITIVE_QUERY_RE.sub(r"\g<1>***", url or "")


# ============================================================ 命令行调试


def _main(argv: list[str] | None = None) -> int:
    """``python -m app.core.util.service_client <METHOD> <PATH> [JSON参数]``。"""
    import argparse

    parser = argparse.ArgumentParser(description="按环境配置发一次业务请求（调试用）")
    parser.add_argument("method", help="请求方式：GET / POST / PUT / DELETE…")
    parser.add_argument("path", help="接口路径，如 /forestry-api/system/unit/currentUserByToken")
    parser.add_argument("params", nargs="?", default=None, help="JSON 参数（GET 拼 query，其它方法作 body）")
    parser.add_argument("--env", default=None, help="环境名（缺省读 config/app.yaml 的 env:）")
    parser.add_argument("--token", default=None, help="登录 token（缺省用已登记的会话 token）")
    parser.add_argument(
        "--token-in-query",
        action="store_true",
        help="把 token 也放到 query 的 access_token（服务端只认 query 的接口要加）",
    )
    parser.add_argument("--base-url", default=None, help="覆盖根地址，如 http://127.0.0.1:7008")
    parser.add_argument("--query", default=None, help="显式 JSON query 参数")
    parser.add_argument("--json", dest="json_body", default=None, help="显式 JSON body")
    parser.add_argument("--timeout", type=float, default=None, help="超时秒数")
    parser.add_argument("--no-raise", action="store_true", help="失败也打印响应体、不抛异常")
    args = parser.parse_args(argv)

    def _json(text: str | None) -> Any:
        if text is None:
            return None
        try:
            return json.loads(text)
        except json.JSONDecodeError as exc:
            parser.error(f"参数不是合法 JSON：{exc}")

    try:
        response = request(
            args.path,
            _json(args.params),
            args.method,
            query=_json(args.query),
            json_body=_json(args.json_body),
            token=args.token,
            token_in_query=args.token_in_query,
            base_url=args.base_url,
            env=args.env,
            timeout=args.timeout,
            raise_on_error=not args.no_raise,
        )
    except ServiceError as exc:
        print(f"[失败] {exc}")
        return 1
    print(f"[{response.status_code}] {response}")
    if response.message:
        print(f"message: {response.message}")
    print(json.dumps(response.payload, ensure_ascii=False, indent=2) if response.payload is not None else response.text)
    return 0 if response.ok else 1


if __name__ == "__main__":
    raise SystemExit(_main())
