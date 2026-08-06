"""瓦片服务路由。

迁自 :class:`api.routes.map`：把配置改走 YAML + dataclass。
"""

from __future__ import annotations

import json
import os
from typing import Optional

from fastapi import APIRouter, HTTPException
from fastapi.responses import FileResponse
from pydantic import BaseModel

from app.server.dependencies import get_server_config
from app.server.config import ServerConfig


router = APIRouter(tags=["tiles"])


class TileConfig(BaseModel):
    """瓦片配置。"""

    id: int
    name: str
    basePass: str
    format: str = "xyz-pbf"


def _load_tile_config(path: str) -> list[dict]:
    """加载瓦片配置 JSON。"""
    if not os.path.exists(path):
        return []
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except (OSError, json.JSONDecodeError):
        return []


def _find_tile_config(config_path: str, cfg_id: int) -> Optional[dict]:
    for item in _load_tile_config(config_path):
        if int(item.get("id", -1)) == cfg_id:
            return item
    return None


@router.get("/test", summary="服务存活测试")
def test() -> str:
    return "成功！"


@router.get("/config", summary="返回瓦片配置列表")
def list_configs(server_cfg: ServerConfig = None) -> list[dict]:
    server_cfg = server_cfg or get_server_config()
    return _load_tile_config(server_cfg.tile_config_path)


@router.get("/map/{cfg_id}/{z}/{x}/{y}", summary="获取地图瓦片")
def out_map(cfg_id: int, z: int, x: int, y: int) -> FileResponse:
    server_cfg = get_server_config()
    item = _find_tile_config(server_cfg.tile_config_path, cfg_id)
    if item is None:
        raise HTTPException(status_code=404, detail="config not found")
    base = item.get("basePass", "")
    tile_path = os.path.join(base, str(z), str(x), f"{y}.pbf")
    if os.path.exists(tile_path):
        return FileResponse(tile_path, media_type="application/x-protobuf")
    raise HTTPException(status_code=404, detail=f"tile not found: {tile_path}")
