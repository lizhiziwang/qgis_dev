"""无人机影像拼接流水线：OpenSfM 空三 → 密集点云 → DSM → DOM。

对外入口 :func:`run_orthomosaic` 在后台线程里执行，通过 ``progress`` 回调
报告进度与阶段文字，通过 ``cancel`` 回调响应取消。所有 OpenSfM 命令都在
外部引擎 Python 子进程里跑，本模块只做数据准备、编排、解析与后处理。
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import os
import shutil
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from app.core.uav.engine import (
    ENV_ENGINE_PYTHON,
    ENV_ENGINE_ROOT,
    CancelledError,
    EngineInfo,
    probe_engine,
)
from app.core.uav.models import UavJob
from app.core.uav.opensfm import (
    DatasetLayout,
    OpensfmError,
    ReconstructionSummary,
    build_stages,
    prepare_dataset,
    read_reconstruction,
    run_stage,
    summarize,
    write_cli_script,
)
from app.core.uav.postprocess import OutputPaths, export_outputs

ProgressCallback = Callable[[int, str], None]
CancelCallback = Callable[[], bool]

ENGINE_AVAILABLE = True
ENGINE_NOTE = (
    "已接入 OpenSfM 实景三维解算：空三、稠密点云、DSM 与正射影像均可生成。"
)

#: 中间文件目录名（位于输出目录下）
WORK_DIR_NAME = "_processing"

#: 重跑签名文件
SIGNATURE_NAME = "run_signature.json"


class EngineUnavailableError(RuntimeError):
    """找不到或无法启动拼接引擎。"""


@dataclass
class TaskResult:
    """一次完整拼接的结果。"""

    output_dir: str
    work_dir: str
    summary: ReconstructionSummary
    outputs: OutputPaths
    engine: dict[str, Any] = field(default_factory=dict)
    elapsed_seconds: float = 0.0
    stage_times: dict[str, float] = field(default_factory=dict)
    warnings: list[str] = field(default_factory=list)
    log_dir: str = ""

    @property
    def dom(self) -> str:
        return self.outputs.dom

    @property
    def report(self) -> str:
        return self.outputs.report

    def to_dict(self) -> dict[str, Any]:
        return {
            "output_dir": self.output_dir,
            "work_dir": self.work_dir,
            "summary": self.summary.to_dict(),
            "outputs": self.outputs.to_dict(),
            "engine": self.engine,
            "elapsed_seconds": self.elapsed_seconds,
            "stage_times": self.stage_times,
            "warnings": self.warnings,
            "log_dir": self.log_dir,
        }


def run_orthomosaic(
    job: UavJob,
    output_path: str,
    progress: ProgressCallback | None = None,
    cancel: CancelCallback | None = None,
    *,
    engine_python: str = "",
    engine_root: str = "",
) -> TaskResult:
    """执行完整拼接流程并返回成果路径。

    ``output_path`` 是成果目录；中间文件放在其下的 ``_processing``。
    ``engine_python`` 为空时读取环境变量 ``QGIS_UAV_ENGINE_PYTHON``。
    """
    started = time.time()
    python = engine_python or os.environ.get(ENV_ENGINE_PYTHON, "")
    root = engine_root or os.environ.get(ENV_ENGINE_ROOT, "")
    if not python:
        raise EngineUnavailableError(
            "尚未配置拼接引擎。请到「无人机 → 拼接引擎设置」安装引擎组件，"
            "或指定已安装 OpenSfM 的 Python 环境。"
        )
    if not Path(python).is_file():
        raise EngineUnavailableError(f"拼接引擎 Python 不存在：{python}")

    info = EngineInfo(python=python, root=root, source="user", message="未校验")
    probe_engine(info)
    if not info.available:
        raise EngineUnavailableError(info.message or "拼接引擎不可用，请重新安装。")

    if progress is not None:
        progress(0, "检查任务参数…")
    if not job.images:
        raise ValueError("任务不包含任何影像。")
    output_dir = Path(output_path or job.output_dir)
    if not str(output_dir):
        raise ValueError("未设置输出目录。")
    work_dir = output_dir / WORK_DIR_NAME
    work_dir.mkdir(parents=True, exist_ok=True)
    logs_dir = work_dir / "logs"
    logs_dir.mkdir(exist_ok=True)
    if progress is not None:
        progress(1, "准备 OpenSfM 数据集…")

    layout, profile = prepare_dataset(
        job,
        work_dir,
        use_gcp_roles=job.alignment_mode == "gcp",
        progress=(
            None
            if progress is None
            else lambda p, text: progress(1 + int(p / 100 * 2), text)
        ),
        cancel=cancel,
    )
    _clear_stale_inputs(job, layout)
    _clean_stale_derived_data(job, layout, work_dir, profile, python)

    cli_script = write_cli_script(work_dir)
    stages = build_stages(
        job, layout, has_geotags=layout.geotags.is_file(), dense=job.dense_cloud
    )
    total_weight = sum(max(1, stage.weight) for stage in stages)
    stage_times: dict[str, float] = {}
    warnings: list[str] = []
    cursor = 0
    _check_cancel(cancel)
    if progress is not None:
        progress(3, "开始空三解算…")

    for stage in stages:
        _check_cancel(cancel)
        share = max(1, stage.weight) / total_weight
        base = 3 + 92 * cursor / total_weight
        span = 92 * share
        cursor += max(1, stage.weight)
        stage_started = time.time()
        try:
            run_stage(
                stage,
                python=python,
                engine_root=root,
                cli_script=cli_script,
                dataset_root=layout.root,
                log_path=logs_dir / f"{stage.name}.log",
                progress=progress,
                cancel=cancel,
                base_percent=base,
                span_percent=span,
            )
        except OpensfmError as exc:
            if exc.message == "任务已取消":
                raise CancelledError("任务已取消") from exc
            if stage.optional:
                warnings.append(f"{stage.label}失败（不影响成果）：{exc.message}")
                if progress is not None:
                    progress(int(base + span), f"{stage.label}失败，已跳过")
                continue
            raise
        finally:
            stage_times[stage.name] = round(time.time() - stage_started, 1)

        if stage.name == "reconstruct":
            _verify_reconstruction(layout, job)
    _check_cancel(cancel)
    summary = summarize(layout, len(job.images), job)
    summary.warnings.extend(warnings)
    if summary.registered_images == 0:
        raise OpensfmError(
            "reconstruct",
            "空三没有成功注册任何影像。请检查影像重叠度、曝光与镜头参数。",
        )
    if job.dense_cloud and not layout.ortho.is_file():
        raise OpensfmError(
            "dense_merging",
            "密集点云已完成，但未生成正射影像。请检查日志中的 DSM/正射渲染错误。",
            _tail_log(logs_dir / "dense_merging.log"),
        )

    if progress is not None:
        progress(95, "整理成果…")
    outputs = export_outputs(
        job,
        layout,
        summary,
        output_dir=output_dir,
        progress=(
            None
            if progress is None
            else lambda p, text: progress(95 + int(p / 100 * 5), text)
        ),
        cancel=cancel,
        engine_name=f"OpenSfM {info.opensfm_version or info.version}",
        elapsed_seconds=time.time() - started,
    )
    elapsed = time.time() - started
    result = TaskResult(
        output_dir=str(output_dir),
        work_dir=str(work_dir),
        summary=summary,
        outputs=outputs,
        engine=info.to_dict(),
        elapsed_seconds=elapsed,
        stage_times=stage_times,
        warnings=[*summary.warnings, *outputs.warnings],
        log_dir=str(logs_dir),
    )
    _write_run_result(work_dir, result)
    if progress is not None:
        if job.dense_cloud and outputs.dom:
            progress(100, f"处理完成（{_duration_text(elapsed)}）")
        else:
            progress(100, f"空三处理完成（{_duration_text(elapsed)}）")
    return result


def _check_cancel(cancel: CancelCallback | None) -> None:
    if cancel is not None and cancel():
        raise CancelledError("任务已取消")


def _clear_stale_inputs(job: UavJob, layout: DatasetLayout) -> None:
    """防止上一次运行的位置文件残留在数据集里。"""
    if not layout.geotags.is_file():
        with contextlib.suppress(OSError):
            (layout.root / "exif_overrides.json").unlink()


def _run_signature(
    job: UavJob, layout: DatasetLayout, profile: dict[str, Any], python: str
) -> str:
    digest = hashlib.sha256()

    def add(value: str) -> None:
        digest.update(value.encode("utf-8", errors="replace"))
        digest.update(b"\0")

    add(f"engine={python}")
    add(f"crs={job.crs_epsg}")
    add(f"mode={job.alignment_mode}")
    add(f"preset={job.preset}")
    add(f"dense={job.dense_cloud}")
    add(f"quality={json.dumps(profile, sort_keys=True)}")
    for image in job.images:
        path = Path(image.path)
        try:
            stat = path.stat()
            add(f"{image.name}|{stat.st_size}|{stat.st_mtime_ns}")
        except OSError:
            add(f"{image.name}|missing")
    for path in (layout.geotags, layout.camera_overrides, layout.gcp_json, layout.config):
        if path.is_file():
            add(f"{path.name}|{hashlib.sha256(path.read_bytes()).hexdigest()}")
        else:
            add(f"{path.name}|absent")
    return digest.hexdigest()


def _clean_stale_derived_data(
    job: UavJob,
    layout: DatasetLayout,
    work_dir: Path,
    profile: dict[str, Any],
    python: str,
) -> None:
    """输入或质量参数变化时清掉旧的中间结果，避免复用过期特征。"""
    signature_path = work_dir / SIGNATURE_NAME
    signature = _run_signature(job, layout, profile, python)
    try:
        previous = json.loads(signature_path.read_text(encoding="utf-8")).get("signature")
    except (OSError, ValueError):
        previous = None
    if previous == signature:
        return
    keep = {"images", "config.yaml", "camera_models_overrides.json", "ground_control_points.json"}
    if layout.root.is_dir():
        for child in layout.root.iterdir():
            if child.name in keep:
                continue
            if child.is_dir():
                shutil.rmtree(child, ignore_errors=True)
            else:
                with contextlib.suppress(OSError):
                    child.unlink()
    signature_path.write_text(
        json.dumps(
            {
                "signature": signature,
                "created_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
                "images": len(job.images),
                "preset": job.preset,
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )


def _verify_reconstruction(layout: DatasetLayout, job: UavJob) -> None:
    records = read_reconstruction(layout)
    registered = sum(len(record.get("shots") or {}) for record in records)
    if registered == 0:
        raise OpensfmError(
            "reconstruct",
            "空三失败：没有影像成功注册。常见原因：航向/旁向重叠不足、"
            "影像模糊、镜头参数错误，或控制点坐标/刺点不匹配。",
            _tail_log(layout.logs / "reconstruct.log"),
        )


def _tail_log(path: Path, lines: int = 30) -> str:
    try:
        content = path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return ""
    return "\n".join(content[-lines:])


def _duration_text(seconds: float) -> str:
    seconds = max(0, int(seconds))
    minutes, sec = divmod(seconds, 60)
    if minutes >= 60:
        hours, minutes = divmod(minutes, 60)
        return f"{hours} 小时 {minutes} 分"
    if minutes:
        return f"{minutes} 分 {sec} 秒"
    return f"{sec} 秒"


def _write_run_result(work_dir: Path, result: TaskResult) -> None:
    with contextlib.suppress(OSError):
        (work_dir / "run_result.json").write_text(
            json.dumps(result.to_dict(), ensure_ascii=False, indent=2),
            encoding="utf-8",
        )


__all__ = [
    "ENGINE_AVAILABLE",
    "ENGINE_NOTE",
    "EngineUnavailableError",
    "TaskResult",
    "run_orthomosaic",
]
