"""应用配置加载。"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional

import yaml


@dataclass(frozen=True)
class AppConfig:
    """``config/app.yaml`` 反序列化结果。"""

    env: str                       # "dev" | "prod"
    app_name: str
    language: str
    crs: str
    map: "MapConfig"
    qgis: "QgisConfig"
    paths: "PathsConfig"
    logging: "LoggingConfig"
    server: "ServerConfig"

    @classmethod
    def from_file(cls, path: Path) -> "AppConfig":
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
        return cls.from_dict(data)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "AppConfig":
        return cls(
            env=data.get("env", "dev"),
            app_name=data.get("app", {}).get("name", "qgis_dev"),
            language=data.get("app", {}).get("language", "zh-Hans"),
            crs=data.get("app", {}).get("crs", "EPSG:4490"),
            map=MapConfig.from_dict(data.get("map", {})),
            qgis=QgisConfig.from_dict(data.get("qgis", {})),
            paths=PathsConfig.from_dict(data.get("paths", {})),
            logging=LoggingConfig.from_dict(data.get("logging", {})),
            server=ServerConfig.from_dict(data.get("server", {})),
        )


@dataclass(frozen=True)
class QgisConfig:
    install_path: Optional[Path]    # 仅 dev 用；prod 走环境变量
    python_path: Optional[Path]
    bin_path: Optional[Path]

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "QgisConfig":
        return cls(
            install_path=_to_path(data.get("install_path")),
            python_path=_to_path(data.get("python_path")),
            bin_path=_to_path(data.get("bin_path")),
        )


@dataclass(frozen=True)
class PathsConfig:
    resource: Optional[Path]
    log: Optional[Path]
    tmp: Optional[Path]

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "PathsConfig":
        return cls(
            resource=_to_path(data.get("resource")),
            log=_to_path(data.get("log")),
            tmp=_to_path(data.get("tmp")),
        )


@dataclass(frozen=True)
class LoggingConfig:
    level: str
    file_level: str
    rotation_mb: int
    backup_count: int

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "LoggingConfig":
        return cls(
            level=data.get("level", "INFO"),
            file_level=data.get("file_level", "DEBUG"),
            rotation_mb=int(data.get("rotation_mb", 10)),
            backup_count=int(data.get("backup_count", 5)),
        )


@dataclass(frozen=True)
class ServerConfig:
    host: str
    port: int
    workers: int

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "ServerConfig":
        return cls(
            host=data.get("host", "0.0.0.0"),
            port=int(data.get("port", 8000)),
            workers=int(data.get("workers", 1)),
        )


@dataclass(frozen=True)
class BaseTileConfig:
    """XYZ 瓦片底图配置（如天地图）。"""

    enabled: bool
    name: str
    type: str
    url: str
    referer: str
    zmin: int
    zmax: int

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "BaseTileConfig":
        return cls(
            enabled=bool(data.get("enabled", True)),
            name=data.get("name", "天地图影像"),
            type=data.get("type", "xyz"),
            url=data.get("url", ""),
            referer=data.get("referer", ""),
            zmin=int(data.get("zmin", 0)),
            zmax=int(data.get("zmax", 18)),
        )


@dataclass(frozen=True)
class MapConfig:
    """地图相关配置。"""

    base_tile: Optional[BaseTileConfig]

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "MapConfig":
        base = data.get("base_tile")
        return cls(base_tile=BaseTileConfig.from_dict(base) if base else None)


def _to_path(v: Any) -> Optional[Path]:
    if v is None:
        return None
    return Path(v)
