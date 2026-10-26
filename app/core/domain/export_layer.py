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

import json
import os
import re
import shutil
import sqlite3
from dataclasses import dataclass
from typing import TYPE_CHECKING

from app.core.util.ogr_fields import PROVIDER_ID_NAMES, provider_id_field_indices
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
    #: 只导出图层当前**选中**的要素。与 ``expression`` 互斥（同时给出时以本项为准）。
    selected_only: bool = False
    #: 只导出这些字段（图层字段名）；``None`` = 全部字段。
    #: Shapefile 超过 :data:`SHAPEFILE_MAX_FIELDS` 个字段时，调用方必须给出一个
    #: ≤255 的子集（空元组 = 只导几何、不写属性）。
    field_names: tuple[str, ...] | None = None
    #: 与 ``field_names`` 一一对应的**导出字段名**（可改名）；``None`` = 用原字段名。
    #: 目标驱动支持时由 ``QgsVectorFileWriter.attributesExportNames`` 写入。
    field_export_names: tuple[str, ...] | None = None
    #: 是否把字段挂接的字典一起导出：
    #: - FileGDB → 写成 GDB 的**属性域**（coded value domain）并挂到字段上；
    #: - GeoPackage / SQLite → 在目标库里附带字典表，并写字段元数据；
    #: - Shapefile 不支持，调用方应禁用这个选项。
    convert_dict: bool = False

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


def export_vector_layer(
    layer: QgsVectorLayer,
    spec: ExportSpec,
    *,
    progress=None,
) -> str:
    """把 ``layer`` 导出到 ``spec`` 指定的目标，返回写出的文件路径。

    Args:
        layer: 要导出的图层（可以是本地库里的表 —— 打开它的地方自己负责）。
        spec: 导出参数。
        progress: 可选进度回调 ``callable(str)``；写图层 / 写字典阶段各报一次
            文字（GDB 大图层导出可能要几十秒，界面靠它显示“正在做什么”）。

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
    if (
        spec.driver == "ESRI Shapefile"
        and spec.field_names is not None
        and len(spec.field_names) > SHAPEFILE_MAX_FIELDS
    ):
        raise ExportLayerError(
            f"Shapefile 最多支持 {SHAPEFILE_MAX_FIELDS} 个字段，当前要导出 "
            f"{len(spec.field_names)} 个。请减少导出字段，或改用 FileGDB / GeoPackage。"
        )

    source = _prepare_source(layer, spec)
    # 字段选择 / 改名：OGR 写入器自带 attributes / attributesExportNames，
    # 不用再复制图层；映射同时用于字典属性域与别名落库（按导出后的列名写）。
    export_map = _export_name_map(layer, spec)
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
    _apply_field_options(options, source, spec, export_map)
    # 不附带字典表的格式（Excel / SHP / GeoJSON / KML）：勾选「转换字典」时
    # 把代码值换成字典名称；GDB / GPKG / SQLite 仍走属性域 / 字典表那套。
    value_converter = None
    if spec.convert_dict and spec.driver not in (
        "FileGDB",
        "OpenFileGDB",
        "GPKG",
        "SQLite",
    ):
        value_converter = _make_dict_value_converter(layer)
        if value_converter is not None:
            options.fieldValueConverter = value_converter
    # 容器已存在 → 只替换同名图层；否则（新文件 / 单文件格式）整份写出。
    # **默认的 CreateOrOverwriteFile 会整库删掉重建**，绝不能用在已有的库上。
    if existed and is_container:
        options.actionOnExistingFile = (
            QgsVectorFileWriter.ActionOnExistingFile.CreateOrOverwriteLayer
        )

    # GDB 读出来的中文别名可能是「UTF-8 被当成 Latin-1」的乱码；属性表显示时
    # 会修（``feature_attrs.field_alias``），导出写盘时也必须修，否则目标里
    # 的字段中文还是乱码。写完后把原图层的别名还原，避免悄悄改用户图层。
    alias_restore = _repair_source_aliases(source)
    if progress is not None:
        progress("正在写入图层…")
    try:
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
    finally:
        _restore_aliases(source, alias_restore)

    logger.info(
        "已导出图层 %s → %s（%s · %s）",
        layer.name(),
        target,
        spec.driver,
        "选中要素" if spec.selected_only else (spec.expression or "全部要素"),
    )
    if spec.convert_dict:
        if progress is not None:
            progress("正在写入字典…")
        try:
            _export_dictionaries(layer, spec, target, export_map)
        except Exception as exc:  # noqa: BLE001 —— 数据已写成功，字典失败不毁结果
            logger.warning("导出字典失败（图层数据已写出）：%s", exc, exc_info=True)
    try:
        _store_export_aliases(layer, spec, target, export_map)
    except Exception as exc:  # noqa: BLE001 —— 别名失败不毁已写出的数据
        logger.warning("导出字段别名失败（图层数据已写出）：%s", exc, exc_info=True)
    if progress is not None:
        progress("导出完成")
    return target


def _store_export_aliases(layer, spec: ExportSpec, target: str, name_map=None) -> None:
    """把字段别名写进导出的 GPKG / SQLite。

    Shapefile 没有别名承载位置；其它单文件格式也不处理。SQLite 目标写本工程
    的 ``qgis_dev_column_alias`` 表，GPKG 写标准 ``gpkg_data_columns``。
    字段被改名时按**导出后的列名**记别名（``name_map`` = 原名 → 导出名）。
    """
    from app.core.util.local_db_util import local_db_util
    from app.core.util.text_encoding import repair_mojibake

    ext = str(spec.extension or os.path.splitext(spec.file_name)[1]).lower()
    if ext not in (".gpkg", ".sqlite", ".sqlite3", ".db"):
        return
    mapping = name_map or {}
    wanted = set(spec.field_names) if spec.field_names is not None else None
    aliases: dict[str, str] = {}
    for field in layer.fields():
        name = field.name()
        if wanted is not None and name not in wanted:
            continue
        alias = repair_mojibake(field.alias() or "")
        if alias:
            aliases[mapping.get(name, name)] = alias
    if not aliases:
        return
    layer_name = spec.layer_name.strip() or os.path.splitext(spec.file_name)[0]
    db = local_db_util(target)
    try:
        # SQLite 驱动可能把表名 / 列名改成小写：按目标真实名字写别名
        actual_layer = layer_name
        actual_cols: dict[str, str] = {}
        try:
            row = db.cursor.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table' "
                "AND lower(name) = lower(?)",
                (layer_name,),
            ).fetchone()
            if row is not None:
                actual_layer = str(row[0])
            quoted = '"' + actual_layer.replace('"', '""') + '"'
            for info_row in db.cursor.execute(f"PRAGMA table_info({quoted})"):
                actual_cols[str(info_row[1]).casefold()] = str(info_row[1])
        except Exception:  # noqa: BLE001
            pass
        resolved = {
            actual_cols.get(name.casefold(), name): alias
            for name, alias in aliases.items()
        }
        if db.kind == "gpkg":
            for column, alias in resolved.items():
                db.set_gpkg_column_alias(actual_layer, column, alias)
        else:
            db.set_column_aliases(actual_layer, resolved)
        db.conn.commit()
    finally:
        db.conn.close()


# --------------------------------------------------------------------- 字典导出


def _export_dictionaries(layer, spec: ExportSpec, target: str, name_map=None) -> None:
    """按格式把字段挂接的字典导出（GDB 写属性域，GPKG/SQLite 写字典表）。"""
    mapping = name_map or {}
    fields = [
        (mapping.get(name, name), info)
        for name, info in _dict_fields_for_export(layer, spec)
    ]
    if not fields:
        logger.warning("导出字典：图层没有挂接字典的字段，已跳过")
        return
    ext = str(spec.extension or os.path.splitext(spec.file_name)[1]).lower()
    if ext == ".gdb":
        _export_gdb_domains(fields, spec, target)
    elif ext in (".gpkg", ".sqlite", ".sqlite3", ".db"):
        _export_dict_tables(layer, fields, spec, target)
    else:
        logger.info("导出字典：%s 不支持携带字典，已跳过", ext or spec.driver)


def _dict_fields_for_export(layer, spec: ExportSpec):
    """图层里挂了字典、且本次确实要导出的字段：``[(字段名, FieldDict), ...]``。"""
    from app.core.util.dict_values import layer_dict_fields

    wanted = set(spec.field_names) if spec.field_names is not None else None
    return [
        (name, info)
        for name, info in layer_dict_fields(layer)
        if wanted is None or name in wanted
    ]


def _domain_token(info) -> str:
    """GDB 属性域名：``dict_<字典表>_<域>``，只保留词字符。"""
    raw = f"{info.table}_{info.domain}"
    token = re.sub(r"[^\w]+", "_", str(raw), flags=re.UNICODE).strip("_")
    return ("dict_" + token)[:60] or "dict_domain"


def _export_gdb_domains(fields, spec: ExportSpec, target: str) -> None:
    """FileGDB：把字典域写成 coded value domain 并挂到对应字段上。"""
    try:
        from osgeo import ogr
    except ImportError as exc:  # pragma: no cover —— QGIS 自带 GDAL 绑定
        raise ExportLayerError("本机没有 GDAL Python 绑定，无法写入 GDB 属性域") from exc

    driver = ogr.GetDriverByName(spec.driver or "OpenFileGDB")
    if driver is None:
        driver = ogr.GetDriverByName("OpenFileGDB")
    ds = driver.Open(target, 1) if driver is not None else None
    if ds is None:
        raise ExportLayerError(f"打不开导出后的 GDB：{target}")
    try:
        layer = ds.GetLayerByName(spec.layer_name.strip())
        if layer is None:
            layer = ds.GetLayer(0)
        if layer is None:
            raise ExportLayerError("GDB 里找不到刚导出的图层")
        existing = set(ds.GetFieldDomainNames() or ())
        used_names: set[str] = set()
        #: (字典表, 字典域) → 已创建的 GDB 属性域名（多个字段共用同一字典时复用）
        domain_names: dict[tuple[str, str], str] = {}
        for field_name, info in fields:
            index = layer.GetLayerDefn().GetFieldIndex(field_name)
            if index < 0:
                logger.warning("GDB 属性域：图层里没有字段 %s，已跳过", field_name)
                continue
            key = (info.table, info.domain)
            name = domain_names.get(key, "")
            if not name:
                enum: dict = {}
                field_type = layer.GetLayerDefn().GetFieldDefn(index).GetType()
                for value in info.values:
                    if not value.code:
                        continue
                    if field_type in (ogr.OFTInteger, ogr.OFTInteger64):
                        try:
                            enum[int(value.code)] = value.name
                        except (TypeError, ValueError):
                            logger.warning(
                                "字典域 %s 的代码 %r 不是整数，无法挂到整型字段 %s",
                                info.domain,
                                value.code,
                                field_name,
                            )
                    else:
                        enum[str(value.code)] = value.name
                if not enum:
                    continue
                name = _domain_token(info)
                if name in used_names:
                    suffix = 2
                    while f"{name}_{suffix}" in used_names:
                        suffix += 1
                    name = f"{name}_{suffix}"
                used_names.add(name)
                domain = ogr.CreateCodedFieldDomain(
                    name,
                    f"{info.table} / {info.domain}",
                    field_type,
                    ogr.OFSTNone,
                    enum,
                )
                if name in existing:
                    ds.UpdateFieldDomain(domain)
                else:
                    added = ds.AddFieldDomain(domain)
                    if not added:
                        logger.warning("GDB 属性域 %s 写入失败", name)
                        continue
                    existing.add(name)
                domain_names[key] = name
            original = layer.GetLayerDefn().GetFieldDefn(index)
            new_defn = ogr.FieldDefn(original.GetName(), original.GetType())
            with_meta = (
                ("SetWidth", original.GetWidth()),
                ("SetPrecision", original.GetPrecision()),
                ("SetSubType", original.GetSubType()),
            )
            for method, value in with_meta:
                try:
                    getattr(new_defn, method)(value)
                except (AttributeError, TypeError):
                    pass
            new_defn.SetDomainName(name)
            if layer.AlterFieldDefn(index, new_defn, ogr.ALTER_DOMAIN_FLAG) != ogr.OGRERR_NONE:
                logger.warning("GDB 字段 %s 挂接属性域 %s 失败", field_name, name)
        logger.info("已把 %d 个字典域写入 GDB 属性域：%s", len(used_names), target)
    finally:
        ds = None


def _local_dict_source_table(layer, info) -> str:
    """图层所在库里 ``info.table`` 对应的字典表名（没有返回空串）。"""
    from app.core.util.local_layer_loader import layer_source_container

    container = ""
    try:
        container = str(layer_source_container(layer) or "")
    except Exception:  # noqa: BLE001
        return ""
    if not container or not os.path.isfile(container):
        return ""
    try:
        conn = sqlite3.connect(container, timeout=5.0)
    except sqlite3.Error:
        return ""
    try:
        if info.local_table:
            row = conn.execute(
                "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?",
                (info.local_table,),
            ).fetchone()
            if row is not None:
                return info.local_table
        try:
            rows = conn.execute(
                "SELECT table_name, attr FROM user_relation WHERE type = 'dict'"
            ).fetchall()
        except sqlite3.Error:
            return ""
        import json

        for table, attr in rows:
            table = str(table or "")
            if table == info.table:
                return table
            try:
                data = json.loads(attr) if attr else {}
            except (TypeError, ValueError):
                data = {}
            if isinstance(data, dict) and str(data.get("dict_table") or "") == info.table:
                return table
        return ""
    finally:
        conn.close()


def _export_dict_tables(layer, fields, spec: ExportSpec, target: str) -> None:
    """GPKG / SQLite：把字典表写进目标库，并写字段元数据（打开后能显示名称）。"""
    from app.core.domain.create_data.dict_tables import (
        DICT_COLUMNS,
        DICT_INSERT_COLUMNS,
        DICT_SCHEMA_VERSION,
        DictPlan,
        DictSource,
        DictTableSpec,
        build_dict_table_specs,
        persist_dict_tables,
    )
    from app.core.domain.dict_manage import DictService
    from app.core.util.field_meta import FieldMeta, upsert_field_meta
    from app.core.util.local_db_util import local_db_util

    service = DictService()
    try:
        known = {item.name for item in service.list_categories()}
    except Exception:  # noqa: BLE001
        known = set()
    domains: set[tuple[str, str]] = set()
    sources: list[DictSource] = []
    direct: list[tuple[str, object]] = []
    for field_name, info in fields:
        source_table = _local_dict_source_table(layer, info)
        if source_table:
            from app.core.util.local_layer_loader import layer_source_container

            container = str(layer_source_container(layer) or "")
            sources.append(
                DictSource(
                    db_path=container,
                    table=source_table,
                    category=info.table,
                    domain=info.domain,
                )
            )
        elif info.table in known:
            domains.add((info.table, info.domain))
        else:
            # 直接从 GDB / GPKG 的 OGR 属性域来的字段：没有本地字典表、
            # 也不在应用级字典里，按读到的取值直接生成字典表。
            direct.append((field_name, info))
    plan = DictPlan(domains=frozenset(domains), sources=tuple(sources))
    specs = list(build_dict_table_specs(target, plan, service))
    if direct:
        from app.core.domain.import_layer import unique_table_name

        taken = {item.table_name for item in specs}
        done: set[tuple[str, str]] = set()
        for _field_name, info in direct:
            key = (info.table, info.domain)
            if key in done or not info.values:
                continue
            done.add(key)
            table = unique_table_name(
                target, info.table or info.domain, taken=taken
            )
            taken.add(table)
            rows = tuple(
                (info.domain, value.code, value.name, value.parent_code, index)
                for index, value in enumerate(info.values)
            )
            specs.append(
                DictTableSpec(
                    table_name=table,
                    columns=DICT_COLUMNS,
                    rows=rows,
                    attr=json.dumps(
                        {
                            "schema": DICT_SCHEMA_VERSION,
                            "dict_table": table,
                            "domains": [info.domain],
                        },
                        ensure_ascii=False,
                    ),
                    upsert_key=("domain", "code"),
                    insert_columns=DICT_INSERT_COLUMNS,
                    category=info.table,
                    domains=(info.domain,),
                )
            )
    if not specs:
        logger.warning("导出字典：没有可写入目标库的字典表")
        return
    layer_name = spec.layer_name.strip() or os.path.splitext(spec.file_name)[0]
    db = local_db_util(target)
    try:
        persist_dict_tables(db, specs)
        # OGR 写出的 SQLite 可能把表名 / 列名改成小写：字段元数据必须按
        # **目标里的真实名字**写，否则重新打开图层时匹配不上。
        actual_layer = layer_name
        actual_cols: dict[str, str] = {}
        try:
            row = db.cursor.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table' "
                "AND lower(name) = lower(?)",
                (layer_name,),
            ).fetchone()
            if row is not None:
                actual_layer = str(row[0])
            quoted = '"' + actual_layer.replace('"', '""') + '"'
            for info_row in db.cursor.execute(f"PRAGMA table_info({quoted})"):
                actual_cols[str(info_row[1]).casefold()] = str(info_row[1])
        except Exception:  # noqa: BLE001 —— 取不到就按原名写
            pass
        # 导出的图层本身也登记成 spatial，这样把目标库加进「数据管理」时
        # 图层和字典都能看到（OGR 写出的库没有 user_relation 注册行）。
        try:
            exists = db.cursor.execute(
                "SELECT 1 FROM user_relation WHERE table_name = ? AND type = 'spatial'",
                (actual_layer,),
            ).fetchone()
            if exists is None:
                db.add_domain_table(actual_layer, "spatial")
        except Exception:  # noqa: BLE001 —— 注册失败不影响已写出的数据
            logger.warning("导出字典后登记图层失败：%s", actual_layer, exc_info=True)
        for field_name, info in fields:
            match = next(
                (
                    item
                    for item in specs
                    if item.category == info.table and info.domain in item.domains
                ),
                None,
            )
            if match is None:
                continue
            column_name = actual_cols.get(field_name.casefold(), field_name)
            upsert_field_meta(
                db.conn,
                actual_layer,
                column_name,
                FieldMeta(
                    dict_table=match.category,
                    dict_domain=info.domain,
                    dict_local_table=match.table_name,
                ),
            )
        db.conn.commit()
    finally:
        db.conn.close()
    logger.info("已把 %d 张字典表写入 %s", len(specs), target)


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
    return {
        str(field.name()): sanitize_shapefile_field_name(field.name(), used)
        for field in fields
    }


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


def _export_fields(layer: QgsVectorLayer) -> list:
    """要写进目标的字段：**排除数据源自维护的 fid/ogc_fid**。

    OGR 会把 gpkg / gdb 的 ``fid`` 当普通字段列进 ``fields()``；把它当属性写出去
    时，目标写入器会把它当 FID 列校验，类型对不上就报
    ``Wrong field type for fid``（用户导出时踩过）。FID 交给目标文件自己分配。
    """
    fields = list(layer.fields())
    skip = provider_id_field_indices(layer)
    return [field for index, field in enumerate(fields) if index not in skip]


def _copy_to_memory(
    layer: QgsVectorLayer,
    features,
    fields,
    renamed: dict[str, str],
    target_crs=None,
) -> QgsVectorLayer:
    """把 ``fields``（源字段子集，可改名）与要素复制到内存图层。

    ``features=None`` 表示没做筛选（现取全部）。属性按**源字段下标**逐个搬运：
    不能把源要素直接塞进字段数不同的图层（按位置对齐，会错列）。
    ``target_crs`` 非空时把几何重投影到该坐标系（KML / GeoJSON 用）。
    """
    from qgis.core import QgsFeature, QgsField, QgsGeometry, QgsVectorLayer

    wkb_name = _wkb_name(layer)
    crs = target_crs or layer.crs()
    crs_text = crs.authid() or crs.toProj()
    memory = QgsVectorLayer(f"{wkb_name}?crs={crs_text}", layer.name(), "memory")
    if not memory.isValid():
        raise ExportLayerError("建不出导出用的内存图层（图层没有几何类型？）")

    transform = None
    if target_crs is not None:
        from qgis.core import QgsCoordinateTransform, QgsProject

        transform = QgsCoordinateTransform(
            layer.crs(), target_crs, QgsProject.instance()
        )

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

    if transform is None and _is_full_unchanged(layer, fields, {}):
        # 字段原样、只是筛了要素：源要素与目标结构一一对应，直接写
        if not provider.addFeatures(features):
            raise ExportLayerError("把要素复制到导出用的内存图层时失败")
        return memory

    copies = []
    for feature in features:
        source_attrs = feature.attributes()
        copy = QgsFeature(memory.fields())
        try:
            geometry = feature.geometry()
            if transform is not None and geometry is not None and not geometry.isNull():
                geometry = QgsGeometry(geometry)
                geometry.transform(transform)
            copy.setGeometry(geometry)
            copy.setAttributes([source_attrs[index] for index in source_indices])
        except (IndexError, KeyError):
            # 字段结构对不上（图层在导出过程中被改过）——跳过这条，别整批崩
            logger.warning("导出：要素 %s 的属性列对不上，已跳过", feature.id())
            continue
        copies.append(copy)
    if copies and not provider.addFeatures(copies):
        raise ExportLayerError("把要素复制到导出用的内存图层时失败")
    return memory


def _repair_source_aliases(source) -> list[tuple[int, str]]:
    """把「UTF-8 被当成 Latin-1」的字段别名修好（返回原值供还原）。"""
    from app.core.util.text_encoding import repair_mojibake

    restore: list[tuple[int, str]] = []
    try:
        fields = source.fields()
    except RuntimeError:
        return restore
    for index in range(fields.count()):
        raw = fields.at(index).alias() or ""
        fixed = repair_mojibake(raw)
        if fixed == raw:
            continue
        restore.append((index, raw))
        source.setFieldAlias(index, fixed)
    return restore


def _restore_aliases(source, restore: list[tuple[int, str]]) -> None:
    """把导出前临时修过的别名还原（图层对象可能是工程里的原件）。"""
    if not restore:
        return
    try:
        for index, raw in restore:
            source.setFieldAlias(index, raw)
    except RuntimeError:
        pass


def _prepare_source(layer: QgsVectorLayer, spec: ExportSpec) -> QgsVectorLayer:
    """要写给 OGR 的那份图层：按需筛要素 / 把 KML、GeoJSON 转到经纬度。

    字段子集与改名**不在这里复制**：``QgsVectorFileWriter`` 的
    ``attributes`` / ``attributesExportNames`` 能直接选字段、改导出名（所有
    格式通用），见 :func:`_apply_field_options`。
    """
    from qgis.core import QgsCoordinateReferenceSystem, QgsExpression, QgsFeatureRequest

    expression = (spec.expression or "").strip()
    features = None
    if spec.selected_only:
        # 只导出选中要素：按 fid 过滤，不构造表达式（选中数可能很大）
        fids = list(layer.selectedFeatureIds())
        if not fids:
            raise ExportLayerError("当前图层没有选中要素，请先选择要素或改为导出全部")
        features = list(layer.getFeatures(QgsFeatureRequest().setFilterFids(fids)))
        logger.info("导出筛选：仅选中要素 → %d 个", len(features))
    elif expression:
        expr = QgsExpression(expression)
        if expr.hasParserError():
            raise ExportLayerError(f"筛选条件有语法错误：{expr.parserErrorString()}")
        names = {field.name() for field in layer.fields()}
        unknown = [name for name in expr.referencedColumns() if name not in names]
        if unknown:
            raise ExportLayerError("筛选条件引用了不存在的字段：" + "、".join(unknown))
        features = list(layer.getFeatures(QgsFeatureRequest().setFilterExpression(expression)))
        logger.info("导出筛选：%s → 命中 %d 个要素", expression, len(features))

    # KML / GeoJSON 规范只认经纬度：投影坐标系（3857 / 4547 …）直接写会把米
    # 当成度。这里先复制并转到 EPSG:4326；本来就是地理坐标系的图层零拷贝。
    target_crs = None
    if spec.driver in ("KML", "GeoJSON"):
        crs = layer.crs()
        if crs is not None and crs.isValid() and not crs.isGeographic():
            target_crs = QgsCoordinateReferenceSystem("EPSG:4326")
    if target_crs is not None:
        return _copy_to_memory(
            layer, features, _export_fields(layer), {}, target_crs
        )
    if features is not None:
        return _copy_to_memory(layer, features, _export_fields(layer), {})
    return layer


def sanitize_shapefile_field_name(name: str, used: set[str]) -> str:
    """单个字段名 → 合法 DBF 字段名（≤10 字节、去重、字母开头）。"""
    cleaned = "".join(
        ch if (ch.isalnum() or ch == "_") else "_" for ch in str(name)
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
    return candidate


def _make_dict_value_converter(layer):
    """不附带字典表的格式：把字典字段的代码值转换成显示名称。

    用于 Excel / Shapefile / GeoJSON / KML 这类没有「字典表 / 属性域」落点的格式：
    ``convert_dict`` 勾选时字段值写字典名称，不勾选保持原始代码。
    用写入器的 ``FieldValueConverter`` 做，不复制图层。
    """
    from qgis.core import QgsField, QgsVectorFileWriter
    from qgis.PyQt.QtCore import QVariant

    from app.core.util.dict_values import layer_dict_fields

    try:
        infos = {name: info for name, info in layer_dict_fields(layer)}
    except Exception:  # noqa: BLE001 —— 读不到字典就不转换
        return None
    if not infos:
        return None
    fields = layer.fields()

    class _DictValueConverter(QgsVectorFileWriter.FieldValueConverter):
        def convert(self, field_idx, value):  # noqa: N802 —— Qt 命名
            try:
                info = infos.get(fields.at(field_idx).name())
            except (IndexError, RuntimeError):
                return value
            if info is None or value is None:
                return value
            name = info.name_for(value)
            return value if name is None else name

        def fieldDefinition(self, field):  # noqa: N802 —— Qt 命名
            """字典字段导出成文本列（原名可能是整型代码）。"""
            if field.name() not in infos:
                return field
            copy = QgsField(field)
            copy.setType(QVariant.String)
            return copy

    return _DictValueConverter()


def _export_name_map(layer: QgsVectorLayer, spec: ExportSpec) -> dict[str, str]:
    """``{原字段名: 导出字段名}``（只含真正改名的字段）。

    - 优先用 ``spec.field_export_names``（与 ``field_names`` 一一对应）；
    - 没给导出名时用原字段名（顺手修 GDB 的 Latin-1 乱码字段名）；
    - Shapefile 再按 DBF 规则截到 ≤10 字节并去重；
    - 其它格式重名（不分大小写）自动加 ``_2``。
    """
    from app.core.util.text_encoding import repair_mojibake

    if spec.driver == "DXF":
        return {}
    fields = layer.fields()
    wanted = (
        list(spec.field_names)
        if spec.field_names is not None
        else [field.name() for field in fields]
    )
    requested = dict(zip(wanted, spec.field_export_names or ()))
    is_shapefile = spec.driver == "ESRI Shapefile"
    used: set[str] = set()
    rename: dict[str, str] = {}
    for original in wanted:
        if fields.indexOf(original) < 0:
            continue
        desired = str(requested.get(original) or "").strip()
        if not desired:
            desired = repair_mojibake(original) if not is_shapefile else original
        if is_shapefile:
            desired = sanitize_shapefile_field_name(desired, used)
        else:
            base = desired
            index = 2
            while desired.casefold() in used:
                desired = f"{base}_{index}"
                index += 1
            used.add(desired.casefold())
        if desired != original:
            rename[original] = desired
    return rename


def _apply_field_options(options, source: QgsVectorLayer, spec: ExportSpec, name_map) -> None:
    """把「只导出这些字段 / 改成这些名字」写进 OGR 写入选项。

    ``attributesExportNames`` 的约定是**与图层字段一一对应的全量列表**（不是
    只对应选中的那几个）；没选的字段填原名即可，写入器会忽略。
    """
    if spec.driver == "DXF":
        # DXF 不支持任意属性列（写了会报 "does not support arbitrary field
        # creation"），只写几何
        options.skipAttributeCreation = True
        return
    fields = source.fields()
    provider_ids = provider_id_field_indices(source)
    if spec.field_names is None:
        indices = [i for i in range(fields.size()) if i not in provider_ids]
    else:
        indices = [
            fields.indexOf(name)
            for name in spec.field_names
            if fields.indexOf(name) >= 0
            and name.strip().lower() not in PROVIDER_ID_NAMES
        ]
        # 源图层自己维护的 FID 也不写（缓存来自旧调用方时兜底）
        indices = [i for i in indices if i not in provider_ids]
    if not indices:
        return
    options.attributes = indices
    if name_map:
        options.attributesExportNames = [
            name_map.get(field.name(), field.name()) for field in fields
        ]


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
