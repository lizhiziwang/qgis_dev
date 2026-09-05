"""复用 QGIS 内置能力的 5 种绘制工具。

与 ``app.core.domain.drawing.tools``（自定义实现）并存：

- **自定义版**：从零实现 ``QgsMapTool`` 子类，自己管 ``temp_points`` / 橡胶线。
- **本子包 (QGIS 版)**：点/线/面统一继承 ``QgsMapToolCapture``（经
  :class:`~.capture.QGISCaptureTool` 的链式子类），开启磁性捕捉（吸附已有
  要素顶点/边）；采集几何经 ``geometryCaptured`` 虚函数交给 Python 侧做后处理。
  矩形/圆在 QGIS 4 已移除 ``QgsMapToolShape*``，回退到自定义版实现
  （见 ``circle.py`` / ``rectangle.py``）。

二者通过 :class:`app.core.domain.drawing.manager.DrawManager` 上的不同入口激活：
``enable_draw_tool`` 走自定义版，``enable_qgis_draw_tool`` 走本子包。

模块导出 :data:`QGIS_TOOL_REGISTRY` 与 :func:`get_qgis_tool_class`，
与自定义版的 ``TOOL_REGISTRY`` / ``get_tool_class`` 接口对齐。
"""

from __future__ import annotations

from .base import BaseQgsDrawTool
from .circle import QGISCircleDrawTool
from .line import QGISLineDrawTool
from .point import QGISPointDrawTool
from .polygon import QGISPolygonDrawTool
from .rectangle import QGISRectangleDrawTool

QGIS_TOOL_REGISTRY: dict[str, type[BaseQgsDrawTool]] = {
    "point": QGISPointDrawTool,
    "line": QGISLineDrawTool,
    "polygon": QGISPolygonDrawTool,
    "rectangle": QGISRectangleDrawTool,
    "circle": QGISCircleDrawTool,
}


def get_qgis_tool_class(name: str) -> type[BaseQgsDrawTool]:
    """按名字拿 QGIS 复用版的工具类。"""
    if name not in QGIS_TOOL_REGISTRY:
        raise KeyError(f"unknown qgis draw tool: {name!r}")
    return QGIS_TOOL_REGISTRY[name]


__all__ = [
    "BaseQgsDrawTool",
    "QGISCircleDrawTool",
    "QGISLineDrawTool",
    "QGISPointDrawTool",
    "QGISPolygonDrawTool",
    "QGISRectangleDrawTool",
    "QGIS_TOOL_REGISTRY",
    "get_qgis_tool_class",
]
