"""删除本地库里已注册的数据(数据表 + ``user_relation`` 注册行)。

「数据管理」Dock 左树右键「删除图层」的业务实现:把库内那张表连同目录注册
一起抹掉,使左树 / 右表刷新后都不再出现它。按类别分流落库:

- ``spatial`` / ``dict``:走 :meth:`local_db_util._drop_spatial_table`
  —— spatialite 先拆空间索引、注销几何列再删表;GeoPackage 先清
  ``gpkg_geometry_columns`` / ``gpkg_contents`` 登记再删表。标量表(dict)
  没有几何登记,那几条清理静默失败后照样删表;
- ``image``:走 :meth:`local_db_util._drop_image_store_table`
  —— GeoPackage 额外清 ``gpkg_tile_matrix*`` 金字塔登记。

spatialite 侧再补一刀**不依赖 mod_spatialite** 的目录清理(直接删
``geometry_columns`` / ``views_geometry_columns`` / ``virts_geometry_columns``
里该表的行):老库没装扩展时 ``DisableSpatialIndex`` 会失败,只删表会在
``geometry_columns`` 留下指向空表名的脏行。

最后删 ``user_relation`` 中该表名的注册行——这就是需求里的「同步删除注册
信息」;只删表不删注册会在树上留一个双击打不开的脏节点。整个操作一次性
提交,失败抛 :class:`DeleteDataError` 由调用方弹模态。

工程里已打开的对应图层**不在这里动**——释放文件句柄 / gpkg 别名属于 UI
层的事(见 ``app.ui.main_window.mixins.app_actions`` 的 ``_on_dm_delete_layer``
先移图层再删库)。
"""

from __future__ import annotations

from contextlib import suppress
from pathlib import Path
from typing import Any

# 必须用 pysqlite3 的异常类: local_db_util 的驱动就是 pysqlite3
# (``import pysqlite3 as sqlite3``),它抛的 DatabaseError / OperationalError
# 不是标准库 sqlite3 里的那几个类,用标准库的 ``except sqlite3.Error``
# 接不住(异常会原样穿出去,调用方看到的是裸 DatabaseError)。
import pysqlite3 as sqlite3

from app.core.util.local_db_util import local_db_util
from app.infra.logging import get_logger

logger = get_logger(__name__)


class DeleteDataError(RuntimeError):
    """删除本地库数据失败(路径缺失 / 服务器库 / 库格式不支持 / 写库出错)。"""


def delete_registered_data(db: dict[str, Any], category: str, name: str) -> None:
    """删掉 ``db`` 里注册名为 ``name`` 的数据表 + 其 ``user_relation`` 注册行。

    Args:
        db: 本地库连接 dict(至少要有 ``path``;``kind == "server"`` 直接拒绝)。
        category: 注册类别(``spatial`` / ``dict`` / ``image``)。
        name: 注册的数据表名(与 ``user_relation.table_name`` 一致)。

    Raises:
        DeleteDataError: 路径缺失 / 文件不存在 / 服务器库 / 打开或写库失败。
    """
    if db.get("kind") == "server":
        raise DeleteDataError(
            "服务器库删除尚未实现，请选用本地库（SpatiaLite / GeoPackage）"
        )
    path = str(db.get("path") or "")
    if not path:
        raise DeleteDataError(f"「{name}」缺少库文件路径，无法删除")
    if not Path(path).is_file():
        raise DeleteDataError(f"本地库文件不存在：{path}")

    try:
        util = local_db_util(path)
    except sqlite3.Error as exc:
        raise DeleteDataError(f"打开本地库失败：{path}（{exc}）") from exc

    try:
        _delete_with_util(util, category, name)
        util.conn.commit()
    except sqlite3.Error as exc:
        with suppress(sqlite3.Error):
            util.conn.rollback()
        raise DeleteDataError(f"删除「{name}」失败：{exc}") from exc
    finally:
        util.conn.close()
    logger.info("已删除本地库数据: %s → %s(%s)", Path(path).name, name, category)


def delete_all_registered_data(
    db: dict[str, Any],
) -> tuple[int, list[tuple[str, str, str]]]:
    """删掉库里**注册的全部数据表** + ``user_relation`` 注册行。

    数据管理左树右键库连接「移除全部图层」用。一次打开库文件、逐表删除
    （每张表独立提交）：某张表删失败只记进失败清单，**不影响其余表** ——
    批量操作里最怕"删到一半停下，用户不知道哪些删了哪些没删"。

    Args:
        db: 本地库连接 dict(至少 ``path`` 与 ``categories``；后者是
            ``{类别: [表名…]}``，由 :func:`describe_local_db` 扫描得到)。

    Returns:
        ``(成功删除的表数, [(类别, 表名, 失败原因), …])``。
        库里没有注册表时返回 ``(0, [])``。

    Raises:
        DeleteDataError: 服务器库 / 路径缺失 / 文件不存在 / 打不开库。
    """
    if db.get("kind") == "server":
        raise DeleteDataError(
            "服务器库删除尚未实现，请选用本地库（SpatiaLite / GeoPackage）"
        )
    path = str(db.get("path") or "")
    if not path:
        raise DeleteDataError("库连接缺少文件路径，无法移除图层")
    if not Path(path).is_file():
        raise DeleteDataError(f"本地库文件不存在：{path}")

    categories = db.get("categories") or {}
    targets = [
        (str(category), str(name))
        for category, names in categories.items()
        for name in (names or [])
        if name
    ]
    if not targets:
        return 0, []

    try:
        util = local_db_util(path)
    except sqlite3.Error as exc:
        raise DeleteDataError(f"打开本地库失败：{path}（{exc}）") from exc

    deleted = 0
    failures: list[tuple[str, str, str]] = []
    try:
        for category, name in targets:
            try:
                _delete_with_util(util, category, name)
                util.conn.commit()
                deleted += 1
            except Exception as exc:  # noqa: BLE001 —— 单表失败不能中断整批
                with suppress(Exception):
                    util.conn.rollback()
                failures.append((category, name, str(exc)))
    finally:
        util.conn.close()
    logger.info(
        "已移除本地库 %s 的 %d 个图层（失败 %d 个）",
        Path(path).name,
        deleted,
        len(failures),
    )
    return deleted, failures


def _delete_with_util(util: local_db_util, category: str, name: str) -> None:
    """在已打开的库连接上删一张表 + 它的注册行（不提交，由调用方提交 / 回滚）。"""
    if category == "image":
        util._drop_image_store_table(name)
    else:
        util._drop_spatial_table(name, _geometry_column(util, name))
        if util.kind == "spatialite":
            _purge_spatialite_catalog(util, name)
    # 老库 / 纯 MBTiles 库可能压根没有 user_relation(那也就没注册行可删)
    with suppress(sqlite3.OperationalError):
        util.cursor.execute(
            "DELETE FROM user_relation WHERE table_name = ?", (name,)
        )


def _geometry_column(util: local_db_util, table: str) -> str:
    """查该表在库里的几何列名(spatialite ``geometry_columns`` / GPKG 侧)。

    查不到退回 ``geom``: ``_drop_spatial_table`` 对不存在的几何列只在
    spatialite 侧默默失败(那几条语句本就带 ``suppress``),不影响删表。
    """
    sql = (
        "SELECT column_name FROM gpkg_geometry_columns WHERE table_name = ?"
        if util.kind == "gpkg"
        else "SELECT f_geometry_column FROM geometry_columns WHERE f_table_name = ?"
    )
    try:
        row = util.cursor.execute(sql, (table,)).fetchone()
    except sqlite3.Error:
        return "geom"
    return str(row[0]) if row and row[0] else "geom"


def _purge_spatialite_catalog(util: local_db_util, table: str) -> None:
    """清 spatialite 空间目录里该表的登记行(不依赖 mod_spatialite 是否加载)。"""
    for stmt, arg in (
        ("DELETE FROM geometry_columns WHERE f_table_name = ?", table),
        ("DELETE FROM views_geometry_columns WHERE view_name = ?", table),
        ("DELETE FROM virts_geometry_columns WHERE virt_name = ?", table),
    ):
        with suppress(sqlite3.Error):
            util.cursor.execute(stmt, (arg,))


__all__ = ["DeleteDataError", "delete_all_registered_data", "delete_registered_data"]
