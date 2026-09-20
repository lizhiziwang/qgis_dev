"""当前登录用户信息（``currentUserByToken``）——**业务接口，走统一请求工具**。

登录成功后（进主界面之前）拉一次，主界面标题栏显示的就是它::

    GET {base_url}{current_user_path}?access_token=<token>
    unitkey: Bearer <token>

    {"code": 5000, "message": "操作成功", "success": true,
     "data": {"userInfo": {"userAccount": "ayxlyj", "userName": "测试安义县",
              "departmentName": "办公室", "organizeName": "南昌市安义县林业局",
              "mobilePhone": "", "headIcon": "/api/file/Image/userAvatar/001.png",
              "areaName": "安义县", "prevLoginTime": 1773105860000,
              "prevLoginIPAddress": "10.8.61.23"},
              "unitKey": "Bearer <token>", "needResetPassword": false}}

实测：该接口**只认 query 里的 ``access_token``**（头不带也能过；token 不对则
HTTP 200 但 ``code: 401``）——头按前端约定照带，解析以 ``success`` / ``code`` 为准。

传输层统一走 :mod:`app.core.util.service_client`（除登录以外的业务服务器访问都走它，
见 ``app/auth.md`` §2.2.2）：本模块只负责"调哪个接口 + 怎么解析成
:class:`CurrentUser`"，不再自己拼 URL / 发 HTTP。
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any

from app.core.auth._payload import pick, pick_any, pick_str
from app.core.auth.config import AuthServerConfig, load_auth_server_config
from app.core.auth.errors import AuthError
from app.core.util import service_client

__all__ = ["CurrentUser", "fetch_current_user"]

#: 用户对象可能被包在哪个键下（``currentUserByToken`` 用 ``data.userInfo``）
_USER_KEYS: tuple[str, ...] = ("userInfo", "userinfo", "user", "currentUser", "account")
#: 登录账号字段
_ACCOUNT_KEYS: tuple[str, ...] = ("userAccount", "account", "username", "userName", "loginName")
#: 姓名 / 显示名字段（``userName`` 优先，其次常见别名）
_NAME_KEYS: tuple[str, ...] = ("userName", "realName", "nickName", "name", "displayName")


@dataclass(frozen=True)
class CurrentUser:
    """``currentUserByToken`` 返回的当前登录用户（主界面标题栏显示的就是它）。

    字段全部可缺省：服务端少给哪个，界面上对应的那一项就不显示，**不报错**。
    """

    #: 登录账号（``userAccount``）
    account: str = ""
    #: 姓名 / 显示名（``userName``）
    name: str = ""
    #: 部门（``departmentName``）
    department: str = ""
    #: 单位 / 机构（``organizeName``）
    organize: str = ""
    #: 行政区划（``areaName``）
    area: str = ""
    #: 手机号（``mobilePhone``，实测可能为空串）
    mobile: str = ""
    #: 头像相对路径（``headIcon``）
    avatar: str = ""
    #: 后续业务请求要带的头值（``data.unitKey``，形如 ``"Bearer <token>"``）
    unit_key: str = ""
    #: 服务端要求重置密码（``needResetPassword``）
    need_reset_password: bool = False
    #: 上次登录时间（epoch 秒；服务端给毫秒时会自动换算）
    prev_login_at: int | None = None
    #: 上次登录 IP（``prevLoginIPAddress``）
    prev_login_ip: str = ""
    #: 原始 ``data`` 字典（排查问题时看）
    raw: dict[str, Any] = field(default_factory=dict)

    def display_name(self) -> str:
        """界面上显示的名字：姓名 → 账号 → ``"未登录"``。"""
        return self.name or self.account or "未登录"

    def organize_line(self) -> str:
        """“单位 / 部门”合成一行（哪一段缺就只显示另一段，都缺为空串）。"""
        parts = [part for part in (self.organize, self.department) if part]
        return " / ".join(parts)

    def prev_login_text(self) -> str:
        """上次登录信息（``2026-03-10 12:04 · 10.8.61.23``）；没数据返回空串。"""
        if not self.prev_login_at:
            return self.prev_login_ip
        stamp = time.strftime("%Y-%m-%d %H:%M", time.localtime(self.prev_login_at))
        return f"{stamp} · {self.prev_login_ip}" if self.prev_login_ip else stamp


def fetch_current_user(
    token: str,
    *,
    config: AuthServerConfig | None = None,
    env: str | None = None,
) -> CurrentUser:
    """用 token 拉当前登录用户信息（进主界面时调，标题栏显示的就是它）。

    请求交给 :func:`app.core.util.service_client.request`：路径只写
    ``current_user_path``，根地址（``scheme://ip:port``）由它按环境配置补；
    token 走 query 的 ``access_token``（``token_in_query=True``），同时按前端约定
    带上 ``unitkey: Bearer <token>`` 头。

    :param token: 登录拿到的 token（``Bearer`` 前缀可有可无，会去掉首尾空白）
    :param config: 服务器配置；``None`` ＝ :func:`load_auth_server_config` 按环境读
    :param env: 环境名；``None`` ＝ 读 ``config/app.yaml`` 的 ``env:``
    :raises AuthError: 没带 token / 网络不通 / 超时 / token 过期（``code`` 401）/\
        服务端报错 / 响应格式异常。
    """
    token = (token or "").strip()
    if not token:
        raise AuthError("缺少登录 token，无法获取用户信息")

    cfg = config if config is not None else load_auth_server_config(env)
    try:
        response = service_client.request(
            cfg.current_user_path,
            method="GET",
            token=token,
            token_in_query=True,
            config=cfg,  # 根地址 / 头名 / 超时都按这份配置，别再去读环境变量
        )
    except service_client.ServiceError as exc:
        raise AuthError(str(exc)) from exc
    return _parse_current_user(response.payload)


def _parse_current_user(payload: Any) -> CurrentUser:
    """响应 JSON（``ServiceResponse.payload``）→ :class:`CurrentUser`；字段全可缺。"""
    if not isinstance(payload, dict):
        raise AuthError("获取当前用户信息的响应格式异常（顶层不是 JSON 对象）")
    info = _user_payload(payload)
    return CurrentUser(
        account=pick_str(info, _ACCOUNT_KEYS),
        name=pick_str(info, _NAME_KEYS),
        department=pick_str(info, ("departmentName", "deptName", "department", "dept")),
        organize=pick_str(info, ("organizeName", "orgName", "organize", "org", "unitName")),
        area=pick_str(info, ("areaName", "area", "areaCode")),
        mobile=pick_str(info, ("mobilePhone", "mobile", "phone", "telephone")),
        avatar=pick_str(info, ("headIcon", "avatar", "headImg", "icon")),
        unit_key=pick_str(payload, ("unitKey", "unitkey")),
        need_reset_password=bool(pick(payload, "needResetPassword") or pick(info, "needResetPassword")),
        prev_login_at=_timestamp_seconds(pick_any(info, ("prevLoginTime", "lastLoginTime"))),
        prev_login_ip=pick_str(info, ("prevLoginIPAddress", "prevLoginIp", "lastLoginIp")),
        raw=info,
    )


def _user_payload(payload: dict[str, Any]) -> dict[str, Any]:
    """从响应里挑出「用户对象」（``data.userInfo`` / ``userInfo`` / ``data.user`` …）。

    找不到就用顶层字典（字段可能直接平铺），这样各家后端写法都能对上。
    """
    for key in _USER_KEYS:
        value = pick(payload, key)
        if isinstance(value, dict):
            return value
    data = payload.get("data")
    return data if isinstance(data, dict) else payload


def _timestamp_seconds(value: Any) -> int | None:
    """时间戳 → epoch 秒；服务端给毫秒（13 位）时自动除 1000，认不出来返回 ``None``。"""
    try:
        num = int(value)
    except (TypeError, ValueError):
        return None
    if num <= 0:
        return None
    return num // 1000 if num > 10_000_000_000 else num
