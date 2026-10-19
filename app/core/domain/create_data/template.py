"""字段结构模板文件（Excel）的读写：创建数据向导第二步「结构来源」用。

模板就是一张表，表头九列（与第二步的字段表同序，只少了「选中」这一列）：

``字段名 | 字段别名 | 字段类型 | 字段长度 | 小数位数 | 字典表 | 字典域 | 是否可为空 | 是否可见``

**走本机 QGIS 自带的 GDAL，不引第三方 Excel 库**（本工程没有 openpyxl / xlrd，也不打算加）：

- **写**：只有 ``XLSX`` 驱动可写（实测 ``Xls -vector- (ro)`` / ``XLSX -vector- (rw+uv)``），
  所以「生成空模板文件」产出的**一定是 .xlsx**。原型界面上写的是「模板文件(XLS)」，
  读的时候 .xls / .xlsx 两个都收（``XLS`` 驱动只读，正好够用）。
  本机没有 XLSX 驱动时 :func:`xlsx_available` 为假，界面据此灰掉按钮 ——
  别让用户点了才报错。
- **读**：``QgsVectorLayer(path, name, "ogr")`` 直接打开。**两个驱动都会按单元格内容
  猜列类型**（用户在第 3 行填个数字，整列就成整数列，再填文字就丢了），所以 xlsx 一律带
  ``|option:FIELD_TYPES=STRING`` 打开，让所有格子都是字符串。
- **表头所在行不固定**：``HEADERS`` 的自动判定在 .xls / .xlsx 两个驱动上不一致
  （一个可能把第一行吃掉当字段名）。:func:`read_template` 两种都认：先看图层字段名里
  有没有「字段名」，再看第一行数据，谁认得出来用谁（见 :func:`_resolve_header`）。
- **空模板也要有表头**：OGR 的 XLSX 是「先有数据行才有列」，0 个要素写出来是一张空表，
  回读连列都没有（实测）。所以没有字段可写时补一行全空的行，读的时候再把整行皆空的
  行跳掉（:func:`_is_blank_row`）—— 用户拿到的是「只有表头的空模板」。
"""

from __future__ import annotations

import os
from typing import TYPE_CHECKING

from app.core.domain.create_data.fields import (
    FIELD_TYPES,
    MAX_FIELD_LENGTH,
    MAX_FIELD_PRECISION,
    FieldSpec,
    to_int,
    type_for_label,
)
from app.core.domain.create_data.service import available_drivers
from app.infra.logging import get_logger

if TYPE_CHECKING:
    from collections.abc import Iterable, Sequence

logger = get_logger(__name__)

#: 模板表头（顺序 = 第二步字段表的顺序，少一列「选中」）
TEMPLATE_HEADERS: tuple[str, ...] = (
    "字段名",
    "字段别名",
    "字段类型",
    "字段长度",
    "小数位数",
    "字典表",
    "字典域",
    "是否可为空",
    "是否可见",
)

#: 模板里工作表的名字（写盘时的 ``layerName``）
TEMPLATE_SHEET_NAME = "字段结构"

#: 「生成空模板文件」保存对话框的过滤器 / 默认扩展名
TEMPLATE_FILTER = "Excel 工作簿 (*.xlsx);;所有文件 (*)"
TEMPLATE_EXTENSION = ".xlsx"

#: 读模板时的文件过滤器（.xls 只读，所以只在导入时出现）
TEMPLATE_READ_FILTER = "Excel 模板 (*.xls *.xlsx *.xlsm);;所有文件 (*)"

#: 「是 / 否」两列里认得出的值（别的写法按默认处理）
_TRUE_WORDS = frozenset({"是", "y", "yes", "true", "1", "可空", "可见"})
_FALSE_WORDS = frozenset({"否", "n", "no", "false", "0", "不可空", "不可见", "隐藏"})

#: 表头文字 → 规范化名（容忍空格与全角空格；用户手打的表头常常带空格）
_HEADER_ALIASES: dict[str, str] = {
    "字段名": "字段名",
    "字段名称": "字段名",
    "名称": "字段名",
    "字段别名": "字段别名",
    "别名": "字段别名",
    "字段类型": "字段类型",
    "类型": "字段类型",
    "字段长度": "字段长度",
    "长度": "字段长度",
    "小数位数": "小数位数",
    "小数位": "小数位数",
    "精度": "小数位数",
    "字典表": "字典表",
    "字典域": "字典域",
    "是否可为空": "是否可为空",
    "可为空": "是否可为空",
    "是否可空": "是否可为空",
    "是否可见": "是否可见",
    "可见": "是否可见",
}


class FieldTemplateError(RuntimeError):
    """模板文件读写失败。消息可直接展示给用户。"""


def xlsx_available() -> bool:
    """本机 QGIS 的 GDAL 能不能写出 .xlsx（决定「生成空模板文件」能不能点）。"""
    return "XLSX" in available_drivers()


# --------------------------------------------------------------------- 写


def write_template(path: str, fields: Sequence[FieldSpec] = ()) -> str:
    """把 ``fields`` 写成一份模板文件（``fields`` 为空就写只有表头的空模板）。

    Args:
            path: 目标文件路径（.xlsx；调用方负责让用户选，通常经 ``save_file``）。
            fields: 预填进模板的字段（通常为空 —— 模板是给用户填的）。

    Returns:
            实际写出的文件路径。

    Raises:
            FieldTemplateError: 本机没有 XLSX 驱动 / 写盘失败。
    """
    from qgis.core import (
        QgsCoordinateTransformContext,
        QgsField,
        QgsFeature,
        QgsVectorFileWriter,
        QgsVectorLayer,
    )
    from qgis.PyQt.QtCore import QVariant

    if not xlsx_available():
        raise FieldTemplateError(
            "本机 QGIS 没有可写 Excel（XLSX）的 GDAL 驱动，生成不了模板文件"
        )
    # 「None」= 纯属性表（模板没有几何）
    layer = QgsVectorLayer("None", TEMPLATE_SHEET_NAME, "memory")
    if not layer.isValid():
        raise FieldTemplateError("建不出模板用的属性表图层")
    layer.dataProvider().addAttributes(
        [QgsField(name, QVariant.String, "string", 255, 0) for name in TEMPLATE_HEADERS]
    )
    layer.updateFields()

    rows = [_row_values(spec) for spec in fields]
    if not rows:
        # 一行数据都没有的话，XLSX 里连列都写不出来（表头都没了）——补一行全空的
        rows = [[""] * len(TEMPLATE_HEADERS)]
    feats = []
    for values in rows:
        feature = QgsFeature(layer.fields())
        feature.setAttributes(values)
        feats.append(feature)
    layer.dataProvider().addFeatures(feats)

    try:
        if os.path.exists(path):
            os.remove(path)
        options = QgsVectorFileWriter.SaveVectorOptions()
        options.driverName = "XLSX"
        options.layerName = TEMPLATE_SHEET_NAME
        options.fileEncoding = "UTF-8"
        error, errmsg, _new, _saved = QgsVectorFileWriter.writeAsVectorFormatV3(
            layer, path, QgsCoordinateTransformContext(), options
        )
    except OSError as exc:
        # 目标文件只读 / 正被 Excel 占着（Windows 上 Excel 打开着就删不掉也写不进）：
        # **必须在这里转成 FieldTemplateError** —— 本函数是从按钮的槽里调的，
        # 让 OSError 冒到槽外面在 PyQt6 里会直接把进程带走（实测过）。
        raise FieldTemplateError(f"写模板文件失败：{path}\n{exc}") from exc
    if error != QgsVectorFileWriter.WriterError.NoError:
        raise FieldTemplateError(f"写模板文件失败：{errmsg or '未知原因'}")
    logger.info("已生成字段结构模板：%s（%d 个字段）", path, len(fields))
    return path


def _row_values(spec: FieldSpec) -> list[str]:
    """一个 :class:`FieldSpec` → 模板的一行（「是 / 否」用界面上的说法）。"""
    return [
        spec.name,
        spec.alias,
        spec.field_type.label,
        str(spec.length) if spec.length else "",
        str(spec.precision) if spec.precision else "",
        spec.dict_table,
        spec.dict_domain,
        "是" if spec.nullable else "否",
        "是" if spec.visible else "否",
    ]


# --------------------------------------------------------------------- 读


def read_template(path: str) -> list[FieldSpec]:
    """读一份模板文件，返回里面的字段结构。

    **空字段名的行照样返回**（不在这里拦）：用户填漏一行是常事，交给
    :func:`~app.core.domain.create_data.fields.validate_fields` 报
    「存在空字段名，请删除该行」，比在这里静默丢掉更说得清。

    Args:
        path: 模板文件路径（.xls / .xlsx / .xlsm）。

    Returns:
        模板里读出来的字段结构（顺序 = 文件里的行序）。

    Raises:
        FieldTemplateError: 打不开 / 认不出表头（没有任何一行像表头）。
    """
    from qgis.core import QgsVectorLayer

    layer = _open_template(path)
    if not layer.isValid():
        raise FieldTemplateError(
            f"打不开模板文件：{path}\n"
            "（支持 Excel 的 .xls / .xlsx；文件被别的程序占用也会打不开）"
        )
    field_names = [layer.fields().at(i).name() for i in range(layer.fields().size())]
    rows = [
        ["" if value is None else str(value).strip() for value in feature.attributes()]
        for feature in layer.getFeatures()
    ]
    columns, data_rows = _resolve_header(field_names, rows)
    return [spec for spec in (_row_to_spec(row, columns) for row in data_rows) if spec is not None]


def _open_template(path: str):
    """按扩展名挑打开方式（xlsx 系列一律强制所有列为字符串，见模块头）。"""
    from qgis.core import QgsVectorLayer

    uri = path
    if os.path.splitext(path)[1].lower() in (".xlsx", ".xlsm"):
        uri = f"{path}|option:FIELD_TYPES=STRING"
    return QgsVectorLayer(uri, "字段结构模板", "ogr")


def _resolve_header(
    field_names: Sequence[str], rows: Sequence[Sequence[str]]
) -> tuple[dict[str, int], Sequence[Sequence[str]]]:
    """定位表头：返回 ``(表头名 → 列号, 数据行)``。

    表头可能被驱动当成「字段名」（此时数据从第一行开始），也可能就是普通的第一行数据
    （此时数据从第二行开始）—— 谁认得出来用谁，认得出的列多者优先。
    """
    from_fields = _header_columns(field_names)
    first_row = list(rows[0]) if rows else []
    from_row = _header_columns(first_row)
    if len(from_row) >= len(from_fields) and "字段名" in from_row:
        return from_row, list(rows[1:])
    if "字段名" in from_fields:
        return from_fields, list(rows)
    if "字段名" in from_row:
        return from_row, list(rows[1:])
    raise FieldTemplateError(
        "模板里没有「字段名」这一列，认不出表头。请用「生成空模板文件」产出的模板填写"
    )


def _header_columns(names: Sequence[str]) -> dict[str, int]:
    """一串表头文字 → ``{规范名: 列号}``（只收认得出的列）。"""
    columns: dict[str, int] = {}
    for index, raw in enumerate(names):
        key = _HEADER_ALIASES.get(_normalize_header(raw))
        if key and key not in columns:
            columns[key] = index
    return columns


def _normalize_header(raw: str) -> str:
    """表头文字规范化：去空格（含全角空格）、去星号（必填标记）。"""
    return str(raw or "").replace(" ", "").replace("　", "").replace("*", "").strip()


def _row_to_spec(row: Sequence[str], columns: dict[str, int]) -> FieldSpec | None:
    """一行 → :class:`FieldSpec`；整行皆空（空模板里补的那行）返回 ``None``。"""

    def cell(name: str) -> str:
        index = columns.get(name, -1)
        return row[index].strip() if 0 <= index < len(row) else ""

    values = [cell(name) for name in TEMPLATE_HEADERS]
    if _is_blank_row(values):
        return None

    name = values[0]
    ftype = type_for_label(values[2]) if values[2] else None
    if ftype is None:
        if values[2]:
            logger.info("模板里的字段类型「%s」认不出，字段 %s 按文本处理", values[2], name)
        ftype = FIELD_TYPES[0]
    return FieldSpec(
        name=name,
        alias=values[1],
        type_key=ftype.key,
        length=to_int(values[3], MAX_FIELD_LENGTH),
        precision=to_int(values[4], MAX_FIELD_PRECISION),
        dict_table=values[5],
        dict_domain=values[6],
        nullable=_to_bool(values[7], default=True),
        visible=_to_bool(values[8], default=True),
    )


def _is_blank_row(values: Iterable[str]) -> bool:
    """整行都是空的（空模板里为了保住表头而补的那一行）。"""
    return not any(str(v or "").strip() for v in values)


def _to_bool(raw: str, *, default: bool) -> bool:
    """「是 / 否」列 → bool；空着或认不出用 ``default``。"""
    text = str(raw or "").strip().lower()
    if not text:
        return default
    if text in _FALSE_WORDS:
        return False
    if text in _TRUE_WORDS:
        return True
    return default
