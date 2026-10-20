"""扫描训练产物目录中的 run（纯 stdlib，不 import torch）。

推理页的模型下拉只需要 manifest / metrics / 权重是否存在与少量元数据；
真正校验 checkpoint 结构由 sidecar 的 ``--inspect`` 完成。
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from pathlib import Path

#: 递归查找的最大深度（模型根目录 → run 目录）
MAX_DEPTH = 4
#: 一次最多返回的 run 数（按修改时间倒序）
DEFAULT_LIMIT = 200


@dataclass(frozen=True)
class RunInfo:
    """一个可推理的训练 run。"""

    run_dir: str
    checkpoint: str
    task: str = ""
    model: str = ""
    encoder: str = ""
    tile_size: int = 512
    classes: tuple[dict, ...] = field(default_factory=tuple)
    best_value: float = 0.0
    best_epoch: int = 0
    best_metric: str = ""
    status: str = ""
    modified: float = 0.0

    @property
    def name(self) -> str:
        return Path(self.run_dir).name

    @property
    def summary(self) -> str:
        metric = self.best_metric or "best"
        if self.best_value:
            return f"{self.name} · {self.model} · {metric} {self.best_value:.4f}"
        return f"{self.name} · {self.model}"


def checkpoint_in(run_dir: str | Path) -> str:
    """run 目录下优先 ``best.pt``，否则 ``last.pt``；都没有返回空串。"""
    folder = Path(run_dir)
    for name in ("best.pt", "last.pt"):
        candidate = folder / name
        if candidate.is_file():
            return str(candidate)
    return ""


def read_run(run_dir: str | Path) -> RunInfo | None:
    """读取单个 run；缺文件 / JSON 损坏 / 没有权重时返回 ``None``。"""
    folder = Path(run_dir)
    manifest_path = folder / "manifest.json"
    metrics_path = folder / "metrics.json"
    if not manifest_path.is_file() or not metrics_path.is_file():
        return None
    checkpoint = checkpoint_in(folder)
    if not checkpoint:
        return None
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        metrics = json.loads(metrics_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(manifest, dict) or not isinstance(metrics, dict):
        return None

    params = manifest.get("params") if isinstance(manifest.get("params"), dict) else {}
    classes_raw = metrics.get("classes")
    if not isinstance(classes_raw, list):
        classes_raw = (manifest.get("dataset") or {}).get("classes") or []
    classes = tuple(dict(item) for item in classes_raw if isinstance(item, dict))

    try:
        best_value = float(metrics.get("best_value") or 0.0)
    except (TypeError, ValueError):
        best_value = 0.0
    try:
        best_epoch = int(metrics.get("best_epoch") or 0)
    except (TypeError, ValueError):
        best_epoch = 0
    try:
        tile_size = int(params.get("tile_size") or 512)
    except (TypeError, ValueError):
        tile_size = 512

    try:
        modified = max(
            manifest_path.stat().st_mtime,
            metrics_path.stat().st_mtime,
            Path(checkpoint).stat().st_mtime,
        )
    except OSError:
        modified = 0.0

    return RunInfo(
        run_dir=str(folder),
        checkpoint=checkpoint,
        task=str(metrics.get("task") or manifest.get("task") or ""),
        model=str(metrics.get("model") or manifest.get("model") or ""),
        encoder=str(metrics.get("encoder") or manifest.get("encoder") or ""),
        tile_size=tile_size,
        classes=classes,
        best_value=best_value,
        best_epoch=best_epoch,
        best_metric=str(metrics.get("best_metric") or ""),
        status=str(metrics.get("status") or ""),
        modified=modified,
    )


def scan_runs(
    root: str | Path,
    *,
    max_depth: int = MAX_DEPTH,
    limit: int = DEFAULT_LIMIT,
) -> list[RunInfo]:
    """在模型根目录下查找训练 run，按修改时间倒序返回。"""
    base = Path(root)
    if not base.is_dir():
        return []

    found: list[RunInfo] = []

    def walk(folder: Path, depth: int) -> None:
        info = read_run(folder)
        if info is not None:
            found.append(info)
            return
        if depth >= max_depth:
            return
        try:
            entries = sorted(os.scandir(folder), key=lambda item: item.name)
        except OSError:
            return
        for entry in entries:
            if not entry.is_dir(follow_symlinks=False) or entry.name.startswith("."):
                continue
            walk(Path(entry.path), depth + 1)

    walk(base, 0)
    found.sort(key=lambda item: item.modified, reverse=True)
    return found[: max(0, int(limit))]


__all__ = [
    "DEFAULT_LIMIT",
    "MAX_DEPTH",
    "RunInfo",
    "checkpoint_in",
    "read_run",
    "scan_runs",
]
