"""坐标系换算工具（WGS84 为主）。

- QGIS 可用（真实 app / 运行时）→ 用 ``QgsCoordinateTransform.transformBoundingBox``
  精确换算；
- QGIS 不可用（headless 纯逻辑/测试）→ 只对 EPSG:4326/4490 做恒等（两坐标精度
  差异对下载裁剪可接受），其它坐标系抛 :class:`PlanError` 由调用方给出中文提示。
"""

from __future__ import annotations

from typing import Optional

from .model import PlanError

_IDENT = {"4326", "4490", ""}


def rect_to_wgs84(
    xmin: float,
    ymin: float,
    xmax: float,
    ymax: float,
    src_auth: str,
) -> tuple[float, float, float, float]:
    """矩形外包换算到 WGS84（EPSG:4326）；源为 4326/4490 → 恒等返回。"""
    if _norm_code(src_auth) in _IDENT:
        return (xmin, ymin, xmax, ymax)
    res = _qgis_transform(xmin, ymin, xmax, ymax, src_auth, "EPSG:4326")
    if res is None:
        raise PlanError(
            f"空间范围坐标系 {src_auth or '未知'} 非 WGS84，且当前无 QGIS 环境可换算"
        )
    return res


def rect_wgs84_to(
    xmin: float,
    ymin: float,
    xmax: float,
    ymax: float,
    dst_auth: str,
) -> Optional[tuple[float, float, float, float]]:
    """WGS84 矩形换算到目标坐标系；目标是 4326/4490/空 → None（无需换算）。"""
    if _norm_code(dst_auth) in _IDENT:
        return None
    return _qgis_transform(xmin, ymin, xmax, ymax, "EPSG:4326", dst_auth)


def _qgis_transform(
    xmin: float,
    ymin: float,
    xmax: float,
    ymax: float,
    src_auth: str,
    dst_auth: str,
) -> Optional[tuple[float, float, float, float]]:
    try:
        from qgis.core import (
            QgsCoordinateReferenceSystem,
            QgsCoordinateTransform,
            QgsRectangle,
        )

        src = QgsCoordinateReferenceSystem(src_auth)
        dst = QgsCoordinateReferenceSystem(dst_auth)
        if not src.isValid() or not dst.isValid():
            return None
        rect = QgsCoordinateTransform(src, dst).transformBoundingBox(
            QgsRectangle(xmin, ymin, xmax, ymax)
        )
        return (rect.xMinimum(), rect.yMinimum(), rect.xMaximum(), rect.yMaximum())
    except Exception:  # noqa: BLE001  QGIS 未加载等
        return None


def _norm_code(auth: str) -> str:
    return str(auth or "").upper().replace("EPSG:", "")
