"""训练数据集：语义分割与实例分割。

约定：

- 影像统一缩放为 ``tile_size × tile_size``（BILINEAR），掩膜 NEAREST；
- ``crop`` 只裁影像（legacy 宫格掩膜本身已是切片）；
- 类别经 manifest 的 ``class_map`` 映射成连续的 ``1..K``，0 为背景；
- 实例目标由语义掩膜做连通域得到（环境里没有 scipy，用 cv2）。
"""

from __future__ import annotations

import numpy as np

from .common import build_lut, read_mask_ids

IMAGENET_MEAN = np.array([0.485, 0.456, 0.406], dtype=np.float32)
IMAGENET_STD = np.array([0.229, 0.224, 0.225], dtype=np.float32)


def _resampling():
    from PIL import Image

    return getattr(Image, "Resampling", Image).BILINEAR


def _nearest():
    from PIL import Image

    return getattr(Image, "Resampling", Image).NEAREST


def _load_image(sample: dict, tile_size: int) -> np.ndarray:
    """读影像 → 可选裁剪 → 缩放到 tile_size → RGB uint8 数组。"""
    from PIL import Image

    path = str(sample.get("image") or "")
    with Image.open(path) as image:
        image = image.convert("RGB")
        crop = sample.get("crop")
        if crop:
            try:
                x, y, width, height = (int(value) for value in crop)
            except (TypeError, ValueError):
                x = y = width = height = 0
            if width > 0 and height > 0:
                image = image.crop((x, y, x + width, y + height))
        if image.size != (tile_size, tile_size):
            image = image.resize((tile_size, tile_size), _resampling())
        return np.asarray(image, dtype=np.uint8)


def _load_mask(sample: dict, tile_size: int, class_map: dict, num_classes: int) -> np.ndarray:
    """读掩膜 → 类别重映射 → 缩放到 tile_size（最近邻）。"""
    from PIL import Image

    ids = read_mask_ids(sample.get("mask") or "")
    lut = build_lut(class_map, num_classes)
    target = lut[ids]
    if target.shape != (tile_size, tile_size):
        target = np.asarray(
            Image.fromarray(target).resize((tile_size, tile_size), _nearest()),
            dtype=np.uint8,
        )
    return np.ascontiguousarray(target)


def _normalize(image: np.ndarray):
    import torch

    array = image.astype(np.float32) / 255.0
    array = (array - IMAGENET_MEAN) / IMAGENET_STD
    return torch.from_numpy(np.ascontiguousarray(array)).permute(2, 0, 1).float()


def _augment_semantic(image: np.ndarray, target: np.ndarray, rng: np.random.Generator):
    if rng.random() < 0.5:
        image, target = np.fliplr(image), np.fliplr(target)
    if rng.random() < 0.5:
        image, target = np.flipud(image), np.flipud(target)
    if rng.random() < 0.5:
        quarters = int(rng.integers(0, 4))
        if quarters:
            image = np.rot90(image, quarters)
            target = np.rot90(target, quarters)
    return np.ascontiguousarray(image), np.ascontiguousarray(target)


class SemanticDataset:
    """语义分割数据集（torch Dataset 鸭子类型，避免顶层 import torch）。"""

    def __init__(
        self,
        samples: list[dict],
        *,
        tile_size: int,
        num_classes: int,
        augment: bool = False,
        seed: int = 0,
    ) -> None:
        self.samples = list(samples)
        self.tile_size = int(tile_size)
        self.num_classes = int(num_classes)
        self.augment = bool(augment)
        self._seed = int(seed)

    def __len__(self) -> int:
        return len(self.samples)

    def __getitem__(self, index: int):
        sample = self.samples[index]
        image = _load_image(sample, self.tile_size)
        target = _load_mask(
            sample,
            self.tile_size,
            sample.get("class_map") or {},
            self.num_classes,
        )
        if self.augment:
            rng = np.random.default_rng(self._seed + int(index) * 7919)
            image, target = _augment_semantic(image, target, rng)
        import torch

        return _normalize(image), torch.from_numpy(target.astype(np.int64))


class InstanceDataset:
    """实例分割数据集：语义掩膜 → 连通域 boxes / masks / labels。"""

    def __init__(
        self,
        samples: list[dict],
        *,
        tile_size: int,
        num_classes: int,
        min_area: int = 16,
        augment: bool = False,
        seed: int = 0,
    ) -> None:
        self.samples = list(samples)
        self.tile_size = int(tile_size)
        self.num_classes = int(num_classes)
        self.min_area = max(0, int(min_area))
        self.augment = bool(augment)
        self._seed = int(seed)

    def __len__(self) -> int:
        return len(self.samples)

    def __getitem__(self, index: int):
        import torch

        sample = self.samples[index]
        image = _load_image(sample, self.tile_size)
        target = _load_mask(
            sample,
            self.tile_size,
            sample.get("class_map") or {},
            self.num_classes,
        )
        if self.augment:
            rng = np.random.default_rng(self._seed + int(index) * 104729)
            if rng.random() < 0.5:
                image, target = np.fliplr(image), np.fliplr(target)
            if rng.random() < 0.5:
                image, target = np.flipud(image), np.flipud(target)
            image = np.ascontiguousarray(image)
            target = np.ascontiguousarray(target)

        boxes, masks, labels = _instances_from_mask(target, self.min_area)
        tensor = torch.from_numpy(np.ascontiguousarray(image.transpose(2, 0, 1))).float() / 255.0
        # 先转成整块 ndarray，避免 torch.as_tensor(list[ndarray]) 的逐项拷贝警告
        boxes_array = np.asarray(boxes, dtype=np.float32).reshape(-1, 4)
        if masks:
            masks_array = np.ascontiguousarray(np.asarray(masks, dtype=np.uint8))
        else:
            masks_array = np.zeros((0, self.tile_size, self.tile_size), dtype=np.uint8)
        target_dict = {
            "boxes": torch.from_numpy(boxes_array),
            "labels": torch.from_numpy(np.asarray(labels, dtype=np.int64)),
            "masks": torch.from_numpy(masks_array),
            "image_id": torch.tensor([int(index)], dtype=torch.int64),
        }
        return tensor, target_dict


def _instances_from_mask(mask: np.ndarray, min_area: int):
    """对每个类别做连通域，返回 ``(boxes[N,4], masks[N,H,W], labels[N])``。"""
    import cv2

    boxes: list[list[float]] = []
    masks: list[np.ndarray] = []
    labels: list[int] = []
    for class_id in sorted(int(value) for value in np.unique(mask) if int(value) > 0):
        binary = (mask == class_id).astype(np.uint8)
        count, components, stats, _centroids = cv2.connectedComponentsWithStats(binary, connectivity=8)
        for component in range(1, count):
            x, y, width, height, area = (int(value) for value in stats[component])
            if area < max(1, min_area) or width < 2 or height < 2:
                continue
            boxes.append([float(x), float(y), float(x + width), float(y + height)])
            masks.append((components == component).astype(np.uint8))
            labels.append(class_id)
    return boxes, masks, labels


def count_instances(sample: dict, *, tile_size: int, num_classes: int, min_area: int) -> int:
    """粗查一个样本能产出多少实例（用于预筛空样本，真正训练时还会重算）。"""
    target = _load_mask(sample, tile_size, sample.get("class_map") or {}, num_classes)
    boxes, _masks, _labels = _instances_from_mask(target, min_area)
    return len(boxes)


__all__ = [
    "IMAGENET_MEAN",
    "IMAGENET_STD",
    "InstanceDataset",
    "SemanticDataset",
    "count_instances",
]
