"""无人机航测（影像拼接）核心逻辑。

模块只依赖标准库 + exifread（延迟导入），不 import qgis / osgeo。
"""

from app.core.uav.models import (
    CameraIntrinsics,
    FlightSummary,
    GcpPoint,
    GcpSet,
    ImageMeta,
    PrecheckIssue,
    PrecheckOutputs,
    UavJob,
)
from app.core.uav.presets import PRESET_MAP, PRESETS, Preset, apply_preset, get_preset

__all__ = [
    "PRESETS",
    "PRESET_MAP",
    "CameraIntrinsics",
    "FlightSummary",
    "GcpPoint",
    "GcpSet",
    "ImageMeta",
    "PrecheckIssue",
    "PrecheckOutputs",
    "Preset",
    "UavJob",
    "apply_preset",
    "get_preset",
]
