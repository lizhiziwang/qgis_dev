"""影像切片（MBTiles 输出）任务数据类与服务门面。

用 ``native:tilesxyzmbtiles`` 把当前地图画布渲染为单个 MBTiles 文件（SQLite
内嵌的 XYZ 瓦片金字塔），便于分发 / 离线使用。与
:mod:`app.core.domain.imagery_tiles`（XYZ 目录输出）同源，共用瓦片数估算
（:func:`estimate_tile_count`）与日志反馈（:class:`ProcessingLogFeedback`）。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from app.core.domain.imagery_tiles.service import estimate_tile_count  # noqa: F401  (再导出，供对话框用)

if TYPE_CHECKING:
    from qgis.core import QgsRasterLayer

    from .task import CreateImageryMbtilesTask

#: ``native:tilesxyzmbtiles`` TILE_FORMAT 枚举（索引即算法取值，与 directory 版一致）
TILE_FORMAT_LABELS = ("PNG", "JPG")


@dataclass(frozen=True)
class ImageryMbtilesJob:
    """一次 MBTiles 切片参数。

    MBTiles 算法同样按**画布渲染**：``layer`` 仅用于决定切片范围（EXTENT）；
    切出的内容 = 画布上可见的所有图层（含底图、矢量叠加等）。瓦片固定
    256×256、按 TMS 规范存进单个 .mbtiles 文件。

    :param layer: 决定切片范围（EXTENT）的栅格图层（**对象**）。
    :param output_file: 输出 .mbtiles 文件路径。
    :param min_zoom: 最小缩放级别。
    :param max_zoom: 最大缩放级别。
    :param tile_format: ``TILE_FORMAT`` 枚举：0=PNG 1=JPG。
    :param antialias: 是否启用抗锯齿。
    :param html: 是否在输出旁生成可双击预览的 Leaflet 网页
        （``{文件名}_preview/`` 目录；浏览器读不了 SQLite 格式的 mbtiles，
        故把瓦片解成目录后引用）。
    """

    layer: "QgsRasterLayer"
    output_file: str
    min_zoom: int = 0
    max_zoom: int = 8
    tile_format: int = 0
    antialias: bool = True
    html: bool = False

    def to_native_params(self) -> dict:
        """转成 ``processing.run('native:tilesxyzmbtiles', ...)`` 接受的 dict。"""
        from PyQt6.QtGui import QColor

        ext = self.layer.extent()
        crs = self.layer.crs().authid() or "EPSG:4326"
        return {
            "EXTENT": (
                f"{ext.xMinimum()},{ext.xMaximum()},"
                f"{ext.yMinimum()},{ext.yMaximum()} [{crs}]"
            ),
            "ZOOM_MIN": self.min_zoom,
            "ZOOM_MAX": self.max_zoom,
            "DPI": 96,
            "BACKGROUND_COLOR": QColor(0, 0, 0, 0),  # 透明背景
            "ANTIALIAS": self.antialias,
            "TILE_FORMAT": self.tile_format,
            "QUALITY": 75,
            # 一次渲染合并 10×10 瓦片，减少渲染任务数（与 directory 版同款提速）
            "METATILESIZE": 10,
            "OUTPUT_FILE": self.output_file,
        }


class ImageryMbtilesService:
    """MBTiles 切片服务门面。

    与 :class:`~app.core.domain.imagery_tiles.service.ImageryTileService` 同款：
    ``submit`` 只构造任务对象（**不**提交 taskManager，避免原生崩溃），由调用方
    ``start()`` 启动普通后台线程。
    """

    def submit(self, job: ImageryMbtilesJob) -> "CreateImageryMbtilesTask":
        """构造切片任务并返回。调用方需自行 ``task.start()`` 启动。"""
        from .task import CreateImageryMbtilesTask

        return CreateImageryMbtilesTask("", job)

    def cancel(self, task) -> None:
        """取消任务。"""
        if task is not None:
            task.cancel()
