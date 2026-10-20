"""库里持久化的字段元数据（字典关联 / 可见性）。

SQLite / GeoPackage 没有「字段字典」和「字段隐藏」这种标准位置：

- **别名**：GPKG 写 ``gpkg_data_columns``，SpatiaLite 写本工程的
  ``qgis_dev_column_alias``（见 :mod:`app.core.util.local_db_util`）；
- **字典表 / 字典域 / 本地字典表 / 是否可见**：放在本工程自己的
  ``qgis_dev_field_meta`` 表里，一行一个字段。

这样从「数据管理」重新打开图层时，字典关联和隐藏设置不会丢；创建数据勾选的
字典也能被 :func:`app.core.domain.create_data.fields.fields_from_layer` 再读出来。

本模块只依赖 sqlite3 连接对象，Qt-free。
"""

from __future__ import annotations

import sqlite3
from contextlib import suppress
from dataclasses import dataclass

#: 图层自定义属性前缀（与 create_data/fields.py 保持一致；导入侧读的就是这几个键）
DICT_TABLE_PROP = "create_data/dict_table/"
DICT_DOMAIN_PROP = "create_data/dict_domain/"
DICT_LOCAL_TABLE_PROP = "create_data/dict_local_table/"

#: 表名（与 user_relation / metadata 同级，不登记进 gpkg_contents）
FIELD_META_TABLE = "qgis_dev_field_meta"

_SCHEMA = f"""
CREATE TABLE IF NOT EXISTS "{FIELD_META_TABLE}" (
    table_name       TEXT NOT NULL,
    column_name      TEXT NOT NULL,
    dict_table       TEXT NOT NULL DEFAULT '',
    dict_domain      TEXT NOT NULL DEFAULT '',
    dict_local_table TEXT NOT NULL DEFAULT '',
    visible          INTEGER NOT NULL DEFAULT 1,
    PRIMARY KEY (table_name, column_name)
)
"""


@dataclass(frozen=True)
class FieldMeta:
    dict_table: str = ""
    dict_domain: str = ""
    dict_local_table: str = ""
    visible: bool = True

    @property
    def is_default(self) -> bool:
        return (
            not self.dict_table
            and not self.dict_domain
            and not self.dict_local_table
            and self.visible
        )


def ensure_field_meta_table(conn: sqlite3.Connection) -> None:
    """幂等建表（不 commit，跟随调用方的事务）。"""
    conn.execute(_SCHEMA)


def read_field_meta(
    conn: sqlite3.Connection, table_name: str
) -> dict[str, FieldMeta]:
    """读一张表的全部字段元数据；表不存在 / 读失败返回空字典。"""
    try:
        rows = conn.execute(
            f"""
            SELECT column_name, dict_table, dict_domain, dict_local_table, visible
            FROM "{FIELD_META_TABLE}" WHERE table_name = ?
            """,
            (table_name,),
        ).fetchall()
    except Exception:  # noqa: BLE001 —— pysqlite3 / stdlib 错误类不兼容
        return {}
    return {
        str(row[0]): FieldMeta(
            dict_table=str(row[1] or ""),
            dict_domain=str(row[2] or ""),
            dict_local_table=str(row[3] or ""),
            visible=bool(row[4]),
        )
        for row in rows
    }


def upsert_field_meta(
    conn: sqlite3.Connection,
    table_name: str,
    column_name: str,
    meta: FieldMeta,
    *,
    commit: bool = False,
) -> None:
    ensure_field_meta_table(conn)
    conn.execute(
        f"""
        INSERT INTO "{FIELD_META_TABLE}"
            (table_name, column_name, dict_table, dict_domain, dict_local_table, visible)
        VALUES (?, ?, ?, ?, ?, ?)
        ON CONFLICT(table_name, column_name) DO UPDATE SET
            dict_table = excluded.dict_table,
            dict_domain = excluded.dict_domain,
            dict_local_table = excluded.dict_local_table,
            visible = excluded.visible
        """,
        (
            table_name,
            column_name,
            meta.dict_table,
            meta.dict_domain,
            meta.dict_local_table,
            1 if meta.visible else 0,
        ),
    )
    if commit:
        conn.commit()


def rename_field_meta(
    conn: sqlite3.Connection, table_name: str, old_name: str, new_name: str
) -> None:
    with suppress(Exception):  # 表还没建 / 驱动异常都按无处理
        conn.execute(
            f'UPDATE "{FIELD_META_TABLE}" SET column_name = ? '
            "WHERE table_name = ? AND column_name = ?",
            (new_name, table_name, old_name),
        )


def delete_field_meta(
    conn: sqlite3.Connection, table_name: str, column_name: str
) -> None:
    with suppress(Exception):  # 同上
        conn.execute(
            f'DELETE FROM "{FIELD_META_TABLE}" WHERE table_name = ? AND column_name = ?',
            (table_name, column_name),
        )


def clear_dict_links(
    conn: sqlite3.Connection,
    *,
    category: str = "",
    local_table: str = "",
    all_rows: bool = False,
) -> int:
    """清空字段的字典表 / 字典域 / 本地字典表关联；返回受影响行数。

    删除本地字典表时调用：否则图层重新打开后字段仍指向已不存在的字典，
    数据管理和字段检查都会读不到字典。
    """
    if all_rows:
        where, params = "1 = 1", []
    else:
        conditions = []
        params: list[str] = []
        if category:
            conditions.append("dict_table = ?")
            params.append(category)
        if local_table:
            conditions.append("dict_local_table = ?")
            params.append(local_table)
        if not conditions:
            return 0
        where = " OR ".join(conditions)
    try:
        sql = (
            f'UPDATE "{FIELD_META_TABLE}" '
            "SET dict_table = '', dict_domain = '', dict_local_table = '' "
            "WHERE " + where
        )
        cur = conn.execute(sql, params)
        return max(0, int(cur.rowcount or 0))
    except Exception:  # noqa: BLE001 —— 表还没建 / 驱动异常
        return 0


def clear_dict_domain_links(
    conn: sqlite3.Connection,
    *,
    domain: str,
    category: str = "",
    local_table: str = "",
) -> int:
    """清空**指定字典域**上的字段关联；返回受影响行数。

    删除某个字典域时调用（区别于 :func:`clear_dict_links` 的整表删除）：
    只清 ``dict_domain`` 等于该域的字段，同一字典表里的其它域保持不动。
    匹配条件用「类别」或「库内表名」二选一（都是该字典表的身份）。
    """
    domain = str(domain or "").strip()
    if not domain:
        return 0
    conditions = ["dict_domain = ?"]
    params: list[str] = [domain]
    if category and local_table:
        conditions.append("(dict_table = ? OR dict_local_table = ?)")
        params.extend((category, local_table))
    elif category:
        conditions.append("dict_table = ?")
        params.append(category)
    elif local_table:
        conditions.append("dict_local_table = ?")
        params.append(local_table)
    try:
        cur = conn.execute(
            f'UPDATE "{FIELD_META_TABLE}" '
            "SET dict_table = '', dict_domain = '', dict_local_table = '' "
            "WHERE " + " AND ".join(conditions),
            params,
        )
        return max(0, int(cur.rowcount or 0))
    except Exception:  # noqa: BLE001 —— 表还没建 / 驱动异常
        return 0


def delete_all_field_meta(conn: sqlite3.Connection, table_name: str) -> None:
    with suppress(Exception):  # 同上
        conn.execute(
            f'DELETE FROM "{FIELD_META_TABLE}" WHERE table_name = ?', (table_name,)
        )
