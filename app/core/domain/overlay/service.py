"""矢量叠加分析任务数据类与服务门面。"""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import TYPE_CHECKING, Dict, Optional, Tuple

from app.core.domain.geom_cleanup import copy_with_widened_string_fields
from app.infra.logging import get_logger

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
    input_layer: "QgsVectorLayer"
    #: 叠加矢量图层
    overlay_layer: "QgsVectorLayer"
    #: 勾选的运算 key 集合（交集/并集/差集/对称差）
    operations: Tuple[str, ...] = ("intersection",)
    #: 输出图层名前缀，结果命名为 ``{前缀}_{运算中文名}``
    output_prefix: str = "叠加结果"
    #: 输出目录。为 ``None`` 时结果写入临时 memory 图层；给定目录时每个运算写一个
    #: ``{前缀}_{运算中文名}.gpkg`` 文件，并返回从文件加载的图层。
    output_dir: Optional[str] = None


class OverlayService:
    """矢量叠加分析服务门面。

    **同步执行**所选叠加运算（``native:intersection`` / ``native:union`` /
    ``native:difference`` / ``native:symmetricaldifference``），每个运算生成一个
    memory 结果图层，以 ``{key: 图层}`` 字典返回（调用方负责加入工程）。常规图层
    秒级完成，同步执行即可。

    依赖 Processing 插件（与 :class:`~app.core.domain.buffer.service.BufferService`
    一致）。
    """

    def run(
        self,
        job: OverlayJob,
        feedback: Optional["QgsProcessingFeedback"] = None,
    ) -> Dict[str, "QgsVectorLayer"]:
        """执行所选叠加运算，返回 ``{运算 key: 结果图层}``。"""
        from qgis import processing
        from qgis.core import QgsProcessingContext, QgsProcessingFeedback, QgsProject

        context = QgsProcessingContext()
        context.setProject(QgsProject.instance())
        fb = feedback or QgsProcessingFeedback()

        # 输入/叠加图层都可能来自 GDB，先做字符串字段长度修正，避免 memory sink
        # 拒绝写入超长字符串值（同 buffer 的「无法将要素写入 OUTPUT」问题）。
        input_layer = copy_with_widened_string_fields(job.input_layer)
        overlay_layer = copy_with_widened_string_fields(job.overlay_layer)

        results: Dict[str, "QgsVectorLayer"] = {}
        for op in job.operations:
            alg = OPERATION_ALGORITHMS.get(op)
            if alg is None:
                logger.warning("未知叠加运算 %r，跳过", op)
                continue
            label = OPERATION_NAMES.get(op, op)
            output_name = f"{job.output_prefix}_{label}"
            if job.output_dir:
                # 目录输出：每个运算写一个独立 gpkg 文件（文件内仅一层，可按路径加载）
                file_path = os.path.join(job.output_dir, f"{output_name}.gpkg")
                params = {
                    "INPUT": input_layer,
                    "OVERLAY": overlay_layer,
                    "OUTPUT": file_path,
                }
            else:
                params = {
                    "INPUT": input_layer,
                    "OVERLAY": overlay_layer,
                    "OUTPUT": f"memory:{output_name}",
                }
            try:
                res = processing.run(alg, params, context=context, feedback=fb)
            except Exception as exc:  # noqa: BLE001
                logger.error(
                    "%s 执行失败（前缀 %s）: %s | 输入 wkb=%s/%s crs=%s/%s 要素=%s/%s",
                    alg,
                    job.output_prefix,
                    exc,
                    input_layer.wkbType(),
                    overlay_layer.wkbType(),
                    input_layer.crs().authid() or "(无CRS)",
                    overlay_layer.crs().authid() or "(无CRS)",
                    input_layer.featureCount(),
                    overlay_layer.featureCount(),
                    exc_info=True,
                )
                raise

            if job.output_dir:
                # 文件输出时 results["OUTPUT"] 是路径字符串，从文件重新加载图层
                from qgis.core import QgsVectorLayer

                layer = QgsVectorLayer(res["OUTPUT"], output_name, "ogr")
            else:
                layer = res["OUTPUT"]
            if layer is None or not layer.isValid():
                raise RuntimeError(f"{label} 结果图层无效")
            results[op] = layer
        return results
