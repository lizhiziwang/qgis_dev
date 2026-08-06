"""FastAPI app 工厂。

旧 :class:`api.interface:app` 是在 UI 进程内 ``threading.Thread(target=run_api).start()`` 启动。
新代码把 :func:`create_app` 作为工厂；UI 通过 HTTP 调用，server 独立进程::

    python -m app.server         # uvicorn 启动
"""

from __future__ import annotations

from contextlib import asynccontextmanager

from fastapi import FastAPI
from starlette.middleware.cors import CORSMiddleware

from app.infra.logging import get_logger
from app.server.config import ServerConfig
from app.server.dependencies import get_check_service, get_server_config
from app.server.lifespan import app_lifespan
from app.server.routes import check as check_routes
from app.server.routes import health as health_routes
from app.server.routes import map as map_routes


logger = get_logger(__name__)


def create_app() -> FastAPI:
    """构造 :class:`FastAPI` 实例。"""
    app = FastAPI(
        title="qgis_dev tile & check service",
        version="0.2.0",
        lifespan=app_lifespan,
    )

    # CORS（开发期放开；生产应白名单）
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    # 路由
    app.include_router(health_routes.router)
    app.include_router(map_routes.router, prefix="/getmap")
    app.include_router(check_routes.router, prefix="/check")

    # 启动时记录配置
    cfg = ServerConfig.from_env()
    logger.info("server configured: host=%s port=%s", cfg.host, cfg.port)

    return app


# 让 ``uvicorn app.server.app:app`` 也能直接跑
app = create_app()


__all__ = ["app", "create_app"]
