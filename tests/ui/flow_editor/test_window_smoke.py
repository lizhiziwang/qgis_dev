"""流程编辑器主窗口冒烟测试（gui 标记）。

覆盖：窗口装配、选中节点刷新参数面板、运行流程（成功与失败分支）、
参数面板输入/输出/执行逻辑三个 tab 存在、JSON 往返。
对话框全部替换掉，避免 offscreen 下阻塞。
"""

from __future__ import annotations

import pytest
from PyQt6.QtWidgets import QApplication, QMessageBox

from app.core.domain.flow.executor import STATUS_FAILED, STATUS_SUCCESS
from app.core.domain.flow.serialization import FlowCodec
from app.ui.flow_editor import FlowEditorWindow

pytestmark = pytest.mark.gui


@pytest.fixture(autouse=True)
def _no_dialogs(monkeypatch):
    """把弹窗换成直接返回 Yes，防止 offscreen 下阻塞。"""
    for name in ("information", "warning", "critical", "question"):
        monkeypatch.setattr(
            QMessageBox,
            name,
            lambda *a, **k: QMessageBox.StandardButton.Yes,
        )


@pytest.fixture(autouse=True)
def _qapp(qgis_app):
    return QApplication.instance() or QApplication([])


def test_window_builds(qgis_app):
    w = FlowEditorWindow()
    assert w.scene is not None
    assert w.param_panel is not None
    assert w.log_panel is not None
    assert w.node_library is not None
    # 工具条有运行按钮
    assert w.act_run is not None


def test_select_node_shows_param_panel(qgis_app):
    w = FlowEditorWindow()
    nid = w.scene.add_template("constant", 0, 0)
    w._on_node_selected(nid)
    assert w.param_panel.current_node_id() == nid
    # 面板内含输入配置 / 输出配置 / 执行逻辑三个 tab
    from PyQt6.QtWidgets import QTabWidget

    tab = w.param_panel.findChild(QTabWidget)
    assert tab is not None
    texts = [tab.tabText(i) for i in range(tab.count())]
    assert "输入配置" in texts
    assert "输出配置" in texts
    assert "执行逻辑" in texts


def test_run_success_flow(qgis_app):
    w = FlowEditorWindow()
    nid = w.scene.add_template("constant", 0, 0)
    w.scene.graph.nodes[nid].inputs[0].default = "7"
    w._run_flow()
    assert w.executor.status_of(nid) == STATUS_SUCCESS
    assert w.executor.result_value(nid, w.scene.graph.nodes[nid].outputs[0].id) == 7


def test_run_failed_node_marks_failed(qgis_app):
    w = FlowEditorWindow()
    nid = w.scene.add_template("load_vector", 0, 0)
    w.scene.graph.nodes[nid].inputs[0].default = "/不存在/图层.gpkg"
    w._run_flow()  # 失败分支：弹窗已被 monkeypatch 替换，不会阻塞
    assert w.executor.status_of(nid) == STATUS_FAILED


def test_json_roundtrip_preserves_structure(qgis_app, tmp_path):
    w = FlowEditorWindow()
    src = w.scene.add_template("load_vector", 0, 0)
    dst = w.scene.add_template("buffer", 300, 0)

    src_item = w.scene.node_item(src)
    dst_item = w.scene.node_item(dst)
    src_out = src_item.port_items[src_item.node.outputs[0].id]
    dst_in = dst_item.port_items[dst_item.node.inputs[0].id]
    w.scene.on_port_pressed(src_out)
    w.scene.on_port_released(src_out, dst_in.scenePos())

    text = FlowCodec.to_json(w.scene.graph)
    graph2, warnings = FlowCodec.from_json(text)
    assert len(graph2.nodes) == 2
    assert len(graph2.edges) == 1
    assert not warnings
