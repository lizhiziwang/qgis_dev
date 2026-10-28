"""矢量瓦片任务数据类与服务门面。"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Iterable, List, Tuple

if TYPE_CHECKING:
    from qgis.core import QgsApplication, QgsRectangle, QgsTask


@dataclass(frozen=True)
class TileJob:
    """一次矢量瓦片生成的参数。"""

    output_dir: str
    xyz_template: str = "{z}/{x}/{y}.pbf"
    layer_sources: List[str] = field(default_factory=list)
    min_zoom: int = 0
    max_zoom: int = 3
    # extent: (xmin, xmax, ymin, ymax, authid)
    extent: Tuple[float, float, float, float, str] = (0.0, 0.0, 0.0, 0.0, "EPSG:4490")
    #: 图层显示名（与 ``layer_sources`` 对应）：生成网页查看器时用作
    #: VectorGrid 的样式键；不参与算法参数。
    layer_names: Tuple[str, ...] = ()

    def to_native_params(self) -> dict:
        """转成 ``processing.run('native:writevectortiles_xyz', ...)`` 接受的 dict。"""
        xmin, xmax, ymin, ymax, authid = self.extent
        return {
            "OUTPUT_DIRECTORY": self.output_dir,
            "XYZ_TEMPLATE": self.xyz_template,
            "LAYERS": [{"layer": src} for src in self.layer_sources],
            "MIN_ZOOM": self.min_zoom,
            "MAX_ZOOM": self.max_zoom,
            "EXTENT": f"{xmin},{xmax},{ymin},{ymax} [{authid}]",
        }


class TileService:
    """矢量瓦片服务门面。

    业务层只跟 :class:`TileService` 打交道。对话框在 :meth:`create` 之后**先
    连好信号再** :meth:`start`，避免极快的任务在信号连接前就完成；:meth:`submit`
    保留给「提交即运行」的调用方。
    """

    def create(self, job: TileJob) -> "QgsTask":
        """构造任务但不提交（调用方连好信号后再 :meth:`start`）。"""
        from .task import CreateTileTask  # noqa: PLC0415 - 循环依赖：task 反向依赖 service，上提成环

        return CreateTileTask("", job)

    def create_group(self, job: TileJob) -> "QgsTask":
        """构造按缩放级拆分的并行任务（单段 / 内存图层时退回普通任务）。

        多段时每段由独立子进程执行（同进程多线程并发跑 processing 在大会
        崩溃，见 :mod:`app.core.domain.tiles.parallel`）；返回的任务带
        ``ranges`` 属性，供对话框显示「并行 N 组」。

        **内存图层不能交给子进程**（子进程里没有这个图层，会报
        ``Incorrect parameter value for LAYERS``），遇到时退回进程内单任务。
        """
        from .parallel import create_work_dir, split_zoom_ranges  # noqa: PLC0415
        from .task import CreateTileGroupTask  # noqa: PLC0415

        ranges = split_zoom_ranges(job.min_zoom, job.max_zoom)
        if len(ranges) <= 1 or self._needs_inprocess(job):
            task = self.create(job)
            task.ranges = list(ranges) if len(ranges) > 1 else [(job.min_zoom, job.max_zoom)]
            return task
        return CreateTileGroupTask("", job, ranges, create_work_dir())

    #: 子进程能按 source 重新打开的矢量 provider（本地文件 / SpatiaLite）
    _PARALLEL_PROVIDERS = frozenset({"ogr", "spatialite"})

    @classmethod
    def _needs_inprocess(cls, job: TileJob) -> bool:
        """作业里有内存图层 / 子进程无法可靠重载的数据源时退回进程内单任务。

        内存图层（``memory`` 或 ``Polygon?…&uid=…``）在子进程里根本不存在；
        服务器类 provider 的 source 可能带会话 / 凭据，也不冒险拆进子进程。
        """
        from qgis.core import QgsProject  # noqa: PLC0415

        sources = {str(src) for src in job.layer_sources}
        if any(src.startswith("memory:") for src in sources):
            return True
        try:
            layers = list(QgsProject.instance().mapLayers().values())
        except RuntimeError:
            return False
        for layer in layers:
            try:
                if layer.source() in sources and layer.providerType() not in cls._PARALLEL_PROVIDERS:
                    return True
            except RuntimeError:
                continue
        return False

    def start(self, task: "QgsTask") -> None:
        """把已构造的任务提交到 :class:`QgsApplication.taskManager`。"""
        from qgis.core import QgsApplication

        QgsApplication.taskManager().addTask(task)

    def submit(self, job: TileJob) -> "QgsTask":
        """构造并立即提交任务，返回 :class:`QgsTask`。"""
        task = self.create(job)
        self.start(task)
        return task

    def cancel(self, task: "QgsTask") -> None:
        """取消任务（同时把取消标志传给算法反馈）。"""
        if task is not None and not task.isCanceled():
            task.cancel()


# ------------------------------------------------------------------ 网页查看器


def _extent_center_wgs84(
    extent: Tuple[float, float, float, float, str],
) -> tuple[float, float, tuple[float, float, float, float]]:
    """把作业范围换算到 WGS84，返回 ``(中心经度, 中心纬度, bounds)``。

    ``bounds`` 为 ``(min_lon, min_lat, max_lon, max_lat)``；换算失败时退回
    ``(0, 0, 全 0)``，查看器仍能打开但需要用户手动移动地图。
    """
    xmin, xmax, ymin, ymax, authid = extent
    if not authid:
        authid = "EPSG:4326"
    try:
        from qgis.core import (  # noqa: PLC0415 - 模块顶层不依赖 qgis
            QgsCoordinateReferenceSystem,
            QgsCoordinateTransform,
            QgsCoordinateTransformContext,
            QgsRectangle,
        )

        src = QgsCoordinateReferenceSystem(str(authid))
        dst = QgsCoordinateReferenceSystem("EPSG:4326")
        rect = QgsRectangle(float(xmin), float(ymin), float(xmax), float(ymax))
        if src.isValid() and src != dst:
            rect = QgsCoordinateTransform(
                src, dst, QgsCoordinateTransformContext()
            ).transformBoundingBox(rect)
        if not rect.isNull() and not rect.isEmpty():
            return (
                (rect.xMinimum() + rect.xMaximum()) / 2.0,
                (rect.yMinimum() + rect.yMaximum()) / 2.0,
                (
                    rect.xMinimum(),
                    rect.yMinimum(),
                    rect.xMaximum(),
                    rect.yMaximum(),
                ),
            )
    except Exception:  # noqa: BLE001 - 查看器只是锦上添花，换算失败不阻断切片
        pass
    return 0.0, 0.0, (0.0, 0.0, 0.0, 0.0)


def write_vector_tile_viewer(
    job: TileJob,
    *,
    title: str = "",
    layer_names: Iterable[str] | None = None,
) -> str:
    """在输出目录写一个离线 Leaflet + VectorGrid 的 ``index.html``。

    ``layer_names`` 应传 **pbf 里的真实图层名**
    （见 :func:`app.core.domain.tiles.inspect.inspect_vector_tiles`）；
    未传时退回 ``job.layer_names``。Leaflet 资源复制到输出目录，
    页面用相对路径引用，离线可用。矢量 pbf 需经 HTTP 访问，应用打开时
    由 :mod:`app.infra.viewer_server` 自动起本机服务。
    """
    from app.core.domain.tile_viewer import write_tile_viewer  # noqa: PLC0415

    os.makedirs(job.output_dir, exist_ok=True)
    _lon, _lat, bounds = _extent_center_wgs84(job.extent)
    names_source = job.layer_names if layer_names is None else layer_names
    names = [str(name) for name in names_source if str(name).strip()]
    return write_tile_viewer(
        job.output_dir,
        title=title,
        tile_template=job.xyz_template or "{z}/{x}/{y}.pbf",
        min_zoom=job.min_zoom,
        max_zoom=job.max_zoom,
        bounds=bounds,
        vector=True,
        layer_names=names,
    )


__all__ = ["TileJob", "TileService", "write_vector_tile_viewer"]
