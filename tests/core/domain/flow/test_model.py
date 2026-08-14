"""测试流程图数据模型：建节点 / 删节点 / 绑定 / 防环。"""

from __future__ import annotations

import pytest

from app.core.domain.flow.model import FlowGraph, PortRef
from app.core.domain.flow.types import PortType


def _chain(graph: FlowGraph, name: str, ptype: PortType) -> str:
    """建一个「单输入单输出」节点，返回节点 id。"""
    node = graph.add_node(name=name)
    graph.add_input(node.id, "in", ptype)
    graph.add_output(node.id, "out", ptype)
    return node.id


def test_add_node_generates_unique_ids():
    graph = FlowGraph()
    a = graph.add_node()
    b = graph.add_node()
    assert a.id != b.id
    assert set(graph.nodes) == {a.id, b.id}


def test_add_input_output_unique_port_ids():
    graph = FlowGraph()
    node = graph.add_node()
    p1 = graph.add_input(node.id, "a", PortType.STRING)
    p2 = graph.add_input(node.id, "b", PortType.STRING)
    assert p1.id != p2.id
    o1 = graph.add_output(node.id, "x", PortType.STRING)
    o2 = graph.add_output(node.id, "y", PortType.STRING)
    assert o1.id != o2.id


def test_remove_node_removes_touching_edges():
    graph = FlowGraph()
    a = _chain(graph, "a", PortType.STRING)
    b = _chain(graph, "b", PortType.STRING)
    graph.bind_input(b, graph.nodes[b].inputs[0].id, PortRef(a, graph.nodes[a].outputs[0].id))
    assert len(graph.edges) == 1
    graph.remove_node(a)
    assert len(graph.edges) == 0
    assert b in graph.nodes


def test_bind_input_type_mismatch_raises():
    graph = FlowGraph()
    a = _chain(graph, "a", PortType.STRING)
    b = _chain(graph, "b", PortType.NUMBER)
    with pytest.raises(ValueError, match="类型不兼容"):
        graph.bind_input(b, graph.nodes[b].inputs[0].id, PortRef(a, graph.nodes[a].outputs[0].id))


def test_bind_input_cycle_prevented():
    graph = FlowGraph()
    a = _chain(graph, "a", PortType.STRING)
    b = _chain(graph, "b", PortType.STRING)
    graph.bind_input(b, graph.nodes[b].inputs[0].id, PortRef(a, graph.nodes[a].outputs[0].id))
    # b 的输出 → a 的输入会成环
    with pytest.raises(ValueError, match="循环依赖"):
        graph.bind_input(a, graph.nodes[a].inputs[0].id, PortRef(b, graph.nodes[b].outputs[0].id))


def test_bind_input_self_reference_rejected():
    graph = FlowGraph()
    a = _chain(graph, "a", PortType.STRING)
    with pytest.raises(ValueError, match="循环依赖"):
        graph.bind_input(a, graph.nodes[a].inputs[0].id, PortRef(a, graph.nodes[a].outputs[0].id))


def test_bind_input_replaces_previous_edge():
    graph = FlowGraph()
    a = _chain(graph, "a", PortType.STRING)
    b = _chain(graph, "b", PortType.STRING)
    c = _chain(graph, "c", PortType.STRING)
    in_b = graph.nodes[b].inputs[0].id
    graph.bind_input(b, in_b, PortRef(a, graph.nodes[a].outputs[0].id))
    graph.bind_input(b, in_b, PortRef(c, graph.nodes[c].outputs[0].id))
    # 一个输入端口只允许一个数据源
    assert len(graph.edges) == 1
    assert graph.input_binding(b, in_b) == PortRef(c, graph.nodes[c].outputs[0].id)


def test_input_binding_derived_from_edge():
    graph = FlowGraph()
    a = _chain(graph, "a", PortType.STRING)
    b = _chain(graph, "b", PortType.STRING)
    in_b = graph.nodes[b].inputs[0].id
    out_a = graph.nodes[a].outputs[0].id
    assert graph.input_binding(b, in_b) is None
    graph.bind_input(b, in_b, PortRef(a, out_a))
    assert graph.input_binding(b, in_b) == PortRef(a, out_a)


def test_unbind_input():
    graph = FlowGraph()
    a = _chain(graph, "a", PortType.STRING)
    b = _chain(graph, "b", PortType.STRING)
    in_b = graph.nodes[b].inputs[0].id
    graph.bind_input(b, in_b, PortRef(a, graph.nodes[a].outputs[0].id))
    graph.unbind_input(b, in_b)
    assert len(graph.edges) == 0
    assert graph.input_binding(b, in_b) is None


def test_remove_input_clears_feeding_edge():
    graph = FlowGraph()
    a = _chain(graph, "a", PortType.STRING)
    b = _chain(graph, "b", PortType.STRING)
    in_b = graph.nodes[b].inputs[0].id
    graph.bind_input(b, in_b, PortRef(a, graph.nodes[a].outputs[0].id))
    graph.remove_input(b, in_b)
    assert len(graph.edges) == 0


def test_remove_output_clears_source_edges():
    graph = FlowGraph()
    a = _chain(graph, "a", PortType.STRING)
    b = _chain(graph, "b", PortType.STRING)
    out_a = graph.nodes[a].outputs[0].id
    graph.bind_input(b, graph.nodes[b].inputs[0].id, PortRef(a, out_a))
    graph.remove_output(a, out_a)
    assert len(graph.edges) == 0


def test_reset_defaults_clears_params_and_bindings():
    graph = FlowGraph()
    a = _chain(graph, "a", PortType.STRING)
    b = _chain(graph, "b", PortType.STRING)
    a_input = graph.nodes[a].inputs[0]
    a_input.default = "some/path.shp"
    b_output = graph.nodes[b].outputs[0]
    b_output.save_path = "D:/out.gpkg"
    b_output.load_to_canvas = True
    graph.bind_input(b, graph.nodes[b].inputs[0].id, PortRef(a, graph.nodes[a].outputs[0].id))

    graph.reset_defaults()

    assert a_input.default == ""
    assert b_output.save_path == ""
    assert b_output.load_to_canvas is False
    assert len(graph.edges) == 0


def test_upstream_outputs_excludes_self():
    graph = FlowGraph()
    a = _chain(graph, "a", PortType.STRING)
    b = _chain(graph, "b", PortType.STRING)
    refs = {nid for nid, _ in graph.upstream_outputs(b)}
    assert refs == {a}
