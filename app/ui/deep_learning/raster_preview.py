"""无 overview 大影像的预览代理。

QGIS 画布直接渲染数万×数万像素、又没有 overview 的源影像时，为了铺满窗口
会读取整幅源数据。渲染任务长时间不结束，而 :class:`QgsMapCanvas` 在换层、
缩放、关闭时调用 ``stopRendering()`` 同步等待任务，表现为窗口「未响应」、
画布长时间空白。

这里在后台线程用 GDAL 生成降采样的预览 GeoTIFF，QGIS 只渲染这个几十 MB 的
小文件（GDAL 块缓存可完整容纳，缩放/平移不必反复读取源影像）；推理仍读取原始
全分辨率影像。源文件不会被修改，预览按（路径、大小、修改时间、预览长边）缓存
到用户配置目录，下次选择同一影像直接命中。
"""

from __future__ import annotations

import contextlib
import hashlib
import os
import time
from collections.abc import Callable
from pathlib import Path

from app.core.auth._user_paths import user_config_dir
from app.infra.logging import get_logger
from app.ui.deep_learning.raster_io import RasterInfo

logger = get_logger(__name__)

#: 无 overview 且像素数超过该值的影像才生成预览代理（约 64 Mpx）。
PREVIEW_PIXEL_THRESHOLD = 64_000_000
#: 预览代理长边像素数上限（沿用「预览分辨率」设置，但不超过该值）。
PREVIEW_MAX_SIDE = 8192
#: 缓存文件最长保留天数（生成新预览时顺带清理）。
CACHE_MAX_AGE_DAYS = 30
#: 生成预览期间临时限制的 GDAL 块缓存（字节）。
#: GDAL 默认缓存是物理内存的 5%，整幅影像读完时缓存里会堆下几 GB 的解码数据，
#: 之后释放阶段会长时间占用 CPU 不返回（实测 2.6 GB、128×128 分块的影像超过
#: 4 分钟）。限制到 512 MB 后同样的读取约 20 秒完成；单次顺序降采样不会因此
#: 重复读取数据。
PREVIEW_CACHE_LIMIT = 512 * 1024 * 1024

#: 进度回调：参数为 0..100 的百分比，返回 False 取消生成。
ProgressCallback = Callable[[int], bool]


def preview_cache_dir() -> Path:
    """预览代理缓存目录（与 SAM 缓存同在用户配置目录下）。"""
    return user_config_dir() / "cache" / "raster_preview"


def needs_preview(info: RasterInfo) -> bool:
    """是否应该用预览代理代替源影像挂到画布。

    只有「像素数很大」且「没有 overview」两个条件同时成立时才需要：
    有 overview 的影像 QGIS 本身就能快速渲染，小影像整幅读取也很快。
    """
    if info.overview_count > 0:
        return False
    return int(info.width) * int(info.height) >= PREVIEW_PIXEL_THRESHOLD


def preview_cache_path(source: str | Path, *, max_side: int = PREVIEW_MAX_SIDE) -> Path:
    """按源文件（绝对路径、大小、修改时间）与预览长边计算缓存文件名。"""
    source = Path(source)
    stat = source.stat()
    key = f"{source.resolve()}|{stat.st_size}|{stat.st_mtime_ns}|{max(256, int(max_side))}"
    digest = hashlib.sha1(key.encode("utf-8")).hexdigest()[:16]
    stem = "".join(ch if ch.isalnum() or ch in "-_" else "_" for ch in source.stem)[:48]
    return preview_cache_dir() / f"{stem or 'preview'}-{digest}.tif"


def cached_preview(source: str | Path, *, max_side: int = PREVIEW_MAX_SIDE) -> Path | None:
    """已生成且非空的预览代理；没有缓存时返回 ``None``。"""
    try:
        dest = preview_cache_path(source, max_side=max_side)
        if dest.is_file() and dest.stat().st_size > 0:
            return dest
    except OSError:
        return None
    return None


def build_preview(
    source: str | Path,
    dest: str | Path,
    *,
    max_side: int = PREVIEW_MAX_SIDE,
    progress: ProgressCallback | None = None,
) -> Path:
    """生成预览代理并原子地放到 ``dest``（源文件只读）。

    ``progress`` 收到 0..100 的百分比；返回 ``False`` 时中止生成并删除临时
    文件。生成失败抛出异常，由调用方决定是否提示。
    """
    from osgeo import gdal

    source = Path(source)
    dest = Path(dest)
    dest.parent.mkdir(parents=True, exist_ok=True)
    _prune_cache(dest.parent)

    dataset = gdal.Open(str(source), gdal.GA_ReadOnly)
    if dataset is None:
        raise RuntimeError(gdal.GetLastErrorMsg() or "GDAL 无法打开影像")
    tmp = dest.with_name(f".{dest.stem}.{os.getpid()}.part.tif")
    previous_cache_max = gdal.GetCacheMax()
    gdal.SetCacheMax(min(previous_cache_max, PREVIEW_CACHE_LIMIT))
    try:
        width, height = dataset.RasterXSize, dataset.RasterYSize
        ratio = min(1.0, max(1, int(max_side)) / max(width, height, 1))
        out_w = max(1, round(width * ratio))
        out_h = max(1, round(height * ratio))
        first_band = dataset.GetRasterBand(1)
        data_type = first_band.DataType
        # PREDICTOR=2 只适合整数；浮点数据用 3，避免写出错误值
        predictor = "3" if data_type in (gdal.GDT_Float32, gdal.GDT_Float64) else "2"
        # 调色板/类别数据必须最近邻，平均会把类别 id 混成不存在的值
        paletted = any(
            dataset.GetRasterBand(index).GetColorTable() is not None for index in range(1, dataset.RasterCount + 1)
        )
        with contextlib.suppress(OSError):
            tmp.unlink(missing_ok=True)
        result = gdal.Translate(
            str(tmp),
            dataset,
            format="GTiff",
            width=out_w,
            height=out_h,
            resampleAlg="nearest" if paletted else "average",
            creationOptions=[
                "TILED=YES",
                "COMPRESS=DEFLATE",
                f"PREDICTOR={predictor}",
                "BIGTIFF=IF_SAFER",
                "NUM_THREADS=ALL_CPUS",
            ],
            callback=_gdal_progress(progress),
        )
        if result is None:
            raise RuntimeError(gdal.GetLastErrorMsg() or "GDAL 生成预览失败")
        result = None
        os.replace(tmp, dest)
        return dest
    except BaseException:
        with contextlib.suppress(OSError):
            tmp.unlink(missing_ok=True)
        raise
    finally:
        # 先关闭数据集再恢复缓存上限：缓存过大时卡住的是关闭/释放阶段
        dataset = None
        gdal.SetCacheMax(previous_cache_max)


def _gdal_progress(progress: ProgressCallback | None):
    """把 GDAL 的 (complete, message, data) 回调转成百分比回调。"""
    if progress is None:
        return None

    def callback(complete, _message, _data):
        try:
            return 1 if progress(round(float(complete) * 100)) else 0
        except Exception:
            logger.exception("预览生成进度回调失败")
            return 1

    return callback


def _prune_cache(directory: Path) -> None:
    """清理超过保留期的旧预览（尽力而为，不影响生成）。"""
    cutoff = time.time() - CACHE_MAX_AGE_DAYS * 86400
    try:
        entries = list(directory.glob("*.tif"))
    except OSError:
        return
    for path in entries:
        try:
            if path.stat().st_mtime < cutoff:
                path.unlink()
        except OSError:
            continue


__all__ = [
    "CACHE_MAX_AGE_DAYS",
    "PREVIEW_CACHE_LIMIT",
    "PREVIEW_MAX_SIDE",
    "PREVIEW_PIXEL_THRESHOLD",
    "ProgressCallback",
    "build_preview",
    "cached_preview",
    "needs_preview",
    "preview_cache_dir",
    "preview_cache_path",
]
