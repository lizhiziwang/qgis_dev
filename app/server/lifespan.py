"""FastAPI lifespan（启动 / 关闭钩子）。"""

from __future__ import annotations

from contextlib import asynccontextmanager

from app.infra.logging import get_logger


logger = get_logger(__name__)


@asynccontextmanager
async def app_lifespan(app):
    """记录启动 / 关闭日志。"""
    logger.info("server starting")
    yield
    logger.info("server stopped")
