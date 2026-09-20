"""授权相关异常（单独一个模块，避免 ``client`` / ``current_user`` 互相 import）。"""

from __future__ import annotations

__all__ = ["AuthError"]


class AuthError(Exception):
    """授权失败（账号密码错 / 网络不通 / 服务端报错 / 响应格式异常 / token 失效）。

    ``str(exc)`` 是可直接展示给用户的中文提示。
    """
