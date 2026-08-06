"""测试 :func:`app.core.data.gdb_to_postgis` 行为。"""

from __future__ import annotations

from unittest.mock import patch

from app.core.data.gdb_to_postgis import (
    ImportResult,
    _build_ogr2ogr_command,
    gdb_to_postgis,
    gdb_to_postgis_from_config,
)


def test_build_ogr2ogr_command_basic():
    cmd = _build_ogr2ogr_command("/data/xxx.gdb", "host=localhost dbname=test")
    assert cmd[0] == "ogr2ogr"
    assert cmd[1:3] == ["-f", "PostgreSQL"]
    assert "PG:host=localhost dbname=test" in cmd
    assert "/data/xxx.gdb" in cmd
    assert "LAUNDER=NO" in cmd


def test_build_ogr2ogr_command_with_options():
    cmd = _build_ogr2ogr_command("/x.gdb", "PG:...", options=["-t_srs", "EPSG:4490"])
    assert cmd[-2:] == ["-t_srs", "EPSG:4490"]


def test_gdb_to_postgis_ogr2ogr_not_in_path(monkeypatch):
    """ogr2ogr 不在 PATH 时返回 failed。"""
    monkeypatch.setattr("shutil.which", lambda _: None)
    result = gdb_to_postgis("/x.gdb", "host=...")
    assert result.status.startswith("failed:")
    assert "ogr2ogr" in result.status


def test_gdb_to_postgis_subprocess_success(monkeypatch):
    """mock subprocess.run 返回成功。"""
    fake_completed = type("R", (), {"returncode": 0, "stderr": ""})()
    monkeypatch.setattr("shutil.which", lambda _: "/usr/bin/ogr2ogr")
    monkeypatch.setattr(
        "subprocess.run", lambda *a, **kw: fake_completed
    )
    result = gdb_to_postgis("/x.gdb", "host=...")
    assert result.status == "success"
    assert result.table_name == "x.gdb"


def test_gdb_to_postgis_subprocess_calledprocesserror(monkeypatch):
    """mock subprocess.run 抛 CalledProcessError。"""
    import subprocess

    monkeypatch.setattr("shutil.which", lambda _: "/usr/bin/ogr2ogr")

    def fake_run(*a, **kw):
        err = subprocess.CalledProcessError(1, a[0] if a else [])
        err.stderr = "fail"
        raise err

    monkeypatch.setattr("subprocess.run", fake_run)
    result = gdb_to_postgis("/x.gdb", "host=...")
    assert result.status.startswith("failed:")
    assert "fail" in result.status


def test_gdb_to_postgis_from_config(monkeypatch):
    """用 PgConfig 生成 DSN 并调 gdb_to_postgis。"""
    fake_completed = type("R", (), {"returncode": 0, "stderr": ""})()
    monkeypatch.setattr("shutil.which", lambda _: "/usr/bin/ogr2ogr")
    monkeypatch.setattr(
        "subprocess.run", lambda *a, **kw: fake_completed
    )
    from app.core.data.pgsql import PgConfig
    cfg = PgConfig(host="h", port=1, dbname="d", user="u", password="p")
    result = gdb_to_postgis_from_config("/y.gdb", cfg=cfg)
    assert result.status == "success"
