"""列出数据源里的图层（子图层）并按它打开 —— 「外部数据集」挑字段结构时用。

容器型数据源一个文件里有多个图层（``.gdb`` 目录、``.gpkg`` / ``.sqlite`` / ``.db`` 库），
得先把里面的图层列出来让用户挑；单图层文件（``.shp`` / ``.geojson`` / …）只有一个，
列出来也是一项，界面直接选中它。

**列**走 QGIS 自己的 ``QgsProviderRegistry.querySublayers``（实测多图层 GDB / gpkg /
sqlite 都能列全），拿不到时退回 ``dataProvider().subLayers()`` 那串
``0!!::!!图层名!!::!!…``（本仓库既有的解析口径，见 ``create_data/service.py``）。

**开**走 :func:`~app.core.util.local_layer_loader.open_local_vector_table`（本仓库统一的
本地库打开策略：``.sqlite`` 要优先 spatialite provider，否则会凭空多出一列 ``rowid``），
其它格式按列出来的 uri 直接开。

**过滤掉本工程自己的记账表 / 系统表，以及库里登记为 dict 的字典表**：它们是库里的
普通表，OGR 会把它们当图层列出来，但它们不是用户的数据，混在候选里只会让人困惑。
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from app.infra.logging import get_logger

if TYPE_CHECKING:
    from qgis.core import QgsVectorLayer

logger = get_logger(__name__)

#: 本工程自己的记账 / 系统表，不算「数据源里的图层」
_OWN_TABLES = frozenset(
    {
        "user_relation",
        "metadata",
        "qgis_dev_column_alias",
        "qgis_dev_field_meta",
        "sqlite_sequence",
        "gpkg_contents",
        "gpkg_geometry_columns",
        "gpkg_spatial_ref_sys",
        "gpkg_data_columns",
        "gpkg_extensions",
        "gpkg_ogr_contents",
        "geometry_columns",
        "spatial_ref_sys",
    }
)

#: 走 :func:`open_local_vector_table` 的后缀（本地库：要按内容挑 provider）
_LOCAL_DB_SUFFIXES = frozenset({".gpkg", ".sqlite", ".sqlite3", ".db"})

#: ``dataProvider().subLayers()`` 的分隔符（与 ``create_data/service.py`` 同一口径）
_SUBLAYER_SEP = "!!::!!"

#: 「浏览文件」时认的矢量扩展名（GDB 是目录，得单独走「浏览 GDB」）
VECTOR_FILE_FILTER = (
    "矢量数据 (*.shp *.gpkg *.geojson *.json *.gml *.kml *.tab *.fgb *.sqlite *.sqlite3 *.db);;"
    "所有文件 (*)"
)


@dataclass(frozen=True)
class Sublayer:
    """数据源里的一个图层（子图层）。"""

    #: 图层名（库里的表名 / GDB 里的要素类名）
    name: str
    #: 打开它的 URI（``…|layername=xxx`` 这类）
    uri: str
    #: 有没有几何。**纯属性表也算合法候选** —— 复制字段结构一样用得上
    has_geometry: bool
    #: 几何类别（``point`` / ``line`` / ``polygon`` / 空串 = 无几何或认不出）。
    #: 「追加数据」要按目标图层的几何类型筛数据源，靠它判
    geometry_kind: str = ""


#: ``QgsWkbTypes.GeometryType`` → 几何类别名（**按枚举值比较**，别拿 ``str()`` 切：
#: PyQt6 里 ``str(枚举)`` 是 ``GeometryType.PointGeometry``，切出来对不上）
_GEOMETRY_KINDS: tuple[tuple[str, str], ...] = (
    ("PointGeometry", "point"),
    ("LineGeometry", "line"),
    ("PolygonGeometry", "polygon"),
)


def geometry_kind_of_layer(layer) -> str:
    """图层的几何类别：``point`` / ``line`` / ``polygon``；无几何或认不出返回空串。"""
    from qgis.core import QgsWkbTypes

    return _kind_from_geometry_type(QgsWkbTypes.geometryType(layer.wkbType()))


def _kind_from_geometry_type(geometry_type) -> str:
    """``QgsWkbTypes.GeometryType`` 枚举 → 类别名。"""
    from qgis.core import QgsWkbTypes

    for name, kind in _GEOMETRY_KINDS:
        if geometry_type == getattr(QgsWkbTypes.GeometryType, name):
            return kind
    return ""


def _kind_from_name(text: str) -> str:
    """按几何类型**名字**认类别（老写法 ``subLayers()`` 只给名字，如 ``MultiPolygon``）。"""
    lowered = str(text or "").lower()
    for kind in ("polygon", "line", "point"):
        if kind in lowered:
            return kind
    return ""


def list_sublayers(path: str) -> list[Sublayer]:
    """列出 ``path`` 里的所有图层（按名字排序）；读不出来返回空表。

    GDB 是**目录**，传它的目录路径即可（``querySublayers`` 认）。
    """
    if not Path(path).exists():
        logger.info("列子图层：路径不存在 %s", path)
        return []
    found = _query_sublayers(path)
    if not found:
        found = _data_provider_sublayers(path)
    return sorted(found, key=lambda item: item.name)


def hidden_tables(path: str) -> set[str]:
    """列图层时要隐藏的表：本工程记账表 + 库里登记为 dict 的字典表。"""
    hidden = set(_OWN_TABLES)
    if Path(path).suffix.lower() not in _LOCAL_DB_SUFFIXES:
        return hidden
    try:
        conn = sqlite3.connect(path, timeout=5.0)
    except sqlite3.Error:
        return hidden
    try:
        rows = conn.execute(
            "SELECT table_name FROM user_relation WHERE type = 'dict'"
        ).fetchall()
        hidden.update(str(row[0]) for row in rows if row and row[0])
    except sqlite3.Error:
        pass
    finally:
        conn.close()
    return hidden


def _query_sublayers(path: str) -> list[Sublayer]:
    """走 ``QgsProviderRegistry.querySublayers``（现代做法）。"""
    from qgis.core import QgsProviderRegistry, QgsWkbTypes

    hidden = hidden_tables(path)
    try:
        metadata = QgsProviderRegistry.instance().providerMetadata("ogr")
    except Exception as exc:  # noqa: BLE001 —— 裁剪过的安装包可能没有 OGR 元数据
        logger.info("列子图层：拿不到 OGR 的 provider 元数据（%s）", exc)
        return []
    if metadata is None:
        return []
    try:
        details = metadata.querySublayers(path)
    except Exception as exc:  # noqa: BLE001 —— 驱动不支持 / 文件损坏
        logger.warning("列 %s 的子图层失败：%s", path, exc)
        return []
    found: list[Sublayer] = []
    for detail in details:
        name = str(detail.name() or "")
        if not name or name in hidden:
            continue
        geometry = QgsWkbTypes.geometryType(detail.wkbType())
        kind = _kind_from_geometry_type(geometry)
        found.append(Sublayer(name, str(detail.uri() or path), bool(kind), kind))
    return found


def _data_provider_sublayers(path: str) -> list[Sublayer]:
    """老写法兜底：``dataProvider().subLayers()`` 那串 ``序号!!::!!图层名!!::!!…``。"""
    from qgis.core import QgsVectorLayer

    # 必须绑到局部变量：临时 QgsVectorLayer 一被回收，它的 dataProvider 也失效
    probe = QgsVectorLayer(path, "probe", "ogr")
    if not probe.isValid():
        return []
    provider = probe.dataProvider()
    if provider is None:
        return []
    found: list[Sublayer] = []
    hidden = hidden_tables(path)
    for row in provider.subLayers():
        parts = row.split(_SUBLAYER_SEP)
        if len(parts) < 2 or not parts[1] or parts[1] in hidden:
            continue
        geom_text = parts[3] if len(parts) > 3 else ""
        found.append(
            Sublayer(
                parts[1],
                f"{path}|layername={parts[1]}",
                bool(geom_text and geom_text != "None"),
                _kind_from_name(geom_text),
            )
        )
    return found


def open_sublayer(path: str, sublayer: Sublayer) -> tuple[QgsVectorLayer | None, str]:
    """打开 ``path`` 里的 ``sublayer``，返回 ``(图层, 失败原因)``。

    本地库（``.gpkg`` / ``.sqlite`` / ``.db``）走
    :func:`~app.core.util.local_layer_loader.open_local_vector_table` —— 那是本仓库
    「本地库矢量表唯一的打开策略」，别在这里另挑 provider；其余格式按 uri 直接开。
    """
    if Path(path).suffix.lower() in _LOCAL_DB_SUFFIXES:
        from app.core.util.local_layer_loader import open_local_vector_table

        return open_local_vector_table(path, sublayer.name)

    from qgis.core import QgsVectorLayer

    layer = QgsVectorLayer(sublayer.uri, sublayer.name, "ogr")
    if layer.isValid():
        return layer, ""
    try:
        reason = layer.error().message() or "打不开"
    except Exception:  # noqa: BLE001 —— 无效图层的 error() 也可能取不到
        reason = "打不开"
    return None, f"打开「{sublayer.name}」失败：{reason}"


__all__ = [
    "VECTOR_FILE_FILTER",
    "Sublayer",
    "geometry_kind_of_layer",
    "hidden_tables",
    "list_sublayers",
    "open_sublayer",
]
