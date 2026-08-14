"""测试端口类型兼容矩阵。"""

from __future__ import annotations

import pytest

from app.core.domain.flow.types import PortType, can_connect, parse_port_type


@pytest.mark.parametrize(
    ("src", "dst"),
    [
        (PortType.VECTOR, PortType.VECTOR),
        (PortType.RASTER, PortType.RASTER),
        (PortType.TABLE, PortType.TABLE),
        (PortType.NUMBER, PortType.NUMBER),
        (PortType.BOOLEAN, PortType.BOOLEAN),
        (PortType.STRING, PortType.STRING),
        (PortType.FILE, PortType.FILE),
        (PortType.FOLDER, PortType.FOLDER),
        (PortType.FIELDS, PortType.FIELDS),
    ],
)
def test_can_connect_exact_match(src, dst):
    assert can_connect(src, dst)


@pytest.mark.parametrize(
    ("src", "dst"),
    [
        # 矢量图层派生：属性表 / 字段列表
        (PortType.VECTOR, PortType.TABLE),
        (PortType.VECTOR, PortType.FIELDS),
        # 字符串路径 ↔ 文件 / 文件夹
        (PortType.STRING, PortType.FILE),
        (PortType.STRING, PortType.FOLDER),
        (PortType.FILE, PortType.STRING),
        (PortType.FOLDER, PortType.STRING),
    ],
)
def test_can_connect_derived(src, dst):
    assert can_connect(src, dst)


@pytest.mark.parametrize(
    ("src", "dst"),
    [
        (PortType.VECTOR, PortType.RASTER),
        (PortType.RASTER, PortType.VECTOR),
        (PortType.NUMBER, PortType.STRING),
        (PortType.STRING, PortType.NUMBER),
        (PortType.BOOLEAN, PortType.NUMBER),
        (PortType.FIELDS, PortType.VECTOR),
        (PortType.TABLE, PortType.VECTOR),
    ],
)
def test_can_connect_rejects(src, dst):
    assert not can_connect(src, dst)


def test_can_connect_is_directional():
    # 矢量→字段表可以，反向不行
    assert can_connect(PortType.VECTOR, PortType.TABLE)
    assert not can_connect(PortType.TABLE, PortType.VECTOR)


def test_parse_port_type_by_name_and_label():
    assert parse_port_type("VECTOR") is PortType.VECTOR
    assert parse_port_type("矢量图层") is PortType.VECTOR
    assert parse_port_type("NUMBER") is PortType.NUMBER


def test_parse_port_type_unknown_raises():
    with pytest.raises(ValueError):
        parse_port_type("不存在的类型")
