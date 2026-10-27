"""把库里「已存在但未登记」的矢量表纳入数据管理。

数据管理的目录只读 ``user_relation`` 注册表（见
:func:`app.core.util.local_db_util.describe_local_db`），而 OGR / 其它 GIS 工具
写出的 GPKG、SQLite 通常没有这张表 —— 于是库里明明有图层，面板里却什么都看不到。

本模块负责：

- :func:`find_unregistered_spatial_layers`：只读元数据，找出有几何、但
  ``user_relation`` 里没有登记的表（GPKG 查 ``gpkg_contents`` +
  ``gpkg_geometry_columns``；SpatiaLite / OGR SQLite 查 ``geometry_columns``）；
- :func:`register_spatial_layers`：把选中的表按 ``type=spatial`` 写进
  ``user_relation``（幂等），只改目录表，不动业务数据。

纯属性表 / GPKG 栅格（``attributes`` / ``tiles``）不在发现范围内：它们不是
「矢量图层」，影像登记还需要 bounds / zoom 等额外元数据，留给单独的影像流程。
"""

from __future__ import annotations

import contextlib
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

try:  # 与 local_db_util 保持一致：QGIS 内置的是 pysqlite3
    import pysqlite3 as sqlite3
except ImportError:  # pragma: no cover - 极端裁剪环境
    import sqlite3

from app.core.util.local_db_util import _conn_is_gpkg, _table_meta, local_db_util
from app.infra.logging import get_logger

if TYPE_CHECKING:
    from collections.abc import Sequence

logger = get_logger(__name__)


class LocalRegisterError(Exception):
    """登记未登记图层失败（打开库 / 写目录表出错）。"""


@dataclass(frozen=True)
class UnregisteredLayer:
    """库里存在、但还没登记到数据管理的一张矢量表。"""

    #: 库内真实表名（后续登记、打开图层都用它）
    name: str
    #: 几何类型中文名（点 / 线 / 面 / …；读不出时回落「几何」）
    geometry: str
    #: EPSG 代码；读不出为 None
    srid: int | None
    #: 坐标系展示文案（如 ``EPSG:4490 · CGCS2000``）；读不出为空串
    crs: str


def _own_tables() -> frozenset[str]:
    """本工程记账表 / GPKG 系统表：列图层时不展示。"""
    from app.core.util.sublayers import _OWN_TABLES

    return _OWN_TABLES


def _registered_names(conn) -> set[str]:
    """``user_relation`` 里已登记的表名（不区分大小写）；没有注册表视为空。"""
    try:
        return {
            str(row[0]).casefold()
            for row in conn.execute("SELECT table_name FROM user_relation")
            if row and row[0]
        }
    except sqlite3.Error:
        return set()


def _spatial_table_names(conn, gpkg: bool) -> list[str]:
    """库里有几何的表名（GPKG 走 ``gpkg_contents``，其余走 ``geometry_columns``）。"""
    query = (
        "SELECT c.table_name FROM gpkg_contents c "
        "JOIN gpkg_geometry_columns g ON g.table_name = c.table_name "
        "WHERE c.data_type = 'features'"
        if gpkg
        else "SELECT f_table_name FROM geometry_columns"
    )
    try:
        return [str(row[0]) for row in conn.execute(query) if row and row[0]]
    except sqlite3.Error:
        return []


def find_unregistered_spatial_layers(db_path: str | Path) -> list[UnregisteredLayer]:
    """找出库里未登记到数据管理的矢量表；读不出来返回空表（只记日志）。

    只查元数据表，不 ``COUNT(*)``、不解析几何，大库同样是毫秒级。
    """
    path = Path(db_path)
    if not path.is_file():
        return []
    try:
        conn = sqlite3.connect(str(path), timeout=5.0)
    except sqlite3.Error:
        logger.warning("扫描未登记图层：打开库失败 %s", path, exc_info=True)
        return []
    try:
        conn.execute("PRAGMA query_only = ON")
        names = _spatial_table_names(conn, _conn_is_gpkg(conn))
        if not names:
            return []
        registered = _registered_names(conn)
        own = {name.casefold() for name in _own_tables()}
        candidates = [
            name
            for name in names
            if name.casefold() not in registered and name.casefold() not in own
        ]
        if not candidates:
            return []
        meta = _table_meta(conn, candidates)
        return [
            UnregisteredLayer(
                name=name,
                geometry=str((meta.get(name) or {}).get("geom_label") or "几何"),
                srid=(meta.get(name) or {}).get("srid"),
                crs=str((meta.get(name) or {}).get("crs") or ""),
            )
            for name in sorted(candidates)
        ]
    except sqlite3.Error:
        logger.warning("扫描未登记图层失败：%s", path, exc_info=True)
        return []
    finally:
        conn.close()


def register_spatial_layers(db_path: str | Path, names: Sequence[str]) -> int:
    """把 ``names`` 里的矢量表登记为 ``user_relation(type=spatial)``，返回登记数。

    - 只登记库里**确实有几何**的表：名字过时 / 传错的直接跳过；
    - 幂等：先删同名旧注册行再写，重复调用不会产生重复目录项；
    - 只写目录表，不改动图层数据。

    Raises:
        LocalRegisterError: 打开库或写目录表失败。
    """
    wanted = {str(name).strip().casefold() for name in names if str(name).strip()}
    if not wanted:
        return 0
    try:
        db = local_db_util(str(db_path))
    except Exception as exc:  # noqa: BLE001 —— sqlite / 扩展加载都可能抛
        raise LocalRegisterError(f"打开数据库失败：{exc}") from exc
    registered = 0
    try:
        spatial = {
            name.casefold(): name
            for name in _spatial_table_names(db.conn, _conn_is_gpkg(db.conn))
        }
        for key in wanted:
            actual = spatial.get(key)
            if actual is None:
                logger.warning("跳过登记：%s 不是库里的矢量表", key)
                continue
            # 老库 / 刚由 OGR 建出的库可能还没有 user_relation：DELETE 失败
            # 没关系，紧接着的 add_domain_table 会幂等建表再登记。
            with contextlib.suppress(Exception):
                db.cursor.execute(
                    "DELETE FROM user_relation WHERE table_name = ?", (actual,)
                )
            db.add_domain_table(actual, "spatial")
            registered += 1
    except Exception as exc:  # noqa: BLE001
        raise LocalRegisterError(f"登记未登记图层失败：{exc}") from exc
    finally:
        db.conn.close()
    logger.info("已登记 %d 个未登记矢量图层：%s", registered, db_path)
    return registered


__all__ = [
    "LocalRegisterError",
    "UnregisteredLayer",
    "find_unregistered_spatial_layers",
    "register_spatial_layers",
]
