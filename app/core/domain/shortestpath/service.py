"""最短路径分析任务数据类与服务门面。"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Optional, Tuple

from app.infra.logging import get_logger

if TYPE_CHECKING:
    from qgis.core import QgsPointXY, QgsProcessingFeedback, QgsVectorLayer

logger = get_logger(__name__)

#: 路径类型：(算法取值, 中文名)。0 最短距离 / 1 最快时间（native:shortestpathpointtopoint 的 STRATEGY）
STRATEGY_NAMES = {0: "最短距离", 1: "最快时间"}
#: 默认方向：(算法取值, 中文名)。0 顺向 / 1 逆向 / 2 双向
DEFAULT_DIRECTION_NAMES = {0: "顺向", 1: "逆向", 2: "双向"}


@dataclass(frozen=True)
class ShortestPathJob:
    """一次最短路径分析的参数（对应 ``native:shortestpathpointtopoint``）。

    网络必须是线图层；起终点为地图坐标（图层坐标系单位）。结果图层只携带
    起点/终点/代价三个字段（算法不复制网络字段），故无 GDB 字符串字段长度问题。
    """

    #: 网络矢量图层（线图层）。必须是图层**对象**——同 :class:`BufferJob` 的约定，
    #: memory 图层 source 带 ``uid``，离开工程上下文无法按字符串重载。
    network_layer: "QgsVectorLayer"
    #: 起点（图层坐标系坐标）
    start_point: "QgsPointXY"
    #: 终点（图层坐标系坐标）
    end_point: "QgsPointXY"
    #: 路径类型：0 最短距离 / 1 最快时间
    strategy: int = 0
    #: 方向字段名（用于表达单行线）；``None`` 表示网络无方向限制
    direction_field: Optional[str] = None
    #: 方向字段中表示顺向 / 逆向 / 双向的值（仅当 ``direction_field`` 给定时使用）
    value_forward: str = ""
    value_backward: str = ""
    value_both: str = ""
    #: 默认方向（无方向字段时生效）：0 顺向 / 1 逆向 / 2 双向
    default_direction: int = 2
    #: 速度字段名（最快时间策略用）；``None`` 表示用 ``default_speed``
    speed_field: Optional[str] = None
    #: 默认速度（km/h），最快时间策略且无速度字段时使用
    default_speed: float = 50.0
    #: 拓扑容差（图层坐标单位，默认 0）
    tolerance: float = 0.0
    #: 起终点到网络的吸附容差；``None`` 表示不限
    point_tolerance: Optional[float] = None
    #: 输出图层名（``memory:<name>`` 临时图层；也用作文件输出时的图层名）
    output_name: str = "最短路径"
    #: 输出文件路径（gpkg/shp 等，由扩展名决定）。``None`` 时写临时 memory 图层。
    output_path: Optional[str] = None


def build_shortest_path_params(job: "ShortestPathJob", output: str) -> dict:
    """构造 ``native:shortestpathpointtopoint`` 的参数。

    同步服务与后台任务共用同一份参数逻辑，避免两处漂移。
    """
    params = {
        "INPUT": job.network_layer,
        "STRATEGY": job.strategy,
        "START_POINT": job.start_point,
        "END_POINT": job.end_point,
        "DEFAULT_SPEED": job.default_speed,
        "TOLERANCE": job.tolerance,
        "OUTPUT": output,
    }
    if job.direction_field:
        params["DIRECTION_FIELD"] = job.direction_field
        params["VALUE_FORWARD"] = job.value_forward
        params["VALUE_BACKWARD"] = job.value_backward
        params["VALUE_BOTH"] = job.value_both
    else:
        # 无方向字段时 DEFAULT_DIRECTION 控制方向处理
        params["DEFAULT_DIRECTION"] = job.default_direction
    if job.speed_field:
        params["SPEED_FIELD"] = job.speed_field
    if job.point_tolerance is not None:
        params["POINT_TOLERANCE"] = job.point_tolerance
    return params


class ShortestPathService:
    """最短路径分析服务门面。

    **同步执行** ``native:shortestpathpointtopoint``，返回 ``(结果图层, 总代价)``。
    总代价：最短距离策略为图层单位长度；最快时间策略为秒（算法按网络字段/默认
    速度折算）。常规网络秒级完成，同步执行即可（同 :class:`BufferService`）。
    """

    def run(
        self,
        job: ShortestPathJob,
        feedback: Optional["QgsProcessingFeedback"] = None,
    ) -> Tuple["QgsVectorLayer", Optional[float]]:
        """执行最短路径分析，返回 ``(结果图层, 总代价)``。"""
        from qgis import processing
        from qgis.core import QgsProcessingContext, QgsProcessingFeedback, QgsProject

        context = QgsProcessingContext()
        context.setProject(QgsProject.instance())
        fb = feedback or QgsProcessingFeedback()

        params = build_shortest_path_params(job, job.output_path or f"memory:{job.output_name}")

        logger.info(
            "执行算法 native:shortestpathpointtopoint：输出=%s 起点=%s 终点=%s strategy=%s",
            job.output_path or f"memory:{job.output_name}",
            job.start_point.asWkt(),
            job.end_point.asWkt(),
            job.strategy,
        )
        try:
            res = processing.run(
                "native:shortestpathpointtopoint", params, context=context, feedback=fb
            )
        except Exception as exc:  # noqa: BLE001
            logger.error(
                "native:shortestpathpointtopoint 执行失败 %s: %s | 起点=%s 终点=%s "
                "strategy=%s 方向字段=%s",
                job.output_name,
                exc,
                job.start_point.asWkt(),
                job.end_point.asWkt(),
                job.strategy,
                job.direction_field,
                exc_info=True,
            )
            raise

        if job.output_path:
            # 文件输出时 res["OUTPUT"] 是路径字符串，从文件重新加载图层
            from qgis.core import QgsVectorLayer

            layer = QgsVectorLayer(res["OUTPUT"], job.output_name, "ogr")
        else:
            layer = res["OUTPUT"]
        if layer is None or not layer.isValid():
            raise RuntimeError(f"最短路径结果图层无效：{job.output_name}")
        travel_cost = res.get("TRAVEL_COST")
        logger.info(
            "native:shortestpathpointtopoint 完成 → %s（总代价=%s%s）",
            layer.name(),
            travel_cost,
            f"，文件={job.output_path}" if job.output_path else "",
        )
        return layer, travel_cost
