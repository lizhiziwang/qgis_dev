"""GCP 控制点文件的导入 / 刺点保存。

支持带表头或不带表头的 txt / csv：

- 表头：``点号,东坐标,北坐标,高程,类型``（中英文均可，顺序不限）；
- 无表头：``点号 东坐标 北坐标 [高程] [类型]``。

刺点结果存到控制点文件旁边的 ``<文件名>.marks.json``，只记录原始像素坐标。
"""

from __future__ import annotations

import json
import os
from pathlib import Path

from app.core.uav.models import GcpPoint, GcpSet

_HEADER_KEYS: dict[str, tuple[str, ...]] = {
    "name": ("点名", "点号", "名称", "name", "point", "id"),
    "east": ("东坐标", "东", "east", "e"),
    "north": ("北坐标", "北", "north", "n"),
    "height": ("高程", "高", "height", "h", "z", "elevation"),
    "role": ("类型", "role", "type"),
}

_SCHEMA_VERSION = 1


def marks_path(gcp_path: str) -> str:
    path = Path(gcp_path)
    return str(path.with_name(f"{path.stem}.marks.json"))


def _read_lines(path: str) -> list[str]:
    text = Path(path).read_text(encoding="utf-8-sig", errors="replace")
    lines = []
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or line.startswith("//"):
            continue
        lines.append(line)
    return lines


def _detect_delimiter(line: str) -> str | None:
    if "\t" in line:
        return "\t"
    if "," in line:
        return ","
    if ";" in line:
        return ";"
    return None


def _split_line(line: str, delimiter: str | None) -> list[str]:
    if delimiter is None:
        return line.split()
    return [token.strip() for token in line.split(delimiter)]


def _cell_matches(cell: str, key: str) -> bool:
    lowered = cell.strip().lower()
    if not lowered:
        return False
    if key.isascii() and len(key) <= 2:
        return lowered == key
    return key in lowered


def _match_header(cell: str) -> str | None:
    for field_name, keys in _HEADER_KEYS.items():
        if any(_cell_matches(cell, key) for key in keys):
            return field_name
    return None


def _looks_like_header(cells: list[str]) -> bool:
    return sum(1 for cell in cells if _match_header(cell)) >= 2


def _to_float(text: str | None) -> float | None:
    if text is None:
        return None
    try:
        return float(str(text).strip())
    except (TypeError, ValueError):
        return None


def _normalize_role(text: str) -> str:
    lowered = text.strip().lower()
    if "检查" in text or "check" in lowered:
        return "检查点"
    return "控制点"


def parse_gcp_file(path: str) -> GcpSet:
    """解析控制点文件；解析失败的行记入 ``GcpSet.errors``。"""
    result = GcpSet(source_path=path)
    try:
        lines = _read_lines(path)
    except OSError as exc:
        result.errors.append(f"无法读取控制点文件：{exc}")
        return result
    if not lines:
        result.errors.append("控制点文件为空")
        return result

    delimiter = _detect_delimiter(lines[0])
    first_cells = _split_line(lines[0], delimiter)
    mapping: dict[str, int] = {}
    data_lines = lines
    if _looks_like_header(first_cells):
        for index, cell in enumerate(first_cells):
            field_name = _match_header(cell)
            if field_name and field_name not in mapping:
                mapping[field_name] = index
        missing = [key for key in ("name", "east", "north") if key not in mapping]
        if missing:
            result.errors.append("表头缺少必要的列：点号 / 东坐标 / 北坐标")
            return result
        data_lines = lines[1:]

    for offset, line in enumerate(data_lines):
        cells = _split_line(line, delimiter)
        line_no = offset + (2 if mapping else 1)
        if mapping:
            values = {key: (cells[index] if index < len(cells) else "") for key, index in mapping.items()}
            name = values.get("name", "").strip()
            east = _to_float(values.get("east"))
            north = _to_float(values.get("north"))
            height = _to_float(values.get("height")) or 0.0
            role = _normalize_role(values.get("role", ""))
        else:
            if len(cells) < 3:
                result.errors.append(f"第 {line_no} 行：至少需要 点号 / 东坐标 / 北坐标")
                continue
            name = cells[0].strip()
            east = _to_float(cells[1])
            north = _to_float(cells[2])
            height = _to_float(cells[3]) if len(cells) > 3 else 0.0
            role = _normalize_role(cells[4]) if len(cells) > 4 else "控制点"
        if not name:
            result.errors.append(f"第 {line_no} 行：点号为空")
            continue
        if east is None or north is None:
            result.errors.append(f"第 {line_no} 行：东/北坐标不是数字")
            continue
        if result.get(name) is not None:
            result.errors.append(f"第 {line_no} 行：点号重复（{name}）")
            continue
        result.points.append(
            GcpPoint(
                name=name,
                east=east,
                north=north,
                height=height or 0.0,
                role=role,
            )
        )
    if not result.points and not result.errors:
        result.errors.append("控制点文件里没有有效记录")
    return result


def load_marks(gcp_set: GcpSet) -> int:
    """把 ``<gcp>.marks.json`` 里已有的刺点读回 ``gcp_set``，返回读取的刺点数。"""
    if not gcp_set.source_path:
        return 0
    path = marks_path(gcp_set.source_path)
    if not os.path.isfile(path):
        return 0
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return 0
    if not isinstance(data, dict):
        return 0
    points = data.get("points")
    if not isinstance(points, list):
        return 0
    count = 0
    for item in points:
        if not isinstance(item, dict):
            continue
        point = gcp_set.get(str(item.get("name", "")))
        if point is None:
            continue
        marks = item.get("marks")
        if not isinstance(marks, dict):
            continue
        for image_name, coords in marks.items():
            if not isinstance(coords, (list, tuple)) or len(coords) < 2:
                continue
            try:
                point.marks[str(image_name)] = (float(coords[0]), float(coords[1]))
                count += 1
            except (TypeError, ValueError):
                continue
    return count


def save_marks(gcp_set: GcpSet) -> str:
    """原子写回刺点文件，返回路径。"""
    if not gcp_set.source_path:
        raise ValueError("未指定控制点文件路径")
    path = marks_path(gcp_set.source_path)
    payload = {
        "schema_version": _SCHEMA_VERSION,
        "gcp_file": os.path.abspath(gcp_set.source_path),
        "points": [
            {
                "name": point.name,
                "marks": {
                    image_name: [round(px, 2), round(py, 2)]
                    for image_name, (px, py) in point.marks.items()
                },
            }
            for point in gcp_set.points
            if point.marks
        ],
    }
    text = json.dumps(payload, ensure_ascii=False, indent=2)
    tmp_path = f"{path}.tmp"
    Path(tmp_path).write_text(text, encoding="utf-8")
    os.replace(tmp_path, path)
    return path


def validate_gcp(gcp_set: GcpSet | None) -> list[str]:
    """刺点完整性检查，返回中文提示（空列表 = 没问题）。"""
    if gcp_set is None or not gcp_set.points:
        return ["尚未导入控制点"]
    messages = []
    unmarked = [point.name for point in gcp_set.points if not point.marks]
    if unmarked:
        messages.append(f"{len(unmarked)} 个点尚未刺点：{'、'.join(unmarked[:5])}")
    sparse = [
        point.name for point in gcp_set.points if point.marks and len(point.marks) < 2
    ]
    if sparse:
        messages.append(f"{len(sparse)} 个点只刺了 1 张影像：{'、'.join(sparse[:5])}")
    return messages


__all__ = [
    "load_marks",
    "marks_path",
    "parse_gcp_file",
    "save_marks",
    "validate_gcp",
]
