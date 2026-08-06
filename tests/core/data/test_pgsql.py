"""测试 :class:`app.core.data.pgsql.PgConfig`。"""

from __future__ import annotations

from app.core.data.pgsql import PgConfig


def test_default_config():
    cfg = PgConfig()
    assert cfg.host == "localhost"
    assert cfg.port == 5432
    assert cfg.dbname == "postgis_30_sample"
    assert cfg.user == "postgres"


def test_dsn_returns_dict():
    cfg = PgConfig(host="x", port=1234, dbname="d", user="u", password="p")
    dsn = cfg.dsn()
    assert dsn["host"] == "x"
    assert dsn["port"] == 1234
    assert dsn["password"] == "p"


def test_from_env_reads(monkeypatch):
    monkeypatch.setenv("PGHOST", "remote.example.com")
    monkeypatch.setenv("PGPORT", "5433")
    monkeypatch.setenv("PGDATABASE", "mydb")
    monkeypatch.setenv("PGUSER", "alice")
    monkeypatch.setenv("PGPASSWORD", "secret")
    cfg = PgConfig.from_env()
    assert cfg.host == "remote.example.com"
    assert cfg.port == 5433
    assert cfg.user == "alice"
    assert cfg.password == "secret"
