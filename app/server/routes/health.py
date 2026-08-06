"""健康检查路由。"""

from __future__ import annotations

from fastapi import APIRouter


router = APIRouter(tags=["health"])


@router.get("/health", summary="服务健康检查")
def health() -> dict:
    """返回服务健康状态。"""
    return {"status": "ok", "service": "qgis_dev"}


@router.get("/ready", summary="就绪检查")
def ready() -> dict:
    return {"ready": True}
