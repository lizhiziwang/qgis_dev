"""相机内参与地面分辨率（GSD）估算。

公式：

    pixel_size(mm) = sensor_width(mm) / image_width(px)
    GSD(m/px)      = AGL(m) * pixel_size(mm) / focal(mm)
                   = AGL(m) * sensor_width(mm) / (focal(mm) * image_width(px))
"""

from __future__ import annotations

#: 常见航测相机的传感器宽度（mm）；读不到 35mm 等效时兜底
SENSOR_WIDTH_BY_MODEL: dict[str, float] = {
    "EP800": 13.2,  # DJI Zenmuse L1 的 RGB 相机
    "L1": 13.2,
    "L2": 17.3,
    "P4": 13.2,
    "MAVIC": 13.2,
    "PHANTOM": 13.2,
}
DEFAULT_SENSOR_WIDTH_MM = 13.2


def sensor_width_mm(
    focal_mm: float | None,
    focal_px: float | None,
    width_px: int | None,
    focal_35mm: float | None = None,
    model: str = "",
) -> float | None:
    """推算传感器宽度：像素焦距 → 35mm 等效 → 机型表 → 默认值。"""
    if focal_mm and focal_px and width_px:
        return width_px * focal_mm / focal_px
    if focal_mm and focal_35mm:
        return 36.0 * focal_mm / focal_35mm
    upper = (model or "").upper()
    for key, width in SENSOR_WIDTH_BY_MODEL.items():
        if key in upper:
            return width
    return DEFAULT_SENSOR_WIDTH_MM


def pixel_size_mm(sensor_width: float | None, width_px: int | None) -> float | None:
    if not sensor_width or not width_px:
        return None
    return sensor_width / width_px


def estimate_gsd(
    agl_m: float | None,
    focal_mm: float | None,
    sensor_width: float | None,
    width_px: int | None,
) -> float | None:
    """地面分辨率（米/像素）。"""
    if not agl_m or not focal_mm or not sensor_width or not width_px:
        return None
    return agl_m * sensor_width / (focal_mm * width_px)


def estimate_footprint(
    agl_m: float | None,
    focal_mm: float | None,
    sensor_width: float | None,
    width_px: int | None,
    height_px: int | None,
) -> tuple[float, float] | None:
    """单张影像的地面覆盖宽高（m）。"""
    gsd = estimate_gsd(agl_m, focal_mm, sensor_width, width_px)
    if gsd is None or not height_px:
        return None
    return gsd * width_px, gsd * height_px


__all__ = [
    "DEFAULT_SENSOR_WIDTH_MM",
    "SENSOR_WIDTH_BY_MODEL",
    "estimate_footprint",
    "estimate_gsd",
    "pixel_size_mm",
    "sensor_width_mm",
]
