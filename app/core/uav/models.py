"""无人机航测向导的数据模型。

纯 Python（不 import qgis / osgeo），方便在无 QGIS 的测试环境里直接跑。
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any


@dataclass(frozen=True)
class CameraIntrinsics:
    """相机内参（尽量从 EXIF / DJI XMP 读取，缺项留 ``None``）。"""

    make: str = ""
    model: str = ""
    width_px: int = 0
    height_px: int = 0
    focal_mm: float | None = None
    focal_px: float | None = None
    sensor_width_mm: float | None = None
    principal_x: float | None = None
    principal_y: float | None = None
    dewarp: tuple[float, ...] = ()

    @property
    def pixel_size_mm(self) -> float | None:
        """像元尺寸（mm）；优先由像素焦距反算，其次由传感器宽度反算。"""
        if self.focal_mm and self.focal_px:
            return self.focal_mm / self.focal_px
        if self.sensor_width_mm and self.width_px:
            return self.sensor_width_mm / self.width_px
        return None

    @property
    def is_complete(self) -> bool:
        """焦距、传感器宽度与影像尺寸是否齐全（够算 GSD）。"""
        return bool(self.width_px and self.height_px and self.focal_mm and self.sensor_width_mm)


@dataclass
class ImageMeta:
    """单张航片的元数据。"""

    path: str
    name: str
    lon: float | None = None
    lat: float | None = None
    altitude_ellipsoid: float | None = None
    relative_altitude: float | None = None
    gimbal_yaw: float | None = None
    gimbal_pitch: float | None = None
    gimbal_roll: float | None = None
    flight_yaw: float | None = None
    rtk_flag: int | None = None
    rtk_std: tuple[float, float, float] | None = None
    source: str = ""
    intrinsics: CameraIntrinsics = field(default_factory=CameraIntrinsics)
    exif_error: str = ""

    @property
    def has_position(self) -> bool:
        return self.lon is not None and self.lat is not None

    @property
    def agl(self) -> float | None:
        """相对航高（m）；测试数据来自 DJI XMP 的 RelativeAltitude。"""
        return self.relative_altitude


@dataclass
class GcpPoint:
    """控制点 / 检查点。

    ``marks`` 只存**原始像素坐标**：``{影像文件名: (px, py)}``。
    """

    name: str
    east: float
    north: float
    height: float = 0.0
    role: str = "控制点"
    marks: dict[str, tuple[float, float]] = field(default_factory=dict)

    @property
    def is_checkpoint(self) -> bool:
        return self.role == "检查点"


@dataclass
class GcpSet:
    """一组控制点（含检查点）及其刺点结果。"""

    points: list[GcpPoint] = field(default_factory=list)
    source_path: str = ""
    errors: list[str] = field(default_factory=list)

    @property
    def control_points(self) -> list[GcpPoint]:
        return [p for p in self.points if not p.is_checkpoint]

    @property
    def check_points(self) -> list[GcpPoint]:
        return [p for p in self.points if p.is_checkpoint]

    @property
    def marked_point_count(self) -> int:
        return sum(1 for p in self.points if p.marks)

    @property
    def total_marks(self) -> int:
        return sum(len(p.marks) for p in self.points)

    def get(self, name: str) -> GcpPoint | None:
        for point in self.points:
            if point.name == name:
                return point
        return None


@dataclass
class FlightSummary:
    """对一次航飞数据的粗估结果（只用于预检/报告，不参与解算）。"""

    image_count: int = 0
    positioned_count: int = 0
    rtk_fixed_count: int = 0
    bbox: tuple[float, float, float, float] | None = None
    mean_agl: float | None = None
    mean_altitude: float | None = None
    gsd_m: float | None = None
    footprint_width_m: float | None = None
    footprint_height_m: float | None = None
    median_spacing_m: float | None = None
    forward_overlap: float | None = None

    @property
    def gps_coverage(self) -> float:
        if not self.image_count:
            return 0.0
        return self.positioned_count / self.image_count

    @property
    def rtk_fixed_ratio(self) -> float:
        if not self.image_count:
            return 0.0
        return self.rtk_fixed_count / self.image_count


@dataclass(frozen=True)
class PrecheckIssue:
    """一条预检结论。``level`` ∈ error / warn / info / ok。"""

    level: str
    code: str
    title: str
    detail: str = ""
    fix: str = ""

    @property
    def is_error(self) -> bool:
        return self.level == "error"


@dataclass(frozen=True)
class PrecheckOutputs:
    """预检产物路径与结论。"""

    report_path: str
    manifest_path: str
    issues: tuple[PrecheckIssue, ...]
    summary: FlightSummary

    @property
    def error_count(self) -> int:
        return sum(1 for issue in self.issues if issue.level == "error")

    @property
    def warning_count(self) -> int:
        return sum(1 for issue in self.issues if issue.level == "warn")


@dataclass
class UavJob:
    """一次无人机影像拼接任务的全部参数。"""

    image_dir: str = ""
    position_file: str = ""
    gcp_file: str = ""
    output_dir: str = ""
    output_format: str = "tif"
    crs_epsg: int = 4527
    preset: str = "standard"
    alignment_mode: str = "gps"
    dense_cloud: bool = True
    dense_quality: str = "medium"
    build_dsm: bool = True
    build_dtm: bool = False
    dsm_resolution_m: float = 0.0
    gsd_mode: str = "auto"
    gsd_cm: float = 0.0
    blend: bool = True
    seamline: str = "auto"
    resampling: str = "bilinear"
    relative_altitude_m: float | None = None
    ground_elevation_m: float | None = None
    images: list[ImageMeta] = field(default_factory=list)
    summary: FlightSummary | None = None
    gcp: GcpSet | None = None
    show_positions: bool = True

    # ------------------------------------------------------------ 序列化

    def to_manifest(self) -> dict[str, Any]:
        """任务清单（下一轮拼接引擎直接消费）。"""
        return {
            "schema_version": 1,
            "image_dir": self.image_dir,
            "position_file": self.position_file,
            "gcp_file": self.gcp_file,
            "output_dir": self.output_dir,
            "crs_epsg": self.crs_epsg,
            "preset": self.preset,
            "alignment_mode": self.alignment_mode,
            "relative_altitude_m": self.relative_altitude_m,
            "ground_elevation_m": self.ground_elevation_m,
            "dense_cloud": self.dense_cloud,
            "dense_quality": self.dense_quality,
            "build_dsm": self.build_dsm,
            "build_dtm": self.build_dtm,
            "dsm_resolution_m": self.dsm_resolution_m,
            "gsd_mode": self.gsd_mode,
            "gsd_cm": self.gsd_cm,
            "blend": self.blend,
            "seamline": self.seamline,
            "resampling": self.resampling,
            "output_format": self.output_format,
            "images": [_image_to_dict(im) for im in self.images],
            "gcp": _gcp_to_dict(self.gcp),
            "summary": asdict(self.summary) if self.summary is not None else None,
        }


def _image_to_dict(image: ImageMeta) -> dict[str, Any]:
    return {
        "name": image.name,
        "path": image.path,
        "lon": image.lon,
        "lat": image.lat,
        "altitude_ellipsoid": image.altitude_ellipsoid,
        "relative_altitude": image.relative_altitude,
        "gimbal_yaw": image.gimbal_yaw,
        "gimbal_pitch": image.gimbal_pitch,
        "gimbal_roll": image.gimbal_roll,
        "flight_yaw": image.flight_yaw,
        "rtk_flag": image.rtk_flag,
        "rtk_std": list(image.rtk_std) if image.rtk_std is not None else None,
        "source": image.source,
        "intrinsics": asdict(image.intrinsics),
        "exif_error": image.exif_error,
    }


def _gcp_to_dict(gcp: GcpSet | None) -> dict[str, Any] | None:
    if gcp is None:
        return None
    return {
        "source_path": gcp.source_path,
        "points": [
            {
                "name": point.name,
                "east": point.east,
                "north": point.north,
                "height": point.height,
                "role": point.role,
                "marks": {name: list(px) for name, px in point.marks.items()},
            }
            for point in gcp.points
        ],
        "errors": list(gcp.errors),
    }


__all__ = [
    "CameraIntrinsics",
    "FlightSummary",
    "GcpPoint",
    "GcpSet",
    "ImageMeta",
    "PrecheckIssue",
    "PrecheckOutputs",
    "UavJob",
]
