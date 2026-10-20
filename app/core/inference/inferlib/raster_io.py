"""推理侧的栅格窗口读写（rasterio，延迟导入）。

- 源影像按窗口读取选中波段，统一转成 RGB uint8；Byte 保持原值，其它位深用
  2%–98% 百分位拉伸（与标注页生成训练样本的口径一致）。
- 输出为单波段 Byte GeoTIFF（类别 id + 调色板），分块写入，不在内存里保存
  整幅类别图。
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

import numpy as np

#: 非 Byte 源影像计算拉伸区间时的缩略图长边
RANGE_MAX_SIDE = 1024
#: 输出 GeoTIFF 内部瓦片大小
OUTPUT_BLOCK = 256
#: 默认概览层级
OVERVIEW_LEVELS = (2, 4, 8, 16)


@dataclass(frozen=True)
class RasterMeta:
    """源影像元信息（足够推理与输出复制地理参考）。"""

    path: str
    width: int
    height: int
    count: int
    dtype: str
    crs: str
    transform: object
    nodata: float | None
    band_names: tuple[str, ...]
    is_geographic: bool
    pixel_size: tuple[float, float]
    crs_name: str = ""
    linear_unit_factor: float = 1.0

    @property
    def is_byte(self) -> bool:
        return self.dtype == "uint8"


def default_bands(count: int) -> tuple[int, int, int]:
    """≥3 波段取 1/2/3，否则单波段灰度复制。"""
    return (1, 2, 3) if int(count) >= 3 else (1, 1, 1)


def resolve_bands(bands, count: int) -> tuple[int, int, int]:
    """把界面/配置里的波段号收敛到合法范围；0 或非法值走默认。"""
    try:
        values = tuple(int(item) for item in (bands or ()))
    except (TypeError, ValueError):
        values = ()
    if len(values) != 3 or any(item <= 0 for item in values):
        return default_bands(count)
    return tuple(min(max(1, item), int(count)) for item in values)  # type: ignore[return-value]


def _crs_display_name(crs) -> str:
    """rasterio 的 CRS 没有 ``display_name``：优先 EPSG 码，其次 WKT 名称。"""
    try:
        authority = crs.to_authority()
    except Exception:  # noqa: BLE001 —— 自定义 WKT 可能没有 authority
        authority = None
    if authority:
        return f"{authority[0]}:{authority[1]}"
    try:
        wkt = crs.to_wkt()
    except Exception:  # noqa: BLE001
        return ""
    match = re.match(r'^[A-Za-z_]+\["([^"]+)"', wkt or "")
    return match.group(1) if match else ""


def open_meta(path: str | Path) -> RasterMeta:
    """打开影像读元信息（不读像素）。"""
    import rasterio

    dataset = rasterio.open(str(path))
    try:
        crs = dataset.crs
        transform = dataset.transform
        names = []
        for index in range(dataset.count):
            names.append(dataset.descriptions[index] or f"band_{index + 1}")
        pixel_size = (abs(float(transform.a)), abs(float(transform.e)))
        crs_name = ""
        linear_unit_factor = 1.0
        if crs:
            crs_name = _crs_display_name(crs)
            try:
                # rasterio 返回 (单位名, 到米的换算系数)
                linear_unit_factor = float(crs.linear_units_factor[1])
            except Exception:  # noqa: BLE001
                linear_unit_factor = 1.0
            if not linear_unit_factor or not np.isfinite(linear_unit_factor):
                linear_unit_factor = 1.0
        return RasterMeta(
            path=str(path),
            width=int(dataset.width),
            height=int(dataset.height),
            count=int(dataset.count),
            dtype=str(dataset.dtypes[0]),
            crs=crs.to_wkt() if crs else "",
            transform=transform,
            nodata=dataset.nodata,
            band_names=tuple(names),
            is_geographic=bool(crs.is_geographic) if crs else False,
            pixel_size=pixel_size,
            crs_name=crs_name,
            linear_unit_factor=linear_unit_factor,
        )
    finally:
        dataset.close()


class SourceRaster:
    """保持打开的源影像，供滑动窗口反复读取。"""

    def __init__(
        self,
        path: str | Path,
        bands,
        meta: RasterMeta | None = None,
        *,
        zero_as_nodata: bool = True,
    ) -> None:
        import rasterio

        self.meta = meta or open_meta(path)
        self.path = str(path)
        self.bands = resolve_bands(bands, self.meta.count)
        #: 选中波段同时为 0 视为 nodata（遥感影像常见的黑色填充边界）
        self.zero_as_nodata = bool(zero_as_nodata)
        self._dataset = rasterio.open(self.path)
        self._nodata = tuple(self._dataset.nodatavals)
        self.ranges: tuple[tuple[float, float], ...] | None = None
        if not self.meta.is_byte:
            self.ranges = self._compute_ranges()

    def close(self) -> None:
        dataset = getattr(self, "_dataset", None)
        if dataset is not None:
            dataset.close()
            self._dataset = None

    def __enter__(self) -> SourceRaster:
        return self

    def __exit__(self, *_exc) -> None:
        self.close()

    # ------------------------------------------------------------------ 读取
    def read_rgb(self, ix: int, iy: int, iw: int, ih: int) -> tuple[np.ndarray, np.ndarray]:
        """读取一个窗口，返回 ``(rgb[H,W,3] uint8, valid[H,W] bool)``。"""
        from rasterio.windows import Window

        window = Window(col_off=int(ix), row_off=int(iy), width=int(iw), height=int(ih))
        if len(set(self.bands)) == 1:
            band = self.bands[0]
            data = self._dataset.read(band, window=window)
            layers = [data, data, data]
            nodata_values = [self._nodata[band - 1]] * 3
            mask_data = self._dataset.read_masks(band, window=window)
            masks = [mask_data] * 3
        else:
            indexes = list(self.bands)
            data = self._dataset.read(indexes=indexes, window=window)
            layers = [data[index] for index in range(data.shape[0])]
            nodata_values = [self._nodata[index - 1] for index in indexes]
            mask_data = self._dataset.read_masks(indexes=indexes, window=window)
            masks = [mask_data[index] for index in range(mask_data.shape[0])]

        valid = np.ones(layers[0].shape, dtype=bool)
        for layer, nodata, mask in zip(layers, nodata_values, masks, strict=True):
            valid &= np.isfinite(layer)
            if nodata is not None:
                valid &= layer != nodata
            # read_masks 同时覆盖 nodata、内部/外部掩膜与 alpha
            valid &= mask != 0
        if self.zero_as_nodata:
            zero = np.ones_like(valid)
            for layer in layers:
                zero &= layer == 0
            valid &= ~zero

        channels = []
        for index, layer in enumerate(layers):
            channels.append(self._to_uint8(layer, index, valid))
        rgb = np.stack(channels, axis=-1)
        return np.ascontiguousarray(rgb), valid

    def _to_uint8(self, layer: np.ndarray, index: int, valid: np.ndarray) -> np.ndarray:
        if self.meta.is_byte:
            out = np.zeros(layer.shape, dtype=np.uint8)
            out[valid] = layer[valid].astype(np.uint8)
            return out
        low, high = self.ranges[index] if self.ranges else (0.0, 1.0)
        if high <= low:
            high = low + 1.0
        scaled = np.clip((layer.astype(np.float32) - low) / (high - low) * 255.0, 0, 255)
        out = np.zeros(layer.shape, dtype=np.uint8)
        out[valid] = scaled[valid].astype(np.uint8)
        return out

    def _compute_ranges(self) -> tuple[tuple[float, float], ...]:
        """在缩略图上算各选中波段的 2%–98% 区间（所有窗口共用）。"""
        indexes = list(dict.fromkeys(self.bands))
        scale = min(1.0, RANGE_MAX_SIDE / max(self.meta.width, self.meta.height, 1))
        out_w = max(1, round(self.meta.width * scale))
        out_h = max(1, round(self.meta.height * scale))
        data = self._dataset.read(indexes=indexes, out_shape=(len(indexes), out_h, out_w))
        masks = self._dataset.read_masks(
            indexes=indexes, out_shape=(len(indexes), out_h, out_w)
        )
        zero = np.zeros((out_h, out_w), dtype=bool)
        if self.zero_as_nodata:
            zero = (data == 0).all(axis=0)
        ranges = []
        for row, band_index in enumerate(indexes):
            sample = data[row].astype(np.float64)
            nodata = self._nodata[band_index - 1]
            keep = np.isfinite(sample) & (masks[row] != 0) & ~zero
            if nodata is not None:
                keep &= sample != nodata
            sample = sample[keep]
            if sample.size == 0:
                ranges.append((0.0, 1.0))
                continue
            if sample.size > 1_000_000:
                sample = sample[:: max(1, sample.size // 1_000_000)]
            low, high = np.percentile(sample, (2, 98))
            if not np.isfinite(low) or not np.isfinite(high) or high <= low:
                low, high = float(sample.min()), float(sample.max())
            if not np.isfinite(low) or not np.isfinite(high) or high <= low:
                low, high = 0.0, 1.0
            ranges.append((float(low), float(high)))
        by_band = dict(zip(indexes, ranges, strict=True))
        return tuple(by_band[band] for band in self.bands)


class OutputRaster:
    """单波段类别 GeoTIFF：创建即设调色板，按输出块写入。"""

    def __init__(
        self,
        path: str | Path,
        meta: RasterMeta,
        *,
        colormap: dict[int, tuple[int, int, int, int]],
    ) -> None:
        import rasterio

        self.path = str(path)
        profile = {
            "driver": "GTiff",
            "width": meta.width,
            "height": meta.height,
            "count": 1,
            "dtype": "uint8",
            "nodata": 0,
            "tiled": True,
            "blockxsize": OUTPUT_BLOCK,
            "blockysize": OUTPUT_BLOCK,
            "compress": "lzw",
            "BIGTIFF": "IF_SAFER",
            "interleave": "band",
        }
        if meta.crs:
            profile["crs"] = meta.crs
        if meta.transform is not None:
            profile["transform"] = meta.transform
        self._rasterio = rasterio
        self._dataset = rasterio.open(self.path, "w", **profile)
        if colormap:
            self._dataset.write_colormap(1, colormap)

    def write(self, ox: int, oy: int, array: np.ndarray) -> None:
        from rasterio.windows import Window

        height, width = array.shape
        self._dataset.write(
            np.ascontiguousarray(array.astype(np.uint8)),
            1,
            window=Window(col_off=int(ox), row_off=int(oy), width=int(width), height=int(height)),
        )

    def build_overviews(self) -> None:
        self._dataset.build_overviews(list(OVERVIEW_LEVELS), self._rasterio.enums.Resampling.nearest)
        self._dataset.update_tags(ns="rio_overview", resampling="nearest")

    def close(self) -> None:
        dataset = getattr(self, "_dataset", None)
        if dataset is not None:
            dataset.close()
            self._dataset = None


def colormap_from_classes(
    classes: list[dict],
    *,
    include_background: bool,
) -> dict[int, tuple[int, int, int, int]]:
    """按类别表生成 GDAL 调色板：0 号透明，其余用 checkpoint 颜色。"""
    table: dict[int, tuple[int, int, int, int]] = {0: (0, 0, 0, 0)}
    for index, item in enumerate(classes):
        class_id = index if include_background else index + 1
        if class_id <= 0 or class_id > 255:
            continue
        try:
            color = str(item.get("color") or "").lstrip("#")
            red, green, blue = (int(color[pos : pos + 2], 16) for pos in (0, 2, 4))
        except (TypeError, ValueError):
            red, green, blue = 230, 25, 75
        table[class_id] = (red, green, blue, 255)
    return table


__all__ = [
    "OUTPUT_BLOCK",
    "OVERVIEW_LEVELS",
    "OutputRaster",
    "RasterMeta",
    "SourceRaster",
    "colormap_from_classes",
    "default_bands",
    "open_meta",
    "resolve_bands",
]
