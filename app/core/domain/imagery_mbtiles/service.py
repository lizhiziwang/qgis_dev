"""影像切片（MBTiles 输出）任务数据类与服务门面。

用 ``native:tilesxyzmbtiles`` 把当前地图画布渲染为单个 MBTiles 文件（SQLite
内嵌的 XYZ 瓦片金字塔），便于分发 / 离线使用。与
:mod:`app.core.domain.imagery_tiles`（XYZ 目录输出）同源，共用瓦片数估算
（:func:`estimate_tile_count`）与日志反馈（:class:`ProcessingLogFeedback`）。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from app.core.domain.imagery_tiles.service import (  # noqa: F401  (estimate_tile_count 再导出，供对话框用)
    clip_to_mercator_bounds,
    estimate_tile_count,
    extent_to_wgs84,
)

if TYPE_CHECKING:
    from qgis.core import QgsRasterLayer, QgsRectangle

    from .task import CreateImageryMbtilesTask
    from app.core.domain.imagery_tiles.worker_runner import WorkerSpec

#: ``native:tilesxyzmbtiles`` TILE_FORMAT 枚举（索引即算法取值，与 directory 版一致）
TILE_FORMAT_LABELS = ("PNG", "JPG")


@dataclass(frozen=True)
class ImageryMbtilesJob:
    """一次 MBTiles 切片参数。

    MBTiles 算法同样按**画布渲染**：``layer`` 仅用于决定切片范围（EXTENT）；
    切出的内容 = 画布上可见的所有图层（含底图、矢量叠加等）。对话框默认交给
    子进程（见 :mod:`app.core.domain.imagery_tiles.worker_runner`），子进程在
    自己的工程副本里隐藏其他图层，避免重复渲染底图/矢量导致耗时成倍增加，
    主进程的图层可见性不受影响。瓦片固定 256×256、按 TMS 规范存进单个
    .mbtiles 文件。

    :param layer: 决定切片范围（EXTENT）的栅格图层（**对象**）。
    :param output_file: 输出 .mbtiles 文件路径。
    :param min_zoom: 最小缩放级别。
    :param max_zoom: 最大缩放级别。
    :param tile_format: ``TILE_FORMAT`` 枚举：0=PNG 1=JPG。
    :param antialias: 是否启用抗锯齿。
    :param html: 是否在输出旁生成可双击预览的 Leaflet 网页
        （``{文件名}_preview/`` 目录；浏览器读不了 SQLite 格式的 mbtiles，
        故把瓦片解成目录后引用）。
    :param extent_wgs84: 切片范围（EPSG:4326）；为空时用图层完整范围。
    """

    layer: "QgsRasterLayer"
    output_file: str
    min_zoom: int = 0
    max_zoom: int = 8
    tile_format: int = 0
    antialias: bool = True
    html: bool = False
    extent_wgs84: "QgsRectangle | None" = None

    def slice_extent_wgs84(self) -> "QgsRectangle":
        """实际使用的切片范围：显式范围优先，否则取图层范围，并收进墨卡托边界。"""
        ext = self.extent_wgs84 if self.extent_wgs84 is not None else extent_to_wgs84(self.layer)
        return clip_to_mercator_bounds(ext)

    def to_native_params(self) -> dict:
        """转成 ``processing.run('native:tilesxyzmbtiles', ...)`` 接受的 dict。

        EXTENT 与 directory 版一致，统一换算到 EPSG:4326（算法按 WGS84 经纬度
        算瓦片号，投影坐标会得到负号 / 越界瓦片号，见
        :func:`~app.core.domain.imagery_tiles.service.extent_to_wgs84`）。
        """
        from qgis.PyQt.QtGui import QColor

        ext = self.slice_extent_wgs84()
        return {
            "EXTENT": (
                f"{ext.xMinimum()},{ext.xMaximum()},"
                f"{ext.yMinimum()},{ext.yMaximum()} [EPSG:4326]"
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

    def submit(
        self, job: ImageryMbtilesJob, worker_spec: "WorkerSpec | None" = None
    ) -> "CreateImageryMbtilesTask":
        """构造切片任务并返回。调用方需自行 ``task.start()`` 启动。

        :param worker_spec: 子进程作业（见
            :func:`~app.core.domain.imagery_tiles.worker_runner.prepare_worker_job`）；
            为空时退回进程内切片。
        """
        from .task import CreateImageryMbtilesTask  # noqa: PLC0415 - 循环依赖：task 反向依赖 service，上提成环

        return CreateImageryMbtilesTask("", job, worker_spec=worker_spec)

    def cancel(self, task) -> None:
        """取消任务。"""
        if task is not None:
            task.cancel()
