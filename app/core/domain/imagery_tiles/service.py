"""影像切片（栅格瓦片）任务数据类与服务门面。"""

from __future__ import annotations

import math
import os
from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from qgis.core import QgsRasterLayer, QgsRectangle

    from .task import CreateImageryTileTask as ImageryTileTask

#: ``native:tilesxyzdirectory`` TILE_FORMAT 枚举（索引即算法取值）
TILE_FORMAT_LABELS = ("PNG", "JPG")

#: 预估瓦片数超过该值时，对话框执行前会弹窗确认（防止误开超大任务）
TILE_COUNT_WARN_THRESHOLD = 2000


def estimate_tile_count(wgs84_extent: "QgsRectangle", min_zoom: int, max_zoom: int) -> int:
    """估算 XYZ 瓦片总数（与算法 ``getMetatiles`` 同一套 lon/lat→tile 公式）。

    瓦片数是切片耗时的主导因素（每个缩放级瓦片数约×4）；对话框据此在执行前
    给出预警。``wgs84_extent`` 为 WGS84（EPSG:4326）下的切片范围。
    """
    x_min_lon, x_max_lon = wgs84_extent.xMinimum(), wgs84_extent.xMaximum()
    y_min_lat, y_max_lat = wgs84_extent.yMinimum(), wgs84_extent.yMaximum()
    total = 0
    for z in range(min_zoom, max_zoom + 1):
        n = 1 << z
        x_min = int((x_min_lon + 180.0) / 360.0 * n)
        x_max = int((x_max_lon + 180.0) / 360.0 * n)
        y_min = int((1.0 - math.asinh(math.tan(math.radians(y_max_lat))) / math.pi) / 2.0 * n)
        y_max = int((1.0 - math.asinh(math.tan(math.radians(y_min_lat))) / math.pi) / 2.0 * n)
        total += (x_max - x_min + 1) * (y_max - y_min + 1)
    return total


@dataclass(frozen=True)
class ImageryTileJob:
    """一次影像切片的参数。

    使用 QGIS 原生 ``native:tilesxyzdirectory``（把当前地图画布渲染为 XYZ 瓦片
    目录）。注意：该算法**按画布渲染**，不直接接收影像图层参数；``layer`` 仅用于
    决定切片范围（EXTENT）与网页标题，实际切出的内容 = 画布上可见的所有图层。

    :param layer: 决定切片范围（EXTENT）的栅格图层（**对象**，约定同 BufferJob）。
    :param output_dir: 输出瓦片目录（{z}/{x}/{y}.png 金字塔）。
    :param min_zoom: 最小缩放级别。
    :param max_zoom: 最大缩放级别。
    :param tile_format: ``TILE_FORMAT`` 枚举：0=PNG 1=JPG。
    :param tile_size: 瓦片边长（px），默认 256。
    :param antialias: 是否启用抗锯齿。
    :param html: 是否在输出目录生成 Leaflet 网页查看器（index.html）。
    """

    layer: "QgsRasterLayer"
    output_dir: str
    min_zoom: int = 0
    max_zoom: int = 8
    tile_format: int = 0
    tile_size: int = 256
    antialias: bool = True
    html: bool = False

    def to_native_params(self) -> dict:
        """转成 ``processing.run('native:tilesxyzdirectory', ...)`` 接受的 dict。"""
        from qgis.PyQt.QtGui import QColor

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
            # METATILESIZE 决定一次渲染合并多少个瓦片：4→10 实测同量瓦片耗时
            # 约 6 倍提速（减少 QgsMapRendererSequentialJob 任务数）。算法上限 20。
            "METATILESIZE": 10,
            "TILE_WIDTH": self.tile_size,
            "TILE_HEIGHT": self.tile_size,
            "TMS_CONVENTION": False,
            "HTML_TITLE": self.layer.name(),
            "HTML_ATTRIBUTION": "",
            "HTML_OSM": False,
            "OUTPUT_DIRECTORY": self.output_dir,
            # 不生成网页查看器时传空串，processing 即不产出 HTML 文件
            "OUTPUT_HTML": (
                os.path.join(self.output_dir, "index.html") if self.html else ""
            ),
        }


class ImageryTileService:
    """影像切片服务门面。

    业务层只跟 :class:`ImageryTileService` 打交道；具体
    :class:`~app.core.domain.imagery_tiles.task.CreateImageryTileTask` 由
    service 在 ``submit`` 时构造并返回（**不**提交到
    :class:`QgsApplication.taskManager`——实测 ``native:tilesxyzdirectory``
    经 taskManager 后台线程运行会原生崩溃，改为普通线程，由调用方
    ``start()`` 启动）。栅格切片可能耗时较长，走后台线程，可取消。
    """

    def submit(self, job: ImageryTileJob) -> "ImageryTileTask":
        """构造切片任务并返回。调用方需自行 ``task.start()`` 启动。

        :rtype: :class:`~app.core.domain.imagery_tiles.task.CreateImageryTileTask`
        """
        from .task import CreateImageryTileTask

        return CreateImageryTileTask("", job)

    def cancel(self, task) -> None:
        """取消任务。"""
        if task is not None:
            task.cancel()
