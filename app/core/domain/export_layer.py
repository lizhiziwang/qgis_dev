"""把矢量图层导出成文件（支持先按条件筛要素）：数据管理「导出数据」用。

支持**四种**目标（用户点名要的）：Shapefile / FileGDB / GeoPackage / SQLite。
不直接复用创建数据那套：创建是「建一个空图层」，这里是「把已有要素写出去」，
目标已存在时的语义也不同（见下）。

**目标已存在时的语义**（这条错了会毁数据）：

- **容器格式**（``.gdb`` / ``.gpkg`` / ``.sqlite``）已存在 → ``CreateOrOverwriteLayer``：
  只替换**同名的那一个图层**，库里别的图层原样保留。切不可用默认的
  ``CreateOrOverwriteFile`` —— 那是把整个数据集删掉重建（本仓库在创建服务里为同一个坑
  写过说明，见 ``create_data/service.py`` 模块头）。
- **单文件格式**（``.shp``）：``CreateOrOverwriteFile`` 覆盖该文件（含同名附属文件）；
  用户在保存对话框里已经确认过覆盖。
- **写失败**：只有**本次新建**的目标才清掉半成品；覆盖既有文件时绝不删 —— 删了等于
  把用户原来的数据连锅端了。

**按条件筛**：``QgsVectorFileWriter`` 的 ``SaveVectorOptions`` **没有**"按表达式筛"
这种选项（只有「仅选中要素 / 范围过滤」），所以带条件时先把符合条件的要素复制到一个
内存图层再写。不带条件时直接把原图层写出去（零拷贝）。图层的 ``subsetString``
（定义查询）在两种路径下都照样生效。

**Shapefile 字段上限**：DBF 最多 255 个字段，GDAL 照写不误，但 ArcMap 打开超过
上限的文件会直接报错。``ExportSpec.field_names`` 用来只写用户勾选的字段子集；
不传且图层字段超过 :data:`SHAPEFILE_MAX_FIELDS` 时直接抛 :class:`ExportLayerError`
（导出对话框会默认裁到 255 个并让用户确认）。
"""

from __future__ import annotations

import os
import shutil
from dataclasses import dataclass
from typing import TYPE_CHECKING

from app.infra.logging import get_logger

if TYPE_CHECKING:
    from qgis.core import QgsVectorLayer

logger = get_logger(__name__)

#: 导出对话框里提供的四种格式（用户点名要的；顺序即下拉顺序）
EXPORT_EXTENSIONS: tuple[str, ...] = (".shp", ".gdb", ".gpkg", ".sqlite")

#: 容器格式的扩展名（判"目标已存在时是替换图层还是覆盖文件"用）。
#: **别用驱动名判**：FileGDB 的驱动常被解析成 ``OpenFileGDB``，拿驱动名会漏。
_CONTAINER_EXTENSIONS = frozenset({".gdb", ".gpkg", ".sqlite", ".sqlite3", ".db"})

#: Shapefile（dBASE 表）的字段数硬上限。GDAL 会照写不误，但 ArcMap 打开
#: 超过 255 列的 DBF 直接报"无法打开"（用户报过：335 个字段的图层导出后
#: ArcMap 打不开，QGIS 自己能读）。导出前必须把字段裁到上限以内。
SHAPEFILE_MAX_FIELDS = 255


class ExportLayerError(RuntimeError):
    """导出失败。消息可直接展示给用户。"""


@dataclass(frozen=True)
class ExportSpec:
    """一次导出所需的参数（对话框收集，服务消费）。"""

    #: 目标所在目录（已 normpath）
    path: str
    #: 目标文件名（含扩展名，如 ``小班.gpkg``）
    file_name: str
    #: OGR 驱动名（``ESRI Shapefile`` / ``OpenFileGDB`` / ``GPKG`` / ``SQLite``）
    driver: str
    #: 目标扩展名（如 ``.gdb``）——判"是不是容器"用**它**（没给时从 ``file_name`` 推）。
    #: 别用驱动名判：FileGDB 的驱动会被解析成 ``OpenFileGDB``，拿驱动名判会漏
    extension: str = ""
    #: 图层在容器里的名字（单文件格式无所谓）
    layer_name: str = ""
    #: 筛选条件（QGIS 表达式）；空串 = 全部要素
    expression: str = ""
    #: 只导出这些字段（图层字段名）；``None`` = 全部字段。
    #: Shapefile 超过 :data:`SHAPEFILE_MAX_FIELDS` 个字段时，调用方必须给出一个
    #: ≤255 的子集（空元组 = 只导几何、不写属性）。
    field_names: tuple[str, ...] | None = None

    @property
    def file_path(self) -> str:
        """目标文件完整路径（``path`` + ``file_name``）。"""
        return os.path.join(self.path, self.file_name)

    @property
    def is_container(self) -> bool:
        """目标是不是多图层容器（决定"已存在时只换该图层"还是"覆盖文件"）。"""
        ext = str(self.extension or os.path.splitext(self.file_name)[1]).lower()
        return ext in _CONTAINER_EXTENSIONS


def existing_target_description(spec: ExportSpec) -> str:
    """目标"已经存在"时返回一句能直接给用户看的话；不存在返回空串。

    两种情况才算"会覆盖到东西"，调用方据此弹「是否替换」：

    - **单文件格式**：文件本身已存在（覆盖会毁掉原文件）；
    - **容器格式**：文件存在**且里面有同名图层**（会替换那个图层）。
      容器存在但没有同名图层时只是"加一个图层"，不用拦。
    """
    if not os.path.exists(spec.file_path):
        return ""
    if not spec.is_container:
        return f"文件已存在：{spec.file_path}"
    try:
        from app.core.util.sublayers import list_sublayers

        names = [item.name for item in list_sublayers(spec.file_path)]
    except Exception as exc:  # noqa: BLE001 —— 列不出来时按"不冲突"处理，导出那步会报真错
        logger.warning("检查导出目标时列不出 %s 的图层：%s", spec.file_path, exc)
        return ""
    if spec.layer_name.strip() in names:
        return (
            f"「{os.path.basename(spec.file_path)}」里已有同名图层"
            f"「{spec.layer_name.strip()}」"
        )
    return ""


def export_vector_layer(layer: QgsVectorLayer, spec: ExportSpec) -> str:
    """把 ``layer`` 导出到 ``spec`` 指定的目标，返回写出的文件路径。

    Args:
        layer: 要导出的图层（可以是本地库里的表 —— 打开它的地方自己负责）。
        spec: 导出参数。

    Returns:
        实际写出的文件（或 ``.gdb`` 目录）路径。

    Raises:
        ExportLayerError: 条件非法 / 驱动不可用 / 目标无效 / 写盘失败。
    """
    if not spec.file_name:
        raise ExportLayerError("请填写导出的文件名")
    is_container = spec.is_container
    if is_container and not spec.layer_name.strip():
        raise ExportLayerError("导出到库/地理数据库时，图层名不能为空")

    source = _prepare_source(layer, spec)
    target = spec.file_path
    existed = os.path.exists(target)

    from qgis.core import (
        QgsCoordinateTransformContext,
        QgsVectorFileWriter,
    )

    options = QgsVectorFileWriter.SaveVectorOptions()
    options.driverName = spec.driver
    options.layerName = spec.layer_name.strip() or os.path.splitext(spec.file_name)[0]
    options.fileEncoding = "UTF-8"
    # 容器已存在 → 只替换同名图层；否则（新文件 / 单文件格式）整份写出。
    # **默认的 CreateOrOverwriteFile 会整库删掉重建**，绝不能用在已有的库上。
    if existed and is_container:
        options.actionOnExistingFile = (
            QgsVectorFileWriter.ActionOnExistingFile.CreateOrOverwriteLayer
        )

    try:
        error, errmsg, _new, _saved = QgsVectorFileWriter.writeAsVectorFormatV3(
            source, target, QgsCoordinateTransformContext(), options
        )
    except OSError as exc:
        if not existed:
            _cleanup(target)
        raise ExportLayerError(f"导出失败：{target}\n{exc}") from exc

    if error != QgsVectorFileWriter.WriterError.NoError:
        # 只清理**本次新建**的半成品；覆盖既有文件时删掉会把用户原数据毁掉
        if not existed:
            _cleanup(target)
        raise ExportLayerError(f"导出失败：{errmsg or '未知原因'}")

    logger.info(
        "已导出图层 %s → %s（%s · %s）",
        layer.name(),
        target,
        spec.driver,
        spec.expression or "全部要素",
    )
    return target


def shapefile_field_names(fields) -> dict[str, str]:
    """Shapefile 导出时每个字段最终的名字（``{原名: 导出名}``，没改的原样映射）。

    **为什么要在导出前自己改**：DBF 的字段名只有 10 个字节，GDAL 按**字节**截断，
    中文名会被从中间切断（``图斑编号`` → ``图斑编`` + 一个残缺字节），生成的 DBF
    字段名不是合法 UTF-8 —— QGIS 能容忍，**ArcMap 打开就报错**（用户报的）。
    这里按「字符」截到 ≤10 字节、去重、保证以字母开头，交出去的名字一定是合法的。

    规则：

    - 只保留字母 / 数字 / 下划线（其余换成 ``_``），去掉首尾下划线；
    - 按 UTF-8 字节数 ≤10 截断，**不切多字节字符**；
    - 首字符不是 ASCII 字母时前缀 ``F``（DBF 规范要求以字母开头）；
    - 重名（不分大小写）追加 ``_1`` / ``_2``。
    """
    used: set[str] = set()
    mapping: dict[str, str] = {}
    for field in fields:
        original = str(field.name())
        cleaned = "".join(
            ch if (ch.isalnum() or ch == "_") else "_" for ch in original
        ).strip("_")
        if not cleaned:
            cleaned = "FIELD"
        # 首字符必须是 ASCII 字母：中文名的第一个字也算「不是字母」
        prefix = "" if cleaned[0].isascii() and cleaned[0].isalpha() else "F"
        budget = 10 - len(prefix.encode("utf-8"))
        body = ""
        for ch in cleaned:
            if len((body + ch).encode("utf-8")) > budget:
                break
            body += ch
        candidate = f"{prefix}{body}" or "FIELD"
        base = candidate
        index = 1
        while candidate.lower() in used:
            suffix = f"_{index}"
            index += 1
            trimmed = ""
            for ch in base:
                if len((trimmed + ch + suffix).encode("utf-8")) > 10:
                    break
                trimmed += ch
            candidate = f"{trimmed or 'F'}{suffix}"
        used.add(candidate.lower())
        mapping[original] = candidate
    return mapping


def _shapefile_copy(
    layer: QgsVectorLayer,
    features,
    names: dict[str, str],
    fields,
) -> QgsVectorLayer:
    """建一份字段名已按 Shapefile 规则改好的内存副本（要素原样带过去）。

    只有**字段/名字真的变了**才复制；没变时不复制（零拷贝）。``features`` 为
    ``None`` 表示没做筛选，这里现取全部要素；``fields`` 是要写出的源字段子集。
    """
    if _is_full_unchanged(layer, fields, names):
        return layer
    return _copy_to_memory(layer, features, fields, names)


def _is_full_unchanged(layer: QgsVectorLayer, fields, names: dict[str, str]) -> bool:
    """``fields`` 就是图层全部字段、且没有任何一个被改名（→ 可零拷贝直写）。"""
    layer_fields = list(layer.fields())
    if len(fields) != len(layer_fields):
        return False
    for original, field in zip(layer_fields, fields):
        if original.name() != field.name():
            return False
        if names.get(field.name(), field.name()) != field.name():
            return False
    return True


def _selected_fields(layer: QgsVectorLayer, field_names) -> list:
    """要写出的字段（源 ``QgsField`` 对象，按调用方给的顺序）。

    ``field_names is None`` → 图层全部字段；空元组 → 一个字段都不写（只导几何）。
    认不出的名字直接忽略（调用方一般来自字段勾选，正常不会出现）。
    """
    all_fields = list(layer.fields())
    if field_names is None:
        return all_fields
    by_name = {field.name(): field for field in all_fields}
    return [by_name[name] for name in field_names if name in by_name]


def _copy_to_memory(
    layer: QgsVectorLayer,
    features,
    fields,
    renamed: dict[str, str],
) -> QgsVectorLayer:
    """把 ``fields``（源字段子集，可改名）与要素复制到内存图层。

    ``features=None`` 表示没做筛选（现取全部）。属性按**源字段下标**逐个搬运：
    不能把源要素直接塞进字段数不同的图层（按位置对齐，会错列）。
    """
    from qgis.core import QgsFeature, QgsField, QgsVectorLayer

    wkb_name = _wkb_name(layer)
    crs_text = layer.crs().authid() or layer.crs().toProj()
    memory = QgsVectorLayer(f"{wkb_name}?crs={crs_text}", layer.name(), "memory")
    if not memory.isValid():
        raise ExportLayerError("建不出导出用的内存图层（图层没有几何类型？）")

    source_fields = layer.fields()
    source_indices: list[int] = []
    target_fields = []
    for field in fields:
        copy = QgsField(field)
        renamed_name = renamed.get(field.name())
        if renamed_name and renamed_name != field.name():
            copy.setName(renamed_name)
        target_fields.append(copy)
        source_indices.append(source_fields.indexOf(field.name()))

    provider = memory.dataProvider()
    provider.addAttributes(target_fields)
    memory.updateFields()
    if features is None:
        features = list(layer.getFeatures())
    if not features:
        return memory

    if _is_full_unchanged(layer, fields, {}):
        # 字段原样、只是筛了要素：源要素与目标结构一一对应，直接写
        if not provider.addFeatures(features):
            raise ExportLayerError("把要素复制到导出用的内存图层时失败")
        return memory

    copies = []
    for feature in features:
        source_attrs = feature.attributes()
        copy = QgsFeature(memory.fields())
        try:
            copy.setGeometry(feature.geometry())
            copy.setAttributes([source_attrs[index] for index in source_indices])
        except (IndexError, KeyError):
            # 字段结构对不上（图层在导出过程中被改过）——跳过这条，别整批崩
            logger.warning("导出：要素 %s 的属性列对不上，已跳过", feature.id())
            continue
        copies.append(copy)
    if copies and not provider.addFeatures(copies):
        raise ExportLayerError("把要素复制到导出用的内存图层时失败")
    return memory


def _prepare_source(layer: QgsVectorLayer, spec: ExportSpec) -> QgsVectorLayer:
    """要写给 OGR 的那份图层：按需筛要素 / 选字段 / 改 Shapefile 字段名。

    除 Shapefile 的字段名改写外，都不复制图层（零拷贝）；需要复制时走
    :func:`_copy_to_memory`。
    """
    expression = (spec.expression or "").strip()
    features = None
    if expression:
        from qgis.core import QgsExpression, QgsFeatureRequest

        expr = QgsExpression(expression)
        if expr.hasParserError():
            raise ExportLayerError(f"筛选条件有语法错误：{expr.parserErrorString()}")
        names = {field.name() for field in layer.fields()}
        unknown = [name for name in expr.referencedColumns() if name not in names]
        if unknown:
            raise ExportLayerError("筛选条件引用了不存在的字段：" + "、".join(unknown))
        features = list(layer.getFeatures(QgsFeatureRequest().setFilterExpression(expression)))
        logger.info("导出筛选：%s → 命中 %d 个要素", expression, len(features))

    fields = _selected_fields(layer, spec.field_names)
    if spec.driver == "ESRI Shapefile":
        if len(fields) > SHAPEFILE_MAX_FIELDS:
            raise ExportLayerError(
                f"Shapefile 最多支持 {SHAPEFILE_MAX_FIELDS} 个字段，当前要导出 "
                f"{len(fields)} 个。请减少导出字段，或改用 FileGDB / GeoPackage。"
            )
        names = shapefile_field_names(fields)
        return _shapefile_copy(layer, features, names, fields)

    if features is None and spec.field_names is None:
        return layer
    return _copy_to_memory(layer, features, fields, {})


def _wkb_name(layer: QgsVectorLayer) -> str:
    """图层的几何类型名（内存图层 URI 用）；无几何的图层用 ``None``。"""
    from qgis.core import QgsWkbTypes

    wkb = layer.wkbType()
    if wkb == QgsWkbTypes.Type.NoGeometry:
        return "None"
    return QgsWkbTypes.displayString(wkb) or "Unknown"


def _cleanup(path: str) -> None:
    """清掉导出失败留下的半成品（**只对本次新建的目标调用**）。"""
    try:
        if os.path.isdir(path):
            shutil.rmtree(path, ignore_errors=True)
        elif os.path.exists(path):
            os.remove(path)
    except OSError as exc:
        logger.warning("清理导出失败的产物 %s 时出错：%s", path, exc)


__all__ = [
    "EXPORT_EXTENSIONS",
    "ExportLayerError",
    "ExportSpec",
    "existing_target_description",
    "export_vector_layer",
    "shapefile_field_names",
]
