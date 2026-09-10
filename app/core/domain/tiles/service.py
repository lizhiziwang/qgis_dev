"""矢量瓦片任务数据类与服务门面。"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, List, Tuple

if TYPE_CHECKING:
    from qgis.core import QgsApplication, QgsTask


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

    业务层只跟 :class:`TileService` 打交道；具体 :class:`CreateTileTask` 由
    service 在 ``submit`` 时构造并提交到 :class:`QgsApplication.taskManager`。
    """

    def submit(self, job: TileJob) -> "QgsTask":
        """把任务提交到 :class:`QgsApplication.taskManager`，返回 :class:`QgsTask`。"""
        from qgis.core import QgsApplication

        from .task import CreateTileTask  # noqa: PLC0415 - 循环依赖：task 反向依赖 service，上提成环

        task = CreateTileTask("", job)
        manager = QgsApplication.taskManager()
        manager.addTask(task)
        return task

    def cancel(self, task: "QgsTask") -> None:
        """取消任务。"""
        if task is not None and not task.isCanceled():
            task.cancel()

