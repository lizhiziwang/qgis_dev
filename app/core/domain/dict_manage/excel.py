"""字典的 Excel 导入 / 导出（走本机 QGIS 自带 GDAL，不引第三方 Excel 库）。

模板参照 ``业务字典-FL_GDB_DICTS_林草湿更新成果.xls``：

- 工作表「字典表」；
- 表头 ``字典域 | 代码 | 名称 | 上级代码(仅多级字典需填写)``；
- 上面可能还有一行文件标题，所以**不能假设第一行是表头**，要扫描前若干行。

**导入归属由界面决定**：调用方先选好类别，文件里所有行都进这个类别 ——
不再按域名前缀或「类别」列猜。

读：``QgsVectorLayer(path, ..., "ogr")``，统一带
``|option:FIELD_TYPES=STRING`` —— 强制字符串避免 OGR 把 ``0100`` 之类代码
猜成数字、丢掉前导零。**不用 ``HEADERS=NO``**：本机 GDAL 的 XLS/XLSX 驱动
不认这个值（只报一条 warning 后忽略），而表头本来就要靠扫描前若干行来定位
（参考模板第一行是文件标题）。

写：GDAL 的 ``XLSX`` 驱动（``.xls`` 只读，不提供写），表头由字段名生成；
导出当前类别，列与模板一致（4 列）。

本模块保持 Qt-free：``qgis.core`` 一律在函数内延迟导入（同
:mod:`app.core.domain.create_data.template`）。
"""

from __future__ import annotations

import os
import re
from collections import defaultdict
from collections.abc import Sequence
from pathlib import Path

from app.core.domain.dict_manage.models import DictError, DictItem, DictRow, ImportReport
from app.core.domain.dict_manage.service import DictService
from app.infra.logging import get_logger

logger = get_logger(__name__)

#: 导出文件的工作表名（与参考模板一致）
EXPORT_SHEET_NAME = "字典表"

#: 导出表头：与参考模板完全一致（类别由「导出当前类别」隐含）
EXPORT_HEADERS: tuple[str, ...] = (
    "字典域",
    "代码",
    "名称",
    "上级代码(仅多级字典需填写)",
)

#: 文件选择框过滤器
IMPORT_FILTER = "Excel 业务字典 (*.xls *.xlsx *.xlsm);;所有文件 (*)"
EXPORT_FILTER = "Excel 工作簿 (*.xlsx);;所有文件 (*)"

#: 表头文字 → 规范名（容忍标题行、别名、空格、必填星号、括号补充说明）
_HEADER_ALIASES: dict[str, str] = {
    "字典域": "domain",
    "字典表": "domain",
    "字典名": "domain",
    "字典名称": "domain",
    "域": "domain",
    "代码": "code",
    "编码": "code",
    "代码值": "code",
    "名称": "name",
    "名称值": "name",
    "显示名": "name",
    "显示名称": "name",
    "上级代码": "parent_code",
    "上级编码": "parent_code",
    "父代码": "parent_code",
    "父级代码": "parent_code",
    "上级": "parent_code",
}

#: 表头里视为「必须认出」的三列
_REQUIRED = ("domain", "code", "name")

#: 表头最多在前多少行里找（参考模板第 0 行是标题、第 1 行才是表头）
_HEADER_SCAN_ROWS = 10

_PAREN_RE = re.compile(r"[（(][^）)]*[）)]")


class DictImportError(DictError):
    """Excel 读不了 / 认不出表头。消息可直接展示。"""


class DictExportError(DictError):
    """Excel 写不了（驱动缺失 / 文件被占用）。消息可直接展示。"""


# --------------------------------------------------------------------- 导入


def import_excel(
    path: str | Path,
    *,
    service: DictService,
    category: str,
    replace_domains: bool = False,
) -> ImportReport:
    """把一份 Excel 业务字典全部导入 ``category`` 指定的类别。

    Args:
        path: ``.xls`` / ``.xlsx`` / ``.xlsm``。
        service: 目标字典库。
        category: 导入到哪个类别（界面当前下拉选中的类别）。
        replace_domains: ``True`` = 先清空文件中出现过的域再写入；默认合并更新。

    Returns:
        :class:`ImportReport`（含新增 / 更新 / 跳过 / 告警统计）。

    Raises:
        DictImportError: 文件打不开 / 认不出表头。
    """
    rows, skipped, warnings = _read_rows(path, category)
    report = service.apply_import(rows, replace_domains=replace_domains)
    report.rows_total += skipped
    report.rows_skipped += skipped
    for message in warnings:
        report.warn(message)
    return report


def _read_rows(
    path: str | Path,
    category: str,
) -> tuple[list[DictRow], int, list[str]]:
    """读文件 → ``(有效行, 跳过行数, 告警)``；所有行归入 ``category``。"""
    source = str(path)
    if not os.path.isfile(source):
        raise DictImportError(f"文件不存在：{source}")
    layer = _open_layer(source)
    field_names = [layer.fields().at(i).name() for i in range(layer.fields().size())]
    raw_rows = [
        [_cell(value) for value in feature.attributes()]
        for feature in layer.getFeatures()
    ]
    columns, data_rows = _resolve_header(field_names, raw_rows)

    rows: list[DictRow] = []
    skipped = 0
    warnings: list[str] = []
    for index, values in enumerate(data_rows, start=1):
        if _is_blank_row(values):
            continue
        # 有的文件会在中间重复表头（分页打印导出常见），别当数据。
        if len(_header_columns(values)) >= len(_REQUIRED):
            continue

        def cell(name: str, values: Sequence[str] = values) -> str:
            col = columns.get(name, -1)
            return values[col] if 0 <= col < len(values) else ""

        domain = cell("domain")
        code = cell("code")
        name = cell("name")
        if not (domain and code and name):
            skipped += 1
            if len(warnings) < 50:
                warnings.append(f"数据第 {index} 行缺少字典域 / 代码 / 名称，已跳过")
            continue
        rows.append(
            DictRow(
                category=category,
                domain=domain,
                code=code,
                name=name,
                parent_code=cell("parent_code"),
                row_number=index,
            )
        )
    if not rows:
        raise DictImportError(
            f"没有读到有效的字典数据：{source}\n"
            f"表头应包含：{' / '.join(('字典域', '代码', '名称'))}"
        )
    return rows, skipped, warnings


def _open_layer(path: str):
    """按原始行模式打开 Excel；失败时退回普通打开（只读驱动仍能读）。"""
    from qgis.core import QgsVectorLayer

    lower = os.path.splitext(path)[1].lower()
    uri = (
        f"{path}|option:FIELD_TYPES=STRING"
        if lower in (".xls", ".xlsx", ".xlsm")
        else path
    )
    layer = QgsVectorLayer(uri, "字典导入", "ogr")
    if not layer.isValid():
        layer = QgsVectorLayer(path, "字典导入", "ogr")
    if not layer.isValid():
        raise DictImportError(
            f"打不开 Excel 文件：{path}\n"
            "（支持 .xls / .xlsx / .xlsm；文件正被 Excel 独占时请先关闭）"
        )
    return layer


def _resolve_header(
    field_names: Sequence[str],
    rows: Sequence[Sequence[str]],
) -> tuple[dict[str, int], Sequence[Sequence[str]]]:
    """定位表头，返回 ``(规范名 → 列号, 数据行)``。

    优先在数据前若干行里找「字典域 + 代码 + 名称」都认得出的行（参考模板
    第一行是标题）；认不出再看驱动给的字段名（可能是表头被当成字段名了）。
    """
    best: tuple[int, int, dict[str, int]] | None = None  # (匹配数, 行号, 列)
    for index, row in enumerate(rows[:_HEADER_SCAN_ROWS]):
        columns = _header_columns(row)
        if all(key in columns for key in _REQUIRED):
            score = len(columns)
            if best is None or score > best[0]:
                best = (score, index, columns)
    if best is not None:
        return best[2], list(rows[best[1] + 1:])

    from_fields = _header_columns(field_names)
    if all(key in from_fields for key in _REQUIRED):
        return from_fields, list(rows)

    raise DictImportError(
        "认不出表头：文件里需要有「字典域 / 代码 / 名称」三列"
        "（可选「上级代码」）"
    )


def _header_columns(names: Sequence[str]) -> dict[str, int]:
    """一行表头 → ``{规范名: 列号}``；只收认得出的列。"""
    columns: dict[str, int] = {}
    for index, raw in enumerate(names):
        key = _HEADER_ALIASES.get(_normalize_header(raw))
        if key and key not in columns:
            columns[key] = index
    return columns


def _normalize_header(raw: str) -> str:
    """去括号说明、空格（含全角）、必填星号，得到可查别名的表头。"""
    text = _PAREN_RE.sub("", str(raw or ""))
    return text.replace(" ", "").replace("　", "").replace("*", "").strip()


def _cell(value: object) -> str:
    """单元格值 → 字符串；``1.0`` 这类整数值不写成 ``"1.0"``。"""
    if value is None:
        return ""
    if isinstance(value, bool):
        return "1" if value else "0"
    if isinstance(value, float) and value.is_integer():
        return str(int(value))
    return str(value).replace("　", " ").strip()


def _is_blank_row(values: Sequence[str]) -> bool:
    return not any(str(value or "").strip() for value in values)


# --------------------------------------------------------------------- 导出


def xlsx_available() -> bool:
    """本机 GDAL 是否能写 ``.xlsx``（决定「Excel 导出」按钮是否可点）。"""
    from qgis.core import QgsVectorFileWriter

    return ".xlsx" in QgsVectorFileWriter.supportedFormatExtensions()


def export_excel(
    path: str | Path,
    *,
    service: DictService,
    category: str | None = None,
) -> tuple[str, int]:
    """把字典导出成 ``.xlsx``，返回 ``(实际写出的路径, 行数)``。

    Args:
        path: 目标路径；扩展名不是 ``.xlsx`` 时自动改掉。
        service: 字典库。
        category: 导出哪个类别；``None`` = 全部类别（界面固定传当前类别）。

    Raises:
        DictExportError: 没有数据 / 驱动缺失 / 文件被占用。
    """
    written = normalize_export_path(path)
    items = service.export_items(category)
    if not items:
        raise DictExportError("没有可导出的字典数据")
    rows = _ordered_rows(items)
    _write_xlsx(written, rows)
    logger.info("已导出字典：%s（%d 行）", written, len(rows))
    return written, len(rows)


def normalize_export_path(path: str | Path) -> str:
    """导出路径收口为 ``.xlsx``（GDAL 不能写 ``.xls``）。"""
    text = str(path).strip()
    if not text:
        raise DictExportError("请选择导出文件路径")
    stem, ext = os.path.splitext(text)
    if ext.lower() != ".xlsx":
        text = f"{stem}.xlsx"
    return text


def _ordered_rows(items: Sequence[DictItem]) -> list[list[str]]:
    """按「域 → 先序（父在前子在后）」摊平成导出行。

    数据再脏也不能丢：成环 / 悬空父项的节点追加为顶级再写一遍。
    """
    groups: dict[str, list[DictItem]] = defaultdict(list)
    for item in items:
        groups[item.domain].append(item)

    out: list[list[str]] = []
    for domain, group in groups.items():
        children: dict[str, list[DictItem]] = defaultdict(list)
        codes = {item.code for item in group}
        for item in group:
            # 父代码在本域里不存在 → 当顶级，避免这一支在 Excel 里失踪。
            parent = item.parent_code if item.parent_code in codes else ""
            children[parent].append(item)

        written: set[str] = set()

        def emit(
            parent: str,
            children: dict[str, list[DictItem]] = children,
            written: set[str] = written,
            domain: str = domain,
        ) -> None:
            for item in children.get(parent, []):
                if item.code in written:
                    continue
                written.add(item.code)
                out.append([domain, item.code, item.name, item.parent_code])
                emit(item.code)

        emit("")
        # 成环的节点从 "" 走不到：按顶级补写，不让用户以为数据丢了。
        for item in group:
            if item.code not in written:
                out.append([domain, item.code, item.name, ""])
                written.add(item.code)
                emit(item.code)
    return out


def _write_xlsx(path: str, rows: Sequence[Sequence[str]]) -> None:
    """用 GDAL XLSX 驱动写一张纯属性表（字段名即表头）。"""
    from qgis.core import (
        QgsCoordinateTransformContext,
        QgsFeature,
        QgsField,
        QgsVectorFileWriter,
        QgsVectorLayer,
    )
    from qgis.PyQt.QtCore import QVariant

    layer = QgsVectorLayer("None", EXPORT_SHEET_NAME, "memory")
    if not layer.isValid():
        raise DictExportError("建不出导出用的属性表图层")
    layer.dataProvider().addAttributes(
        [QgsField(name, QVariant.String, "string", 255, 0) for name in EXPORT_HEADERS]
    )
    layer.updateFields()
    features = []
    for values in rows:
        feature = QgsFeature(layer.fields())
        feature.setAttributes(list(values))
        features.append(feature)
    layer.dataProvider().addFeatures(features)

    options = QgsVectorFileWriter.SaveVectorOptions()
    options.driverName = "XLSX"
    options.layerName = EXPORT_SHEET_NAME
    options.fileEncoding = "UTF-8"
    try:
        # Windows 上 Excel 打开着目标文件时删不掉也写不进；转成可展示的错误，
        # 不让 OSError 冒到 Qt 槽外面（PyQt6 会直接杀进程）。
        if os.path.exists(path):
            os.remove(path)
        error, errmsg, _new, _saved = QgsVectorFileWriter.writeAsVectorFormatV3(
            layer, path, QgsCoordinateTransformContext(), options
        )
    except OSError as exc:
        raise DictExportError(
            f"写 Excel 失败：{path}\n"
            f"目标文件可能正被 Excel / WPS 打开，请关闭后重试。\n{exc}"
        ) from exc
    if error != QgsVectorFileWriter.WriterError.NoError:
        try:
            if os.path.exists(path) and os.path.getsize(path) == 0:
                os.remove(path)
        except OSError:
            pass
        raise DictExportError(f"写 Excel 失败：{errmsg or '未知原因'}")
