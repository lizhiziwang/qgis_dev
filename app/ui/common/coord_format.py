"""坐标的显示精度与格式化。

约定（与 ``app/ui/main_window/coord_locate_overlay.py`` 里那两个常量同源）：

- **地理坐标系**（经纬度）：6 位小数 ≈ 0.11 m；
- **投影坐标系**（米）：3 位小数 ≈ 1 mm 级显示，够用且不刷屏。

单独拆出来是因为不止一处要显示坐标（输入框的小数位、节点列表的 X/Y 列……），
以前这段判断是内联在 ``coord_locate_overlay`` 里的，新代码别再造一份。
"""

from __future__ import annotations

#: 地理坐标系坐标的小数位（≈0.11 m）
DECIMALS_GEO = 6
#: 投影坐标系坐标的小数位
DECIMALS_PROJ = 3
#: 判断不出 CRS 时的兜底小数位
DECIMALS_DEFAULT = 3

__all__ = ["DECIMALS_GEO", "DECIMALS_PROJ", "coord_decimals", "format_coord"]


def coord_decimals(crs) -> int:
    """该 CRS 下坐标应显示的小数位；``crs`` 为空 / 无效时用投影档。"""
    try:
        if crs is not None and crs.isValid() and crs.isGeographic():
            return DECIMALS_GEO
    except (RuntimeError, AttributeError):
        pass
    return DECIMALS_PROJ


def format_coord(value, crs) -> str:
    """按 :func:`coord_decimals` 的精度格式化一个坐标分量。"""
    try:
        return f"{float(value):.{coord_decimals(crs)}f}"
    except (TypeError, ValueError):
        return "" if value is None else str(value)
