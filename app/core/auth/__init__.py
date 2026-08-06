"""凭证 / 鉴权模块。

当前仅实现 30 天本地凭证（HMAC-SHA256 签名）—— 见
:mod:`app.core.auth.credential`。后续接入真实 auth 服务（OAuth / JWT /
公司 SSO）时，把 :func:`app.bootstrap._run_login_dialog` 里的用户名密码
校验换成调远端 API，再调 :func:`Credential.issue` 即可。
"""

from __future__ import annotations

from .credential import (
    VALIDITY_SECONDS,
    Credential,
    clear_credential,
    load_credential,
    save_credential,
    user_credential_path,
)

__all__ = [
    "VALIDITY_SECONDS",
    "Credential",
    "clear_credential",
    "load_credential",
    "save_credential",
    "user_credential_path",
]
