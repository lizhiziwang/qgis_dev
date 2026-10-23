"""把源图层的要素**追加**进本地库里已有的图层（表）：数据管理「追加数据」用。

与导入链路（:mod:`app.core.domain.import_layer`）的区别：那边是**建一张新表**再写；
这里表已经在了，只往里插行。两者共用同一个插入原语
:meth:`~app.core.util.local_db_util.local_db_util.batch_add_vector_data` —— 它会自己
读目标表的真实列 / 几何列 / SRID，按列名（大小写不敏感）绑定，未知字段自动忽略。

几条硬事实（都写在实现里了）：

- **要素要按目标表的 SRID 投影**：``batch_add_vector_data`` 不会替你转坐标，
  它按目标 SRID 解释你给的 WKT。源图层坐标系与目标不同时必须先转，否则数据"进去了，
  位置全错"。
- **字段按同名匹配**（大小写不敏感）。源里多出来的字段不导、目标里缺的列留空，
  结果里把两边都列出来 —— 用户得知道哪些字段没跟上。**数据源自己维护的 FID
  （GPKG 的 ``fid`` 等）不参与匹配**：它由目标库自增分配，复制过去会撞目标主键
  （见 :mod:`app.core.util.ogr_fields`）。
- **几何类型单多部件要对得上**：目标列是单部件类型（如 ``POLYGON``）时，源里的多部件
  要素会让整批回滚（``batch_add_vector_data`` 的约定）；错误原样报给用户。
- 整批是一个事务：任一行失败就全部回滚，不会留下半批数据。
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import TYPE_CHECKING

from app.core.util.local_db_util import local_db_util, table_geometry_column
from app.core.util.ogr_fields import PROVIDER_ID_NAMES, provider_id_field_indices
from app.infra.logging import get_logger

if TYPE_CHECKING:
    from qgis.core import QgsVectorLayer

logger = get_logger(__name__)


class AppendLayerError(RuntimeError):
    """追加失败。消息可直接展示给用户。"""


@dataclass(frozen=True)
class AppendResult:
    """一次追加的结果（给界面回显用）。"""

    #: 实际写入的要素数
    inserted: int
    #: 目标表名
    table: str
    #: 按同名匹配上的字段（目标列名）
    matched_fields: tuple[str, ...]
    #: 源里没能写进去的字段（目标表没这一列）
    skipped_fields: tuple[str, ...]


@dataclass(frozen=True)
class ImportDataResult:
    """一次「导入数据」（追加 / 替换）的结果（给界面回显用）。"""

    #: 实际写入的要素数
    inserted: int
    #: 目标表名
    table: str
    #: 替换模式清掉的原要素数（追加模式为 0）
    replaced: int
    #: 按同名匹配上的字段（目标列名，多源取并集）
    matched_fields: tuple[str, ...]
    #: 源里没能写进去的字段（多源取并集）
    skipped_fields: tuple[str, ...]
    #: 准备阶段就失败的源：``(源名, 原因)``（这些源的要素一条都没写）
    failed_sources: tuple[tuple[str, str], ...]
    #: 因「目标表是单部件类型、源是多部件要素」而跳过的要素数（多部件≥2 个部分时
    #: 无法安全转成单部件 —— 转换会**丢部件**，只能跳过并如实报数）
    skipped_multipart: int = 0


def import_features_into_table(
    sources,
    db_path: str,
    table: str,
    *,
    replace: bool = False,
) -> ImportDataResult:
    """把若干源图层的要素导入 ``db_path`` 里的 ``table``：**追加**或**替换**。

    - **追加**：往表里加行，原有要素不动；
    - **替换**：先清空目标表的要素（表结构不变），再写入新数据 —— 用户在确认框里
      点过「替换」才会走到这里。

    所有源的要素**先全部准备好再一次性写**（一个事务）：任一源准备失败（几何转不了、
    字段全对不上等）会被记进 ``failed_sources``，其余源照常写；而"替换"的清空发生在
    准备完成之后，不会出现"清空了却写不进去"（写库本身失败仍会回滚，但清空已提交 ——
    见 :func:`_clear_table` 的说明）。

    Args:
        sources: 源图层列表（工程图层或从文件打开的图层）。
        db_path: 目标本地库路径。
        table: 目标表名（库里的图层名）。
        replace: ``True`` = 替换（先清空）；``False``（默认）= 追加。

    Returns:
        导入结果。

    Raises:
        AppendLayerError: 库 / 表不存在、不是可写的本地库、一个源都没写成功等。
    """
    layers = [layer for layer in sources if layer is not None]
    if not layers:
        raise AppendLayerError("没有选择任何数据源")
    if not os.path.isfile(db_path):
        raise AppendLayerError(f"目标数据库不存在：{db_path}")
    if not table:
        raise AppendLayerError("目标图层为空")

    try:
        db = local_db_util(db_path)
    except Exception as exc:  # noqa: BLE001 —— 打不开的库把原因带给用户
        raise AppendLayerError(f"打不开目标数据库：{db_path}（{exc}）") from exc

    try:
        if db.kind not in ("gpkg", "spatialite"):
            raise AppendLayerError(
                f"「{os.path.basename(db_path)}」不是可写图层的本地库（只支持 GeoPackage / SpatiaLite）"
            )
        if not _table_exists(db, table):
            raise AppendLayerError(f"库里没有这个图层：{table}")
        # 目标表可以是**只有几何列**的（创建数据时没填字段）：那时 target_columns
        # 为空是正常的，不能当成"表不存在"（用户报过这个误报）
        target_columns = _table_columns(db, table, table_geometry_column(db_path, table))
        srid = _table_srid(db, table)
        target_geometry = _table_geometry_type(db, table)

        rows: list[dict] = []
        matched_all: list[str] = []
        skipped_all: list[str] = []
        skipped_multipart = 0
        failed: list[tuple[str, str]] = []
        for layer in layers:
            matched, skipped = _match_fields(layer, target_columns)
            transform = _geometry_transform(layer, srid)
            try:
                source_rows, source_skipped = _build_rows(
                    layer, matched, transform, target_geometry
                )
            except AppendLayerError as exc:
                failed.append((layer.name(), str(exc)))
                continue
            if not source_rows and not source_skipped:
                failed.append((layer.name(), "没有可导入的要素"))
                continue
            rows.extend(source_rows)
            skipped_multipart += source_skipped
            matched_all.extend(name for name in matched.values() if name not in matched_all)
            skipped_all.extend(name for name in skipped if name not in skipped_all)
        if not rows:
            reasons = "；".join(f"{name}：{why}" for name, why in failed)
            if skipped_multipart:
                reasons = (
                    f"{skipped_multipart} 个要素是多部件、目标表是单部件类型，无法导入"
                    + (f"；{reasons}" if reasons else "")
                )
            raise AppendLayerError(f"导入失败：{reasons or '没有可导入的要素'}")

        replaced = _clear_table(db, table) if replace else 0
        try:
            inserted = db.batch_add_vector_data(table, rows)
        except ValueError as exc:
            # 整批回滚（一个事务），但"替换"的清空已经提交了 —— 明确告诉用户
            suffix = "（目标表原有的要素已被清空）" if replace else ""
            raise AppendLayerError(f"写入失败：{exc}{suffix}") from exc
        except Exception as exc:  # noqa: BLE001 —— SQLite 层面的各种失败
            suffix = "（目标表原有的要素已被清空）" if replace else ""
            raise AppendLayerError(f"写入失败：{exc}{suffix}") from exc
    finally:
        db.conn.close()

    logger.info(
        "已%s %d 个要素到 %s/%s（替换清掉 %d；源 %d 个，失败 %d 个）",
        "替换" if replace else "追加",
        inserted,
        os.path.basename(db_path),
        table,
        replaced,
        len(layers),
        len(failed),
    )
    return ImportDataResult(
        inserted=inserted,
        table=table,
        replaced=replaced,
        matched_fields=tuple(matched_all),
        skipped_fields=tuple(skipped_all),
        failed_sources=tuple(failed),
        skipped_multipart=skipped_multipart,
    )


def append_features_to_table(layer: QgsVectorLayer, db_path: str, table: str) -> AppendResult:
    """把 ``layer`` 的要素追加到 ``db_path`` 里的 ``table``（单源便捷入口）。

    源图层的定义查询（``subsetString``）照常生效 —— 用户当前看到的那些要素才会被追加。

    Args:
        layer: 源图层（工程里已加载的图层，或从本地文件打开的一份）。
        db_path: 目标本地库路径。
        table: 目标表名（库里的图层名）。

    Returns:
        追加结果（写入条数、匹配上的字段、被跳过的字段）。

    Raises:
        AppendLayerError: 库 / 表不存在、不是可写的本地库、坐标系无效、写入失败。
    """
    result = import_features_into_table([layer], db_path, table, replace=False)
    return AppendResult(
        inserted=result.inserted,
        table=result.table,
        matched_fields=result.matched_fields,
        skipped_fields=result.skipped_fields,
    )


def _clear_table(db, table: str) -> int:
    """清空目标表的要素（表结构与登记信息不动），返回清掉的条数。

    **单独提交**是为了让"替换"在写入失败时也能看出发生了什么：调用方会在错误里
    明确写出"原有要素已被清空"。整表 `DELETE` 在 gpkg / SpatiaLite 上都是普通的
    SQL，不涉及几何列的特殊处理。
    """
    before = db.cursor.execute(f'SELECT COUNT(*) FROM "{table}"').fetchone()[0]
    db.cursor.execute(f'DELETE FROM "{table}"')
    db.conn.commit()
    return int(before or 0)


def _table_exists(db, table: str) -> bool:
    """目标表在不在（**别拿"有没有属性列"判断**：只有几何列的表也是存在的表）。"""
    try:
        row = db.cursor.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND lower(name) = lower(?)",
            (table,),
        ).fetchone()
    except Exception:  # noqa: BLE001 —— 库打不开时由调用方的写入报错兜底
        return False
    return row is not None


def _table_columns(db, table: str, geometry_column: str = "") -> list[str]:
    """目标表的真实列名（不含几何列，也不含由数据源自增的主键列）。

    主键列（``fid INTEGER PRIMARY KEY`` 这类）必须排除：追加时让目标库自己分配
    FID，而不是把源图层的 FID 写进去 —— 两边都从 1 开始时必然撞
    ``UNIQUE constraint failed: 表名.fid``（导入点位数据到已有图层时踩过）。
    """
    rows = db.cursor.execute(f'PRAGMA table_info("{table}")').fetchall()
    columns: list[str] = []
    for row in rows:
        name = str(row[1] or "")
        if not name:
            continue
        if geometry_column and name.lower() == str(geometry_column).lower():
            continue
        try:
            is_pk = int(row[5] or 0) != 0
        except (TypeError, ValueError):
            is_pk = False
        # 只排除「名字是 fid/ogc_fid 的整型主键」；业务主键（TEXT PRIMARY KEY 等）
        # 仍按同名匹配复制，不在这里一刀切
        if is_pk and name.lower() in PROVIDER_ID_NAMES:
            continue
        columns.append(name)
    return columns


def _table_srid(db, table: str) -> int:
    """目标表的 SRID（取不到按 0；0 表示未知，按源图层坐标系理解）。"""
    try:
        if db.kind == "gpkg":
            row = db.cursor.execute(
                "SELECT srs_id FROM gpkg_geometry_columns WHERE table_name = ?", (table,)
            ).fetchone()
        else:
            row = db.cursor.execute(
                "SELECT srid FROM geometry_columns WHERE f_table_name = ?", (table,)
            ).fetchone()
    except Exception:  # noqa: BLE001 —— 老库缺元数据表，按未知处理
        return 0
    try:
        return int(row[0]) if row and row[0] is not None else 0
    except (TypeError, ValueError):
        return 0


def _match_fields(layer: QgsVectorLayer, target_columns: list[str]) -> tuple[dict[str, str], list[str]]:
    """源字段 → 目标列（大小写不敏感同名匹配）。返回 ``({源字段: 目标列}, [没匹配上的源字段])``。

    源图层里由数据源自己维护的 FID 字段（GPKG 的 ``fid`` 等）**不参与匹配**：
    它不是业务属性，写进目标主键列只会撞 UNIQUE；也不该出现在「被跳过的字段」
    里让用户以为丢了数据。
    """
    by_lower = {column.lower(): column for column in target_columns}
    matched: dict[str, str] = {}
    skipped: list[str] = []
    provider_ids = provider_id_field_indices(layer)
    for index, field in enumerate(layer.fields()):
        if index in provider_ids:
            continue
        target = by_lower.get(field.name().lower())
        if target is None:
            skipped.append(field.name())
        else:
            matched[field.name()] = target
    return matched, skipped


def _geometry_transform(layer: QgsVectorLayer, srid: int):
    """源图层 → 目标 SRID 的坐标变换（同坐标系 / 未知 SRID 时返回 ``None``）。"""
    from qgis.core import QgsCoordinateReferenceSystem, QgsCoordinateTransform

    source_crs = layer.crs()
    if not srid or not source_crs.isValid():
        return None
    target_crs = QgsCoordinateReferenceSystem(f"EPSG:{srid}")
    if not target_crs.isValid() or target_crs == source_crs:
        return None
    # 按图层自己的工程上下文转换（含工程级的坐标变换设置）
    return QgsCoordinateTransform(source_crs, target_crs, layer.transformContext())


def _table_geometry_type(db, table: str) -> str:
    """目标表的几何类型名（``POLYGON`` / ``MULTIPOLYGON`` / ``GEOMETRY``）；取不到返回空串。

    导入时要拿它判断源几何能不能直接放进去：单部件列收不了多部件要素（库那边会整批
    拒绝），见 :func:`_build_rows`。
    """
    try:
        if db.kind == "gpkg":
            row = db.cursor.execute(
                "SELECT geometry_type_name FROM gpkg_geometry_columns WHERE table_name = ?",
                (table,),
            ).fetchone()
        else:
            row = db.cursor.execute(
                "SELECT type FROM geometry_columns WHERE f_table_name = ?", (table,)
            ).fetchone()
    except Exception:  # noqa: BLE001 —— 老库缺元数据表，按未知处理
        return ""
    return str(row[0] or "").upper() if row else ""


#: 单部件几何类型 → 对应的多部件类型名（GPKG / SpatiaLite 的规范名）
_SINGLE_TO_MULTI: dict[str, str] = {
    "POINT": "MULTIPOINT",
    "LINESTRING": "MULTILINESTRING",
    "POLYGON": "MULTIPOLYGON",
}


def _fit_geometry(geometry, target_geometry: str) -> object:
    """把源几何调整成目标列能收的形态；**多部件放不进单部件列时返回 ``None``**。

    - 目标列是多部件 / ``GEOMETRY``：原样返回（库那边会把单部件自动包成多部件）；
    - 目标列是单部件、源恰好只有一个部件：``convertToSingleType()`` 转过去
      （实测：GDB 的面图层一律报 ``MultiPolygon``，哪怕要素只有一个部件 —— 不转就
      整批被库拒掉，用户看到"点导入没反应"）；
    - 源有**两个及以上部件**：单部件列放不下，`convertToSingleType()` 会**悄悄丢掉
      其余部件**（实测），所以不转、返回 ``None``，由调用方跳过并如实报数。
    """
    from qgis.core import QgsWkbTypes

    expected_multi = _SINGLE_TO_MULTI.get(str(target_geometry or "").upper())
    if not expected_multi:
        return geometry
    flat = QgsWkbTypes.flatType(geometry.wkbType())
    if not QgsWkbTypes.isMultiType(flat):
        return geometry
    if QgsWkbTypes.displayString(flat).upper() != expected_multi:
        return geometry  # 类型对不上（点 vs 面）：交给库那边报错，别在这儿乱转
    if len(geometry.asGeometryCollection()) > 1:
        return None
    geometry.convertToSingleType()
    return geometry


def _build_rows(
    layer: QgsVectorLayer,
    matched: dict[str, str],
    transform,
    target_geometry: str = "",
) -> tuple[list[dict], int]:
    """把要素整理成 ``batch_add_vector_data`` 吃的行（属性按目标列名、几何用 WKT）。

    Returns:
        ``(行列表, 因多部件放不进单部件列而跳过的要素数)``。
    """
    from qgis.core import QgsGeometry

    rows: list[dict] = []
    skipped_multipart = 0
    for feature in layer.getFeatures():
        attributes = {}
        source_attributes = feature.attributes()
        for index, field in enumerate(layer.fields()):
            target = matched.get(field.name())
            if target is not None:
                attributes[target] = source_attributes[index]
        geometry = feature.geometry()
        wkt = ""
        if geometry is not None and not geometry.isNull():
            # 复制一份再转：别改动源要素的几何
            geometry = QgsGeometry(geometry)
            if transform is not None:
                try:
                    geometry.transform(transform)
                except Exception as exc:  # noqa: BLE001 —— 单条要素转不了就报出来
                    raise AppendLayerError(f"要素坐标转换失败：{exc}") from exc
            fitted = _fit_geometry(geometry, target_geometry)
            if fitted is None:
                skipped_multipart += 1
                continue
            wkt = fitted.asWkt()
        if not attributes and not wkt:
            continue  # 既没有能写的属性、也没有几何：跳过
        rows.append({"attributes": attributes, "geometry": wkt or None})
    return rows, skipped_multipart


__all__ = [
    "AppendLayerError",
    "AppendResult",
    "ImportDataResult",
    "append_features_to_table",
    "import_features_into_table",
]
