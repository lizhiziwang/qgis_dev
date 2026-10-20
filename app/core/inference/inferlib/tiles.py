"""滑动窗口规划（center-crop overlap）。

每个输出块在场景内互不重叠、无缝铺满整幅影像；对应的输入窗口以输出块为中心
向四周多读 ``margin`` 像素，因此窗口之间有重叠、但输出像素只由所属块写入，
不需要权重融合或整幅 score 缓冲。
"""

from __future__ import annotations

from dataclasses import dataclass

#: 多数分割骨干要求输入边长是 32 的倍数
ALIGN = 32


@dataclass(frozen=True)
class Window:
    """一个输出块及其对应的输入窗口（均为场景像素坐标）。"""

    #: 输出块（keep 区）
    ox: int
    oy: int
    ow: int
    oh: int
    #: 输入窗口（含重叠边缘）
    ix: int
    iy: int
    iw: int
    ih: int

    @property
    def padded_size(self) -> tuple[int, int]:
        return pad_size(self.iw), pad_size(self.ih)


def pad_size(length: int, multiple: int = ALIGN) -> int:
    """向上取整到 ``multiple`` 的倍数（至少一个 multiple）。"""
    length = max(1, int(length))
    multiple = max(1, int(multiple))
    return ((length + multiple - 1) // multiple) * multiple


def window_geometry(window: int, overlap_percent: int) -> tuple[int, int]:
    """返回 ``(margin, keep)``；``keep`` 至少 32，且不超过 ``window``。"""
    window = max(ALIGN, int(window))
    overlap = min(50, max(0, int(overlap_percent)))
    margin = round(window * overlap / 100 / 2)
    keep = max(ALIGN, window - 2 * margin)
    keep = min(window, keep)
    return margin, keep


def plan_windows(
    width: int,
    height: int,
    window: int,
    overlap_percent: int,
) -> list[Window]:
    """把 ``width × height`` 影像铺成若干输出块。

    - 输出块按 ``keep × keep`` 从左上到右下排列，最后一行/列按剩余尺寸截断；
    - 输入窗口以输出块为中心，越界时平移回影像内（贴边块因此不严格居中）；
    - 影像小于窗口时窗口取影像尺寸，只产生一个输入窗口。
    """
    width, height = int(width), int(height)
    if width <= 0 or height <= 0:
        return []
    window = max(ALIGN, int(window))
    margin, keep = window_geometry(window, overlap_percent)
    win_w = min(window, width)
    win_h = min(window, height)
    max_ix = max(0, width - window)
    max_iy = max(0, height - window)

    windows: list[Window] = []
    oy = 0
    while oy < height:
        oh = min(keep, height - oy)
        iy = min(max(0, oy - margin), max_iy)
        ox = 0
        while ox < width:
            ow = min(keep, width - ox)
            ix = min(max(0, ox - margin), max_ix)
            windows.append(Window(ox=ox, oy=oy, ow=ow, oh=oh, ix=ix, iy=iy, iw=win_w, ih=win_h))
            ox += keep
        oy += keep
    return windows


def cover_count(width: int, height: int, window: int, overlap_percent: int) -> int:
    """输出块数量（不实际构造列表，供界面预估进度）。"""
    if width <= 0 or height <= 0:
        return 0
    _margin, keep = window_geometry(window, overlap_percent)
    return ((int(width) + keep - 1) // keep) * ((int(height) + keep - 1) // keep)


__all__ = [
    "ALIGN",
    "Window",
    "cover_count",
    "pad_size",
    "plan_windows",
    "window_geometry",
]
