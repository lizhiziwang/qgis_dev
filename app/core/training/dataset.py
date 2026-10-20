"""扫描标注成果，生成训练样本清单（纯标准库）。

支持标注页保存过的三种布局：

1. 单图：``root/<stem>_mask.png`` + ``<stem>_mask.json``，影像取 JSON ``image``
   或同目录同名影像；
2. 宫格 paired（当前格式）：``<stem>_grid/images/rNN_cNN.png`` +
   ``<stem>_grid/masks/rNN_cNN.png``（JSON 与掩膜同名）；
3. 宫格 legacy：``<stem>_grid/rNN_cNN_mask.png``，影像按 JSON ``grid`` 的
   x/y/width/height 从源影像裁剪。

每个样本携带 ``class_map``（该掩膜原始 id → 全局类别 id）。不同标注会话的
JSON 标签顺序可能不同，不能把掩膜原始 id 直接当类别号；训练时按名字对齐。
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from pathlib import Path

IMAGE_SUFFIXES: tuple[str, ...] = (".png", ".tif", ".tiff", ".jpg", ".jpeg", ".bmp")

#: JSON 缺颜色时给类别补色（与标注页默认调色板一致）
FALLBACK_COLORS: tuple[str, ...] = (
    "#e6194b",
    "#3cb44b",
    "#ffe119",
    "#4363d8",
    "#f58231",
    "#911eb4",
    "#46f0f0",
    "#f032e6",
    "#bcf60c",
    "#fabebe",
    "#008080",
    "#e6beff",
    "#9a6324",
    "#fffac8",
    "#800000",
    "#aaffc3",
)


@dataclass(frozen=True)
class TrainingSample:
    """一张影像 + 一张掩膜。``crop`` 只作用于影像（掩膜已是切片）。"""

    image: str
    mask: str
    crop: tuple[int, int, int, int] | None = None
    #: 掩膜原始 id → 全局类别 id；空表示按类别列表同名下标推断
    class_map: tuple[tuple[int, int], ...] = ()

    def as_dict(self) -> dict:
        payload: dict = {"image": self.image, "mask": self.mask}
        payload["crop"] = list(self.crop) if self.crop else None
        payload["class_map"] = {str(raw): int(global_id) for raw, global_id in self.class_map}
        return payload


@dataclass
class DatasetScan:
    """一次目录扫描结果。"""

    samples: list[TrainingSample] = field(default_factory=list)
    #: ``[{"id": 1, "name": "建筑", "color": "#e6194b"}, ...]``
    classes: list[dict] = field(default_factory=list)
    roots: list[str] = field(default_factory=list)
    skipped: list[str] = field(default_factory=list)

    @property
    def image_count(self) -> int:
        return len({sample.image for sample in self.samples})

    @property
    def mask_count(self) -> int:
        return len(self.samples)

    def summary(self) -> str:
        if not self.samples:
            return "没有找到可训练的影像 / 掩膜"
        text = f"{self.image_count} 张影像 / {self.mask_count} 个掩膜"
        if len(self.classes) > 1:
            text += f" / {len(self.classes)} 个类别"
        if self.skipped:
            text += f"（跳过 {len(self.skipped)} 项）"
        return text


@dataclass
class _Entry:
    image: str
    mask: str
    crop: tuple[int, int, int, int] | None
    labels: list[dict]


def _read_json(path: Path) -> dict:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


def _existing(path: Path) -> bool:
    try:
        return path.is_file()
    except OSError:
        return False


def _find_image(directory: Path, stem: str) -> Path | None:
    for suffix in IMAGE_SUFFIXES:
        candidate = directory / f"{stem}{suffix}"
        if _existing(candidate):
            return candidate
    return None


def _crop_from_json(payload: dict) -> tuple[int, int, int, int] | None:
    grid = payload.get("grid")
    if not isinstance(grid, dict):
        return None
    try:
        x = int(grid.get("x"))
        y = int(grid.get("y"))
        width = int(grid.get("width"))
        height = int(grid.get("height"))
    except (TypeError, ValueError):
        return None
    if width <= 0 or height <= 0:
        return None
    return x, y, width, height


def _source_image(directory: Path, payload: dict) -> Path | None:
    """源影像：优先 JSON ``image``，否则 ``<目录名去掉 _grid>`` 同名文件。"""
    image_text = str(payload.get("image") or "").strip()
    if image_text:
        candidate = Path(image_text)
        if _existing(candidate):
            return candidate
        # JSON 里是另一台机器/被移动过的绝对路径：按文件名回目录里找
        sibling = directory / candidate.name
        if _existing(sibling):
            return sibling
    stem = directory.name
    if stem.lower().endswith("_grid"):
        stem = stem[:-5]
    # 宫格目录本身没有源影像时，再到它的上一级找（单图与宫格混放的常见布局）
    for folder in (directory, directory.parent):
        found = _find_image(folder, stem)
        if found is not None:
            return found
    return None


def _labels_from_json(payload: dict) -> list[dict]:
    labels = payload.get("labels")
    if not isinstance(labels, list):
        return []
    result: list[dict] = []
    for item in labels:
        if not isinstance(item, dict):
            continue
        try:
            label_id = int(item.get("id"))
        except (TypeError, ValueError):
            continue
        name = str(item.get("name") or f"类别{label_id}").strip() or f"类别{label_id}"
        result.append(
            {
                "id": label_id,
                "name": name,
                "color": str(item.get("color") or ""),
            }
        )
    return result


def _paired_image(directory: Path, stem: str) -> Path | None:
    return _find_image(directory / "images", stem)


def _scan_grid_dir(directory: Path, skipped: list[str]) -> list[_Entry]:
    """扫描一个 ``*_grid`` 目录（paired 优先，否则 legacy 平铺掩膜）。"""
    entries: list[_Entry] = []
    masks_dir = directory / "masks"
    paired = False
    try:
        if masks_dir.is_dir() and any(masks_dir.glob("*.png")):
            paired = True
    except OSError:
        paired = False

    if paired:
        for mask in sorted(masks_dir.glob("*.png")):
            payload = _read_json(mask.with_suffix(".json"))
            image = _paired_image(directory, mask.stem)
            if image is not None:
                # images/ 已是切片，crop 不能再套一次
                entries.append(_Entry(str(image), str(mask), None, _labels_from_json(payload)))
                continue
            source = _source_image(directory, payload)
            crop = _crop_from_json(payload)
            if source is not None and crop is not None:
                entries.append(_Entry(str(source), str(mask), crop, _labels_from_json(payload)))
            else:
                skipped.append(f"{mask.name}：缺少同名影像切片且无法按 grid 裁剪")
        return entries

    for mask in sorted(directory.glob("*_mask.png")):
        stem = mask.stem[:-5] if mask.stem.endswith("_mask") else mask.stem
        payload = _read_json(mask.with_suffix(".json"))
        image = _paired_image(directory, stem)
        if image is not None:
            entries.append(_Entry(str(image), str(mask), None, _labels_from_json(payload)))
            continue
        source = _source_image(directory, payload)
        crop = _crop_from_json(payload)
        if source is not None and crop is not None:
            entries.append(_Entry(str(source), str(mask), crop, _labels_from_json(payload)))
        else:
            skipped.append(f"{mask.name}：缺少源影像或 grid 裁剪信息")
    return entries


def _scan_flat_entry(mask: Path, skipped: list[str]) -> _Entry | None:
    stem = mask.stem[:-5] if mask.stem.endswith("_mask") else mask.stem
    payload = _read_json(mask.with_suffix(".json"))
    image = _find_image(mask.parent, stem)
    if image is None:
        image = _source_image(mask.parent, payload)
    if image is None:
        skipped.append(f"{mask.name}：找不到对应影像")
        return None
    crop = _crop_from_json(payload)
    return _Entry(str(image), str(mask), crop, _labels_from_json(payload))


def _looks_like_grid_dir(root: Path) -> bool:
    if root.name.lower().endswith("_grid"):
        return True
    try:
        return (root / "masks").is_dir()
    except OSError:
        return False


def _scan_root(root: Path, skipped: list[str]) -> list[_Entry]:
    if _looks_like_grid_dir(root):
        return _scan_grid_dir(root, skipped)

    entries: list[_Entry] = []
    for dirpath, dirnames, filenames in os.walk(root):
        # images/ 与 masks/ 属于父级 *_grid，已在父级处理，避免重复扫描
        dirnames[:] = sorted(
            name for name in dirnames if name.lower() not in ("images", "masks")
        )
        current = Path(dirpath)
        for name in list(dirnames):
            if name.lower().endswith("_grid"):
                entries.extend(_scan_grid_dir(current / name, skipped))
                dirnames.remove(name)
        for filename in sorted(filenames):
            if not filename.lower().endswith("_mask.png"):
                continue
            entry = _scan_flat_entry(current / filename, skipped)
            if entry is not None:
                entries.append(entry)
    return entries


def _build_classes(entries: list[_Entry]) -> tuple[list[dict], list[TrainingSample]]:
    """按标签名字对齐不同 JSON 的类别 id，并生成全局 class_map。"""
    ordered = sorted(entries, key=lambda item: (item.image.lower(), item.mask.lower()))
    by_name: dict[str, int] = {}
    classes: list[dict] = []
    samples: list[TrainingSample] = []
    for entry in ordered:
        mapping: dict[int, int] = {}
        for label in entry.labels:
            name = label["name"]
            global_id = by_name.get(name)
            if global_id is None:
                global_id = len(classes) + 1
                by_name[name] = global_id
                color = label["color"] or FALLBACK_COLORS[(global_id - 1) % len(FALLBACK_COLORS)]
                classes.append({"id": global_id, "name": name, "color": color})
            mapping.setdefault(int(label["id"]), global_id)
        samples.append(
            TrainingSample(
                image=entry.image,
                mask=entry.mask,
                crop=entry.crop,
                class_map=tuple(sorted(mapping.items())),
            )
        )
    return classes, samples


def _dedupe(samples: list[TrainingSample]) -> list[TrainingSample]:
    seen: set[tuple] = set()
    result: list[TrainingSample] = []
    for sample in samples:
        key = (sample.image, sample.mask, sample.crop)
        if key in seen:
            continue
        seen.add(key)
        result.append(sample)
    return result


def scan_dataset(root: str | Path) -> DatasetScan:
    """扫描标注成果目录。任何 IO 错误都转成 ``skipped``，不抛异常。"""
    raw = str(root or "").strip()
    scan = DatasetScan()
    if not raw:
        scan.skipped.append("尚未选择标注成果目录")
        return scan
    path = Path(raw)
    try:
        if not path.exists():
            scan.skipped.append(f"目录不存在：{path}")
            return scan
        if not path.is_dir():
            scan.skipped.append(f"不是目录：{path}")
            return scan
    except OSError as exc:
        scan.skipped.append(f"无法访问目录：{exc}")
        return scan

    # 用户可能直接选中 images/ 或 masks/，回到所在宫格目录
    if path.name.lower() in ("images", "masks") and path.parent.name.lower().endswith("_grid"):
        path = path.parent
    normalized = path.resolve() if _safe_resolve(path) else path
    scan.roots.append(str(normalized))

    try:
        entries = _scan_root(path, scan.skipped)
    except OSError as exc:
        scan.skipped.append(f"扫描失败：{exc}")
        return scan

    entries = [entry for entry in entries if _existing(Path(entry.image)) and _existing(Path(entry.mask))]
    classes, samples = _build_classes(entries)
    scan.classes = classes
    scan.samples = _dedupe(samples)
    if not scan.samples and not scan.skipped:
        scan.skipped.append("目录里没有 *_mask.png 或 *_grid/masks/*.png")
    return scan


def _safe_resolve(path: Path) -> bool:
    try:
        path.resolve()
    except OSError:
        return False
    return True


__all__ = [
    "FALLBACK_COLORS",
    "IMAGE_SUFFIXES",
    "DatasetScan",
    "TrainingSample",
    "scan_dataset",
]
