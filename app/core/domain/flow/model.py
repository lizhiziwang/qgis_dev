"""流程编排：流程图数据模型。

纯逻辑模块，不依赖 QGIS / Qt。节点输入与上游输出的绑定关系**以边为准**——
:class:`FlowGraph.bind_input` 负责建边（自动拆掉该输入原有的边），
:meth:`FlowGraph.input_binding` 从边推导绑定，避免「边」与「绑定字段」双份
存储互相漂移。连线合法性（类型兼容 + 不成环）在建边时校验。
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass, field

from .types import PortType, can_connect


@dataclass(frozen=True)
class PortRef:
    """对某个节点输出端口的引用（上游绑定目标）。"""

    node_id: str
    port_id: str


@dataclass
class InputSpec:
    """节点输入参数定义。绑定关系见 :meth:`FlowGraph.input_binding`。"""

    id: str
    name: str
    type: PortType
    default: str = ""
    required: bool = False
    #: 可选项（如「距离单位」的 米 / 千米 / 度）；非空时运行参数用下拉框。
    choices: tuple[str, ...] = ()
    #: 类型是否由算法执行逻辑固定（True 时参数面板禁用「类型」下拉）。
    type_locked: bool = False


@dataclass
class OutputSpec:
    """节点输出端口定义。"""

    id: str
    name: str
    type: PortType
    #: 输出保存路径（如 ``D:/结果.gpkg``）。为空时不落盘。
    save_path: str = ""
    #: 是否写入临时图层（memory）——不落盘、不入工程，仅内存传递。
    write_temp: bool = True
    #: 是否把结果图层加载到 QGIS 图层面板（工程「流程输出」分组）。
    load_to_canvas: bool = False
    #: 类型是否由算法执行逻辑固定（True 时参数面板禁用「类型」下拉）。
    type_locked: bool = False


@dataclass
class NodeSpec:
    """单个流程节点。"""

    id: str
    name: str = "节点"
    x: float = 0.0
    y: float = 0.0
    #: 禁用节点：置灰、不参与运算（在拓扑序中仍占位，下游走默认值）。
    enabled: bool = True
    #: 跳过节点：整流程运行时跳过不执行。
    skip: bool = False
    inputs: list[InputSpec] = field(default_factory=list)
    outputs: list[OutputSpec] = field(default_factory=list)
    #: 执行代码，可引用 ``inputs`` / ``outputs`` / ``processing`` / ``context`` / ``feedback``。
    code: str = ""
    #: 关联的处理算法 id（如 ``native:buffer``），仅作记录与模板重建。
    algorithm_id: str = ""


@dataclass
class EdgeSpec:
    """连接上游输出端口 → 下游输入端口的边。"""

    id: str
    src_node: str
    src_port: str
    dst_node: str
    dst_port: str


def _fresh_id(prefix: str, pool: Iterable[str]) -> str:
    """生成 pool 中不存在的 ``prefixN``（N 从 1 递增）。"""
    pool_set = set(pool)
    n = 1
    while f"{prefix}{n}" in pool_set:
        n += 1
    return f"{prefix}{n}"


class FlowGraph:
    """流程图：节点 + 边的容器，提供建边校验与绑定维护。"""

    def __init__(self) -> None:
        self.nodes: dict[str, NodeSpec] = {}
        self.edges: dict[str, EdgeSpec] = {}
        self._node_seq = 1
        self._port_seq = 1
        self._edge_seq = 1

    # ------------------------------------------------------------ 节点

    def add_node(
        self,
        name: str = "节点",
        x: float = 0.0,
        y: float = 0.0,
        inputs: list[InputSpec] | None = None,
        outputs: list[OutputSpec] | None = None,
        code: str = "",
        algorithm_id: str = "",
    ) -> NodeSpec:
        """新增节点；id / 端口 id 自动生成。"""
        node_id = _fresh_id("node", self.nodes)
        node = NodeSpec(
            id=node_id,
            name=name,
            x=x,
            y=y,
            inputs=inputs or [],
            outputs=outputs or [],
            code=code,
            algorithm_id=algorithm_id,
        )
        self.nodes[node_id] = node
        return node

    def remove_node(self, node_id: str) -> list[str]:
        """删除节点及其关联边。返回被删除的边 id 列表。"""
        if node_id not in self.nodes:
            raise KeyError(node_id)
        removed_edges = [
            e.id for e in self.edges.values() if e.src_node == node_id or e.dst_node == node_id
        ]
        for eid in removed_edges:
            del self.edges[eid]
        del self.nodes[node_id]
        return removed_edges

    def add_input(
        self,
        node_id: str,
        name: str,
        type_: PortType,
        default: str = "",
        choices: tuple[str, ...] = (),
        type_locked: bool = False,
    ) -> InputSpec:
        node = self._node(node_id)
        port_id = _fresh_id("in", [i.id for i in node.inputs])
        spec = InputSpec(
            id=port_id,
            name=name,
            type=type_,
            default=default,
            choices=tuple(choices),
            type_locked=type_locked,
        )
        node.inputs.append(spec)
        return spec

    def remove_input(self, node_id: str, input_id: str) -> None:
        """删除输入端口，并清掉连到该端口的边。"""
        node = self._node(node_id)
        for eid in [e.id for e in self.edges.values() if e.dst_node == node_id and e.dst_port == input_id]:
            del self.edges[eid]
        node.inputs = [i for i in node.inputs if i.id != input_id]

    def add_output(
        self,
        node_id: str,
        name: str,
        type_: PortType,
        type_locked: bool = False,
    ) -> OutputSpec:
        node = self._node(node_id)
        port_id = _fresh_id("out", [o.id for o in node.outputs])
        spec = OutputSpec(
            id=port_id, name=name, type=type_, type_locked=type_locked
        )
        node.outputs.append(spec)
        return spec

    def remove_output(self, node_id: str, output_id: str) -> None:
        """删除输出端口，并清掉以该端口为源的边。"""
        node = self._node(node_id)
        for eid in [e.id for e in self.edges.values() if e.src_node == node_id and e.src_port == output_id]:
            del self.edges[eid]
        node.outputs = [o for o in node.outputs if o.id != output_id]

    # ------------------------------------------------------------ 边与绑定

    def input_port(self, node_id: str, port_id: str) -> InputSpec | None:
        node = self.nodes.get(node_id)
        if node is None:
            return None
        for ins in node.inputs:
            if ins.id == port_id:
                return ins
        return None

    def output_port(self, node_id: str, port_id: str) -> OutputSpec | None:
        node = self.nodes.get(node_id)
        if node is None:
            return None
        for outs in node.outputs:
            if outs.id == port_id:
                return outs
        return None

    def input_binding(self, dst_node: str, dst_port: str) -> PortRef | None:
        """推导某个输入端口的绑定（上游输出端口引用）；未绑定返回 None。"""
        for e in self.edges.values():
            if e.dst_node == dst_node and e.dst_port == dst_port:
                return PortRef(node_id=e.src_node, port_id=e.src_port)
        return None

    def edge_into(self, dst_node: str, dst_port: str) -> EdgeSpec | None:
        """返回连接到指定输入端口的边（同一输入最多一条）。"""
        for e in self.edges.values():
            if e.dst_node == dst_node and e.dst_port == dst_port:
                return e
        return None

    def bind_input(self, dst_node: str, dst_port: str, src: PortRef) -> EdgeSpec:
        """把输入端口绑定到上游输出端口：拆旧边 → 校验 → 建边。

        :raises ValueError: 端口不存在 / 类型不兼容 / 会形成环。
        """
        if src.node_id not in self.nodes or dst_node not in self.nodes:
            raise ValueError("上游或下游节点不存在")
        src_port = self.output_port(src.node_id, src.port_id)
        dst_port_spec = self.input_port(dst_node, dst_port)
        if src_port is None:
            raise ValueError(f"上游节点无输出端口：{src.port_id}")
        if dst_port_spec is None:
            raise ValueError(f"下游节点无输入端口：{dst_port}")
        if not can_connect(src_port.type, dst_port_spec.type):
            raise ValueError(
                f"类型不兼容：{src_port.type.value} 不能连到 {dst_port_spec.type.value}"
            )
        if src.node_id == dst_node or self._can_reach(dst_node, src.node_id):
            raise ValueError("连线会形成循环依赖")

        # 拆掉该输入原有的边（一个输入端口只允许一个数据源）
        old = self.edge_into(dst_node, dst_port)
        if old is not None:
            del self.edges[old.id]

        edge = EdgeSpec(
            id=_fresh_id("edge", self.edges),
            src_node=src.node_id,
            src_port=src.port_id,
            dst_node=dst_node,
            dst_port=dst_port,
        )
        self.edges[edge.id] = edge
        return edge

    def unbind_input(self, dst_node: str, dst_port: str) -> EdgeSpec | None:
        """断开输入端口的绑定（删除连入该端口的边）。"""
        old = self.edge_into(dst_node, dst_port)
        if old is not None:
            del self.edges[old.id]
        return old

    def remove_edge(self, edge_id: str) -> None:
        if edge_id in self.edges:
            del self.edges[edge_id]

    def edges_from(self, node_id: str) -> list[EdgeSpec]:
        return [e for e in self.edges.values() if e.src_node == node_id]

    def edges_to(self, node_id: str) -> list[EdgeSpec]:
        return [e for e in self.edges.values() if e.dst_node == node_id]

    def upstream_outputs(self, node_id: str) -> list[tuple[str, OutputSpec]]:
        """该节点可绑定的所有上游候选输出（其它节点的输出端口）。

        供输入面板「绑定上游」下拉使用；具体某条连线是否合法仍由
        :meth:`bind_input` 校验。
        """
        return [
            (nid, outs)
            for nid, node in self.nodes.items()
            if nid != node_id
            for outs in node.outputs
        ]

    def reset_defaults(self) -> None:
        """重置所有参数为初始值：清空输入默认值、输出保存路径，并断开全部绑定。"""
        for node in self.nodes.values():
            for ins in node.inputs:
                ins.default = ""
            for outs in node.outputs:
                outs.save_path = ""
                outs.write_temp = True
                outs.load_to_canvas = False
        self.edges.clear()

    # ------------------------------------------------------------ 内部

    def _node(self, node_id: str) -> NodeSpec:
        if node_id not in self.nodes:
            raise KeyError(node_id)
        return self.nodes[node_id]

    def _can_reach(self, start: str, target: str) -> bool:
        """沿现有边能否从 start 走到 target（用于防环）。"""
        seen: set[str] = set()
        stack = [start]
        while stack:
            cur = stack.pop()
            if cur == target:
                return True
            if cur in seen:
                continue
            seen.add(cur)
            stack.extend(e.dst_node for e in self.edges_from(cur))
        return False


def unbound_inputs(graph: FlowGraph) -> list[tuple[str, str, InputSpec]]:
    """运行前需要填值的输入：可运行节点里未绑定上游的输入。

    绑定到**启用**上游的输入由流程提供值，不需要手动填；未绑定、或虽绑定但
    上游被禁用/跳过（运行时取不到缓存、会退回默认值）的输入都返回，供
    「运行前参数填写」对话框与执行器覆盖逻辑共用。

    :return: ``[(node_id, node_name, InputSpec), ...]``
    """
    out: list[tuple[str, str, InputSpec]] = []
    for nid, node in graph.nodes.items():
        if not node.enabled or node.skip:
            continue
        for ins in node.inputs:
            edge = graph.edge_into(nid, ins.id)
            if edge is None:
                out.append((nid, node.name, ins))
                continue
            src = graph.nodes.get(edge.src_node)
            if src is None or not src.enabled or src.skip:
                out.append((nid, node.name, ins))
    return out
