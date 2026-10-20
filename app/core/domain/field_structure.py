"""字段结构的导出（.xls）与规范检查（Qt-free）。

导出格式与「创建数据 → 结构来源 → 模板文件」完全一致（同一套 9 列表头），
导出的文件可以直接再作为字段结构模板导入。

检查项：

- 字段名合法性、别名为空、mzguid 是否缺失；
- 字典字段：本地库里是否有对应字典表 / 字典域、字段实际值是否都在字典里；
  本地缺少但应用级字典管理里有时，降为警告并提示重新落库；
- NOT NULL 字段是否存在空值。

读写都走 :mod:`app.core.domain.local_table_edit` 与 ``local_db_util``。
"""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass

from app.core.domain.create_data.fields import FIELD_TYPES
from app.core.domain.create_data.template import TEMPLATE_HEADERS
from app.core.domain.local_table_edit import read_table_info
from app.core.util.field_meta import FieldMeta
from app.core.util.local_db_util import local_db_util
from app.infra.logging import get_logger

logger = get_logger(__name__)

_NAME_RE = re.compile(r"^[^\W\d]\w*$", re.UNICODE)
_TYPE_BY_KEY = {item.key: item for item in FIELD_TYPES}
_SAMPLE_LIMIT = 1000


class FieldStructureError(RuntimeError):
    """字段结构导出失败。消息可直接展示。"""


@dataclass(frozen=True)
class FieldIssue:
    """一条检查结果；``level`` 为「错误」或「警告」。"""

    level: str
    field: str
    message: str


def _quote(name: str) -> str:
    return '"' + str(name).replace('"', '""') + '"'


def _parse_declared(declared: str) -> tuple[str, int, int]:
    """SQL 类型声明 → ``(type_key, length, precision)``。"""
    text = str(declared or "").upper()
    match = re.match(r"(?:TEXT|VARCHAR|CHAR)\s*\((\d+)\)", text)
    if match:
        return "text", int(match.group(1)), 0
    match = re.match(r"(?:DECIMAL|NUMERIC)\s*\((\d+)(?:\s*,\s*(\d+))?\)", text)
    if match:
        return "double", int(match.group(1)), int(match.group(2) or 0)
    if "DATETIME" in text or "TIMESTAMP" in text or text.startswith("TIME"):
        return "datetime", 0, 0
    if "DATE" in text:
        return "date", 0, 0
    if "BOOL" in text:
        return "bool", 0, 0
    if "INT" in text:
        return "integer", 0, 0
    if any(word in text for word in ("REAL", "FLOA", "DOUB")):
        return "double", 0, 0
    return "text", 0, 0


def _type_label(type_key: str) -> str:
    item = _TYPE_BY_KEY.get(type_key)
    return item.label if item is not None else FIELD_TYPES[0].label


# --------------------------------------------------------------------- 导出


def field_structure_rows(db_path: str, table: str) -> list[list[str]]:
    """当前表的业务字段结构（顺序 = 字段表顺序，排除几何列 / 自增主键）。"""
    info = read_table_info(db_path, table)
    metas = info.field_meta or {}
    aliases = info.aliases or {}
    rows: list[list[str]] = []
    for col in info.editable_columns:
        meta = metas.get(col.name) or FieldMeta()
        type_key, length, precision = _parse_declared(col.declared_type)
        if meta.dict_table:
            type_key = "dict"
        rows.append(
            [
                col.name,
                aliases.get(col.name, ""),
                _type_label(type_key),
                str(length) if length else "",
                str(precision) if precision else "",
                meta.dict_table,
                meta.dict_domain,
                "是" if not col.notnull else "否",
                "是" if meta.visible else "否",
            ]
        )
    return rows


def export_field_structure_xls(path: str, db_path: str, table: str) -> tuple[str, int]:
    """把字段结构写成 ``.xls``（BIFF，用 QGIS 自带的 xlwt）。

    Returns:
        ``(实际路径, 字段数)``。

    Raises:
        FieldStructureError: 没有字段 / 缺少 xlwt / 文件被占用。
    """
    rows = field_structure_rows(db_path, table)
    if not rows:
        raise FieldStructureError("这张表没有可导出的业务字段")
    try:
        import xlwt
    except ImportError as exc:  # pragma: no cover —— QGIS 运行时自带
        raise FieldStructureError("本机缺少 xlwt，无法导出 .xls") from exc
    target = str(path).strip()
    if not target.lower().endswith(".xls"):
        target = os.path.splitext(target)[0] + ".xls"
    try:
        workbook = xlwt.Workbook(encoding="utf-8")
        sheet = workbook.add_sheet("字段结构")
        for column, header in enumerate(TEMPLATE_HEADERS):
            sheet.write(0, column, header)
        for row_index, values in enumerate(rows, start=1):
            for column, value in enumerate(values):
                sheet.write(row_index, column, value)
        workbook.save(target)
    except OSError as exc:
        raise FieldStructureError(
            f"写 Excel 失败：{target}\n目标文件可能正被 Excel / WPS 打开，请关闭后重试。\n{exc}"
        ) from exc
    logger.info("已导出字段结构：%s（%d 个字段）", target, len(rows))
    return target, len(rows)


# --------------------------------------------------------------------- 检查


def check_field_structure(
    db_path: str,
    table: str,
    *,
    dict_service=None,
) -> list[FieldIssue]:
    """检查字段结构 / 字典关联；返回问题列表（错误在前，警告在后）。"""
    info = read_table_info(db_path, table)
    columns = info.editable_columns
    issues: list[FieldIssue] = []
    if not columns:
        return [FieldIssue("警告", "", "这张表没有可检查的业务字段")]

    names = {col.name.casefold() for col in columns}
    if "mzguid" not in names:
        issues.append(
            FieldIssue(
                "警告",
                "",
                "缺少 mzguid 字段（成果数据的图斑唯一标识；创建数据时通常会自动加）",
            )
        )

    local_dicts = _local_dicts(db_path)
    service = dict_service
    app_categories: set[str] = set()
    app_domains: dict[str, set[str]] = {}
    if service is not None:
        try:
            for category in service.list_categories():
                app_categories.add(category.name)
                app_domains[category.name] = {
                    domain.domain for domain in service.list_domains(category.name)
                }
        except Exception as exc:  # noqa: BLE001
            logger.warning("读取应用级字典失败，跳过该项检查：%s", exc)

    aliases = info.aliases or {}
    metas = info.field_meta or {}

    for col in columns:
        if not _NAME_RE.match(col.name):
            issues.append(
                FieldIssue("错误", col.name, "字段名不合法（不能以数字开头 / 只能含中文、字母、数字、下划线）")
            )
        if not str(aliases.get(col.name) or "").strip():
            issues.append(FieldIssue("警告", col.name, "字段别名为空"))

        if col.notnull:
            nulls = _count_nulls(db_path, table, col.name)
            if nulls:
                issues.append(
                    FieldIssue("错误", col.name, f"字段声明为不可为空，但有 {nulls} 行为空")
                )

        meta = metas.get(col.name) or FieldMeta()
        if not meta.dict_table:
            continue
        type_key, _length, _precision = _parse_declared(col.declared_type)
        if type_key not in ("text", "dict"):
            issues.append(
                FieldIssue("警告", col.name, "字典字段底层不是文本列，可能无法按字典值匹配")
            )
        category = meta.dict_table
        domain = meta.dict_domain
        entry = local_dicts.get(category)
        app_has = category in app_categories
        if entry is None:
            if app_has:
                issues.append(
                    FieldIssue(
                        "错误",
                        col.name,
                        f"本地库缺少字典表「{category}」（字典管理里有，建议重新创建数据或重新落库）",
                    )
                )
            else:
                issues.append(
                    FieldIssue("错误", col.name, f"字典表「{category}」不存在")
                )
            continue
        if not domain:
            issues.append(
                FieldIssue("警告", col.name, "字段设置了字典表，但没有选择字典域")
            )
            continue
        if entry["has_domain"]:
            if entry["domains"] and domain not in entry["domains"]:
                if domain in app_domains.get(category, set()):
                    issues.append(
                        FieldIssue(
                            "错误",
                            col.name,
                            f"本地字典表「{category}」缺少字典域「{domain}」",
                        )
                    )
                else:
                    issues.append(
                        FieldIssue(
                            "错误",
                            col.name,
                            f"本地字典表「{category}」里没有字典域「{domain}」",
                        )
                    )
                continue
            codes = entry["codes_by_domain"].get(domain, set())
        else:
            codes = entry["codes"]
        values = _distinct_values(db_path, table, col.name)
        if len(values) > _SAMPLE_LIMIT:
            issues.append(
                FieldIssue(
                    "警告",
                    col.name,
                    f"字段值超过 {_SAMPLE_LIMIT} 个，仅抽检前 {_SAMPLE_LIMIT} 个",
                )
            )
        missing = sorted(value for value in values if value not in codes)
        if missing:
            preview = "、".join(missing[:5])
            issues.append(
                FieldIssue(
                    "警告",
                    col.name,
                    f"字典值缺失 {len(missing)} 个（不在「{category}/{domain}」里）：{preview}"
                    + ("…" if len(missing) > 5 else ""),
                )
            )
        elif not values:
            issues.append(
                FieldIssue("警告", col.name, "字典字段没有非空值，无法校验字典")
            )

    issues.sort(key=lambda item: 0 if item.level == "错误" else 1)
    return issues


def _local_dicts(db_path: str) -> dict[str, dict]:
    """本地库里登记的 dict 表 → {字典表: {domains, has_domain, codes, codes_by_domain}}。"""
    db = local_db_util(db_path)
    out: dict[str, dict] = {}
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
            category = str(data.get("dict_table") or data.get("category") or table)
            try:
                columns = {
                    str(row[1])
                    for row in db.cursor.execute(
                        f"PRAGMA table_info({_quote(table)})"
                    )
                }
                if "code" not in columns:
                    continue
                entry = out.setdefault(
                    category,
                    {
                        "domains": set(),
                        "has_domain": "domain" in columns,
                        "codes": set(),
                        "codes_by_domain": {},
                    },
                )
                if "domain" in columns:
                    for domain, code in db.cursor.execute(
                        f"SELECT domain, code FROM {_quote(table)}"
                    ):
                        domain, code = str(domain or ""), str(code or "")
                        entry["domains"].add(domain)
                        entry["codes_by_domain"].setdefault(domain, set()).add(code)
                        entry["codes"].add(code)
                else:
                    for (code,) in db.cursor.execute(
                        f"SELECT code FROM {_quote(table)}"
                    ):
                        entry["codes"].add(str(code or ""))
                    entry["domains"].update(
                        str(value) for value in (data.get("domains") or [])
                    )
            except Exception:
                logger.warning("读取本地字典表失败：%s", table, exc_info=True)
    finally:
        db.conn.close()
    return out


def _distinct_values(db_path: str, table: str, column: str) -> set[str]:
    db = local_db_util(db_path)
    try:
        quoted = _quote(column)
        rows = db.cursor.execute(
            f"SELECT DISTINCT CAST({quoted} AS TEXT) AS v FROM {_quote(table)} "
            f"WHERE {quoted} IS NOT NULL AND TRIM(CAST({quoted} AS TEXT)) <> '' "
            f"LIMIT {_SAMPLE_LIMIT + 1}"
        ).fetchall()
    finally:
        db.conn.close()
    return {str(row[0]) for row in rows if row[0] is not None}


def _count_nulls(db_path: str, table: str, column: str) -> int:
    db = local_db_util(db_path)
    try:
        row = db.cursor.execute(
            f"SELECT COUNT(*) FROM {_quote(table)} WHERE {_quote(column)} IS NULL"
        ).fetchone()
    finally:
        db.conn.close()
    return int(row[0]) if row else 0


__all__ = [
    "FieldIssue",
    "FieldStructureError",
    "check_field_structure",
    "export_field_structure_xls",
    "field_structure_rows",
]
