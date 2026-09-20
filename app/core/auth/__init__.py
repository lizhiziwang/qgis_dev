"""登录鉴权模块。

用户名密码提交到**授权服务器**换取 token：服务器地址与接口契约在
:mod:`app.core.auth.config`（部署改那一个文件即可），HTTP 客户端是
:class:`app.core.auth.client.AuthClient`（仅标准库 urllib，无第三方依赖）。

**每次启动都要登录**：不再签发 / 保存本地 token 凭证，也没有"30 天免登录"。
token 只存在于本次运行的内存里（:class:`app.core.auth.client.AuthResult`），
由 :func:`app.bootstrap._run_login_dialog` 取出后传给主窗口显示账号信息。

**登录之外的业务接口一律走** :mod:`app.core.util.service_client`（统一根地址 + 自动带
``unitkey: Bearer <token>``）：登录成功后在 bootstrap 里 ``set_session_token()``，随后
当前用户信息由 :func:`app.core.auth.current_user.fetch_current_user` 拉取（内部就是调
service_client），其它业务模块也照这个写法，本包不再自己发业务请求。

**"记住密码"是另一回事**：:mod:`app.core.auth.saved_logins` 只在登录成功后把
「用户名 + 密码」留在本机，供下次登录点用户名输入框时一键回填 Token 之前的输入，
**与鉴权无关**（每次仍然要连服务器）。密码不落明文：Windows 用 DPAPI，
Linux / macOS 用机器 + 用户绑定的本地密钥（:mod:`app.core.auth._secret`）；
见这两个模块的 docstring 风险说明与 ``QGIS_DEV_REMEMBER_PASSWORDS`` 开关。
"""

from __future__ import annotations

from .client import AuthClient, AuthResult, encode_password, md5_hex
from .config import AuthServerConfig, load_auth_server_config
from .current_user import CurrentUser, fetch_current_user
from .errors import AuthError
from .saved_logins import (
    MAX_SAVED_LOGINS,
    SavedLogin,
    clear_saved_logins,
    load_saved_logins,
    password_protection,
    remember_passwords_enabled,
    remove_login,
    save_login,
    saved_logins_path,
)

__all__ = [
    "MAX_SAVED_LOGINS",
    "AuthClient",
    "AuthError",
    "AuthResult",
    "AuthServerConfig",
    "CurrentUser",
    "SavedLogin",
    "clear_saved_logins",
    "encode_password",
    "fetch_current_user",
    "load_auth_server_config",
    "load_saved_logins",
    "md5_hex",
    "password_protection",
    "remember_passwords_enabled",
    "remove_login",
    "save_login",
    "saved_logins_path",
]
