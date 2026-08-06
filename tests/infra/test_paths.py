"""测试 :class:`app.infra.fs.paths.Paths` 与 :mod:`app.infra.fs.io`。"""

from __future__ import annotations

from pathlib import Path

import pytest

from app.infra.fs.io import read_text, write_bytes, write_text
from app.infra.fs.paths import Paths


def test_default_paths_resolve_under_project():
    p = Paths.default()
    assert p.project_root.is_dir()
    # 至少能解析出 logs 子目录（项目根存在 logs/）
    assert p.logs.parent == p.project_root
    assert p.config.parent == p.project_root


def test_paths_ensure_creates_missing_dirs(tmp_path: Path, monkeypatch):
    # monkeypatch default 路径：把 project_root 重定向到 tmp
    monkeypatch.setenv("QGIS_DEV_PATHS_PROJECT_ROOT", str(tmp_path))
    p = Paths.from_env(Paths.default())
    p.ensure()
    for d in (p.logs, p.tmp, p.config):
        assert d.is_dir(), f"missing dir {d}"


def test_resource_joins_parts():
    p = Paths.default()
    target = p.resource("png", "app_icon.png")
    assert target == p.resources / "png" / "app_icon.png"
    assert isinstance(target, Path)


def test_env_overrides_each_field(monkeypatch, tmp_path: Path):
    monkeypatch.setenv("QGIS_DEV_PATHS_RESOURCE", str(tmp_path / "r"))
    monkeypatch.setenv("QGIS_DEV_PATHS_LOG", str(tmp_path / "l"))
    monkeypatch.setenv("QGIS_DEV_PATHS_TMP", str(tmp_path / "t"))
    monkeypatch.setenv("QGIS_DEV_PATHS_CONFIG", str(tmp_path / "c"))

    p = Paths.from_env(Paths.default())
    assert p.resources == tmp_path / "r"
    assert p.logs == tmp_path / "l"
    assert p.tmp == tmp_path / "t"
    assert p.config == tmp_path / "c"


def test_paths_is_frozen():
    p = Paths.default()
    with pytest.raises((AttributeError, Exception)):
        p.project_root = Path("/tmp")  # type: ignore[misc]


# --------------------------------------------------------------- io

def test_write_text_roundtrip(tmp_path: Path):
    p = tmp_path / "a.txt"
    assert write_text(p, "你好\n世界") is True
    assert read_text(p) == "你好\n世界"


def test_write_text_returns_false_on_bad_path(tmp_path: Path):
    assert write_text(tmp_path / "no_such_dir" / "a.txt", "x") is False


def test_write_bytes(tmp_path: Path):
    p = tmp_path / "b.bin"
    assert write_bytes(p, b"\x00\x01\xff") is True
    assert p.read_bytes() == b"\x00\x01\xff"
