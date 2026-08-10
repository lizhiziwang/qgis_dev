"""测试 :class:`app.env.config.AppConfig` 与 :class:`app.env.qgis_runtime.QgisRuntime`。"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from app.env.config import AppConfig
from app.env.qgis_runtime import QgisRuntime


def test_app_config_from_file(tmp_path: Path):
    p = tmp_path / "app.yaml"
    p.write_text(
        """
env: dev
app:
  name: demo
  language: zh-Hans
  crs: EPSG:4490
qgis:
  install_path: D:/qgis
  python_path: D:/qgis/python
  bin_path: D:/qgis/bin
paths:
  resource: resource
  log: logs
  tmp: .tmp
logging:
  level: INFO
  file_level: DEBUG
  rotation_mb: 10
  backup_count: 5
server:
  host: 0.0.0.0
  port: 8000
  workers: 1
""",
        encoding="utf-8",
    )
    cfg = AppConfig.from_file(p)
    assert cfg.env == "dev"
    assert cfg.app_name == "demo"
    assert cfg.crs == "EPSG:4490"
    assert cfg.qgis.install_path == Path("D:/qgis")
    assert cfg.logging.level == "INFO"
    assert cfg.server.port == 8000


def test_app_config_from_dict_uses_defaults():
    cfg = AppConfig.from_dict({})
    assert cfg.env == "dev"
    assert cfg.app_name == "qgis_dev"
    assert cfg.crs == "EPSG:4490"
    assert cfg.server.port == 8000


def test_app_config_map_base_tile_parses():
    cfg = AppConfig.from_dict(
        {
            "map": {
                "base_tile": {
                    "enabled": True,
                    "name": "天地图影像",
                    "type": "xyz",
                    "url": "https://t0.tianditu.gov.cn/img_w/wmts?TILECOL={x}&TILEROW={y}&TILEMATRIX={z}&tk=key",
                    "zmin": 0,
                    "zmax": 18,
                }
            }
        }
    )
    tile = cfg.map.base_tile
    assert tile is not None
    assert tile.enabled and tile.type == "xyz"
    assert "{x}" in tile.url and "{z}" in tile.url
    assert tile.zmax == 18


def test_app_config_map_defaults_to_none():
    cfg = AppConfig.from_dict({})
    assert cfg.map.base_tile is None


def test_qgis_runtime_for_dev_builds_paths():
    base = Path("D:/qgis")
    r = QgisRuntime.for_dev(base)
    assert r.qgis_prefix == base / "apps" / "qgis"
    assert r.qt6 == base / "apps" / "qt6"
    assert r.gdal == base / "apps" / "gdal"
    assert r.proj == base / "share" / "proj"
    assert r.bin == base / "bin"


def test_qgis_runtime_for_prod_requires_env(monkeypatch):
    monkeypatch.delenv("QGIS_PREFIX", raising=False)
    with pytest.raises(RuntimeError):
        QgisRuntime.for_prod()


def test_qgis_runtime_for_prod_uses_env(monkeypatch, tmp_path: Path):
    monkeypatch.setenv("QGIS_PREFIX", str(tmp_path))
    r = QgisRuntime.for_prod()
    assert r.qgis_base == tmp_path


def test_apply_env_sets_required_vars(monkeypatch, tmp_path: Path):
    monkeypatch.delenv("PATH", raising=False)
    r = QgisRuntime.for_dev(tmp_path)
    r.apply_env()
    assert os.environ["QGIS_PREFIX_PATH"] == str(r.qgis_prefix)
    assert os.environ["PROJ_DATA"].endswith("share" + os.sep + "proj")
    assert os.environ["GDAL_DATA"].endswith("share" + os.sep + "gdal")
    assert os.environ["GDAL_DRIVER_PATH"].endswith("gdalplugins")
    assert str(r.qt6 / "plugins") in os.environ["QT_PLUGIN_PATH"]


def test_apply_env_idempotent(monkeypatch, tmp_path: Path):
    r = QgisRuntime.for_dev(tmp_path)
    r.apply_env()
    first = os.environ["QGIS_PREFIX_PATH"]
    r.apply_env()
    assert os.environ["QGIS_PREFIX_PATH"] == first
