"""服务配置加载。"""

from __future__ import annotations

import os
from dataclasses import dataclass


@dataclass(frozen=True)
class ServerConfig:
    """FastAPI 服务配置。"""

    host: str = "0.0.0.0"
    port: int = 8000
    workers: int = 1
    reload: bool = False
    tile_config_path: str = "api/resource/config_xyz.json"

    @classmethod
    def from_env(cls) -> "ServerConfig":
        return cls(
            host=os.environ.get("QGIS_DEV_SERVER_HOST", "0.0.0.0"),
            port=int(os.environ.get("QGIS_DEV_SERVER_PORT", "8000")),
            workers=int(os.environ.get("QGIS_DEV_SERVER_WORKERS", "1")),
            reload=os.environ.get("QGIS_DEV_SERVER_RELOAD", "").lower() in ("1", "true", "yes"),
            tile_config_path=os.environ.get(
                "QGIS_DEV_TILE_CONFIG", "api/resource/config_xyz.json"
            ),
        )
