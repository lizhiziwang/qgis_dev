"""测试运行前参数收集逻辑 :func:`unbound_inputs`。"""

from __future__ import annotations

from app.core.domain.flow.model import FlowGraph, PortRef, unbound_inputs
from app.core.domain.flow.types import PortType


def _chain(graph: FlowGraph, name: str, ptype: PortType) -> str:
    """建一个「单输入单输出」节点，返回节点 id。"""
    node = graph.add_node(name=name)
    graph.add_input(node.id, "in", ptype)
    graph.add_output(node.id, "out", ptype)
    return node.id


def test_unbound_inputs_lists_only_truly_unbound():
    graph = FlowGraph()
    a = _chain(graph, "a", PortType.STRING)
    b = _chain(graph, "b", PortType.STRING)
    graph.bind_input(b, graph.nodes[b].inputs[0].id, PortRef(a, graph.nodes[a].outputs[0].id))

    entries = unbound_inputs(graph)

    # a 的输入未绑定 → 需要填写；b 的输入已绑定到启用上游 → 不需要
    assert {(nid, ins.name) for nid, _, ins in entries} == {(a, "in")}


def test_unbound_inputs_skips_disabled_and_skipped_nodes():
    graph = FlowGraph()
    a = _chain(graph, "a", PortType.STRING)
    b = _chain(graph, "b", PortType.STRING)
    graph.nodes[a].enabled = False
    graph.nodes[b].skip = True

    # 禁用 / 跳过的节点不运行，其输入不需要填
    assert unbound_inputs(graph) == []


def test_unbound_inputs_includes_bound_to_disabled_upstream():
    graph = FlowGraph()
    a = _chain(graph, "a", PortType.STRING)
    b = _chain(graph, "b", PortType.STRING)
    graph.bind_input(b, graph.nodes[b].inputs[0].id, PortRef(a, graph.nodes[a].outputs[0].id))
    graph.nodes[a].enabled = False

    # b 的输入虽连了线，但上游 a 被禁用 → 运行时取不到缓存、退回默认值，仍需用户填写
    entries = unbound_inputs(graph)
    assert {(nid, ins.name) for nid, _, ins in entries} == {(b, "in")}


def test_unbound_inputs_multiple_inputs_per_node():
    graph = FlowGraph()
    node = graph.add_node(name="n")
    graph.add_input(node.id, "a", PortType.FILE)
    graph.add_input(node.id, "b", PortType.NUMBER)

    entries = unbound_inputs(graph)

    assert len(entries) == 2
    assert {(nid, ins.name) for nid, _, ins in entries} == {(node.id, "a"), (node.id, "b")}
