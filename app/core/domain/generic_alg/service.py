"""通用算法任务数据类与服务门面。

为 tools_dock 上挂的 59 个 ``gdal:*`` 算法（以及将来任意 processing 算法）
提供统一的 service 接口：dialog 只负责参数收集 + 触发执行，service 负责
实际 ``processing.run`` + 结果图层装配。

设计要点：

- **destination 处理**：GDAL 算法的输出参数（``OUTPUT`` / ``OUTPUT_RASTER`` /
  ``OUTPUT_VECTOR`` / ``OUTPUT_FILE`` / ``OUTPUT_FOLDER`` 等）由 service
  根据输出定义类型做差异处理：
  - ``VectorDestination`` + 用户给了输出路径 → 从路径重新加载成 ``QgsVectorLayer``
  - ``VectorDestination`` + 留空 → ``processing.run`` 直接给 memory layer
  - ``RasterDestination`` 同理，但 GDAL 不会输出 memory raster，service 从路径 reload
  - ``FolderDestination`` / ``FileDestination`` / ``HtmlDestination`` →
    ``processing.run`` 返回的是路径字符串，service 把字符串包成单字段 dict 返回
    （dialog 弹 messagebar 即可，不调 ``addMapLayer``）
  - 其它（``NumberOutput`` / ``StringOutput``）→ 同字符串模式
- **输入图层（vector source / raster layer）**：若参数值是图层对象（dialog 通过
  ``MapLayerWidgetWrapper`` 拿到的就是），先经
  :func:`copy_with_widened_string_fields` 兜底 GDB 脏数据；其它类型透传
- **feedback 不挂 progressChanged**：与 :class:`BufferService` 同原因，
  直连/排队连接都会触发原生崩溃 ``0xC0000409``
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Optional, TYPE_CHECKING

from app.core.domain.geom_cleanup import copy_with_widened_string_fields
from app.infra.logging import get_logger

if TYPE_CHECKING:
    from qgis.core import (
        QgsMapLayer,
        QgsProcessingContext,
        QgsProcessingFeedback,
        QgsProcessingOutputDefinition,
        QgsProcessingParameterDefinition,
    )

logger = get_logger(__name__)


@dataclass(frozen=True)
class GenericAlgJob:
    """一次 processing 算法调用的参数集合。

    :attr algo_id: ``native:buffer`` / ``gdal:warpreproject`` 等完整算法 id
    :attr params: 已经过 dialog 收集好的 ``{name: value}`` 字典；service 会
        自动覆盖 ``OUTPUT*`` 字段（按 :attr output_path / output_name）
    :attr output_name: 无 output_path 时，结果图层 / 字符串用的显示名
    :attr output_path: 用户在 dialog 底部填的具体输出路径；留空则走
        ``memory:<name>`` 或 processing 默认临时路径
    """

    algo_id: str
    params: dict = field(default_factory=dict)
    output_name: str = "处理结果"
    output_path: Optional[str] = None


#: result 标记：result 是路径字符串而非图层（dialog 据此只发 messagebar）
RESULT_IS_PATH = "path"


class GenericAlgService:
    """通用 processing 算法 service。

    实例无状态，可多次复用。``run`` 同步调用 ``processing.run``，
    返回值类型随算法输出定义变化：

    - :class:`QgsMapLayer`（VectorDestination / RasterDestination + path 模式）
    - :class:`QgsMapLayer`（VectorDestination + memory 模式，processing 直接给图层）
    - ``dict`` 含 ``RESULT_IS_PATH`` 标记（其它类型）

    :class:`app.ui.dialogs.generic_alg.GenericAlgDialog` 据返回值类型决定
    是 ``project.addMapLayer`` 还是 messagebar 提示路径。
    """

    def run(
        self,
        job: GenericAlgJob,
        feedback: Optional["QgsProcessingFeedback"] = None,
    ) -> Any:
        """执行算法并按输出定义装配返回值。失败抛 ``RuntimeError``。"""
        from qgis import processing
        from qgis.core import (
            QgsProcessingContext,
            QgsProcessingFeedback,
            QgsProject,
            QgsRasterLayer,
            QgsVectorLayer,
        )

        alg = self._get_algorithm(job.algo_id)
        context = QgsProcessingContext()
        context.setProject(QgsProject.instance())
        fb = feedback or QgsProcessingFeedback()

        params = dict(job.params)  # 拷贝避免污染 dialog 状态
        params = self._clean_inputs(alg, params)

        # 注入 OUTPUT 占位符（按 destination 类型）
        primary_output = self._primary_destination(alg)
        if primary_output is not None:
            params[primary_output.name()] = (
                job.output_path or f"memory:{job.output_name}"
            )

        logger.info(
            "执行算法 %s：输出=%s",
            job.algo_id,
            job.output_path or f"memory:{job.output_name}",
        )
        results = processing.run(
            job.algo_id,
            params,
            context=context,
            feedback=fb,
        )
        logger.info("%s 完成：%s", job.algo_id, self._describe_output(primary_output, results))

        # 按输出定义装配返回值
        if primary_output is not None:
            return self._wrap_destination_result(
                results, primary_output, job
            )

        # 无 destination（gdal:ogrinfo / gdal:gdalinfo / gdal:dataset_identify）
        # 整体当作路径 / 字符串结果返回；dialog 不加图层
        return {
            RESULT_IS_PATH: True,
            "name": job.output_name,
            "value": str(results.get(job.output_name, "")),
        }

    # ----------------------------------------------------------- helpers

    def _get_algorithm(self, algo_id: str):
        from qgis.core import QgsApplication

        alg = QgsApplication.processingRegistry().algorithmById(algo_id)
        if alg is None:
            raise RuntimeError(f"算法未注册或已被卸载：{algo_id}")
        return alg

    def _clean_inputs(self, alg, params: dict) -> dict:
        """对 vector/raster source 输入做 GDB 兜底清洗；其它透传。"""
        from qgis.core import QgsVectorLayer

        out = {}
        for pdef in alg.parameterDefinitions():
            name = pdef.name()
            if name not in params:
                continue
            value = params[name]
            cleaned = self._clean_input_value(pdef, value)
            if cleaned is not None:
                out[name] = cleaned
        return out

    def _clean_input_value(self, pdef, value):
        """单个参数值清洗：vector layer → 修正字段长度；其它原样返回。"""
        from qgis.core import QgsVectorLayer

        # 仅在传入图层对象且是矢量图层时做字段宽度修正
        if isinstance(value, QgsVectorLayer):
            try:
                return copy_with_widened_string_fields(value)
            except Exception:  # noqa: BLE001
                # 兜底失败不应阻塞算法（极少数情况下字段扫描会抛），
                # 服务层记日志由 caller 处置
                return value
        return value

    def _primary_destination(self, alg):
        """取第一个 destination 类输出（GDAL 算法大多只有一个 OUTPUT）。"""
        for odef in alg.destinationParameterDefinitions():
            return odef
        return None

    def _describe_output(self, primary_output, results: dict) -> str:
        """把 ``results`` 里的主输出整理成日志可读的字符串。"""
        if primary_output is None:
            return "无输出（信息型算法）"
        value = results.get(primary_output.name())
        if value is None:
            return f"{primary_output.name()}=(空)"
        name = value.name() if hasattr(value, "name") else str(value)
        return f"{primary_output.name()}={name}"

    def _wrap_destination_result(self, results: dict, dest_param, job: GenericAlgJob):
        """按 destination 参数类型把 ``results["OUTPUT"]`` 包装成图层或路径 dict。

        GDAL 算法的 ``results["OUTPUT"]`` 几乎总是文件路径字符串（即使在
        memory 模式下，GDAL 工具也走临时 GeoTIFF 落盘），不会直接给图层对象。
        这里按 destination 参数类型决定怎么解读返回值：

        - :class:`QgsProcessingParameterRasterDestination` → ``QgsRasterLayer``
        - :class:`QgsProcessingParameterVectorDestination` → ``QgsVectorLayer``
        - :class:`QgsProcessingParameterFileDestination` → 当成文件路径返回
        - :class:`QgsProcessingParameterFolderDestination` → 当成目录返回
        - :class:`QgsProcessingParameterHtmlDestination` → 当成文件路径返回
        - 其它 / 未匹配 → 字符串透传
        """
        from qgis.core import (
            QgsProcessingParameterFileDestination,
            QgsProcessingParameterFolderDestination,
            QgsProcessingParameterRasterDestination,
            QgsProcessingParameterVectorDestination,
            QgsRasterLayer,
            QgsVectorLayer,
        )

        name = dest_param.name()
        value = results.get(name)

        # 内存对象（native:buffer 等 QGIS native 算法偶尔会直出图层对象）
        if value is not None and hasattr(value, "isValid"):
            if not value.isValid():
                raise RuntimeError(
                    f"{job.algo_id} 输出图层无效：{name}"
                )
            return value

        if isinstance(value, str):
            if isinstance(dest_param, (QgsProcessingParameterRasterDestination,)):
                layer = QgsRasterLayer(value, job.output_name, "gdal")
                if layer is None or not layer.isValid():
                    raise RuntimeError(
                        f"{job.algo_id} 输出栅格无法加载：{value}"
                    )
                return layer
            if isinstance(dest_param, (QgsProcessingParameterVectorDestination,)):
                layer = QgsVectorLayer(value, job.output_name, "ogr")
                if layer is None or not layer.isValid():
                    raise RuntimeError(
                        f"{job.algo_id} 输出矢量无法加载：{value}"
                    )
                return layer
            if isinstance(dest_param, (
                QgsProcessingParameterFileDestination,
                QgsProcessingParameterFolderDestination,
            )):
                return {
                    RESULT_IS_PATH: True,
                    "name": job.output_name,
                    "value": value,
                }

            # 未知 destination 类型（如 HtmlDestination） → 当作路径返回
            return {
                RESULT_IS_PATH: True,
                "name": job.output_name,
                "value": value,
            }

        # 其它（number / string 等非路径结果）
        return {
            RESULT_IS_PATH: True,
            "name": job.output_name,
            "value": str(value) if value is not None else "",
        }