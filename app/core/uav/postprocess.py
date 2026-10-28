"""成果后处理：坐标/分辨率重采样、格式转换、DTM 生成与质检报告。

GDAL / numpy / scipy 全部延迟导入，模块本身可在无 QGIS 的普通 Python 下
导入（供纯逻辑单测）。真正的栅格处理在 QGIS 自带 Python 里执行。
"""

from __future__ import annotations

import contextlib
import json
import math
import os
import shutil
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from app.core.uav.engine import CancelledError
from app.core.uav.models import UavJob
from app.core.uav.opensfm import (
    DatasetLayout,
    ReconstructionSummary,
    read_reference_altitude,
    stats_or_none,
)

ProgressCallback = Callable[[int, str], None]
CancelCallback = Callable[[], bool]

#: 输出文件名（中文便于非专业用户识别）
DOM_NAMES = {"tif": "正射影像_DOM.tif", "png": "正射影像_DOM.png", "jpg": "正射影像_DOM.jpg"}
DSM_NAME = "数字表面模型_DSM.tif"
DTM_NAME = "数字地面模型_DTM.tif"
POINT_CLOUD_NAME = "密集点云.laz"
POINT_CLOUD_FALLBACK_NAME = "密集点云.ply"
REPORT_NAME = "成果质检报告.md"
MANIFEST_NAME = "成果清单.json"
OPEN_SFM_REPORT_NAME = "OpenSfM质量报告.pdf"
STATS_NAME = "精度统计.json"


@dataclass
class OutputPaths:
    """最终交付文件路径（空字符串表示未生成）。"""

    output_dir: str
    dom: str = ""
    dsm: str = ""
    dtm: str = ""
    point_cloud: str = ""
    report: str = ""
    manifest: str = ""
    stats: str = ""
    pdf_report: str = ""
    warnings: list[str] = field(default_factory=list)
    quality: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "output_dir": self.output_dir,
            "dom": self.dom,
            "dsm": self.dsm,
            "dtm": self.dtm,
            "point_cloud": self.point_cloud,
            "report": self.report,
            "manifest": self.manifest,
            "stats": self.stats,
            "pdf_report": self.pdf_report,
            "warnings": self.warnings,
            "quality": self.quality,
        }


def _gdal():
    from osgeo import gdal  # noqa: PLC0415 —— 延迟导入，纯逻辑测试不需要 GDAL

    gdal.UseExceptions()
    return gdal


def _check_cancel(cancel: CancelCallback | None) -> None:
    if cancel is not None and cancel():
        raise CancelledError("任务已取消")


def resolve_target_gsd(job: UavJob, summary: ReconstructionSummary | None) -> float:
    """目标 DOM 分辨率（米/像素）；0 表示沿用引擎输出。"""
    if job.gsd_mode == "manual" and job.gsd_cm > 0:
        return job.gsd_cm / 100.0
    if summary is not None and summary.gsd_m and summary.gsd_m > 0:
        return float(summary.gsd_m)
    if job.summary is not None and job.summary.gsd_m and job.summary.gsd_m > 0:
        return float(job.summary.gsd_m)
    return 0.0


def _creation_options() -> list[str]:
    return [
        "TILED=YES",
        "COMPRESS=DEFLATE",
        "PREDICTOR=2",
        "BIGTIFF=IF_SAFER",
        "NUM_THREADS=ALL_CPUS",
        "TFW=YES",
    ]


def _warp_options(
    *,
    gsd: float,
    resampling: str,
    fmt: str = "GTiff",
    creation: list[str] | None = None,
    dst_nodata: float | None = None,
) -> Any:
    gdal = _gdal()
    options: dict[str, Any] = {
        "format": fmt,
        "xRes": gsd,
        "yRes": gsd,
        "resampleAlg": resampling,
        "multithread": True,
        "creationOptions": creation if creation is not None else _creation_options(),
    }
    if dst_nodata is not None:
        options["dstNodata"] = dst_nodata
    return gdal.WarpOptions(**options)


def raster_gsd(path: str | Path) -> float:
    """返回栅格像元大小（米）；无法读取时返回 0。"""
    gdal = _gdal()
    dataset = gdal.Open(str(path))
    if dataset is None:
        return 0.0
    try:
        geotransform = dataset.GetGeoTransform()
        return abs(geotransform[1]) if geotransform else 0.0
    finally:
        dataset = None


def warp_to_gsd(
    source: str | Path,
    target: str | Path,
    *,
    gsd: float,
    resampling: str,
    dst_nodata: float | None = None,
) -> None:
    """把栅格重采样到指定分辨率（保持源 CRS 与范围）。"""
    gdal = _gdal()
    source_path = str(source)
    target_path = str(target)
    if gsd <= 0:
        gdal.Translate(
            target_path,
            source_path,
            format="GTiff",
            creationOptions=_creation_options(),
        )
        return
    dataset = gdal.Open(source_path)
    if dataset is None:
        raise RuntimeError(f"无法打开栅格：{source_path}")
    source_gsd = abs(dataset.GetGeoTransform()[1])
    if source_gsd > 0 and abs(source_gsd - gsd) < 1e-9:
        gdal.Translate(
            target_path,
            source_path,
            format="GTiff",
            creationOptions=_creation_options(),
        )
        return
    gdal.Warp(
        target_path,
        source_path,
        options=_warp_options(
            gsd=gsd, resampling=resampling, dst_nodata=dst_nodata
        ),
    )


def _promote_world_file(target: Path, suffix: str) -> None:
    """GDAL 的 WORLDFILE=YES 统一写 .wld；改成 PNG/JPEG 惯用的 .pgw/.jgw。"""
    wld = target.with_suffix(".wld")
    if wld.is_file():
        os.replace(wld, target.with_suffix(suffix))


def _translate_dom(
    source_tif: str | Path,
    target: str | Path,
    output_format: str,
) -> None:
    """把已重采样的 GeoTIFF 转成用户选择的格式，并写 world file。"""
    gdal = _gdal()
    fmt = output_format.lower()
    if fmt == "tif":
        # 已在上一步用 GeoTIFF 写好；这里连同 world file 一起改名。
        if str(source_tif) != str(target):
            shutil.copy2(source_tif, target)
        source_tfw = Path(source_tif).with_suffix(".tfw")
        target_tfw = Path(target).with_suffix(".tfw")
        if source_tfw.is_file() and source_tfw != target_tfw:
            shutil.copy2(source_tfw, target_tfw)
        return
    if fmt == "png":
        gdal.Translate(
            str(target),
            str(source_tif),
            format="PNG",
            creationOptions=["WORLDFILE=YES", "ZLEVEL=6"],
        )
        _promote_world_file(Path(target), ".pgw")
        return
    if fmt in {"jpg", "jpeg"}:
        gdal.Translate(
            str(target),
            str(source_tif),
            format="JPEG",
            bandList=[1, 2, 3],
            creationOptions=["WORLDFILE=YES", "QUALITY=95"],
        )
        _promote_world_file(Path(target), ".jgw")
        return
    raise ValueError(f"不支持的输出格式：{output_format}")


def _copy_if_exists(source: Path, target: Path) -> str:
    if not source.is_file() or source.stat().st_size <= 0:
        return ""
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(source, target)
    return str(target)


def _open_info(path: str) -> dict[str, Any]:
    gdal = _gdal()
    try:
        dataset = gdal.Open(path)
    except RuntimeError:
        return {}
    if dataset is None:
        return {}
    geotransform = dataset.GetGeoTransform()
    band = dataset.GetRasterBand(1)
    info = {
        "width": dataset.RasterXSize,
        "height": dataset.RasterYSize,
        "bands": dataset.RasterCount,
        "crs": dataset.GetSpatialRef().GetAuthorityCode(None) if dataset.GetSpatialRef() else "",
        "gsd_x": abs(geotransform[1]) if geotransform else 0.0,
        "gsd_y": abs(geotransform[5]) if geotransform else 0.0,
        "nodata": band.GetNoDataValue() if band else None,
        "dtype": gdal.GetDataTypeName(band.DataType) if band else "",
        "size_bytes": Path(path).stat().st_size if Path(path).is_file() else 0,
    }
    dataset = None
    return info


# ---------------------------------------------------------------------- DTM


def _read_reduced_dsm(path: str, max_pixels: int = 2000) -> tuple[Any, list[float], Any, float]:
    """读取降采样 DSM，返回 (数组, geotransform, nodata, 原分辨率)。"""
    import numpy as np  # noqa: PLC0415

    gdal = _gdal()
    dataset = gdal.Open(path)
    if dataset is None:
        raise RuntimeError(f"无法打开 DSM：{path}")
    band = dataset.GetRasterBand(1)
    width, height = dataset.RasterXSize, dataset.RasterYSize
    scale = max(1.0, width / max_pixels, height / max_pixels)
    buf_w = max(1, round(width / scale))
    buf_h = max(1, round(height / scale))
    array = band.ReadAsArray(buf_xsize=buf_w, buf_ysize=buf_h)
    if array is None:
        raise RuntimeError("DSM 读取失败（可能为空栅格）。")
    array = np.asarray(array, dtype=np.float32)
    gt = list(dataset.GetGeoTransform())
    gt[1] = dataset.GetGeoTransform()[1] * (width / buf_w)
    gt[5] = dataset.GetGeoTransform()[5] * (height / buf_h)
    nodata = band.GetNoDataValue()
    dataset = None
    return array, gt, nodata, abs(gt[1])


def _fill_nodata(array: Any, invalid: Any) -> Any:
    """用最近有效值填充无效区，避免形态学滤波被 NoData 带偏。"""
    import numpy as np  # noqa: PLC0415
    from scipy import ndimage  # noqa: PLC0415

    if not invalid.any():
        return array
    if invalid.all():
        return np.zeros_like(array)
    indices = ndimage.distance_transform_edt(
        invalid, return_distances=False, return_indices=True
    )
    return array[tuple(indices)]


def _progressive_ground(
    dsm: Any,
    *,
    cell_m: float,
    dh_m: float = 0.4,
    max_window_m: float = 24.0,
) -> Any:
    """渐进形态学开运算提取地面（PMF 简化版）。

    每个窗口尺寸下，把“明显高于开运算表面”的像素视为地物并压低；
    阈值随局部坡度放大，避免把陡坡误判成建筑而过度削平。
    """
    import numpy as np  # noqa: PLC0415
    from scipy import ndimage  # noqa: PLC0415

    ground = dsm.copy()
    gy, gx = np.gradient(dsm)
    slope = np.hypot(gx, gy)
    windows = [3, 5, 7, 9, 11, 15, 21, 27, 35, 45]
    max_px = max(3, round(max_window_m / max(cell_m, 1e-6)))
    for size in windows:
        if size > max_px:
            break
        structure = np.ones((size, size), dtype=bool)
        opened = ndimage.grey_opening(dsm, footprint=structure, mode="nearest")
        threshold = dh_m + slope * (size * max(cell_m, 1e-6))
        object_mask = (dsm - opened) > threshold
        ground[object_mask] = opened[object_mask]
    ground = ndimage.median_filter(ground, size=3, mode="nearest")
    return np.minimum(ground, dsm).astype(np.float32)


def build_dtm(
    dsm_path: str | Path,
    dtm_path: str | Path,
    *,
    resolution: float,
    progress: ProgressCallback | None = None,
    cancel: CancelCallback | None = None,
) -> None:
    """从 DSM 提取 DTM（近似地面模型）。

    采用降采样 + 渐进形态学开运算，兼顾速度与可用性；平坦城区/植被区
    效果最好，陡崖、桥梁等复杂地物需人工检查。
    """
    import numpy as np  # noqa: PLC0415

    gdal = _gdal()
    _check_cancel(cancel)
    if progress is not None:
        progress(5, "读取 DSM…")
    array, gt, nodata, source_res = _read_reduced_dsm(str(dsm_path))
    invalid = ~np.isfinite(array)
    if nodata is not None:
        invalid |= np.isclose(array, nodata)
    filled = _fill_nodata(array, invalid)
    _check_cancel(cancel)
    if progress is not None:
        progress(35, "提取地面点…")
    ground = _progressive_ground(filled, cell_m=source_res)
    ground[invalid] = -9999.0

    tmp_path = Path(dtm_path).with_name(Path(dtm_path).stem + "_reduced.tif")
    driver = gdal.GetDriverByName("GTiff")
    dataset = driver.Create(
        str(tmp_path),
        ground.shape[1],
        ground.shape[0],
        1,
        gdal.GDT_Float32,
        options=["TILED=YES", "COMPRESS=DEFLATE", "PREDICTOR=3", "BIGTIFF=IF_SAFER"],
    )
    if dataset is None:
        raise RuntimeError("无法创建 DTM 临时文件")
    dataset.SetGeoTransform(gt)
    source_ds = gdal.Open(str(dsm_path))
    dataset.SetProjection(source_ds.GetProjection())
    band = dataset.GetRasterBand(1)
    band.SetNoDataValue(-9999.0)
    band.WriteArray(ground)
    band.FlushCache()
    dataset = None
    source_ds = None

    _check_cancel(cancel)
    if progress is not None:
        progress(70, "重采样 DTM…")
    target_res = resolution if resolution > 0 else source_res
    gdal.Warp(
        str(dtm_path),
        str(tmp_path),
        options=_warp_options(
            gsd=target_res,
            resampling="bilinear",
            dst_nodata=-9999.0,
        ),
    )
    with contextlib.suppress(OSError):
        tmp_path.unlink()
    if progress is not None:
        progress(100, "DTM 完成")


# ---------------------------------------------------------------------- 质检


def _fmt_m(value: float | None) -> str:
    if value is None:
        return "—"
    return f"{value:.3f} m"


def _fmt_cm(value: float | None) -> str:
    if value is None:
        return "—"
    if abs(value) < 0.01:
        # 厘米保留 1 位会把毫米级残差显示成 0.0，改用毫米并保留 2 位。
        return f"{value * 1000:.2f} mm"
    return f"{value * 100:.1f} cm"


def quality_grade(value: float | None, *, good: float, fair: float) -> tuple[str, str]:
    if value is None:
        return "未统计", "缺少检查点或统计信息"
    if value <= good:
        return "优", f"≤ {good * 100:.0f} cm"
    if value <= fair:
        return "良", f"≤ {fair * 100:.0f} cm"
    return "需复核", f"> {fair * 100:.0f} cm"


def build_quality(
    job: UavJob,
    summary: ReconstructionSummary,
    outputs: OutputPaths,
) -> dict[str, Any]:
    gcp_grade, gcp_note = quality_grade(
        summary.gcp_average_m, good=0.05, fair=0.10
    )
    cp_grade, cp_note = quality_grade(
        summary.checkpoint_average_m, good=0.10, fair=0.20
    )
    return {
        "total_images": summary.total_images,
        "registered_images": summary.registered_images,
        "sparse_points": summary.sparse_points,
        "reprojection_error_px": summary.reprojection_error_px,
        "output_crs": summary.output_crs or f"EPSG:{job.crs_epsg}",
        "gsd_m": summary.gsd_m,
        "gcp_average_m": summary.gcp_average_m,
        "gcp_grade": gcp_grade,
        "gcp_note": gcp_note,
        "checkpoint_average_m": summary.checkpoint_average_m,
        "checkpoint_grade": cp_grade,
        "checkpoint_note": cp_note,
        "gcp_details": summary.gcp_details,
        "warning_count": len(outputs.warnings) + len(summary.warnings),
    }


def render_report(
    job: UavJob,
    summary: ReconstructionSummary,
    quality: dict[str, Any],
    outputs: OutputPaths,
    *,
    engine_name: str = "",
    elapsed_seconds: float = 0.0,
    log_dir: str = "",
) -> str:
    from datetime import datetime  # noqa: PLC0415

    lines = [
        "# 无人机影像拼接成果质检报告",
        "",
        f"- 生成时间：{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}",
        f"- 影像目录：`{job.image_dir}`",
        f"- 输出目录：`{outputs.output_dir}`",
        f"- 处理引擎：{engine_name or 'OpenSfM'}",
        f"- 处理耗时：{_format_duration(elapsed_seconds)}",
        "",
        "## 一、成果清单",
        "",
        "| 成果 | 路径 | 说明 |",
        "| --- | --- | --- |",
    ]
    rows = [
        ("正射影像 DOM", outputs.dom, "带地理坐标的拼接正射影像"),
        ("数字表面模型 DSM", outputs.dsm, "包含建筑、植被等地物高度"),
        ("数字地面模型 DTM", outputs.dtm, "由 DSM 提取的近似地面高程"),
        ("密集点云", outputs.point_cloud, "LAS/LAZ 或 PLY 点云"),
        ("OpenSfM 质量报告", outputs.pdf_report, "引擎生成的原始 PDF 报告"),
    ]
    for name, path, note in rows:
        if path:
            lines.append(f"| {name} | `{Path(path).name}` | {note} |")
        elif name in {"正射影像 DOM", "数字表面模型 DSM"}:
            lines.append(f"| {name} | 未生成 | 本次未开启密集点云/DSM |")
    lines.extend(
        [
            "",
            "## 二、空三精度",
            "",
            "| 指标 | 数值 | 评价 |",
            "| --- | --- | --- |",
            f"| 参与空三影像 | {summary.registered_images}/{summary.total_images} | "
            f"{'全部参与' if summary.registered_images == summary.total_images else '部分影像未参与'} |",
            f"| 稀疏点数量 | {summary.sparse_points} | — |",
            f"| 平均重投影误差 | {_fmt_px(summary.reprojection_error_px)} | "
            f"{_grade_reprojection(summary.reprojection_error_px)} |",
            f"| 控制点中误差 | {_fmt_cm(quality.get('gcp_average_m'))} | "
            f"{quality.get('gcp_grade', '未统计')}（{quality.get('gcp_note', '')}） |",
            f"| 检查点中误差 | {_fmt_cm(quality.get('checkpoint_average_m'))} | "
            f"{quality.get('checkpoint_grade', '未统计')}（{quality.get('checkpoint_note', '')}） |",
            f"| 成果坐标系 | EPSG:{job.crs_epsg} | 与第 ① 步选择一致 |",
            f"| 地面分辨率 | {_fmt_cm(summary.gsd_m)} / 像素 | — |",
            "",
        ]
    )
    if summary.gcp_details:
        lines.extend(
            [
                "## 三、控制点残差明细",
                "",
                "| 点号 | 类型 | 残差 |",
                "| --- | --- | --- |",
            ]
        )
        for detail in summary.gcp_details:
            point_id = detail.get("id") or detail.get("name") or "—"
            role = detail.get("role") or "—"
            error = detail.get("error")
            if isinstance(error, dict):
                # OpenSfM 逐点误差为 {x, y, z}（东/北/高程），换算成三维中误差；
                # 与 stats 的 average_error（各点三维范数的均值）口径一致。
                components = [error.get(axis) for axis in ("x", "y", "z")]
                if all(isinstance(v, (int, float)) for v in components):
                    error = math.sqrt(sum(float(v) ** 2 for v in components))
                else:
                    error = None
            if not isinstance(error, (int, float)):
                error = detail.get("average_error")
            lines.append(
                f"| {point_id} | {role} | {_fmt_cm(error if isinstance(error, (int, float)) else None)} |"
            )
        lines.append("")

    warnings = [*outputs.warnings, *summary.warnings]
    lines.extend(["## 四、需要注意的问题", ""])
    if warnings:
        for warning in warnings:
            lines.append(f"- {warning}")
    else:
        lines.append("- 未发现需要注意的问题。")
    lines.append("")

    lines.extend(
        [
            "## 五、使用说明",
            "",
            "1. 正射影像适合量测与制图；DSM 包含地表附着物，DTM 为估算地面，"
            "在建筑密集区或陡坡处请人工复核。",
            "2. 成果坐标系为工程坐标系时，CAD/GIS 中请使用同一坐标系打开。",
            "3. 处理中间文件保存在 `_processing` 目录，确认成果无误后可删除以释放磁盘。",
            f"4. 完整运行日志：`{log_dir}`。",
            "",
            "## 六、精度等级说明",
            "",
            "- 控制点中误差 ≤ 5 cm 为优，≤ 10 cm 为良；检查点 ≤ 10 cm 为优，≤ 20 cm 为良。",
            "- 重投影误差 ≤ 1.0 像素表示空三稳定；> 1.5 像素建议检查影像质量与重叠度。",
            "- 精度统计依赖控制点/检查点；未导入控制点时，成果的绝对精度主要取决于影像 RTK 定位。",
            "",
        ]
    )
    return "\n".join(lines)


def _fmt_px(value: float | None) -> str:
    return "—" if value is None else f"{value:.2f} px"


def _grade_reprojection(value: float | None) -> str:
    if value is None:
        return "未统计"
    if value <= 1.0:
        return "优"
    if value <= 1.5:
        return "良"
    return "需复核"


def _format_duration(seconds: float) -> str:
    seconds = max(0, int(seconds))
    minutes, sec = divmod(seconds, 60)
    if minutes >= 60:
        hours, minutes = divmod(minutes, 60)
        return f"{hours} 小时 {minutes} 分 {sec} 秒"
    return f"{minutes} 分 {sec} 秒"


def _write_json_atomic(path: Path, payload: dict[str, Any]) -> None:
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    os.replace(tmp, path)


def _write_text_atomic(path: Path, text: str) -> None:
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, path)


# ---------------------------------------------------------------------- 导出


def export_outputs(
    job: UavJob,
    layout: DatasetLayout,
    summary: ReconstructionSummary,
    *,
    output_dir: str | Path,
    progress: ProgressCallback | None = None,
    cancel: CancelCallback | None = None,
    engine_name: str = "",
    elapsed_seconds: float = 0.0,
) -> OutputPaths:
    """把 OpenSfM 产物转换/重采样/复制到用户输出目录，并写质检报告。"""
    target_dir = Path(output_dir)
    target_dir.mkdir(parents=True, exist_ok=True)
    outputs = OutputPaths(output_dir=str(target_dir))
    resampling = _resample_name(job.resampling)
    target_gsd = resolve_target_gsd(job, summary)

    dom_source = layout.ortho
    dsm_source = layout.dsm
    has_dense = dom_source.is_file() or dsm_source.is_file()
    # 引擎的 DSM/正射按融合网格（通常几厘米）生成；请求更细的 GSD 时
    # 只会上采样出假细节并让文件膨胀，因此以原生分辨率为下限。
    dsm_native = raster_gsd(dsm_source) if dsm_source.is_file() else 0.0
    dom_native = raster_gsd(dom_source) if dom_source.is_file() else 0.0

    if has_dense and job.build_dsm and dsm_source.is_file():
        _check_cancel(cancel)
        if progress is not None:
            progress(5, "重采样 DSM…")
        dsm_out = target_dir / DSM_NAME
        dsm_gsd = job.dsm_resolution_m if job.dsm_resolution_m > 0 else target_gsd
        if dsm_native > 0 and (dsm_gsd <= 0 or dsm_gsd < dsm_native):
            dsm_gsd = dsm_native
        try:
            warp_to_gsd(
                dsm_source,
                dsm_out,
                gsd=dsm_gsd,
                resampling=resampling,
                dst_nodata=-9999.0,
            )
            outputs.dsm = str(dsm_out)
        except Exception as exc:  # noqa: BLE001
            outputs.warnings.append(f"DSM 导出失败：{exc}")

    if has_dense and dom_source.is_file():
        _check_cancel(cancel)
        if progress is not None:
            progress(20, "重采样正射影像…")
        tmp_dom = target_dir / "_dom_resampled.tif"
        dom_gsd = target_gsd
        if dom_native > 0 and (dom_gsd <= 0 or dom_gsd < dom_native):
            dom_gsd = dom_native
            if target_gsd > 0:
                outputs.warnings.append(
                    f"正射影像按引擎原生分辨率 {dom_native:.3f} m 导出"
                    f"（请求 {target_gsd:.3f} m 更细，上采样不会增加细节）"
                )
        try:
            warp_to_gsd(
                dom_source,
                tmp_dom,
                gsd=dom_gsd,
                resampling=resampling,
            )
            _check_cancel(cancel)
            if progress is not None:
                progress(45, "转换成果格式…")
            dom_out = target_dir / DOM_NAMES.get(job.output_format.lower(), DOM_NAMES["tif"])
            _translate_dom(tmp_dom, dom_out, job.output_format)
            outputs.dom = str(dom_out)
        except Exception as exc:  # noqa: BLE001
            outputs.warnings.append(f"正射影像导出失败：{exc}")
        finally:
            with contextlib.suppress(OSError):
                tmp_dom.unlink()
            with contextlib.suppress(OSError):
                tmp_dom.with_suffix(".tfw").unlink()

    if has_dense and job.build_dtm and outputs.dsm:
        _check_cancel(cancel)
        if progress is not None:
            progress(60, "生成 DTM（地面模型）…")
        dtm_out = target_dir / DTM_NAME
        try:
            resolution = raster_gsd(outputs.dsm)
            if resolution <= 0:
                resolution = (
                    job.dsm_resolution_m
                    if job.dsm_resolution_m > 0
                    else target_gsd
                )
            build_dtm(
                outputs.dsm,
                dtm_out,
                resolution=resolution,
                progress=(
                    None
                    if progress is None
                    else lambda p, text: progress(60 + int(p * 0.2), text)
                ),
                cancel=cancel,
            )
            outputs.dtm = str(dtm_out)
        except CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001
            outputs.warnings.append(f"DTM 生成失败（正射与 DSM 不受影响）：{exc}")

    _check_cancel(cancel)
    if progress is not None:
        progress(82, "整理点云与统计文件…")
    for source, name in (
        (layout.fused_laz, POINT_CLOUD_NAME),
        (layout.fused_las, POINT_CLOUD_NAME),
        (layout.fused_ply, POINT_CLOUD_FALLBACK_NAME),
    ):
        copied = _copy_if_exists(source, target_dir / name)
        if copied:
            outputs.point_cloud = copied
            break
    outputs.pdf_report = _copy_if_exists(
        layout.report_pdf, target_dir / OPEN_SFM_REPORT_NAME
    )
    outputs.stats = _copy_if_exists(layout.stats, target_dir / STATS_NAME)

    _check_cancel(cancel)
    if progress is not None:
        progress(90, "生成质检报告…")
    outputs.quality = build_quality(job, summary, outputs)
    reference_altitude = read_reference_altitude(layout)
    if reference_altitude is not None:
        outputs.quality["reference_altitude_m"] = reference_altitude
    report_text = render_report(
        job,
        summary,
        outputs.quality,
        outputs,
        engine_name=engine_name,
        elapsed_seconds=elapsed_seconds,
        log_dir=str(layout.logs),
    )
    report_path = target_dir / REPORT_NAME
    _write_text_atomic(report_path, report_text)
    outputs.report = str(report_path)

    manifest = {
        "schema_version": 1,
        "created_at": _now_iso(),
        "job": job.to_manifest(),
        "engine": {"name": engine_name},
        "quality": outputs.quality,
        "outputs": {
            "dom": outputs.dom,
            "dsm": outputs.dsm,
            "dtm": outputs.dtm,
            "point_cloud": outputs.point_cloud,
            "report": outputs.report,
            "pdf_report": outputs.pdf_report,
            "stats": outputs.stats,
        },
        "warnings": outputs.warnings,
        "stats": stats_or_none(layout),
    }
    manifest_path = target_dir / MANIFEST_NAME
    _write_json_atomic(manifest_path, manifest)
    outputs.manifest = str(manifest_path)
    if progress is not None:
        progress(100, "成果整理完成")
    return outputs


def _now_iso() -> str:
    from datetime import datetime  # noqa: PLC0415

    return datetime.now().isoformat(timespec="seconds")


def _resample_name(value: str) -> str:
    mapping = {
        "bilinear": "bilinear",
        "nearest": "near",
        "cubic": "cubic",
        "cubicspline": "cubicspline",
        "lanczos": "lanczos",
    }
    return mapping.get(value.lower(), "bilinear")


__all__ = [
    "DOM_NAMES",
    "DSM_NAME",
    "DTM_NAME",
    "MANIFEST_NAME",
    "REPORT_NAME",
    "OutputPaths",
    "build_dtm",
    "build_quality",
    "export_outputs",
    "quality_grade",
    "render_report",
    "resolve_target_gsd",
    "warp_to_gsd",
]
