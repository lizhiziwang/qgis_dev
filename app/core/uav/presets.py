"""处理模式预设：快拼 / 标准 / 高精度。"""

from __future__ import annotations

from dataclasses import dataclass

from app.core.uav.models import UavJob


@dataclass(frozen=True)
class Preset:
    """一套下游参数默认值。"""

    key: str
    label: str
    summary: str
    dense_cloud: bool
    dense_quality: str
    build_dsm: bool
    build_dtm: bool
    require_gcp: bool


PRESETS: tuple[Preset, ...] = (
    Preset(
        key="fast",
        label="快拼（最快）",
        summary="跳过密集点云和 DSM，用平均高程快速出图；速度快，精度较低。",
        dense_cloud=False,
        dense_quality="low",
        build_dsm=False,
        build_dtm=False,
        require_gcp=False,
    ),
    Preset(
        key="standard",
        label="标准（推荐）",
        summary="生成密集点云和 DSM，兼顾精度与速度；建议导入控制点。",
        dense_cloud=True,
        dense_quality="medium",
        build_dsm=True,
        build_dtm=False,
        require_gcp=False,
    ),
    Preset(
        key="high",
        label="高精度（含控制点）",
        summary="高质量密集点云 + DSM；要求至少 3 个控制点并完成刺点。",
        dense_cloud=True,
        dense_quality="high",
        build_dsm=True,
        build_dtm=False,
        require_gcp=True,
    ),
)

PRESET_MAP: dict[str, Preset] = {preset.key: preset for preset in PRESETS}


def get_preset(key: str) -> Preset:
    return PRESET_MAP.get(key, PRESET_MAP["standard"])


def apply_preset(job: UavJob, key: str) -> Preset:
    """把预设写入任务参数；已导入控制点时高精度模式自动切到控制点空三。"""
    preset = get_preset(key)
    job.preset = preset.key
    job.dense_cloud = preset.dense_cloud
    job.dense_quality = preset.dense_quality
    job.build_dsm = preset.build_dsm
    job.build_dtm = preset.build_dtm
    if preset.require_gcp and job.gcp and job.gcp.points:
        job.alignment_mode = "gcp"
    elif preset.key == "fast":
        job.alignment_mode = "gps"
    return preset


__all__ = ["PRESETS", "PRESET_MAP", "Preset", "apply_preset", "get_preset"]
