"""最小外包矩形任务数据类与服务门面（``native:orientedminimumboundingbox``）。"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Optional

from app.core.domain.geom_cleanup import copy_with_widened_string_fields
from app.infra.logging import get_logger

if TYPE_CHECKING:
    from qgis.core import QgsProcessingFeedback, QgsVectorLayer

logger = get_logger(__name__)


@dataclass(frozen=True)
class OrientedBBoxJob:
    """一次「定向最小外包矩形」生成的参数。

    该算法由 QGIS core 提供（``QgsProcessingFeatureBasedAlgorithm``），按输入
    要素逐个生成对应方向的最小外包矩形面要素并写入结果图层。结果字段由算法
    自动注入（``angle`` / ``area`` / ``perimeter`` / ``width`` /
    ``height``），故参数面板只需选择输入图层。
    """

    layer: "QgsVectorLayer"
    output_name: str = "最小外包矩形结果"
    output_path: Optional[str] = None


class OrientedBBoxService:
    """最小外包矩形服务门面。

    **同步执行** ``native:orientedminimumboundingbox`` 并返回结果图层。
    与 :class:`~app.core.domain.buffer.service.BufferService` 风格一致：调用方
    负责把结果加入工程；小到中尺寸图层秒级完成，超大图层主线程短暂阻塞也是
    有意取舍（结果图层直接在主线程返回，无需跨线程移交）。
    """

    def run(
        self,
        job: OrientedBBoxJob,
        feedback: Optional["QgsProcessingFeedback"] = None,
    ) -> "QgsVectorLayer":
        """执行最小外包矩形生成，返回结果图层（已置入工程上下文，未加入工程）。"""
        from qgis import processing
        from qgis.core import QgsProcessingContext, QgsProcessingFeedback, QgsProject

        context = QgsProcessingContext()
        context.setProject(QgsProject.instance())
        fb = feedback or QgsProcessingFeedback()

        # GDB/ArcGIS 等导出的图层常带「字段声明长度 < 实际数据长度」的脏数据，
        # 直接交给 memory sink 会被拒（与 native:buffer 同问题，复用同一兜底）。
        input_layer = copy_with_widened_string_fields(job.layer)
        params = {
            "INPUT": input_layer,  # 传图层对象，不要传 job.layer.source()
            "OUTPUT": job.output_path or f"memory:{job.output_name}",
        }
        logger.info(
            "执行算法 native:orientedminimumboundingbox：输出=%s",
            job.output_path or f"memory:{job.output_name}",
        )
        results = processing.run(
            "native:orientedminimumboundingbox",
            params,
            context=context,
            feedback=fb,
        )

        if job.output_path:
            from qgis.core import QgsVectorLayer

            layer = QgsVectorLayer(results["OUTPUT"], job.output_name, "ogr")
        else:
            layer = results["OUTPUT"]
        if layer is None or not layer.isValid():
            raise RuntimeError(f"最小外包矩形结果图层无效：{job.output_name}")
        logger.info(
            "native:orientedminimumboundingbox 完成 → %s（%s 要素%s）",
            layer.name(),
            layer.featureCount(),
            f"，文件={job.output_path}" if job.output_path else "",
        )
        return layer