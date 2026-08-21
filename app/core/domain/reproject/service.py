"""矢量重投影任务数据类与服务门面。"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Optional

from app.core.domain.geom_cleanup import copy_with_widened_string_fields
from app.infra.logging import get_logger

if TYPE_CHECKING:
    from qgis.core import QgsCoordinateReferenceSystem, QgsProcessingFeedback, QgsVectorLayer

logger = get_logger(__name__)


@dataclass(frozen=True)
class ReprojectJob:
    """一次矢量重投影的参数。"""

    #: 输入矢量图层。必须是图层**对象**（memory 图层带 uid 的 source 离开工程
    #: 上下文 processing 无法重载——同 :class:`BufferJob` 的约定）。
    input_layer: "QgsVectorLayer"
    #: 目标坐标系（:class:`QgsCoordinateReferenceSystem`）。
    target_crs: "QgsCoordinateReferenceSystem"
    #: 输出图层名（作为 ``memory:<name>`` 的临时图层；也用作文件输出时的图层名）
    output_name: str = "重投影结果"
    #: 输出文件路径（如 ``D:/xx/结果.gpkg``）。为 ``None`` 时结果写入临时 memory
    #: 图层；给定路径时写入文件（gpkg/shp 等，由扩展名决定），并返回从文件加载的图层。
    output_path: Optional[str] = None


class ReprojectService:
    """矢量重投影服务门面。

    **同步执行** ``native:reprojectlayer`` 并返回结果图层（调用方负责加入工程）。
    常规图层秒级完成，同步执行即可（同 :class:`BufferService` 的取舍）。

    依赖 Processing 插件（与 :class:`~app.core.domain.buffer.service.BufferService`
    一致）。
    """

    def run(
        self,
        job: ReprojectJob,
        feedback: Optional["QgsProcessingFeedback"] = None,
    ) -> "QgsVectorLayer":
        """执行重投影，返回结果图层（已置入工程上下文，未加入工程）。"""
        from qgis import processing
        from qgis.core import QgsProcessingContext, QgsProcessingFeedback, QgsProject, QgsVectorLayer

        if not job.target_crs.isValid():
            raise ValueError("目标坐标系无效")

        context = QgsProcessingContext()
        context.setProject(QgsProject.instance())
        fb = feedback or QgsProcessingFeedback()

        # GDB/ArcGIS 导出图层的字符串字段长度脏数据，先拷贝为已修正的临时图层，
        # 避免 memory sink 拒绝超长字符串值（同 buffer/overlay 的修法）。
        input_layer = copy_with_widened_string_fields(job.input_layer)
        params = {
            "INPUT": input_layer,  # 传图层对象，不要传 job.input_layer.source()
            "TARGET_CRS": job.target_crs,
            "OUTPUT": job.output_path or f"memory:{job.output_name}",
        }
        logger.info(
            "执行算法 native:reprojectlayer：输出=%s 目标坐标系=%s",
            job.output_path or f"memory:{job.output_name}",
            job.target_crs.authid() or job.target_crs.description() or "(未指定)",
        )
        try:
            results = processing.run("native:reprojectlayer", params, context=context, feedback=fb)
        except Exception as exc:  # noqa: BLE001
            # 记录输入图层特征，便于定位 sink 创建/写入类失败
            try:
                layer = job.input_layer
                logger.error(
                    "native:reprojectlayer 执行失败 %s: %s | 输入 provider=%s wkb=%s crs=%s "
                    "要素=%s 目标=%s",
                    job.output_name,
                    exc,
                    layer.providerType(),
                    layer.wkbType(),
                    layer.crs().authid() or layer.crs().description() or "(无CRS)",
                    layer.featureCount(),
                    job.target_crs.authid() or job.target_crs.description(),
                    exc_info=True,
                )
            except Exception:  # noqa: BLE001
                logger.error("native:reprojectlayer 执行失败 %s: %s", job.output_name, exc, exc_info=True)
            raise

        if job.output_path:
            # 文件输出时 results["OUTPUT"] 是路径字符串，从文件重新加载图层
            layer = QgsVectorLayer(results["OUTPUT"], job.output_name, "ogr")
        else:
            layer = results["OUTPUT"]
        if layer is None or not layer.isValid():
            raise RuntimeError(f"重投影结果图层无效：{job.output_name}")
        logger.info(
            "native:reprojectlayer 完成 → %s（%s 要素%s）",
            layer.name(),
            layer.featureCount(),
            f"，文件={job.output_path}" if job.output_path else "",
        )
        return layer
