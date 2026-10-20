"""本地库表结构 / 行编辑（Qt-free）。

供给「数据管理」右侧详情面板：

- 矢量表：读字段结构（类型 / 长度 / 可空 / 别名 / 字典元数据），支持加列、改名、
  删列；类型 / 长度 / 小数位 / 可空变化时**重建表并保留数据**；
- 字典表：分页读行、按 rowid/主键增删改。

实现要点：

- 所有写操作走 ``local_db_util`` 的连接并显式 ``BEGIN IMMEDIATE`` / ``commit``；
- 重建表沿用原 ``CREATE TABLE SQL``，只替换目标列的类型 / NOT NULL，
  保留 DEFAULT / CHECK / 引用等列级约束；表级约束原样保留；
- 复制数据时保留 ``fid`` / ``rowid``（空间索引按 rowid 建，换了 rowid 会让索引失效）；
- 旧表删除时 SQLite 会一并删掉它的索引 / 触发器，重建后按原 SQL 恢复
  （GPKG rtree / SpatiaLite 空间索引的触发器都在里面；虚拟索引表本身不随表删除，
  行键又保持不变，所以索引数据仍然有效）；
- 类型转换先做预检（NULL / 非数字文本等），不静默丢掉或改坏数据。
"""

from __future__ import annotations

import json
import os
import re
from contextlib import suppress
from dataclasses import dataclass
from typing import Any

from app.core.util.field_meta import (
    FieldMeta,
    delete_field_meta,
    read_field_meta,
    rename_field_meta,
)
from app.core.util.local_db_util import local_db_util, table_geometry_column
from app.infra.logging import get_logger

logger = get_logger(__name__)

#: 允许写进 DDL 的类型（防止把界面输入拼进 SQL）
_TYPE_RE = re.compile(
    r"^(TEXT(\(\d+\))?|INTEGER|INT|REAL|DOUBLE|FLOAT|DATE|DATETIME|BOOLEAN|NUMERIC)$",
    re.IGNORECASE,
)

#: 约束关键字（解析列定义时用来区分类型与约束）
_CONSTRAINT_WORDS = frozenset(
    {
        "NOT",
        "NULL",
        "DEFAULT",
        "PRIMARY",
        "UNIQUE",
        "CHECK",
        "REFERENCES",
        "COLLATE",
        "GENERATED",
        "CONSTRAINT",
    }
)


class LocalTableError(RuntimeError):
    """本地表编辑失败。消息可直接展示。"""


@dataclass(frozen=True)
class TableColumn:
    name: str
    declared_type: str
    notnull: bool = False
    default: str | None = None
    pk: bool = False
    hidden: bool = False


@dataclass(frozen=True)
class TableInfo:
    db_path: str
    table: str
    kind: str
    columns: tuple[TableColumn, ...]
    geometry_column: str = ""
    aliases: dict[str, str] | None = None
    field_meta: dict[str, FieldMeta] | None = None
    row_count: int = 0
    pk_columns: tuple[str, ...] = ()
    without_rowid: bool = False
    attr: dict[str, Any] | None = None

    @property
    def uses_rowid(self) -> bool:
        """没有 INTEGER 主键、又是 rowid 表 → 行键用 rowid。"""
        return not self.without_rowid and not any(
            col.pk and "INT" in col.declared_type.upper() for col in self.columns
        )

    @property
    def editable_columns(self) -> tuple[TableColumn, ...]:
        """字段编辑器显示的列：业务列；几何列与自动主键隐藏。"""
        out = []
        for col in self.columns:
            if col.hidden:
                continue
            if self.geometry_column and col.name == self.geometry_column:
                continue
            if col.pk and not self.without_rowid and "AUTOINCREMENT" in (
                self._create_sql or ""
            ).upper():
                continue
            out.append(col)
        return tuple(out)

    #: ``read_table_info`` 里补的原始 CREATE SQL（editable_columns 判断自增用）
    _create_sql: str = ""


@dataclass(frozen=True)
class RowKey:
    columns: tuple[str, ...]
    uses_rowid: bool = False


@dataclass(frozen=True)
class TableRows:
    columns: tuple[str, ...]
    rows: tuple[dict[str, Any], ...]
    total: int
    key: RowKey


# --------------------------------------------------------------------- 基础


def _quote(name: str) -> str:
    return '"' + str(name).replace('"', '""') + '"'


def _open(db_path: str):
    if not db_path or not os.path.isfile(db_path):
        raise LocalTableError(f"数据库不存在：{db_path}")
    try:
        return local_db_util(db_path)
    except Exception as exc:
        raise LocalTableError(f"打不开数据库：{db_path}（{exc}）") from exc


def _validate_name(name: str) -> str:
    text = str(name or "").strip()
    if not text:
        raise LocalTableError("字段名不能为空")
    if text[0].isdigit():
        raise LocalTableError("字段名不能以数字开头")
    if re.search(r"[^\w]", text):
        raise LocalTableError("字段名只能包含字母、数字、下划线或中文")
    return text


def _validate_type(declared: str) -> str:
    text = str(declared or "").strip()
    if not _TYPE_RE.match(text):
        raise LocalTableError(f"不支持的字段类型声明：{declared!r}")
    return text


def _affinity(declared: str) -> str:
    upper = declared.upper()
    if "INT" in upper:
        return "INTEGER"
    if any(word in upper for word in ("REAL", "FLOA", "DOUB")):
        return "REAL"
    if any(word in upper for word in ("DATE", "TIME", "CHAR", "CLOB", "TEXT")):
        return "TEXT"
    if "BOOL" in upper:
        return "NUMERIC"
    return "TEXT"


def _split_top_level(body: str) -> list[str]:
    """按顶层逗号切 ``CREATE TABLE`` 的列/约束定义（忽略括号和引号里的逗号）。"""
    parts: list[str] = []
    buf: list[str] = []
    depth = 0
    quote = ""
    index = 0
    while index < len(body):
        char = body[index]
        if quote:
            buf.append(char)
            if char == quote:
                if index + 1 < len(body) and body[index + 1] == quote:
                    buf.append(body[index + 1])
                    index += 1
                else:
                    quote = ""
        else:
            if char in ('"', "'", "`"):
                quote = char
                buf.append(char)
            elif char == "[":
                quote = "]"
                buf.append(char)
            elif char == "(":
                depth += 1
                buf.append(char)
            elif char == ")":
                depth -= 1
                buf.append(char)
            elif char == "," and depth == 0:
                parts.append("".join(buf).strip())
                buf = []
            else:
                buf.append(char)
        index += 1
    if buf:
        parts.append("".join(buf).strip())
    return [part for part in parts if part]


def _split_body(sql: str) -> tuple[str, str]:
    """拆出 ``CREATE TABLE`` 括号内的定义与括号后的选项（WITHOUT ROWID / STRICT）。"""
    start = sql.find("(")
    if start < 0:
        raise LocalTableError("读不出建表 SQL")
    depth = 0
    quote = ""
    index = start
    while index < len(sql):
        char = sql[index]
        if quote:
            if char == quote:
                if index + 1 < len(sql) and sql[index + 1] == quote:
                    index += 1
                else:
                    quote = ""
        else:
            if char in ('"', "'", "`"):
                quote = char
            elif char == "[":
                quote = "]"
            elif char == "(":
                depth += 1
            elif char == ")":
                depth -= 1
                if depth == 0:
                    return sql[start + 1 : index], sql[index + 1 :]
        index += 1
    raise LocalTableError("建表 SQL 的括号不完整")


def _segment_name(segment: str) -> tuple[str, str]:
    """列定义 / 表约束 → ``(名字, 剩余部分)``；表约束返回 ``("", segment)``。"""
    text = segment.lstrip()
    upper = text.upper()
    if upper.startswith(("CONSTRAINT", "PRIMARY", "UNIQUE", "CHECK", "FOREIGN")):
        return "", segment
    if text.startswith('"'):
        index = 1
        name = []
        while index < len(text):
            if text[index] == '"':
                if index + 1 < len(text) and text[index + 1] == '"':
                    name.append('"')
                    index += 2
                    continue
                return "".join(name), text[index + 1 :]
            name.append(text[index])
            index += 1
        return "", segment
    if text.startswith("["):
        end = text.find("]")
        if end > 0:
            return text[1:end], text[end + 1 :]
    match = re.match(r"([^\s]+)(.*)$", text, re.S)
    if match:
        return match.group(1), match.group(2)
    return "", segment


def _replace_column_def(segment: str, new_type: str, notnull: bool) -> str:
    """保留列名与约束，只替换类型和 NOT NULL。"""
    raw_name, rest = _segment_name(segment)
    if not raw_name:
        return segment
    name_token = segment.lstrip()[: len(segment.lstrip()) - len(rest)]
    tokens = rest.split()
    type_parts: list[str] = []
    index = 0
    while index < len(tokens) and tokens[index].upper() not in _CONSTRAINT_WORDS:
        type_parts.append(tokens[index])
        index += 1
    tail_tokens = tokens[index:]
    cleaned: list[str] = []
    skip = False
    for pos, token in enumerate(tail_tokens):
        if skip:
            skip = False
            continue
        if token.upper() == "NOT" and pos + 1 < len(tail_tokens) and (
            tail_tokens[pos + 1].upper() == "NULL"
        ):
            skip = True
            continue
        cleaned.append(token)
    tail = " ".join(cleaned).strip()
    result = f"{name_token} {new_type}"
    if tail:
        result += f" {tail}"
    if notnull:
        result += " NOT NULL"
    return result


def _cast_expr(column: str, target_type: str) -> str:
    quoted = _quote(column)
    affinity = _affinity(target_type)
    if affinity == "TEXT":
        return f"CAST({quoted} AS TEXT)"
    # 数字 / 日期 / 布尔：空串按 NULL 处理，别让 "" 变成 0
    return f"CAST(NULLIF(TRIM({quoted}), '') AS {affinity})"


def _valid_value(value: Any, target_type: str) -> bool:
    if value is None:
        return True
    affinity = _affinity(target_type)
    if affinity == "TEXT":
        return True
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return True
        if "DATE" in target_type.upper() or "TIME" in target_type.upper():
            return True
        if "BOOL" in target_type.upper():
            return text.casefold() in {"0", "1", "true", "false", "yes", "no", "是", "否"}
        if affinity == "INTEGER":
            return bool(re.fullmatch(r"-?\d+", text))
        try:
            float(text)
            return True
        except ValueError:
            return False
    if "BOOL" in target_type.upper():
        return value in (0, 1, True, False)
    if affinity == "INTEGER":
        return isinstance(value, int) or (
            isinstance(value, float) and value.is_integer()
        )
    return isinstance(value, (int, float))


# --------------------------------------------------------------------- 读表


def read_table_info(db_path: str, table: str) -> TableInfo:
    """读一张表的字段结构、别名、字典元数据与行数。"""
    db = _open(db_path)
    try:
        info = db.cursor.execute(f"PRAGMA table_xinfo({_quote(table)})").fetchall()
        if not info:
            raise LocalTableError(f"表不存在：{table}")
        columns = tuple(
            TableColumn(
                name=str(row[1]),
                declared_type=str(row[2] or ""),
                notnull=bool(row[3]),
                default=row[4],
                pk=bool(row[5]),
                hidden=bool(row[6]) if len(row) > 6 else False,
            )
            for row in info
        )
        create_row = db.cursor.execute(
            "SELECT sql FROM sqlite_master WHERE type = 'table' AND name = ?", (table,)
        ).fetchone()
        create_sql = str(create_row[0] or "") if create_row else ""
        row = db.cursor.execute(
            f"SELECT COUNT(*) FROM {_quote(table)}"
        ).fetchone()
        row_count = int(row[0]) if row else 0
        geometry = ""
        with suppress(Exception):
            geometry = str(table_geometry_column(db_path, table) or "")
        aliases: dict[str, str] = {}
        with suppress(Exception):
            aliases.update(db.column_aliases(table))
        if db.kind == "gpkg":
            with suppress(Exception):
                for name, column in db.cursor.execute(
                    "SELECT name, column_name FROM gpkg_data_columns "
                    "WHERE table_name = ?",
                    (table,),
                ):
                    if name and column:
                        aliases[str(column)] = str(name)
        # GPKG 标准位置 / 本工程自己的别名表都可能是「UTF-8 被当成
        # Latin-1」的乱码（从 GDB 导入的中文别名）。读出来就修，字段编辑器 /
        # 详情页、重新保存时都不会再把乱码写回去。
        from app.core.util.text_encoding import repair_mojibake

        aliases = {
            str(name): repair_mojibake(alias)
            for name, alias in aliases.items()
        }
        meta = read_field_meta(db.conn, table)
        attr: dict[str, Any] = {}
        with suppress(Exception):
            raw = db.cursor.execute(
                "SELECT attr FROM user_relation WHERE table_name = ?", (table,)
            ).fetchone()
            if raw and raw[0]:
                value = json.loads(raw[0])
                if isinstance(value, dict):
                    attr = value
        pk_columns = tuple(col.name for col in columns if col.pk)
        return TableInfo(
            db_path=str(db_path),
            table=table,
            kind=db.kind,
            columns=columns,
            geometry_column=geometry,
            aliases=aliases,
            field_meta=meta,
            row_count=row_count,
            pk_columns=pk_columns,
            without_rowid="WITHOUT ROWID" in create_sql.upper(),
            attr=attr,
            _create_sql=create_sql,
        )
    finally:
        db.conn.close()


# --------------------------------------------------------------------- DDL


def add_column(
    db_path: str,
    table: str,
    name: str,
    declared_type: str,
    *,
    notnull: bool = False,
    default: str | None = None,
) -> None:
    """加一列；NOT NULL 必须给默认值（SQLite 的 ALTER 限制）。"""
    field = _validate_name(name)
    declared = _validate_type(declared_type)
    if notnull and default is None:
        raise LocalTableError(
            "新增「不可为空」字段必须给默认值（SQLite 加列限制）；"
            "也可以先建成可空字段"
        )
    default_sql = ""
    if default is not None:
        text = str(default)
        if _affinity(declared) in ("INTEGER", "REAL", "NUMERIC") and re.fullmatch(
            r"-?\d+(\.\d+)?", text.strip()
        ):
            default_sql = f" DEFAULT {text.strip()}"
        else:
            default_sql = " DEFAULT '" + text.replace("'", "''") + "'"
    db = _open(db_path)
    try:
        try:
            db.cursor.execute(
                f"ALTER TABLE {_quote(table)} ADD COLUMN {_quote(field)} "
                f"{declared}{' NOT NULL' if notnull else ''}{default_sql}"
            )
            db.conn.commit()
        except Exception as exc:
            db.conn.rollback()
            raise LocalTableError(f"加字段失败：{exc}") from exc
    finally:
        db.conn.close()


def rename_column(db_path: str, table: str, old: str, new: str) -> None:
    """改字段名并同步别名 / 字典元数据；禁止改几何列与主键。"""
    old_name, new_name = str(old), _validate_name(new)
    info = read_table_info(db_path, table)
    if old_name == info.geometry_column:
        raise LocalTableError("几何列不能在这里改名")
    if any(col.name == old_name and col.pk for col in info.columns):
        raise LocalTableError("主键列不能在这里改名")
    if any(col.name == new_name for col in info.columns):
        raise LocalTableError(f"字段「{new_name}」已存在")
    db = _open(db_path)
    try:
        try:
            db.cursor.execute(
                f"ALTER TABLE {_quote(table)} RENAME COLUMN "
                f"{_quote(old_name)} TO {_quote(new_name)}"
            )
            with suppress(Exception):
                db.cursor.execute(
                    "UPDATE gpkg_data_columns SET column_name = ? "
                    "WHERE table_name = ? AND column_name = ?",
                    (new_name, table, old_name),
                )
            with suppress(Exception):
                db.cursor.execute(
                    "UPDATE qgis_dev_column_alias SET column_name = ? "
                    "WHERE table_name = ? AND column_name = ?",
                    (new_name, table, old_name),
                )
            rename_field_meta(db.conn, table, old_name, new_name)
            db.conn.commit()
        except Exception as exc:
            db.conn.rollback()
            raise LocalTableError(f"改字段名失败：{exc}") from exc
    finally:
        db.conn.close()


def rename_local_table(db_path: str, old: str, new: str) -> None:
    """重命名库内表，并同步所有目录 / 元数据表里的表名。

    SQLite 的 ``ALTER TABLE ... RENAME`` 会改索引 / 触发器 / 视图里的引用；
    但 ``user_relation``、GPKG 元数据、SpatiaLite geometry_columns、本工程的
    别名 / 字段元数据表都要手动跟着改，不然数据管理 / 图层打开就对不上了。
    """
    old_name, new_name = str(old), str(new).strip()
    if not new_name:
        raise LocalTableError("表名不能为空")
    if new_name == old_name:
        return
    info = read_table_info(db_path, old_name)
    if info.table != old_name:
        raise LocalTableError(f"表不存在：{old_name}")
    db = _open(db_path)
    try:
        exists = db.cursor.execute(
            "SELECT 1 FROM sqlite_master WHERE type IN ('table', 'view') AND name = ?",
            (new_name,),
        ).fetchone()
        if exists is not None:
            raise LocalTableError(f"表名「{new_name}」已存在")
        # GPKG 的 gpkg_geometry_columns / gpkg_data_columns 对 gpkg_contents 有外键：
        # 必须先关外键再改元数据（先改子表再改父表也救不了中间态），改完再打开。
        with suppress(Exception):
            db.conn.execute("PRAGMA foreign_keys = OFF")
        try:
            db.cursor.execute(
                f"ALTER TABLE {_quote(old_name)} RENAME TO {_quote(new_name)}"
            )
            # 目录 / 元数据里的表名（子表在前，gpkg_contents 最后）
            for sql in (
                "UPDATE user_relation SET table_name = ? WHERE table_name = ?",
                "UPDATE gpkg_geometry_columns SET table_name = ? WHERE table_name = ?",
                "UPDATE gpkg_data_columns SET table_name = ? WHERE table_name = ?",
                "UPDATE gpkg_extensions SET table_name = ? WHERE table_name = ?",
                "UPDATE gpkg_contents SET table_name = ? WHERE table_name = ?",
                "UPDATE geometry_columns SET f_table_name = ? WHERE f_table_name = ?",
                "UPDATE qgis_dev_column_alias SET table_name = ? WHERE table_name = ?",
                "UPDATE qgis_dev_field_meta SET table_name = ? WHERE table_name = ?",
                "UPDATE qgis_dev_field_meta SET dict_local_table = ? WHERE dict_local_table = ?",
                "UPDATE sqlite_sequence SET name = ? WHERE name = ?",
            ):
                with suppress(Exception):
                    db.cursor.execute(sql, (new_name, old_name))
            # 本地字典表的"名字"就是表名：重命名后，挂在它上面的字段元数据
            # 和 user_relation.attr 里的逻辑名都要跟着改，否则字段列表/挂接框
            # 还显示旧名字。
            with suppress(Exception):
                row = db.cursor.execute(
                    "SELECT attr FROM user_relation "
                    "WHERE table_name = ? AND type = 'dict'",
                    (new_name,),
                ).fetchone()
                if row is not None:
                    try:
                        attr = json.loads(row[0]) if row[0] else {}
                    except (TypeError, ValueError):
                        attr = {}
                    if isinstance(attr, dict):
                        attr["dict_table"] = new_name
                        attr.pop("category", None)
                        db.cursor.execute(
                            "UPDATE user_relation SET attr = ? "
                            "WHERE table_name = ? AND type = 'dict'",
                            (json.dumps(attr, ensure_ascii=False), new_name),
                        )
                    db.cursor.execute(
                        'UPDATE "qgis_dev_field_meta" SET dict_table = ? '
                        "WHERE dict_local_table = ?",
                        (new_name, new_name),
                    )
            db.conn.commit()
        except LocalTableError:
            raise
        except Exception as exc:
            db.conn.rollback()
            raise LocalTableError(f"重命名表失败：{exc}") from exc
        finally:
            with suppress(Exception):
                db.conn.execute("PRAGMA foreign_keys = ON")
    finally:
        db.conn.close()
    logger.info("表已重命名：%s → %s", old_name, new_name)


def drop_column(db_path: str, table: str, name: str) -> None:
    """删字段（禁止几何列 / 主键），并清理别名与字典元数据。"""
    column = str(name)
    info = read_table_info(db_path, table)
    if column == info.geometry_column:
        raise LocalTableError("几何列不能删除")
    if any(col.name == column and col.pk for col in info.columns):
        raise LocalTableError("主键列不能删除")
    if len([col for col in info.columns if not col.hidden]) <= 1:
        raise LocalTableError("至少保留一个字段")
    db = _open(db_path)
    try:
        try:
            db.cursor.execute(
                f"ALTER TABLE {_quote(table)} DROP COLUMN {_quote(column)}"
            )
            with suppress(Exception):
                db.cursor.execute(
                    "DELETE FROM gpkg_data_columns "
                    "WHERE table_name = ? AND column_name = ?",
                    (table, column),
                )
            with suppress(Exception):
                db.cursor.execute(
                    "DELETE FROM qgis_dev_column_alias "
                    "WHERE table_name = ? AND column_name = ?",
                    (table, column),
                )
            delete_field_meta(db.conn, table, column)
            db.conn.commit()
        except Exception as exc:
            db.conn.rollback()
            raise LocalTableError(
                f"删除字段失败（可能被索引 / 视图 / 触发器引用）：{exc}"
            ) from exc
    finally:
        db.conn.close()


def rebuild_for_types(
    db_path: str,
    table: str,
    changes: dict[str, tuple[str, bool]],
) -> None:
    """类型 / 长度 / 小数位 / 可空变化：重建表并保留数据。

    ``changes`` = ``{列名: (新的类型声明, 是否 NOT NULL)}``。只改业务列；
    几何列 / 主键列由调用方排除。先预检 NULL 与不可转换的值，失败不动原表。
    """
    info = read_table_info(db_path, table)
    for column, (declared, _notnull) in changes.items():
        _validate_type(declared)
        if column == info.geometry_column:
            raise LocalTableError("几何列不能修改类型")
        if any(col.name == column and col.pk for col in info.columns):
            raise LocalTableError("主键列不能修改类型")
        if not any(col.name == column for col in info.columns):
            raise LocalTableError(f"字段不存在：{column}")

    db = _open(db_path)
    try:
        _preflight_changes(db, table, info, changes)
        create_row = db.cursor.execute(
            "SELECT sql FROM sqlite_master WHERE type = 'table' AND name = ?",
            (table,),
        ).fetchone()
        if create_row is None or not create_row[0]:
            raise LocalTableError(f"读不出「{table}」的建表 SQL")
        body, suffix = _split_body(str(create_row[0]))
        segments = _split_top_level(body)
        wanted = {name.casefold(): (typed, notnull) for name, (typed, notnull) in changes.items()}
        new_segments: list[str] = []
        for segment in segments:
            name, _rest = _segment_name(segment)
            change = wanted.get(name.casefold()) if name else None
            if change is not None:
                new_segments.append(_replace_column_def(segment, change[0], change[1]))
            else:
                new_segments.append(segment)
        # 依赖对象（索引 / 触发器；GPKG rtree 与 SpatiaLite 空间索引的触发器都在这里）
        dependents = [
            str(row[0])
            for row in db.cursor.execute(
                "SELECT sql FROM sqlite_master WHERE tbl_name = ? "
                "AND type IN ('index', 'trigger') AND sql IS NOT NULL",
                (table,),
            )
        ]
        tmp = f"__tmp_{table}"
        tmp_sql = f"CREATE TABLE {_quote(tmp)} (" + ", ".join(new_segments) + ")" + suffix
        insert_columns = [col.name for col in info.columns]
        select_exprs = []
        for col in info.columns:
            change = wanted.get(col.name.casefold())
            select_exprs.append(
                _cast_expr(col.name, change[0]) if change else _quote(col.name)
            )
        if info.uses_rowid:
            insert_columns = ["rowid", *insert_columns]
            select_exprs = ["rowid", *select_exprs]

        db.conn.execute("PRAGMA foreign_keys = OFF")
        try:
            db.cursor.execute("BEGIN IMMEDIATE")
            db.cursor.execute(f"DROP TABLE IF EXISTS {_quote(tmp)}")
            db.cursor.execute(tmp_sql)
            db.cursor.execute(
                f"INSERT INTO {_quote(tmp)} "
                f"({', '.join(_quote(name) for name in insert_columns)}) "
                f"SELECT {', '.join(select_exprs)} FROM {_quote(table)}"
            )
            db.cursor.execute(f"DROP TABLE {_quote(table)}")
            db.cursor.execute(f"ALTER TABLE {_quote(tmp)} RENAME TO {_quote(table)}")
            # AUTOINCREMENT 的 sqlite_sequence 跟着改名/显式 id 复制校正一下
            auto_pk = next(
                (
                    col.name
                    for col in info.columns
                    if col.pk and "INT" in col.declared_type.upper()
                ),
                "",
            )
            if auto_pk and "AUTOINCREMENT" in str(create_row[0]).upper():
                with suppress(Exception):
                    db.cursor.execute(
                        "DELETE FROM sqlite_sequence WHERE name = ?", (tmp,)
                    )
                with suppress(Exception):
                    db.cursor.execute(
                        "DELETE FROM sqlite_sequence WHERE name = ?", (table,)
                    )
                with suppress(Exception):
                    max_row = db.cursor.execute(
                        f"SELECT MAX({_quote(auto_pk)}) FROM {_quote(table)}"
                    ).fetchone()
                    max_id = int(max_row[0] or 0) if max_row else 0
                    db.cursor.execute(
                        "INSERT INTO sqlite_sequence (name, seq) VALUES (?, ?)",
                        (table, max_id),
                    )
            for dep_sql in dependents:
                try:
                    db.cursor.execute(dep_sql)
                except Exception as exc:  # noqa: BLE001
                    logger.warning("重建后恢复索引/触发器失败：%s（%s）", dep_sql, exc)
            db.conn.commit()
        except Exception:
            db.conn.rollback()
            raise
        finally:
            with suppress(Exception):
                db.conn.execute("PRAGMA foreign_keys = ON")
        _check_integrity(db, table)
    except LocalTableError:
        raise
    except Exception as exc:
        raise LocalTableError(f"重建表失败（原表未改动）：{exc}") from exc
    finally:
        db.conn.close()


def _preflight_changes(db, table: str, info: TableInfo, changes) -> None:
    """NULL / 类型转换预检：不合格直接拦下，不动原表。"""
    for column, (declared, notnull) in changes.items():
        quoted = _quote(column)
        if notnull:
            nulls = db.cursor.execute(
                f"SELECT COUNT(*) FROM {_quote(table)} WHERE {quoted} IS NULL"
            ).fetchone()[0]
            if int(nulls):
                raise LocalTableError(
                    f"字段「{column}」有 {nulls} 行为空，不能改成「不可为空」"
                )
        bad: list[str] = []
        for (value,) in db.cursor.execute(
            f"SELECT {quoted} FROM {_quote(table)} WHERE {quoted} IS NOT NULL"
        ):
            if not _valid_value(value, declared):
                if len(bad) < 3:
                    bad.append(str(value))
                else:
                    break
        if bad:
            raise LocalTableError(
                f"字段「{column}」有值无法转换为 {declared}："
                + "、".join(bad)
                + "（请先修正数据）"
            )


def _check_integrity(db, table: str) -> None:
    with suppress(Exception):
        result = db.cursor.execute("PRAGMA quick_check").fetchone()
        if result and str(result[0]).lower() != "ok":
            raise LocalTableError(f"重建后完整性检查失败：{result[0]}")


# --------------------------------------------------------------------- 行


def row_key(info: TableInfo) -> RowKey:
    if info.uses_rowid:
        return RowKey(("__rowid",), uses_rowid=True)
    if not info.pk_columns:
        raise LocalTableError("这张表没有主键也没有 rowid，无法编辑")
    return RowKey(tuple(info.pk_columns), uses_rowid=False)


def read_rows(
    db_path: str, table: str, *, offset: int = 0, limit: int = 500
) -> TableRows:
    """分页读行；rowid 表额外带 ``__rowid`` 列作为行键。"""
    info = read_table_info(db_path, table)
    key = row_key(info)
    db = _open(db_path)
    try:
        if key.uses_rowid:
            select = "rowid AS __rowid, *"
            order = "rowid"
        else:
            select = "*"
            order = ", ".join(_quote(name) for name in key.columns)
        rows = db.cursor.execute(
            f"SELECT {select} FROM {_quote(table)} "
            f"ORDER BY {order} LIMIT ? OFFSET ?",
            (max(1, int(limit)), max(0, int(offset))),
        ).fetchall()
        columns = tuple(description[0] for description in db.cursor.description or ())
        data = tuple(dict(row) for row in rows)
        total = info.row_count
        return TableRows(columns=columns, rows=data, total=total, key=key)
    finally:
        db.conn.close()


def _where_for_key(key: RowKey, row: dict[str, Any]) -> tuple[str, list[Any]]:
    clauses = []
    params: list[Any] = []
    for column in key.columns:
        clauses.append(f"{_quote(column)} = ?")
        params.append(row.get(column))
    return " AND ".join(clauses), params


def update_row(
    db_path: str, table: str, row: dict[str, Any], values: dict[str, Any]
) -> None:
    """按行键更新若干列。"""
    info = read_table_info(db_path, table)
    key = row_key(info)
    sets = [f"{_quote(name)} = ?" for name in values]
    if not sets:
        return
    where, params = _where_for_key(key, row)
    db = _open(db_path)
    try:
        try:
            db.cursor.execute(
                f"UPDATE {_quote(table)} SET {', '.join(sets)} WHERE {where}",
                [*values.values(), *params],
            )
            db.conn.commit()
        except Exception as exc:
            db.conn.rollback()
            raise LocalTableError(f"更新字典行失败：{exc}") from exc
    finally:
        db.conn.close()


def insert_row(db_path: str, table: str, values: dict[str, Any]) -> None:
    """插入一行（跳过值为 ``None`` 且列可空的列交给默认值）。"""
    if not values:
        raise LocalTableError("没有可写入的列")
    columns = list(values)
    db = _open(db_path)
    try:
        try:
            db.cursor.execute(
                f"INSERT INTO {_quote(table)} "
                f"({', '.join(_quote(name) for name in columns)}) "
                f"VALUES ({', '.join('?' for _ in columns)})",
                [values[name] for name in columns],
            )
            db.conn.commit()
        except Exception as exc:
            db.conn.rollback()
            raise LocalTableError(f"新增字典行失败：{exc}") from exc
    finally:
        db.conn.close()


def delete_rows(db_path: str, table: str, rows: list[dict[str, Any]]) -> int:
    """按行键删除多行；返回删除条数。"""
    if not rows:
        return 0
    info = read_table_info(db_path, table)
    key = row_key(info)
    db = _open(db_path)
    deleted = 0
    try:
        try:
            for row in rows:
                where, params = _where_for_key(key, row)
                cursor = db.cursor.execute(
                    f"DELETE FROM {_quote(table)} WHERE {where}", params
                )
                deleted += max(0, cursor.rowcount)
            db.conn.commit()
        except Exception as exc:
            db.conn.rollback()
            raise LocalTableError(f"删除字典行失败：{exc}") from exc
    finally:
        db.conn.close()
    return deleted


def delete_all_rows(db_path: str, table: str) -> None:
    db = _open(db_path)
    try:
        db.cursor.execute(f"DELETE FROM {_quote(table)}")
        db.conn.commit()
    finally:
        db.conn.close()


__all__ = [
    "LocalTableError",
    "RowKey",
    "TableColumn",
    "TableInfo",
    "TableRows",
    "add_column",
    "delete_all_rows",
    "delete_rows",
    "drop_column",
    "insert_row",
    "read_rows",
    "read_table_info",
    "rebuild_for_types",
    "rename_column",
    "rename_local_table",
    "row_key",
    "update_row",
]
