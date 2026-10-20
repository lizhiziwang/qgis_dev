"""实例分割后处理：框 IoU、跨窗口重复抑制、实例筛选与栅格化。

只依赖 numpy，便于在 GUI 侧做单元测试；不 import torch / rasterio。
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass

import numpy as np


def box_iou(left, right) -> float:
    """两个 ``(x0, y0, x1, y1)`` 框的 IoU；退化框按 0 处理。"""
    left = np.asarray(left, dtype=np.float64)
    right = np.asarray(right, dtype=np.float64)
    x0 = max(left[0], right[0])
    y0 = max(left[1], right[1])
    x1 = min(left[2], right[2])
    y1 = min(left[3], right[3])
    width = max(0.0, x1 - x0)
    height = max(0.0, y1 - y0)
    inter = width * height
    if inter <= 0:
        return 0.0
    left_area = max(0.0, left[2] - left[0]) * max(0.0, left[3] - left[1])
    right_area = max(0.0, right[2] - right[0]) * max(0.0, right[3] - right[1])
    union = left_area + right_area - inter
    return float(inter / union) if union > 0 else 0.0


def mask_centroid(mask: np.ndarray) -> tuple[float, float] | None:
    """掩膜质心 ``(x, y)``（像素中心坐标）；空掩膜返回 ``None``。"""
    rows, cols = np.nonzero(mask)
    if rows.size == 0:
        return None
    return float(cols.mean()), float(rows.mean())


def select_instances(
    boxes,
    scores,
    masks,
    *,
    score_threshold: float,
    min_instance_pixels: int,
) -> list[int]:
    """按分数与掩膜像素数筛选检测；返回保留的下标（保持原顺序）。"""
    boxes = np.asarray(boxes)
    scores = np.asarray(scores, dtype=np.float64)
    masks = np.asarray(masks)
    kept: list[int] = []
    for index in range(len(scores)):
        if float(scores[index]) < float(score_threshold):
            continue
        mask = masks[index]
        if int(np.count_nonzero(mask)) < int(min_instance_pixels):
            continue
        box = boxes[index]
        if float(box[2]) <= float(box[0]) or float(box[3]) <= float(box[1]):
            continue
        kept.append(index)
    return kept


class BoxSuppressor:
    """跨窗口重复框抑制（空间哈希 + 同类 box IoU）。

    只登记“真正保留”的实例，避免一个在邻窗被丢弃的部分检测把后面的完整
    检测压掉。内存只保存框，不保存掩膜。
    """

    def __init__(self, *, iou_threshold: float = 0.5, cell_size: int = 256) -> None:
        self.iou_threshold = float(iou_threshold)
        self.cell_size = max(16, int(cell_size))
        self._boxes: list[np.ndarray] = []
        self._classes: list[int] = []
        self._cells: dict[tuple[int, int], list[int]] = {}

    def _cells_for(self, box) -> Iterable[tuple[int, int]]:
        x0, y0, x1, y1 = (float(value) for value in box)
        cx0 = int(np.floor(x0 / self.cell_size))
        cy0 = int(np.floor(y0 / self.cell_size))
        cx1 = int(np.floor(max(x0, x1 - 1e-6) / self.cell_size))
        cy1 = int(np.floor(max(y0, y1 - 1e-6) / self.cell_size))
        for cy in range(cy0, cy1 + 1):
            for cx in range(cx0, cx1 + 1):
                yield cx, cy

    def duplicate(self, class_id: int, box) -> bool:
        if not self._boxes:
            return False
        candidates: set[int] = set()
        for key in self._cells_for(box):
            candidates.update(self._cells.get(key, ()))
        for index in candidates:
            if self._classes[index] != int(class_id):
                continue
            if box_iou(box, self._boxes[index]) > self.iou_threshold:
                return True
        return False

    def add(self, class_id: int, box) -> None:
        index = len(self._boxes)
        self._boxes.append(np.asarray(box, dtype=np.float64))
        self._classes.append(int(class_id))
        for key in self._cells_for(box):
            self._cells.setdefault(key, []).append(index)

    def __len__(self) -> int:
        return len(self._boxes)


@dataclass(frozen=True)
class BlockRect:
    """输出块在场景中的位置。"""

    x: int
    y: int
    width: int
    height: int


def paste_mask(
    block: np.ndarray,
    mask: np.ndarray,
    *,
    window_origin: tuple[int, int],
    block_rect: BlockRect,
    class_id: int,
) -> int:
    """把窗口坐标系里的掩膜裁进输出块；返回写入的像素数。

    ``block`` 的左上角对应 ``block_rect``；窗口之外的掩膜部分自动裁掉。
    """
    ix, iy = window_origin
    height, width = mask.shape
    x0 = max(block_rect.x, ix)
    y0 = max(block_rect.y, iy)
    x1 = min(block_rect.x + block_rect.width, ix + width)
    y1 = min(block_rect.y + block_rect.height, iy + height)
    if x1 <= x0 or y1 <= y0:
        return 0

    crop = mask[y0 - iy : y1 - iy, x0 - ix : x1 - ix].astype(bool)
    target = block[
        y0 - block_rect.y : y1 - block_rect.y,
        x0 - block_rect.x : x1 - block_rect.x,
    ]
    target[crop] = np.uint8(class_id)
    return int(crop.sum())


__all__ = [
    "BlockRect",
    "BoxSuppressor",
    "box_iou",
    "mask_centroid",
    "paste_mask",
    "select_instances",
]

