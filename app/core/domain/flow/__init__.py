"""流程编排核心（纯逻辑，不依赖 QGIS / Qt）。

- :mod:`.types` 端口类型与兼容矩阵
- :mod:`.model` 流程图数据模型（节点 / 端口 / 边 / 绑定）
- :mod:`.topo` 拓扑排序与依赖查询
- :mod:`.serialization` 流程配置 JSON 导入导出

依赖 QGIS 的部分（执行器、算法库）按需 import，不在此处加载。
"""

from __future__ import annotations

from .model import (
    EdgeSpec,
    FlowGraph,
    InputSpec,
    NodeSpec,
    OutputSpec,
    PortRef,
)
from .serialization import SCHEMA_VERSION, FlowCodec
from .topo import (
    FlowCycleError,
    ancestors,
    dependencies,
    dependents,
    descendants,
    topo_sort,
)
from .types import PortType, can_connect, parse_port_type

__all__ = [
    "SCHEMA_VERSION",
    "EdgeSpec",
    "FlowCodec",
    "FlowCycleError",
    "FlowGraph",
    "InputSpec",
    "NodeSpec",
    "OutputSpec",
    "PortRef",
    "PortType",
    "ancestors",
    "can_connect",
    "dependencies",
    "dependents",
    "descendants",
    "parse_port_type",
    "topo_sort",
]
