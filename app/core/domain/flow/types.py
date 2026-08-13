"""流程编排：端口类型与类型兼容矩阵。

纯逻辑模块，不依赖 QGIS / Qt，可离线单测。端口类型决定节点间连线是否合法。
"""

from __future__ import annotations

from enum import Enum


class PortType(str, Enum):
    """流程节点端口数据类型。

    值（中文标签）用于 UI 展示，序列化时用 :attr:`name` 保持稳定。
    """

    VECTOR = "矢量图层"
    RASTER = "栅格图层"
    TABLE = "字段表"
    NUMBER = "数值"
    BOOLEAN = "布尔"
    STRING = "字符串"
    FILE = "文件路径"
    FOLDER = "文件夹"
    FIELDS = "多选字段"


#: 源类型 → 可连的目标类型集合。
#: 除精确匹配外，允许派生关系：矢量图层自带属性表/字段，可喂给
#: 「字段表」「多选字段」；字符串路径可当作文件/文件夹使用（反向亦可）。
_COMPAT: dict[PortType, frozenset[PortType]] = {
    PortType.VECTOR: frozenset({PortType.VECTOR, PortType.TABLE, PortType.FIELDS}),
    PortType.RASTER: frozenset({PortType.RASTER}),
    PortType.TABLE: frozenset({PortType.TABLE}),
    PortType.NUMBER: frozenset({PortType.NUMBER}),
    PortType.BOOLEAN: frozenset({PortType.BOOLEAN}),
    PortType.STRING: frozenset({PortType.STRING, PortType.FILE, PortType.FOLDER}),
    PortType.FILE: frozenset({PortType.FILE, PortType.STRING}),
    PortType.FOLDER: frozenset({PortType.FOLDER, PortType.STRING}),
    PortType.FIELDS: frozenset({PortType.FIELDS}),
}


def can_connect(src: PortType, dst: PortType) -> bool:
    """源输出端口类型能否连到目标输入端口。"""
    return dst in _COMPAT.get(src, frozenset())


def parse_port_type(raw: str) -> PortType:
    """按枚举名或中文标签解析端口类型；未知抛 :class:`ValueError`。"""
    for pt in PortType:
        if raw == pt.name or raw == pt.value:
            return pt
    raise ValueError(f"未知端口类型：{raw}")
