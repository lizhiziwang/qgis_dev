"""检查生成的矢量瓦片：读取 pbf 的图层名与要素数。

用途有两个：

* 网页查看器的样式键必须是 **pbf 内部的源图层名**。算法实际写入的名字
  可能带扩展名、中文或 ``/``，不能靠 ``layer.name()`` 猜；
* 低缩放级别下面/线要素会被简化掉，可能生成一堆「只有图层定义、没有要素」
  的 pbf。此时查看器必然空白，界面应直接提示，而不是让用户对着空白页猜。

这里实现的是 MVT（Mapbox Vector Tile）protobuf 的最小读取逻辑，只取
``Tile.layers[].name`` 和 ``Layer.features`` 数量，不依赖额外库。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path

logger = logging.getLogger(__name__)

#: 单次检查最多读取多少个瓦片（大任务无需为预览扫全量）
MAX_SCAN_FILES = 500

# protobuf wire type
_VARINT = 0
_FIXED64 = 1
_LENGTH = 2
_FIXED32 = 5


def _read_varint(data: bytes, pos: int) -> tuple[int, int]:
    value = 0
    shift = 0
    while pos < len(data):
        byte = data[pos]
        pos += 1
        value |= (byte & 0x7F) << shift
        if not byte & 0x80:
            return value, pos
        shift += 7
        if shift > 63:
            break
    raise ValueError("varint 越界或过长")


def _skip_field(data: bytes, pos: int, wire_type: int) -> int:
    if wire_type == _VARINT:
        _, pos = _read_varint(data, pos)
    elif wire_type == _FIXED64:
        pos += 8
    elif wire_type == _LENGTH:
        length, pos = _read_varint(data, pos)
        pos += length
    elif wire_type == _FIXED32:
        pos += 4
    else:
        raise ValueError(f"不支持的 wire type: {wire_type}")
    if pos > len(data):
        raise ValueError("字段长度越界")
    return pos


def _read_message_fields(data: bytes, wanted: set[int]) -> dict[int, list[bytes | int]]:
    """读取 protobuf 消息中 ``wanted`` 字段；其余字段跳过。

    返回 ``{字段号: [值]}``，length-delimited 字段返回 ``bytes``，varint 返回
    ``int``。
    """
    result: dict[int, list[bytes | int]] = {number: [] for number in wanted}
    pos = 0
    while pos < len(data):
        key, pos = _read_varint(data, pos)
        number, wire_type = key >> 3, key & 0x07
        if number in wanted:
            if wire_type == _LENGTH:
                length, pos = _read_varint(data, pos)
                value = data[pos : pos + length]
                if len(value) != length:
                    raise ValueError("length-delimited 字段越界")
                pos += length
                result[number].append(value)
            elif wire_type == _VARINT:
                value, pos = _read_varint(data, pos)
                result[number].append(value)
            else:
                pos = _skip_field(data, pos, wire_type)
        else:
            pos = _skip_field(data, pos, wire_type)
    return result


def tile_layers(data: bytes) -> list[tuple[str, int]]:
    """返回一个 MVT 瓦片里的 ``[(图层名, 要素数)]``。"""
    layers: list[tuple[str, int]] = []
    for layer_data in _read_message_fields(data, {3})[3]:
        if not isinstance(layer_data, bytes):
            continue
        fields = _read_message_fields(layer_data, {1, 2})
        name_values = fields[1]
        name = (
            name_values[0].decode("utf-8", errors="replace")
            if name_values and isinstance(name_values[0], bytes)
            else ""
        )
        feature_count = sum(1 for _ in fields[2])
        layers.append((name or "(未命名图层)", feature_count))
    return layers


@dataclass
class VectorTileStats:
    """一次瓦片检查的结果。"""

    tiles_total: int = 0
    tiles_scanned: int = 0
    feature_count: int = 0
    layer_names: list[str] = field(default_factory=list)
    parse_errors: int = 0

    @property
    def has_features(self) -> bool:
        return self.feature_count > 0

    @property
    def truncated(self) -> bool:
        return self.tiles_scanned < self.tiles_total


def inspect_vector_tiles(
    output_dir: str,
    template: str = "{z}/{x}/{y}.pbf",
    *,
    max_files: int = MAX_SCAN_FILES,
) -> VectorTileStats:
    """扫描输出目录里的 pbf，统计图层名与要素数（最多 ``max_files`` 个）。"""
    root = Path(str(output_dir))
    stats = VectorTileStats()
    if not root.is_dir():
        return stats
    suffix = Path(str(template).replace("\\", "/")).suffix or ".pbf"
    files = sorted(root.rglob(f"*{suffix}")) if suffix else sorted(root.rglob("*.pbf"))
    stats.tiles_total = len(files)
    seen: set[str] = set()
    for path in files[: max(1, int(max_files))]:
        try:
            data = path.read_bytes()
            layers = tile_layers(data)
        except (OSError, ValueError):
            stats.parse_errors += 1
            logger.debug("读取矢量瓦片失败：%s", path, exc_info=True)
            continue
        stats.tiles_scanned += 1
        for name, count in layers:
            if name not in seen:
                seen.add(name)
                stats.layer_names.append(name)
            stats.feature_count += count
    return stats


__all__ = ["MAX_SCAN_FILES", "VectorTileStats", "inspect_vector_tiles", "tile_layers"]
