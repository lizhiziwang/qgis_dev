"""矢量缓冲区任务数据类与服务门面。"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Optional

from app.core.domain.geom_cleanup import copy_with_widened_string_fields
from app.infra.logging import get_logger

if TYPE_CHECKING:
    from qgis.core import QgsProcessingFeedback, QgsVectorLayer

logger = get_logger(__name__)


@dataclass(frozen=True)
class BufferJob:
    """一次矢量缓冲区生成的参数。"""

    #: 输入矢量图层。必须是图层**对象**——memory 图层（如处理结果）的
    #: ``source()`` 带 ``uid`` 参数，离开工程上下文后 ``processing`` 无法按
    #: source 字符串重新加载（实测报 ``Could not load source layer for INPUT``）。
    layer: "QgsVectorLayer"
    #: 缓冲距离（数值部分，单位见 ``distance_unit``）
    distance: float = 100.0
    #: 距离单位（:class:`QgsUnitTypes.DistanceUnit` 的整数值，如 0=米 6=度）；
    #: ``None`` 表示已按图层坐标系单位给出（默认，兼容直接传图层单位的旧调用）。
    #: 服务端会按图层 CRS 的 ``mapUnits()`` 自动换算成算法要求的单位。
    distance_unit: Optional[int] = None
    #: 圆弧分段数（越大多边形越圆，代价是顶点更多）
    segments: int = 8
    #: 是否融合重叠的缓冲多边形（默认各自成多边形）
    dissolve: bool = False
    #: 端点样式，对应 ``native:buffer`` 的 END_CAP_STYLE：0 圆 / 1 平 / 2 方
    end_cap: int = 0
    #: 连接样式，对应 ``native:buffer`` 的 JOIN_STYLE：0 圆 / 1 斜接 / 2 平切
    join_style: int = 0
    #: 输出图层名（作为 ``memory:<name>`` 的临时图层；也用作文件输出时的图层名）
    output_name: str = "缓冲区结果"
    #: 输出文件路径（如 ``D:/xx/结果.gpkg``）。为 ``None`` 时结果写入临时 memory
    #: 图层；给定路径时写入文件（gpkg/shp 等，由扩展名决定），并返回从文件加载的图层。
    output_path: Optional[str] = None


def _to_layer_units(job: BufferJob) -> float:
    """把 ``job.distance`` 从其声明单位换算成图层坐标系单位。

    ``native:buffer`` 的 ``DISTANCE`` 要求用**图层坐标系单位**（投影 CRS 为米、
    地理 CRS 为度）。``distance_unit`` 为 ``None`` 时视为已是图层单位，原样返回
    （兼容旧调用）；否则按 :func:`QgsUnitTypes.fromUnitToUnitFactor` 线性换算。
    """
    from qgis.core import QgsUnitTypes

    if job.distance_unit is None:
        return job.distance
    layer_unit = job.layer.crs().mapUnits()
    if int(layer_unit) == int(QgsUnitTypes.DistanceUnknownUnit):
        # 图层 CRS 单位未知时无法换算，按原值交给算法
        return job.distance
    try:
        src = QgsUnitTypes.DistanceUnit(job.distance_unit)
    except (ValueError, TypeError):
        logger.warning("未知距离单位 %r，按图层单位原样使用", job.distance_unit)
        return job.distance
    return job.distance * QgsUnitTypes.fromUnitToUnitFactor(src, layer_unit)


class BufferService:
    """矢量缓冲区服务门面。

    **同步执行** ``native:buffer`` 并返回结果图层（调用方负责加入工程）。
    缓冲区操作对常规图层是秒级，同步执行即可；超大图层时主线程会短暂阻塞，
    这是有意的取舍（结果图层直接在主线程返回，无需跨线程移交）。

    依赖 Processing 插件（与 :class:`~app.core.domain.tiles.task.CreateTileTask`
    一致，真实 QGIS app 已加载；离线环境需先 ``Processing.initialize()``）。
    """

    def run(
        self,
        job: BufferJob,
        feedback: Optional["QgsProcessingFeedback"] = None,
    ) -> "QgsVectorLayer":
        """执行缓冲区生成，返回结果图层（已置入工程上下文，未加入工程）。"""
        from qgis import processing
        from qgis.core import QgsProcessingContext, QgsProcessingFeedback, QgsProject

        context = QgsProcessingContext()
        context.setProject(QgsProject.instance())
        fb = feedback or QgsProcessingFeedback()

        # GDB/ArcGIS 导出图层常见「字段声明长度 < 实际数据长度」的脏数据，
        # 直接交给 memory sink 会拒绝超长字符串值（报「无法将要素写入 OUTPUT」）。
        # 先把输入拷贝为字段长度已修正的临时图层，再交给算法。
        input_layer = copy_with_widened_string_fields(job.layer)
        # DISTANCE 要求图层坐标系单位；job.distance 可能带了用户单位，先换算
        distance = _to_layer_units(job)
        params = {
            "INPUT": input_layer,  # 传图层对象，不要传 job.layer.source()
            "DISTANCE": distance,
            "SEGMENTS": job.segments,
            "END_CAP_STYLE": job.end_cap,
            "JOIN_STYLE": job.join_style,
            "DISSOLVE": job.dissolve,
            "OUTPUT": job.output_path or f"memory:{job.output_name}",
        }
        logger.info(
            "执行算法 native:buffer：输出=%s 距离=%s 融合=%s",
            job.output_path or f"memory:{job.output_name}",
            job.distance,
            job.dissolve,
        )
        try:
            results = processing.run("native:buffer", params, context=context, feedback=fb)
        except Exception as exc:  # noqa: BLE001
            # 记录输入图层特征，便于定位"Could not write features to OUTPUT"这类
            # 算法级失败（sink 创建/写入失败，通常是图层类型/几何数据的问题）
            try:
                layer = job.layer
                logger.error(
                    "native:buffer 执行失败 %s: %s | 输入 provider=%s wkb=%s crs=%s "
                    "要素=%s 字段=%s dissolve=%s 距离=%s",
                    job.output_name,
                    exc,
                    layer.providerType(),
                    layer.wkbType(),
                    layer.crs().authid() or layer.crs().description() or "(无CRS)",
                    layer.featureCount(),
                    [f.name() for f in layer.fields()],
                    job.dissolve,
                    job.distance,
                    exc_info=True,
                )
            except Exception:  # noqa: BLE001
                logger.error("native:buffer 执行失败 %s: %s", job.output_name, exc, exc_info=True)
            raise

        if job.output_path:
            # 文件输出时 results["OUTPUT"] 是路径字符串，从文件重新加载图层
            from qgis.core import QgsVectorLayer

            layer = QgsVectorLayer(results["OUTPUT"], job.output_name, "ogr")
        else:
            layer = results["OUTPUT"]
        if layer is None or not layer.isValid():
            raise RuntimeError(f"缓冲区结果图层无效：{job.output_name}")
        logger.info(
            "native:buffer 完成 → %s（%s 要素%s）",
            layer.name(),
            layer.featureCount(),
            f"，文件={job.output_path}" if job.output_path else "",
        )
        return layer
