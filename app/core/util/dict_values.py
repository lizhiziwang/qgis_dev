"""图层字段 → 字典取值（属性表显示 / 单元格编辑 / 导出共用）。

字典本身仍是「库内字典表」（当前库）或应用级「字典管理」。本模块只做一件事：
按图层的 ``create_data/dict_*`` 自定义属性（没有时退回库里的字段元数据）找到
该字段挂接的字典表 / 字典域，把 ``(code, name, parent_code)`` 全部读出来。

Qt-free：只依赖 sqlite / 领域服务，属性表和导出服务都能用。
"""

from __future__ import annotations

import json
import os
import sqlite3
from dataclasses import dataclass

from app.core.util.field_meta import (
    DICT_DOMAIN_PROP,
    DICT_LOCAL_TABLE_PROP,
    DICT_TABLE_PROP,
    read_field_meta,
)
from app.infra.logging import get_logger

logger = get_logger(__name__)


@dataclass(frozen=True)
class DictValue:
    """一条字典项（``code`` 是库里存的原始值，``name`` 是显示名）。"""

    code: str
    name: str
    parent_code: str = ""


@dataclass(frozen=True)
class FieldDict:
    """一个字段挂接的字典域（含全部取值）。"""

    table: str
    domain: str
    local_table: str
    values: tuple[DictValue, ...]

    def name_for(self, value: object) -> str | None:
        """原始值 → 字典显示名（没挂接 / 找不到返回 ``None``）。"""
        if value is None:
            return None
        text = str(value)
        for item in self.values:
            if item.code == text:
                return item.name
        return None

    def code_for(self, name: object) -> str | None:
        """显示名 → 原始值（反查，编辑回填用）。"""
        text = str(name)
        for item in self.values:
            if item.name == text:
                return item.code
        return None

    def as_tree(self) -> list[tuple[int, DictValue]]:
        """按 ``parent_code`` 展开成 ``(层级, 字典项)``，父项先于子项。"""
        children: dict[str, list[DictValue]] = {}
        codes = {item.code for item in self.values}
        roots: list[DictValue] = []
        for item in self.values:
            if item.parent_code and item.parent_code in codes:
                children.setdefault(item.parent_code, []).append(item)
            else:
                roots.append(item)
        out: list[tuple[int, DictValue]] = []
        seen: set[str] = set()

        def walk(item: DictValue, depth: int) -> None:
            if item.code in seen:
                return
            seen.add(item.code)
            out.append((depth, item))
            for child in children.get(item.code, ()):
                walk(child, depth + 1)

        for root in roots:
            walk(root, 0)
        # 成环 / 悬空父项的兜底：把没走到的项按原顺序补进来
        for item in self.values:
            if item.code not in seen:
                walk(item, 0)
        return out


def _quote(name: str) -> str:
    return '"' + str(name).replace('"', '""') + '"'


def _layer_container(layer) -> str:
    try:
        from app.core.util.local_layer_loader import layer_source_container

        return str(layer_source_container(layer) or "")
    except Exception:  # noqa: BLE001
        return ""


def _layer_table(layer) -> str:
    try:
        from app.core.util.local_layer_loader import layer_source_table

        return str(layer_source_table(layer) or "")
    except Exception:  # noqa: BLE001
        return ""


def _read_local_values(
    db_path: str, table: str, category: str, domain: str
) -> tuple[DictValue, ...] | None:
    """从本地库的字典表读取值；表不存在 / 结构不是字典返回 ``None``。"""
    if not db_path or not os.path.isfile(db_path):
        return None
    try:
        conn = sqlite3.connect(db_path, timeout=5.0)
    except sqlite3.Error:
        return None
    try:
        conn.row_factory = sqlite3.Row
        table = str(table or "").strip()
        if not table:
            table = _find_local_dict_table(conn, category)
        if not table:
            return None
        info = conn.execute(f"PRAGMA table_info({_quote(table)})").fetchall()
        columns = {str(row[1]) for row in info}
        if not {"code", "name"} <= columns:
            return None
        parent = "COALESCE(parent_code, '')" if "parent_code" in columns else "''"
        order = "sort_order, rowid" if "sort_order" in columns else "rowid"
        where = ""
        params: list[str] = []
        if "domain" in columns and domain:
            where = "WHERE domain = ?"
            params.append(domain)
        rows = conn.execute(
            f"SELECT code, name, {parent} AS parent_code FROM {_quote(table)} "
            f"{where} ORDER BY {order}",
            params,
        ).fetchall()
        return tuple(
            DictValue(
                code=str(row["code"] or ""),
                name=str(row["name"] or ""),
                parent_code=str(row["parent_code"] or ""),
            )
            for row in rows
        )
    except sqlite3.Error as exc:
        logger.info("读本地字典取值失败 %s/%s：%s", db_path, table, exc)
        return None
    finally:
        conn.close()


def _find_local_dict_table(conn: sqlite3.Connection, category: str) -> str:
    """库里哪张表是 ``category`` 对应的字典表（表名 / attr 两种线索）。"""
    if not category:
        return ""
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


def _read_system_values(category: str, domain: str) -> tuple[DictValue, ...]:
    """应用级「字典管理」里的取值（本地表读不到时的兜底）。"""
    if not category or not domain:
        return ()
    try:
        from app.core.domain.dict_manage import DictService

        items = DictService().export_items_for_domains(category, [domain])
    except Exception as exc:  # noqa: BLE001 —— 字典管理没配置 / 类别不存在
        logger.info("读应用级字典取值失败 %s/%s：%s", category, domain, exc)
        return ()
    return tuple(
        DictValue(
            code=str(item.code or ""),
            name=str(item.name or ""),
            parent_code=str(item.parent_code or ""),
        )
        for item in items
    )


def _meta_from_container(layer) -> dict[str, FieldMeta]:
    """图层没有自定义属性时，从库里的字段元数据补一份（一次读全表）。"""
    container = _layer_container(layer)
    table = _layer_table(layer)
    if not container or not table or not os.path.isfile(container):
        return {}
    try:
        conn = sqlite3.connect(container, timeout=5.0)
    except sqlite3.Error:
        return {}
    try:
        return read_field_meta(conn, table)
    finally:
        conn.close()


def _ogr_field_dicts(layer) -> dict[str, FieldDict]:
    """直接打开 GDB / GPKG 时，从 OGR 字段域得到「字段 → 字典域」。"""
    try:
        from app.core.util.ogr_domains import layer_field_domains

        domains = layer_field_domains(layer)
    except Exception as exc:  # noqa: BLE001
        logger.info("读取 OGR 字段域失败：%s", exc)
        return {}
    return {
        info.field_name: FieldDict(
            table=info.domain_name,
            domain=info.domain_name,
            local_table="",
            values=tuple(
                DictValue(code=code, name=name) for code, name in info.values
            ),
        )
        for info in domains
    }


def _meta_for(metas: dict[str, FieldMeta], field_name: str) -> FieldMeta | None:
    """字段元数据查找（大小写不敏感：SQLite 驱动可能把列名改成小写）。"""
    meta = metas.get(field_name)
    if meta is not None:
        return meta
    folded = field_name.casefold()
    for key, value in metas.items():
        if key.casefold() == folded:
            return value
    return None


def _field_dict(
    layer,
    field_name: str,
    metas: dict[str, FieldMeta],
    ogr_map: dict[str, FieldDict],
) -> FieldDict | None:
    table = str(layer.customProperty(DICT_TABLE_PROP + field_name) or "").strip()
    domain = str(layer.customProperty(DICT_DOMAIN_PROP + field_name) or "").strip()
    local_table = str(
        layer.customProperty(DICT_LOCAL_TABLE_PROP + field_name) or ""
    ).strip()
    if not table or not domain:
        meta = _meta_for(metas, field_name)
        if meta is not None:
            table, domain, local_table = (
                meta.dict_table,
                meta.dict_domain,
                meta.dict_local_table,
            )
    if not table or not domain:
        return ogr_map.get(field_name)
    container = _layer_container(layer)
    values = _read_local_values(container, local_table, table, domain)
    if values is None:
        values = _read_system_values(table, domain)
    return FieldDict(
        table=table,
        domain=domain,
        local_table=local_table,
        values=tuple(values),
    )


def load_field_dict(layer, field_name: str) -> FieldDict | None:
    """取字段挂接的字典域与全部取值；没挂接返回 ``None``。"""
    return _field_dict(
        layer, field_name, _meta_from_container(layer), _ogr_field_dicts(layer)
    )


def layer_dict_field_names(layer) -> list[str]:
    """图层里挂了字典的字段名（只读元数据，不加载取值；导出对话框用）。"""
    if layer is None:
        return []
    metas = _meta_from_container(layer)
    ogr_map = _ogr_field_dicts(layer)
    names: list[str] = []
    for field in layer.fields():
        name = field.name()
        table = str(layer.customProperty(DICT_TABLE_PROP + name) or "").strip()
        domain = str(layer.customProperty(DICT_DOMAIN_PROP + name) or "").strip()
        if not table or not domain:
            meta = _meta_for(metas, name)
            if meta is not None:
                table, domain = meta.dict_table, meta.dict_domain
        if table and domain:
            names.append(name)
        elif name in ogr_map:
            names.append(name)
    return names


def layer_dict_fields(layer) -> list[tuple[str, FieldDict]]:
    """图层里所有挂了字典的字段：``[(字段名, FieldDict), ...]``。"""
    out: list[tuple[str, FieldDict]] = []
    if layer is None:
        return out
    metas = _meta_from_container(layer)
    ogr_map = _ogr_field_dicts(layer)
    for field in layer.fields():
        name = field.name()
        info = _field_dict(layer, name, metas, ogr_map)
        if info is not None and info.values:
            out.append((name, info))
    return out


__all__ = [
    "DictValue",
    "FieldDict",
    "layer_dict_field_names",
    "layer_dict_fields",
    "load_field_dict",
]
