"""创建数据向导「数据字段设置」的字段模型：类型目录、校验、映射与落盘。

本模块是「创建数据」第二步（字段结构）的**唯一真相**：界面（``create_data_fields_step``）
只负责把表格里的行读成 :class:`FieldSpec`，两个创建服务（``service`` / ``library``）
只负责把 :class:`FieldSpec` 变成真实的列。中间的类型映射 / 校验文案 / 落盘口径全在这里。

**字段类型目录**（仿 ``layer_properties/add_field_dialog.py`` 的 ``_TYPE_OPTIONS``，
但多一个「字典」——字典是本项目业务上常用的类型，见下）：

===========  ==============  ==============  ========================
界面标签      ``type_key``    QgsField 类型   库内 SQL 类型声明
===========  ==============  ==============  ========================
文本          text            string          ``TEXT(n)`` / ``TEXT``
整型          integer         integer         ``INTEGER``
长整型        integer64       integer64       ``INTEGER``
双精度        double          double          ``REAL``
日期          date            date            ``DATE``
日期时间      datetime        datetime        ``DATETIME``
布尔          bool            boolean         ``BOOLEAN``
字典          dict            string          ``TEXT(n)`` / ``TEXT``
===========  ==============  ==============  ========================

SQL 类型名与 GDAL 写 gpkg 时用的**完全一致**（实测：``string(50)`` → ``TEXT(50)``、
``double(18,2)`` → ``REAL``、``date`` → ``DATE``、``boolean`` → ``BOOLEAN``），
这样「新建独立文件」与「写入已有本地数据库」两条路建出来的表结构对得上。

**「字典」按文本列建**：「字典」在界面/业务上有字典表与字典域（下拉取值的来源），
但库里并没有「字典列」这种东西 —— 它就是个受字典约束的文本列，所以这里按 ``text``
建列，字典表 / 字典域另存成**图层自定义属性**（``create_data/dict/*``，随工程保存）。
以后要做「值域校验」时按这两个键读即可。

**落盘口径（实测，别在界面上承诺过头）**——同一份字段结构写到不同目标，能落下的东西不同：

- **写入已有本地数据库**（gpkg / SpatiaLite，走本工程自己的建表原语，DDL 由我们写）：
  字段名 / 类型 / 长度 / 小数位数 / **不可为空（真 NOT NULL）** 都能进库；
  别名也进库 —— gpkg 写标准的 ``gpkg_data_columns``，SpatiaLite 写本工程自己的
  ``qgis_dev_column_alias``（打开库时由 ``local_layer_loader`` 套回图层，实测两个格式
  关掉再打开都还在）。
- **新建独立文件**（OGR 写盘）：字段名 / 类型 / 长度 / 小数位数看格式 ——
  Shapefile / GML / MapInfo TAB / FlatGeobuf / CSV 保留（CSV 的类型一律变文本），
  **GeoJSON / KML / DXF / GPX 的空文件根本存不下字段结构**（它们的 schema 是从要素里推的，
  而本工程建的就是空图层）；别名只有 GPKG / FlatGeobuf 保留（实测），其余格式丢弃；
  「不可为空」没有任何格式接受（OGR 不写 NOT NULL）。
- 落不下的部分：**能落在图层对象上的就落在图层上**（别名 / 隐藏 / 字典属性 / NotNull 约束，
  随工程保存，见 :func:`configure_created_layer`），剩下的如实报给用户 ——
  界面不许假装落盘成功。
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import TYPE_CHECKING

from app.infra.logging import get_logger

if TYPE_CHECKING:
    from qgis.core import QgsVectorLayer

logger = get_logger(__name__)

#: 字段名合法性：与原型一致 —— 只允许「词字符」（Python 的 ``\w`` 是 Unicode 语义，
#: 中文也在里面）与原型额外列出的 CJK 区段。原型写 ``[^\w一-龥]``（JS 的 ``\w``
#: 只认 ASCII，所以要补 CJK 区段），Python 这边 ``\w`` 已经包含中文，等价写法就是 ``[^\w]``。
_ILLEGAL_FIELD_CHARS = re.compile(r"[^\w]")

#: 字段名以数字开头会被 SQL / 多数格式拒绝（原型也是这么校验的）
_LEADING_DIGIT = re.compile(r"^[0-9]")

#: 字典信息存放的图层自定义属性前缀（``create_data/dict_table/<字段名>`` 等）
from app.core.util.field_meta import (
    DICT_DOMAIN_PROP as _DICT_DOMAIN_PROP,
)
from app.core.util.field_meta import (
    DICT_LOCAL_TABLE_PROP as _DICT_LOCAL_TABLE_PROP,
)
from app.core.util.field_meta import (
    DICT_TABLE_PROP as _DICT_TABLE_PROP,
)


@dataclass(frozen=True)
class FieldType:
    """一种字段类型的元信息（界面标签 ↔ QVariant 枚举名 ↔ QgsField 类型名 ↔ SQL 类型）。"""

    #: 类型键（:class:`FieldSpec.type_key` 用的值）
    key: str
    #: 界面标签
    label: str
    #: :mod:`qgis.PyQt.QtCore` 里 ``QVariant`` 的属性名（延迟解析，避免顶层 import qgis）
    qvariant: str
    #: :meth:`QgsField.typeName` 的取值
    type_name: str
    #: 库内 SQL 类型名（不含长度 / 小数位 / NOT NULL）
    sql_type: str
    #: 新选这个类型时的默认长度
    default_length: int = 0
    #: 新选这个类型时的默认小数位数
    default_precision: int = 0


#: 字段类型目录（顺序 = 界面下拉顺序）
FIELD_TYPES: tuple[FieldType, ...] = (
    FieldType("text", "文本", "String", "string", "TEXT", 50, 0),
    FieldType("integer", "整型", "Int", "integer", "INTEGER"),
    FieldType("integer64", "长整型", "LongLong", "integer64", "INTEGER"),
    FieldType("double", "双精度", "Double", "double", "REAL", 18, 2),
    FieldType("date", "日期", "Date", "date", "DATE"),
    FieldType("datetime", "日期时间", "DateTime", "datetime", "DATETIME"),
    FieldType("bool", "布尔", "Bool", "boolean", "BOOLEAN"),
    # 「字典」按文本列建，见模块头
    FieldType("dict", "字典", "String", "string", "TEXT", 20, 0),
)

#: ``type_key`` → :class:`FieldType`
_BY_KEY: dict[str, FieldType] = {t.key: t for t in FIELD_TYPES}

#: 认得的「类型」写法 → ``type_key``。**除了本模块的标签，还要认原型里那套**
#: （``app.js`` 的 ``CD_DEFAULT_FIELDS`` 用的是 文本/字典/小数/整数/日期）与外部数据集的
#: QGIS 类型名（``String`` / ``Real`` / ``Boolean`` …），模板文件是人手填的，不能太挑。
_TYPE_ALIASES: dict[str, str] = {
    **{t.label: t.key for t in FIELD_TYPES},
    **{t.key: t.key for t in FIELD_TYPES},
    **{t.type_name.lower(): t.key for t in FIELD_TYPES},
    # 原型 / 常见叫法
    "整数": "integer",
    "短整型": "integer",
    "小数": "double",
    "浮点": "double",
    "双精度浮点": "double",
    "数值": "double",
    "字符串": "text",
    "字符": "text",
    "长文本": "text",
    "布尔值": "bool",
    "逻辑": "bool",
    "时间": "datetime",
    "整数64": "integer64",
    "int": "integer",
    "bool": "bool",
    "string": "text",
    "real": "double",
    "float": "double",
}

#: :meth:`QgsField.typeName` → ``type_key``（从图层复制字段结构时用）
_QGS_TYPE_NAMES: dict[str, str] = {
    "string": "text",
    "integer": "integer",
    "integer64": "integer64",
    "longlong": "integer64",
    "real": "double",
    "double": "double",
    "date": "date",
    "datetime": "datetime",
    "time": "datetime",
    "bool": "bool",
    "boolean": "bool",
    "binary": "text",
}


@dataclass(frozen=True)
class FieldSpec:
    """一个待建字段的结构定义（界面收集，服务消费）。

    **全部字段都是 ``str`` / ``int`` / ``bool``** —— 界面的整张字段表就是
    ``tuple[FieldSpec, ...]``，它会被塞进 :class:`CreateDataSpec` / :class:`CreateLayerInDbSpec`，
    而对话框的「应用」判定（``_already_applied``）是按 ``==`` 比 spec 的，
    frozen dataclass 里只要有一个不可哈希的成员（list / dict / QgsField），整条链就废了。
    """

    #: 数据源里的字段名（必填）
    name: str
    #: 显示别名（图层级；能否进数据源随格式而定，见模块头）
    alias: str = ""
    #: 类型键（见 :data:`FIELD_TYPES`）
    type_key: str = "text"
    #: 字段长度（0 = 用类型的默认值）
    length: int = 0
    #: 小数位数（0 = 用类型的默认值）
    precision: int = 0
    #: 是否可为空（``False`` → 库内 NOT NULL / 图层 NotNull 约束）
    nullable: bool = True
    #: 是否可见（``False`` → 隐藏编辑控件）
    visible: bool = True
    #: 字典表名（仅 ``type_key == "dict"`` 有意义，存图层自定义属性）
    dict_table: str = ""
    #: 字典域（同上，如「6项」）
    dict_domain: str = ""

    @property
    def field_type(self) -> FieldType:
        """本字段对应的类型元信息（认不出的键按文本处理）。"""
        return _BY_KEY.get(self.type_key, FIELD_TYPES[0])

    @property
    def effective_length(self) -> int:
        """实际要用的长度：填 0 时用类型的默认值。"""
        return self.length or self.field_type.default_length

    @property
    def effective_precision(self) -> int:
        """实际要用的小数位数：填 0 时用类型的默认值。"""
        return self.precision or self.field_type.default_precision


def type_for_label(label: str) -> FieldType | None:
    """把界面 / 模板里的类型写法解析成 :class:`FieldType`（认不出返回 ``None``）。"""
    key = _TYPE_ALIASES.get(str(label or "").strip().lower())
    return _BY_KEY.get(key) if key else None


def is_dict_field(spec: FieldSpec) -> bool:
    """是不是「字典」字段（按文本列建，但带字典表 / 字典域）。"""
    return spec.type_key == "dict"


# --------------------------------------------------------------------- 映射


def qgs_field(spec: FieldSpec):
    """:class:`FieldSpec` → :class:`QgsField`（含别名与 NotNull 约束）。

    长度 / 小数位数按 :attr:`FieldSpec.effective_length` 补默认值 —— 界面上留空（0）
    的字段建出来也要是个像样的 ``string(50)``，与「添加字段」对话框的行为一致。
    """
    from qgis.core import QgsField, QgsFieldConstraints
    from qgis.PyQt.QtCore import QVariant

    ftype = spec.field_type
    field = QgsField(
        spec.name,
        getattr(QVariant, ftype.qvariant),
        ftype.type_name,
        spec.effective_length,
        spec.effective_precision,
    )
    if spec.alias:
        field.setAlias(spec.alias)
    if not spec.nullable:
        # QGIS 4.2 的签名是 setConstraint(constraint, origin=ConstraintOriginLayer)
        constraints = field.constraints()
        constraints.setConstraint(QgsFieldConstraints.Constraint.ConstraintNotNull)
        field.setConstraints(constraints)
    return field


def sqlite_type(spec: FieldSpec) -> str:
    """:class:`FieldSpec` → 库内 SQL 列类型声明（含长度与 ``NOT NULL``）。

    类型名与 GDAL 写 gpkg 时用的保持一致（``TEXT(n)`` / ``REAL`` / ``DATE`` /
    ``DATETIME`` / ``BOOLEAN``），另两种整型统一成 ``INTEGER``（GDAL 会写
    ``MEDIUMINT``，那是它的存储优化，不是语义）。**NOT NULL 是直接拼进 DDL 的**，
    所以这条在库里是真约束（这也是唯一能把「不可为空」真正落盘的路径）。
    """
    ftype = spec.field_type
    if ftype.key in ("text", "dict") and spec.effective_length > 0:
        decl = f"{ftype.sql_type}({spec.effective_length})"
    else:
        decl = ftype.sql_type
    return decl if spec.nullable else f"{decl} NOT NULL"


# --------------------------------------------------------------------- 校验


#: 字段长度上限（与「添加字段」对话框的 ``QSpinBox`` 一致）
MAX_FIELD_LENGTH = 65535

#: 小数位数上限
MAX_FIELD_PRECISION = 20


def to_int(raw: object, maximum: int) -> int:
    """把用户填的东西解析成 ``0..maximum`` 的整数；解析不出按 0（= 用类型默认值）。

    **上限和 :class:`OverflowError` 都得接住**：单元格 / 模板里填 ``1e30`` 甚至 ``inf``
    时，``int()`` 要么算出个荒唐的长度（``TEXT(1000…)``），要么直接抛
    :class:`OverflowError`（``int(float('inf'))``）。而这两个入口都可能是从 Qt 的槽里
    调过来的 —— 异常冒到槽外面在 PyQt6 里会把进程带走（实测），用户只会看到程序没了。
    """
    text = str(raw if raw is not None else "").strip()
    if not text:
        return 0
    try:
        return max(0, min(maximum, int(float(text))))
    except (ValueError, OverflowError):
        logger.info("「%s」不是有效数字，按 0（用类型默认值）处理", text)
        return 0


#: 「全局唯一标识」字段名：成果数据里图斑的唯一标识，与 FGUID / GUID / GlobalID 同类。
#: :mod:`app.ui.tools.feature_attrs` 认得它（新画的要素会自动生成 uuid），所以本工程
#: **创建出来的图层一律自带这个字段**（用户要求，见 :func:`with_mzguid`）。
MZGUID_FIELD_NAME = "mzguid"

#: 自动附带的 ``mzguid`` 字段：文本、长度 36（uuid4 的字符串长度）。
#: **可空**：导入 / 追加链路不一定填它，加 NOT NULL 会把那些写入口堵死。
MZGUID_FIELD = FieldSpec(name=MZGUID_FIELD_NAME, type_key="text", length=36)


def with_mzguid(fields: tuple[FieldSpec, ...] | list[FieldSpec]) -> tuple[FieldSpec, ...]:
    """确保字段结构里有 ``mzguid``（没有就补一个，放在最前面）。

    用户自己已经建了 ``mzguid``（不论什么类型）就用他的，不重复添加 —— 但**不能没有**：
    成果数据靠这个字段串图斑身份，创建时漏了它，后续汇总 / 比对全得返工。
    """
    if any(str(spec.name).strip().lower() == MZGUID_FIELD_NAME for spec in fields):
        return tuple(fields)
    return (MZGUID_FIELD, *fields)


def validate_fields(fields: tuple[FieldSpec, ...] | list[FieldSpec]) -> str:
    """校验字段表；返回空串表示通过，否则返回要展示给用户的文案。

    四条文案**逐字沿用原型**（``app.js`` 的 ``cdValidateFields``）—— 用户按文案对照验收，
    顺序也照原型：空名 → 数字开头 → 特殊字符 → 重名。原型里「空名」是 toast、
    其余是弹窗，本工程统一由对话框的底部提示条展示，文案不变。
    """
    seen: set[str] = set()
    for spec in fields:
        name = spec.name.strip()
        if not name:
            return "存在空字段名，请删除该行"
        if _LEADING_DIGIT.match(name):
            return f'字段名"{name}"不能以数字开头，请修改。'
        if _ILLEGAL_FIELD_CHARS.search(name):
            return f'字段名"{name}"包含特殊字符，请修改。'
        if name in seen:
            return f'字段名"{name}"重复'
        seen.add(name)
    return ""


# --------------------------------------------------------------------- 图层互转


def _match_index(layer_fields, spec: FieldSpec) -> int:
    """单个 spec 对到图层列上（只按名字，见 :func:`match_layer_fields`）。

    给「只要一个字段」的场合用（如 ``fields_from_layer`` 的落盘提示）；批量配对请走
    :func:`match_layer_fields` —— 它才处理得了重名 / 截断 / 抢列这些情况。
    """
    matched = match_layer_fields(layer_fields, (spec,))
    return matched[0]


def _common_prefix_len(left: str, right: str) -> int:
    """两个字符串的公共前缀长度。"""
    limit = min(len(left), len(right))
    index = 0
    while index < limit and left[index] == right[index]:
        index += 1
    return index


#: 按公共前缀配对时的最低要求。字段名被格式截断时前缀一定很长（Shapefile 是 10 个字符），
#: 取 3 是给「非 ASCII 名字被 GDAL 洗成乱码尾巴」留的余量（``行政区名称备注`` → ``行政区?``
#: 前缀还剩 3 个字），再短就可能是碰巧同名开头、不该硬配。
_MIN_PREFIX_MATCH = 3


def match_layer_fields(layer_fields, specs) -> list[int]:
    """把一份字段结构对到图层的列上：返回每个 spec 的列号（``-1`` = 没配上）。

    **第一趟按名字精确匹配；剩下的按「最长公共前缀」配对，且一列只能被占一次。**
    字段名会被格式改写（实测）：Shapefile 只留 10 个字符（``CHECK_RESULT`` →
    ``CHECK_RESU``），非 ASCII 的还可能被 GDAL 洗成乱码尾巴（``行政区名称备注`` →
    ``行政区?``）。早先这里是「扫到第一个是前缀的就配」，于是
    ``AREA_HA_TOTAL`` 会被配到 ``AREA_HA`` 上 —— 别名 / 不可为空 / 隐藏全打错列，
    还把本该属于前面那个字段的东西覆盖掉（实测复现过）。取最长公共前缀 + 占位去重
    能同时挡住这两种错配。
    """
    matched = [-1] * len(specs)
    claimed: set[int] = set()

    for position, spec in enumerate(specs):
        index = layer_fields.indexOf(spec.name)
        if index >= 0 and index not in claimed:
            matched[position] = index
            claimed.add(index)

    for position, spec in enumerate(specs):
        if matched[position] >= 0 or not spec.name:
            continue
        best, best_score = -1, 0
        for index in range(layer_fields.size()):
            if index in claimed:
                continue
            actual = layer_fields.at(index).name()
            if not actual:
                continue
            score = _common_prefix_len(spec.name, actual)
            if score > best_score:
                best, best_score = index, score
        if best >= 0 and best_score >= _MIN_PREFIX_MATCH:
            matched[position] = best
            claimed.add(best)
    return matched


def fields_from_layer(layer: QgsVectorLayer) -> list[FieldSpec]:
    """从已有图层复制字段结构（「结构来源 → 外部数据集」）。

    带出：名字 / 别名 / 类型 / 长度 / 小数位数 / 是否可为空 / 是否可见；
    字典信息优先看图层自定义属性；没有时读本地字典 / GDB、GPKG 属性域
    （见 :mod:`app.core.util.dict_values`）。
    """
    from qgis.core import QgsFieldConstraints

    from app.core.util.dict_values import layer_dict_fields
    from app.core.util.text_encoding import repair_mojibake

    #: 字段名 → 字典域（本地字典 / GDB、GPKG 属性域都算）
    dict_map = {name: info for name, info in layer_dict_fields(layer)}
    specs: list[FieldSpec] = []
    layer_fields = layer.fields()
    for i in range(layer_fields.size()):
        fld = layer_fields.at(i)
        not_null = bool(
            fld.constraints().constraints() & QgsFieldConstraints.Constraint.ConstraintNotNull
        )
        info = dict_map.get(fld.name())
        dict_table = (
            layer.customProperty(_DICT_TABLE_PROP + fld.name(), "") or ""
        ) or (info.table if info is not None else "")
        dict_domain = (
            layer.customProperty(_DICT_DOMAIN_PROP + fld.name(), "") or ""
        ) or (info.domain if info is not None else "")
        specs.append(
            FieldSpec(
                name=fld.name(),
                alias=repair_mojibake(fld.alias()),
                # 带字典信息的按「字典」还原：库里就是个文本列，类型是从自定义属性认回来的，
                # 不然「复制字段结构」过来会把字典字段悄悄降级成普通文本
                type_key="dict" if (dict_table or dict_domain) else _QGS_TYPE_NAMES.get(fld.typeName().lower(), "text"),
                length=fld.length(),
                precision=fld.precision(),
                nullable=not not_null,
                visible=fld.editorWidgetSetup().type() != "Hidden",
                dict_table=dict_table,
                dict_domain=dict_domain,
            )
        )
    return specs


def configure_created_layer(
    layer: QgsVectorLayer,
    fields: tuple[FieldSpec, ...] | list[FieldSpec],
    *,
    in_db: bool,
) -> tuple[str, ...]:
    """建完图层后的收尾：把数据源存不下的字段信息落在图层上，并如实报出没落下的部分。

    **在调用方（对话框）拿到图层后调一次**，不在创建服务里 —— 别名 / 不可为空 / 隐藏
    都是**工程状态**（跟着工程保存、不跟数据源走），而创建服务的约定是「只落盘、
    不碰工程状态」（见 ``service.py`` 模块头）。

    打三样东西，都只影响这个图层对象、不动数据源：

    - **别名** ``setFieldAlias``：Shapefile / SpatiaLite 之类不存别名，落在这里至少
      当前工程里属性表 / 表单显示的是中文别名（QGIS 的既有行为就是图层级别名，见
      ``layer_properties/fields.py`` 里的同名注释）；
    - **不可为空** ``setFieldConstraint``：OGR 写盘不带 NOT NULL，落成图层约束后
      编辑时是真拦（数据源层面拦不住，这就是「尽力」的边界）；
    - **隐藏** ``setEditorWidgetSetup("Hidden")``：读侧是
      ``app/ui/dialogs/attribute_entry.py`` 里那句 ``editorWidgetSetup().type() == "Hidden"``；
    - **字典表 / 字典域**：图层自定义属性（``create_data/dict_*``），见模块头。

    **顺序要紧**：先按「数据源自己给了什么」判一遍，再动手打补丁 —— 补丁一打上去，
    图层上就看不出数据源原本存没存下了。

    Args:
        layer: 建完回读的图层（还没打补丁的那个）。
        fields: 用户在第二步填的字段结构。
        in_db: 是不是「写入已有本地数据库」那条路（决定「不可为空」要不要提示）。

    Returns:
        出问题的项，每条一句、可直接展示给用户；全都落下了就是空元组。
    """
    from qgis.core import QgsEditorWidgetSetup, QgsFieldConstraints

    if not fields:
        return ()
    layer_fields = layer.fields()
    #: 每个 spec 在图层里的列号（-1 = 压根没进图层）。**先算好再用**：下面一打别名 /
    #: 约束，图层上就看不出「数据源原本存没存下」了。
    matched = match_layer_fields(layer_fields, fields)

    missing = [f"「{spec.name}」" for spec, index in zip(fields, matched) if index < 0]
    lost_alias = [
        f"「{spec.name}」"
        for spec, index in zip(fields, matched)
        if spec.alias and index >= 0 and not layer_fields.at(index).alias()
    ]
    # 数据源把名字改写了（如 Shapefile 截到 10 字符）：别名 / 约束还是打在这一列上，
    # 但用户手上的字段表和数据源已经对不上号了，得说
    renamed = [
        f"{spec.name} → {layer_fields.at(index).name()}"
        for spec, index in zip(fields, matched)
        if index >= 0 and layer_fields.at(index).name() != spec.name
    ]

    for spec, index in zip(fields, matched):
        if index < 0:
            continue
        if spec.alias:
            layer.setFieldAlias(index, spec.alias)
        if not spec.nullable:
            layer.setFieldConstraint(index, QgsFieldConstraints.Constraint.ConstraintNotNull)
        if not spec.visible:
            layer.setEditorWidgetSetup(index, QgsEditorWidgetSetup("Hidden", {}))
        if spec.dict_table:
            layer.setCustomProperty(_DICT_TABLE_PROP + spec.name, spec.dict_table)
        if spec.dict_domain:
            layer.setCustomProperty(_DICT_DOMAIN_PROP + spec.name, spec.dict_domain)

    notes: list[str] = []
    if missing:
        notes.append(
            "、".join(missing)
            + " 没有写进数据源：这个格式的空文件不保存字段结构（字段结构是随要素一起写的），"
            "建完后要往图层里手动添加字段"
        )
    if renamed:
        notes.append(
            "数据源把字段名改写了（格式限制，如 Shapefile 字段名最多 10 个字符）："
            + "、".join(renamed)
            + "。属性表里显示的是改写后的名字"
        )
    if lost_alias:
        notes.append(
            "、".join(lost_alias)
            + " 的字段别名没能写进数据源（这个格式不存别名），只在当前工程里生效"
        )
    if not in_db and any(not s.nullable for s in fields):
        # 库内那条路的 NOT NULL 是 DDL 里的真约束，不用提示；文件这条路才要说清楚
        notes.append(
            "「不可为空」在这里只是本工程里的字段约束：文件格式没地方存它，"
            "用别的软件打开这份数据时不受此约束"
        )
    return tuple(notes)
