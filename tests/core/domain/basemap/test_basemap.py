"""测试 :func:`app.core.domain.basemap.build_xyz_uri`（纯 Python，不依赖 QGIS）。"""

from __future__ import annotations

from urllib.parse import unquote

from app.core.domain.basemap import build_xyz_uri
from app.env.config import BaseTileConfig


def _tile(url: str = "", referer: str = "") -> BaseTileConfig:
    return BaseTileConfig(
        enabled=True,
        name="天地图影像",
        type="xyz",
        url=url,
        referer=referer,
        zmin=0,
        zmax=18,
    )


def test_build_xyz_uri_shape():
    uri = build_xyz_uri(
        _tile("https://t0.tianditu.gov.cn/img_w/wmts?SERVICE=WMTS&TILECOL={x}&TILEROW={y}&TILEMATRIX={z}&tk=KEY")
    )
    assert uri.startswith("type=xyz&url=")
    assert uri.endswith("&zmin=0&zmax=18")


def test_build_xyz_uri_appends_referer():
    uri = build_xyz_uri(
        _tile(
            "https://t0.tianditu.gov.cn/img_w/wmts?SERVICE=WMTS&TILECOL={x}&tk=KEY",
            referer="https://www.tianditu.gov.cn/",
        )
    )
    assert "&referer=https://www.tianditu.gov.cn/" in uri
    assert uri.endswith("&zmax=18&referer=https://www.tianditu.gov.cn/")


def test_build_xyz_uri_no_referer_by_default():
    uri = build_xyz_uri(_tile("https://example.com/{z}/{x}/{y}.png"))
    assert "referer" not in uri


def test_build_xyz_uri_encodes_inner_ampersand():
    # url 内部的 & 必须被编码，否则连接串会被 QgsDataSourceUri 拆断
    uri = build_xyz_uri(
        _tile("https://t0.tianditu.gov.cn/img_w/wmts?a=1&b=2")
    )
    assert "&a=1" not in uri
    assert "%26" in uri


def test_build_xyz_uri_keeps_placeholders_after_decode():
    tile = _tile(
        "https://t0.tianditu.gov.cn/img_w/wmts?SERVICE=WMTS&TILECOL={x}&TILEROW={y}&TILEMATRIX={z}&tk=KEY"
    )
    uri = build_xyz_uri(tile)
    # 只编码 &，保留 : / ? = {x} 原样，避免 QGIS 把 host 编码坏导致请求失败
    assert "https://t0.tianditu.gov.cn/img_w/wmts" in uri
    assert "{x}" in uri
    assert "%7B" not in uri
    assert "%26" in uri
    url_part = uri.split("&url=", 1)[1].split("&zmin=", 1)[0]
    assert unquote(url_part) == tile.url
