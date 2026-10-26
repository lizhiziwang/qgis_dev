"""创建数据：把业务字典落进目标本地库（Qt-free）。

口径（与字典管理一致）：**一张字典表（类别）落一张表，多个字典域用 ``domain``
列共存**。外部数据集源库里的 dict 表不按原样复制，而是**归一化**到同一张
canonical 表里；没有可识别的代码/名称列时跳过并报告。

目标表的固定结构（应用级字典）::

    id          INTEGER PRIMARY KEY AUTOINCREMENT
    domain      TEXT NOT NULL          -- 字典域
    code        TEXT NOT NULL
    name        TEXT NOT NULL
    parent_code TEXT NOT NULL DEFAULT ''
    sort_order  INTEGER NOT NULL DEFAULT 0
    UNIQUE(domain, code)               -- 以唯一索引实现

本模块只做「把已解析好的数据写进库」：字典数据由调用方（创建数据对话框）从
:class:`~app.core.domain.dict_manage.service.DictService` 解析后装进
:class:`DictTableSpec`，因此本模块不依赖 UI。
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import sqlite3
from contextlib import suppress
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from app.infra.logging import get_logger

if TYPE_CHECKING:
    from collections.abc import Sequence

logger = get_logger(__name__)

#: 应用级字典落库的表结构版本（写进 user_relation.attr）
DICT_SCHEMA_VERSION = "dict_v1"

#: 应用级字典目标表的列定义
DICT_COLUMNS: tuple[tuple[str, str], ...] = (
    ("id", "INTEGER PRIMARY KEY AUTOINCREMENT"),
    ("domain", "TEXT NOT NULL"),
    ("code", "TEXT NOT NULL"),
    ("name", "TEXT NOT NULL"),
    ("parent_code", "TEXT NOT NULL DEFAULT ''"),
    ("sort_order", "INTEGER NOT NULL DEFAULT 0"),
)

#: 应用级字典写入时实际提供的列（id 自增）
DICT_INSERT_COLUMNS: tuple[str, ...] = (
    "domain",
    "code",
    "name",
    "parent_code",
    "sort_order",
)

#: 源库字典表列名识别（大小写不敏感）
_SOURCE_COLUMN_ALIASES: dict[str, tuple[str, ...]] = {
    "domain": ("domain", "dict_domain", "dictdomain", "字典域", "域"),
    "code": ("code", "dict_code", "代码", "编码"),
    "name": ("name", "dict_name", "名称"),
    "parent": ("parent_code", "parent", "上级代码", "父代码"),
    "sort": ("sort_order", "sort", "order", "排序", "排序号"),
}

#: 目标库保留表名（不能拿来建字典表）
_RESERVED_TABLES = frozenset(
    {
        "user_relation",
        "metadata",
        "qgis_dev_column_alias",
        "qgis_dev_field_meta",
        "sqlite_sequence",
    }
)

_SAFE_TOKEN = re.compile(r"[^\w]+", re.UNICODE)


class DictTableError(RuntimeError):
    """字典表落库失败。消息可直接展示。"""


@dataclass(frozen=True)
class DictSource:
    """外部数据集源库里的一张 dict 表（已关联到字段的字典表 / 域）。"""

    db_path: str
    table: str
    category: str = ""
    domain: str = ""


@dataclass(frozen=True)
class DictInline:
    """随字段结构一起带进来的字典内容（GDB / GPKG 属性域等）。

    没有源库字典表可读（属性域不在 ``user_relation`` 里），直接把
    ``(domain, code, name, parent_code, sort_order)`` 行带进创建流程。
    """

    category: str
    domain: str
    rows: tuple[tuple[str, str, str, str, int], ...] = ()


@dataclass(frozen=True)
class DictPlan:
    """创建数据字段表里涉及的字典需求。"""

    domains: frozenset[tuple[str, str]] = frozenset()
    sources: tuple[DictSource, ...] = ()
    #: 直接带值的字典（GDB / GPKG 属性域），不需要再读系统字典或源库
    inline: tuple[DictInline, ...] = ()


@dataclass(frozen=True)
class DictTableSpec:
    """一张待写入目标库的字典表。"""

    table_name: str
    columns: tuple[tuple[str, str], ...]
    rows: tuple[tuple[Any, ...], ...]
    attr: str = ""
    #: 目标表已存在时不再覆盖已有行（只补缺）
    upsert_key: tuple[str, ...] = ()
    insert_columns: tuple[str, ...] = ()
    category: str = ""
    domains: tuple[str, ...] = ()


# --------------------------------------------------------------------- 工具


def _sql_token(name: object) -> str:
    """把字典表 / 表名洗成可用的 SQL 标识符（保留中文和词字符）。"""
    text = _SAFE_TOKEN.sub("_", str(name or "").strip())
    text = text.strip("_")
    if not text:
        text = "dict"
    if text[0].isdigit():
        text = f"_{text}"
    if text.lower().startswith("sqlite_"):
        text = f"dict_{text}"
    return text[:60]


def _quote(name: str) -> str:
    return '"' + str(name).replace('"', '""') + '"'


def _index_token(raw: object) -> str:
    """字典表索引名：可读短名 + 原文哈希，避免长表名截断后撞名。

    ``_sql_token`` 会截到 60 字符；对很长的字典表名，
    ``ux_<表名>_domain_code`` 与 ``ux_<表名>_domain_parent`` 会截成**同一个名字**。
    先建的普通索引让后面的唯一索引 ``IF NOT EXISTS`` 直接跳过，随后
    ``INSERT ... ON CONFLICT(domain, code)`` 就报
    ``ON CONFLICT clause does not match any PRIMARY KEY or UNIQUE constraint``
    （GDB 属性域导出到 GeoPackage 时踩过）。
    """
    text = str(raw or "")
    token = _sql_token(text)
    digest = hashlib.sha1(text.encode("utf-8")).hexdigest()[:8]
    return f"{token[:50]}_{digest}"


def local_dict_categories(db_path: str) -> set[str]:
    """目标库里已登记为字典表的类别名集合。

    本地字典的"名字"以**库内表名**为准（``dict_table`` 只是兼容老数据的
    逻辑名），所以表名本身一定算一个可挂接类别。
    """
    _tables, attrs = _read_target_state(db_path)
    categories: set[str] = set(attrs)
    for table, attr in attrs.items():
        data = attr if isinstance(attr, dict) else {}
        categories.add(str(data.get("dict_table") or data.get("category") or table))
    return categories


def _local_dict_table(db_path: str, category: str, attrs: dict[str, dict]) -> str:
    """目标库里 ``category`` 对应的字典表名（没有返回空串）。"""
    for table, attr in attrs.items():
        data = attr if isinstance(attr, dict) else {}
        name = str(data.get("dict_table") or data.get("category") or table)
        if name == category:
            return table
    return category if category in attrs else ""


def _read_target_state(path: str) -> tuple[set[str], dict[str, dict]]:
    """读目标库已有表名和 ``user_relation`` 里登记的 dict 表属性。"""
    if not path or not os.path.isfile(path):
        return set(), {}
    tables: set[str] = set()
    attrs: dict[str, dict] = {}
    try:
        conn = sqlite3.connect(str(path), timeout=5.0)
    except sqlite3.Error:
        return set(), {}
    try:
        conn.execute("PRAGMA query_only = ON")
        tables = {
            str(row[0])
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type IN ('table', 'view')"
            )
        }
        try:
            for name, attr in conn.execute(
                "SELECT table_name, attr FROM user_relation WHERE type = 'dict'"
            ):
                try:
                    attrs[str(name)] = json.loads(attr) if attr else {}
                except (TypeError, ValueError):
                    attrs[str(name)] = {}
        except sqlite3.Error:
            pass
    except sqlite3.Error:
        return tables, attrs
    finally:
        conn.close()
    return tables, attrs


def _choose_name(
    preferred: str,
    tables: set[str],
    attrs: dict[str, dict],
    used: set[str],
    accept,
) -> str:
    """选一个目标表名：已存在且属性对得上就复用，否则加后缀。"""
    lowered = {name.casefold() for name in tables | used}
    if preferred.casefold() not in lowered and preferred not in _RESERVED_TABLES:
        used.add(preferred)
        return preferred
    if preferred in attrs and accept(attrs.get(preferred) or {}):
        used.add(preferred)
        return preferred
    index = 2
    while True:
        candidate = f"{preferred}_{index}"
        if (
            candidate.casefold() not in {name.casefold() for name in tables | used}
            and candidate not in _RESERVED_TABLES
        ):
            used.add(candidate)
            return candidate
        index += 1


def _read_table_rows(source_path: str, table: str) -> tuple[list[tuple[str, str]], list[tuple]]:
    """只读读取源库的一张表（列定义 + 全部行）。"""
    if not source_path or not os.path.isfile(source_path):
        raise DictTableError(f"源库不存在：{source_path}")
    conn = sqlite3.connect(str(source_path), timeout=5.0)
    try:
        conn.execute("PRAGMA query_only = ON")
        info = conn.execute(f"PRAGMA table_info({_quote(table)})").fetchall()
        if not info:
            raise DictTableError(f"源库字典表不存在：{table}")
        columns = [(str(row[1]), str(row[2] or "TEXT")) for row in info]
        col_sql = ", ".join(_quote(name) for name, _decl in columns)
        rows = [tuple(row) for row in conn.execute(f"SELECT {col_sql} FROM {_quote(table)}")]
        return columns, rows
    except sqlite3.Error as exc:
        raise DictTableError(f"读源库字典表失败：{table}（{exc}）") from exc
    finally:
        conn.close()


def _read_source_attr(source_path: str, table: str) -> dict:
    try:
        conn = sqlite3.connect(str(source_path), timeout=5.0)
    except sqlite3.Error:
        return {}
    try:
        conn.execute("PRAGMA query_only = ON")
        row = conn.execute(
            "SELECT attr FROM user_relation WHERE table_name = ? AND type = 'dict'",
            (table,),
        ).fetchone()
        if row is None or not row[0]:
            return {}
        try:
            value = json.loads(row[0])
        except (TypeError, ValueError):
            return {}
        return value if isinstance(value, dict) else {}
    except sqlite3.Error:
        return {}
    finally:
        conn.close()


def _column_map(columns: Sequence[tuple[str, str]]) -> dict[str, int]:
    """源表列名 → 规范列 index（domain/code/name/parent/sort）。"""
    folded = {str(name).casefold(): index for index, (name, _decl) in enumerate(columns)}
    found: dict[str, int] = {}
    for key, aliases in _SOURCE_COLUMN_ALIASES.items():
        for alias in aliases:
            if alias.casefold() in folded:
                found[key] = folded[alias.casefold()]
                break
    return found


def _normalize_source_rows(
    source_path: str,
    table: str,
    *,
    default_category: str,
    default_domain: str,
) -> tuple[str, list[tuple[str, str, str, str, int]]]:
    """把源库 dict 表归一化成 canonical 行；返回 ``(字典表名, 行)``。"""
    columns, raw_rows = _read_table_rows(source_path, table)
    mapping = _column_map(columns)
    if "code" not in mapping or "name" not in mapping:
        raise DictTableError(
            f"源库字典表「{table}」缺少代码/名称列，无法归一化，已跳过"
        )
    attr = _read_source_attr(source_path, table)
    category = str(
        attr.get("dict_table") or attr.get("category") or default_category or ""
    ).strip()
    if not category:
        raise DictTableError(f"源库字典表「{table}」无法确定字典表名，已跳过")
    rows: list[tuple[str, str, str, str, int]] = []
    for index, row in enumerate(raw_rows):
        def cell(key: str, row=row) -> str:
            pos = mapping.get(key)
            if pos is None or pos >= len(row):
                return ""
            value = row[pos]
            return "" if value is None else str(value).strip()

        code = cell("code")
        name = cell("name")
        if not code or not name:
            continue
        domain = cell("domain") or default_domain
        if not domain:
            # 没有域列时没法归入任何域；用表名兜底并留下痕迹
            domain = str(table)
        parent = cell("parent")
        try:
            sort_order = int(cell("sort") or index)
        except ValueError:
            sort_order = index
        rows.append((domain, code, name, parent, sort_order))
    return category, rows


# --------------------------------------------------------------------- 构建


def normalize_local_dict_names(db_path: str) -> int:
    """老本地库迁移：字典逻辑名统一成库内表名，并同步字段元数据。

    早期本地字典表把「复制来源的系统字典类别」记在 ``attr.dict_table``，
    导致同一个字典出现两个名字（如「字典表」/「字典表12」），而且重命名
    表名后字段上还显示旧名。这里幂等地把 ``attr.dict_table`` 改成表名，
    并把挂在该本地表（``dict_local_table``）上的字段元数据 ``dict_table``
    一并改成表名。返回需要改写的字典表数量。
    """
    if not db_path or not os.path.isfile(db_path):
        return 0
    from app.core.util.local_db_util import local_db_util

    db = local_db_util(db_path)
    changed = 0
    try:
        rows = db.cursor.execute(
            "SELECT table_name, attr FROM user_relation WHERE type = 'dict'"
        ).fetchall()
        for table, attr in rows:
            table = str(table)
            try:
                data = json.loads(attr) if attr else {}
            except (TypeError, ValueError):
                data = {}
            if not isinstance(data, dict):
                data = {}
            old = str(data.get("dict_table") or data.get("category") or table)
            if old != table:
                data["dict_table"] = table
                data.pop("category", None)
                db.cursor.execute(
                    "UPDATE user_relation SET attr = ? "
                    "WHERE table_name = ? AND type = 'dict'",
                    (json.dumps(data, ensure_ascii=False), table),
                )
                changed += 1
            with suppress(Exception):
                db.cursor.execute(
                    'UPDATE "qgis_dev_field_meta" SET dict_table = ? '
                    "WHERE dict_local_table = ? AND dict_table <> ?",
                    (table, table, table),
                )
        db.conn.commit()
        return changed
    except Exception as exc:  # noqa: BLE001 —— 迁移失败只记日志，不影响正常打开
        with suppress(Exception):
            db.conn.rollback()
        logger.warning("规范化本地字典表名失败 %s：%s", db_path, exc)
        return 0
    finally:
        db.conn.close()


def build_dict_table_specs(
    target_db_path: str,
    plan: DictPlan,
    dict_service,
) -> list[DictTableSpec]:
    """把 :class:`DictPlan` 解析成「每个字典表一张」的目标表 specs。"""
    tables, attrs = _read_target_state(target_db_path)
    used: set[str] = set()
    # category → domain → (code,name,parent,sort)，后写覆盖先写
    grouped: dict[str, dict[str, dict[str, tuple[str, str, str, int]]]] = {}

    by_category: dict[str, set[str]] = {}
    #: 类别 → 目标库里已有的字典表名（落库时沿用真实表名）
    local_names: dict[str, str] = {}
    for category, domain in plan.domains:
        category, domain = str(category or "").strip(), str(domain or "").strip()
        if category and domain:
            by_category.setdefault(category, set()).add(domain)
    # 直接带值的字典（GDB / GPKG 属性域）：先灌进分组，后续不再去系统字典查
    inline_categories = {item.category for item in plan.inline}
    for item in plan.inline:
        bucket = grouped.setdefault(item.category, {})
        for domain, code, name, parent, sort_order in item.rows:
            bucket.setdefault(domain, {})[code] = (name, parent, sort_order)
        bucket.setdefault(item.domain, {})
    for category, domains in by_category.items():
        bucket = grouped.setdefault(category, {})
        # **目标库自己已有的字典表优先**：字段挂接只允许选当前库里的字典，
        # 内容就该从这张表原样带过来（不再绕回应用级字典管理找同类别）。
        local_table = _local_dict_table(target_db_path, category, attrs)
        if local_table and local_table in tables:
            local_names[category] = local_table
            try:
                _cat, local_rows = _normalize_source_rows(
                    target_db_path,
                    local_table,
                    default_category=category,
                    default_domain="",
                )
            except DictTableError as exc:
                logger.warning("读取目标库字典表 %s 失败：%s", local_table, exc)
                local_rows = []
            for domain, code, name, parent, sort_order in local_rows:
                bucket.setdefault(domain, {})[code] = (name, parent, sort_order)
            for domain in domains:
                bucket.setdefault(domain, {})
            continue
        if category in inline_categories:
            # 内容已经在上面灌进来了，不能再去应用级字典里查（类别可能不存在）
            for domain in domains:
                bucket.setdefault(domain, {})
            continue
        try:
            items = list(dict_service.export_items_for_domains(category, sorted(domains)))
        except Exception as exc:
            raise DictTableError(f"读取字典表「{category}」失败：{exc}") from exc
        for item in items:
            bucket.setdefault(item.domain, {})[item.code] = (
                item.name,
                item.parent_code,
                item.sort_order,
            )
        for domain in domains:
            bucket.setdefault(domain, {})

    for source in plan.sources:
        try:
            category, rows = _normalize_source_rows(
                source.db_path,
                source.table,
                default_category=source.category,
                default_domain=source.domain,
            )
        except DictTableError as exc:
            # 源库表结构认不出（没有代码/名称列）就跳过：不因为一张怪表
            # 把整个创建流程拦住，但日志里要留下痕迹。
            logger.warning("跳过源库字典表 %s：%s", source.table, exc)
            continue
        bucket = grouped.setdefault(category, {})
        for domain, code, name, parent, sort_order in rows:
            bucket.setdefault(domain, {})[code] = (name, parent, sort_order)

    specs: list[DictTableSpec] = []
    for category, domains in grouped.items():
        normalized_rows: list[tuple[str, str, str, str, int]] = []
        for domain, codes in domains.items():
            for code, (name, parent, sort_order) in codes.items():
                normalized_rows.append((domain, code, name, parent, sort_order))
        # 目标库里已有对应字典表时**沿用真实表名**（它可能不叫 _sql_token(category)，
        # 比如用户新建时起了别的名字），否则会平白多出一张同内容的表。
        preferred = local_names.get(category) or _sql_token(category)
        name = _choose_name(
            preferred,
            tables,
            attrs,
            used,
            lambda attr, _cat=category: attr.get("dict_table") == _cat,
        )
        attr = json.dumps(
            {
                "schema": DICT_SCHEMA_VERSION,
                "dict_table": category,
                "domains": sorted(domains),
            },
            ensure_ascii=False,
        )
        specs.append(
            DictTableSpec(
                table_name=name,
                columns=DICT_COLUMNS,
                rows=tuple(normalized_rows),
                attr=attr,
                upsert_key=("domain", "code"),
                insert_columns=DICT_INSERT_COLUMNS,
                category=category,
                domains=tuple(sorted(domains)),
            )
        )
    return specs


def source_dict_tables(layer, fields) -> list[DictSource]:
    """外部数据集源库里，和字段字典信息匹配上的 dict 表（带字段的字典表/域）。"""
    try:
        from app.core.util.local_layer_loader import layer_source_container

        path = str(layer_source_container(layer) or "")
    except Exception:  # noqa: BLE001
        return []
    if not path or not os.path.isfile(path):
        return []
    wanted_by_words: list[tuple[str, str, str]] = []  # (word, category, domain)
    for field in fields:
        category = str(getattr(field, "dict_table", "") or "").strip()
        domain = str(getattr(field, "dict_domain", "") or "").strip()
        if category:
            wanted_by_words.append((category, category, domain))
        if domain:
            wanted_by_words.append((domain, category, domain))
    if not wanted_by_words:
        return []
    try:
        conn = sqlite3.connect(str(path), timeout=5.0)
    except sqlite3.Error:
        return []
    try:
        conn.execute("PRAGMA query_only = ON")
        rows = conn.execute(
            "SELECT table_name FROM user_relation WHERE type = 'dict'"
        ).fetchall()
    except sqlite3.Error:
        return []
    finally:
        conn.close()

    sources: list[DictSource] = []
    seen: set[tuple[str, str, str, str]] = set()
    for (table,) in rows:
        name = str(table)
        folded = name.casefold()
        for word, category, domain in wanted_by_words:
            needle = word.casefold()
            if not needle or not (folded == needle or needle in folded):
                continue
            key = (path, name, category, domain)
            if key in seen:
                continue
            seen.add(key)
            sources.append(
                DictSource(
                    db_path=path,
                    table=name,
                    category=category,
                    domain=domain,
                )
            )
    return sources


def source_inline_dicts(layer, fields) -> tuple[DictInline, ...]:
    """外部数据集里**只能从数据源本身读到**的字典（GDB / GPKG 属性域）。

    :func:`source_dict_tables` 只认源库 ``user_relation`` 里登记的字典表；
    GDB / GPKG 的属性域不在里面（连 sqlite 都打不开 ``.gdb`` 目录），所以这里
    把字段挂接的字典取值直接带进创建流程 —— 否则「字典表 / 字典域」两列虽然
    显示出来了，落库时却因为应用级字典里没有这个类别而被跳过。
    """
    wanted = {
        (field.dict_table.strip(), field.dict_domain.strip())
        for field in fields
        if field.dict_table.strip() and field.dict_domain.strip()
    }
    if not wanted or layer is None:
        return ()
    try:
        from app.core.util.dict_values import layer_dict_fields

        dict_fields = layer_dict_fields(layer)
    except Exception as exc:  # noqa: BLE001 —— 读不到属性域就按没有
        logger.info("读取源数据集属性域失败：%s", exc)
        return ()
    merged: dict[tuple[str, str], dict[str, tuple[str, str, int]]] = {}
    for _name, info in dict_fields:
        key = (str(info.table or "").strip(), str(info.domain or "").strip())
        if key not in wanted:
            continue
        bucket = merged.setdefault(key, {})
        for index, value in enumerate(info.values):
            if not value.code:
                continue
            bucket.setdefault(
                str(value.code), (str(value.name), str(value.parent_code), index)
            )
    inline: list[DictInline] = []
    for (category, domain), codes in merged.items():
        if not codes:
            continue
        rows = tuple(
            (domain, code, name, parent, sort_order)
            for code, (name, parent, sort_order) in codes.items()
        )
        inline.append(DictInline(category=category, domain=domain, rows=rows))
    return tuple(inline)


def resolve_local_table(field, specs: Sequence[DictTableSpec]) -> str:
    """字段对应的目标库字典表名（找不到返回空串）。"""
    category = str(getattr(field, "dict_table", "") or "").strip()
    if not category:
        return ""
    for spec in specs:
        if spec.category == category:
            return spec.table_name
    return ""


def ensure_local_dict_domain(
    db_path: str,
    local_table: str,
    category: str,
    domain: str,
    dict_service=None,
) -> int:
    """确保本地字典表里包含指定字典域；缺就从应用级字典复制该域。返回复制条数。"""
    from app.core.util.local_db_util import local_db_util

    table = str(local_table or "").strip()
    domain = str(domain or "").strip()
    if not table or not domain:
        return 0
    db = local_db_util(db_path)
    try:
        columns = {
            str(row[1]) for row in db.cursor.execute(f"PRAGMA table_info({_quote(table)})")
        }
        if "domain" not in columns or "code" not in columns:
            return 0
        exists = db.cursor.execute(
            f"SELECT 1 FROM {_quote(table)} WHERE domain = ? LIMIT 1", (domain,)
        ).fetchone()
        if exists is not None:
            return 0
        rows: list[tuple] = []
        if dict_service is not None and category:
            items = list(dict_service.export_items_for_domains(category, [domain]))
            rows = [
                (item.domain, item.code, item.name, item.parent_code, item.sort_order)
                for item in items
            ]
        index_name = _index_token(f"ux_{table}_domain_code")
        db.cursor.execute(
            f"CREATE UNIQUE INDEX IF NOT EXISTS {_quote(index_name)} "
            f"ON {_quote(table)} (domain, code)"
        )
        if rows:
            db.cursor.executemany(
                f"INSERT INTO {_quote(table)} "
                "(domain, code, name, parent_code, sort_order) VALUES (?, ?, ?, ?, ?) "
                "ON CONFLICT(domain, code) DO NOTHING",
                rows,
            )
        # 把域补进 user_relation.attr 的 domains 清单
        with suppress(Exception):
            row = db.cursor.execute(
                "SELECT attr FROM user_relation WHERE table_name = ? AND type = 'dict'",
                (table,),
            ).fetchone()
            data = {}
            if row and row[0]:
                try:
                    data = json.loads(row[0])
                except (TypeError, ValueError):
                    data = {}
            if isinstance(data, dict):
                domains = {str(value) for value in (data.get("domains") or [])}
                domains.add(domain)
                data["domains"] = sorted(domains)
                data.setdefault("schema", DICT_SCHEMA_VERSION)
                data.setdefault("dict_table", category or table)
                db.cursor.execute(
                    "UPDATE user_relation SET attr = ? "
                    "WHERE table_name = ? AND type = 'dict'",
                    (json.dumps(data, ensure_ascii=False), table),
                )
        db.conn.commit()
        return len(rows)
    except Exception as exc:
        db.conn.rollback()
        raise DictTableError(f"更新本地字典域失败：{exc}") from exc
    finally:
        db.conn.close()


def create_local_dict_table(
    db_path: str,
    table_name: str,
    *,
    source_category: str = "",
    dict_service=None,
) -> int:
    """在本地库里新建一张 canonical 字典表（可选从应用级字典表复制内容）。

    Returns:
        写入的字典项条数（空表为 0）。
    """
    from app.core.util.local_db_util import local_db_util

    name = str(table_name or "").strip()
    if not name:
        raise DictTableError("字典表名称不能为空")
    if name.casefold() in _RESERVED_TABLES or name.lower().startswith("sqlite_"):
        raise DictTableError(f"表名「{name}」是保留名，请换一个")
    db = local_db_util(db_path)
    created = False
    try:
        exists = db.cursor.execute(
            "SELECT 1 FROM sqlite_master WHERE type IN ('table', 'view') AND name = ?",
            (name,),
        ).fetchone()
        if exists is not None:
            raise DictTableError(f"表名「{name}」已存在")
        db.create_vector_table(
            name,
            columns=list(DICT_COLUMNS),
            geometry_column=None,
            register=False,
            overwrite=False,
        )
        created = True
        rows: list[tuple] = []
        domains: list[str] = []
        # **本地字典表的唯一名字就是库内表名**：以前这里记的是复制来源的
        # 系统字典类别，于是「字典表」和「字典表12」两个名字同时出现在树上 /
        # 挂接框里，重命名后字段上的名字也不跟着变。来源另存一个字段留痕。
        attr_category = name
        source = str(source_category or "").strip()
        if source and dict_service is not None:
            items = list(dict_service.export_items(source))
            rows = [
                (item.domain, item.code, item.name, item.parent_code, item.sort_order)
                for item in items
            ]
            domains = sorted({item.domain for item in items})
            with suppress(Exception):
                domains = sorted(
                    set(domains)
                    | {
                        domain.domain
                        for domain in dict_service.list_domains(source)
                    }
                )
        index_name = _index_token(f"ux_{name}_domain_code")
        db.cursor.execute(
            f"CREATE UNIQUE INDEX IF NOT EXISTS {_quote(index_name)} "
            f"ON {_quote(name)} (domain, code)"
        )
        # 数据管理里展开字典域时按 (domain, parent_code) 查子项，并对每行做
        # EXISTS 子查询。没有这个索引，2 万条字典展开一次要十几秒（实测 15.6s
        # → 0.03s）。建表时顺手建好，老库在打开字典表详情时再补（见
        # table_detail.DictTableEditor._ensure_lookup_index）。
        parent_index = _index_token(f"ux_{name}_domain_parent")
        db.cursor.execute(
            f"CREATE INDEX IF NOT EXISTS {_quote(parent_index)} "
            f"ON {_quote(name)} (domain, parent_code)"
        )
        if rows:
            db.cursor.executemany(
                f"INSERT INTO {_quote(name)} "
                "(domain, code, name, parent_code, sort_order) VALUES (?, ?, ?, ?, ?)",
                rows,
            )
        db.cursor.execute(
            "DELETE FROM user_relation WHERE table_name = ?", (name,)
        )
        attr = {
            "schema": DICT_SCHEMA_VERSION,
            "dict_table": attr_category,
            "domains": domains,
        }
        if source and source != name:
            attr["source_category"] = source
        db.add_domain_table(
            name,
            "dict",
            json.dumps(attr, ensure_ascii=False),
        )
        db.conn.commit()
        return len(rows)
    except DictTableError:
        db.conn.rollback()
        if created:
            with suppress(Exception):
                db.cursor.execute(f"DROP TABLE IF EXISTS {_quote(name)}")
            with suppress(Exception):
                db.cursor.execute(
                    "DELETE FROM user_relation WHERE table_name = ?", (name,)
                )
            with suppress(Exception):
                db.conn.commit()
        raise
    except Exception as exc:
        db.conn.rollback()
        if created:
            with suppress(Exception):
                db.cursor.execute(f"DROP TABLE IF EXISTS {_quote(name)}")
            with suppress(Exception):
                db.cursor.execute(
                    "DELETE FROM user_relation WHERE table_name = ?", (name,)
                )
            with suppress(Exception):
                db.conn.commit()
        raise DictTableError(f"创建字典表失败：{exc}") from exc
    finally:
        db.conn.close()


# --------------------------------------------------------------------- 写入


def persist_dict_tables(db, specs: Sequence[DictTableSpec]) -> list[str]:
    """把 specs 写进已打开的 ``local_db_util``；返回实际写入的表名。

    已存在的同名字典表**只补缺不覆盖**（``ON CONFLICT DO NOTHING``），避免把
    用户在目标库里手工改过的字典行冲掉。失败时补偿删除本次新建的表和登记行。
    """
    if not specs:
        return []
    written: list[str] = []
    created: list[str] = []
    touched: list[DictTableSpec] = []
    existing: list[DictTableSpec] = []
    try:
        for spec in specs:
            touched.append(spec)
            exists = _table_exists(db, spec.table_name)
            if not exists:
                db.create_vector_table(
                    spec.table_name,
                    columns=list(spec.columns),
                    geometry_column=None,
                    register=False,
                    overwrite=False,
                )
                created.append(spec.table_name)
            else:
                existing.append(spec)

            # 目标库可能还没有 user_relation（例如刚由 OGR 导出的 GPKG /
            # SQLite）：DELETE 先失败也没关系，紧接着的 add_domain_table 会
            # 幂等建表再登记。
            with suppress(Exception):
                db.cursor.execute(
                    "DELETE FROM user_relation WHERE table_name = ?",
                    (spec.table_name,),
                )
            db.add_domain_table(spec.table_name, "dict", spec.attr or None)

            # 数据管理展开字典域靠 (domain, parent_code) 索引（见
            # create_local_dict_table 的说明）；老库/已存在的表也在这里补上。
            column_names = {name for name, _declared in spec.columns}
            if {"domain", "parent_code"} <= column_names:
                parent_index = _index_token(f"ux_{spec.table_name}_domain_parent")
                db.cursor.execute(
                    f"CREATE INDEX IF NOT EXISTS {_quote(parent_index)} "
                    f"ON {_quote(spec.table_name)} (domain, parent_code)"
                )

            insert_columns = spec.insert_columns or tuple(
                name
                for name, declared in spec.columns
                if "AUTOINCREMENT" not in declared.upper()
            )
            if not insert_columns:
                written.append(spec.table_name)
                continue
            cols_sql = ", ".join(_quote(name) for name in insert_columns)
            marks = ", ".join("?" for _ in insert_columns)
            if spec.upsert_key:
                keys = ", ".join(_quote(name) for name in spec.upsert_key)
                index_name = _index_token(
                    f"ux_{spec.table_name}_{'_'.join(spec.upsert_key)}"
                )
                db.cursor.execute(
                    f"CREATE UNIQUE INDEX IF NOT EXISTS {_quote(index_name)} "
                    f"ON {_quote(spec.table_name)} ({keys})"
                )
                sql = (
                    f"INSERT INTO {_quote(spec.table_name)} ({cols_sql}) "
                    f"VALUES ({marks}) ON CONFLICT({keys}) DO NOTHING"
                )
            else:
                sql = (
                    f"INSERT INTO {_quote(spec.table_name)} ({cols_sql}) VALUES ({marks})"
                )
            if spec.rows:
                db.cursor.executemany(sql, [tuple(row) for row in spec.rows])
            written.append(spec.table_name)
        db.conn.commit()
    except Exception as exc:
        with suppress(Exception):
            db.conn.rollback()
        for name in created:
            with suppress(Exception):
                db.cursor.execute(f"DROP TABLE IF EXISTS {_quote(name)}")
        for spec in touched:
            with suppress(Exception):
                db.cursor.execute(
                    "DELETE FROM user_relation WHERE table_name = ?",
                    (spec.table_name,),
                )
        # 已存在的表不删，只把登记恢复回去（它的数据由 rollback 保住）
        for spec in existing:
            with suppress(Exception):
                db.add_domain_table(spec.table_name, "dict", spec.attr or None)
        with suppress(Exception):
            db.conn.commit()
        raise DictTableError(f"写入字典表失败：{exc}") from exc

    logger.info("已把 %d 张字典表写入 %s：%s", len(written), db.db_path, written)
    return written


def _table_exists(db, table: str) -> bool:
    row = db.cursor.execute(
        "SELECT 1 FROM sqlite_master WHERE type IN ('table', 'view') AND name = ?",
        (table,),
    ).fetchone()
    return row is not None
