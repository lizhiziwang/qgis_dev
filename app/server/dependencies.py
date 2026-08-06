"""FastAPI 路由依赖注入。"""

from __future__ import annotations

from fastapi import Depends

from app.core.domain.check import CheckService
from app.server.config import ServerConfig


def get_server_config() -> ServerConfig:
    """返回当前 :class:`ServerConfig`。"""
    return ServerConfig.from_env()


def get_check_service() -> CheckService:
    """返回 :class:`CheckService` 单例。"""
    return CheckService()
