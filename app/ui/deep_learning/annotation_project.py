"""标注工程文件：保存影像清单、宫格参数、标签、进度与上次位置。"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from pathlib import Path

PROJECT_VERSION = 1
PROJECT_SUFFIX = ".qann"
PROJECT_FILTER = "标注工程 (*.qann)"
PROJECT_TYPE = "qgis_dev.annotation_project"


@dataclass
class ProjectImage:
    """工程中的一张影像。``grid_tile_size > 0`` 表示启用宫格。"""

    path: str
    grid_tile_size: int = 0
    grid_overlap_px: int = 0
    annotated: bool = False
    done: int = 0
    total: int = 0

    def grid_settings(self) -> tuple[int, int] | None:
        if self.grid_tile_size <= 0:
            return None
        return self.grid_tile_size, max(0, self.grid_overlap_px)


@dataclass
class AnnotationProject:
    images: list[ProjectImage] = field(default_factory=list)
    labels: list[dict] = field(default_factory=list)
    current: str = ""
    current_tile: tuple[int, int] | None = None
    output_dir: str = ""
    last_grid_tile_size: int = 512
    last_grid_overlap_percent: int = 10
    selected_label_id: int = 0
    version: int = PROJECT_VERSION


def _as_int(value, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _encode_path(value: str, base: Path | None) -> str:
    """工程目录内的文件存相对路径，工程整体搬移后仍能打开。"""
    if not value:
        return ""
    path = Path(value)
    if base is not None:
        try:
            return path.resolve().relative_to(base.resolve()).as_posix()
        except (OSError, ValueError):
            pass
    return str(path)


def _decode_path(value, base: Path | None) -> str:
    text = str(value or "").strip()
    if not text:
        return ""
    path = Path(text)
    if base is not None and not path.is_absolute():
        path = base / path
    return str(path)


def project_to_dict(project: AnnotationProject, base: Path | None = None) -> dict:
    images = []
    for image in project.images:
        images.append(
            {
                "path": _encode_path(image.path, base),
                "grid": (
                    {
                        "tile_size": image.grid_tile_size,
                        "overlap_px": image.grid_overlap_px,
                    }
                    if image.grid_tile_size > 0
                    else None
                ),
                "annotated": bool(image.annotated),
                "progress": {"done": max(0, image.done), "total": max(0, image.total)},
            }
        )
    return {
        "type": PROJECT_TYPE,
        "version": PROJECT_VERSION,
        "images": images,
        "labels": [
            {
                "id": _as_int(item.get("id")),
                "name": str(item.get("name") or ""),
                "color": str(item.get("color") or ""),
            }
            for item in project.labels
            if isinstance(item, dict) and str(item.get("name") or "").strip()
        ],
        "current": _encode_path(project.current, base),
        "current_tile": (
            [_as_int(project.current_tile[0]), _as_int(project.current_tile[1])]
            if project.current_tile is not None
            else None
        ),
        "output_dir": _encode_path(project.output_dir, base),
        "last_grid": {
            "tile_size": _as_int(project.last_grid_tile_size, 512),
            "overlap_percent": _as_int(project.last_grid_overlap_percent, 10),
        },
        "selected_label_id": _as_int(project.selected_label_id),
    }


def project_from_dict(data, base: Path | None = None) -> AnnotationProject:
    if not isinstance(data, dict):
        raise ValueError("标注工程格式无效")
    version = _as_int(data.get("version"), 0)
    if version > PROJECT_VERSION:
        raise ValueError(f"标注工程版本过新（{version}），请升级软件后再打开")

    images: list[ProjectImage] = []
    for item in data.get("images") or []:
        if not isinstance(item, dict):
            continue
        path = _decode_path(item.get("path"), base)
        if not path:
            continue
        grid = item.get("grid") if isinstance(item.get("grid"), dict) else {}
        progress = item.get("progress") if isinstance(item.get("progress"), dict) else {}
        images.append(
            ProjectImage(
                path=path,
                grid_tile_size=max(0, _as_int(grid.get("tile_size"))),
                grid_overlap_px=max(0, _as_int(grid.get("overlap_px"))),
                annotated=bool(item.get("annotated")),
                done=max(0, _as_int(progress.get("done"))),
                total=max(0, _as_int(progress.get("total"))),
            )
        )

    labels = [item for item in data.get("labels") or [] if isinstance(item, dict)]

    current_tile = data.get("current_tile")
    tile: tuple[int, int] | None = None
    if isinstance(current_tile, (list, tuple)) and len(current_tile) >= 2:
        tile = (max(0, _as_int(current_tile[0])), max(0, _as_int(current_tile[1])))

    last_grid = data.get("last_grid") if isinstance(data.get("last_grid"), dict) else {}
    return AnnotationProject(
        images=images,
        labels=labels,
        current=_decode_path(data.get("current"), base),
        current_tile=tile,
        output_dir=_decode_path(data.get("output_dir"), base),
        last_grid_tile_size=max(0, _as_int(last_grid.get("tile_size"), 512)),
        last_grid_overlap_percent=max(0, _as_int(last_grid.get("overlap_percent"), 10)),
        selected_label_id=max(0, _as_int(data.get("selected_label_id"))),
        version=version or PROJECT_VERSION,
    )


def save_project(path: str | Path, project: AnnotationProject) -> Path:
    """原子写入工程文件（``.tmp`` + ``os.replace``）。"""
    target = Path(path)
    payload = project_to_dict(project, base=target.parent)
    target.parent.mkdir(parents=True, exist_ok=True)
    temp = target.with_name(target.name + ".tmp")
    temp.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    os.replace(temp, target)
    return target


def load_project(path: str | Path) -> AnnotationProject:
    source = Path(path)
    try:
        data = json.loads(source.read_text(encoding="utf-8"))
    except OSError as exc:
        raise ValueError(f"无法读取标注工程：{exc}") from exc
    except json.JSONDecodeError as exc:
        raise ValueError(f"标注工程不是有效的 JSON：{exc}") from exc
    return project_from_dict(data, base=source.parent)
