"""落盘布局与 zip 打包（纯逻辑，可 headless 单测）。

打包规范见方案 §6.1（矢量容器已按本项目约定改为 **SpatiaLite .sqlite 数据库**，
不再用 FileGDB——QGIS 官方构建缺 FileGDB 写驱动，只有 OpenFileGDB 只读）：
zip 内 ``矢量.sqlite``（矢量，SpatiaLite）＋ ``影像/<数据集>/<时相>/tiles`` ＋
``文件/<数据集>/``，根下 ``manifest.json``。库内图层名用 ASCII（由 ``name_map``
还原中文显示名，见 §11 风险表）。
"""

from __future__ import annotations

import os
import re
import shutil
import time
from datetime import datetime

from .model import FetchPlan

#: 矢量数据库文件名（zip/磁盘根下，SQLite + SpatiaLite，扩展名沿用项目 .db 习惯）
SQLITE_CONTAINER = "矢量.db"

#: 影像 phase → 中文数据集目录
PHASE_DIRS = {
    "before": "前时相",
    "after": "后时相",
    "baseline": "本底数据",
    "reference": "参考影像",
    "imagery": "影像",
}

#: 关键矢量 dataset key → 库内图层名（ASCII）
_FEATURE_CLASS_ASCII = {
    "features": "tuban",
    "admin_boundary": "xingzheng_bianjie",
    "reference": "cankao",
}


def safe_name(name: str, fallback: str = "unnamed", max_len: int = 60) -> str:
    """归一成安全的目录/文件名（去 OS 非法字符；中文/数字开头均可保留）。"""
    s = str(name or "").strip()
    if not s:
        s = fallback
    s = re.sub(r"[\\/:*?\"<>|\x00-\x1f]+", "_", s)  # 非法文件名字符 → _
    s = s.replace(" ", "_").strip("._")
    if not s:
        s = fallback
    return s[:max_len]


def ascii_name(name: str, fallback: str = "unnamed") -> str:
    """纯 ASCII 落盘名（SpatiaLite 图层名 / 跨平台安全），中文转下划线缩写。"""
    s = str(name or "").strip()
    if not s:
        s = fallback
    s = re.sub(r"\s+", "_", s)
    s = re.sub(r"[^A-Za-z0-9_.-]+", "_", s).strip("._")
    if not re.match(r"[A-Za-z]", s):
        s = "n_" + s
    return s[:48] or fallback


def phase_dir(phase: Optional[str]) -> str:
    return PHASE_DIRS.get(phase or "imagery", PHASE_DIRS["imagery"])


def imagery_target(phase: Optional[str], scene: str) -> str:
    """影像落点相对路径：``影像/<数据集目录>/<时相>/tiles``。"""
    return os.path.join("影像", phase_dir(phase), safe_name(scene), "tiles").replace("\\", "/")


def files_target(key: str, label: str) -> str:
    return f"文件/{safe_name(label)}"


def feature_class(key: str, label: str) -> str:
    """矢量 → SpatiaLite .sqlite 库内图层 ASCII 名。"""
    return _FEATURE_CLASS_ASCII.get(key) or ascii_name(label)


def zip_base_name(task_id: str, task_name: str) -> str:
    """zip 文件名（不含扩展名）：``<taskId>_<taskName>_<时间戳>``。"""
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    return f"{safe_name(task_id, 'task')}_{safe_name(task_name, 'task')}_{stamp}"


def ensure_stage(plan: FetchPlan) -> str:
    """建 staging 根与影像/文件目录（矢量 .sqlite 由抓取器用 SQLite+SpatiaLite
    驱动创建，不在此处预建空文件）。"""
    if not plan.stage_root:
        raise ValueError("plan.stage_root 未设置")
    os.makedirs(plan.stage_root, exist_ok=True)
    for item in plan.items:
        if item.kind in ("imagery", "files") and item.target:
            os.makedirs(
                os.path.join(plan.stage_root, item.target.replace("/", os.sep)),
                exist_ok=True,
            )
    return plan.stage_root


def write_manifest(stage_root: str, manifest: dict) -> str:
    import json

    path = os.path.join(stage_root, "manifest.json")
    with open(path, "w", encoding="utf-8") as f:
        json.dump(manifest, f, ensure_ascii=False, indent=2)
    return path


def build_manifest_dict(plan: FetchPlan, results: dict) -> dict:
    """组装 manifest.json（方案 §6.1）。``results``: item 标识 → 元数据字典。"""
    x1, y1, x2, y2 = plan.scope_wgs84
    datasets = []
    for item in plan.items:
        meta = dict(results.get(item.label, results.get(item.target, {})) or {})
        base = {"label": item.label, "kind": item.kind, "source_type": item.source_type}
        if item.service_url:
            base["service_url"] = item.service_url
        if item.phase:
            base["phase"] = item.phase
        if item.kind == "vector":
            base.update(
                feature_class=item.target,
                display_name=item.display_name or item.label,
                size_bytes=meta.get("size_bytes"),
                feature_count=meta.get("feature_count"),
                crs=meta.get("crs"),
                sha256=meta.get("sha256"),
            )
        elif item.kind == "imagery":
            base.update(
                dir=item.target,
                zmin=item.zmin,
                zmax=item.zmax,
                tile_count=meta.get("tile_count"),
                size_bytes=meta.get("size_bytes"),
                sha256=meta.get("sha256"),
            )
        else:  # files
            base.update(dir=item.target, entries=meta.get("entries") or [])
        datasets.append({k: v for k, v in base.items() if v is not None})
    return {
        "schema": 1,
        "task_id": plan.task_id,
        "task_name": plan.task_name,
        "created_at": datetime.now().isoformat(timespec="seconds"),
        "vector_container": SQLITE_CONTAINER,
        "spatial_scope": {
            "type": plan.scope_type,
            "extent_wgs84": [x1, y1, x2, y2],
        },
        "datasets": datasets,
    }


def make_zip(stage_root: str, target_dir: str, zip_name: str) -> str:
    """把 staging 整树打成 ``<target_dir>/<zip_name>.zip``，返回 zip 路径。

    staging 结构即 zip 内结构（root_dir=stage_root），打包成功后由调用方删除 staging。
    """
    os.makedirs(target_dir, exist_ok=True)
    base = os.path.join(target_dir, zip_name.rstrip(".zip"))
    archive = shutil.make_archive(base, "zip", root_dir=stage_root)
    return str(archive)


def stage_timestamp() -> int:
    return int(time.time())
