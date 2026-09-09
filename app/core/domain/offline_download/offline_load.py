"""离线下载产物加载助手：把下载好的本地 XYZ 金字塔 / 矢量 .db 加进 QGIS。

- 影像：瓦片金字塔**不能**直接拖进 QGIS 或按栅格打开，必须用本地 XYZ 连接串
  ``type=xyz&url=file:///<解压目录>/…/tiles/{z}/{x}/{y}.png&zmin=..&zmax=..&format=..``
  （QGIS 的 xyz provider 同样支持 ``file://``，无需起 HTTP 服务）；
- 矢量：直接 OGR ``path|layername=<表名>`` 打开（与数据管理同款）。
"""

from __future__ import annotations

import os
from typing import Optional

#: 找不到文件时给宽层级兜底（金字塔实际只到哪级 QGIS 自己会 404 跳过）
_FALLBACK_ZOOM = (0, 24)


def build_local_xyz_uri(
    tiles_dir: str,
    zmin: int,
    zmax: int,
    image_format: str = "png",
) -> str:
    """构造本地 XYZ 连接串（URL 内中文/空格已 percent 编码，见 basemap 同款约定）。"""
    from urllib.parse import quote

    p = os.path.abspath(tiles_dir).replace("\\", "/").rstrip("/")
    enc = quote(p, safe=":/")
    return (
        f"type=xyz&url=file:///{enc}/{{z}}/{{x}}/{{y}}.{image_format}"
        f"&zmin={zmin}&zmax={zmax}&format={image_format}"
    )


def detect_tile_format(tiles_dir: str) -> str:
    """扫描金字塔目录第一个瓦片，判断 png/jpg（文件夹可能混存则按首个）。"""
    for _root, _dirs, files in os.walk(tiles_dir):
        for name in files:
            ext = name.rsplit(".", 1)[-1].lower()
            if ext in ("png", "jpg", "jpeg"):
                return "jpg" if ext == "jpeg" else ext
        # 单层内找到文件就返回，不用扫完整棵金字塔
        if files:
            break
    return "png"


def open_local_tiles_layer(
    tiles_dir: str,
    layer_name: str,
    zmin: Optional[int] = None,
    zmax: Optional[int] = None,
    image_format: Optional[str] = None,
    project=None,
):
    """把本地 XYZ 瓦片目录加载成图层（返回图层；失败返回 None 并记日志）。

    可用 :func:`build_local_xyz_uri` 只看连接串。层级缺省宽给兜底。
    """
    from qgis.core import QgsProject, QgsRasterLayer

    fmt = image_format or detect_tile_format(tiles_dir)
    lo, hi = _FALLBACK_ZOOM if zmin is None else (zmin, zmax)
    uri = build_local_xyz_uri(tiles_dir, lo, hi, fmt)
    layer = QgsRasterLayer(uri, layer_name, "wms")
    if not layer.isValid():
        return None
    (project or QgsProject.instance()).addMapLayer(layer)
    return layer


def open_local_db_vector(db_path: str, table_name: str, project=None):
    """打开下载库里的矢量表（OGR，与数据管理一致）；失败返回 None。"""
    from qgis.core import QgsProject, QgsVectorLayer

    layer = QgsVectorLayer(f"{db_path}|layername={table_name}", table_name, "ogr")
    if not layer.isValid():
        return None
    (project or QgsProject.instance()).addMapLayer(layer)
    return layer


__all__ = [
    "build_local_xyz_uri",
    "detect_tile_format",
    "open_local_db_vector",
    "open_local_tiles_layer",
]
