"""流程编排：流程配置 JSON 导入 / 导出。

纯逻辑模块。导出为 schema v1 字典/JSON 字符串；导入时对脏数据做容错——
未知节点引用 / 类型不兼容 / 端口不存在的边会被丢弃并收集 warning（由 UI
弹窗提醒用户）。
"""

from __future__ import annotations

import json
from typing import Any

from .model import EdgeSpec, FlowGraph, InputSpec, NodeSpec, OutputSpec
from .types import can_connect, parse_port_type

SCHEMA_VERSION = 1


class FlowCodec:
    """流程图 ↔ JSON 互转。"""

    @staticmethod
    def to_dict(graph: FlowGraph) -> dict[str, Any]:
        nodes = []
        for node in graph.nodes.values():
            nodes.append(
                {
                    "id": node.id,
                    "name": node.name,
                    "x": node.x,
                    "y": node.y,
                    "enabled": node.enabled,
                    "skip": node.skip,
                    "algorithm_id": node.algorithm_id,
                    "code": node.code,
                    "inputs": [
                        {
                            "id": ins.id,
                            "name": ins.name,
                            "type": ins.type.name,
                            "default": ins.default,
                            "required": ins.required,
                            "choices": list(ins.choices),
                            "type_locked": ins.type_locked,
                        }
                        for ins in node.inputs
                    ],
                    "outputs": [
                        {
                            "id": outs.id,
                            "name": outs.name,
                            "type": outs.type.name,
                            "save_path": outs.save_path,
                            "write_temp": outs.write_temp,
                            "load_to_canvas": outs.load_to_canvas,
                            "type_locked": outs.type_locked,
                        }
                        for outs in node.outputs
                    ],
                }
            )
        edges = [
            {
                "id": e.id,
                "src_node": e.src_node,
                "src_port": e.src_port,
                "dst_node": e.dst_node,
                "dst_port": e.dst_port,
            }
            for e in graph.edges.values()
        ]
        return {"schema_version": SCHEMA_VERSION, "nodes": nodes, "edges": edges}

    @staticmethod
    def to_json(graph: FlowGraph) -> str:
        return json.dumps(FlowCodec.to_dict(graph), ensure_ascii=False, indent=2)

    @staticmethod
    def from_dict(data: dict[str, Any]) -> tuple[FlowGraph, list[str]]:
        """从字典重建流程图。返回 ``(graph, warnings)``，warnings 是给用户看的提示。"""
        warnings: list[str] = []
        graph = FlowGraph()

        for raw in data.get("nodes", []):
            node = NodeSpec(
                id=str(raw["id"]),
                name=str(raw.get("name", "节点")),
                x=float(raw.get("x", 0.0)),
                y=float(raw.get("y", 0.0)),
                enabled=bool(raw.get("enabled", True)),
                skip=bool(raw.get("skip", False)),
                algorithm_id=str(raw.get("algorithm_id", "")),
                code=str(raw.get("code", "")),
            )
            for iraw in raw.get("inputs", []):
                try:
                    node.inputs.append(
                        InputSpec(
                            id=str(iraw["id"]),
                            name=str(iraw.get("name", "输入")),
                            type=parse_port_type(str(iraw.get("type", "STRING"))),
                            default=str(iraw.get("default", "")),
                            required=bool(iraw.get("required", False)),
                            choices=tuple(
                                str(c) for c in iraw.get("choices", []) if str(c)
                            ),
                            type_locked=bool(iraw.get("type_locked", False)),
                        )
                    )
                except (KeyError, ValueError) as exc:
                    warnings.append(f"节点「{node.name}」的输入参数解析失败：{exc}，已跳过")
            for oraw in raw.get("outputs", []):
                try:
                    node.outputs.append(
                        OutputSpec(
                            id=str(oraw["id"]),
                            name=str(oraw.get("name", "输出")),
                            type=parse_port_type(str(oraw.get("type", "STRING"))),
                            save_path=str(oraw.get("save_path", "")),
                            write_temp=bool(oraw.get("write_temp", True)),
                            load_to_canvas=bool(oraw.get("load_to_canvas", False)),
                            type_locked=bool(oraw.get("type_locked", False)),
                        )
                    )
                except (KeyError, ValueError) as exc:
                    warnings.append(f"节点「{node.name}」的输出端口解析失败：{exc}，已跳过")
            graph.nodes[node.id] = node

        for eraw in data.get("edges", []):
            edge, warn = _build_edge(eraw, graph)
            if edge is not None:
                graph.edges[edge.id] = edge
            if warn:
                warnings.append(warn)

        return graph, warnings

    @staticmethod
    def from_json(text: str) -> tuple[FlowGraph, list[str]]:
        data = json.loads(text)
        if data.get("schema_version", SCHEMA_VERSION) != SCHEMA_VERSION:
            # 只支持当前版本，其它版本尝试按当前结构解析，容错交给 from_dict
            pass
        return FlowCodec.from_dict(data)


def _build_edge(raw: dict[str, Any], graph: FlowGraph) -> tuple[EdgeSpec | None, str | None]:
    """按一条边字典建边并校验；不合法返回 ``(None, 原因)``。"""
    src_node = str(raw.get("src_node", ""))
    src_port = str(raw.get("src_port", ""))
    dst_node = str(raw.get("dst_node", ""))
    dst_port = str(raw.get("dst_port", ""))
    edge_id = str(raw.get("id", "")) or f"edge_{len(graph.edges) + 1}"

    if src_node not in graph.nodes or dst_node not in graph.nodes:
        return None, f"边 {src_node}.{src_port}→{dst_node}.{dst_port} 引用了不存在的节点，已丢弃"
    src_out = graph.output_port(src_node, src_port)
    dst_in = graph.input_port(dst_node, dst_port)
    if src_out is None or dst_in is None:
        return None, f"边 {src_node}.{src_port}→{dst_node}.{dst_port} 引用了不存在的端口，已丢弃"
    if not can_connect(src_out.type, dst_in.type):
        return None, (
            f"边 {src_node}.{src_port}→{dst_node}.{dst_port} 类型不兼容"
            f"（{src_out.type.value}→{dst_in.type.value}），已丢弃"
        )
    # 同一输入端口出现多条边 → 只保留第一条
    if graph.edge_into(dst_node, dst_port) is not None:
        return None, f"输入端口 {dst_node}.{dst_port} 存在多条数据源，已丢弃多余的边"

    return (
        EdgeSpec(
            id=edge_id,
            src_node=src_node,
            src_port=src_port,
            dst_node=dst_node,
            dst_port=dst_port,
        ),
        None,
    )
