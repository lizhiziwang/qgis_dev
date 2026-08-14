"""测试拓扑排序与依赖查询。"""

from __future__ import annotations

import pytest

from app.core.domain.flow.model import EdgeSpec, FlowGraph, PortRef
from app.core.domain.flow.topo import (
    FlowCycleError,
    ancestors,
    dependencies,
    dependents,
    descendants,
    topo_sort,
)
from app.core.domain.flow.types import PortType


def _link(graph: FlowGraph, src: str, dst: str) -> None:
    """从 src 的输出端口连到 dst 的输入端口。"""
    src_out = graph.nodes[src].outputs[0].id
    dst_in = graph.nodes[dst].inputs[0].id
    graph.bind_input(dst, dst_in, PortRef(src, src_out))


def _chain_node(graph: FlowGraph, name: str) -> str:
    node = graph.add_node(name=name)
    graph.add_input(node.id, "in", PortType.STRING)
    graph.add_output(node.id, "out", PortType.STRING)
    return node.id


def _inject_edge(graph: FlowGraph, src: str, dst: str) -> None:
    """绕过 bind_input 直接注入边（模拟导入脏数据产生的环）。"""
    edge = EdgeSpec(
        id=f"edge_{len(graph.edges) + 1}",
        src_node=src,
        src_port=graph.nodes[src].outputs[0].id,
        dst_node=dst,
        dst_port=graph.nodes[dst].inputs[0].id,
    )
    graph.edges[edge.id] = edge


def test_linear_chain_order():
    graph = FlowGraph()
    a = _chain_node(graph, "a")
    b = _chain_node(graph, "b")
    c = _chain_node(graph, "c")
    _link(graph, a, b)
    _link(graph, b, c)
    assert topo_sort(graph) == [a, b, c]


def test_diamond_order():
    graph = FlowGraph()
    a = _chain_node(graph, "a")
    b = _chain_node(graph, "b")
    c = _chain_node(graph, "c")
    d = _chain_node(graph, "d")
    _link(graph, a, b)
    _link(graph, a, c)
    _link(graph, b, d)
    _link(graph, c, d)
    order = topo_sort(graph)
    assert order[0] == a
    assert order[-1] == d
    assert order.index(b) < order.index(d)
    assert order.index(c) < order.index(d)


def test_cycle_raises():
    graph = FlowGraph()
    a = _chain_node(graph, "a")
    b = _chain_node(graph, "b")
    _link(graph, a, b)
    # 注入反向边 b→a 形成环（bind_input 阶段会拒绝，这里模拟脏数据）
    _inject_edge(graph, b, a)
    with pytest.raises(FlowCycleError):
        topo_sort(graph)


def test_cycle_reports_nodes():
    graph = FlowGraph()
    a = _chain_node(graph, "a")
    b = _chain_node(graph, "b")
    c = _chain_node(graph, "c")
    _link(graph, a, b)
    _link(graph, b, c)
    _inject_edge(graph, c, b)  # b→c→b 环
    with pytest.raises(FlowCycleError) as exc_info:
        topo_sort(graph)
    assert {b, c} <= set(exc_info.value.nodes_in_cycle)


def test_dependencies_and_dependents():
    graph = FlowGraph()
    a = _chain_node(graph, "a")
    b = _chain_node(graph, "b")
    c = graph.add_node(name="c")
    graph.add_input(c.id, "in1", PortType.STRING)
    graph.add_input(c.id, "in2", PortType.STRING)
    graph.add_output(c.id, "out", PortType.STRING)
    graph.bind_input(c.id, c.inputs[0].id, PortRef(a, graph.nodes[a].outputs[0].id))
    graph.bind_input(c.id, c.inputs[1].id, PortRef(b, graph.nodes[b].outputs[0].id))
    assert dependencies(graph, c.id) == {a, b}
    assert dependents(graph, a) == {c.id}


def test_ancestors_and_descendants():
    graph = FlowGraph()
    a = _chain_node(graph, "a")
    b = _chain_node(graph, "b")
    c = _chain_node(graph, "c")
    d = _chain_node(graph, "d")
    _link(graph, a, b)
    _link(graph, b, c)
    _link(graph, c, d)
    assert ancestors(graph, d) == {a, b, c}
    assert descendants(graph, a) == {b, c, d}
    assert ancestors(graph, a) == set()
    assert descendants(graph, d) == set()


def test_self_loop_rejected_at_bind_time():
    # bind_input 层面就拒绝自环，拓扑排序拿不到环
    graph = FlowGraph()
    a = _chain_node(graph, "a")
    with pytest.raises(ValueError):
        _link(graph, a, a)
    assert topo_sort(graph) == [a]
