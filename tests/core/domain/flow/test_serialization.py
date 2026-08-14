"""测试流程配置 JSON 序列化。"""

from __future__ import annotations

from app.core.domain.flow.model import FlowGraph, PortRef
from app.core.domain.flow.serialization import FlowCodec
from app.core.domain.flow.types import PortType


def _build_sample_graph() -> FlowGraph:
    graph = FlowGraph()
    a = graph.add_node(name="加载图层", algorithm_id="load_vector")
    a.x, a.y = 10.0, 20.0
    graph.add_input(a.id, "路径", PortType.FILE, default="D:/data.shp")
    graph.add_output(a.id, "图层", PortType.VECTOR)

    b = graph.add_node(name="缓冲区", algorithm_id="native:buffer")
    graph.add_input(b.id, "输入", PortType.VECTOR)
    graph.add_input(b.id, "距离", PortType.NUMBER, default="100")
    out = graph.add_output(b.id, "结果", PortType.VECTOR, )
    out.save_path = "D:/out.gpkg"
    out.load_to_canvas = True

    graph.bind_input(
        b.id, b.inputs[0].id, PortRef(a.id, a.outputs[0].id)
    )
    return graph


def test_roundtrip_preserves_graph():
    graph = _build_sample_graph()
    restored, warnings = FlowCodec.from_dict(FlowCodec.to_dict(graph))

    assert warnings == []
    assert set(restored.nodes) == set(graph.nodes)
    assert set(restored.edges) == set(graph.edges)

    a = restored.nodes["node1"]
    assert a.name == "加载图层"
    assert a.algorithm_id == "load_vector"
    assert (a.x, a.y) == (10.0, 20.0)
    assert a.inputs[0].type is PortType.FILE
    assert a.inputs[0].default == "D:/data.shp"

    b = restored.nodes["node2"]
    assert b.outputs[0].type is PortType.VECTOR
    assert b.outputs[0].save_path == "D:/out.gpkg"
    assert b.outputs[0].load_to_canvas is True

    # 绑定关系（边）还原
    in_b = restored.nodes["node2"].inputs[0].id
    assert restored.input_binding("node2", in_b) == PortRef("node1", restored.nodes["node1"].outputs[0].id)


def test_to_json_from_json_roundtrip():
    graph = _build_sample_graph()
    text = FlowCodec.to_json(graph)
    restored, warnings = FlowCodec.from_json(text)
    assert warnings == []
    assert len(restored.nodes) == 2
    assert len(restored.edges) == 1


def test_from_dict_drops_edge_to_unknown_node():
    data = FlowCodec.to_dict(_build_sample_graph())
    data["edges"].append(
        {"id": "edge_9", "src_node": "ghost", "src_port": "out1", "dst_node": "node2", "dst_port": "in1"}
    )
    graph, warnings = FlowCodec.from_dict(data)
    assert len(graph.edges) == 1
    assert any("不存在的节点" in w for w in warnings)


def test_from_dict_drops_incompatible_edge():
    data = FlowCodec.to_dict(_build_sample_graph())
    # 改一条边指向类型不兼容的端口：node1.out(矢量) → node2.in(数值)
    data["edges"].append(
        {"id": "edge_9", "src_node": "node1", "src_port": "out1", "dst_node": "node2", "dst_port": "in2"}
    )
    graph, warnings = FlowCodec.from_dict(data)
    assert len(graph.edges) == 1
    assert any("类型不兼容" in w for w in warnings)


def test_from_dict_drops_duplicate_input_edge():
    data = FlowCodec.to_dict(_build_sample_graph())
    # 给同一输入端口再来一条边
    extra = {"id": "edge_9", "src_node": "node1", "src_port": "out1", "dst_node": "node2", "dst_port": "in1"}
    data["edges"].append(extra)
    graph, warnings = FlowCodec.from_dict(data)
    assert len(graph.edges) == 1
    assert any("多条数据源" in w for w in warnings)


def test_from_dict_tolerates_unknown_type_as_string():
    data = FlowCodec.to_dict(_build_sample_graph())
    data["nodes"][1]["inputs"][1]["type"] = "NUMBER"
    graph, _ = FlowCodec.from_dict(data)
    assert graph.nodes["node2"].inputs[1].type is PortType.NUMBER


def test_empty_graph_roundtrip():
    graph = FlowGraph()
    restored, warnings = FlowCodec.from_dict(FlowCodec.to_dict(graph))
    assert warnings == []
    assert restored.nodes == {}
    assert restored.edges == {}
