"""航拍影像元数据读取：EXIF（exifread）+ DJI XMP。

背景（实测）：这批 DJI Zenmuse L1 影像用 GDAL 的 ``GetMetadata("EXIF")``
读不到任何字段，而 QGIS 环境自带的 ``exifread`` 能正常读到 GPS/焦距；
DJI 的相对航高、云台姿态、标定焦距、RTK 质量则在 **XMP 属性**里。

``exifread`` 延迟导入：没有它时仍能读 XMP，只是 EXIF 字段为空。
"""

from __future__ import annotations

import os
import re
from pathlib import Path

from app.core.uav.intrinsics import sensor_width_mm
from app.core.uav.models import CameraIntrinsics, ImageMeta
from app.core.uav.position import PositionRecord

#: 支持的影像扩展名
SUPPORTED_EXTS = (".jpg", ".jpeg", ".png", ".tif", ".tiff")

#: XMP 通常紧跟在 APP1 段，读前 512KB 足够
_XMP_SCAN_BYTES = 512 * 1024

_XMP_ATTR_RE = re.compile(r'drone-dji:([A-Za-z][A-Za-z0-9_]*)\s*=\s*"([^"]*)"')

_NUMBER_RE = re.compile(r"[-+]?\d+(?:\.\d+)?(?:/\d+(?:\.\d+)?)?")


def list_images(folder: str) -> list[str]:
    """列出目录内支持的影像（大小写不敏感去重、按文件名排序）。"""
    try:
        entries = os.scandir(folder)
    except OSError:
        return []
    found: dict[str, str] = {}
    try:
        for entry in entries:
            if not entry.is_file():
                continue
            name = entry.name
            if name.startswith("."):
                continue
            if os.path.splitext(name)[1].lower() not in SUPPORTED_EXTS:
                continue
            # Windows 文件系统大小写不敏感：*.JPG / *.jpg 会重复匹配，
            # 这里按 normcase 去重，避免 68 张影像被数成 136 张。
            found.setdefault(os.path.normcase(entry.path), entry.path)
    finally:
        entries.close()
    return sorted(found.values(), key=lambda item: os.path.basename(item).lower())


def _to_float(value: object) -> float | None:
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return float(value)
    text = str(value).strip()
    if not text:
        return None
    try:
        if "/" in text:
            numerator, denominator = text.split("/", 1)
            return float(numerator) / float(denominator)
        return float(text)
    except (TypeError, ValueError, ZeroDivisionError):
        return None


def parse_dms(value: object, ref: str = "") -> float | None:
    """把 EXIF 的度分秒（list / 字符串 / Ratio）转成十进制度。"""
    if value is None:
        return None
    if isinstance(value, (int, float)):
        numbers = [float(value)]
    elif isinstance(value, str):
        numbers = [_to_float(token) for token in _NUMBER_RE.findall(value)]
        numbers = [number for number in numbers if number is not None]
    else:
        try:
            numbers = [_to_float(item) for item in value]  # type: ignore[union-attr]
        except TypeError:
            numbers = [_to_float(value)]
        numbers = [number for number in numbers if number is not None]
    if not numbers:
        return None
    degrees = numbers[0]
    if len(numbers) > 1:
        degrees += numbers[1] / 60.0
    if len(numbers) > 2:
        degrees += numbers[2] / 3600.0
    if str(ref).strip().upper() in ("S", "W"):
        degrees = -abs(degrees)
    return degrees


def parse_dji_xmp(text: str) -> dict[str, str]:
    """从 XMP 文本中取 ``drone-dji:*`` 属性。"""
    return {match.group(1): match.group(2) for match in _XMP_ATTR_RE.finditer(text)}


def read_dji_xmp(path: str) -> dict[str, str]:
    try:
        with open(path, "rb") as handle:
            raw = handle.read(_XMP_SCAN_BYTES)
    except OSError:
        return {}
    return parse_dji_xmp(raw.decode("utf-8", "ignore"))


def _read_exif(path: str) -> tuple[dict[str, object], str]:
    try:
        import exifread  # 延迟导入：无 exifread 时只丢 EXIF，不影响 XMP
    except ImportError:
        return {}, "缺少 exifread 依赖，无法读取 EXIF"
    try:
        with open(path, "rb") as handle:
            tags = exifread.process_file(handle, details=False)
    except Exception as exc:  # noqa: BLE001 —— 第三方库对坏文件会抛各种异常
        return {}, f"EXIF 读取失败：{exc}"
    return dict(tags), ""


def _tag_values(tags: dict[str, object], key: str) -> object | None:
    tag = tags.get(key)
    return None if tag is None else getattr(tag, "values", None)


def _tag_text(tags: dict[str, object], key: str) -> str:
    tag = tags.get(key)
    return "" if tag is None else str(tag).strip()


def _tag_number(tags: dict[str, object], key: str) -> float | None:
    """EXIF 数值标签通常把值放在 ``tag.values[0]``（Ratio / int）。"""
    values = _tag_values(tags, key)
    if isinstance(values, list):
        return _to_float(values[0]) if values else None
    return _to_float(values)


def _tag_int(tags: dict[str, object], *keys: str) -> int:
    for key in keys:
        number = _tag_number(tags, key)
        if number is not None:
            return int(number)
    return 0


def _exif_altitude(tags: dict[str, object]) -> float | None:
    values = _tag_values(tags, "GPS GPSAltitude")
    if not isinstance(values, list) or not values:
        return None
    altitude = _to_float(values[0])
    if altitude is None:
        return None
    ref_values = _tag_values(tags, "GPS GPSAltitudeRef")
    ref = _to_float(ref_values[0]) if isinstance(ref_values, list) and ref_values else 0.0
    return -altitude if ref == 1 else altitude


def _xmp_float(xmp: dict[str, str], *keys: str) -> float | None:
    for key in keys:
        if key in xmp:
            value = _to_float(xmp[key])
            if value is not None:
                return value
    return None


def _xmp_int(xmp: dict[str, str], *keys: str) -> int | None:
    value = _xmp_float(xmp, *keys)
    return int(value) if value is not None else None


def _parse_dewarp(text: str) -> tuple[float, ...]:
    """``DewarpData``：``日期;fx,fy,cx,cy,k1,k2,p1,p2,k3``。"""
    if not text:
        return ()
    payload = text.split(";")[-1]
    numbers = []
    for token in payload.split(","):
        number = _to_float(token)
        if number is not None:
            numbers.append(number)
    return tuple(numbers)


def read_image_meta(
    path: str,
    position: PositionRecord | None = None,
    position_source: str = "POS",
) -> ImageMeta:
    """读取单张影像的 EXIF / XMP / 外部 POS 元数据。"""
    tags, exif_error = _read_exif(path)
    xmp = read_dji_xmp(path)

    lon = _xmp_float(xmp, "GpsLongitude", "GpsLongtitude")
    lat = _xmp_float(xmp, "GpsLatitude")
    source_parts: list[str] = []
    if lon is not None and lat is not None:
        source_parts.append("DJI XMP")
    else:
        lon = parse_dms(_tag_values(tags, "GPS GPSLongitude"), _tag_text(tags, "GPS GPSLongitudeRef"))
        lat = parse_dms(_tag_values(tags, "GPS GPSLatitude"), _tag_text(tags, "GPS GPSLatitudeRef"))
        if lon is not None and lat is not None:
            source_parts.append("EXIF")
        elif position is not None and position.has_lonlat:
            lon, lat = position.lon, position.lat
            source_parts.append(position_source)

    altitude = _xmp_float(xmp, "AbsoluteAltitude")
    if altitude is None:
        altitude = _exif_altitude(tags)
    if altitude is None and position is not None:
        altitude = position.altitude

    relative_altitude = _xmp_float(xmp, "RelativeAltitude")
    if relative_altitude is None and position is not None:
        relative_altitude = position.relative_altitude

    width_px = _tag_int(tags, "EXIF ExifImageWidth", "Image ImageWidth")
    height_px = _tag_int(tags, "EXIF ExifImageLength", "Image ImageLength")

    focal_mm = _tag_number(tags, "EXIF FocalLength")
    focal_35mm = _tag_number(tags, "EXIF FocalLengthIn35mmFilm")
    focal_px = _xmp_float(xmp, "CalibratedFocalLength")
    model = _tag_text(tags, "Image Model")
    make = _tag_text(tags, "Image Make")
    sensor_width = sensor_width_mm(
        focal_mm, focal_px, width_px, focal_35mm=focal_35mm, model=model
    )

    dewarp = _parse_dewarp(xmp.get("DewarpData", ""))
    principal_x = _xmp_float(xmp, "CalibratedOpticalCenterX")
    principal_y = _xmp_float(xmp, "CalibratedOpticalCenterY")

    rtk_std = None
    std_lon = _xmp_float(xmp, "RtkStdLon")
    std_lat = _xmp_float(xmp, "RtkStdLat")
    std_hgt = _xmp_float(xmp, "RtkStdHgt")
    if None not in (std_lon, std_lat, std_hgt):
        rtk_std = (float(std_lon), float(std_lat), float(std_hgt))

    yaw = _xmp_float(xmp, "GimbalYawDegree")
    if yaw is None and position is not None:
        yaw = position.yaw

    return ImageMeta(
        path=path,
        name=Path(path).name,
        lon=lon,
        lat=lat,
        altitude_ellipsoid=altitude,
        relative_altitude=relative_altitude,
        gimbal_yaw=yaw,
        gimbal_pitch=_xmp_float(xmp, "GimbalPitchDegree"),
        gimbal_roll=_xmp_float(xmp, "GimbalRollDegree"),
        flight_yaw=_xmp_float(xmp, "FlightYawDegree"),
        rtk_flag=_xmp_int(xmp, "RtkFlag"),
        rtk_std=rtk_std,
        source="+".join(source_parts),
        intrinsics=CameraIntrinsics(
            make=make,
            model=model,
            width_px=width_px,
            height_px=height_px,
            focal_mm=focal_mm,
            focal_px=focal_px,
            sensor_width_mm=sensor_width,
            principal_x=principal_x,
            principal_y=principal_y,
            dewarp=dewarp,
        ),
        exif_error=exif_error,
    )


__all__ = [
    "SUPPORTED_EXTS",
    "list_images",
    "parse_dji_xmp",
    "parse_dms",
    "read_dji_xmp",
    "read_image_meta",
]
