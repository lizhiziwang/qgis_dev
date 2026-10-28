"""国内常用坐标系的自动推荐（纯数值，不依赖 QGIS）。

默认按 **CGCS2000 / 3 度带高斯-克吕格** 推荐，带号与 EPSG 的对应关系：

- 3 度带：`EPSG = 4513 + (带号 - 25)`，第 39 带 → EPSG:4527；
- 3 度带（中央经线命名）：`EPSG = 4534 + (中央经线 - 75) / 3`，CM 117°E → EPSG:4548；
- 6 度带：`EPSG = 4491 + (带号 - 13)`。

中国范围外回退 WGS84 / UTM。
"""

from __future__ import annotations

from dataclasses import dataclass

#: 粗略的中国范围（经纬度），只用于决定是否推荐 CGCS2000
CHINA_BBOX = (73.0, 3.0, 136.0, 54.0)


@dataclass(frozen=True)
class CrsSuggestion:
    """推荐坐标系。"""

    epsg: int
    name: str
    method: str
    zone: int | None = None
    central_meridian: float | None = None

    @property
    def authid(self) -> str:
        return f"EPSG:{self.epsg}"


def is_in_china(lon: float, lat: float) -> bool:
    lon_min, lat_min, lon_max, lat_max = CHINA_BBOX
    return lon_min <= lon <= lon_max and lat_min <= lat <= lat_max


def recommend_crs(lon: float, lat: float, method: str = "gk3") -> CrsSuggestion:
    """根据影像中心经纬度推荐投影坐标系。"""
    if is_in_china(lon, lat):
        if method == "gk6":
            zone = int(lon // 6) + 1
            central_meridian = zone * 6 - 3
            if 13 <= zone <= 23:
                return CrsSuggestion(
                    epsg=4491 + zone - 13,
                    name=f"CGCS2000 / 6度带 第{zone}带（CM {central_meridian:g}°E）",
                    method="gk6",
                    zone=zone,
                    central_meridian=central_meridian,
                )
        else:
            zone = int((lon + 1.5) // 3)
            central_meridian = zone * 3
            if 25 <= zone <= 45:
                return CrsSuggestion(
                    epsg=4513 + zone - 25,
                    name=f"CGCS2000 / 3度带 第{zone}带（CM {central_meridian:g}°E）",
                    method="gk3",
                    zone=zone,
                    central_meridian=central_meridian,
                )

    utm_zone = int((lon + 180) // 6) + 1
    hemisphere = "N" if lat >= 0 else "S"
    epsg = (32600 if lat >= 0 else 32700) + utm_zone
    return CrsSuggestion(
        epsg=epsg,
        name=f"WGS 84 / UTM {utm_zone}{hemisphere}",
        method="utm",
        zone=utm_zone,
        central_meridian=utm_zone * 6 - 183,
    )


def central_meridian_of(epsg: int) -> float | None:
    """反查 CGCS2000 高斯-克吕格 EPSG 的中央经线；非该范围返回 ``None``。"""
    if 4513 <= epsg <= 4533:  # 3 度带（带号）
        return float((epsg - 4513 + 25) * 3)
    if 4534 <= epsg <= 4554:  # 3 度带（中央经线）
        return float(75 + (epsg - 4534) * 3)
    if 4491 <= epsg <= 4501:  # 6 度带
        zone = epsg - 4491 + 13
        return float(zone * 6 - 3)
    return None


def is_geographic_epsg(epsg: int) -> bool:
    """常见地理坐标系。（4490 = CGCS2000，4326 = WGS84）"""
    return epsg in (4490, 4326, 4269)


__all__ = [
    "CHINA_BBOX",
    "CrsSuggestion",
    "central_meridian_of",
    "is_geographic_epsg",
    "is_in_china",
    "recommend_crs",
]
