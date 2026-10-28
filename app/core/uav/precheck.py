"""航测数据预检：扫描影像、估算航高/GSD/重叠、生成报告与任务清单。

预检本身不执行空三/密集点云/正射，只把真实可检查的项目算出来，产出：

- ``预检报告.md``：给人看的结论与建议；
- ``任务清单.json``：拼接引擎直接消费的完整参数与影像清单。

预检通过后由 :func:`app.core.uav.pipeline.run_orthomosaic` 执行真实解算。
"""

from __future__ import annotations

import json
import math
import os
import shutil
import statistics
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from itertools import pairwise
from pathlib import Path

from app.core.uav.crs import central_meridian_of, is_geographic_epsg, recommend_crs
from app.core.uav.exif import list_images, read_image_meta
from app.core.uav.gcp import validate_gcp
from app.core.uav.intrinsics import estimate_footprint, estimate_gsd
from app.core.uav.models import (
    FlightSummary,
    GcpSet,
    ImageMeta,
    PrecheckIssue,
    PrecheckOutputs,
    UavJob,
)
from app.core.uav.pipeline import ENGINE_AVAILABLE, ENGINE_NOTE
from app.core.uav.position import PositionFile, find_mrk_file, parse_mrk, parse_position_file

ProgressCallback = Callable[[int, str], None]
CancelCallback = Callable[[], bool]

_LEVEL_LABEL = {"error": "✘ 错误", "warn": "⚠ 警告", "info": "ℹ 提示", "ok": "✔ 通过"}

_PRESET_LABEL = {"fast": "快拼", "standard": "标准", "high": "高精度"}
_DENSE_QUALITY_LABEL = {"low": "低", "medium": "中", "high": "高"}
_SEAMLINE_LABEL = {"auto": "自动拼接线", "geometry": "几何拼接线", "none": "不处理"}
_FORMAT_LABEL = {"tif": "GeoTIFF + TFW", "png": "PNG + PGW", "jpg": "JPEG + JGW"}


class PrecheckError(RuntimeError):
    """预检存在 error 级问题，不生成报告。"""

    def __init__(self, issues: list[PrecheckIssue]) -> None:
        super().__init__("预检未通过")
        self.issues = issues


@dataclass
class ScanResult:
    """影像扫描结果。"""

    images: list[ImageMeta]
    summary: FlightSummary
    position_errors: list[str]
    position_source: str = ""


def default_output_dir(image_dir: str) -> str:
    """默认输出目录：影像目录同级的 ``<目录名>_成果``。"""
    if not image_dir:
        return ""
    path = Path(image_dir)
    return str(path.parent / f"{path.name}_成果")


def _median_spacing(images: list[ImageMeta]) -> float | None:
    distances = []
    for first, second in pairwise(images):
        if not (first.has_position and second.has_position):
            continue
        lat_mid = math.radians((first.lat + second.lat) / 2.0)  # type: ignore[operator]
        dx = (second.lon - first.lon) * 111320.0 * math.cos(lat_mid)  # type: ignore[operator]
        dy = (second.lat - first.lat) * 110540.0  # type: ignore[operator]
        distance = math.hypot(dx, dy)
        if distance > 0.5:
            distances.append(distance)
    return statistics.median(distances) if distances else None


def estimate_summary(
    images: list[ImageMeta], gsd_override_m: float | None = None
) -> FlightSummary:
    """根据影像元数据估算覆盖范围、相对航高、GSD 与重叠度。"""
    positioned = [image for image in images if image.has_position]
    summary = FlightSummary(
        image_count=len(images),
        positioned_count=len(positioned),
        rtk_fixed_count=sum(1 for image in images if image.rtk_flag == 50),
    )
    if positioned:
        lons = [image.lon for image in positioned]
        lats = [image.lat for image in positioned]
        summary.bbox = (
            float(min(lons)),
            float(min(lats)),
            float(max(lons)),
            float(max(lats)),
        )

    altitudes = [image.relative_altitude for image in images if image.relative_altitude]
    ellipsoid = [
        image.altitude_ellipsoid for image in images if image.altitude_ellipsoid is not None
    ]
    if altitudes:
        summary.mean_agl = float(statistics.fmean(altitudes))
    if ellipsoid:
        summary.mean_altitude = float(statistics.fmean(ellipsoid))

    intrinsics = next((image.intrinsics for image in images if image.intrinsics.is_complete), None)
    if intrinsics is None:
        intrinsics = images[0].intrinsics if images else None
    if intrinsics is not None:
        summary.gsd_m = gsd_override_m or estimate_gsd(
            summary.mean_agl,
            intrinsics.focal_mm,
            intrinsics.sensor_width_mm,
            intrinsics.width_px,
        )
        footprint = estimate_footprint(
            summary.mean_agl,
            intrinsics.focal_mm,
            intrinsics.sensor_width_mm,
            intrinsics.width_px,
            intrinsics.height_px,
        )
        if footprint is not None:
            summary.footprint_width_m, summary.footprint_height_m = footprint

    summary.median_spacing_m = _median_spacing(images)
    if summary.median_spacing_m and summary.footprint_height_m:
        overlap = 1.0 - summary.median_spacing_m / summary.footprint_height_m
        summary.forward_overlap = min(1.0, max(0.0, overlap))
    return summary


def _position_file_for(image_dir: str, position_file: str) -> tuple[PositionFile | None, str]:
    if position_file:
        return parse_position_file(position_file), "POS"
    mrk_path = find_mrk_file(image_dir)
    if mrk_path:
        return parse_mrk(mrk_path), "MRK"
    return None, ""


def scan_images(
    image_dir: str,
    position_file: str = "",
    progress: ProgressCallback | None = None,
    cancel: CancelCallback | None = None,
) -> ScanResult:
    """扫描影像目录并读取每张影像的元数据。"""
    paths = list_images(image_dir)
    position, position_source = _position_file_for(image_dir, position_file)
    position_errors = list(position.errors) if position is not None else []
    images: list[ImageMeta] = []
    total = max(1, len(paths))
    for index, path in enumerate(paths):
        if cancel is not None and cancel():
            break
        record = position.lookup(os.path.basename(path)) if position is not None else None
        if record is None and position is not None and index < len(position.ordered):
            record = position.ordered[index]
        images.append(
            read_image_meta(path, position=record, position_source=position_source)
        )
        if progress is not None:
            progress(int((index + 1) / total * 100), f"读取影像 {index + 1}/{len(paths)}")
    summary = estimate_summary(images)
    return ScanResult(
        images=images,
        summary=summary,
        position_errors=position_errors,
        position_source=position_source,
    )


def _issue(level: str, code: str, title: str, detail: str = "", fix: str = "") -> PrecheckIssue:
    return PrecheckIssue(level=level, code=code, title=title, detail=detail, fix=fix)


def _check_crs(job: UavJob, summary: FlightSummary) -> list[PrecheckIssue]:
    issues: list[PrecheckIssue] = []
    if summary.bbox is None:
        return issues
    lon_min, lat_min, lon_max, lat_max = summary.bbox
    center_lon = (lon_min + lon_max) / 2.0
    center_lat = (lat_min + lat_max) / 2.0
    suggestion = recommend_crs(center_lon, center_lat)
    if is_geographic_epsg(job.crs_epsg):
        issues.append(
            _issue(
                "warn",
                "crs_geographic",
                "输出坐标系为经纬度（度）",
                f"当前 EPSG:{job.crs_epsg}；影像中心约 {center_lon:.5f}°E, {center_lat:.5f}°N。",
                "国内测绘成果建议使用 CGCS2000 高斯-克吕格投影，"
                f"例如 {suggestion.name}（EPSG:{suggestion.epsg}）。",
            )
        )
        return issues
    job_cm = central_meridian_of(job.crs_epsg)
    if (
        job_cm is not None
        and suggestion.central_meridian is not None
        and abs(job_cm - suggestion.central_meridian) > 0.01
    ):
        issues.append(
            _issue(
                "warn",
                "crs_zone_mismatch",
                "输出坐标系与影像位置可能不在同一带",
                f"影像中心 {center_lon:.5f}°E，当前 EPSG:{job.crs_epsg} 的中央经线为 {job_cm:g}°E。",
                f"建议改用 {suggestion.name}（EPSG:{suggestion.epsg}）。",
            )
        )
    else:
        issues.append(
            _issue("ok", "crs_ok", "输出坐标系", f"EPSG:{job.crs_epsg}")
        )
    return issues


def _check_gcp(job: UavJob) -> list[PrecheckIssue]:
    issues: list[PrecheckIssue] = []
    gcp = job.gcp
    wants_gcp = job.alignment_mode == "gcp" or job.preset == "high"
    if gcp is None or not gcp.points:
        if wants_gcp:
            issues.append(
                _issue(
                    "warn",
                    "gcp_missing",
                    "尚未导入控制点",
                    "当前空三模式需要控制点，但任务里没有控制点。",
                    "在第 ① 步导入控制点文件，并在第 ② 步完成刺点；或改用“仅使用影像 GPS”。",
                )
            )
        return issues

    control = gcp.control_points
    check = gcp.check_points
    issues.append(
        _issue(
            "info",
            "gcp_stats",
            "控制点统计",
            f"控制点 {len(control)} 个，检查点 {len(check)} 个，已刺点 {gcp.marked_point_count} 个。",
        )
    )
    if wants_gcp and len(control) < 3:
        issues.append(
            _issue(
                "warn",
                "gcp_too_few",
                "控制点数量不足",
                f"当前只有 {len(control)} 个控制点，高精度空三建议至少 3 个。",
                "补充控制点或改用标准/快拼模式。",
            )
        )
    for message in validate_gcp(gcp):
        issues.append(_issue("warn", "gcp_marks", "控制点刺点不完整", message))
    image_names = {image.name for image in job.images}
    unknown = sorted(
        {
            name
            for point in gcp.points
            for name in point.marks
            if name not in image_names
        }
    )
    if unknown:
        issues.append(
            _issue(
                "warn",
                "gcp_unknown_image",
                "刺点引用了不存在的影像",
                "、".join(unknown[:5]) + (" 等" if len(unknown) > 5 else ""),
                "可能更换了影像目录；请重新检查刺点。",
            )
        )
    if check:
        issues.append(
            _issue(
                "info",
                "gcp_checkpoints",
                "检查点残差",
                f"{len(check)} 个检查点；空三解算完成后显示残差。",
            )
        )
    return issues


def build_issues(job: UavJob, summary: FlightSummary | None = None) -> list[PrecheckIssue]:
    """生成预检结论（error / warn / info / ok）。"""
    summary = summary or estimate_summary(job.images, _gsd_from_job(job))
    issues: list[PrecheckIssue] = []
    if not job.images:
        issues.append(
            _issue(
                "error",
                "no_images",
                "没有可处理的影像",
                "影像目录为空或没有支持的格式（jpg/jpeg/png/tif/tiff）。",
                "重新选择无人机影像文件夹并扫描。",
            )
        )
        return issues

    issues.append(
        _issue(
            "ok",
            "image_count",
            "影像数量",
            f"{summary.image_count} 张；GPS 覆盖 {summary.positioned_count}/{summary.image_count}。",
        )
    )
    if summary.image_count < 5:
        issues.append(
            _issue(
                "warn",
                "few_images",
                "影像数量偏少",
                f"只有 {summary.image_count} 张影像，可能不足以完成空三。",
                "确认是否选错目录，或补充航片。",
            )
        )
    if summary.positioned_count == 0:
        issues.append(
            _issue(
                "warn",
                "no_position",
                "所有影像都没有定位信息",
                "没有 EXIF/XMP 坐标，也没有可用的 POS/MRK 文件。",
                "空三只能完全依靠同名点解算，建议导入 POS 文件并确认航向重叠。",
            )
        )
    elif summary.positioned_count < summary.image_count:
        missing = summary.image_count - summary.positioned_count
        issues.append(
            _issue(
                "warn",
                "partial_position",
                "部分影像缺少定位信息",
                f"{missing} 张影像没有经纬度。",
                "检查这些影像的 EXIF/XMP 或补充 POS 文件。",
            )
        )
    else:
        issues.append(
            _issue("ok", "position_ok", "影像定位", "所有影像都有经纬度。")
        )

    if summary.rtk_fixed_count:
        issues.append(
            _issue(
                "info",
                "rtk_stats",
                "RTK 质量",
                f"{summary.rtk_fixed_count}/{summary.image_count} 张为固定解（RtkFlag=50）。",
            )
        )

    sizes = {
        (image.intrinsics.width_px, image.intrinsics.height_px)
        for image in job.images
        if image.intrinsics.width_px and image.intrinsics.height_px
    }
    if len(sizes) > 1:
        issues.append(
            _issue(
                "warn",
                "mixed_size",
                "影像尺寸不一致",
                f"发现 {len(sizes)} 种尺寸：{', '.join(f'{w}×{h}' for w, h in sorted(sizes))}。",
                "混用不同相机/分辨率会降低空三成功率，建议分开处理。",
            )
        )
    if not any(image.intrinsics.is_complete for image in job.images):
        issues.append(
            _issue(
                "warn",
                "intrinsics_missing",
                "相机内参不完整",
                "未能从 EXIF/XMP 读取完整的焦距与传感器尺寸。",
                "可在第 ⑤ 步手动指定 GSD；空三阶段仍会尝试自标定。",
            )
        )
    elif summary.gsd_m:
        issues.append(
            _issue(
                "info",
                "gsd",
                "估算地面分辨率",
                f"GSD ≈ {summary.gsd_m * 100:.2f} cm/px；相对航高 ≈ {summary.mean_agl:.1f} m。"
                if summary.mean_agl
                else f"GSD ≈ {summary.gsd_m * 100:.2f} cm/px。",
            )
        )
    if summary.forward_overlap is not None:
        level = "warn" if summary.forward_overlap < 0.6 else "ok"
        issues.append(
            _issue(
                level,
                "overlap",
                "估算航向重叠",
                f"约 {summary.forward_overlap * 100:.0f}%，影像间距中位数 "
                f"{summary.median_spacing_m:.1f} m。",
                "低于 60% 时同名点匹配容易失败，建议补飞或降低航高。"
                if level == "warn"
                else "",
            )
        )

    issues.extend(_check_crs(job, summary))
    issues.extend(_check_gcp(job))
    issues.extend(_check_output(job))
    issues.append(_issue("info", "engine", "拼接引擎", ENGINE_NOTE))
    return issues


def _gsd_from_job(job: UavJob) -> float | None:
    if job.gsd_mode == "manual" and job.gsd_cm > 0:
        return job.gsd_cm / 100.0
    return None


def _check_output(job: UavJob) -> list[PrecheckIssue]:
    output_dir = job.output_dir.strip()
    if not output_dir:
        return [
            _issue(
                "error",
                "output_missing",
                "未设置输出目录",
                "请在第 ⑥ 步选择成果输出目录。",
            )
        ]
    target = Path(output_dir)
    if target.exists() and not target.is_dir():
        return [
            _issue(
                "error",
                "output_not_dir",
                "输出路径不是文件夹",
                output_dir,
                "换一个目录。",
            )
        ]
    probe = target if target.exists() else target.parent
    if not probe.exists() or not probe.is_dir():
        return [
            _issue(
                "error",
                "output_parent_missing",
                "输出目录的上级目录不存在",
                str(probe),
                "重新选择输出目录。",
            )
        ]
    if not os.access(probe, os.W_OK):
        return [
            _issue(
                "error",
                "output_not_writable",
                "输出目录不可写",
                str(probe),
                "换一个有写权限的目录。",
            )
        ]
    try:
        free_gb = shutil.disk_usage(probe).free / (1024**3)
    except OSError:
        free_gb = None
    if free_gb is not None and free_gb < 1.0:
        return [
            _issue(
                "warn",
                "disk_low",
                "磁盘剩余空间不足 1 GB",
                f"{probe} 所在磁盘剩余 {free_gb:.2f} GB。",
                "清理磁盘后再运行拼接。",
            )
        ]
    return [_issue("ok", "output_ok", "输出目录可写", output_dir)]


def _fmt(value: float | None, digits: int = 1, unit: str = "") -> str:
    if value is None:
        return "—"
    return f"{value:.{digits}f}{unit}"


def _md_cell(text: str) -> str:
    return str(text).replace("|", "\\|").replace("\n", " ")


def render_report(
    job: UavJob, issues: list[PrecheckIssue], summary: FlightSummary | None = None
) -> str:
    """生成 Markdown 预检报告。"""
    summary = summary or job.summary or estimate_summary(job.images, _gsd_from_job(job))
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    bbox_text = "—"
    if summary.bbox is not None:
        lon_min, lat_min, lon_max, lat_max = summary.bbox
        bbox_text = f"{lon_min:.6f}–{lon_max:.6f}°E，{lat_min:.6f}–{lat_max:.6f}°N"

    lines = [
        "# 无人机影像拼接预检报告",
        "",
        f"- 生成时间：{now}",
        f"- 影像目录：{job.image_dir}",
        f"- 输出目录：{job.output_dir}",
        f"- 处理模式：{_PRESET_LABEL.get(job.preset, job.preset)}",
        f"- 输出坐标系：EPSG:{job.crs_epsg}",
        "",
        "## 1. 数据概况",
        "",
        "| 项目 | 值 |",
        "| --- | --- |",
        f"| 影像数量 | {summary.image_count} 张 |",
        f"| 有定位影像 | {summary.positioned_count} 张 |",
        f"| RTK 固定解 | {summary.rtk_fixed_count} 张 |",
        f"| 覆盖范围 | {_md_cell(bbox_text)} |",
        f"| 相对航高 | {_fmt(summary.mean_agl, 1, ' m')} |",
        f"| 估算 GSD | {_fmt(summary.gsd_m * 100 if summary.gsd_m else None, 2, ' cm/px')} |",
        f"| 影像间距中位数 | {_fmt(summary.median_spacing_m, 1, ' m')} |",
        f"| 估算航向重叠 | {_fmt(summary.forward_overlap * 100 if summary.forward_overlap is not None else None, 0, '%')} |",
        "",
        "## 2. 空三输入检查",
        "",
        f"- 定位来源：{job.images[0].source if job.images and job.images[0].source else '无'}",
        f"- 相机：{_camera_text(job)}",
        f"- 控制点：{_gcp_text(job.gcp)}",
        f"- 空三模式：{'影像 GPS + 控制点' if job.alignment_mode == 'gcp' else '仅影像 GPS'}",
        "",
        "## 3. 处理参数",
        "",
        f"- 密集点云：{'生成（' + _DENSE_QUALITY_LABEL.get(job.dense_quality, job.dense_quality) + '质量）' if job.dense_cloud else '跳过'}",
        f"- DSM：{'生成' if job.build_dsm else '不生成'}；DTM：{'生成' if job.build_dtm else '不生成'}",
        f"- 输出 GSD：{'自动' if job.gsd_mode == 'auto' else f'{job.gsd_cm:g} cm'}",
        f"- 匀色：{'开启' if job.blend else '关闭'}；拼接线：{_SEAMLINE_LABEL.get(job.seamline, job.seamline)}",
        f"- 输出格式：{_FORMAT_LABEL.get(job.output_format, job.output_format)}",
        "",
        "## 4. 检查结果",
        "",
        "| 级别 | 检查项 | 说明 | 建议 |",
        "| --- | --- | --- | --- |",
    ]
    for issue in issues:
        lines.append(
            f"| {_LEVEL_LABEL.get(issue.level, issue.level)} | {_md_cell(issue.title)} | "
            f"{_md_cell(issue.detail)} | {_md_cell(issue.fix)} |"
        )
    errors = sum(1 for issue in issues if issue.level == "error")
    warnings = sum(1 for issue in issues if issue.level == "warn")
    lines.extend(
        [
            "",
            f"合计：{errors} 个错误，{warnings} 个警告。",
            "",
            "## 5. 下一步",
            "",
            "- 预检通过后，将按第 ③–⑤ 步的参数执行空三、密集点云与 DSM/正射影像生产。",
            "- 空三解算完成后，控制点/检查点残差会写入成果质检报告。",
            "- 成果按 `任务清单.json` 的分辨率与格式输出（GeoTIFF + TFW / PNG + PGW / JPEG + JGW）。",
            "",
        ]
    )
    return "\n".join(lines)


def _camera_text(job: UavJob) -> str:
    intrinsics = next(
        (image.intrinsics for image in job.images if image.intrinsics.is_complete), None
    )
    if intrinsics is None:
        intrinsics = job.images[0].intrinsics if job.images else None
    if intrinsics is None:
        return "—"
    model = intrinsics.model or intrinsics.make or "未知相机"
    parts = [model]
    if intrinsics.width_px and intrinsics.height_px:
        parts.append(f"{intrinsics.width_px}×{intrinsics.height_px}")
    if intrinsics.focal_mm:
        parts.append(f"焦距 {intrinsics.focal_mm:g} mm")
    if intrinsics.sensor_width_mm:
        parts.append(f"传感器宽 {intrinsics.sensor_width_mm:.2f} mm")
    return "，".join(parts)


def _gcp_text(gcp: GcpSet | None) -> str:
    if gcp is None or not gcp.points:
        return "未导入"
    return (
        f"{len(gcp.control_points)} 个控制点，{len(gcp.check_points)} 个检查点，"
        f"已刺点 {gcp.marked_point_count}/{len(gcp.points)} 个"
    )


def _atomic_write(path: Path, text: str) -> None:
    tmp_path = path.with_name(f"{path.name}.tmp")
    tmp_path.write_text(text, encoding="utf-8")
    os.replace(tmp_path, path)


def write_outputs(job: UavJob, issues: list[PrecheckIssue], output_dir: str = "") -> PrecheckOutputs:
    """写 ``预检报告.md`` 与 ``任务清单.json``（原子替换）。"""
    target = Path(output_dir or job.output_dir)
    target.mkdir(parents=True, exist_ok=True)
    summary = job.summary or estimate_summary(job.images, _gsd_from_job(job))
    report_path = target / "预检报告.md"
    manifest_path = target / "任务清单.json"

    manifest = job.to_manifest()
    manifest["created_at"] = datetime.now().isoformat(timespec="seconds")
    manifest["engine"] = {"available": ENGINE_AVAILABLE, "note": ENGINE_NOTE}
    manifest["precheck"] = {
        "errors": sum(1 for issue in issues if issue.level == "error"),
        "warnings": sum(1 for issue in issues if issue.level == "warn"),
        "issues": [
            {
                "level": issue.level,
                "code": issue.code,
                "title": issue.title,
                "detail": issue.detail,
                "fix": issue.fix,
            }
            for issue in issues
        ],
    }

    _atomic_write(report_path, render_report(job, issues, summary))
    _atomic_write(manifest_path, json.dumps(manifest, ensure_ascii=False, indent=2))
    return PrecheckOutputs(
        report_path=str(report_path),
        manifest_path=str(manifest_path),
        issues=tuple(issues),
        summary=summary,
    )


def run_precheck(
    job: UavJob,
    progress: ProgressCallback | None = None,
    cancel: CancelCallback | None = None,
) -> PrecheckOutputs:
    """预检入口：有 error 时抛 :class:`PrecheckError`，否则写报告与清单。"""
    if progress is not None:
        progress(10, "检查影像与参数…")
    summary = job.summary or estimate_summary(job.images, _gsd_from_job(job))
    issues = build_issues(job, summary)
    errors = [issue for issue in issues if issue.is_error]
    if errors:
        raise PrecheckError(errors)
    if cancel is not None and cancel():
        raise RuntimeError("任务已取消")
    if progress is not None:
        progress(60, "生成预检报告与任务清单…")
    outputs = write_outputs(job, issues, job.output_dir)
    if progress is not None:
        progress(100, "预检完成")
    return outputs


__all__ = [
    "PrecheckError",
    "ScanResult",
    "build_issues",
    "default_output_dir",
    "estimate_summary",
    "render_report",
    "run_precheck",
    "scan_images",
    "write_outputs",
]
