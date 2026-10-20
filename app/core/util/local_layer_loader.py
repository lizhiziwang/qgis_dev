"""把「数据管理」Dock 双击 → 加入地图 的策略抽出 MainWindow。

本模块是 :class:`LocalLayerLoader` + 异常族,职责:
- 接 ``(db, category, name)`` 三参,根据 category 分流:
  - ``image``:GeoPackage 影像 → GDAL/GPKG 栅格**直开**(QGIS gdal provider;
    GPKG 栅格驱动按 ``gpkg_tile_matrix`` 元数据懒读, 没有 MBTiles 驱动扫
    全表卡 UI 的问题)。不依赖 metadata 表。直开失败(非 GeoPackage 内容 /
    库损坏)→ 抛 :class:`LocalLayerBuildFailedError` 提示 —— 影像只支持
    GeoPackage 内容, 没有本地 XYZ 瓦片回落(该服务已随 xyz 时代整体移除,
    老 spatialite 影像请迁移重落为 .gpkg);
  - ``spatial``:矢量表按 :func:`open_local_vector_table` 打开 —— **GeoPackage 内容**
    走 OGR(``路径|layername=表名``, GDAL(3.11) 按文件名后缀裁决驱动: 新规落库本体
    直接 ``.gpkg`` 后缀, GPKG 驱动直接接管、无别名无第二文件; 仅兼容**老 ``.db``
    后缀**的 gpkg 内容库经 :func:`~app.core.util.local_db_util.gpkg_open_path` 映射到
    同 inode 的 ``.gpkg`` 硬链接再开(零拷贝; 别名是**临时的**——只在图层存续期间存在,
    图层移除/会话结束即删)), **SpatiaLite 内容**走 spatialite provider(OGR 会把 FID 列
    当普通字段暴露出来, sqlite 库无主键就是 ``rowid`` → 属性表凭空多一列; 见
    :func:`open_local_vector_table`), provider 打不开才退 OGR。
    不采用 ``GPKG:`` 驱动前缀 —— Windows 盘符路径(如 ``GPKG:D:/…``)会被 GDAL 截断成
    盘符(实测 sqlite3_open(D) 失败; 仅相对路径无冒号时前缀可开, 但工程需持久化绝对
    路径, 故直接落库 ``.gpkg`` 后缀)。
  - ``dict`` 等无几何数据:抛 :class:`LocalLayerUnsupportedCategoryError`
    让上层弹原有 ``QMessageBox.information``(UX 行为保持不变)。
- 成功返回 :class:`QgsMapLayer` 实例(可直接 :meth:`QgsProject.addMapLayer`);
- 失败抛 :class:`LocalLayerError` 子类,由 MainWindow 转译为模态警告。

影像直开细节
------------
- QgsRasterLayer(gdal provider)在图层析构时会给库文件旁写 ``.aux.xml``
  (PAM)边车(QGIS 内部还会开 ``zoom_level=0`` 子视图)污染数据目录 ——
  直开一律经 :func:`_open_gdal_raster_no_pam`(开库前置
  ``GDAL_PAM_ENABLED=NO``)抑制, 目录永远只有库文件。
- 影像加载不依赖库内全局 ``metadata`` 表(GPKG 栅格驱动自己读
  ``gpkg_tile_matrix_set / gpkg_tile_matrix``)。
"""
from __future__ import annotations

import sqlite3
from typing import Any, Optional
from urllib.parse import unquote

from qgis.PyQt.QtCore import QObject
from qgis.core import QgsDataSourceUri, QgsMapLayer, QgsRasterLayer, QgsVectorLayer

from app.core.util.local_db_util import (
    gpkg_open_path,
    local_db_util,
    repair_gpkg_if_needed,
    table_geometry_column,
)
from app.infra.logging import get_logger

logger = get_logger(__name__)


def _open_gdal_raster_no_pam(path: str, name: str) -> QgsRasterLayer:
    """开文件栅格, 且不让 GDAL 在库目录里写 ``.aux.xml``(PAM)边车。

    实测: ``QgsRasterLayer(path, name, "gdal")`` 在图层析构/关闭时会往
    库文件旁写 ``<名>.aux.xml`` 与 QGIS 内部子视图的
    ``<名> - zoom_level=0.aux.xml`` 两个文件(数据目录会"多出文件")。
    raw ``gdal.Open`` 不写 —— 是 QgsRasterLayer 会话内改动触发的。

    GDAL 的 PAM 是否启用是**开库那一刻**按配置项固定的: 先置
    ``GDAL_PAM_ENABLED=NO`` 再 open, 建层成功后立刻还原(只影响本层,
    进程内后续其它栅格仍走默认)。GPKG 金字塔元数据在库内, 不需要 PAM。
    """
    from osgeo import gdal as _gdal

    _gdal.SetConfigOption("GDAL_PAM_ENABLED", "NO")
    try:
        return QgsRasterLayer(path, name, "gdal")
    finally:
        _gdal.SetConfigOption("GDAL_PAM_ENABLED", None)


class LocalLayerError(Exception):
    """本地库图层加载失败的基类(MainWindow 捕获后弹模态)。"""


class LocalLayerMissingPathError(LocalLayerError):
    """``db["path"]`` 为空。"""


class LocalLayerEmptyTableError(LocalLayerError):
    """矢量表无法打开(OGR 未识别 / 表不存在)。"""


class LocalLayerBuildFailedError(LocalLayerError):
    """影像 raster layer 直开失败(非 GeoPackage 内容 / 库损坏 / GDAL 报错)。"""


class LocalLayerUnsupportedCategoryError(LocalLayerError):
    """category 无几何数据(dict 等),由上层弹信息框。"""


class LocalLayerLoader(QObject):
    """数据管理 Dock 的加载策略入口。MainWindow 在 ``__init__`` 持有一个实例。

    线程模型:所有方法应在主线程调用;纯同步直开, 无后台线程/服务。
    """

    def __init__(self, parent: Optional[QObject] = None) -> None:
        super().__init__(parent)

    # ------------------------------------------------------------ 公共 API

    def load(
        self,
        db: dict[str, Any],
        category: str,
        name: str,
        *,
        parent: Optional[QObject] = None,  # noqa: ARG002 保留接口(给将来错误对话框)
    ) -> Optional[QgsMapLayer]:
        """根据 category 分流,返回 :class:`QgsMapLayer`(已 valid)。

        Raises:
            LocalLayerMissingPathError: ``db["path"]`` 为空。
            LocalLayerUnsupportedCategoryError: ``category == "dict"`` 等。
            LocalLayerEmptyTableError: 矢量表无法打开。
            LocalLayerBuildFailedError: 影像 raster layer isValid False。
        """
        path = str(db.get("path") or "")
        if not path:
            raise LocalLayerMissingPathError(
                f"「{name}」缺少库文件路径, 无法加载。"
            )
        # 旧版写坏的 GeoPackage 几何头(flags=0x00)会让 GDAL/QGIS 报
        # "Unable to read geometry"、几何全空。开图前先修一次, 不依赖数据
        # 管理 Dock 是否开过(文件浏览器/拖拽等直开路径也会走到这里)。
        fixed = repair_gpkg_if_needed(path)
        if fixed:
            logger.info("已修复旧版 GPKG 几何头: %s(%d 行)", path, fixed)
        if category == "image":
            return self._load_image(db, name, path)
        if category == "spatial":
            return self._load_vector(db, name, path)
        # "dict" 走这里 — 让 MainWindow 弹原有的 QMessageBox.information
        raise LocalLayerUnsupportedCategoryError(category)

    # ------------------------------------------------------------ 影像

    def _load_image(self, db: dict, name: str, path: str) -> Optional[QgsRasterLayer]:
        """影像加载:GeoPackage 内容 → GDAL/GPKG 栅格**直开**。

        GDAL(3.11)对 GPKG 的驱动裁决只看文件后缀: 新规落库本体直接
        ``.gpkg`` → GPKG 栅格直开; 仅老 ``.db`` 后缀的 gpkg 库经
        :func:`~app.core.util.local_db_util.gpkg_open_path` 映射到同 inode
        的 ``.gpkg`` 硬链接再开(临时别名)。直开一律经
        ``_open_gdal_raster_no_pam`` 抑制 .aux.xml 边车。

        失败(非 GeoPackage 内容 / 库损坏 / GDAL 不识别)不再回落本地 XYZ
        服务(已移除)——直接抛错提示, 影像只支持 GeoPackage 内容。
        """
        if not _db_file_is_gpkg(path):
            raise LocalLayerBuildFailedError(
                f"「{name}」影像直开失败: 该库不是 GeoPackage 内容。\n"
                f"文件: {path}\n\n"
                "影像只支持 GeoPackage(.gpkg)内容, 老格式(spatialite/纯\n"
                "sqlite 瓦片)影像请用 .gpkg 库重新落库后再加载。"
            )
        open_path = gpkg_open_path(path)
        layer = _open_gdal_raster_no_pam(open_path, name)
        if layer.isValid():
            return layer
        err = layer.error().message() or "QgsRasterLayer invalid"
        logger.warning("影像 GPKG 直开失败: %s / %s: %s", name, open_path, err)
        raise LocalLayerBuildFailedError(
            f"「{name}」影像直开失败, 无法作为地图图层打开。\n"
            f"文件: {open_path}\nGDAL 报错: {err}\n\n"
            "请检查库文件是否完整(GeoPackage 影像需含 gpkg_tile_matrix\n"
            "金字塔元数据与瓦片数据)。"
        )

    # ------------------------------------------------------------ 矢量

    def _load_vector(self, db: dict, name: str, path: str) -> Optional[QgsVectorLayer]:
        """矢量层直开:交给 :func:`open_local_vector_table`(按库类型选 provider)。

        失败时抛 :class:`LocalLayerEmptyTableError`, 并把库里那张表的
        provider / OGR 报错一起带出来。
        """
        layer, err = open_local_vector_table(path, name)
        if layer is not None:
            return layer
        raise LocalLayerEmptyTableError(
            f"「{name}」无法作为矢量图层打开(OGR 未识别)\n"
            f"文件: {path}\n报错: {err or '无'}"
        )


# ------------------------------------------------------------------ 打开策略


def open_local_vector_table(
    db_path: str, table: str
) -> tuple[Optional[QgsVectorLayer], str]:
    """打开本地库里的一张矢量表, 返回 ``(图层, 失败原因)``; 失败时图层为 ``None``。

    这是**本地库矢量表唯一的打开策略**, 数据管理双击加载与「创建数据 → 写进已有库」
    的回读都走它 —— 别在别处另抄一份 provider 选择:

    - **GeoPackage 内容**(``.gpkg`` / 老 ``.db`` 后缀): ``gpkg_open_path`` 处理老
      ``.db`` 的同 inode ``.gpkg`` 别名, 再由 OGR 直开;
    - **SpatiaLite 内容**(``.db`` / ``.sqlite``): 优先 **spatialite provider**,
      打不开再退 OGR。

    **为什么 SpatiaLite 要优先 spatialite provider**: OGR 的 SQLite 驱动会把 FID 列
    暴露成一个普通字段 —— 无主键表就是 SQLite 隐式列 ``rowid``。于是属性表 / 属性识别 /
    图层属性 / 导出里都会凭空多出一列 ``rowid``(值还只是行号)。GPKG 驱动不报 FID 列
    (我们的 GPKG 表也没有主键), 所以这毛病只在 sqlite 库上出现 —— 见
    `QGIS-Developer 的讨论 <https://lists.osgeo.org/pipermail/qgis-developer/2020-May/061172.html>`_
    ("we expose the FID as a normal column in QGIS. This is a QGIS only behaviour.")。
    实测两种 provider 的字段 / CRS / 几何类型 / 要素数 / 框选 / 增删改完全一致, 只有
    spatialite 的字段清单是干净的。

    没有 URI 参数能压掉 OGR 那个伪字段(``key=`` / ``uid=`` 直接导致打不开, SQLite 驱动
    也不支持 ``FID`` 开库选项), provider 是唯一解。spatialite provider 依赖
    mod_spatialite, 老库 / 精简包里可能不可用 —— 那才退 OGR(退回时会有 ``rowid``,
    但至少图层能打开)。

    Args:
        db_path: 库文件路径。
        table: 库内表名。

    Returns:
        ``(QgsVectorLayer, "")`` 成功; ``(None, 原因)`` 失败。
    """
    is_gpkg = _db_file_is_gpkg(db_path)
    attempts: list[tuple[str, str]] = []  # (provider, 报错)
    if not is_gpkg:
        layer, err = _open_spatialite(db_path, table)
        if layer is not None:
            return _apply_field_meta(
                _apply_column_aliases(layer, db_path, table), db_path, table
            ), ""
        attempts.append(("spatialite", err))

    open_path = gpkg_open_path(db_path) if is_gpkg else db_path
    layer = QgsVectorLayer(f"{open_path}|layername={table}", table, "ogr")
    if layer.isValid():
        return _apply_field_meta(
            _apply_column_aliases(layer, db_path, table), db_path, table
        ), ""
    attempts.append(("ogr", layer.error().message() or "无"))
    return None, "; ".join(f"{p} 打不开: {e}" for p, e in attempts)


def _apply_column_aliases(layer, db_path: str, table: str):
    """把库里存的字段别名套到刚打开的图层上（没有别名就原样返回）。

    **别名是本工程自己记的**（``qgis_dev_column_alias``）：GeoPackage 有标准的
    ``gpkg_data_columns``，OGR 打开时自己就带出别名；SpatiaLite 没有这种目录表，
    所以建表时写进我们自己的那张表，在这里套回去 —— 不套的话用户"关掉再打开别名就没了"。

    两种格式都尝试套（gpkg 那张表通常不存在，读回空字典、什么都不做）。
    """
    try:
        db = local_db_util(db_path)
    except (sqlite3.Error, RuntimeError) as exc:
        logger.info("读字段别名失败（按没有别名处理）：%s（%s）", db_path, exc)
        return layer
    try:
        aliases = db.column_aliases(table)
    finally:
        db.conn.close()
    for column, alias in aliases.items():
        index = layer.fields().indexOf(column)
        if index >= 0:
            from app.core.util.text_encoding import repair_mojibake

            layer.setFieldAlias(index, repair_mojibake(alias))
    if aliases:
        logger.info("已套用字段别名 %d 个：%s（%s）", len(aliases), table, db_path)
    return layer


def _apply_field_meta(layer, db_path: str, table: str):
    """把库里存的「字典关联 / 可见性」套到刚打开的图层上。

    这些是工程自己的元数据（``qgis_dev_field_meta``），没有标准落点；不套的话
    数据管理重新打开的图层会丢掉创建时选的字典和隐藏字段。
    """
    from qgis.core import QgsEditorWidgetSetup

    from app.core.util.field_meta import (
        DICT_DOMAIN_PROP,
        DICT_LOCAL_TABLE_PROP,
        DICT_TABLE_PROP,
        read_field_meta,
    )

    try:
        db = local_db_util(db_path)
    except Exception as exc:  # noqa: BLE001 —— 读不到就按没有元数据处理
        logger.info("读字段元数据失败（按无处理）：%s（%s）", db_path, exc)
        return layer
    try:
        metas = read_field_meta(db.conn, table)
    finally:
        db.conn.close()
    if not metas:
        return layer
    fields = layer.fields()
    folded = {name.casefold(): meta for name, meta in metas.items()}
    for index in range(fields.size()):
        name = fields.at(index).name()
        # SQLite 驱动可能把列名改成小写，元数据键是原始大小写：大小写不敏感匹配
        meta = metas.get(name) or folded.get(name.casefold())
        if meta is None:
            continue
        if meta.dict_table:
            layer.setCustomProperty(DICT_TABLE_PROP + name, meta.dict_table)
        if meta.dict_domain:
            layer.setCustomProperty(DICT_DOMAIN_PROP + name, meta.dict_domain)
        if meta.dict_local_table:
            layer.setCustomProperty(
                DICT_LOCAL_TABLE_PROP + name, meta.dict_local_table
            )
        if not meta.visible:
            layer.setEditorWidgetSetup(index, QgsEditorWidgetSetup("Hidden", {}))
    return layer


def _open_spatialite(db_path: str, table: str) -> tuple[Optional[QgsVectorLayer], str]:
    """用 spatialite provider 打开一张表; 返回 ``(图层, 失败原因)``。

    **URI 必须显式给几何列名**(从库里的 ``geometry_columns`` 查, 见
    :func:`~app.core.util.local_db_util.table_geometry_column`): 留空时 provider 会把
    整张表当纯属性表读 —— 几何列变成普通字段、CRS 丢失(实测)。SRID / 主键列都由
    provider 自己从库里读, URI 里不用给(给了反而会被当成 key 列)。
    """
    geom_col = table_geometry_column(db_path, table)
    if not geom_col:
        return None, "库里没有这张空间表的几何列登记(geometry_columns)"
    uri = QgsDataSourceUri()
    uri.setDatabase(db_path)
    uri.setDataSource("", table, geom_col)
    layer = QgsVectorLayer(uri.uri(), table, "spatialite")
    if layer.isValid():
        return layer, ""
    return None, layer.error().message() or "无"


def layer_source_container(layer) -> str:
    """图层数据源的**容器路径**(库 / 文件本身), 剥掉子图层与 provider 选项。

    两种数据源形态都要认(见 :func:`open_local_vector_table`):

    - OGR 形态 ``D:/x.gpkg|layername=t`` —— 取 ``|`` 之前那段;
    - spatialite provider 形态 ``dbname='D:/x.db' table="t" (geom)`` —— 交给
      :class:`QgsDataSourceUri` 解析(它顺带处理 ``\\`` 转义, 别自己切字符串)。

    加载方挂在图层上的 ``layer.path_c`` 永远优先(见 ``app_actions`` / ``file_browser``):
    那是加载时确定的容器路径, 不用再解析。
    """
    path_c = getattr(layer, "path_c", None)
    if path_c:
        return str(path_c)
    source = str(layer.source() or "")
    if _looks_like_uri(source):
        return str(QgsDataSourceUri(source).database() or "")
    return source.split("|", 1)[0].strip()


def layer_source_table(layer) -> Optional[str]:
    """图层数据源里的**表名**(子图层名); 不是「库里的一张表」则 ``None``。

    栅格 / 内存 / 在线底图这些数据源没有表名概念, 返回 ``None`` —— 调用方按
    「图层显示名就是表名」兜底(本地库图层加载时就是这么命名的)。
    """
    source = str(layer.source() or "")
    if _looks_like_uri(source):
        return str(QgsDataSourceUri(source).table() or "") or None
    if "layername=" not in source:
        return None
    raw = source.split("layername=", 1)[1].split("|", 1)[0]
    return unquote(raw) or None


def _looks_like_uri(source: str) -> bool:
    """数据源串是不是 ``key=value`` 形态的 URI(而非 ``路径|选项`` 形态)。"""
    return source.startswith("dbname=") or " dbname=" in source


# --------------------------------------------------------------------- helpers


def _db_file_is_gpkg(db_path: str) -> bool:
    """只读内容探测:文件是 GeoPackage(gpkg 系统表齐全) → True。

    看文件实际内容, 不看病名; 损坏 / 非 sqlite / 打不开一律按 False 处理,
    让调用方直开报错。
    """
    try:
        # 普通路径直连(不用 ``file:`` URI —— Windows 路径含空格/中文时
        # URI 解析失败, 会误判成非 gpkg)
        con = sqlite3.connect(db_path, timeout=5.0)
        try:
            row = con.execute(
                "SELECT count(*) FROM sqlite_master WHERE type='table' "
                "AND name IN ('gpkg_contents','gpkg_spatial_ref_sys')"
            ).fetchone()
            return bool(row and row[0] == 2)
        finally:
            con.close()
    except Exception:  # noqa: BLE001
        return False


__all__ = [
    "LocalLayerLoader",
    "LocalLayerError",
    "LocalLayerMissingPathError",
    "LocalLayerEmptyTableError",
    "LocalLayerBuildFailedError",
    "LocalLayerUnsupportedCategoryError",
]
