"""影像切片（栅格瓦片）任务数据类与服务门面。"""

from __future__ import annotations

import math
import os
from dataclasses import dataclass
from typing import TYPE_CHECKING

from app.infra.logging import get_logger

if TYPE_CHECKING:
    from qgis.core import QgsRasterLayer, QgsRectangle

    from .task import CreateImageryTileTask as ImageryTileTask
    from .worker_runner import WorkerSpec

logger = get_logger(__name__)

#: ``native:tilesxyzdirectory`` TILE_FORMAT 枚举（索引即算法取值）
TILE_FORMAT_LABELS = ("PNG", "JPG")
#: 与 TILE_FORMAT_LABELS 对应的输出文件扩展名
TILE_FORMAT_EXTS = ("png", "jpg")

#: 预估瓦片数超过该值时，对话框执行前会弹窗确认（防止误开超大任务）
TILE_COUNT_WARN_THRESHOLD = 2000

#: Web 墨卡托（EPSG:3857）能覆盖的纬度上限，对应 ±20037508.34 米
WEB_MERCATOR_MAX_LAT = 85.0511287798066
#: 范围正好贴到全球边界时向内收缩的度数（约 0.1 米）。QGIS 的瓦片范围计算在
#: 精确边界上会多算一圈：全球范围 z0 会生成 x=1、y=-1、y=1 等越界目录，
#: 预览里也会出现重复/错位的世界地图。收缩 1e-6° 后瓦片号恢复正常。
_TILE_EDGE_EPS = 1e-6


def clip_to_mercator_bounds(ext: "QgsRectangle") -> "QgsRectangle":
    """把 WGS84 范围夹到 Web 墨卡托有效区，并避开精确边界。

    只在范围贴边时起作用；普通的小范围影像（如某个城市的影像）原样返回。
    """
    from qgis.core import QgsRectangle  # noqa: PLC0415

    x_min = min(max(ext.xMinimum(), -180.0), 180.0)
    x_max = min(max(ext.xMaximum(), -180.0), 180.0)
    y_min = min(max(ext.yMinimum(), -WEB_MERCATOR_MAX_LAT), WEB_MERCATOR_MAX_LAT)
    y_max = min(max(ext.yMaximum(), -WEB_MERCATOR_MAX_LAT), WEB_MERCATOR_MAX_LAT)
    if x_min <= -180.0:
        x_min = -180.0 + _TILE_EDGE_EPS
    if x_max >= 180.0:
        x_max = 180.0 - _TILE_EDGE_EPS
    if y_min <= -WEB_MERCATOR_MAX_LAT:
        y_min = -WEB_MERCATOR_MAX_LAT + _TILE_EDGE_EPS
    if y_max >= WEB_MERCATOR_MAX_LAT:
        y_max = WEB_MERCATOR_MAX_LAT - _TILE_EDGE_EPS
    if x_max <= x_min or y_max <= y_min:
        # 完全落在有效区外：保持原范围，交给算法处理，不制造反向范围
        return ext
    return QgsRectangle(x_min, y_min, x_max, y_max)


def extent_to_wgs84(layer) -> "QgsRectangle":
    """把图层范围换算到 EPSG:4326（``native:tilesxyz*`` 的 EXTENT 约定）。

    ``native:tilesxyz*`` 的 EXTENT 直接按 **WGS84 经纬度** 算标准 XYZ 瓦片号，
    **不会**按 EXTENT 里声明的 CRS 做转换：投影坐标（3857 / 高斯克吕格等）
    原样传进去会算出负号、越界瓦片号，瓦片目录按 ``{z}/{x}/{y}`` 读出来位置
    全错，地图客户端里看不到（用户反馈「切片没有正常输出」）。所以业务层
    统一先换算到 EPSG:4326 再交给算法；转换失败（图层 CRS 无效等）退回原范围。

    全球范围（在线底图等）还要再收进 Web 墨卡托边界一点，否则算法会把
    ``x=2^z``、``y=-1`` 这类越界瓦片也切出来（见 :func:`clip_to_mercator_bounds`）。
    """
    from qgis.core import (  # noqa: PLC0415 - 模块顶层不依赖 qgis，便于纯逻辑单测
        QgsCoordinateReferenceSystem,
        QgsCoordinateTransform,
        QgsCoordinateTransformContext,
    )

    ext = layer.extent()
    src = layer.crs()
    wgs84 = QgsCoordinateReferenceSystem("EPSG:4326")
    try:
        if not src.isValid() or src == wgs84:
            return clip_to_mercator_bounds(ext)
        # 变换在后台线程调用，不碰 QgsProject 单例：用默认变换上下文即可
        ct = QgsCoordinateTransform(src, wgs84, QgsCoordinateTransformContext())
        return clip_to_mercator_bounds(ct.transformBoundingBox(ext))
    except Exception:  # noqa: BLE001 - 换算失败退回原范围，至少不比现状差
        logger.warning("图层范围换算到 EPSG:4326 失败，按原 CRS 传给切片算法", exc_info=True)
        return ext


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
    画布上的在线底图 / 要素图层会被逐元瓦片重复渲染，是切片慢的主因；对话框
    默认交给子进程（见 :mod:`worker_runner`），子进程在自己的工程副本里隐藏
    其他图层再切，主进程的图层可见性不受影响。

    :param layer: 决定切片范围（EXTENT）的栅格图层（**对象**，约定同 BufferJob）。
    :param output_dir: 输出瓦片目录（{z}/{x}/{y}.png 金字塔）。
    :param min_zoom: 最小缩放级别。
    :param max_zoom: 最大缩放级别。
    :param tile_format: ``TILE_FORMAT`` 枚举：0=PNG 1=JPG。
    :param tile_size: 瓦片边长（px），默认 256。
    :param antialias: 是否启用抗锯齿。
    :param html: 是否在输出目录生成 Leaflet 网页查看器（index.html）。
    :param extent_wgs84: 切片范围（EPSG:4326）；为空时用图层完整范围。界面可选
        「图层范围」或「当前画布范围」，后者由对话框换算后传入。
    """

    layer: "QgsRasterLayer"
    output_dir: str
    min_zoom: int = 0
    max_zoom: int = 8
    tile_format: int = 0
    tile_size: int = 256
    antialias: bool = True
    html: bool = False
    extent_wgs84: "QgsRectangle | None" = None

    def slice_extent_wgs84(self) -> "QgsRectangle":
        """实际使用的切片范围：显式范围优先，否则取图层范围，并收进墨卡托边界。"""
        ext = self.extent_wgs84 if self.extent_wgs84 is not None else extent_to_wgs84(self.layer)
        return clip_to_mercator_bounds(ext)

    def to_native_params(self) -> dict:
        """转成 ``processing.run('native:tilesxyzdirectory', ...)`` 接受的 dict。

        EXTENT 统一用换算到 EPSG:4326 的范围——算法的瓦片号按 WGS84 经纬度算，
        直接传投影坐标会得到负号 / 越界瓦片号（见 :func:`extent_to_wgs84`）。
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
            # 不用算法自带的 HTML：它从 CDN 加载 OpenLayers/Leaflet，离线环境
            # 打不开。完成后再由 :func:`write_image_tile_viewer` 写离线页面。
            "OUTPUT_HTML": "",
        }


def write_image_tile_viewer(job: ImageryTileJob) -> str:
    """在图层切片目录写离线 Leaflet 查看器，返回 ``index.html`` 路径。

    用图层范围（换算到 WGS84）定位地图；瓦片模板按输出格式取 ``.png`` /
    ``.jpg``。Leaflet 资源随应用打包并复制到输出目录，离线可用。
    """
    from app.core.domain.tile_viewer import write_tile_viewer  # noqa: PLC0415

    ext = (
        TILE_FORMAT_EXTS[job.tile_format]
        if 0 <= int(job.tile_format) < len(TILE_FORMAT_EXTS)
        else "png"
    )
    rect = job.slice_extent_wgs84()
    bounds = (
        rect.xMinimum(),
        rect.yMinimum(),
        rect.xMaximum(),
        rect.yMaximum(),
    )
    return write_tile_viewer(
        job.output_dir,
        title=job.layer.name(),
        tile_template=f"{{z}}/{{x}}/{{y}}.{ext}",
        min_zoom=job.min_zoom,
        max_zoom=job.max_zoom,
        bounds=bounds,
    )


class ImageryTileService:
    """影像切片服务门面。

    业务层只跟 :class:`ImageryTileService` 打交道；具体 service 在 ``submit``
    时构造并返回。默认走子进程（:mod:`.worker_runner`）：算法的
    ``processAlgorithm`` 在 C++ 里整段执行并持有 GIL，放主进程任何线程里都会
    卡住界面。子进程准备失败时才退回进程内切片（普通线程，可取消）。
    """

    def submit(
        self, job: ImageryTileJob, worker_spec: "WorkerSpec | None" = None
    ) -> "ImageryTileTask":
        """构造切片任务并返回。调用方需自行 ``task.start()`` 启动。

        :param worker_spec: 由 :func:`~app.core.domain.imagery_tiles.worker_runner.prepare_worker_job`
            准备的子进程作业；为空时退回进程内切片（会占用主进程 GIL）。
        :rtype: :class:`~app.core.domain.imagery_tiles.task.CreateImageryTileTask`
        """
        from .task import CreateImageryTileTask  # noqa: PLC0415 - 循环依赖：task 反向依赖 service，上提成环

        return CreateImageryTileTask("", job, worker_spec=worker_spec)

    def cancel(self, task) -> None:
        """取消任务。"""
        if task is not None:
            task.cancel()
