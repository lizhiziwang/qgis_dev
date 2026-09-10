"""流程编排：流程执行器。

按拓扑序执行节点代码，把每个节点的输出缓存起来供下游绑定。核心语义：

- **输入解析**：绑定上游的输出端口 → 取上游缓存值并按端口类型归一
  （路径字符串按需加载成图层对象；标量透传）；未绑定 → 用默认值。
- **代码执行**：节点代码在命名空间里 ``exec``，可直接引用 ``inputs`` /
  ``outputs`` / ``processing`` / ``context`` / ``feedback`` 及常用 QGIS 类。
- **输出处理**：图层输出可落盘（``save_path``）、可写入临时图层、可加载到
  QGIS 工程「流程输出」分组；最终值（图层对象 / 路径 / 标量）缓存供下游。

通过信号把节点级进度实时推给 UI（日志面板 / 节点状态灯），
执行本身是同步的（与仓库 buffer 服务一致）。
"""

from __future__ import annotations

import builtins
import sys
import time
import traceback
from pathlib import Path
from typing import Any

from qgis.PyQt.QtCore import QObject, pyqtSignal

from .model import FlowGraph, InputSpec, NodeSpec, OutputSpec
from .topo import topo_sort
from .types import PortType

#: 流程输出图层统一放入工程的这个分组
FLOW_OUTPUT_GROUP = "流程输出"


class FlowNodeExecutionError(RuntimeError):
    """某个节点执行失败（代码异常 / 输入解析失败 / 输出落盘失败）。"""

    def __init__(self, node_id: str, node_name: str, cause: BaseException) -> None:
        self.node_id = node_id
        self.node_name = node_name
        self.cause = cause
        tb = "".join(traceback.format_exception(type(cause), cause, cause.__traceback__))
        super().__init__(f"节点「{node_name}」执行失败：{tb}")


#: 节点状态枚举值（与 UI 节点状态灯对应）
STATUS_IDLE = "idle"
STATUS_RUNNING = "running"
STATUS_SUCCESS = "success"
STATUS_FAILED = "failed"
STATUS_DISABLED = "disabled"
STATUS_SKIPPED = "skipped"


class FlowExecutor(QObject):
    """流程执行器（QObject，通过信号通知 UI）。"""

    flow_started = pyqtSignal(int)  # 计划执行节点数（排除禁用/跳过）
    flow_finished = pyqtSignal(dict)  # {node_id: status}
    node_started = pyqtSignal(str, str)  # node_id, node_name
    node_finished = pyqtSignal(str, str, bool, float, str, str)
    #                                    node_id, node_name, ok, ms, summary, error

    def __init__(self, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self._results: dict[str, dict[str, Any]] = {}
        self._status: dict[str, str] = {}

    # ------------------------------------------------------------ 对外接口

    def reset(self) -> None:
        """清空运行缓存与状态。"""
        self._results.clear()
        self._status.clear()

    def results(self) -> dict[str, dict[str, Any]]:
        """最近一次运行（或单步执行）后各节点的输出缓存。"""
        return self._results

    def result_value(self, node_id: str, output_id: str) -> Any:
        """取某节点某输出端口的缓存值；未执行过返回 None。"""
        return self._results.get(node_id, {}).get(output_id)

    def status_of(self, node_id: str) -> str:
        return self._status.get(node_id, STATUS_IDLE)

    def run_flow(
        self,
        graph: FlowGraph,
        overrides: dict[str, dict[str, str]] | None = None,
    ) -> dict[str, str]:
        """按拓扑序运行整条流程，返回 ``{node_id: 状态}``。

        :param overrides: 本次运行的输入覆盖值 ``{node_id: {input_id: 原始字符串}}``，
            未绑定（或绑定到禁用/跳过上游）的输入优先取覆盖值，否则用默认值。
            只影响本次运行，不写回图模型 / JSON。

        :raises FlowCycleError: 图有环。
        :raises FlowNodeExecutionError: 某节点执行失败（流程中止）。
        """
        order = topo_sort(graph)
        self._results.clear()
        self._status = dict.fromkeys(graph.nodes, STATUS_IDLE)
        planned = sum(
            1
            for nid in order
            if graph.nodes[nid].enabled and not graph.nodes[nid].skip
        )
        self.flow_started.emit(planned)

        for nid in order:
            node = graph.nodes[nid]
            if not node.enabled:
                self._status[nid] = STATUS_DISABLED
                continue
            if node.skip:
                self._status[nid] = STATUS_SKIPPED
                continue
            try:
                self._run_node(graph, node, overrides)
                self._status[nid] = STATUS_SUCCESS
            except Exception:  # noqa: BLE001
                self._status[nid] = STATUS_FAILED
                self.flow_finished.emit(dict(self._status))
                raise

        self.flow_finished.emit(dict(self._status))
        return dict(self._status)

    def run_node(self, graph: FlowGraph, node_id: str) -> dict[str, Any]:
        """单节点分步执行：上游用缓存结果，缺缓存则退回默认值并记录警告。

        :raises FlowNodeExecutionError: 执行失败。
        """
        if node_id not in graph.nodes:
            raise KeyError(node_id)
        node = graph.nodes[node_id]
        if not node.enabled:
            raise ValueError("节点已禁用")
        if node.skip:
            raise ValueError("节点已标记跳过")
        return self._run_node(graph, node)

    # ------------------------------------------------------------ 内部

    def _run_node(
        self,
        graph: FlowGraph,
        node: NodeSpec,
        overrides: dict[str, dict[str, str]] | None = None,
    ) -> dict[str, Any]:
        self._status[node.id] = STATUS_RUNNING
        self.node_started.emit(node.id, node.name)
        t0 = time.perf_counter()
        try:
            resolved = self._resolve_inputs(graph, node, overrides)
            raw_outputs = self._exec_code(node, resolved)
            values = self._process_outputs(node, raw_outputs)
            self._results[node.id] = values
            ms = (time.perf_counter() - t0) * 1000.0
            summary = self._summarize(resolved, values)
            self.node_finished.emit(node.id, node.name, True, ms, summary, "")
            return values
        except Exception as exc:  # noqa: BLE001
            ms = (time.perf_counter() - t0) * 1000.0
            msg = str(exc)
            self.node_finished.emit(node.id, node.name, False, ms, "", msg)
            if not isinstance(exc, FlowNodeExecutionError):
                exc = FlowNodeExecutionError(node.id, node.name, exc)
            raise exc

    def _resolve_inputs(
        self,
        graph: FlowGraph,
        node: NodeSpec,
        overrides: dict[str, dict[str, str]] | None = None,
    ) -> dict[str, Any]:
        resolved: dict[str, Any] = {}
        for ins in node.inputs:
            binding = graph.input_binding(node.id, ins.id)
            if binding is not None:
                value = self._results.get(binding.node_id, {}).get(binding.port_id)
                if value is None:
                    # 上游没执行 / 输出无缓存 → 本次覆盖值优先，否则退回默认值
                    resolved[ins.name] = _override_or_default(node.id, ins, overrides)
                else:
                    resolved[ins.name] = _resolve_for_type(value, ins.type)
            else:
                resolved[ins.name] = _override_or_default(node.id, ins, overrides)
        return resolved

    def _exec_code(self, node: NodeSpec, resolved: dict[str, Any]) -> dict[str, Any]:
        from qgis import processing  # type: ignore

        _ensure_processing_ready()
        from qgis.core import (  # type: ignore
            QgsFeature,
            QgsField,
            QgsGeometry,
            QgsProcessingContext,
            QgsProcessingFeedback,
            QgsProject,
            QgsRasterFileWriter,
            QgsRasterLayer,
            QgsVectorFileWriter,
            QgsVectorLayer,
        )

        ctx = QgsProcessingContext()
        ctx.setProject(QgsProject.instance())
        feedback = QgsProcessingFeedback()
        ns: dict[str, Any] = {
            "__builtins__": builtins,
            "inputs": resolved,
            "outputs": {},
            "processing": processing,
            "context": ctx,
            "feedback": feedback,
            "QgsProject": QgsProject,
            "QgsVectorLayer": QgsVectorLayer,
            "QgsRasterLayer": QgsRasterLayer,
            "QgsVectorFileWriter": QgsVectorFileWriter,
            "QgsRasterFileWriter": QgsRasterFileWriter,
            "QgsField": QgsField,
            "QgsFeature": QgsFeature,
            "QgsGeometry": QgsGeometry,
        }
        code = node.code or "# 空代码：请在「执行逻辑」里填写"
        try:
            exec(compile(code, f"<flow_node:{node.name}>", "exec"), ns)
        except Exception as exc:  # noqa: BLE001
            # 原始 traceback 已嵌入异常消息，无需再链式展示
            raise FlowNodeExecutionError(node.id, node.name, exc) from None
        return ns["outputs"]

    def _process_outputs(
        self, node: NodeSpec, raw_outputs: dict[str, Any]
    ) -> dict[str, Any]:
        values: dict[str, Any] = {}
        for outs in node.outputs:
            value = raw_outputs.get(outs.name)
            if value is None:
                values[outs.id] = None
                continue
            values[outs.id] = self._persist_output(node, outs, value)
        return values

    def _persist_output(self, node: NodeSpec, outs: OutputSpec, value: Any) -> Any:
        """按输出配置处理结果：落盘 / 入工程 / 原样传递。"""
        from qgis.core import QgsMapLayer  # type: ignore

        if isinstance(value, QgsMapLayer):
            if outs.save_path:
                return _write_layer_to_file(value, outs.save_path)
            if outs.load_to_canvas:
                _add_layer_to_project(value, node.name)
            return value  # 临时图层（不落盘、不入工程），下游直接拿图层对象
        if isinstance(value, str):
            if outs.load_to_canvas and outs.type in (PortType.VECTOR, PortType.RASTER):
                _add_layer_file_to_project(value, node.name, outs.type)
            return value
        return value

    def _summarize(self, resolved: dict[str, Any], values: dict[str, Any]) -> str:
        in_sum = ", ".join(f"{k}={_fmt_value(v)}" for k, v in resolved.items())
        out_sum = ", ".join(f"{k}={_fmt_value(v)}" for k, v in values.items())
        return f"输入[{in_sum or '—'}] 输出[{out_sum or '—'}]"


# ------------------------------------------------------------ 帮助函数


def _ensure_processing_ready() -> None:
    """确保 QGIS 处理工具箱就绪（headless / 测试环境也能跑 processing.run）。

    真实 QGIS 应用启动时会加载 processing 插件自动完成；嵌入式 / 测试环境里
    插件目录可能不在 ``sys.path``，且算法提供方尚未注册，这里手动补上。
    幂等：native 提供方已注册即直接返回。
    """
    from qgis.core import QgsApplication  # type: ignore

    registry = QgsApplication.processingRegistry()
    if registry.providerById("native") is not None:
        return


    prefix = QgsApplication.instance().prefixPath()
    plugins_dir = str(Path(prefix) / "python" / "plugins")
    if plugins_dir and plugins_dir not in sys.path:
        sys.path.insert(0, plugins_dir)

    from processing.core.Processing import Processing

    import processing  # noqa: F401 —— 导入插件，把 qgis.processing.run 换成真实现

    Processing.initialize()


def _empty_value(ptype: PortType) -> Any:
    if ptype is PortType.NUMBER:
        return 0.0
    if ptype is PortType.BOOLEAN:
        return False
    return ""


def _override_or_default(
    node_id: str,
    ins: InputSpec,
    overrides: dict[str, dict[str, str]] | None,
) -> Any:
    """本次运行覆盖值优先，否则按默认值强转。"""
    override = overrides.get(node_id, {}).get(ins.id) if overrides else None
    if override is not None:
        return _coerce(override, ins.type)
    return _coerce_default(ins)


def _coerce(raw: Any, ptype: PortType) -> Any:
    """把参数原始值（默认值 / 运行时覆盖值）按端口类型强转。"""
    if raw in (None, ""):
        return _empty_value(ptype)
    if ptype is PortType.NUMBER:
        try:
            return float(raw)
        except (ValueError, TypeError):
            return raw
    if ptype is PortType.BOOLEAN:
        return str(raw).strip().lower() in ("1", "true", "是", "yes")
    return str(raw)


def _coerce_default(ins: InputSpec) -> Any:
    """把默认值字符串按端口类型强转。"""
    return _coerce(ins.default, ins.type)


def _resolve_for_type(value: Any, ptype: PortType) -> Any:
    """把上游输出值按目标输入端口类型归一。

    图层类端口：图层对象直接用；路径字符串按需加载成图层对象，
    保证 memory 图层 + 文件持久化混用时下游算法都能拿到图层。
    """
    if ptype in (PortType.VECTOR, PortType.TABLE, PortType.FIELDS):
        from qgis.core import QgsVectorLayer  # type: ignore

        if isinstance(value, str) and value:
            layer = QgsVectorLayer(value, "上游图层", "ogr")
            if layer.isValid():
                return layer
            return value  # 非图层路径（可能是文件名），原样传给代码
        return value
    if ptype is PortType.RASTER:
        from qgis.core import QgsRasterLayer  # type: ignore

        if isinstance(value, str) and value:
            layer = QgsRasterLayer(value, "上游栅格")
            if layer.isValid():
                return layer
            return value
        return value
    return value


def _write_layer_to_file(layer: Any, path: str) -> str:
    """把图层写盘，返回路径字符串。"""
    from qgis.core import QgsRasterFileWriter, QgsRasterLayer, QgsVectorFileWriter, QgsVectorLayer  # type: ignore

    if isinstance(layer, QgsVectorLayer):
        from qgis.core import QgsCoordinateTransformContext  # type: ignore

        options = QgsVectorFileWriter.SaveVectorOptions()
        options.driverName = _vector_driver_for(path)
        # QGIS 4.x：writeAsVectorFormatV3(layer, path, transformContext, options)
        error, errmsg, _newfile, _saved = QgsVectorFileWriter.writeAsVectorFormatV3(
            layer, path, QgsCoordinateTransformContext(), options
        )
        if error != QgsVectorFileWriter.WriterError.NoError:
            raise RuntimeError(f"保存矢量图层失败：{errmsg}")
        return path
    if isinstance(layer, QgsRasterLayer):
        writer = QgsRasterFileWriter(path)
        err = writer.writeRaster(
            layer.pipe(), layer.width(), layer.height(), layer.extent(), layer.crs()
        )
        if err != QgsRasterFileWriter.WriterError.NoError:
            raise RuntimeError(f"保存栅格图层失败：{err}")
        return path
    raise RuntimeError(f"不支持落盘的类型：{type(layer).__name__}")


def _vector_driver_for(path: str) -> str:
    """按扩展名推断 QGIS 矢量驱动名。"""
    ext = str(path).lower().rsplit(".", 1)[-1] if "." in str(path) else ""
    if ext == "shp":
        return "ESRI Shapefile"
    if ext == "geojson":
        return "GeoJSON"
    return "GPKG"


def _add_layer_file_to_project(path: str, name: str, ptype: PortType) -> None:
    """把路径字符串按类型加载进工程「流程输出」分组。"""
    from qgis.core import QgsRasterLayer, QgsVectorLayer  # type: ignore

    layer = (
        QgsVectorLayer(path, name, "ogr") if ptype is PortType.VECTOR else QgsRasterLayer(path, name)
    )
    if layer.isValid():
        _add_layer_to_project(layer, name)


def _add_layer_to_project(layer: Any, name: str) -> None:
    """把图层加入工程「流程输出」分组；同名旧图层先移除再替换。"""
    from qgis.core import QgsLayerTreeLayer, QgsProject  # type: ignore

    project = QgsProject.instance()
    root = project.layerTreeRoot()
    group = root.findGroup(FLOW_OUTPUT_GROUP)
    if group is None:
        group = root.insertGroup(0, FLOW_OUTPUT_GROUP)

    # 替换同名旧输出，避免重复运行在面板里越积越多
    for child in list(group.children()):
        if isinstance(child, QgsLayerTreeLayer) and child.name() == name:
            old = child.layer()
            group.removeChildNode(child)
            if old is not None:
                project.removeMapLayer(old)
            break

    project.addMapLayer(layer, False)  # 先入注册表，不建树节点
    group.insertLayer(0, layer)  # 再插到分组最顶层（新输出盖在最上面）


def _fmt_value(value: Any) -> str:
    from qgis.core import QgsMapLayer  # type: ignore

    if isinstance(value, QgsMapLayer):
        return f"图层({value.name()})"
    if value is None:
        return "无"
    text = str(value)
    return text if len(text) <= 40 else text[:37] + "…"
