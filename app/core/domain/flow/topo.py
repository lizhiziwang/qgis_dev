"""流程编排：拓扑排序与依赖查询。

纯逻辑模块，不依赖 QGIS / Qt。执行前必须拓扑排序，保证每个节点执行时
上游结果已就绪；有环则抛 :class:`FlowCycleError`。
"""

from __future__ import annotations

from collections import deque

from .model import FlowGraph


class FlowCycleError(ValueError):
    """流程图中存在循环依赖。"""

    def __init__(self, nodes_in_cycle: list[str]) -> None:
        self.nodes_in_cycle = nodes_in_cycle
        super().__init__(f"检测到循环依赖：{' → '.join(nodes_in_cycle)}")


def dependencies(graph: FlowGraph, node_id: str) -> set[str]:
    """直接上游（该节点的输入所来自的节点）。"""
    return {e.src_node for e in graph.edges.values() if e.dst_node == node_id}


def dependents(graph: FlowGraph, node_id: str) -> set[str]:
    """直接下游（把该节点输出作为输入的节点）。"""
    return {e.dst_node for e in graph.edges.values() if e.src_node == node_id}


def ancestors(graph: FlowGraph, node_id: str) -> set[str]:
    """全部祖先节点（沿输入边向上递归）。"""
    seen: set[str] = set()
    stack = list(dependencies(graph, node_id))
    while stack:
        cur = stack.pop()
        if cur in seen:
            continue
        seen.add(cur)
        stack.extend(dependencies(graph, cur))
    return seen


def descendants(graph: FlowGraph, node_id: str) -> set[str]:
    """全部子孙节点（沿输出边向下递归）。"""
    seen: set[str] = set()
    stack = list(dependents(graph, node_id))
    while stack:
        cur = stack.pop()
        if cur in seen:
            continue
        seen.add(cur)
        stack.extend(dependents(graph, cur))
    return seen


def topo_sort(graph: FlowGraph) -> list[str]:
    """Kahn 拓扑排序，返回按依赖顺序排列的节点 id 列表。

    忽略悬空边（引用了不存在的节点，导入脏 JSON 时可能出现）。
    :raises FlowCycleError: 存在循环依赖。
    """
    indegree = dict.fromkeys(graph.nodes, 0)
    adj: dict[str, list[str]] = {nid: [] for nid in graph.nodes}
    for e in graph.edges.values():
        if e.src_node not in graph.nodes or e.dst_node not in graph.nodes:
            continue  # 悬空边（节点已被删/导入脏数据）不参与排序
        adj[e.src_node].append(e.dst_node)
        indegree[e.dst_node] += 1

    queue = deque(nid for nid, deg in indegree.items() if deg == 0)
    order: list[str] = []
    while queue:
        cur = queue.popleft()
        order.append(cur)
        for nxt in adj[cur]:
            indegree[nxt] -= 1
            if indegree[nxt] == 0:
                queue.append(nxt)

    if len(order) != len(graph.nodes):
        remaining = [nid for nid, deg in indegree.items() if deg > 0]
        raise FlowCycleError(remaining)
    return order
