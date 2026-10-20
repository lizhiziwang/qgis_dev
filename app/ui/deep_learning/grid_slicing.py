"""宫格切片：把影像按固定块大小与重叠度切成块。

坐标统一使用源图像素坐标系（左上角为原点），块的 ``x/y/width/height`` 可直接作为
GDAL ``ReadAsArray`` 的窗口参数。相邻块至少重叠 ``overlap_px`` 像素（最后一个块
贴住右/下边缘时可能略多），块大小大于影像边长时只切一块。
"""

from __future__ import annotations

from dataclasses import dataclass

#: 块大小允许范围（像素）
MIN_TILE_SIZE = 64
MAX_TILE_SIZE = 8192
#: 重叠度允许范围（百分比）
MAX_OVERLAP_PERCENT = 50


@dataclass(frozen=True)
class TileSpec:
    """一个宫格块在源图中的位置。"""

    row: int
    col: int
    x: int
    y: int
    width: int
    height: int


@dataclass(frozen=True)
class GridSpec:
    """一幅影像的切分参数。"""

    image_width: int
    image_height: int
    tile_size: int
    overlap_px: int

    @property
    def stride(self) -> int:
        return max(1, self.tile_size - self.overlap_px)

    @property
    def rows(self) -> int:
        return len(_starts(self.image_height, self.tile_size, self.stride))

    @property
    def cols(self) -> int:
        return len(_starts(self.image_width, self.tile_size, self.stride))

    @property
    def count(self) -> int:
        return self.rows * self.cols

    @property
    def overlap_percent(self) -> float:
        return self.overlap_px / self.tile_size * 100.0 if self.tile_size else 0.0

    def tiles(self) -> list[TileSpec]:
        """按行优先顺序返回全部块。"""
        xs = _starts(self.image_width, self.tile_size, self.stride)
        ys = _starts(self.image_height, self.tile_size, self.stride)
        result: list[TileSpec] = []
        for row, y in enumerate(ys):
            for col, x in enumerate(xs):
                result.append(
                    TileSpec(
                        row=row,
                        col=col,
                        x=x,
                        y=y,
                        width=min(self.tile_size, self.image_width - x),
                        height=min(self.tile_size, self.image_height - y),
                    )
                )
        return result

    def tile(self, row: int, col: int) -> TileSpec | None:
        for item in self.tiles():
            if item.row == row and item.col == col:
                return item
        return None


def _starts(length: int, tile: int, stride: int) -> list[int]:
    """沿一个轴计算每块的起点：固定步长滑动，最后一块贴住边缘。"""
    if length <= tile:
        return [0]
    starts = list(range(0, max(1, length - tile), stride))
    if starts[-1] != length - tile:
        starts.append(length - tile)
    return starts


def clamp_tile_size(value: int) -> int:
    return min(MAX_TILE_SIZE, max(MIN_TILE_SIZE, int(value)))


def clamp_overlap_percent(value: int) -> int:
    return min(MAX_OVERLAP_PERCENT, max(0, int(value)))


def overlap_px_from_percent(tile_size: int, percent: int) -> int:
    """把重叠百分比换算成像素（至少保证步长为 1）。"""
    tile_size = clamp_tile_size(tile_size)
    percent = clamp_overlap_percent(percent)
    return min(tile_size - 1, round(tile_size * percent / 100.0))


def build_grid(
    image_width: int,
    image_height: int,
    tile_size: int,
    overlap_percent: int,
) -> GridSpec:
    tile_size = clamp_tile_size(tile_size)
    return GridSpec(
        image_width=max(1, int(image_width)),
        image_height=max(1, int(image_height)),
        tile_size=tile_size,
        overlap_px=overlap_px_from_percent(tile_size, overlap_percent),
    )
