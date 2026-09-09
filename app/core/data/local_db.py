"""本地 SQLite / GeoPackage 连接探测（纯 stdlib，不依赖 GUI）。

数据管理「新建连接·本地数据库」用：只读打开 sqlite 家族文件（普通 SQLite /
GeoPackage / SpatiaLite），列出**用户数据表**（滤掉 sqlite/gpkg/spatialite
的系统表与元数据表），并探测每张表的 ``count``(行数) 与 ``geo``(面/线/点/影像
短标签，无几何为 None)。

几何探测按文件实际元数据走：GeoPackage（gpkg_contents + gpkg_geometry_columns）
优先，其次 SpatiaLite 的 geometry_columns；都没有则按普通表列出。
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

#: 前缀命中的系统表（SpatiaLite 的 v_/vr_/idx_ 是自动生成的视图/索引表）。
_SYSTEM_PREFIXES = ("sqlite_", "gpkg_", "rtree_", "sql_stat", "idx_", "v_", "vr_")

#: 精确名命中的元数据表。
_SYSTEM_NAMES = {
    "spatial_ref_sys",
    "geometry_columns",
    "geometry_columns_statistics",
    "geometry_columns_field_infos",
    "geom_cols_ref_sys",
    "spatialite_history",
    "spatialite_session",
    "virts",
    "views",
    "sql_statements_log",
    "ElementTable",
}

#: SpatiaLite geometry_columns.geometry_type 数值 -> 短标签
#: (1 POINT, 2 LINESTRING, 3 POLYGON, 4 MULTIPOINT, 5 MULTILINESTRING,
#:  6 MULTIPOLYGON; 7 GEOMETRYCOLLECTION 等不映射)。
_SPATIALITE_GEOM_KIND = {1: "点", 2: "线", 3: "面", 4: "点", 5: "线", 6: "面"}


def _q(name: str) -> str:
    """SQLite 标识符引用（防表名带引号/特殊字符）。"""
    return '"' + name.replace('"', '""') + '"'


def _is_system_table(name: str) -> bool:
    return name in _SYSTEM_NAMES or name.startswith(_SYSTEM_PREFIXES)


def _kind_from_type_name(name: str) -> str | None:
    """GeoPackage geometry_type_name / SpatiaLite 文本类型 -> 短标签。"""
    up = (name or "").upper()
    if "POLYGON" in up:
        return "面"
    if "LINE" in up:
        return "线"
    if "POINT" in up:
        return "点"
    return None


def scan_local_sqlite(path: str | Path) -> list[dict]:
    """只读探测本地 sqlite 家族文件的数据表。

    :return: ``[{"name": 表名, "geo": 短标签|None, "count": 行数}]``（按表名排序）
    :raises ValueError: 文件不存在 / 不是 sqlite / 无法打开（消息面向用户）。
    """
    p = Path(path)
    if not p.is_file():
        raise ValueError(f"文件不存在：{p}")

    try:
        # 普通路径直连 + 只读模式(不用 ``file:`` URI —— Windows 路径含
        # 空格/中文时 URI 解析失败, 会误报"无法打开")
        conn = sqlite3.connect(str(p), timeout=5.0)
        conn.execute("PRAGMA query_only = ON")
    except sqlite3.Error as exc:
        raise ValueError(f"无法打开 SQLite 数据库：{exc}") from exc

    try:
        try:
            # master 含 table+view：geometry_columns 在 SpatiaLite 里可能是 view
            master = {
                row[0]
                for row in conn.execute(
                    "SELECT name FROM sqlite_master WHERE type IN ('table', 'view')"
                )
            }
            tables = [
                row[0]
                for row in conn.execute(
                    "SELECT name FROM sqlite_master WHERE type = 'table' ORDER BY name"
                )
            ]
        except sqlite3.DatabaseError as exc:
            raise ValueError(f"不是有效的 SQLite 数据库文件：{p.name}") from exc

        # ---- 几何类型探测 ----
        kind_of: dict[str, str | None] = {}
        if {"gpkg_contents", "gpkg_geometry_columns"} <= master:
            # GeoPackage：data_type=features 有几何；tiles 视作影像
            try:
                contents = conn.execute(
                    "SELECT table_name, data_type FROM gpkg_contents"
                ).fetchall()
                geom = dict(
                    conn.execute(
                        "SELECT table_name, geometry_type_name FROM gpkg_geometry_columns"
                    ).fetchall()
                )
            except sqlite3.Error:
                contents, geom = [], {}
            for table_name, data_type in contents:
                if data_type == "tiles":
                    kind_of[table_name] = "影像"
                elif data_type == "features" and table_name in geom:
                    kind_of[table_name] = _kind_from_type_name(geom[table_name])
        elif "geometry_columns" in master:
            # SpatiaLite
            try:
                rows = conn.execute(
                    "SELECT f_table_name, geometry_type FROM geometry_columns"
                ).fetchall()
            except sqlite3.Error:
                rows = []
            for table_name, geo_type in rows:
                kind_of[table_name] = (
                    _SPATIALITE_GEOM_KIND.get(geo_type)
                    if isinstance(geo_type, int)
                    else _kind_from_type_name(str(geo_type))
                )

        # ---- 逐表行数 ----
        layers: list[dict] = []
        for name in tables:
            if _is_system_table(name):
                continue
            try:
                count = conn.execute(f"SELECT COUNT(*) FROM {_q(name)}").fetchone()[0]
            except sqlite3.Error:
                count = 0
            layers.append({"name": name, "geo": kind_of.get(name), "count": count})
        return layers
    finally:
        conn.close()


__all__ = ["scan_local_sqlite"]
