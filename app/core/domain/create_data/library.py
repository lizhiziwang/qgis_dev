"""本地库（多图层容器）的创建服务：建空库、往已有库里建空图层。

与 :mod:`app.core.domain.create_data.service`（建单图层数据）并列：那边一个文件
就是一层，这边处理的是「一个文件装多个图层」的库（GeoPackage / SpatiaLite）。

两个服务：

- :class:`CreateContainerService` —— **只建空库**，不建任何图层。库的系统表
  （GPKG 的 ``gpkg_*`` / SpatiaLite 的 ``spatial_ref_sys`` + ``user_relation``）
  由 :class:`~app.core.util.local_db_util.local_db_util` 在新建时初始化好，所以建出来
  的库当场就能被「数据管理」用 ``describe_local_db`` 登记（登记编排在 UI 侧，见
  ``AppActionsMixin._on_create_data``）。空库没有坐标系 / 几何类型这些概念，
  故 spec 里没有这些字段。
- :class:`CreateLayerInDbService` —— 往**已存在**的库里建一个空图层（表）。

**为什么往已有库里建图层走 SQL 建表而不是 OGR 写盘**（这是本模块最要紧的一条）：

``QgsVectorFileWriter.writeAsVectorFormatV3`` 写容器格式时，
``SaveVectorOptions.actionOnExistingFile`` 默认是 ``CreateOrOverwriteFile`` ——
**把整个库删掉重建**（见 create_data/service.py 模块头的实测记录）；哪怕显式改成
``CreateOrOverwriteLayer``，服务里那套「写失败就删文件」的清理逻辑
（``_cleanup_failed``）用在已有库上同样是毁库。而
:meth:`local_db_util.create_vector_table` 是本工程自己的建表原语：只管建这一张表，
失败回滚并删掉半成品表、绝不碰库里别的表，``register=True`` 顺带写好
``user_relation``（数据管理看得见它的唯一凭据），``overwrite=False`` 同名直接抛错。
导入链路（``app/core/domain/import_layer.py``）走的就是同一条路。

**这条路径是字段结构落盘最全的一条**（向导第二步填的字段直接变成建表的列定义）：
字段名 / 类型 / 长度 / 小数位与**真 NOT NULL** 都在 DDL 里（见 ``fields.sqlite_type``）；
别名两个格式都落库 —— gpkg 写标准的 ``gpkg_data_columns``，SpatiaLite 写本工程自己的
``qgis_dev_column_alias``（打开时由 ``local_layer_loader`` 套回图层）；隐藏 / 字典表 /
字典域是工程状态，由调用方在建完之后经 ``fields.configure_created_layer`` 落在图层上。
"""

from __future__ import annotations

import os
import shutil
import sqlite3
from dataclasses import dataclass
from typing import TYPE_CHECKING

from app.core.domain.create_data.dict_tables import (
    DictTableSpec,
    persist_dict_tables,
    resolve_local_table,
)
from app.core.domain.create_data.fields import FieldSpec, sqlite_type
from app.core.util.field_meta import (
    FieldMeta,
    ensure_field_meta_table,
    upsert_field_meta,
)
from app.core.util.local_db_util import local_db_util
from app.infra.logging import get_logger

if TYPE_CHECKING:
    from qgis.core import QgsVectorLayer

logger = get_logger(__name__)

#: 页签一只建这两种库：FileGDB 是目录型数据源，数据管理（``describe_local_db`` 只认
#: sqlite 家族）登记不了，放进「建完必进数据管理」的页签里只会打空承诺。
SUPPORTED_CONTAINER_DRIVERS: frozenset[str] = frozenset({"GPKG", "SQLite"})

#: 界面几何类型名（QGIS 的 :class:`QgsWkbTypes` 枚举名）→ 库内几何类型名
#: （GPKG 规范名，SpatiaLite 的 ``AddGeometryColumn`` 同名通用）
_GEOMETRY_SQL_NAMES: dict[str, str] = {
    "Point": "POINT",
    "LineString": "LINESTRING",
    "Polygon": "POLYGON",
    "MultiPoint": "MULTIPOINT",
    "MultiLineString": "MULTILINESTRING",
    "MultiPolygon": "MULTIPOLYGON",
}

#: 库内几何列名（与导入链路建表用的列名保持一致）
GEOMETRY_COLUMN = "geom"


class CreateLibraryError(RuntimeError):
    """建库 / 库里建图层失败。消息可直接展示给用户。"""


@dataclass(frozen=True)
class CreateContainerSpec:
    """创建一个空本地库所需的参数（对话框收集，服务消费）。"""

    #: 库文件所在目录（已 normpath）
    path: str
    #: 库文件名（已按格式补/换扩展名，如 ``新建数据库.gpkg``）
    file_name: str
    #: 数据格式显示名（如 ``GeoPackage``）
    data_format: str
    #: OGR 驱动名（``GPKG`` / ``SQLite``）——决定建库走哪条初始化
    driver: str
    #: 默认扩展名（如 ``.gpkg``）
    extension: str

    @property
    def file_path(self) -> str:
        """库文件完整路径（``path`` + ``file_name``）。"""
        return os.path.join(self.path, self.file_name)


@dataclass(frozen=True)
class CreateLayerInDbSpec:
    """往已存在的本地库里建一个空图层所需的参数。"""

    #: 目标库文件完整路径
    db_path: str
    #: 库内图层名（= 表名）
    layer_name: str
    #: 几何类型（QGIS 名，``Point`` / ``MultiPolygon`` …）
    geometry_type: str
    #: 坐标系 authid（如 ``EPSG:4490``）
    crs_authid: str
    #: 坐标系描述，写进库的 SRS 行名（可选）
    crs_description: str = ""
    #: 用户在「数据字段设置」那一步定下的字段结构（空 = 只建一张空表）。
    #: **必须是 tuple**：spec 要能按 ``==`` 比（对话框的「应用」防重复创建），
    #: list 会让 frozen dataclass 不可哈希（见 :class:`FieldSpec` 的说明）。
    fields: tuple[FieldSpec, ...] = ()
    #: 要一并写进目标库的字典表（每个字典表一张，多个域用 domain 列共存）。
    #: 建图层成功后、回读图层之前写入，避免 OGR 占着库时拿不到写锁。
    dict_tables: tuple[DictTableSpec, ...] = ()


class CreateContainerService:
    """创建空本地库容器的门面（不建图层、不碰工程状态）。"""

    def create(self, spec: CreateContainerSpec) -> str:
        """按 ``spec`` 建出一个空库，返回库文件完整路径。"""
        if spec.driver not in SUPPORTED_CONTAINER_DRIVERS:
            raise CreateLibraryError(f"{spec.data_format} 不是本地数据库容器格式（只支持 GeoPackage / SQLite）")
        if os.path.exists(spec.file_path):
            raise CreateLibraryError(f"数据库已存在：{spec.file_path}，请换个文件名，或先删除它")
        try:
            os.makedirs(spec.path, exist_ok=True)
        except OSError as exc:
            raise CreateLibraryError(f"创建目录失败：{spec.path}（{exc}）") from exc

        try:
            if spec.driver == "GPKG":
                self._create_gpkg(spec.file_path)
            else:
                self._create_spatialite(spec.file_path)
        except CreateLibraryError:
            raise
        except Exception as exc:  # noqa: BLE001 —— 建库失败一律清半成品后再报
            self._cleanup_half_baked(spec.file_path)
            raise CreateLibraryError(f"创建{spec.data_format}库失败：{exc}") from exc

        logger.info("已创建空库：%s（%s）", spec.file_path, spec.data_format)
        return spec.file_path

    @staticmethod
    def _create_gpkg(path: str) -> None:
        """GeoPackage：``local_db_util`` 新建时自动跑 ``init_gpkg_db()``。

        纯 SQL 建 GPKG 1.3 系统表（含 ``gpkg_spatial_ref_sys`` 的 SRS 种子、
        ``user_relation`` 目录表、``metadata``），**不需要** mod_spatialite。
        """
        db = local_db_util(path)
        db.conn.close()

    @staticmethod
    def _create_spatialite(path: str) -> None:
        """SpatiaLite：新建库走 ``init_db()``，内部要加载 mod_spatialite 扩展。"""
        try:
            db = local_db_util(path, load_spatialite=True)
            db.conn.close()
        except sqlite3.Error as exc:
            raise CreateLibraryError(
                "本机 QGIS 缺少 mod_spatialite 扩展，建不出 SpatiaLite 库；请改用 GeoPackage"
            ) from exc

    @staticmethod
    def _cleanup_half_baked(path: str) -> None:
        """清掉建库失败留下的半成品文件（不清理失败会挡住重试：目标已存在）。"""
        try:
            if os.path.isdir(path):
                shutil.rmtree(path, ignore_errors=True)
            elif os.path.exists(path):
                os.remove(path)
        except OSError as exc:
            logger.warning("清理建库失败的文件 %s 时出错：%s", path, exc)


class CreateLayerInDbService:
    """往已存在的本地库里建一个空图层的门面（只建这一张表，不碰库里别的数据）。"""

    def create(self, spec: CreateLayerInDbSpec) -> QgsVectorLayer:
        """在库里建出空图层并回读，返回可加入工程的图层。

        失败抛 :class:`CreateLibraryError`；库里已有的其它图层不受任何影响。
        """
        layer_name = spec.layer_name.strip()
        if not layer_name:
            raise CreateLibraryError("请填写图层名")
        geometry_type = _GEOMETRY_SQL_NAMES.get(spec.geometry_type)
        if geometry_type is None:
            raise CreateLibraryError(f"不支持的几何类型：{spec.geometry_type or '（未填）'}")
        if not os.path.isfile(spec.db_path):
            raise CreateLibraryError(f"目标数据库不存在：{spec.db_path}")

        try:
            db = local_db_util(spec.db_path)
        except sqlite3.Error as exc:
            raise CreateLibraryError(f"打不开目标数据库：{spec.db_path}（{exc}）") from exc
        try:
            if db.kind not in ("gpkg", "spatialite"):
                raise CreateLibraryError(
                    f"「{os.path.basename(spec.db_path)}」不是可写图层的数据库"
                    "（只支持 GeoPackage / SpatiaLite）"
                )
            srid, wkt, auth_name = _resolve_crs(spec.crs_authid, spec.crs_description)
            # GPKG 的 gpkg_contents.srs_id 有外键指向 gpkg_spatial_ref_sys，库里没有
            # 这一行时插内容表会直接 FOREIGN KEY 失败 —— 先按需补种（幂等）。
            db.ensure_gpkg_srs(srid, name=spec.crs_description, wkt=wkt, auth_name=auth_name)
            # 字段结构直接变成建表的列定义（``(列名, SQL 类型声明)``）：长度用 ``TEXT(n)``
            # 记进 schema，小数位用 ``REAL``（与 GDAL 写 gpkg 同款），「不可为空」是 DDL 里的
            # 真 NOT NULL —— 这条只有本路径做得到（见 fields 模块头）。
            columns = [(f.name, sqlite_type(f)) for f in spec.fields]
            try:
                db.create_vector_table(
                    layer_name,
                    columns=columns,
                    geometry_column=GEOMETRY_COLUMN,
                    geometry_type=geometry_type,
                    srid=srid,
                    register=True,
                    overwrite=False,
                )
            except ValueError as exc:
                # create_vector_table 对同名表（overwrite=False）抛的就是 ValueError
                if "已存在" in str(exc):
                    raise CreateLibraryError(
                        f"「{os.path.basename(spec.db_path)}」里已有同名图层「{layer_name}」，请换个图层名"
                    ) from exc
                raise CreateLibraryError(f"建图层失败：{exc}") from exc
            except Exception as exc:  # SpatiaLite 缺扩展等
                raise CreateLibraryError(f"建图层失败：{exc}") from exc
            # 字段别名要落库：gpkg 有标准位置（``gpkg_data_columns``，QGIS 读得回来）；
            # SpatiaLite 没有，写进本工程自己的别名表（``qgis_dev_column_alias``），
            # 打开时由 local_layer_loader 套回图层 —— 否则关掉再打开别名就没了
            if spec.fields:
                aliases = {f.name: f.alias for f in spec.fields if f.alias}
                if db.kind == "gpkg":
                    for name, alias in aliases.items():
                        db.set_gpkg_column_alias(layer_name, name, alias)
                elif aliases:
                    db.set_column_aliases(layer_name, aliases)
            # 字典关联 / 隐藏字段：没有标准落点，写进本工程的 qgis_dev_field_meta；
            # 打开图层时 local_layer_loader 会再套回自定义属性（见 field_meta 模块）。
            if spec.fields:
                ensure_field_meta_table(db.conn)
                for field in spec.fields:
                    meta = FieldMeta(
                        dict_table=field.dict_table,
                        dict_domain=field.dict_domain,
                        dict_local_table=resolve_local_table(field, spec.dict_tables),
                        visible=field.visible,
                    )
                    if not meta.is_default:
                        upsert_field_meta(db.conn, layer_name, field.name, meta)
                db.conn.commit()
            # 字典表与图层表写同一个库：放在回读 QgsVectorLayer 之前，写锁还在
            # 我们手里；写入失败把原因说清楚（此时图层表已建好，不悄悄吞掉）。
            if spec.dict_tables:
                try:
                    persist_dict_tables(db, spec.dict_tables)
                except Exception as exc:  # 字典写入失败统一转错误
                    raise CreateLibraryError(
                        f"图层已建在库里，但字典表写入失败：{exc}"
                    ) from exc
        finally:
            db.conn.close()

        layer = _read_back(spec.db_path, layer_name)
        logger.info("已在库 %s 里创建图层 %s（%s · %s）", spec.db_path, layer_name, geometry_type, srid)
        return layer


def _resolve_crs(authid: str, description: str = "") -> tuple[int, str, str]:
    """authid → ``(srid, WKT, 授权名)``；无效坐标系抛 :class:`CreateLibraryError`。"""
    from qgis.core import QgsCoordinateReferenceSystem

    crs = QgsCoordinateReferenceSystem(authid)
    if not crs.isValid():
        raise CreateLibraryError(f"坐标系无效：{authid or '（未填）'}")
    canonical = crs.authid()  # 归一成 ``EPSG:4490`` 这种形态
    auth_name, _, code = canonical.partition(":")
    srid = int(code) if code.isdigit() else int(crs.postgisSrid())
    return srid, crs.toWkt(), (auth_name or "EPSG")


def _read_back(db_path: str, layer_name: str) -> QgsVectorLayer:
    """从库里回读刚建的空图层（走本地库统一的打开策略，见下）。"""
    # 与「数据管理」双击加载同一条路：GeoPackage 走 OGR、SpatiaLite 走 spatialite
    # provider —— OGR 读 sqlite 表会把 FID 列当普通字段暴露（无主键就是 ``rowid``），
    # 回读出来的图层会凭空多一个属性列（见 open_local_vector_table 的说明）。
    from app.core.util.local_layer_loader import open_local_vector_table

    layer, err = open_local_vector_table(db_path, layer_name)
    if layer is None:
        # 表已经建成并登记好了，不为了回读失败去删它（那是用户的库）
        raise CreateLibraryError(
            f"图层已建在库里，但重新打开失败：{db_path}（图层 {layer_name}）\n{err}"
        )
    return layer


__all__ = [
    "CreateContainerService",
    "CreateContainerSpec",
    "CreateLayerInDbService",
    "CreateLayerInDbSpec",
    "CreateLibraryError",
    "SUPPORTED_CONTAINER_DRIVERS",
]
