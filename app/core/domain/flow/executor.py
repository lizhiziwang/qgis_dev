"""流程编排：流程执行器。

按拓扑序执行节点代码，把每个节点的输出缓存起来供下游绑定。核心语义：

- **输入解析**：绑定上游的输出端口 → 取上游缓存值并按端口类型归一
  （路径字符串按需加载成图层对象；标量透传）；未绑定 → 用默认值。
- **代码执行**：节点代码在命名空间里 ``exec``，可直接引用 ``inputs`` /
  ``outputs`` / ``skip`` / ``processing`` / ``context`` / ``feedback`` 及常用 QGIS 类。
- **运行时跳过**：代码调用 ``skip(原因)``（抛 :class:`FlowNodeSkip`）时，该节点
  标记为「跳过」而非失败，其所有下游节点级联跳过。
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

#: 流程输出图层上记录来源输出端口（``node_id:port_id``）的自定义属性名。
#: 重复运行同一节点时按它替换旧输出，避免同名节点相互顶掉对方图层。
FLOW_OUTPUT_KEY_PROP = "flow_output_key"

#: 数据源类内置算法：它们的「图层路径」输入语义是路径 / 图层名，不该被
#: 还原成图层对象（模板代码要拿字符串去加载 / 按名回查）。
SOURCE_ALGORITHM_IDS = frozenset({"load_vector", "load_raster"})


class FlowNodeExecutionError(RuntimeError):
    """某个节点执行失败（代码异常 / 输入解析失败 / 输出落盘失败）。"""

    def __init__(self, node_id: str, node_name: str, cause: BaseException) -> None:
        self.node_id = node_id
        self.node_name = node_name
        self.cause = cause
        tb = "".join(traceback.format_exception(type(cause), cause, cause.__traceback__))
        super().__init__(f"节点「{node_name}」执行失败：{tb}")


class FlowNodeSkip(Exception):
    """节点在运行时主动跳过（例如「空图层过滤」发现输入图层没有要素）。

    节点代码通过命名空间里的 :func:`skip` 抛出；执行器把该节点标记为
    「跳过」而不是「失败」，并让它的所有下游节点一并跳过。
    """

    def __init__(self, reason: str = "") -> None:
        self.reason = reason
        super().__init__(reason)


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
    node_skipped = pyqtSignal(str, str, str)  # node_id, node_name, 跳过原因

    def __init__(self, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self._results: dict[str, dict[str, Any]] = {}
        self._status: dict[str, str] = {}
        # 增量运行状态（prepare_run / step_run / finish_run 使用）
        self._graph: FlowGraph | None = None
        self._order: list[str] = []
        self._index = 0
        self._overrides: dict[str, dict[str, str]] = {}
        self._skipped: set[str] = set()  # 运行时跳过的节点（下游据此级联跳过）
        self._running = False

    # ------------------------------------------------------------ 对外接口

    def reset(self) -> None:
        """清空运行缓存与状态。"""
        self._results.clear()
        self._status.clear()
        self._skipped.clear()

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
        """按拓扑序运行整条流程，返回 ``{node_id: 状态}``（同步阻塞）。

        这是给无 GUI / 测试用的便捷入口；带界面的调用改用
        :meth:`prepare_run` / :meth:`step_run` / :meth:`finish_run` 增量驱动，
        每执行完一个节点就把控制权还给 Qt 事件循环，节点状态才能实时刷新。

        :param overrides: 本次运行的输入覆盖值 ``{node_id: {input_id: 原始字符串}}``，
            未绑定（或绑定到禁用/跳过上游）的输入优先取覆盖值，否则用默认值。
            只影响本次运行，不写回图模型 / JSON。

        :raises FlowCycleError: 图有环。
        :raises FlowNodeExecutionError: 某节点执行失败（流程中止）。
        """
        self.prepare_run(graph, overrides)
        try:
            while not self.step_run():
                pass
        except Exception:  # 失败时仍要收尾（发 flow_finished）
            self.finish_run()
            raise
        return self.finish_run()

    def prepare_run(
        self,
        graph: FlowGraph,
        overrides: dict[str, dict[str, str]] | None = None,
    ) -> None:
        """初始化一次增量运行：算拓扑序、清缓存、发 ``flow_started``。

        :raises FlowCycleError: 图有环（同步抛出，调用方可在启动前拦截）。
        """
        order = topo_sort(graph)
        self._graph = graph
        self._order = list(order)
        self._index = 0
        self._overrides = dict(overrides) if overrides else {}
        self._results.clear()
        self._skipped = set()
        self._status = dict.fromkeys(graph.nodes, STATUS_IDLE)
        self._running = True
        planned = sum(1 for nid in order if graph.nodes[nid].enabled and not graph.nodes[nid].skip)
        self.flow_started.emit(planned)

    def step_run(self) -> bool:
        """执行下一个待运行节点；整条流程已全部处理完返回 ``True``。

        一次调用最多真正执行 **一个** 节点（禁用 / 跳过的节点顺带标记、不算），
        返回后 Qt 事件循环得以处理重绘，节点状态可实时变绿。

        :raises FlowNodeExecutionError: 当前节点执行失败（状态置 failed）。
        """
        if not self._running or self._graph is None:
            return True
        while self._index < len(self._order):
            nid = self._order[self._index]
            self._index += 1
            node = self._graph.nodes[nid]
            if not node.enabled:
                self._status[nid] = STATUS_DISABLED
                continue
            if node.skip:
                self._status[nid] = STATUS_SKIPPED
                continue
            blocked = self._blocked_by_skip(self._graph, node)
            if blocked is not None:
                self._skip_node(self._graph, node, f"上游节点「{blocked}」已跳过")
                continue
            try:
                self._run_node(self._graph, node, self._overrides)
                self._status[nid] = STATUS_SUCCESS
            except FlowNodeSkip as exc:
                self._skip_node(self._graph, node, exc.reason or "满足跳过条件")
                continue
            except Exception:  # noqa: BLE001
                self._status[nid] = STATUS_FAILED
                raise
            return self._index >= len(self._order)
        return True

    def _skip_node(self, graph: FlowGraph, node: NodeSpec, reason: str) -> None:
        """标记节点运行时跳过，并记录以便下游级联跳过。"""
        self._skipped.add(node.id)
        self._status[node.id] = STATUS_SKIPPED
        self.node_skipped.emit(node.id, node.name, reason)

    def _blocked_by_skip(self, graph: FlowGraph, node: NodeSpec) -> str | None:
        """任一上游在本轮运行中被跳过时返回其名称，否则 None。"""
        for ins in node.inputs:
            binding = graph.input_binding(node.id, ins.id)
            if binding is not None and binding.node_id in self._skipped:
                src = graph.nodes.get(binding.node_id)
                return src.name if src is not None else binding.node_id
        return None

    def finish_run(self) -> dict[str, str]:
        """收尾一次运行：发 ``flow_finished``，返回 ``{node_id: 状态}``。"""
        self._running = False
        self._graph = None
        self.flow_finished.emit(dict(self._status))
        return dict(self._status)

    @property
    def is_running(self) -> bool:
        """是否正处于增量运行中（UI 用它禁掉重复触发）。"""
        return self._running

    def run_node(self, graph: FlowGraph, node_id: str) -> dict[str, Any]:
        """单节点分步执行：上游用缓存结果，缺缓存则退回默认值并记录警告。

        :raises FlowNodeExecutionError: 执行失败。
        :raises FlowNodeSkip: 节点代码主动跳过（调用方自行处理状态）。
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
        except FlowNodeSkip:
            # 主动跳过不是失败：不包装、不发 node_finished，交给 step_run 记状态
            raise
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
        as_source = node.algorithm_id in SOURCE_ALGORITHM_IDS
        for ins in node.inputs:
            binding = graph.input_binding(node.id, ins.id)
            if binding is not None:
                value = self._results.get(binding.node_id, {}).get(binding.port_id)
                if value is None:
                    # 上游没执行 / 输出无缓存 → 本次覆盖值优先，否则退回默认值
                    resolved[ins.name] = _override_or_default(node.id, ins, overrides, as_source)
                else:
                    resolved[ins.name] = _resolve_for_type(value, ins.type, as_source)
            else:
                resolved[ins.name] = _override_or_default(node.id, ins, overrides, as_source)
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

        def skip(reason: str = "") -> None:
            """供节点代码调用：主动跳过本节点及其全部下游节点。"""
            raise FlowNodeSkip(str(reason))

        ns: dict[str, Any] = {
            "__builtins__": builtins,
            "inputs": resolved,
            "outputs": {},
            "skip": skip,
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
        except FlowNodeSkip:
            raise
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
                _add_layer_to_project(value, node.name, f"{node.id}:{outs.id}")
            return value  # 临时图层（不落盘、不入工程），下游直接拿图层对象
        if isinstance(value, str):
            if outs.load_to_canvas and outs.type in (PortType.VECTOR, PortType.RASTER):
                _add_layer_file_to_project(value, node.name, outs.type, f"{node.id}:{outs.id}")
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
    as_source: bool = False,
) -> Any:
    """本次运行覆盖值优先，否则按默认值强转。

    ``as_source``：消费节点是数据源加载器，图层类输入不做对象还原。
    """
    override = overrides.get(node_id, {}).get(ins.id) if overrides else None
    if override is not None:
        return _coerce(override, ins.type, as_source)
    return _coerce_default(ins, as_source)


def _coerce(raw: Any, ptype: PortType, as_source: bool = False) -> Any:
    """把参数原始值（默认值 / 运行时覆盖值）按端口类型强转。

    图层类输入（``VECTOR`` / ``RASTER``）额外做一次工程图层回查：运行参数
    下拉里填的是**纯图层名**，这里还原成图层对象，节点的 processing /
    PyQGIS 代码才能直接使用（内存图层也能拿到）。查不到时原样保留字符串，
    让 ``*.shp`` / ``*.tif`` 这类文件路径走各自的加载分支。

    ``as_source=True``（数据源加载器）时不回查，图层名原样传给模板。
    """
    if raw in (None, ""):
        return _empty_value(ptype)
    if ptype is PortType.NUMBER:
        try:
            return float(raw)
        except (ValueError, TypeError):
            return raw
    if ptype is PortType.BOOLEAN:
        return str(raw).strip().lower() in ("1", "true", "是", "yes")
    text = str(raw)
    if as_source and ptype in (PortType.VECTOR, PortType.RASTER):
        from qgis.core import QgsMapLayer  # type: ignore

        return raw.name() if isinstance(raw, QgsMapLayer) else text
    if ptype in (PortType.VECTOR, PortType.RASTER):
        layer = _project_layer_by_name(text, ptype)
        if layer is not None:
            return layer
    elif ptype in (PortType.FILE, PortType.STRING, PortType.FOLDER):
        # 已经是图层对象却落到路径 / 字符串端口 → 退成图层名，
        # 让数据源模板按名回查，避免把图层对象当成文件路径
        from qgis.core import QgsMapLayer  # type: ignore

        if isinstance(raw, QgsMapLayer):
            return raw.name()
    return text


def _project_layer_by_name(name: str, ptype: PortType) -> Any:
    """按图层名在工程里回查图层；找不到 / 类型不符返回 None。"""
    if not name:
        return None
    from qgis.core import QgsProject, QgsRasterLayer, QgsVectorLayer  # type: ignore

    want = QgsVectorLayer if ptype is PortType.VECTOR else QgsRasterLayer
    for layer in QgsProject.instance().mapLayersByName(name):
        if isinstance(layer, want):
            return layer
    return None


def _coerce_default(ins: InputSpec, as_source: bool = False) -> Any:
    """把默认值字符串按端口类型强转。"""
    return _coerce(ins.default, ins.type, as_source)


def _resolve_for_type(value: Any, ptype: PortType, as_source: bool = False) -> Any:
    """把上游输出值按目标输入端口类型归一。

    图层类端口：图层对象直接用；路径字符串按需加载成图层对象，
    保证 memory 图层 + 文件持久化混用时下游算法都能拿到图层。
    ``as_source=True`` 时反向操作：图层对象退成图层名交给加载器模板。
    """
    if as_source and ptype in (PortType.VECTOR, PortType.RASTER):
        from qgis.core import QgsMapLayer  # type: ignore

        return value.name() if isinstance(value, QgsMapLayer) else value
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
    if ptype in (PortType.FILE, PortType.STRING, PortType.FOLDER):
        # 路径 / 字符串端口收到图层对象（如上游输出写错类型）→ 退成图层名
        from qgis.core import QgsMapLayer  # type: ignore

        if isinstance(value, QgsMapLayer):
            return value.name()
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


def _add_layer_file_to_project(path: str, name: str, ptype: PortType, key: str) -> None:
    """把路径字符串按类型加载进工程「流程输出」分组。"""
    from qgis.core import QgsRasterLayer, QgsVectorLayer  # type: ignore

    layer = (
        QgsVectorLayer(path, name, "ogr") if ptype is PortType.VECTOR else QgsRasterLayer(path, name)
    )
    if layer.isValid():
        _add_layer_to_project(layer, name, key)


def _add_layer_to_project(layer: Any, name: str, key: str) -> None:
    """把图层加入工程「流程输出」分组；同一输出端口的旧图层先移除再替换。

    入工程前把图层名统一改成节点名。processing 出来的结果图层都叫同一个名字
    （如「属性过滤结果」），不改名则面板里一堆同名条目分不清。

    替换按**输出端口身份** ``key``（``node_id:port_id``）判定（存在图层的
    :data:`FLOW_OUTPUT_KEY_PROP` 自定义属性里），而不是按显示名：两个同名节点
    （如两条「加载矢量图层」）的输出互不干扰，不会被对方当成旧输出删掉——
    否则下游缓存的图层对象会连带失效。

    图层本来就在工程里（数据源节点从图层树选图层、直接复用那个对象）时立即
    返回：它在面板里已经可见，再入一次只会多出一个树节点。
    """
    from qgis.core import QgsLayerTreeLayer, QgsProject  # type: ignore

    project = QgsProject.instance()
    if project.mapLayer(layer.id()) is not None:
        return

    layer.setName(name)
    layer.setCustomProperty(FLOW_OUTPUT_KEY_PROP, key)
    root = project.layerTreeRoot()
    group = root.findGroup(FLOW_OUTPUT_GROUP)
    if group is None:
        group = root.insertGroup(0, FLOW_OUTPUT_GROUP)

    # 只替换同一输出端口上次运行遗留的旧图层，避免重复运行在面板里越积越多
    for child in list(group.children()):
        if not isinstance(child, QgsLayerTreeLayer):
            continue
        old = child.layer()
        if old is not None and old.customProperty(FLOW_OUTPUT_KEY_PROP) == key:
            group.removeChildNode(child)
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
