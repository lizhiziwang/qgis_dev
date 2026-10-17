"""矢量叠加分析任务数据类与服务门面。"""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import TYPE_CHECKING

from app.core.domain.geom_cleanup import copy_with_widened_string_fields
from app.infra.logging import get_logger, log_operation

if TYPE_CHECKING:
    from qgis.core import QgsProcessingFeedback, QgsVectorLayer

logger = get_logger(__name__)


#: 叠加运算注册表：(内部 key, 中文名, processing 算法 id)。
#: 参考 QGIS 原始"叠加分析"工具的运算集合；"标识"(identity) 在本版 QGIS
#: 无对应 native 算法，故不提供。
_OVERLAY_OPERATIONS = (
    ("intersection", "交集", "native:intersection"),
    ("union", "并集", "native:union"),
    ("difference", "差集", "native:difference"),
    ("symmetricaldifference", "对称差", "native:symmetricaldifference"),
)
#: 内部 key → 中文名
OPERATION_NAMES = {key: label for key, label, _ in _OVERLAY_OPERATIONS}
#: 内部 key → processing 算法 id
OPERATION_ALGORITHMS = {key: alg for key, _, alg in _OVERLAY_OPERATIONS}


@dataclass(frozen=True)
class OverlayJob:
    """一次矢量叠加分析的参数。

    :param operations: 要执行的运算 key 子集（见 :data:`OPERATION_ALGORITHMS`）。
    """

    #: 输入矢量图层。必须是图层**对象**（memory 图层的 source 带 uid，离开工程
    #: 上下文 processing 无法按字符串重载——同 :class:`BufferJob` 的约定）。
    input_layer: QgsVectorLayer
    #: 叠加矢量图层
    overlay_layer: QgsVectorLayer
    #: 勾选的运算 key 集合（交集/并集/差集/对称差）
    operations: tuple[str, ...] = ("intersection",)
    #: 输出图层名前缀，结果命名为 ``{前缀}_{运算中文名}``
    output_prefix: str = "叠加结果"
    #: 输出目录。为 ``None`` 时结果写入临时 memory 图层；给定目录时每个运算写一个
    #: ``{前缀}_{运算中文名}.gpkg`` 文件，并返回从文件加载的图层。
    output_dir: str | None = None


@dataclass(frozen=True)
class OverlayRun:
    """一次叠加分析的执行计划：输入图层已清洗，可逐个运算复用。

    ``operations`` 已过滤掉未知 key；``input_layer`` / ``overlay_layer`` 是主线程
    创建的临时图层，必须持有到**整轮**任务结束（任务内部只持裸指针）。
    """

    job: OverlayJob
    input_layer: QgsVectorLayer
    overlay_layer: QgsVectorLayer
    operations: tuple[str, ...]


class OverlayService:
    """矢量叠加分析服务门面。

    一次叠加分析是多个算法串行执行。两条路径共用同一套参数准备：

    - :meth:`run`：同步逐个 ``processing.run``，供测试 / 主线程场景；
    - :meth:`build_task`：为其中**一个**运算构造后台任务
      （``QgsProcessingAlgRunnerTask``）。dialog 在任务 ``executed`` 后调
      :meth:`finish_op` 取回结果，再启动下一个；每个算法都在 worker 线程执行，
      主线程事件循环不阻塞。

    依赖 Processing 插件（与 :class:`~app.core.domain.buffer.service.BufferService`
    一致）。
    """

    def prepare(self, job: OverlayJob) -> OverlayRun:
        """主线程准备执行计划（输入图层只清洗一次，各运算复用）。"""
        operations = tuple(op for op in job.operations if op in OPERATION_ALGORITHMS)
        for op in job.operations:
            if op not in OPERATION_ALGORITHMS:
                logger.warning("未知叠加运算 %r，跳过", op)
        if not operations:
            raise ValueError("没有可执行的叠加运算")

        # 输入/叠加图层都可能来自 GDB，先做字符串字段长度修正，避免 memory sink
        # 拒绝写入超长字符串值（同 buffer 的「无法将要素写入 OUTPUT」问题）。
        return OverlayRun(
            job=job,
            input_layer=copy_with_widened_string_fields(job.input_layer),
            overlay_layer=copy_with_widened_string_fields(job.overlay_layer),
            operations=operations,
        )

    @staticmethod
    def output_name(run: OverlayRun, op: str) -> str:
        """某个运算的结果图层名 / 文件名（``{前缀}_{运算中文名}``）。"""
        return f"{run.job.output_prefix}_{OPERATION_NAMES.get(op, op)}"

    def _params_for(self, run: OverlayRun, op: str) -> dict:
        """单个运算的算法参数。"""
        name = self.output_name(run, op)
        # 目录输出：每个运算写一个独立 gpkg 文件（文件内仅一层，可按路径加载）
        output = (
            os.path.join(run.job.output_dir, f"{name}.gpkg")
            if run.job.output_dir
            else f"memory:{name}"
        )
        return {
            "INPUT": run.input_layer,
            "OVERLAY": run.overlay_layer,
            "OUTPUT": output,
        }

    @log_operation("矢量叠加分析")
    def run(
        self,
        job: OverlayJob,
        feedback: QgsProcessingFeedback | None = None,
    ) -> dict[str, QgsVectorLayer]:
        """同步执行所选叠加运算，返回 ``{运算 key: 结果图层}``。"""
        from qgis import processing
        from qgis.core import QgsProcessingContext, QgsProcessingFeedback, QgsProject

        run = self.prepare(job)
        context = QgsProcessingContext()
        context.setProject(QgsProject.instance())
        fb = feedback or QgsProcessingFeedback()

        results: dict[str, QgsVectorLayer] = {}
        for op in run.operations:
            alg = OPERATION_ALGORITHMS[op]
            params = self._params_for(run, op)
            logger.info(
                "执行算法 %s：输出=%s", alg, params["OUTPUT"]
            )
            try:
                res = processing.run(alg, params, context=context, feedback=fb)
            except Exception as exc:
                logger.error(
                    "%s 执行失败（前缀 %s）: %s | 输入 wkb=%s/%s crs=%s/%s 要素=%s/%s",
                    alg,
                    job.output_prefix,
                    exc,
                    run.input_layer.wkbType(),
                    run.overlay_layer.wkbType(),
                    run.input_layer.crs().authid() or "(无CRS)",
                    run.overlay_layer.crs().authid() or "(无CRS)",
                    run.input_layer.featureCount(),
                    run.overlay_layer.featureCount(),
                    exc_info=True,
                )
                raise

            results[op] = self.finish_op(run, op, context, res)
        return results

    def build_task(
        self,
        run: OverlayRun,
        op: str,
        feedback: QgsProcessingFeedback | None = None,
    ):
        """为 ``run`` 里的一个运算构造后台任务（**不启动**），返回
        ``(task, context, feedback)``。

        ``context`` / ``feedback`` 调用方必须持有到该任务结束
        （``QgsProcessingAlgRunnerTask`` 内部保存的是裸指针，被 GC 会悬垂）。
        """
        from qgis.core import QgsApplication, QgsProcessingContext, QgsProject

        from app.core.domain.alg_task import AlgRunnerTask
        from app.core.domain.imagery_tiles.feedback import ProcessingLogFeedback

        alg_id = OPERATION_ALGORITHMS[op]
        alg = QgsApplication.processingRegistry().algorithmById(alg_id)
        if alg is None:
            raise RuntimeError(f"算法未注册或已被卸载：{alg_id}")
        params = self._params_for(run, op)
        context = QgsProcessingContext()
        context.setProject(QgsProject.instance())
        fb = feedback or ProcessingLogFeedback()
        logger.info("准备后台任务 %s：输出=%s", alg_id, params["OUTPUT"])
        task = AlgRunnerTask(alg, params, context, fb, keepalive=params)
        return task, context, fb

    def finish_op(
        self,
        run: OverlayRun,
        op: str,
        context,
        results: dict | None,
    ) -> QgsVectorLayer:
        """主线程装配单个运算的结果（同步 / 后台两条路径共用）。"""
        from app.core.domain.alg_task import resolve_vector_output

        alg = OPERATION_ALGORITHMS[op]
        label = OPERATION_NAMES.get(op, op)
        layer = resolve_vector_output(
            context, (results or {}).get("OUTPUT"), self.output_name(run, op)
        )
        if layer is None or not layer.isValid():
            raise RuntimeError(f"{label} 结果图层无效")
        logger.info("%s 完成 → %s（%s 要素）", alg, layer.name(), layer.featureCount())
        return layer
