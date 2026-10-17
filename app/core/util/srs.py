"""QGIS 自带坐标系库（``srs.db``）的查询封装：EPSG 候选 + 按基准 / 分带取投影。

「创建数据」那边有两处要用它：

- :class:`~app.ui.dialogs.create_data_dialog.CrsComboBox` —— 7200 条候选的搜索下拉；
- :class:`~app.ui.dialogs.crs_pick_dialog.CrsPickDialog` —— 原型那个「坐标系选择」弹窗
  （图层 / 北京54 / 西安80 / WGS84 / CGCS2000 / 自定义），其中四个基准页签要按
  「3度分带 / 6度分带 × 带号 / 不带号」列出对应的投影分带。

**分带列表是从 srs.db 现查出来的，不是写死一张表**：EPSG 编码、各基准的中文/英文命名、
分带号与中央经线的对应关系都随 GDAL / PROJ 版本变，硬编码的表早晚会对不上（写错了用户
拿到的是另一个投影，比不给列表还糟）。查不出来就退回空列表，界面照原样降级。
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from pathlib import Path

from app.infra.logging import get_logger

logger = get_logger(__name__)

#: 下拉项里 authid 与名称的分隔符（``CrsComboBox.authid`` 按它切回 authid）
EPSG_SEP = " — "

#: srs.db 读不到时的兜底候选（全球通用 + 国内常用，描述与 srs.db 保持一致）
FALLBACK_EPSG: tuple[tuple[str, str], ...] = (
    ("EPSG:4490", "China Geodetic Coordinate System 2000"),
    ("EPSG:4326", "WGS 84"),
    ("EPSG:3857", "WGS 84 / Pseudo-Mercator"),
    ("EPSG:4610", "Xian 1980"),
    ("EPSG:4214", "Beijing 1954"),
    ("EPSG:4491", "CGCS2000 / Gauss-Kruger zone 13"),
    ("EPSG:4508", "CGCS2000 / Gauss-Kruger CM 111E"),
    ("EPSG:4534", "CGCS2000 / 3-degree Gauss-Kruger CM 75E"),
    ("EPSG:4547", "CGCS2000 / 3-degree Gauss-Kruger CM 114E"),
    ("EPSG:32650", "WGS 84 / UTM zone 50N"),
)

#: 候选缓存：srs.db 七千条读一次就够（对话框会被反复打开）
_EPSG_CACHE: list[tuple[str, str]] | None = None


def epsg_entries() -> list[tuple[str, str]]:
    """坐标系候选表（带进程级缓存）：优先 ``srs.db``，失败退回 :data:`FALLBACK_EPSG`。"""
    global _EPSG_CACHE

    if _EPSG_CACHE is None:
        _EPSG_CACHE = read_srs_db() or list(FALLBACK_EPSG)
    return _EPSG_CACHE


def read_srs_db() -> list[tuple[str, str]]:
    """读 QGIS 自带 ``srs.db`` 的 ``tbl_srs``，返回 ``[(authid, 描述)]``。

    只取 ``auth_name='EPSG'`` 且未废弃的行：其它授权名（ESRI / IGNF / IAU_2015）
    对「EPSG 代码搜索」没有帮助，反而把候选撑到 1.4 万条。

    读不到（文件缺失 / 只读安装目录 / 表结构不符）时返回空表，由调用方走
    :data:`FALLBACK_EPSG` —— 打开对话框这件事不该被坐标系库绊住。
    """
    from qgis.core import QgsApplication

    db_path = Path(QgsApplication.srsDatabaseFilePath())
    if not db_path.is_file():
        logger.warning("未找到 QGIS 坐标系库 %s，坐标系候选使用内置表", db_path)
        return []
    try:
        # mode=ro：QGIS 安装目录可能只读，纯读也不需要写权限 / 不留 -journal
        con = sqlite3.connect(f"{db_path.as_uri()}?mode=ro", uri=True)
        try:
            rows = con.execute(
                "SELECT auth_id, description FROM tbl_srs "
                "WHERE auth_name = 'EPSG' AND deprecated = 0 "
                "ORDER BY CAST(auth_id AS INTEGER)"
            ).fetchall()
        finally:
            con.close()
    except (sqlite3.Error, OSError, ValueError) as exc:
        logger.warning("读取坐标系库 %s 失败，坐标系候选使用内置表：%s", db_path, exc)
        return []
    return [(f"EPSG:{auth_id}", description or "") for auth_id, description in rows if auth_id]


# --------------------------------------------------------------------- 分带投影


@dataclass(frozen=True)
class Datum:
    """一个基准（弹窗里的一个页签）。"""

    #: 页签标题 / 区间表的键
    key: str
    #: proj 串里的判据（各基准的参考椭球不同，用它核对查出来的代码是不是这个基准的）
    ellps: tuple[str, ...]
    #: 这个基准的**地理坐标系**（经纬度）EPSG 代码
    geographic: int
    #: 是不是 UTM 分带（WGS 84 用 UTM，其余用高斯-克吕格分带）
    utm: bool = False


@dataclass(frozen=True)
class ZoneMode:
    """一种分带模式（弹窗左侧的单选项）。"""

    #: 模式键（区间表的键）
    key: str
    #: 界面标签
    label: str
    #: 每带跨多少度（3 度带 / 6 度带；UTM 也是 6 度）
    width: int
    #: 描述里带的是「带号」还是「中央经线」。**别拿 :attr:`label` 里有没有「带号」判断**：
    #: 「不加带号」也含这两个字，会把两种模式认成同一种（实测踩过）
    numbered: bool = True


@dataclass(frozen=True)
class ZoneEntry:
    """一条投影分带（弹窗右侧列表里的一项）。"""

    #: ``EPSG:4534``
    authid: str
    #: 显示文案（如 ``3度分带（带号34）：中央经线102°E``）
    label: str


#: 弹窗里的四个基准页签（顺序照原型：北京54 / 西安80 / WGS84 / CGCS2000）。
#: ``geographic`` 是各基准的经纬度坐标系代码（EPSG 注册表里的固定事实，用得着时还会
#: 过一遍 proj 串核对，见 :func:`geographic_entry`）。
DATUMS: tuple[Datum, ...] = (
    Datum("北京54", ("ellps=krass",), 4214),
    Datum("西安80", ("ellps=IAU76",), 4610),
    Datum("WGS84", ("ellps=WGS84", "datum=WGS84"), 4326, utm=True),
    Datum("CGCS2000", ("ellps=GRS80",), 4490),
)

#: 高斯-克吕格分带的四种模式（顺序照原型）
GAUSS_MODES: tuple[ZoneMode, ...] = (
    ZoneMode("3带号", "3度分带加带号", 3, numbered=True),
    ZoneMode("3无带号", "3度分带不加带号", 3, numbered=False),
    ZoneMode("6带号", "6度分带加带号", 6, numbered=True),
    ZoneMode("6无带号", "6度分带不加带号", 6, numbered=False),
)

#: UTM 的两种模式（WGS 84 用）
UTM_MODES: tuple[ZoneMode, ...] = (
    ZoneMode("utm北", "北半球 UTM", 6),
    ZoneMode("utm南", "南半球 UTM", 6),
)

#: ``(基准, 模式)`` → EPSG 编码的闭区间。**用编码区间而不是描述文字**：描述随
#: QGIS / GDAL 的语言与版本变（中文版 QGIS 的 srs.db 里全是「国家2000坐标系 经纬度」
#: 这类译名，按英文关键词去匹是一条都匹不上的，实测就是这么翻车的）。
#: 区间是 EPSG 注册表里的稳定事实，而且每个代码还会再过一遍 proj 串核对
#: （见 :func:`zone_entries`），区间写错了也只会少几项、不会给出错的坐标系。
_ZONE_RANGES: dict[tuple[str, str], tuple[int, int]] = {
    ("北京54", "3带号"): (2401, 2421),
    ("北京54", "3无带号"): (2422, 2442),
    ("北京54", "6带号"): (21413, 21423),
    ("北京54", "6无带号"): (21453, 21463),
    ("西安80", "3带号"): (2349, 2369),
    ("西安80", "3无带号"): (2370, 2390),
    ("西安80", "6带号"): (2327, 2337),
    ("西安80", "6无带号"): (2338, 2348),
    ("CGCS2000", "3带号"): (4513, 4533),
    ("CGCS2000", "3无带号"): (4534, 4554),
    ("CGCS2000", "6带号"): (4491, 4501),
    ("CGCS2000", "6无带号"): (4502, 4512),
    ("WGS84", "utm北"): (32601, 32660),
    ("WGS84", "utm南"): (32701, 32760),
}

#: 分带列表缓存（``(基准, 模式)`` → 列表）：查一次要建上百个 CRS 对象读 proj 串
_ZONE_CACHE: dict[tuple[str, str], list[ZoneEntry]] = {}


def modes_for(datum: Datum) -> tuple[ZoneMode, ...]:
    """这个基准可用的分带模式（WGS 84 是 UTM，其余是高斯分带）。"""
    return UTM_MODES if datum.utm else GAUSS_MODES


def geographic_entry(datum: Datum) -> ZoneEntry | None:
    """这个基准的**经纬度（地理坐标系）**条目（列表里的第一项）。

    用户在分带列表里找不到「就是经纬度、不分带」的那种坐标系，所以每个基准页签都把
    它摆在最前面。代码同样要过一遍 proj 串核对（``+proj=longlat`` + 该基准的椭球），
    对不上就不给 —— 宁可少一项，也不能把「北京54 的经纬度」给成 CGCS2000 的。
    """
    from qgis.core import QgsCoordinateReferenceSystem

    authid = f"EPSG:{datum.geographic}"
    crs = QgsCoordinateReferenceSystem(authid)
    if not crs.isValid():
        return None
    proj = crs.toProj() or ""
    if "+proj=longlat" not in proj:
        return None
    if not any(token in proj for token in datum.ellps):
        return None
    return ZoneEntry(authid, f"经纬度（{datum.key} 地理坐标系）")


def zone_entries(datum: Datum, mode: ZoneMode) -> list[ZoneEntry]:
    """某个基准 + 某个分带模式下的投影分带列表（第一项永远是「经纬度」）。"""
    cache_key = (datum.key, mode.key)
    if cache_key in _ZONE_CACHE:
        return _ZONE_CACHE[cache_key]

    span = _ZONE_RANGES.get(cache_key)
    entries: list[ZoneEntry] = []
    if span is not None:
        from qgis.core import QgsCoordinateReferenceSystem

        for code in range(span[0], span[1] + 1):
            authid = f"EPSG:{code}"
            crs = QgsCoordinateReferenceSystem(authid)
            entry = _make_entry(crs, authid, datum, mode)
            if entry is not None:
                entries.append(entry)
        entries.sort(key=lambda item: _central_meridian(item.label))
    if not entries:
        logger.warning("坐标系选择：%s / %s 的分带一个都没查出来", datum.key, mode.label)
    # 「经纬度」不分带，放在最前面当首选（用户要的往往就是它）
    geographic = geographic_entry(datum)
    result = ([geographic] if geographic else []) + entries
    _ZONE_CACHE[cache_key] = result
    return result


def _make_entry(crs, authid: str, datum: Datum, mode: ZoneMode) -> ZoneEntry | None:
    """核对一个 EPSG 代码是不是「这个基准的这个分带」；是就给出列表项，不是返回 ``None``。"""
    if not crs.isValid():
        return None
    proj = crs.toProj() or ""
    if not any(token in proj for token in datum.ellps):
        return None
    expected_proj = "utm" if datum.utm else "tmerc"
    if f"+proj={expected_proj}" not in proj:
        return None

    if datum.utm:
        # UTM 的 proj 串给的是 ``+zone=``（没有 ``+lon_0``），中央经线自己算
        zone_value = _proj_number(proj, "zone")
        if zone_value is None:
            return None
        zone = int(zone_value)
        if zone < 1 or zone > 60:
            return None
        south = "+south" in proj
        if south != (mode.key == "utm南"):
            return None
        # UTM 中央经线 = 带号 × 6 − 183（1 带 → 177°W，31 带 → 3°E）
        return ZoneEntry(
            authid, f"带号{zone}{'S' if south else 'N'}：中央经线{_longitude(zone * 6 - 183)}"
        )

    lon_0 = _proj_number(proj, "lon_0")
    if lon_0 is None:
        return None
    x_0 = _proj_number(proj, "x_0")
    if mode.numbered:
        # 加带号的假东偏是「带号 × 1000000 + 500000」，减掉 500000 就是带号
        if x_0 is None or x_0 == _FALSE_EASTING:
            return None
        zone = int((x_0 - _FALSE_EASTING) / 1_000_000 + 0.5)
        if zone <= 0:
            return None
        return ZoneEntry(authid, f"{mode.label}（带号{zone}）：中央经线{_longitude(lon_0)}")
    if x_0 is not None and x_0 != _FALSE_EASTING:
        # 不带带的假东偏固定 500000；对不上说明拿到的其实是「加带号」那个
        return None
    return ZoneEntry(authid, f"{mode.label}：中央经线{_longitude(lon_0)}")


#: 不带带号的假东偏（500km）
_FALSE_EASTING = 500000.0


def _proj_number(proj: str, key: str) -> float | None:
    """从 proj 串里取 ``+key=数值``（取不到返回 ``None``）。"""
    marker = f"+{key}="
    start = proj.find(marker)
    if start < 0:
        return None
    rest = proj[start + len(marker) :]
    token = ""
    for char in rest:
        if char.isdigit() or char in ".+-":
            token += char
        else:
            break
    try:
        return float(token)
    except ValueError:
        return None


def _central_meridian(label: str) -> float:
    """从显示文案里把中央经线摘出来排序用（``…中央经线102°E`` → 102；西经为负）。"""
    marker = "中央经线"
    start = label.find(marker)
    if start < 0:
        return 0.0
    value = _proj_number(label[start:].replace(marker, "+x="), "x")
    if value is None:
        return 0.0
    return -value if "°W" in label[start:] else value


def _longitude(degrees: float) -> str:
    """中央经线度数 → ``102°E`` / ``177°W``（西经报成负数不好读）。"""
    whole = int(round(degrees))
    return f"{whole}°E" if whole >= 0 else f"{abs(whole)}°W"


def describe_crs(authid: str) -> str:
    """authid → 描述（``EPSG:4490`` → ``China Geodetic Coordinate System 2000``）。"""
    if not authid:
        return ""
    for code, description in epsg_entries():
        if code == authid:
            return description
    return ""


__all__ = [
    "DATUMS",
    "EPSG_SEP",
    "FALLBACK_EPSG",
    "GAUSS_MODES",
    "UTM_MODES",
    "Datum",
    "ZoneEntry",
    "ZoneMode",
    "describe_crs",
    "geographic_entry",
    "epsg_entries",
    "modes_for",
    "read_srs_db",
    "zone_entries",
]
