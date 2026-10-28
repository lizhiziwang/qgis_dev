"""POS / 外方位元素文件与 DJI ``.MRK`` 的解析。

- 通用 POS：txt / csv / pos，支持表头（中文/英文）与空白分隔；
- DJI MRK：``29.18801917,Lat 116.01441319,Lon 95.201,Ellh`` 这种行。

只解析经纬度、高程、相对航高与航向；不处理投影坐标 X/Y。
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from pathlib import Path

_MRK_RE = re.compile(
    r"(?P<lat>-?\d+(?:\.\d+)?)\s*,\s*Lat\s+"
    r"(?P<lon>-?\d+(?:\.\d+)?)\s*,\s*Lon\s+"
    r"(?P<alt>-?\d+(?:\.\d+)?)\s*,\s*Ellh",
    re.IGNORECASE,
)

_HEADER_KEYS: dict[str, tuple[str, ...]] = {
    "name": ("点名", "点号", "名称", "影像", "文件", "name", "file", "id"),
    "lon": ("经度", "lon", "lng", "longitude"),
    "lat": ("纬度", "lat", "latitude"),
    "alt": ("高程", "高度", "椭球", "alt", "altitude", "ellh", "z"),
    "relative": ("相对", "relative", "agl"),
    "yaw": ("航向", "yaw", "heading"),
}


@dataclass(frozen=True)
class PositionRecord:
    """一条定位记录。"""

    name: str = ""
    lon: float | None = None
    lat: float | None = None
    altitude: float | None = None
    relative_altitude: float | None = None
    yaw: float | None = None
    seq: int = 0

    @property
    def has_lonlat(self) -> bool:
        return self.lon is not None and self.lat is not None


@dataclass
class PositionFile:
    """解析结果。``ordered`` 用于按航片顺序回退匹配。"""

    path: str = ""
    records: dict[str, PositionRecord] = field(default_factory=dict)
    ordered: list[PositionRecord] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)

    def lookup(self, image_name: str) -> PositionRecord | None:
        key = _name_key(image_name)
        record = self.records.get(key)
        if record is not None:
            return record
        # 有些 POS 用带扩展名的文件名，有些用不带扩展名的
        stem_key = _name_key(Path(image_name).stem)
        return self.records.get(stem_key)


def _name_key(name: str) -> str:
    return os.path.normcase(Path(str(name).strip()).stem or str(name).strip())


def find_mrk_file(folder: str) -> str:
    """在影像目录里找第一个 ``.MRK``（DJI 的 Timestamp.MRK）。"""
    try:
        entries = os.scandir(folder)
    except OSError:
        return ""
    try:
        candidates = [
            entry.path
            for entry in entries
            if entry.is_file() and entry.name.lower().endswith(".mrk")
        ]
    finally:
        entries.close()
    return sorted(candidates)[0] if candidates else ""


def _read_lines(path: str) -> list[str]:
    text = Path(path).read_text(encoding="utf-8-sig", errors="replace")
    lines = []
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or line.startswith("//"):
            continue
        lines.append(line)
    return lines


def _split_line(line: str, delimiter: str | None) -> list[str]:
    if delimiter is None:
        return line.split()
    return [token.strip() for token in line.split(delimiter)]


def _detect_delimiter(line: str) -> str | None:
    if "\t" in line:
        return "\t"
    if "," in line:
        return ","
    if ";" in line:
        return ";"
    return None


def _match_header(cell: str) -> str | None:
    lowered = cell.strip().lower()
    if not lowered:
        return None
    for field_name, keys in _HEADER_KEYS.items():
        if any(key in lowered for key in keys):
            return field_name
    return None


def _looks_like_header(cells: list[str]) -> bool:
    matches = sum(1 for cell in cells if _match_header(cell))
    return matches >= 2


def _to_float(text: str) -> float | None:
    try:
        return float(str(text).strip())
    except (TypeError, ValueError):
        return None


def _header_mapping(cells: list[str]) -> dict[str, int]:
    mapping: dict[str, int] = {}
    for index, cell in enumerate(cells):
        field_name = _match_header(cell)
        if field_name and field_name not in mapping:
            mapping[field_name] = index
    return mapping


def _row_from_mapping(
    cells: list[str], mapping: dict[str, int], seq: int, fallback_name: str
) -> PositionRecord:
    def value(key: str) -> str | None:
        index = mapping.get(key)
        if index is None or index >= len(cells):
            return None
        return cells[index]

    name = value("name") or fallback_name
    lon = _to_float(value("lon") or "")
    lat = _to_float(value("lat") or "")
    if lon is not None and lat is not None and abs(lon) <= 90 and abs(lat) > 90:
        lon, lat = lat, lon
    return PositionRecord(
        name=name,
        lon=lon,
        lat=lat,
        altitude=_to_float(value("alt") or ""),
        relative_altitude=_to_float(value("relative") or ""),
        yaw=_to_float(value("yaw") or ""),
        seq=seq,
    )


def _row_without_header(cells: list[str], seq: int, fallback_name: str) -> PositionRecord:
    name = fallback_name
    numbers: list[float] = []
    for cell in cells:
        number = _to_float(cell)
        if number is None:
            if len(cells) > 1 and cell == cells[0]:
                name = cell
            continue
        numbers.append(number)
    lon = numbers[0] if numbers else None
    lat = numbers[1] if len(numbers) > 1 else None
    if lon is not None and lat is not None and abs(lon) <= 90 and abs(lat) > 90:
        lon, lat = lat, lon
    return PositionRecord(
        name=name,
        lon=lon,
        lat=lat,
        altitude=numbers[2] if len(numbers) > 2 else None,
        relative_altitude=numbers[3] if len(numbers) > 3 else None,
        yaw=numbers[4] if len(numbers) > 4 else None,
        seq=seq,
    )


def parse_position_file(path: str) -> PositionFile:
    """解析通用 POS / 外方位元素文件。"""
    result = PositionFile(path=path)
    try:
        lines = _read_lines(path)
    except OSError as exc:
        result.errors.append(f"无法读取位置文件：{exc}")
        return result
    if not lines:
        result.errors.append("位置文件为空")
        return result

    delimiter = _detect_delimiter(lines[0])
    first_cells = _split_line(lines[0], delimiter)
    mapping: dict[str, int] = {}
    data_lines = lines
    if _looks_like_header(first_cells):
        mapping = _header_mapping(first_cells)
        data_lines = lines[1:]
        if "lon" not in mapping or "lat" not in mapping:
            result.errors.append("表头中未找到经度/纬度列")
            return result

    for offset, line in enumerate(data_lines):
        cells = _split_line(line, delimiter)
        if len(cells) < 2:
            continue
        if mapping:
            record = _row_from_mapping(cells, mapping, offset, f"IMG_{offset + 1}")
        else:
            record = _row_without_header(cells, offset, f"IMG_{offset + 1}")
        if not record.has_lonlat:
            result.errors.append(f"第 {offset + 1} 行：缺少有效的经纬度")
            continue
        if not (-180 <= record.lon <= 180 and -90 <= record.lat <= 90):
            result.errors.append(f"第 {offset + 1} 行：经纬度超出范围")
            continue
        result.ordered.append(record)
        if record.name:
            result.records[_name_key(record.name)] = record
    if not result.ordered:
        result.errors.append("位置文件里没有可用的定位记录")
    return result


def parse_mrk(path: str) -> PositionFile:
    """解析 DJI ``.MRK``；按文件行序输出。"""
    result = PositionFile(path=path)
    try:
        lines = _read_lines(path)
    except OSError as exc:
        result.errors.append(f"无法读取 MRK：{exc}")
        return result
    for line in lines:
        match = _MRK_RE.search(line)
        if match is None:
            continue
        record = PositionRecord(
            name=f"MRK_{len(result.ordered) + 1}",
            lon=float(match.group("lon")),
            lat=float(match.group("lat")),
            altitude=float(match.group("alt")),
            seq=len(result.ordered),
        )
        result.ordered.append(record)
    if not result.ordered:
        result.errors.append("MRK 中没有可识别的定位行")
    return result


__all__ = [
    "PositionFile",
    "PositionRecord",
    "find_mrk_file",
    "parse_mrk",
    "parse_position_file",
]
