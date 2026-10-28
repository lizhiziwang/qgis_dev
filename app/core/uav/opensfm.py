"""OpenSfM 数据集准备与命令编排。

纯 Python（不 import qgis / osgeo），由 :mod:`app.core.uav.pipeline` 在后台
线程调用。所有 OpenSfM 命令都在外部 Python 子进程里执行，避免把 OpenSfM
的 C++ 扩展、OpenCV、GDAL 依赖带进 QGIS 进程。

职责：

- 把向导的 :class:`~app.core.uav.models.UavJob` 落成 OpenSfM 数据集
  （``images/`` 硬链接、``config.yaml``、相机内参、POS、GCP）；
- 按质量档位生成命令序列；
- 流式读取子进程日志、解析进度、支持取消；
- 解析 ``stats/stats.json`` 与 ``reconstruction.json`` 生成结构化结果。
"""

from __future__ import annotations

import contextlib
import json
import os
import queue
import shutil
import subprocess
import sys
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from app.core.uav.models import GcpPoint, ImageMeta, UavJob

ProgressCallback = Callable[[int, str], None]
CancelCallback = Callable[[], bool]

#: OpenSfM 质量档位（按“处理模式”和“密集点云质量”合并选择）
QUALITY_PROFILES: dict[str, dict[str, Any]] = {
    "fast": {
        "feature_process_size": 1600,
        "depthmap_max_image_size": 1600,
        "undistorted_image_max_size": 1600,
        "depthmap_max_iterations": 3,
        "depthmap_fusion_svo_voxel_level": "quarter",
    },
    "standard": {
        "feature_process_size": 2400,
        "depthmap_max_image_size": 3200,
        "undistorted_image_max_size": 3200,
        "depthmap_max_iterations": 4,
        "depthmap_fusion_svo_voxel_level": "fine",
    },
    "high": {
        "feature_process_size": 3200,
        "depthmap_max_image_size": 4096,
        "undistorted_image_max_size": 4096,
        "depthmap_max_iterations": 6,
        "depthmap_fusion_svo_voxel_level": "fine",
    },
}

#: 阶段权重（总和 100）。``export_report`` 失败不致命。
STAGE_WEIGHTS: dict[str, int] = {
    "extract_geolocation": 1,
    "extract_metadata": 3,
    "detect_features": 12,
    "match_features": 22,
    "create_tracks": 3,
    "reconstruct": 20,
    "undistort": 4,
    "dense_clustering": 2,
    "compute_depthmaps": 20,
    "fuse_depthmaps": 8,
    "dense_merging": 4,
    "compute_statistics": 1,
}

STAGE_LABELS: dict[str, str] = {
    "extract_geolocation": "导入影像定位",
    "extract_metadata": "读取影像元数据与相机参数",
    "detect_features": "提取影像特征点",
    "match_features": "影像特征匹配",
    "create_tracks": "连接同名点轨迹",
    "reconstruct": "空三解算（相机位置、姿态与稀疏点云）",
    "undistort": "影像去畸变",
    "dense_clustering": "密集点云分组与深度范围",
    "compute_depthmaps": "GPU 深度图计算",
    "fuse_depthmaps": "深度图融合为密集点云",
    "dense_merging": "生成 DSM 与正射影像",
    "compute_statistics": "统计精度指标",
    "export_report": "导出质量报告",
}

#: 这些阶段失败不阻断出图，只记警告
OPTIONAL_STAGES = {"export_report"}


class OpensfmError(RuntimeError):
    """OpenSfM 阶段执行失败。"""

    def __init__(self, stage: str, message: str, log_tail: str = "") -> None:
        super().__init__(message)
        self.stage = stage
        self.message = message
        self.log_tail = log_tail


@dataclass
class DatasetLayout:
    """OpenSfM 数据集路径。"""

    root: Path
    images: Path
    config: Path
    camera_overrides: Path
    geotags: Path
    gcp_json: Path
    logs: Path
    reconstruction: Path
    stats: Path
    report_pdf: Path
    dense_dir: Path
    fused_ply: Path
    fused_las: Path
    fused_laz: Path
    dsm: Path
    ortho: Path

    @classmethod
    def under(cls, work_dir: Path) -> DatasetLayout:
        root = work_dir / "opensfm"
        dense = root / "undistorted" / "depthmaps"
        return cls(
            root=root,
            images=root / "images",
            config=root / "config.yaml",
            camera_overrides=root / "camera_models_overrides.json",
            geotags=root / "geotags.csv",
            gcp_json=root / "ground_control_points.json",
            logs=work_dir / "logs",
            reconstruction=root / "reconstruction.json",
            stats=root / "stats" / "stats.json",
            report_pdf=root / "stats" / "report.pdf",
            dense_dir=dense,
            fused_ply=dense / "fused.ply",
            fused_las=dense / "fused.las",
            fused_laz=dense / "fused.laz",
            dsm=dense / "dsm.tif",
            ortho=dense / "ortho.tif",
        )


@dataclass
class Stage:
    """一个 OpenSfM 子命令。"""

    name: str
    args: tuple[str, ...] = ()
    optional: bool = False

    @property
    def label(self) -> str:
        return STAGE_LABELS.get(self.name, self.name)

    @property
    def weight(self) -> int:
        return STAGE_WEIGHTS.get(self.name, 0)


@dataclass
class ReconstructionSummary:
    """从 reconstruction.json / stats.json 提取的精度摘要。"""

    registered_images: int = 0
    total_images: int = 0
    sparse_points: int = 0
    reprojection_error_px: float | None = None
    gsd_m: float | None = None
    output_crs: str = ""
    gcp_average_m: float | None = None
    checkpoint_average_m: float | None = None
    gcp_details: list[dict[str, Any]] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "registered_images": self.registered_images,
            "total_images": self.total_images,
            "sparse_points": self.sparse_points,
            "reprojection_error_px": self.reprojection_error_px,
            "gsd_m": self.gsd_m,
            "output_crs": self.output_crs,
            "gcp_average_m": self.gcp_average_m,
            "checkpoint_average_m": self.checkpoint_average_m,
            "gcp_details": self.gcp_details,
            "warnings": self.warnings,
        }


# ---------------------------------------------------------------------- 数据集


def _cpu_count() -> int:
    try:
        return max(1, min(8, (os.cpu_count() or 4) - 2))
    except Exception:  # noqa: BLE001
        return 4


def _first_intrinsics(job: UavJob) -> tuple[float, int, int] | None:
    """返回 (归一化焦距, 宽, 高)；资料不足时返回 None。"""
    counts: dict[tuple[int, int], int] = {}
    for image in job.images:
        intr = image.intrinsics
        if intr.width_px and intr.height_px:
            key = (intr.width_px, intr.height_px)
            counts[key] = counts.get(key, 0) + 1
    majority = max(counts, key=counts.get) if counts else None

    for image in job.images:
        intr = image.intrinsics
        if not intr.width_px or not intr.height_px:
            continue
        if majority is not None and (intr.width_px, intr.height_px) != majority:
            continue
        focal_px = intr.focal_px
        if not focal_px and intr.focal_mm and intr.sensor_width_mm:
            focal_px = intr.focal_mm / intr.sensor_width_mm * intr.width_px
        if focal_px:
            return (
                float(focal_px) / max(intr.width_px, intr.height_px),
                int(intr.width_px),
                int(intr.height_px),
            )
    return None


def _quality_profile(job: UavJob) -> dict[str, Any]:
    if job.preset == "fast":
        key = "fast"
    elif job.preset == "high" or job.dense_quality == "high":
        key = "high"
    elif job.dense_quality == "low":
        key = "fast"
    else:
        key = "standard"
    return dict(QUALITY_PROFILES[key])


def write_config(job: UavJob, layout: DatasetLayout) -> dict[str, Any]:
    profile = _quality_profile(job)
    lines = [
        "# Generated by qgis_dev UAV wizard",
        f"processes: {_cpu_count()}",
        "io_processes: 4",
        "proj_cdn_enabled: false",
        "default_projection_type: perspective",
        "use_exif_size: true",
        "optimize_camera_parameters: true",
        "five_point_reversal_check: true",
        f"feature_process_size: {profile['feature_process_size']}",
        f"depthmap_max_image_size: {profile['depthmap_max_image_size']}",
        f"undistorted_image_max_size: {profile['undistorted_image_max_size']}",
        f"depthmap_max_iterations: {profile['depthmap_max_iterations']}",
        f"depthmap_fusion_svo_voxel_level: {profile['depthmap_fusion_svo_voxel_level']}",
        "depthmap_num_matching_views: 8",
        "depthmap_hierarchy_levels: 3",
        "dsm_enabled: true",
        f"dsm_merge_feather: {'true' if job.blend else 'false'}",
        "dense_pointcloud_export_las: true",
        "dense_pointcloud_export_laz: true",
        "depthmap_delete_raw_after_clean: true",
        "depthmap_delete_fusion_batches: true",
        "depthmap_save_debug_ply: false",
        "gcp_horizontal_sd: 0.02",
        "gcp_vertical_sd: 0.05",
        "gcp_global_weight: 0.04",
        "",
    ]
    layout.config.write_text("\n".join(lines), encoding="utf-8")
    return profile


def write_camera_overrides(job: UavJob, layout: DatasetLayout) -> bool:
    intrinsics = _first_intrinsics(job)
    if intrinsics is None:
        if layout.camera_overrides.exists():
            layout.camera_overrides.unlink()
        return False
    focal, width, height = intrinsics
    payload = {
        "all": {
            "projection_type": "perspective",
            "width": width,
            "height": height,
            "focal": round(focal, 12),
            "k1": 0.0,
            "k2": 0.0,
        }
    }
    layout.camera_overrides.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return True


def _image_altitude(image: ImageMeta, job: UavJob) -> float:
    if image.altitude_ellipsoid is not None:
        return float(image.altitude_ellipsoid)
    if image.relative_altitude is not None and job.ground_elevation_m is not None:
        return float(image.relative_altitude) + float(job.ground_elevation_m)
    return 0.0


def write_geotags(job: UavJob, layout: DatasetLayout) -> bool:
    """写 ``geotags.csv``（``文件名,经度,纬度,高程``），供 extract_geolocation 使用。

    只在至少一张影像有经纬度时写。坐标顺序按 OpenSfM 的 WGS84 约定：
    第一列经度、第二列纬度。
    """
    rows = ["# filename,longitude,latitude,altitude"]
    positioned = 0
    for image in job.images:
        if not image.has_position:
            continue
        rows.append(
            f"{image.name},{image.lon:.10f},{image.lat:.10f},"
            f"{_image_altitude(image, job):.4f}"
        )
        positioned += 1
    if not positioned:
        if layout.geotags.exists():
            layout.geotags.unlink()
        return False
    layout.geotags.write_text("\n".join(rows) + "\n", encoding="utf-8")
    return True


def _normalized_projection(px: float, py: float, width: int, height: int) -> tuple[float, float]:
    size = float(max(width, height))
    return (
        (float(px) + 0.5 - width / 2.0) / size,
        (float(py) + 0.5 - height / 2.0) / size,
    )


def gcp_json_payload(
    job: UavJob,
    layout: DatasetLayout,
    *,
    use_gcp_roles: bool,
) -> dict[str, Any]:
    """把向导 GCP（像素刺点）转成 OpenSfM ``ground_control_points.json``。

    影像名使用数据集内的文件名；像素坐标按 OpenSfM 约定归一化：
    原点在影像中心，长边为 1。``use_gcp_roles=False`` 时全部按检查点处理，
    只参与精度统计、不参与优化。
    """
    by_name = {image.name: image for image in job.images}
    points: list[dict[str, Any]] = []
    gcp_set = job.gcp
    if gcp_set is not None:
        for index, point in enumerate(gcp_set.points, start=1):
            observations = []
            for image_name, (px, py) in point.marks.items():
                image = by_name.get(image_name)
                if image is None:
                    continue
                width = image.intrinsics.width_px
                height = image.intrinsics.height_px
                if not width or not height:
                    continue
                nx, ny = _normalized_projection(px, py, width, height)
                observations.append(
                    {
                        "shot_id": image_name,
                        "projection": [round(nx, 12), round(ny, 12)],
                    }
                )
            if not observations:
                continue
            role = "gcp" if use_gcp_roles and not point.is_checkpoint else "checkpoint"
            name = _unique_point_id(point, index, {p.get("id") for p in points})
            entry: dict[str, Any] = {
                "id": name,
                "role": role,
                "position": {
                    "easting": float(point.east),
                    "northing": float(point.north),
                    "altitude": float(point.height),
                },
                "observations": observations,
            }
            points.append(entry)
    return {"crs": f"EPSG:{job.crs_epsg}", "points": points}


def _unique_point_id(point: GcpPoint, index: int, used: set[str | None]) -> str:
    base = (point.name or f"P{index:02d}").strip() or f"P{index:02d}"
    name = base
    suffix = 2
    while name in used:
        name = f"{base}_{suffix}"
        suffix += 1
    return name


def write_gcp(
    job: UavJob, layout: DatasetLayout, *, use_gcp_roles: bool
) -> int:
    payload = gcp_json_payload(job, layout, use_gcp_roles=use_gcp_roles)
    layout.gcp_json.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return len(payload["points"])


def prepare_dataset(
    job: UavJob,
    work_dir: str | Path,
    *,
    geotags: bool | None = None,
    use_gcp_roles: bool = True,
    progress: ProgressCallback | None = None,
    cancel: CancelCallback | None = None,
) -> tuple[DatasetLayout, dict[str, Any]]:
    """创建 OpenSfM 数据集；返回路径与本次使用的质量参数。"""
    layout = DatasetLayout.under(Path(work_dir))
    layout.images.mkdir(parents=True, exist_ok=True)
    layout.logs.mkdir(parents=True, exist_ok=True)

    total = max(1, len(job.images))
    for index, image in enumerate(job.images, start=1):
        if cancel is not None and cancel():
            raise RuntimeError("任务已取消")
        source = Path(image.path)
        if not source.is_file():
            raise FileNotFoundError(f"影像不存在：{source}")
        target = layout.images / image.name
        if not target.exists():
            try:
                os.link(source, target)
            except OSError:
                shutil.copy2(source, target)
        if progress is not None:
            progress(int(index / total * 100), f"准备影像 {index}/{total}")
    if not job.images:
        raise ValueError("没有可处理的影像")

    profile = write_config(job, layout)
    write_camera_overrides(job, layout)
    if geotags is None:
        geotags = bool(job.position_file)
    if geotags:
        write_geotags(job, layout)
    elif layout.geotags.exists():
        layout.geotags.unlink()

    wrote_gcp = write_gcp(job, layout, use_gcp_roles=use_gcp_roles)
    if not wrote_gcp:
        # 没有任何控制点/检查点时仍声明输出坐标系：OpenSfM 会优先采用
        # GCP 文件的 CRS（即使 points 为空），这样 DSM/正射可直接落在
        # 用户选择的投影坐标系里，避免事后重投影。
        layout.gcp_json.write_text(
            json.dumps(
                {"crs": f"EPSG:{job.crs_epsg}", "points": []},
                ensure_ascii=False,
                indent=2,
            ),
            encoding="utf-8",
        )
    if progress is not None:
        progress(100, "数据集准备完成")
    return layout, profile


# ---------------------------------------------------------------------- 命令


def build_stages(
    job: UavJob,
    layout: DatasetLayout,
    *,
    has_geotags: bool,
    dense: bool = True,
) -> list[Stage]:
    """生成命令序列；密集点云关闭时只做到空三与统计报告。"""
    stages: list[Stage] = []
    if has_geotags:
        stages.append(
            Stage(
                "extract_geolocation",
                ("--geotag-file", str(layout.geotags), "--crs", "WGS84"),
            )
        )
    stages.extend(
        [
            Stage("extract_metadata"),
            Stage("detect_features"),
            Stage("match_features"),
            Stage("create_tracks"),
            Stage("reconstruct"),
        ]
    )
    if dense:
        stages.extend(
            [
                Stage("undistort"),
                Stage("dense_clustering"),
                Stage("compute_depthmaps"),
                Stage("fuse_depthmaps"),
                Stage("dense_merging", ("--georeferenced",)),
            ]
        )
    stages.extend(
        [
            Stage("compute_statistics"),
            Stage("export_report", optional=True),
        ]
    )
    return stages


def cli_script_text() -> str:
    """写入工作目录的 OpenSfM CLI 启动脚本（由引擎 Python 执行）。"""
    return '''"""Minimal OpenSfM CLI entry point (generated)."""
import contextlib

from opensfm import commands
from opensfm.dataset import DataSet


@contextlib.contextmanager
def create_default_dataset_context(dataset_path, dataset_type=""):
    dataset = DataSet(dataset_path)
    try:
        yield dataset
    finally:
        dataset.clean_up()


def main():
    commands.command_runner(
        commands.opensfm_commands,
        create_default_dataset_context,
        dataset_choices=["opensfm"],
    )


if __name__ == "__main__":
    main()
'''


def write_cli_script(work_dir: str | Path) -> Path:
    path = Path(work_dir) / "opensfm_cli.py"
    path.write_text(cli_script_text(), encoding="utf-8")
    return path


def build_process_env(python: str, engine_root: str = "") -> dict[str, str]:
    """构造干净的引擎子进程环境，避免 QGIS/OSGeo4W 的 PATH/PYTHONPATH 污染。"""
    env = dict(os.environ)
    roots: list[Path] = []
    if engine_root:
        roots.append(Path(engine_root))
    python_path = Path(python)
    if python_path.parent.name.lower() == "bin":
        roots.append(python_path.parent.parent)
    else:
        roots.append(python_path.parent)

    path_parts: list[str] = []
    for root in roots:
        path_parts.extend(
            [
                str(root),
                str(root / "Library" / "bin"),
                str(root / "Library" / "mingw-w64" / "bin"),
                str(root / "Scripts"),
                str(root / "DLLs"),
            ]
        )
    path_parts.append(env.get("PATH", ""))
    env["PATH"] = os.pathsep.join([p for p in path_parts if p])

    env.pop("PYTHONHOME", None)
    env.pop("PYTHONPATH", None)
    for key in ("GDAL_DATA", "GDAL_DRIVER_PATH", "PROJ_LIB", "PROJ_DATA", "QT_PLUGIN_PATH"):
        env.pop(key, None)
    env["PYTHONUTF8"] = "1"
    env["PYTHONIOENCODING"] = "utf-8"
    env["PROJ_NETWORK"] = "OFF"
    env["GDAL_HTTP_TIMEOUT"] = "10"
    # 组件是便携环境，Tk/Tcl 数据目录不随包迁移；统计阶段会用 matplotlib
    # 画残差图，强制无界面后端避免 _tkinter.TclError。
    env["MPLBACKEND"] = "Agg"
    env["OMP_NUM_THREADS"] = env.get("OMP_NUM_THREADS", "4")
    return env


def _kill_process_tree(process: subprocess.Popen[str]) -> None:
    if process.poll() is not None:
        return
    if sys.platform == "win32":
        try:
            subprocess.run(
                ["taskkill", "/F", "/T", "/PID", str(process.pid)],
                capture_output=True,
                timeout=15,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
            return
        except Exception:  # noqa: BLE001
            pass
    with contextlib.suppress(Exception):
        process.kill()


def _read_lines(
    process: subprocess.Popen[str], output: queue.Queue[str | None]
) -> None:
    try:
        assert process.stdout is not None
        for line in process.stdout:
            output.put(line)
    finally:
        output.put(None)


def _progress_from_line(stage: str, line: str, state: dict[str, Any]) -> float | None:
    """从 OpenSfM 日志行估算阶段内进度（0–1），解析不到返回 None。"""
    import re

    if stage == "compute_depthmaps":
        match = re.search(r"\((\d+)/(\d+)\s+views\)", line)
        if match:
            done, total = int(match.group(1)), int(match.group(2))
            if total > 0:
                return min(1.0, done / total)
        match = re.search(r"Cluster (\d+).*?done", line)
        if match:
            state["clusters_done"] = state.get("clusters_done", 0) + 1
    elif stage == "fuse_depthmaps":
        match = re.search(r"Fusion:\s*(\d+)\s+chunk", line)
        if match:
            state["total_units"] = int(match.group(1))
        match = re.search(r"^.*Unit (\d+):", line)
        if match and state.get("total_units"):
            return min(1.0, (int(match.group(1)) + 1) / state["total_units"])
    elif stage == "dense_clustering":
        match = re.search(r"\[clusters [^\]]+\].*?(\d+) cluster", line)
        if match:
            state["clusters_done"] = state.get("clusters_done", 0) + 1
    return None


def run_stage(
    stage: Stage,
    *,
    python: str,
    engine_root: str,
    cli_script: Path,
    dataset_root: Path,
    log_path: Path,
    progress: ProgressCallback | None,
    cancel: CancelCallback | None,
    base_percent: float,
    span_percent: float,
    on_log: Callable[[str], None] | None = None,
) -> None:
    """执行一个 OpenSfM 命令；进度映射到 [base, base+span] 区间。"""
    command = [python, str(cli_script), stage.name, *stage.args, str(dataset_root)]
    env = build_process_env(python, engine_root)
    log_path.parent.mkdir(parents=True, exist_ok=True)
    started = time.time()
    last_emit = 0.0
    state: dict[str, Any] = {}
    log_tail: list[str] = []

    if progress is not None:
        progress(int(base_percent), f"{stage.label}…")
    with log_path.open("w", encoding="utf-8", errors="replace") as log:
        log.write(f"# {' '.join(command)}\n")
        log.flush()
        process = subprocess.Popen(
            command,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            stdin=subprocess.DEVNULL,
            text=True,
            encoding="utf-8",
            errors="replace",
            bufsize=1,
            env=env,
            cwd=str(dataset_root),
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        lines: queue.Queue[str | None] = queue.Queue()
        reader = threading.Thread(
            target=_read_lines, args=(process, lines), daemon=True
        )
        reader.start()
        cancelled = False
        while True:
            if cancel is not None and cancel():
                cancelled = True
                _kill_process_tree(process)
                break
            try:
                line = lines.get(timeout=0.3)
            except queue.Empty:
                line = ""
                elapsed = time.time() - started
                if progress is not None and elapsed - last_emit >= 10:
                    last_emit = elapsed
                    progress(
                        int(base_percent + span_percent * 0.05),
                        f"{stage.label}… 已用时 {_format_elapsed(elapsed)}",
                    )
            if line is None:
                break
            if line:
                log.write(line)
                log.flush()
                log_tail.append(line.rstrip())
                if len(log_tail) > 80:
                    del log_tail[:20]
                fraction = _progress_from_line(stage.name, line, state)
                if fraction is not None and progress is not None:
                    progress(
                        int(base_percent + span_percent * fraction),
                        f"{stage.label}… {fraction * 100:.0f}%",
                    )
                if on_log is not None:
                    text = line.strip()
                    if text:
                        on_log(text)
        code = process.wait()
        elapsed = time.time() - started
        log.write(f"\n# exit={code} elapsed={elapsed:.1f}s\n")

    if cancelled:
        raise OpensfmError(stage.name, "任务已取消")
    if code != 0:
        tail = "\n".join(log_tail[-30:])
        message = _friendly_failure(stage, tail)
        raise OpensfmError(stage.name, message, tail)
    if progress is not None:
        progress(
            int(base_percent + span_percent),
            f"{stage.label}完成（{_format_elapsed(elapsed)}）",
        )


def _format_elapsed(seconds: float) -> str:
    seconds = max(0, int(seconds))
    minutes, sec = divmod(seconds, 60)
    if minutes >= 60:
        hours, minutes = divmod(minutes, 60)
        return f"{hours:02d}:{minutes:02d}:{sec:02d}"
    return f"{minutes:02d}:{sec:02d}"


def _friendly_failure(stage: Stage, tail: str) -> str:
    lower = tail.lower()
    if "no opencl devices" in lower or "cannot compute depthmaps" in lower:
        return (
            "未检测到可用的 OpenCL 显卡设备，无法计算密集点云。"
            "请更新显卡驱动，或在第 ③ 步关闭密集点云后重试。"
        )
    if "not enough images" in lower or "reconstruction failed" in lower:
        return "空三解算失败：影像重叠不足或同名点太少，请检查航向/旁向重叠与影像质量。"
    if "no such file" in lower or "could not find" in lower:
        return f"{stage.label}失败：缺少文件或依赖，详见日志。"
    if "gcp" in lower and ("error" in lower or "invalid" in lower):
        return f"{stage.label}失败：控制点数据有误，请检查点号、坐标与刺点。"
    return f"{stage.label}失败（OpenSfM 退出码非 0），详见日志。"


# ---------------------------------------------------------------------- 结果


def read_reconstruction(layout: DatasetLayout) -> list[dict[str, Any]]:
    if not layout.reconstruction.is_file():
        return []
    try:
        data = json.loads(layout.reconstruction.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    return data if isinstance(data, list) else []


def summarize(
    layout: DatasetLayout, total_images: int, job: UavJob
) -> ReconstructionSummary:
    summary = ReconstructionSummary(total_images=total_images)
    reconstructions = read_reconstruction(layout)
    for rec in reconstructions:
        shots = rec.get("shots") or {}
        points = rec.get("points") or {}
        summary.registered_images += len(shots)
        summary.sparse_points += len(points)
    if layout.stats.is_file():
        try:
            stats = json.loads(layout.stats.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            stats = {}
        summary.output_crs = str(stats.get("output_coordinate_system") or "")
        recon_stats = stats.get("reconstruction_statistics") or {}
        value = recon_stats.get("reprojection_error_pixels")
        if isinstance(value, (int, float)):
            summary.reprojection_error_px = float(value)
        value = recon_stats.get("gsd")
        if isinstance(value, (int, float)) and value > 0:
            summary.gsd_m = float(value)
        gcp_errors = stats.get("gcp_errors") or {}
        value = (gcp_errors.get("gcp_only") or {}).get("average_error")
        if isinstance(value, (int, float)):
            summary.gcp_average_m = float(value)
        value = (gcp_errors.get("cp_only") or {}).get("average_error")
        if isinstance(value, (int, float)):
            summary.checkpoint_average_m = float(value)
        details = gcp_errors.get("details")
        if isinstance(details, list):
            summary.gcp_details = [d for d in details if isinstance(d, dict)]

    if summary.total_images and summary.registered_images < summary.total_images:
        missing = summary.total_images - summary.registered_images
        summary.warnings.append(
            f"{missing} 张影像未参与空三（可能重叠不足或匹配失败）。"
        )
    if (
        summary.reprojection_error_px is not None
        and summary.reprojection_error_px > 1.5
    ):
        summary.warnings.append(
            f"平均重投影误差 {summary.reprojection_error_px:.2f} 像素，"
            "空三质量偏低，建议检查影像重叠或相机参数。"
        )
    gcp = job.gcp
    if gcp is not None and gcp.check_points and summary.checkpoint_average_m is None:
        summary.warnings.append("检查点未参与精度统计（刺点不足或未进入空三）。")
    return summary


def stats_or_none(layout: DatasetLayout) -> dict[str, Any]:
    if not layout.stats.is_file():
        return {}
    try:
        return json.loads(layout.stats.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def read_reference_altitude(layout: DatasetLayout) -> float | None:
    """读取 OpenSfM 参考点大地高，供无 GCP 时估算绝对高程。"""
    path = layout.root / "reference_lla.json"
    if not path.is_file():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return float(data.get("altitude")) if data.get("altitude") is not None else None
    except (OSError, ValueError, TypeError):
        return None


__all__ = [
    "QUALITY_PROFILES",
    "STAGE_LABELS",
    "STAGE_WEIGHTS",
    "DatasetLayout",
    "OpensfmError",
    "ReconstructionSummary",
    "Stage",
    "build_process_env",
    "build_stages",
    "gcp_json_payload",
    "prepare_dataset",
    "read_reconstruction",
    "read_reference_altitude",
    "run_stage",
    "stats_or_none",
    "summarize",
    "write_cli_script",
]
