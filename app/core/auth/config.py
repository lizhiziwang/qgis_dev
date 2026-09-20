"""授权服务器配置（服务器地址 + 登录接口 + 请求/响应契约）。

启动登录**不再本地比对账号**，而是把用户名/密码提交到授权服务器换取 token
（见 :class:`app.core.auth.client.AuthClient`）。**服务器地址按环境区分**，全部
集中在本文件：

- **环境名**取 ``config/app.yaml`` 的 ``env:``（``dev`` / ``prod`` / …），见
  :func:`resolve_env`；
- **每个环境用哪个地址 / 接口**写在本文件的 :data:`PROFILES`——加一个环境（如
  ``uat``）= 在 :data:`PROFILES` 里加一段 + 把 ``app.yaml`` 的 ``env:`` 换成同名，
  业务代码一行都不用动；
- 不改代码临时换机 / 部署注入，用下表的 ``QGIS_DEV_AUTH_*`` 环境变量。

覆盖优先级（高 → 低）：

1. ``QGIS_DEV_AUTH_*`` 环境变量（见下表）
2. :data:`PROFILES` 里当前环境（``app.yaml`` 的 ``env:``）那一段
3. 本文件的 ``DEFAULT_*`` 常量（＝ ``dev`` 的值，也是配置读不到时的兜底）

===============================  ==================================================
环境变量                          覆盖项
===============================  ==================================================
``QGIS_DEV_AUTH_BASE_URL``       完整 base（``scheme://host:port``）；给了就忽略
                                 下面的 IP / PORT / SCHEME
``QGIS_DEV_AUTH_IP``             授权服务器 IP / 主机名
``QGIS_DEV_AUTH_PORT``           端口
``QGIS_DEV_AUTH_SCHEME``         ``http`` / ``https``
``QGIS_DEV_AUTH_LOGIN_PATH``     登录接口路径
``QGIS_DEV_AUTH_CURRENT_USER_PATH``  当前用户信息接口路径（进主界面时用）
``QGIS_DEV_AUTH_TOKEN_HEADER``   带 token 的请求头名（默认 ``unitkey``）
``QGIS_DEV_AUTH_TIMEOUT``        超时（秒，浮点）
``QGIS_DEV_AUTH_VERIFY_SSL``     ``https`` 是否校验证书（``1``/``0``、``true``/``false``）
===============================  ==================================================

契约（``curl`` 实测，2026-09）：

- **请求**：``POST {base_url}{login_path}``，``Content-Type: application/json``::

      {"username": "ayxlyj", "password": "<md5 小写 hex>", "isCode": false}

  密码固定提交 **md5 小写 32 位 hex**（``password_encoding="md5"``）；额外固定
  字段 ``isCode: false`` 由 :data:`DEFAULT_EXTRA_BODY` 加进去。

- **成功**（HTTP 200）：``{"code": 5000, "message": "操作成功", "success": true,
  "data": {"access_token": "...", "token_type": "bearer",
  "refresh_token": "...", "expires_in": 21599, "scope": "..."}}``

- **失败**（HTTP 仍是 200）：``{"code": 5001, "message": "密码错误，请检查后重试",
  "success": false}``

  所以**先看 ``success`` 布尔**（本项目后端会明确返回），没有再按 ``code`` 判；
  token 在 ``data.access_token``，过期秒数在 ``data.expires_in``，用户名校验
  响应里没有 → 回退用请求里提交的那个。

- **当前用户信息**（登录成功后进主界面时拉一次）：

      GET {base_url}{current_user_path}?access_token=<token>
      unitkey: Bearer <token>

  实测（2026-09）：**鉴权只看 query 里的 ``access_token``**——请求头放什么服务端
  都不看（垃圾 token + 好头仍返 401）；但按前端约定仍然带上 ``unitkey`` 头，
  而且响应里的 ``data.unitKey``（形如 ``"Bearer <token>"``）正是后续业务接口
  要带的头值。

      成功: {"code": 5000, "message": "操作成功", "success": true,
             "data": {"userInfo": {"userAccount": "ayxlyj", "userName": "测试安义县",
                      "departmentName": "办公室", "organizeName": "南昌市…林业局",
                      "headIcon": "/api/file/..."},
                      "unitKey": "Bearer <token>", "needResetPassword": false}}
      过期: {"code": 401, "message": "登录过期，请重新登录", "success": false}
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any
from urllib.parse import quote

import yaml

from app.infra.fs.paths import Paths
from app.infra.logging import get_logger

logger = get_logger(__name__)

# ============================================================ 环境 → 服务器

#: 环境名取不到时用的环境（也是 ``config/app.yaml`` 缺 ``env:`` 时的兜底）
DEFAULT_ENV: str = "dev"

#: 按环境区分的授权服务器配置：``config/app.yaml`` 的 ``env:`` 决定用哪一段。
#:
#: - key：环境名，要和 ``app.yaml`` 的 ``env:`` 写得**一模一样**（想加 ``test`` /
#:   ``uat`` 直接加一段，再把 ``app.yaml`` 的 ``env:`` 改过去即生效）；
#: - value：只写要覆盖下面 ``DEFAULT_*`` 的字段，可用键 ``scheme`` / ``ip`` /
#:   ``port`` / ``login_path`` / ``current_user_path`` / ``timeout`` / ``verify_ssl``
#:   / ``password_encoding``；
#:   空 dict ＝ 这些字段全走 ``DEFAULT_*``（请求/响应字段契约不分环境，见下方常量）。
#:
#: 注意 ``app.yaml`` 的 ``env:`` 是全局开关（bootstrap 还用 ``prod`` / ``dev`` 决定
#: QGIS 运行时怎么探测，见 :func:`app.bootstrap._build_qgis_runtime`）——切环境前先
#: 确认那个环境下的 QGIS 安装路径也对得上。
#:
#: ``dev`` 故意留空——``DEFAULT_*`` 就是开发环境的值；``prod`` 目前与 ``dev``
#: 同一台服务器，拿到生产地址后只改这一段（改完不用重新看别的地方）。
PROFILES: dict[str, dict[str, Any]] = {
    "dev": {},
    "prod": {
        "scheme": "http",
        "ip": "59.55.120.111",  # TODO 生产环境授权服务器地址（当前先与 dev 相同）
        "port": 7008,
        "login_path": "/forestry-api/system/unit/unified",
    },
}

#: ``config/app.yaml`` 路径的覆盖环境变量（与 bootstrap / project_ops 同一约定）
ENV_CONFIG_PATH: str = "QGIS_DEV_CONFIG"

# ======================================================== 默认值（dev / 兜底）

#: 授权服务器 IP / 主机名（默认 / dev）
DEFAULT_SERVER_IP: str = "59.55.120.111"
#: 授权服务器端口
DEFAULT_SERVER_PORT: int = 7008
#: 协议：``http`` / ``https``
DEFAULT_SERVER_SCHEME: str = "http"
#: 登录接口路径（拼在 ``{scheme}://{ip}:{port}`` 之后）
DEFAULT_LOGIN_PATH: str = "/forestry-api/system/unit/unified"
#: 当前用户信息接口路径（登录成功后进主界面时拉一次）
DEFAULT_CURRENT_USER_PATH: str = "/forestry-api/system/unit/currentUserByToken"
#: 带 token 的请求头名（服务端实测不看头，但按前端约定照带；后续业务接口也用这个名字）
DEFAULT_TOKEN_HEADER: str = "unitkey"
#: 请求头里 token 的前缀（服务端 ``data.unitKey`` 就是 ``"Bearer " + token``）
DEFAULT_TOKEN_PREFIX: str = "Bearer "
#: token 要放 query 时用的参数名（如 ``currentUserByToken?access_token=…``）
DEFAULT_TOKEN_QUERY_FIELD: str = "access_token"
#: 单次请求超时（秒）
DEFAULT_TIMEOUT_SECONDS: float = 10.0
#: ``https`` 是否校验证书（内网自签名证书时设 ``False``）
DEFAULT_VERIFY_SSL: bool = True

# ---- 请求字段 ----（后端契约不一致时改这里）
#: 密码提交方式：``md5`` = md5(密码) 的 32 位小写 hex；``plain`` = 明文原样
DEFAULT_PASSWORD_ENCODING: str = "md5"
#: 请求 body 里用户名的字段名
DEFAULT_USERNAME_FIELD: str = "username"
#: 请求 body 里密码的字段名
DEFAULT_PASSWORD_FIELD: str = "password"
#: 请求 body 里除用户名/密码外的固定字段（本项目后端要 ``isCode: false``）
DEFAULT_EXTRA_BODY: tuple[tuple[str, Any], ...] = (("isCode", False),)

# ---- 响应字段 ----
#: 响应里 token 的字段名，按顺序找（顶层找不到会在 ``data`` / ``result`` 里再找）
DEFAULT_TOKEN_FIELDS: tuple[str, ...] = ("access_token", "token", "accessToken", "accessTokenValue")
#: 响应里用户名的字段名（本项目后端不返回，回退用请求里提交的用户名）
DEFAULT_RESP_USERNAME_FIELD: str = "username"
#: 视为「业务成功」的 ``code`` / ``status`` 取值。本项目后端用 ``5000``；
#: 其余是常见写法的兼容项。响应里有 ``success`` / ``ok`` 布尔时以布尔为准。
DEFAULT_SUCCESS_CODES: tuple[int | str, ...] = (5000, "5000", 200, 0, "200", "0", "ok", "success")

# 环境变量名
ENV_BASE_URL: str = "QGIS_DEV_AUTH_BASE_URL"
ENV_IP: str = "QGIS_DEV_AUTH_IP"
ENV_PORT: str = "QGIS_DEV_AUTH_PORT"
ENV_SCHEME: str = "QGIS_DEV_AUTH_SCHEME"
ENV_LOGIN_PATH: str = "QGIS_DEV_AUTH_LOGIN_PATH"
ENV_CURRENT_USER_PATH: str = "QGIS_DEV_AUTH_CURRENT_USER_PATH"
ENV_TOKEN_HEADER: str = "QGIS_DEV_AUTH_TOKEN_HEADER"
ENV_TIMEOUT: str = "QGIS_DEV_AUTH_TIMEOUT"
ENV_VERIFY_SSL: str = "QGIS_DEV_AUTH_VERIFY_SSL"

_TRUE_WORDS = frozenset({"1", "true", "yes", "on", "y", "t"})
_FALSE_WORDS = frozenset({"0", "false", "no", "off", "n", "f"})


@dataclass(frozen=True)
class AuthServerConfig:
    """授权服务器地址 + 登录接口 + 请求/响应契约。

    字段默认值全部取自本模块顶部的常量（＝ ``dev`` 的值）；一般只用
    :func:`load_auth_server_config` 构造（它会先按环境 :data:`PROFILES` 取段、
    再叠加环境变量覆盖），不要自己传一堆参数。
    """

    scheme: str = DEFAULT_SERVER_SCHEME
    ip: str = DEFAULT_SERVER_IP
    port: int = DEFAULT_SERVER_PORT
    login_path: str = DEFAULT_LOGIN_PATH
    current_user_path: str = DEFAULT_CURRENT_USER_PATH
    token_header: str = DEFAULT_TOKEN_HEADER
    token_prefix: str = DEFAULT_TOKEN_PREFIX
    token_query_field: str = DEFAULT_TOKEN_QUERY_FIELD
    timeout: float = DEFAULT_TIMEOUT_SECONDS
    verify_ssl: bool = DEFAULT_VERIFY_SSL
    password_encoding: str = DEFAULT_PASSWORD_ENCODING
    username_field: str = DEFAULT_USERNAME_FIELD
    password_field: str = DEFAULT_PASSWORD_FIELD
    extra_body: tuple[tuple[str, Any], ...] = DEFAULT_EXTRA_BODY
    token_fields: tuple[str, ...] = DEFAULT_TOKEN_FIELDS
    resp_username_field: str = DEFAULT_RESP_USERNAME_FIELD
    success_codes: tuple[int | str, ...] = DEFAULT_SUCCESS_CODES

    # ---------------------------------------------------------- 派生地址

    @property
    def base_url(self) -> str:
        """``scheme://ip:port``（不含末尾斜杠）。"""
        return f"{self.scheme}://{self.ip}:{self.port}"

    @property
    def login_url(self) -> str:
        """登录接口完整 URL（``login_path`` 缺前导 ``/`` 会自动补）。"""
        path = self.login_path if self.login_path.startswith("/") else f"/{self.login_path}"
        return f"{self.base_url}{path}"

    def current_user_url(self, token: str) -> str:
        """当前用户信息接口完整 URL（token 放 query，已做 URL 转义）。"""
        path = self._path(self.current_user_path)
        return f"{self.base_url}{path}?access_token={quote(token, safe='')}"

    def token_header_value(self, token: str) -> str:
        """带 token 的请求头值（``Bearer <token>``）；token 已带前缀时不重复拼。"""
        text = (token or "").strip()
        prefix = self.token_prefix
        if prefix and text.startswith(prefix):
            return text
        return f"{prefix}{text}"

    @staticmethod
    def _path(path: str) -> str:
        """补前导 ``/``。"""
        return path if path.startswith("/") else f"/{path}"

    def describe(self) -> str:
        """一行人类可读描述（日志 / 错误提示用）。"""
        return f"{self.login_url}（超时 {self.timeout:g}s，密码 {self.password_encoding}）"


def resolve_env(env: str | None = None, *, config_path: str | Path | None = None) -> str:
    """当前环境名：显式 ``env`` → ``config/app.yaml`` 的 ``env:`` → :data:`DEFAULT_ENV`。

    ``app.yaml`` 读不到（打包缺文件 / 路径写错 / 内容写坏）时也返回
    :data:`DEFAULT_ENV`，**不抛异常**——配置有问题不该拦住启动登录。

    :param env: 直接指定环境名（调用方已知 :attr:`app.env.config.AppConfig.env` 时
        传进来，省一次读盘）
    :param config_path: ``app.yaml`` 路径；``None`` ＝ ``QGIS_DEV_CONFIG`` 或仓库内默认位置
    """
    name = (env or "").strip()
    if name:
        return name
    value = _load_app_config(config_path).get("env")
    if isinstance(value, str) and value.strip():
        return value.strip()
    return DEFAULT_ENV  # 缺失 / 不是字符串（写错成列表等）也算没配


def load_auth_server_config(
    env: str | None = None,
    *,
    config_path: str | Path | None = None,
) -> AuthServerConfig:
    """按环境加载授权服务器配置（环境名怎么来见 :func:`resolve_env`）。

    取值优先级（高 → 低）：``QGIS_DEV_AUTH_*`` 环境变量 → :data:`PROFILES` 里当前
    环境那一段 → :data:`DEFAULT_*` 常量。字段缺失 / 类型不对（如端口不是数字）
    一律**回退上一级**，环境没在 :data:`PROFILES` 里配（``env: staging`` 却没加
    这一段）则记一条 warning 后按 :data:`DEFAULT_*` 走，都不抛异常。

    :param env: 环境名；``None`` ＝ 读 ``config/app.yaml`` 的 ``env:``
    :param config_path: ``app.yaml`` 路径；``None`` ＝ ``QGIS_DEV_CONFIG`` 或仓库内默认位置
    """
    env_name = resolve_env(env, config_path=config_path)
    fields = PROFILES.get(env_name)
    if fields is None:
        logger.warning("授权服务器未配置环境 %s（见 config/app.yaml 的 env:），回退默认值", env_name)
        fields = {}
    return _apply_env_overrides(_config_from_fields(fields))


# ------------------------------------------------------------------ 解析辅助


def _config_from_fields(fields: Mapping[str, Any]) -> AuthServerConfig:
    """用某个环境段的字段覆盖 :data:`DEFAULT_*`（缺字段 / 类型不对 → 用默认值）。"""
    return AuthServerConfig(
        scheme=_text_or(fields.get("scheme"), DEFAULT_SERVER_SCHEME).lower(),
        ip=_text_or(fields.get("ip"), DEFAULT_SERVER_IP),
        port=_int_or(fields.get("port"), DEFAULT_SERVER_PORT),
        login_path=_text_or(fields.get("login_path"), DEFAULT_LOGIN_PATH),
        current_user_path=_text_or(fields.get("current_user_path"), DEFAULT_CURRENT_USER_PATH),
        timeout=_float_or(fields.get("timeout"), DEFAULT_TIMEOUT_SECONDS),
        verify_ssl=_bool_or(fields.get("verify_ssl"), DEFAULT_VERIFY_SSL),
        password_encoding=_text_or(fields.get("password_encoding"), DEFAULT_PASSWORD_ENCODING),
    )


def _apply_env_overrides(cfg: AuthServerConfig) -> AuthServerConfig:
    """叠加 ``QGIS_DEV_AUTH_*`` 环境变量（最高优先级，见模块 docstring 的表）。

    ``QGIS_DEV_AUTH_BASE_URL`` 给了完整 base 就忽略 IP / PORT / SCHEME。
    """
    scheme = _env_str(ENV_SCHEME, cfg.scheme).lower()
    ip = _env_str(ENV_IP, cfg.ip)
    port = _env_int(ENV_PORT, cfg.port)
    base = _env_str(ENV_BASE_URL, "").strip()
    if base:
        # 完整 base 优先：拆出 scheme / host / port（没带协议前缀就整体当 host）
        parsed = _split_base_url(base)
        if parsed is not None:
            scheme, ip, port = parsed
        else:
            ip = base
    return replace(
        cfg,
        scheme=scheme,
        ip=ip,
        port=port,
        login_path=_env_str(ENV_LOGIN_PATH, cfg.login_path),
        current_user_path=_env_str(ENV_CURRENT_USER_PATH, cfg.current_user_path),
        token_header=_env_str(ENV_TOKEN_HEADER, cfg.token_header),
        timeout=_env_float(ENV_TIMEOUT, cfg.timeout),
        verify_ssl=_env_bool(ENV_VERIFY_SSL, cfg.verify_ssl),
    )


def _app_config_path() -> Path:
    """``config/app.yaml`` 实际路径：``QGIS_DEV_CONFIG`` 优先，其次仓库内默认位置。"""
    return Path(os.environ.get(ENV_CONFIG_PATH) or Paths.default().config_file("app.yaml"))


def _load_app_config(config_path: str | Path | None = None) -> dict[str, Any]:
    """读 ``config/app.yaml``（本模块只用它的顶层 ``env`` 键，不做完整解析）。

    文件缺失 / 编码不对 / YAML 解析失败 / 顶层不是映射 → ``{}``（调用方回退默认
    值），**不抛异常**。
    """
    path = Path(config_path) if config_path else _app_config_path()
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, yaml.YAMLError):
        return {}
    return data if isinstance(data, dict) else {}


def _split_base_url(base: str) -> tuple[str, str, int] | None:
    """把 ``http://1.2.3.4:8080`` 拆成 ``("http", "1.2.3.4", 8080)``。

    缺端口时用协议默认端口；无法识别协议前缀返回 ``None``（调用方按 host 处理）。
    """
    text = base.strip().rstrip("/")
    if "://" not in text:
        return None
    scheme, rest = text.split("://", 1)
    scheme = scheme.lower()
    if not rest:
        return None
    default_port = 443 if scheme == "https" else 80
    if rest.startswith("["):  # IPv6 字面量：[::1]:8080
        host, _, tail = rest.partition("]")
        return scheme, f"{host}]", _int_or(tail.lstrip(":"), default_port)
    host, sep, port_text = rest.rpartition(":")
    if not sep or not host:  # 没写端口
        return scheme, rest, default_port
    return scheme, host, _int_or(port_text, default_port)


def _env_str(name: str, default: str) -> str:
    return _text_or(os.environ.get(name), default)


def _env_int(name: str, default: int) -> int:
    return _int_or(os.environ.get(name), default)


def _env_float(name: str, default: float) -> float:
    return _float_or(os.environ.get(name), default)


def _env_bool(name: str, default: bool) -> bool:
    return _bool_or(os.environ.get(name), default)


def _text_or(value: object, default: str) -> str:
    """非空文本，否则 ``default``（``None`` / 空白串都算没配）。"""
    text = str(value).strip() if value is not None else ""
    return text or default


def _int_or(value: object, default: int) -> int:
    """正整数字符串 / 数字，否则 ``default``（端口等不允许 0 或负数）。"""
    try:
        num = int(str(value).strip())
    except (TypeError, ValueError):
        return default
    return num if num > 0 else default


def _float_or(value: object, default: float) -> float:
    """正浮点，否则 ``default``（超时秒数不允许 0 或负数）。"""
    try:
        num = float(str(value).strip())
    except (TypeError, ValueError):
        return default
    return num if num > 0 else default


def _bool_or(value: object, default: bool) -> bool:
    """YAML 布尔 / ``"true"`` / ``"1"`` / ``"off"`` 之类，认不出来 → ``default``。"""
    if isinstance(value, bool):
        return value
    text = str(value).strip().lower() if value is not None else ""
    if text in _TRUE_WORDS:
        return True
    if text in _FALSE_WORDS:
        return False
    return default


__all__ = [
    "DEFAULT_CURRENT_USER_PATH",
    "DEFAULT_ENV",
    "DEFAULT_LOGIN_PATH",
    "DEFAULT_PASSWORD_ENCODING",
    "DEFAULT_SERVER_IP",
    "DEFAULT_SERVER_PORT",
    "DEFAULT_SERVER_SCHEME",
    "DEFAULT_SUCCESS_CODES",
    "DEFAULT_TIMEOUT_SECONDS",
    "DEFAULT_TOKEN_FIELDS",
    "DEFAULT_TOKEN_HEADER",
    "DEFAULT_TOKEN_PREFIX",
    "DEFAULT_TOKEN_QUERY_FIELD",
    "DEFAULT_VERIFY_SSL",
    "ENV_CONFIG_PATH",
    "PROFILES",
    "AuthServerConfig",
    "load_auth_server_config",
    "resolve_env",
]
