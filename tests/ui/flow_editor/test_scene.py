"""流程画布场景测试（gui 标记，需 QGIS + Qt 环境）。

覆盖：模板建节点、端口拖拽连线（合法 / 类型不兼容 / 成环 / 反向拖）、
依赖链高亮、上一级 / 下一级跳转、删除清理。
"""

from __future__ import annotations

import pytest
from PyQt6.QtWidgets import QApplication

from app.core.domain.flow.types import PortType
from app.ui.flow_editor import FlowScene

pytestmark = pytest.mark.gui


@pytest.fixture(autouse=True)
def _qapp(qgis_app):
    """确保 QApplication 存在（qgis_app 已初始化 QgsApplication）。"""
    return QApplication.instance() or QApplication([])


def _connect(scene: FlowScene, src_nid: str, dst_nid: str) -> None:
    """模拟拖线：从 src 的输出端口拖到 dst 的输入端口。"""
    src_item = scene.node_item(src_nid)
    dst_item = scene.node_item(dst_nid)
    src_out = src_item.port_items[src_item.node.outputs[0].id]
    dst_in = dst_item.port_items[dst_item.node.inputs[0].id]
    scene.on_port_pressed(src_out)
    scene.on_port_moved(src_out, dst_in.scenePos())
    scene.on_port_released(src_out, dst_in.scenePos())


def _string_chain(scene: FlowScene, name: str) -> str:
    """建一个「单 STRING 输入 + 单 STRING 输出」节点并同步到画布。"""
    # 错开 X 坐标，避免多个节点叠在同一位置导致端口命中错乱
    x = len(scene.graph.nodes) * 300
    node = scene.graph.add_node(name=name, x=x, y=0)
    scene.graph.add_input(node.id, "in", PortType.STRING)
    scene.graph.add_output(node.id, "out", PortType.STRING)
    scene.sync_node(node.id)
    return node.id


def test_add_template_creates_node(qgis_app):
    scene = FlowScene()
    nid = scene.add_template("constant", 10, 20)
    assert nid is not None
    node = scene.graph.nodes[nid]
    assert node.x == 10
    assert node.y == 20
    assert scene.node_item(nid) is not None
    assert len(scene._node_items) == 1


def test_valid_connect_creates_edge(qgis_app):
    scene = FlowScene()
    src = scene.add_template("load_vector", 0, 0)
    dst = scene.add_template("buffer", 300, 0)
    events = []
    scene.edge_created.connect(lambda a, b: events.append((a, b)))
    _connect(scene, src, dst)
    assert len(scene.graph.edges) == 1
    assert len(scene._edge_items) == 1
    assert events == [(src, dst)]


def test_incompatible_connect_rejected(qgis_app):
    scene = FlowScene()
    src = scene.add_template("constant", 0, 0)  # 输出 NUMBER
    dst = scene.add_template("buffer", 300, 0)  # 输入 VECTOR
    _connect(scene, src, dst)
    assert len(scene.graph.edges) == 0
    assert len(scene._edge_items) == 0


def test_backward_drag_connect(qgis_app):
    """从输入端口拖向输出端口（反向）也应能建连。"""
    scene = FlowScene()
    src = scene.add_template("load_vector", 0, 0)
    dst = scene.add_template("buffer", 300, 0)
    src_item = scene.node_item(src)
    dst_item = scene.node_item(dst)
    src_out = src_item.port_items[src_item.node.outputs[0].id]
    dst_in = dst_item.port_items[dst_item.node.inputs[0].id]
    # 从 buffer 的输入端口拖向 load_vector 的输出端口
    scene.on_port_pressed(dst_in)
    scene.on_port_released(dst_in, src_out.scenePos())
    assert len(scene.graph.edges) == 1


def test_cycle_connect_rejected(qgis_app):
    scene = FlowScene()
    a = _string_chain(scene, "a")
    b = _string_chain(scene, "b")
    _connect(scene, a, b)
    _connect(scene, b, a)  # b→a 会成环，应被拒绝
    assert len(scene.graph.edges) == 1


def test_remove_node_cleans_edge_items(qgis_app):
    scene = FlowScene()
    src = scene.add_template("load_vector", 0, 0)
    dst = scene.add_template("buffer", 300, 0)
    _connect(scene, src, dst)
    scene.remove_node(src)
    assert len(scene.graph.edges) == 0
    assert len(scene._edge_items) == 0
    assert dst in scene.graph.nodes


def test_highlight_chain_dims_others(qgis_app):
    scene = FlowScene()
    a = _string_chain(scene, "a")
    b = _string_chain(scene, "b")
    c = _string_chain(scene, "c")
    _connect(scene, a, b)
    _connect(scene, b, c)
    orphan = _string_chain(scene, "orphan")

    scene.highlight_chain(b)
    assert scene.node_item(a).opacity() == 1.0
    assert scene.node_item(b).opacity() == 1.0
    assert scene.node_item(c).opacity() == 1.0
    assert scene.node_item(orphan).opacity() == 0.35
    # 链路上的边高亮，孤点没有边
    assert any(item._highlighted for item in scene._edge_items.values())

    scene.clear_highlight()
    assert scene.node_item(orphan).opacity() == 1.0


def test_select_ancestor_descendant(qgis_app):
    scene = FlowScene()
    a = _string_chain(scene, "a")
    b = _string_chain(scene, "b")
    _connect(scene, a, b)
    scene._select_node(b)
    assert scene.current_selected_node() == b
    assert scene.select_ancestor() is True
    assert scene.current_selected_node() == a
    assert scene.select_descendant() is True
    assert scene.current_selected_node() == b


def test_delete_selected(qgis_app):
    scene = FlowScene()
    a = _string_chain(scene, "a")
    b = _string_chain(scene, "b")
    _connect(scene, a, b)
    scene.node_item(b).setSelected(True)
    scene.delete_selected()
    assert b not in scene.graph.nodes
    assert len(scene.graph.edges) == 0


def test_copy_node_duplicates_ports(qgis_app):
    scene = FlowScene()
    src = scene.add_template("buffer", 0, 0)
    new_id = scene.copy_node(src)
    assert new_id is not None
    new = scene.graph.nodes[new_id]
    assert len(new.inputs) == 3  # buffer 模板有 3 个输入
    assert len(new.outputs) == 1
    assert new.x == 40.0
    assert new.y == 40.0
