"""创建空矢量数据的领域服务。

「创建数据」对话框只负责收集参数（:class:`CreateDataSpec`），落盘由本服务完成：
建一个空的内存图层 → 按所选 OGR 驱动写盘 → 从盘上回读并返回图层对象；
往工程里加图层由调用方（UI）决定，本服务不碰工程状态。

几条 OGR 写盘的硬事实，都在本机 QGIS 4.2 上实测过：

- **容器格式默认会「清库」**：往已存在的 gpkg / sqlite 里写图层时，
  ``writeAsVectorFormatV3`` 的默认 ``actionOnExistingFile`` 是
  ``CreateOrOverwriteFile`` —— 整个数据集先被删掉，库里原有图层全没（实测
  「甲图层」写完「乙图层」后就只剩「乙图层」）。要保留原有图层必须显式用
  ``CreateOrOverwriteLayer``。本服务按「目标已存在一律不写」的约定从源头避开
  这个坑（见 :meth:`CreateDataService._check_target`）。
  **「往库里再建一个图层」是另一条需求、另走一条路**：建空库与往已有库里加图层
  见 :mod:`app.core.domain.create_data.library`（用本工程自己的 SQL 建表原语，
  只碰那一张新表）。本服务仍只管「一个文件就是一层」的单图层文件。
- **空文件存不下几何类型**：GeoJSON / KML / DXF / CSV / GPX / MapInfo TAB 的几何
  类型是从要素里推出来的，空图层回读只会得到 ``NoGeometry``（TAB 连坐标系都会一起
  丢）；回读时补 ``|geometrytype=`` 提示。
- **坐标系要分三种情况**（实测写盘后的文件内容）：
  - GeoPackage / SQLite / Shapefile / GeoJSON / GML / FlatGeobuf：文件自己存着
    CRS（GeoJSON 写在 ``"crs"`` 成员里），回读到的就是真的，不动；
  - KML / GPX：格式只认 WGS 84，文件里没有 CRS，回读得到 ``EPSG:4326`` ——
    保留并记一条 warning（创建时选的坐标系被格式改写了）；
  - DXF / CSV / MapInfo TAB：格式里根本没有坐标系这个概念（TAB 连 ``CoordSys``
    行都不写），回读出来的 ``4326`` 只是 QGIS 的兜底猜测，按创建时选的坐标系
    记在图层上（见 :data:`_CRSLESS_DRIVERS`）。
- **MapInfo TAB 至少要有一个字段**：0 字段写盘「成功」但产出坏的 .tab/.dat
  （回读失败），所以建 TAB 时补一个 ``name`` 字段。
- **CSV 要 ``GEOMETRY=AS_WKT``**：不给这个图层选项，写出的 CSV 里根本没有几何列。
- **几何类型不被格式接受时**：写盘直接失败（如 GPX + 面 → ``Geometry type of
  `Polygon' not supported in GPX``），错误原样报给用户，不加前置表（各驱动的
  支持面随 GDAL 版本变，猜不如让 GDAL 说话）。
- **字段结构**（向导第二步填的）写盘后活不活得下来，随格式而定（都在本机实测过）：
  字段名 / 类型 / 长度 / 小数位数在 Shapefile / GML / MapInfo TAB / FlatGeobuf 保得住，
  CSV 保得住列名但类型一律变文本；**GeoJSON / KML / DXF / GPX 的空文件根本存不下字段结构**
  （它们的 schema 是从要素里推的，而本服务建的就是空图层 —— 实测空 GeoJSON 回读字段数为 0）；
  别名只有 GPKG / FlatGeobuf 保留（GDAL 会写 ``gpkg_data_columns``），
  「不可为空」没有任何格式接受（OGR 不写 NOT NULL）。落不下的那部分由调用方在建完之后
  经 :func:`~app.core.domain.create_data.fields.configure_created_layer` 落在**工程里**
  （那是工程状态，不是数据源内容，所以不归本服务管），并如实报给用户。
"""

from __future__ import annotations

import os
import shutil
from dataclasses import dataclass
from typing import TYPE_CHECKING

from app.core.domain.create_data.fields import FieldSpec, qgs_field
from app.core.util.vector_formats import container_drivers
from app.infra.logging import get_logger, log_operation

if TYPE_CHECKING:
    from qgis.core import QgsCoordinateReferenceSystem, QgsVectorLayer

logger = get_logger(__name__)

#: 多图层容器格式：一个文件里能放多个图层，回读要带 ``|layername=``
#: （驱动名清单从 :func:`container_drivers` 派生，``VectorFormat.container`` 是唯一真相）
_CONTAINER_DRIVERS = container_drivers()

#: 文件本身不记几何类型的格式：空图层回读要带 ``|geometrytype=`` 提示
_GEOMETRY_HINT_DRIVERS = frozenset({"GeoJSON", "KML", "DXF", "CSV", "GPX", "MapInfo File"})

#: 格式里根本没有「坐标系」这个概念的驱动：回读后按创建时选的坐标系记在图层上
#: （否则会留下 QGIS 兜底猜的 4326 / 空 CRS，跟用户填的完全不是一回事）
_CRSLESS_DRIVERS = frozenset({"DXF", "CSV", "MapInfo File"})

#: 各驱动额外的图层创建选项（CSV 不给 ``GEOMETRY=AS_WKT`` 就没有几何列）
_LAYER_OPTIONS: dict[str, tuple[str, ...]] = {"CSV": ("GEOMETRY=AS_WKT",)}

#: 至少要有一个字段才写得成的驱动（0 字段写出的 MapInfo TAB 是坏文件）
_MIN_ONE_FIELD_DRIVERS = frozenset({"MapInfo File"})

#: 为上面这类驱动补的占位字段名（ASCII：MapInfo 字段名不吃非 ASCII）
_PLACEHOLDER_FIELD = "name"

#: ``QgsDataProvider.subLayers`` 各字段间的分隔符（见 :meth:`CreateDataService._layer_names`）
_SUBLAYER_SEP = "!!::!!"

#: 可写驱动清单的进程级缓存。**别省这一步**：``ogrDriverList()`` 实测要 70~130ms
#: （GDAL 把驱动一个个初始化一遍），而「创建数据」对话框开一次要问它三次
#: （单图层格式下拉 / 本地库格式下拉 / 生成空模板能不能点）—— 实测占了打开耗时的大头。
#: 进程生命周期里驱动集不会变（不装/卸 QGIS 插件），缓存是安全的。
_WRITABLE_DRIVERS: frozenset[str] | None = None


@dataclass(frozen=True)
class CreateDataSpec:
    """创建一份矢量数据所需的全部参数（对话框收集，服务消费）。"""

    #: 数据文件所在目录（已 normpath）
    path: str
    #: 数据文件名称（已按所选格式补/换扩展名，如 ``新建数据.gpkg``）
    file_name: str
    #: 数据格式显示名（如 ``GeoPackage``）
    data_format: str
    #: 数据格式的 OGR 驱动名（如 ``GPKG`` / ``ESRI Shapefile``）——写盘时给 QGIS
    driver: str
    #: 数据格式的默认扩展名（如 ``.gpkg``）
    extension: str
    #: 图层名
    layer_name: str
    #: QGIS 几何类型名（``Point`` / ``LineString`` / ``Polygon`` / ``Multi*``）
    geometry_type: str
    #: 坐标系 authid（如 ``EPSG:4490``）
    crs_authid: str
    #: 坐标系描述（如 ``China Geodetic Coordinate System 2000``），供日志 / 回显
    crs_description: str = ""
    #: 用户在「数据字段设置」那一步定下的字段结构（空 = 只建一个空图层）。
    #: **必须是 tuple**：spec 之间要能按 ``==`` 比（对话框的「应用」防重复创建），
    #: list 会让 frozen dataclass 不可哈希（见 :class:`FieldSpec` 的说明）。
    fields: tuple[FieldSpec, ...] = ()

    @property
    def file_path(self) -> str:
        """数据文件完整路径（``path`` + ``file_name``）。"""
        return os.path.join(self.path, self.file_name)


class CreateDataError(RuntimeError):
    """创建数据失败。消息可直接展示给用户（目标已存在 / 驱动缺失 / 写盘失败）。"""


def available_drivers() -> frozenset[str]:
    """本机 QGIS 可写出的 OGR 驱动名（对话框据此灰掉装不了的格式）。

    结果带进程级缓存：``ogrDriverList()`` 要 70~130ms，界面一次开窗会问好几遍，
    而驱动集在进程里不会变（见 :data:`_WRITABLE_DRIVERS`）。
    """
    global _WRITABLE_DRIVERS

    if _WRITABLE_DRIVERS is None:
        from qgis.core import QgsVectorFileWriter

        _WRITABLE_DRIVERS = frozenset(
            meta.driverName for meta in QgsVectorFileWriter.ogrDriverList()
        )
    return _WRITABLE_DRIVERS


class CreateDataService:
    """创建空矢量数据文件的门面。

    只建「空图层」（0 个要素、属性表按格式的最小要求给）：往里面加字段/要素是
    后续编辑的事。失败一律抛 :class:`CreateDataError`。
    """

    @log_operation("创建矢量数据")
    def create(self, spec: CreateDataSpec) -> QgsVectorLayer:
        """按 ``spec`` 建出数据文件，返回从文件回读的图层（尚未加入工程）。"""
        crs = self._resolve_crs(spec)
        self._check_driver(spec)
        self._check_target(spec)
        source = self._empty_layer(spec, crs)
        self._write(source, spec)
        return self._reload(spec, crs)

    # ------------------------------------------------------------- 前置检查

    @staticmethod
    def _resolve_crs(spec: CreateDataSpec) -> QgsCoordinateReferenceSystem:
        from qgis.core import QgsCoordinateReferenceSystem

        crs = QgsCoordinateReferenceSystem(spec.crs_authid) if spec.crs_authid else QgsCoordinateReferenceSystem()
        if not crs.isValid():
            raise CreateDataError(f"坐标系无效：{spec.crs_authid or '（未填）'}")
        return crs

    @staticmethod
    def _check_driver(spec: CreateDataSpec) -> None:
        if spec.driver not in available_drivers():
            raise CreateDataError(f"本机 QGIS 没有 {spec.data_format}（{spec.driver}）驱动，建不了这个格式的数据")

    def _check_target(self, spec: CreateDataSpec) -> None:
        """目标已存在就拦下。

        「往既有 gpkg 里再挂一个图层」技术上可行（写盘时显式用
        ``CreateOrOverwriteLayer``），但那是在改既有数据源、不是创建，覆盖语义也
        容易误伤，统一交给用户自己先处理（改名或删旧文件）。
        """
        if not os.path.exists(spec.file_path):
            return
        if spec.driver in _CONTAINER_DRIVERS and spec.layer_name in self._layer_names(spec.file_path):
            raise CreateDataError(f"「{spec.file_path}」里已有同名图层「{spec.layer_name}」，请换个图层名或文件名")
        raise CreateDataError(f"数据文件已存在：{spec.file_path}，请换个文件名，或先删除它")

    @staticmethod
    def _layer_names(path: str) -> list[str]:
        """容器里已有的图层名。

        ``subLayers()`` 给的串是
        ``<序号>!!::!!<图层名>!!::!!<几何列号>!!::!!<几何类型>!!::!!<几何列名>!!::!!``
        —— 图层名在**第 2 段**，末尾还挂着一个空段（所以不能顺手取 ``[-1]``）。
        """
        from qgis.core import QgsVectorLayer

        # 必须绑到局部变量：临时 QgsVectorLayer 一被回收，它的 dataProvider 也失效
        probe = QgsVectorLayer(path, "probe", "ogr")
        provider = probe.dataProvider()
        if provider is None:
            return []
        names: list[str] = []
        for row in provider.subLayers():
            parts = row.split(_SUBLAYER_SEP)
            if len(parts) > 1 and parts[1]:
                names.append(parts[1])
        return names

    # ------------------------------------------------------------- 建与写

    @staticmethod
    def _empty_layer(spec: CreateDataSpec, crs: QgsCoordinateReferenceSystem) -> QgsVectorLayer:
        """空的内存图层：URI 给几何类型 + 坐标系，属性列按第二步填的字段结构加。

        字段走 ``addAttributes``（不是 URI 的 ``&field=``）：URI 那套语法只能表达
        ``name:string(50)`` 这种最简形式，拿不到别名 / NotNull 约束。
        """
        from qgis.core import QgsVectorLayer

        crs_text = crs.authid() or crs.toProj()
        uri = f"{spec.geometry_type}?crs={crs_text}"
        if spec.driver in _MIN_ONE_FIELD_DRIVERS and not spec.fields:
            # MapInfo TAB 0 字段会写出坏文件，得补一个占位列。**只在用户没填字段时补** ——
            # 否则占位列会跟着写进文件，用户凭空多出一个空属性列。
            uri += f"&field={_PLACEHOLDER_FIELD}:string(50)"
        layer = QgsVectorLayer(uri, spec.layer_name, "memory")
        if not layer.isValid():
            raise CreateDataError(f"建不出{spec.geometry_type}几何的空图层（坐标系 {crs_text}）")
        if spec.fields:
            added = layer.dataProvider().addAttributes([qgs_field(f) for f in spec.fields])
            if not added:
                names = "、".join(f.name for f in spec.fields)
                raise CreateDataError(f"给新图层加字段失败：{names}")
            layer.updateFields()
        return layer

    @staticmethod
    def _write(source: QgsVectorLayer, spec: CreateDataSpec) -> None:
        from qgis.core import QgsCoordinateTransformContext, QgsVectorFileWriter

        options = QgsVectorFileWriter.SaveVectorOptions()
        options.driverName = spec.driver
        options.layerName = spec.layer_name
        # UTF-8：属性表里的中文不随系统代码页走（shp 会多写一个 .cpg）
        options.fileEncoding = "UTF-8"
        options.layerOptions = list(_LAYER_OPTIONS.get(spec.driver, ()))
        error, errmsg, _new, _saved = QgsVectorFileWriter.writeAsVectorFormatV3(
            source, spec.file_path, QgsCoordinateTransformContext(), options
        )
        if error != QgsVectorFileWriter.WriterError.NoError:
            CreateDataService._cleanup_failed(spec)
            raise CreateDataError(f"写入失败：{errmsg or '未知原因'}")

    @staticmethod
    def _cleanup_failed(spec: CreateDataSpec) -> None:
        """写盘失败时清掉半成品。

        必须清：本服务的约定是「目标已存在就拦下」，留个坏文件会让用户重试时
        撞上「数据文件已存在」，自己还删不干净（shp / TAB 是一组同名文件）。
        """
        try:
            if os.path.isdir(spec.file_path):
                shutil.rmtree(spec.file_path, ignore_errors=True)
            elif os.path.exists(spec.file_path):
                os.remove(spec.file_path)
        except OSError as exc:  # 清理失败不该盖住真正的报错，记一条就够
            logger.warning("清理创建失败的数据文件 %s 时出错：%s", spec.file_path, exc)

    # ------------------------------------------------------------- 回读

    @staticmethod
    def _reload(spec: CreateDataSpec, crs: QgsCoordinateReferenceSystem) -> QgsVectorLayer:
        """从刚写出的文件回读图层（空文件的几何类型 / 坐标系要补提示）。"""
        from qgis.core import QgsVectorLayer

        uri = spec.file_path
        if spec.driver in _CONTAINER_DRIVERS:
            uri += f"|layername={spec.layer_name}"
        elif spec.driver in _GEOMETRY_HINT_DRIVERS:
            uri += f"|geometrytype={spec.geometry_type}"
        layer = QgsVectorLayer(uri, spec.layer_name, "ogr")
        if not layer.isValid():
            CreateDataService._cleanup_failed(spec)
            raise CreateDataError(f"数据文件写出来了，但重新打开失败：{spec.file_path}")
        if spec.driver in _CRSLESS_DRIVERS or not layer.crs().isValid():
            # 文件里没存坐标系（DXF / CSV / TAB）→ 按创建时选的记在图层上；
            # 不做的话回读到的是 QGIS 的兜底值，跟用户填的完全不是一回事
            layer.setCrs(crs)
        elif layer.crs().authid() != spec.crs_authid:
            logger.warning(
                "%s 只认 %s，创建时选的 %s 已被格式改写",
                spec.data_format,
                layer.crs().authid(),
                spec.crs_authid,
            )
        logger.info(
            "已创建数据：%s（%s · %s · %s · 图层名 %s）",
            spec.file_path,
            spec.data_format,
            spec.geometry_type,
            layer.crs().authid(),
            spec.layer_name,
        )
        return layer


__all__ = ["CreateDataError", "CreateDataService", "CreateDataSpec", "available_drivers"]
