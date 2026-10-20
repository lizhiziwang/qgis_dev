"""导入图层时把源数据源的「属性域 / 字典表」附加进目标本地库。

两类来源：

1. OGR 字段域（FileGDB / GPKG 的 coded value domain）；
2. 源本地库（GPKG / SQLite）里已经登记过的字典表（``user_relation.type=dict``）
   及其字段元数据。

口径：**一次导入只落一张字典表**，表名固定为 ``dict_<导入图层名>``；源数据源
里的多个属性域 / 字典表全部并进这一张（用 ``domain`` 列区分）。目标库里已有
同名字典表就直接合并（补缺失代码、更新名称），重复导入不会产生 ``_1``、``_2``
的重复字典表，也不会留下只有一个域、缺条目的半截字典。
"""

from __future__ import annotations

import json
import os
import sqlite3
from contextlib import suppress
from dataclasses import dataclass

from app.core.util.field_meta import (
    DICT_DOMAIN_PROP,
    DICT_LOCAL_TABLE_PROP,
    DICT_TABLE_PROP,
    FieldMeta,
    read_field_meta,
    upsert_field_meta,
)
from app.infra.logging import get_logger

logger = get_logger(__name__)

_DICT_COLUMNS = (
    ("id", "INTEGER PRIMARY KEY AUTOINCREMENT"),
    ("domain", "TEXT NOT NULL"),
    ("code", "TEXT NOT NULL"),
    ("name", "TEXT NOT NULL"),
    ("parent_code", "TEXT NOT NULL DEFAULT ''"),
    ("sort_order", "INTEGER NOT NULL DEFAULT 0"),
)


@dataclass(frozen=True)
class SourceDictBinding:
    """一个字段从源数据源带来的字典内容。"""

    field_name: str
    #: 字段挂接的字典域（写进字段元数据）
    domain: str
    #: 目标字典表的建议表名（源字典表名 / 属性域名）
    table_base: str
    #: ``(domain, code, name, parent_code, sort_order)``；可以包含多个域
    rows: tuple[tuple[str, str, str, str, int], ...]


def _quote(name: str) -> str:
    return '"' + str(name).replace('"', '""') + '"'


def _local_source_table(conn: sqlite3.Connection, category: str) -> str:
    """源库里 ``category`` 对应的字典表名（表名 / attr 两种线索）。"""
    try:
        rows = conn.execute(
            "SELECT table_name, attr FROM user_relation WHERE type = 'dict'"
        ).fetchall()
    except sqlite3.Error:
        return ""
    for table, attr in rows:
        table = str(table or "")
        if table == category:
            return table
        try:
            data = json.loads(attr) if attr else {}
        except (TypeError, ValueError):
            data = {}
        if isinstance(data, dict) and str(data.get("dict_table") or "") == category:
            return table
    return ""


def _read_dict_rows(
    conn: sqlite3.Connection, table: str, default_domain: str
) -> tuple[tuple[str, str, str, str, int], ...]:
    """读源字典表：返回 canonical 行（缺域列时用字段的域兜底）。"""
    info = conn.execute(f"PRAGMA table_info({_quote(table)})").fetchall()
    columns = {str(row[1]) for row in info}
    if not {"code", "name"} <= columns:
        return ()
    domain_expr = "domain" if "domain" in columns else "?"
    parent_expr = "COALESCE(parent_code, '')" if "parent_code" in columns else "''"
    sort_expr = "sort_order" if "sort_order" in columns else "0"
    params = () if "domain" in columns else (default_domain,)
    order = "sort_order, rowid" if "sort_order" in columns else "rowid"
    rows = conn.execute(
        f"SELECT {domain_expr}, code, name, {parent_expr}, {sort_expr} "
        f"FROM {_quote(table)} ORDER BY {order}",
        params,
    ).fetchall()
    return tuple(
        (
            str(row[0] or default_domain),
            str(row[1] or ""),
            str(row[2] or ""),
            str(row[3] or ""),
            int(row[4] or 0),
        )
        for row in rows
        if row[1] not in (None, "") and row[2] not in (None, "")
    )


def _source_local_bindings(layer) -> list[SourceDictBinding]:
    """源本地库里的字典表 + 字段元数据 → bindings。"""
    try:
        from app.core.util.local_layer_loader import (
            layer_source_container,
            layer_source_table,
        )

        container = str(layer_source_container(layer) or "")
        table = str(layer_source_table(layer) or "")
    except Exception:  # noqa: BLE001
        return []
    if not container or not table or not os.path.isfile(container):
        return []
    try:
        conn = sqlite3.connect(container, timeout=5.0)
    except sqlite3.Error:
        return []
    try:
        conn.row_factory = sqlite3.Row
        metas = read_field_meta(conn, table)
        props: dict[str, tuple[str, str, str]] = {}
        for field in layer.fields():
            name = field.name()
            cat = str(layer.customProperty(DICT_TABLE_PROP + name) or "").strip()
            dom = str(layer.customProperty(DICT_DOMAIN_PROP + name) or "").strip()
            local = str(
                layer.customProperty(DICT_LOCAL_TABLE_PROP + name) or ""
            ).strip()
            if cat and dom:
                props[name] = (cat, dom, local)
        out: list[SourceDictBinding] = []
        seen: set[tuple[str, str, tuple]] = set()
        meta_folded = {name.casefold(): meta for name, meta in metas.items()}
        for field in layer.fields():
            name = field.name()
            meta = metas.get(name) or meta_folded.get(name.casefold())
            if name in props:
                category, domain, local = props[name]
            elif meta is not None and meta.dict_table and meta.dict_domain:
                category, domain, local = (
                    meta.dict_table,
                    meta.dict_domain,
                    meta.dict_local_table,
                )
            else:
                continue
            source_table = local or _local_source_table(conn, category)
            if not source_table:
                continue
            try:
                rows = _read_dict_rows(conn, source_table, domain)
            except sqlite3.Error:
                continue
            if not rows:
                continue
            key = (source_table, domain, rows)
            if key in seen:
                continue
            seen.add(key)
            out.append(
                SourceDictBinding(
                    field_name=name,
                    domain=domain,
                    table_base=source_table,
                    rows=rows,
                )
            )
        return out
    except Exception as exc:  # noqa: BLE001
        logger.info("读取源库字典表失败 %s/%s：%s", container, table, exc)
        return []
    finally:
        conn.close()


def source_dict_bindings(layer) -> list[SourceDictBinding]:
    """收集源图层的 OGR 属性域 + 源本地库字典表。"""
    bindings: list[SourceDictBinding] = []
    seen: set[tuple[str, str, str]] = set()

    def add(binding: SourceDictBinding) -> None:
        key = (binding.field_name, binding.domain, binding.table_base)
        if key in seen:
            return
        seen.add(key)
        bindings.append(binding)

    try:
        from app.core.util.ogr_domains import layer_field_domains

        for info in layer_field_domains(layer):
            rows = tuple(
                (info.domain_name, code, name, "", index)
                for index, (code, name) in enumerate(info.values)
            )
            add(
                SourceDictBinding(
                    field_name=info.field_name,
                    domain=info.domain_name,
                    table_base=info.domain_name,
                    rows=rows,
                )
            )
    except Exception as exc:  # noqa: BLE001
        logger.info("读取 OGR 属性域失败：%s", exc)
    for binding in _source_local_bindings(layer):
        add(binding)
    return bindings


def _table_exists(util, table: str) -> bool:
    row = util.cursor.execute(
        "SELECT 1 FROM sqlite_master WHERE type IN ('table', 'view') AND name = ?",
        (table,),
    ).fetchone()
    return row is not None


def _dict_table_names(util) -> set[str]:
    try:
        rows = util.cursor.execute(
            "SELECT table_name FROM user_relation WHERE type = 'dict'"
        ).fetchall()
        return {str(row[0]) for row in rows}
    except Exception:  # noqa: BLE001
        return set()


def _ensure_dict_table(util, table: str, *, create: bool) -> set[str] | None:
    """确保表是 canonical 字典结构；返回列名集合。"""
    if not _table_exists(util, table):
        if not create:
            return None
        util.cursor.execute(
            f"CREATE TABLE {_quote(table)} (" + ", ".join(
                f"{_quote(name)} {declared}" for name, declared in _DICT_COLUMNS
            ) + ")"
        )
    columns = {
        str(row[1])
        for row in util.cursor.execute(f"PRAGMA table_info({_quote(table)})")
    }
    if not {"domain", "code", "name"} <= columns:
        return None
    index = "ux_" + str(table).replace('"', "") + "_import_domain_code"
    with suppress(Exception):
        util.cursor.execute(
            f"CREATE UNIQUE INDEX IF NOT EXISTS {_quote(index)} "
            f"ON {_quote(table)} (domain, code)"
        )
    return columns


def _register_dict(util, table: str, domains: set[str]) -> None:
    try:
        row = util.cursor.execute(
            "SELECT attr FROM user_relation WHERE table_name = ? AND type = 'dict'",
            (table,),
        ).fetchone()
        data = {}
        if row and row[0]:
            try:
                data = json.loads(row[0])
            except (TypeError, ValueError):
                data = {}
        if not isinstance(data, dict):
            data = {}
        merged = set(str(item) for item in (data.get("domains") or [])) | domains
        data.update(
            {
                "schema": data.get("schema") or "dict_v1",
                "dict_table": table,
                "domains": sorted(merged),
            }
        )
        util.cursor.execute(
            "DELETE FROM user_relation WHERE table_name = ? AND type = 'dict'",
            (table,),
        )
        util.add_domain_table(
            table, "dict", json.dumps(data, ensure_ascii=False)
        )
    except Exception:  # noqa: BLE001
        logger.warning("登记导入字典表失败：%s", table, exc_info=True)


def merge_import_dicts(util, target_table: str, bindings, field_sql_map) -> int:
    """把 bindings 合并进**一张**目标字典表；返回写入/更新的字典表数。

    表名固定为 ``dict_<导入图层名>``（重名时按 ``_1`` / ``_2`` 避让）；源数据源
    的多个属性域 / 字典表都写进这一张，用 ``domain`` 列区分。目标已有同名字典表
    就直接复用补缺，已存在的 ``(domain, code)`` 行只更新名称 / 上级 / 排序。
    """
    if not bindings:
        return 0
    db_path = str(util.db_path)
    from app.core.domain.import_layer import unique_table_name

    names = _dict_table_names(util)
    base = f"dict_{str(target_table or '').strip()}".strip() or "dict_imported_layer"
    target = ""
    touched: set[str] = set()
    for binding in bindings:
        domain = str(binding.domain or "")
        if not domain or not binding.rows:
            continue
        if not target:
            # 同名（dict_<图层名>）字典表已存在就复用它；否则起一个不撞库的名字
            target = base if base in names else unique_table_name(db_path, base)
            names.add(target)
        columns = _ensure_dict_table(util, target, create=True)
        if columns is None:
            logger.warning("导入字典表结构不对，已跳过：%s", target)
            continue
        has_parent = "parent_code" in columns
        has_sort = "sort_order" in columns
        insert_cols = ["domain", "code", "name"]
        if has_parent:
            insert_cols.append("parent_code")
        if has_sort:
            insert_cols.append("sort_order")
        updates = ["name = excluded.name"]
        if has_parent:
            updates.append("parent_code = excluded.parent_code")
        if has_sort:
            updates.append("sort_order = excluded.sort_order")
        sql = (
            f"INSERT INTO {_quote(target)} "
            f"({', '.join(_quote(name) for name in insert_cols)}) "
            f"VALUES ({', '.join('?' for _ in insert_cols)}) "
            f"ON CONFLICT(domain, code) DO UPDATE SET {', '.join(updates)}"
        )
        params = []
        for row_domain, code, name, parent, sort in binding.rows:
            values = [row_domain or domain, code, name]
            if has_parent:
                values.append(parent)
            if has_sort:
                values.append(sort)
            params.append(tuple(values))
        try:
            util.cursor.executemany(sql, params)
        except Exception:
            # 没有唯一索引（老表有重复行）：退回只补缺，不因合并失败丢整批
            logger.warning("字典表 %s 合并失败，改为只补缺失行", target, exc_info=True)
            try:
                util.cursor.executemany(
                    f"INSERT OR IGNORE INTO {_quote(target)} "
                    f"({', '.join(_quote(name) for name in insert_cols)}) "
                    f"VALUES ({', '.join('?' for _ in insert_cols)})",
                    params,
                )
            except Exception as exc:  # noqa: BLE001
                logger.warning("写入字典表 %s 失败：%s", target, exc)
                continue
        names.add(target)
        touched.add(target)
        _register_dict(util, target, {str(row[0] or domain) for row in binding.rows} | {domain})
        sql_field = field_sql_map.get(binding.field_name)
        if sql_field:
            try:
                upsert_field_meta(
                    util.conn,
                    target_table,
                    sql_field,
                    FieldMeta(
                        dict_table=target,
                        dict_domain=domain,
                        dict_local_table=target,
                    ),
                )
            except Exception:  # noqa: BLE001
                logger.warning("写导入字段字典元数据失败：%s", sql_field, exc_info=True)
    try:
        util.conn.commit()
    except Exception:  # noqa: BLE001
        logger.warning("提交导入字典表失败", exc_info=True)
    return len(touched)


__all__ = ["SourceDictBinding", "merge_import_dicts", "source_dict_bindings"]
