"""把「数据管理」Dock 双击 → 加入地图 的策略抽出 MainWindow。

本模块是 :class:`LocalLayerLoader` + 异常族,职责:
- 接 ``(db, category, name)`` 三参,根据 category 分流:
  - ``image``:GeoPackage 影像 → GDAL/GPKG 栅格**直开**(QGIS gdal provider;
    GPKG 栅格驱动按 ``gpkg_tile_matrix`` 元数据懒读, 没有 MBTiles 驱动扫
    全表卡 UI 的问题)。不依赖 metadata 表。直开失败(非 GeoPackage 内容 /
    库损坏)→ 抛 :class:`LocalLayerBuildFailedError` 提示 —— 影像只支持
    GeoPackage 内容, 没有本地 XYZ 瓦片回落(该服务已随 xyz 时代整体移除,
    老 spatialite 影像请迁移重落为 .gpkg);
  - ``spatial``:OGR provider 直开(``路径|layername=表名``)。GDAL(3.11)
    按**文件名后缀**裁决驱动 —— 新规落库本体直接 ``.gpkg`` 后缀, GPKG
    驱动直接接管、无别名无第二文件; 仅兼容**老 ``.db`` 后缀**的 gpkg
    内容库经 :func:`~app.core.util.local_db_util.gpkg_open_path` 映射到同
    inode 的 ``.gpkg`` 硬链接再开(零拷贝; 别名是**临时的**——只在图层存续
    期间存在, 图层移除/会话结束即删)。老 spatialite 库原样直读, OGR 失败
    时才退 spatialite provider(仅老库, GeoPackage 无此回退)。
    不采用 ``GPKG:`` 驱动前缀 —— Windows 盘符路径(如 ``GPKG:D:/…``)会
    被 GDAL 截断成盘符(实测 sqlite3_open(D) 失败; 仅相对路径无冒号时前缀
    可开, 但工程需持久化绝对路径, 故直接落库 ``.gpkg`` 后缀)。
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

from qgis.PyQt.QtCore import QObject
from qgis.core import QgsDataSourceUri, QgsMapLayer, QgsRasterLayer, QgsVectorLayer

from app.core.util.local_db_util import gpkg_open_path, repair_gpkg_if_needed
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
        """矢量层直开:OGR provider, ``路径|layername=表名``。

        - 新规库本体为 GeoPackage 且后缀 ``.gpkg`` → 直开(无别名):
          :func:`~app.core.util.local_db_util.gpkg_open_path` 对 ``.gpkg``
          原样返回, GPKG 驱动按后缀直接接管(几何 / SRS / 要素全部正常);
        - 老 ``.db`` 后缀 gpkg 库: 经同 inode 的 ``.gpkg`` 硬链接再开 ——
          GDAL(3.11)按后缀裁决驱动, .db 会被 SQLite 驱动抢先认领成纯属性表;
        - 老 spatialite 库: 原样直读; OGR 失败时才退 spatialite provider
          (仅老库, GeoPackage 没有该回退 —— 那套只认 spatialite)。

        不用 ``GPKG:`` 驱动前缀强制: 实测在 Windows 盘符路径上被截断成
        盘符(``sqlite3_open(D) failed``)。
        """
        is_gpkg = _db_file_is_gpkg(path)
        open_path = gpkg_open_path(path) if is_gpkg else path
        layer = QgsVectorLayer(f"{open_path}|layername={name}", name, "ogr")
        if layer.isValid():
            return layer
        ogr_err = layer.error().message() or "无"
        if not is_gpkg:
            # 老 spatialite 库的兜底(provider 依赖 mod_spatialite)
            meta = (db.get("meta") or {}).get(name) or {}
            geom_col = meta.get("geom_col")
            if geom_col:
                uri = QgsDataSourceUri()
                uri.setDatabase(path)
                uri.setDataSource(
                    "", name, str(geom_col), "", str(meta.get("srid") or "")
                )
                layer = QgsVectorLayer(uri.uri(), name, "spatialite")
                if layer.isValid():
                    return layer
        raise LocalLayerEmptyTableError(
            f"「{name}」无法作为矢量图层打开(OGR 未识别)\n"
            f"文件: {path}\nOGR 报错: {ogr_err}"
        )


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
