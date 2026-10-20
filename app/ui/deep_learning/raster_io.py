"""标注窗口的栅格读写：GDAL 元信息 / 预览图 / 可选 GeoTIFF 掩膜。

- 预览图按工作分辨率（``max_side``）读取，返回缩放比例供 JSON 记录；
- 多波段按 R/G/B 组合渲染，非 8 位数据做 2%–98% 百分位拉伸；
- 掩膜 GeoTIFF 使用 Byte + nodata=0 + 颜色表，像素值仍是 1..N 标签 id。
"""

from __future__ import annotations

import contextlib
import json
import os
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np
from qgis.PyQt.QtCore import Qt
from qgis.PyQt.QtGui import QColor, QImage

from app.infra.logging import get_logger

logger = get_logger(__name__)

#: 常见影像后缀（文件对话框与文件夹扫描共用）
RASTER_SUFFIXES: tuple[str, ...] = (
    ".tif",
    ".tiff",
    ".img",
    ".png",
    ".jpg",
    ".jpeg",
    ".bmp",
    ".jp2",
    ".dem",
    ".asc",
    ".vrt",
)

#: QFileDialog 过滤串
RASTER_FILTER = (
    "影像文件 (*.tif *.tiff *.img *.png *.jpg *.jpeg *.bmp *.jp2 *.dem *.asc *.vrt);;所有文件 (*)"
)


@dataclass(frozen=True)
class RasterInfo:
    """影像元信息（无 GDAL 打开失败时只有基础尺寸字段）。"""

    path: str
    width: int
    height: int
    band_count: int = 1
    dtype_name: str = "Byte"
    projection_name: str = "无"
    projection_wkt: str = ""
    geotransform: tuple[float, ...] | None = None
    pixel_size: tuple[float, float] | None = None
    unit: str = ""
    nodata: float | None = None
    band_names: tuple[str, ...] = field(default_factory=tuple)

    @property
    def georeferenced(self) -> bool:
        return bool(self.projection_wkt and self.geotransform)


def read_raster_info(path: str | Path) -> RasterInfo:
    """读取影像元信息；GDAL 打不开时退回 QImage 的宽高。"""
    path = str(path)
    try:
        from osgeo import gdal, osr

        dataset = gdal.Open(path, gdal.GA_ReadOnly)
        if dataset is not None:
            band = dataset.GetRasterBand(1)
            width, height = dataset.RasterXSize, dataset.RasterYSize
            count = dataset.RasterCount
            dtype = gdal.GetDataTypeName(band.DataType)
            names = []
            for index in range(1, count + 1):
                item = dataset.GetRasterBand(index)
                names.append(item.GetDescription() or f"band_{index}")
            geotransform = dataset.GetGeoTransform(can_return_null=True)
            wkt = dataset.GetProjection() or ""
            projection_name, unit = "无", ""
            if wkt:
                try:
                    srs = osr.SpatialReference()
                    srs.ImportFromWkt(wkt)
                    projection_name = srs.GetName() or "未知投影"
                    unit = "度" if srs.IsGeographic() else (srs.GetLinearUnitsName() or "")
                except Exception:  # noqa: BLE001
                    projection_name = "未知投影"
            pixel_size = None
            if geotransform:
                pixel_size = (abs(geotransform[1]), abs(geotransform[5]))
            nodata = band.GetNoDataValue()
            dataset = None
            return RasterInfo(
                path=path,
                width=width,
                height=height,
                band_count=count,
                dtype_name=dtype,
                projection_name=projection_name,
                projection_wkt=wkt,
                geotransform=tuple(geotransform) if geotransform else None,
                pixel_size=pixel_size,
                unit=unit or "",
                nodata=nodata,
                band_names=tuple(names),
            )
    except Exception as exc:  # noqa: BLE001 —— 元信息失败不影响手动标注
        logger.warning("读取影像元信息失败 %s：%s", path, exc)

    image = QImage(path)
    return RasterInfo(
        path=path,
        width=image.width(),
        height=image.height(),
        band_count=3 if image.format() != QImage.Format.Format_Grayscale8 else 1,
        dtype_name="Byte",
        band_names=("band_1", "band_2", "band_3") if image.format() != QImage.Format.Format_Grayscale8 else ("band_1",),
    )


def default_bands(info: RasterInfo) -> tuple[int, int, int]:
    """默认 R/G/B 波段：≥3 波段取 1/2/3，否则单波段灰度复制成 RGB。"""
    if info.band_count >= 3:
        return 1, 2, 3
    return 1, 1, 1


def read_preview(
    path: str | Path,
    *,
    bands: tuple[int, int, int] = (1, 2, 3),
    max_side: int = 4096,
) -> tuple[QImage, float]:
    """读取工作分辨率预览图，返回 ``(QImage, 缩放比例)``。

    缩放比例 = 工作分辨率 / 源分辨率（≤1）。预览图始终 RGB888。
    """
    path = str(path)
    max_side = max(256, int(max_side))
    try:
        from osgeo import gdal

        dataset = gdal.Open(path, gdal.GA_ReadOnly)
        if dataset is None:
            raise RuntimeError("GDAL 无法打开影像")
        width, height = dataset.RasterXSize, dataset.RasterYSize
        scale = min(1.0, max_side / max(width, height))
        out_w = max(1, round(width * scale))
        out_h = max(1, round(height * scale))
        count = dataset.RasterCount
        chosen = tuple(min(max(1, int(b)), count) for b in bands)
        arrays = []
        for band_index in chosen:
            band = dataset.GetRasterBand(band_index)
            arr = band.ReadAsArray(buf_xsize=out_w, buf_ysize=out_h)
            nodata = band.GetNoDataValue()
            dtype = band.DataType
            arrays.append(_to_uint8(arr, nodata, dtype))
        dataset = None
        return _stack_rgb(arrays, bool(count == 1 or len(set(chosen)) == 1)), out_w / width if width else 1.0
    except Exception as exc:
        logger.warning("GDAL 读取预览失败，改用 QImage：%s（%s）", path, exc)
        image = QImage(path)
        if image.isNull():
            raise OSError(f"无法读取影像：{path}") from exc
        scale = min(1.0, max_side / max(image.width(), image.height()))
        if scale < 1.0:
            image = image.scaled(
                max(1, round(image.width() * scale)),
                max(1, round(image.height() * scale)),
                Qt.AspectRatioMode.IgnoreAspectRatio,
                Qt.TransformationMode.SmoothTransformation,
            )
        if image.format() != QImage.Format.Format_RGB888:
            image = image.convertToFormat(QImage.Format.Format_RGB888)
        return image, scale


def read_class_preview(
    path: str | Path,
    *,
    max_side: int = 4096,
) -> tuple[QImage, float]:
    """读取类别 id GeoTIFF 的缩略图，返回 ``(Grayscale8 QImage, 缩放比例)``。

    像素值就是类别 id（0 = 背景/nodata），供 :class:`AnnotationDocument`
    按标签颜色渲染。缩放与 :func:`read_preview` 同一口径，便于两图套合。
    """
    path = str(path)
    max_side = max(256, int(max_side))
    try:
        from osgeo import gdal

        dataset = gdal.Open(path, gdal.GA_ReadOnly)
        if dataset is None:
            raise RuntimeError("GDAL 无法打开类别图")
        width, height = dataset.RasterXSize, dataset.RasterYSize
        scale = min(1.0, max_side / max(width, height))
        out_w = max(1, round(width * scale))
        out_h = max(1, round(height * scale))
        band = dataset.GetRasterBand(1)
        if band is None:
            raise RuntimeError("类别图没有波段")
        ids = np.asarray(band.ReadAsArray(buf_xsize=out_w, buf_ysize=out_h))
        nodata = band.GetNoDataValue()
        dataset = None
        if ids.ndim == 3:
            ids = ids[0]
        ids = np.array(ids, dtype=np.uint8, copy=True)
        if nodata is not None:
            ids[ids == int(nodata)] = 0
        ids = np.ascontiguousarray(ids)
        image = QImage(
            ids.data,
            ids.shape[1],
            ids.shape[0],
            int(ids.strides[0]),
            QImage.Format.Format_Grayscale8,
        )
        return image.copy(), out_w / width if width else 1.0
    except Exception as exc:
        raise OSError(f"无法读取类别图：{path}") from exc


def read_tile(
    path: str | Path,
    x: int,
    y: int,
    width: int,
    height: int,
    *,
    bands: tuple[int, int, int] = (1, 2, 3),
    ranges: tuple[tuple[float, float], ...] | None = None,
) -> QImage:
    """按源图窗口读取一块宫格（全分辨率），返回 RGB888。

    ``ranges`` 是 :func:`compute_band_ranges` 得到的全图拉伸区间，保证相邻块
    颜色一致；缺省时对该窗口单独做百分位拉伸。
    """
    path = str(path)
    try:
        from osgeo import gdal

        dataset = gdal.Open(path, gdal.GA_ReadOnly)
        if dataset is None:
            raise RuntimeError("GDAL 无法打开影像")
        count = dataset.RasterCount
        chosen = tuple(min(max(1, int(b)), count) for b in bands)
        arrays = []
        for index, band_index in enumerate(chosen):
            band = dataset.GetRasterBand(band_index)
            arr = band.ReadAsArray(int(x), int(y), int(width), int(height))
            if arr is None:
                raise RuntimeError(f"读取波段 {band_index} 失败")
            value_range = ranges[index] if ranges and index < len(ranges) else None
            arrays.append(_to_uint8(arr, band.GetNoDataValue(), band.DataType, value_range))
        dataset = None
        return _stack_rgb(arrays, bool(count == 1 or len(set(chosen)) == 1))
    except Exception as exc:
        logger.warning("GDAL 读取宫格失败，改用 QImage：%s（%s）", path, exc)
        image = QImage(path)
        if image.isNull():
            raise OSError(f"无法读取影像：{path}") from exc
        if x < 0 or y < 0 or x + width > image.width() or y + height > image.height():
            raise OSError(f"影像窗口超出范围：{path} ({x}, {y}, {width}, {height})") from exc
        tile = image.copy(int(x), int(y), int(width), int(height))
        if tile.isNull():
            raise OSError(f"影像窗口超出范围：{path} ({x}, {y}, {width}, {height})") from exc
        if tile.format() != QImage.Format.Format_RGB888:
            tile = tile.convertToFormat(QImage.Format.Format_RGB888)
        return tile


def compute_band_ranges(
    path: str | Path,
    bands: tuple[int, int, int] = (1, 2, 3),
    *,
    max_side: int = 1024,
) -> tuple[tuple[float, float], ...] | None:
    """在缩略图上计算各波段的百分位拉伸区间（供宫格共用）。"""
    path = str(path)
    try:
        from osgeo import gdal

        dataset = gdal.Open(path, gdal.GA_ReadOnly)
        if dataset is None:
            return None
        width, height = dataset.RasterXSize, dataset.RasterYSize
        scale = min(1.0, max_side / max(width, height, 1))
        out_w = max(1, round(width * scale))
        out_h = max(1, round(height * scale))
        count = dataset.RasterCount
        chosen = tuple(min(max(1, int(b)), count) for b in bands)
        ranges = []
        for band_index in chosen:
            band = dataset.GetRasterBand(band_index)
            arr = band.ReadAsArray(buf_xsize=out_w, buf_ysize=out_h)
            ranges.append(_band_range(arr, band.GetNoDataValue(), band.DataType))
        dataset = None
        return tuple(ranges)
    except Exception as exc:  # noqa: BLE001 —— 拉伸失败时逐块计算即可
        logger.warning("计算波段拉伸区间失败 %s：%s", path, exc)
        return None


def _stack_rgb(arrays: list[np.ndarray], gray: bool) -> QImage:
    if gray:
        band = arrays[0]
        rgb = np.stack([band, band, band], axis=-1)
        height, width = band.shape
    else:
        rgb = np.stack(arrays[:3], axis=-1)
        while rgb.shape[2] < 3:
            rgb = np.concatenate([rgb, rgb[:, :, :1]], axis=2)
        height, width = rgb.shape[:2]
    rgb = np.ascontiguousarray(rgb)
    image = QImage(
        rgb.data,
        width,
        height,
        int(rgb.strides[0]),
        QImage.Format.Format_RGB888,
    )
    return image.copy()


def _band_range(arr: np.ndarray, nodata: float | None, dtype) -> tuple[float, float]:
    """计算 2%–98% 拉伸区间；无效 / 常量数据兜底成 min–max。"""
    from osgeo import gdal

    data = np.asarray(arr)
    if dtype == gdal.GDT_Byte:
        return 0.0, 255.0
    valid = np.isfinite(data)
    if nodata is not None:
        valid &= data != nodata
    sample = data[valid]
    if sample.size == 0:
        return 0.0, 1.0
    if sample.size > 1_000_000:
        sample = sample[:: max(1, sample.size // 1_000_000)]
    low, high = np.percentile(sample, (2, 98))
    if not np.isfinite(low) or not np.isfinite(high) or high <= low:
        low, high = float(sample.min()), float(sample.max())
    if not np.isfinite(low) or not np.isfinite(high):
        return 0.0, 1.0
    if high <= low:
        high = low + 1.0
    return float(low), float(high)


def _to_uint8(
    arr: np.ndarray,
    nodata: float | None,
    dtype,
    value_range: tuple[float, float] | None = None,
) -> np.ndarray:
    """把任意波段数据映射到 0..255（8 位保持原值，其它做百分位拉伸）。"""
    from osgeo import gdal

    data = np.asarray(arr)
    valid = np.isfinite(data)
    if nodata is not None:
        valid &= data != nodata
    if dtype == gdal.GDT_Byte:
        out = data.astype(np.uint8)
        out[~valid] = 0
        return out
    low, high = value_range if value_range is not None else _band_range(data, nodata, dtype)
    if high <= low:
        high = low + 1.0
    out = np.clip((data.astype(np.float32) - low) / (high - low) * 255.0, 0, 255).astype(np.uint8)
    out[~valid] = 0
    return out


def write_mask_geotiff(
    ids: np.ndarray,
    path: str | Path,
    info: RasterInfo,
    *,
    class_names: list[str] | None = None,
    class_colors: list[str] | None = None,
    scale_x: float = 1.0,
    scale_y: float = 1.0,
    source_offset: tuple[int, int] = (0, 0),
) -> bool:
    """把 id 掩膜写成带地理参考的 GeoTIFF；无地理参考时返回 False。

    ``source_offset`` 是掩膜左上角在源图中的像素坐标（宫格块使用），
    用于把块的地理原点平移到正确位置。
    """
    if not info.georeferenced:
        return False
    ids = np.ascontiguousarray(ids.astype(np.uint8))
    height, width = ids.shape
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = path.with_name(path.stem + ".tmp.tif")
    try:
        from osgeo import gdal

        driver = gdal.GetDriverByName("GTiff")
        dataset = driver.Create(
            str(tmp_path),
            width,
            height,
            1,
            gdal.GDT_Byte,
            options=["TILED=YES", "COMPRESS=LZW", "PHOTOMETRIC=PALETTE"],
        )
        if dataset is None:
            raise OSError(f"创建 GeoTIFF 失败：{path}")
        gt = list(info.geotransform or (0, 1, 0, 0, 0, -1))
        pixel_x = float(gt[1])
        pixel_y = float(gt[5])
        # 宫格块：把原点平移到块左上角对应的源图坐标（偏移以源像元计）
        gt[0] = float(gt[0]) + float(source_offset[0]) * pixel_x
        gt[3] = float(gt[3]) + float(source_offset[1]) * pixel_y
        # 工作分辨率小于源分辨率时，像元变大，地理范围保持不变
        gt[1] = pixel_x * float(scale_x)
        gt[5] = pixel_y * float(scale_y)
        dataset.SetGeoTransform(gt)
        if info.projection_wkt:
            dataset.SetProjection(info.projection_wkt)
        band = dataset.GetRasterBand(1)
        # 颜色表 / 元数据必须在写像素之前设置：GTiff 在写入状态会拒绝修改
        # PhotometricInterpretation（"Cannot modify tag while writing"）。
        band.SetNoDataValue(0)
        band.SetDescription("class_id")
        names = list(class_names or [])
        band.SetMetadataItem("CLASS_NAMES", json.dumps(names, ensure_ascii=False))
        table = gdal.ColorTable()
        table.SetColorEntry(0, (0, 0, 0, 0))
        for index, hex_color in enumerate(class_colors or [], start=1):
            color = QColor(hex_color)
            table.SetColorEntry(index, (color.red(), color.green(), color.blue(), 255))
        band.SetRasterColorTable(table)
        band.SetRasterColorInterpretation(gdal.GCI_PaletteIndex)
        band.WriteArray(ids)
        band.FlushCache()
        dataset = None
        os.replace(tmp_path, path)
        return True
    except Exception as exc:
        logger.error("写 GeoTIFF 掩膜失败 %s：%s", path, exc, exc_info=True)
        with contextlib.suppress(OSError):
            tmp_path.unlink(missing_ok=True)
        return False
