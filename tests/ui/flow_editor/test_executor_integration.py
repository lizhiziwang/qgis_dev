"""流程执行器 QGIS 集成测试（gui 标记，需 QGIS 4.2 环境）。

走完整链路：加载矢量图层（模板代码）→ 缓冲区（native:buffer）→ 输出入工程。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from app.core.domain.flow.algorithm_library import instantiate_template, template_by_key
from app.core.domain.flow.executor import (
    STATUS_DISABLED,
    STATUS_FAILED,
    STATUS_SKIPPED,
    STATUS_SUCCESS,
    FlowExecutor,
    FlowNodeExecutionError,
)
from app.core.domain.flow.model import FlowGraph, PortRef
from app.core.domain.flow.topo import FlowCycleError
from app.core.domain.flow.types import PortType

#: 本模块全部依赖 QGIS，统一标记为 gui（CI 默认 ``-m 'not gui'`` 跳过）
pytestmark = pytest.mark.gui


@pytest.fixture(autouse=True)
def _clean_project(qgis_app):
    """每例结束清掉工程图层与「流程输出」分组，避免用例间串扰。"""
    yield
    from qgis.core import QgsProject

    QgsProject.instance().removeAllMapLayers()
    root = QgsProject.instance().layerTreeRoot()
    group = root.findGroup("流程输出")
    if group is not None:
        root.removeChildNode(group)


def _write_points_gpkg(tmp_path) -> str:
    """写一个 5 点的临时 GPKG 文件，返回路径。"""
    from PyQt6.QtCore import QVariant
    from qgis.core import (
        QgsFeature,
        QgsField,
        QgsGeometry,
        QgsVectorFileWriter,
        QgsVectorLayer,
    )

    layer = QgsVectorLayer("Point?crs=EPSG:4326", "源点", "memory")
    pr = layer.dataProvider()
    pr.addAttributes([QgsField("fid", QVariant.Int)])
    layer.updateFields()
    feats = []
    for i in range(5):
        f = QgsFeature()
        f.setGeometry(QgsGeometry.fromWkt(f"POINT({i} {i})"))
        f.setAttributes([i])
        feats.append(f)
    pr.addFeatures(feats)

    path = str(tmp_path / "src.gpkg")
    opts = QgsVectorFileWriter.SaveVectorOptions()
    opts.driverName = "GPKG"
    from qgis.core import QgsCoordinateTransformContext

    err, msg, *_ = QgsVectorFileWriter.writeAsVectorFormatV3(
        layer, path, QgsCoordinateTransformContext(), opts
    )
    assert err == QgsVectorFileWriter.WriterError.NoError, msg
    return path


def _write_polygon_gpkg(tmp_path, filename: str, wkt_list) -> str:
    """写一个含多个多边形的临时 GPKG 文件，返回路径。"""
    from qgis.core import (
        QgsCoordinateTransformContext,
        QgsFeature,
        QgsGeometry,
        QgsVectorFileWriter,
        QgsVectorLayer,
    )

    layer = QgsVectorLayer("Polygon?crs=EPSG:4326", "叠加源", "memory")
    pr = layer.dataProvider()
    for wkt in wkt_list:
        f = QgsFeature()
        f.setGeometry(QgsGeometry.fromWkt(wkt))
        pr.addFeatures([f])

    path = str(tmp_path / filename)
    opts = QgsVectorFileWriter.SaveVectorOptions()
    opts.driverName = "GPKG"
    err, msg, *_ = QgsVectorFileWriter.writeAsVectorFormatV3(
        layer, path, QgsCoordinateTransformContext(), opts
    )
    assert err == QgsVectorFileWriter.WriterError.NoError, msg
    return path


def _source_buffer_graph(tmp_path):
    """加载矢量 → 缓冲区 两节点流程图。"""
    path = _write_points_gpkg(tmp_path)
    graph = FlowGraph()
    src = instantiate_template(graph, template_by_key("load_vector"))
    src.inputs[0].default = path  # 图层路径
    buf = instantiate_template(graph, template_by_key("buffer"), x=300)
    graph.bind_input(buf.id, buf.inputs[0].id, PortRef(src.id, src.outputs[0].id))
    return graph, src, buf


def test_buffer_flow_runs_and_adds_result_to_project(qgis_app, tmp_path):
    graph, src, buf = _source_buffer_graph(tmp_path)
    executor = FlowExecutor()
    statuses = executor.run_flow(graph)

    assert statuses[src.id] == STATUS_SUCCESS
    assert statuses[buf.id] == STATUS_SUCCESS

    result = executor.result_value(buf.id, buf.outputs[0].id)
    assert result is not None
    assert result.isValid()

    # 输出已加入工程「流程输出」分组
    from qgis.core import QgsProject

    group = QgsProject.instance().layerTreeRoot().findGroup("流程输出")
    assert group is not None
    assert len(group.findLayers()) >= 1


def test_filter_attrs_flow_filters_features(qgis_app, tmp_path):
    """加载矢量 → 属性过滤：按表达式过滤要素，输出为图层对象且数量正确。"""
    path = _write_points_gpkg(tmp_path)  # 5 点，字段 fid = 0..4
    graph = FlowGraph()
    src = instantiate_template(graph, template_by_key("load_vector"))
    src.inputs[0].default = path
    flt = instantiate_template(graph, template_by_key("filter_attrs"), x=300)
    flt.inputs[1].default = '"fid" >= 3'
    graph.bind_input(flt.id, flt.inputs[0].id, PortRef(src.id, src.outputs[0].id))

    executor = FlowExecutor()
    statuses = executor.run_flow(graph)

    assert statuses[src.id] == STATUS_SUCCESS
    assert statuses[flt.id] == STATUS_SUCCESS

    result = executor.result_value(flt.id, flt.outputs[0].id)
    assert result is not None
    assert result.isValid()
    assert result.featureCount() == 2  # fid 3、4 两要素


@pytest.mark.parametrize(
    "template_key",
    ["overlay_intersect", "overlay_union", "overlay_difference", "overlay_symdiff"],
)
def test_overlay_templates_run(qgis_app, tmp_path, template_key):
    """四种经典叠加节点（相交/并集/差集/对称差）均可运行，输出有效且含要素。

    两个重叠多边形：(0,0)-(10,10) 与 (5,5)-(15,15)。
    """
    path_a = _write_polygon_gpkg(tmp_path, "a.gpkg", ["POLYGON((0 0, 10 0, 10 10, 0 10, 0 0))"])
    path_b = _write_polygon_gpkg(tmp_path, "b.gpkg", ["POLYGON((5 5, 15 5, 15 15, 5 15, 5 5))"])

    graph = FlowGraph()
    src_a = instantiate_template(graph, template_by_key("load_vector"))
    src_a.inputs[0].default = path_a
    src_b = instantiate_template(graph, template_by_key("load_vector"), x=300)
    src_b.inputs[0].default = path_b
    ovl = instantiate_template(graph, template_by_key(template_key), x=600)
    graph.bind_input(ovl.id, ovl.inputs[0].id, PortRef(src_a.id, src_a.outputs[0].id))
    graph.bind_input(ovl.id, ovl.inputs[1].id, PortRef(src_b.id, src_b.outputs[0].id))

    executor = FlowExecutor()
    statuses = executor.run_flow(graph)

    assert statuses[ovl.id] == STATUS_SUCCESS
    result = executor.result_value(ovl.id, ovl.outputs[0].id)
    assert result is not None
    assert result.isValid()
    assert result.featureCount() >= 1


def test_input_value_from_upstream_loaded_as_layer(qgis_app, tmp_path):
    """上游输出是路径字符串时，下游矢量输入应被归一成图层对象。"""
    graph, src, _ = _source_buffer_graph(tmp_path)
    executor = FlowExecutor()
    executor.run_flow(graph)

    # 上游「加载矢量」把图层对象缓存；缓冲区拿到的输入必须是图层对象而非路径
    src_value = executor.result_value(src.id, src.outputs[0].id)
    assert src_value.isValid()
    from qgis.core import QgsVectorLayer

    assert isinstance(src_value, QgsVectorLayer)


def test_flow_signal_sequence(qgis_app, tmp_path):
    graph, src, buf = _source_buffer_graph(tmp_path)
    executor = FlowExecutor()
    started = []
    finished = []
    executor.node_started.connect(lambda nid, name: started.append(nid))
    executor.node_finished.connect(
        lambda nid, name, ok, ms, summary, err: finished.append((nid, ok, ms))
    )
    executor.run_flow(graph)

    assert started == [src.id, buf.id]
    assert all(ok for _, ok, _ in finished)
    assert all(ms > 0 for _, _, ms in finished)
    # 摘要包含输入输出
    summaries = []

    def _cap(nid, name, ok, ms, summary, err):
        summaries.append(summary)

    executor.node_finished.connect(_cap)
    executor.run_flow(graph)
    assert any("输入[" in s for s in summaries)


def test_single_node_run_reuses_upstream_cache(qgis_app, tmp_path):
    graph, src, buf = _source_buffer_graph(tmp_path)
    executor = FlowExecutor()

    executor.run_node(graph, src.id)
    executor.run_node(graph, buf.id)

    result = executor.result_value(buf.id, buf.outputs[0].id)
    assert result is not None
    assert result.isValid()


def test_failed_node_raises_and_marks_failed(qgis_app):
    graph = FlowGraph()
    node = graph.add_node(name="坏节点", code='raise ValueError("boom")')
    graph.add_output(node.id, "out", PortType.STRING)

    executor = FlowExecutor()
    events = []
    executor.node_finished.connect(
        lambda nid, name, ok, ms, summary, err: events.append((nid, ok, err))
    )
    with pytest.raises(FlowNodeExecutionError):
        executor.run_flow(graph)

    assert events
    assert events[0][0] == node.id
    assert events[0][1] is False
    assert "boom" in events[0][2]
    assert executor.status_of(node.id) == STATUS_FAILED


def test_cycle_flow_raises(qgis_app):
    from app.core.domain.flow.model import EdgeSpec

    graph = FlowGraph()
    a = graph.add_node(name="a")
    graph.add_output(a.id, "out", PortType.STRING)
    b = graph.add_node(name="b")
    graph.add_input(b.id, "in", PortType.STRING)
    # 直接注入反向边成环（模拟脏数据）
    graph.edges["edge_1"] = EdgeSpec("edge_1", a.id, a.outputs[0].id, b.id, b.inputs[0].id)
    graph.edges["edge_2"] = EdgeSpec("edge_2", b.id, "ghost_out", a.id, a.inputs[0].id if a.inputs else "ghost_in")

    executor = FlowExecutor()
    with pytest.raises(FlowCycleError):
        executor.run_flow(graph)


def test_disabled_and_skipped_not_executed(qgis_app):
    graph = FlowGraph()
    a = graph.add_node(name="a", code='outputs["out"] = 1')
    a.enabled = False
    graph.add_output(a.id, "out", PortType.NUMBER)
    b = graph.add_node(name="b", code='outputs["out"] = 2')
    b.skip = True
    graph.add_output(b.id, "out", PortType.NUMBER)
    c = graph.add_node(name="c", code='outputs["out"] = 3')
    graph.add_output(c.id, "out", PortType.NUMBER)

    executor = FlowExecutor()
    statuses = executor.run_flow(graph)

    assert statuses[a.id] == STATUS_DISABLED
    assert statuses[b.id] == STATUS_SKIPPED
    assert statuses[c.id] == STATUS_SUCCESS
    assert executor.result_value(a.id, a.outputs[0].id) is None
    assert executor.result_value(b.id, b.outputs[0].id) is None
    assert executor.result_value(c.id, c.outputs[0].id) == 3


def test_output_saved_to_file_when_save_path_set(qgis_app, tmp_path):
    graph, _, buf = _source_buffer_graph(tmp_path)
    out = buf.outputs[0]
    out.save_path = str(tmp_path / "result.gpkg")
    out.load_to_canvas = False

    executor = FlowExecutor()
    executor.run_flow(graph)

    # 输出落盘 → 解析值是路径字符串
    result = executor.result_value(buf.id, out.id)
    assert isinstance(result, str)
    assert result.endswith(".gpkg")
    assert Path(result).exists()


def test_number_output_passthrough(qgis_app):
    graph = FlowGraph()
    node = graph.add_node(name="常量")
    graph.add_input(node.id, "值", PortType.NUMBER, default="7")
    out = graph.add_output(node.id, "数值", PortType.NUMBER)
    node.code = 'outputs["数值"] = inputs["值"]'

    executor = FlowExecutor()
    executor.run_flow(graph)
    assert executor.result_value(node.id, out.id) == 7
