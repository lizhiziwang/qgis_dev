"""最小外包矩形任务数据类与服务门面（``native:orientedminimumboundingbox``）。"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

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

    layer: QgsVectorLayer
    output_name: str = "最小外包矩形结果"
    output_path: str | None = None


class OrientedBBoxService:
    """最小外包矩形服务门面。

    两条执行路径共用同一套参数准备：

    - :meth:`run`：同步执行 ``native:orientedminimumboundingbox``，供测试 /
      主线程场景；
    - :meth:`build_task`：返回后台任务（``QgsProcessingAlgRunnerTask``），dialog
      交给 ``QgsTaskManager`` 后算法在 worker 线程跑，主线程事件循环不阻塞；
      结果经 ``executed`` 信号回主线程后由 :meth:`finish` 装配。

    与 :class:`~app.core.domain.buffer.service.BufferService` 风格一致：调用方
    负责把结果加入工程。
    """

    def _prepare_params(self, job: OrientedBBoxJob) -> dict:
        """主线程准备算法参数（含临时图层创建）。"""
        # GDB/ArcGIS 等导出的图层常带「字段声明长度 < 实际数据长度」的脏数据，
        # 直接交给 memory sink 会被拒（与 native:buffer 同问题，复用同一兜底）。
        input_layer = copy_with_widened_string_fields(job.layer)
        return {
            "INPUT": input_layer,  # 传图层对象，不要传 job.layer.source()
            "OUTPUT": job.output_path or f"memory:{job.output_name}",
        }

    def run(
        self,
        job: OrientedBBoxJob,
        feedback: QgsProcessingFeedback | None = None,
    ) -> QgsVectorLayer:
        """同步执行最小外包矩形生成，返回结果图层（已置入工程上下文，未加入工程）。"""
        from qgis import processing
        from qgis.core import QgsProcessingContext, QgsProcessingFeedback, QgsProject

        context = QgsProcessingContext()
        context.setProject(QgsProject.instance())
        fb = feedback or QgsProcessingFeedback()

        params = self._prepare_params(job)
        logger.info(
            "执行算法 native:orientedminimumboundingbox：输出=%s",
            params["OUTPUT"],
        )
        results = processing.run(
            "native:orientedminimumboundingbox",
            params,
            context=context,
            feedback=fb,
        )

        return self.finish(job, context, results)

    def build_task(
        self,
        job: OrientedBBoxJob,
        feedback: QgsProcessingFeedback | None = None,
    ):
        """构造后台执行任务（**不启动**），返回 ``(task, context, feedback)``。

        ``context`` / ``feedback`` 调用方必须持有到任务结束
        （``QgsProcessingAlgRunnerTask`` 内部保存的是裸指针，被 GC 会悬垂）。
        参数准备（临时 memory 图层）在**主线程**完成，worker 只跑算法本身。
        """
        from qgis.core import QgsApplication, QgsProcessingContext, QgsProject

        from app.core.domain.alg_task import AlgRunnerTask
        from app.core.domain.imagery_tiles.feedback import ProcessingLogFeedback

        alg = QgsApplication.processingRegistry().algorithmById(
            "native:orientedminimumboundingbox"
        )
        if alg is None:
            raise RuntimeError("算法未注册或已被卸载：native:orientedminimumboundingbox")
        params = self._prepare_params(job)
        context = QgsProcessingContext()
        context.setProject(QgsProject.instance())
        fb = feedback or ProcessingLogFeedback()
        logger.info(
            "准备后台任务 native:orientedminimumboundingbox：输出=%s",
            params["OUTPUT"],
        )
        task = AlgRunnerTask(alg, params, context, fb, keepalive=params)
        return task, context, fb

    def finish(
        self,
        job: OrientedBBoxJob,
        context,
        results: dict | None,
    ) -> QgsVectorLayer:
        """主线程装配任务结果（同步 / 后台两条路径共用）。"""
        from app.core.domain.alg_task import resolve_vector_output

        layer = resolve_vector_output(
            context, (results or {}).get("OUTPUT"), job.output_name
        )
        if layer is None or not layer.isValid():
            raise RuntimeError(f"最小外包矩形结果图层无效：{job.output_name}")
        logger.info(
            "native:orientedminimumboundingbox 完成 → %s（%s 要素%s）",
            layer.name(),
            layer.featureCount(),
            f"，文件={job.output_path}" if job.output_path else "",
        )
        return layer
