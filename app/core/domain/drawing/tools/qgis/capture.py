"""QGIS 复用版 - QgsMapToolCapture 的 Python 桥接子类。

QGIS 4.2 的 :class:`qgis.gui.QgsMapToolCapture` 与 3.x 有两点关键差异（源码 + 运行时探针确认）：

1. **构造必须携带真实 :class:`QgsAdvancedDigitizingDockWidget`**。
   4.2 删除了 3.x 的 ``(canvas, mode)`` 两参重载，构造器只有
   ``(canvas, cadDockWidget, mode)``；传入 ``None`` 时
   ``QgsMapToolAdvancedDigitizing::activate()`` 会对空指针取信号，鼠标一按即原生崩溃。
2. **不再提供 ``digitizingFinished`` / ``digitizingCanceled`` 信号**，
   完成几何统一经虚函数 :meth:`geometryCaptured` 交付
   （CapturePoint / CaptureLine / CapturePolygon 三种模式都会调用它）。

吸附机制（4.2 源码 ``qgsmaptooladvanceddigitizing.cpp``）：

- 鼠标事件处理里 ``cadDockWidget->cadEnabled()`` 为 False 时走
  auto-snap 分支：``e->snapPoint()`` → ``canvas->snappingUtils()->snapToMap(...)``。
- 因此磁性捕捉开关（工程 ``QgsProject.snappingConfig()`` + 画布
  ``QgsMapCanvasSnappingUtils``）对捕捉工具直接生效，无需额外接线。
- 另：捕捉坐标按 **canvas 当前图层**（``QgsMapCanvas.currentLayer()``）换算，
  激活工具前需把编辑图层设为画布当前图层（见 ``QgisDigitizingToolbar.enable_qgis_tool``），
  否则 CapturePoint 点空白处会对空图层做坐标换算（原生崩溃风险），
  且点/线/面几何 CRS 也会错。
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from typing import Optional

    from qgis.core import QgsGeometry
    from qgis.gui import (
        QgsAdvancedDigitizingDockWidget,
        QgsMapCanvas,
    )

from qgis.gui import QgsMapToolCapture

from .base import BaseQgsDrawTool


class QGISCaptureTool(QgsMapToolCapture):
    """把 QgsMapToolCapture 的完成几何桥接为 Python 回调的薄子类。"""

    def __init__(
        self,
        canvas: "QgsMapCanvas",
        cad_dock_widget: "QgsAdvancedDigitizingDockWidget",
        mode: "QgsMapToolCapture.CaptureMode",
    ) -> None:
        super().__init__(canvas, cad_dock_widget, mode)
        #: 捕获完成回调（由包装器注入，收 QgsGeometry）
        self._geometry_hook = None  # type: ignore[assignment]

    def geometryCaptured(self, geometry: "QgsGeometry") -> None:  # noqa: ANN001
        """QGIS 4.2 的"几何已捕获"虚函数（点/线/面三种模式统一入口）。"""
        hook = self._geometry_hook
        if hook is not None:
            hook(geometry)


class CaptureQgsDrawTool(BaseQgsDrawTool):
    """点/线/面三个捕捉型 wrapper 的公共实现。

    子类只需声明 :attr:`_capture_mode`。统一负责：

    - 构造 :class:`QGISCaptureTool`（外部传入共享 dock，否则自建并随 wrapper 存活）
    - 把 ``geometryCaptured`` 桥到 :meth:`BaseQgsDrawTool._emit_finished`
    """

    #: 子类设定：QgsMapToolCapture.Capture{Point,Line,Polygon}
    _capture_mode = None  # type: ignore[assignment]

    def __init__(
        self,
        canvas: "QgsMapCanvas",
        *,
        cad_dock_widget: "Optional[QgsAdvancedDigitizingDockWidget]" = None,
        is_buffer: bool = False,
        distance: float = 0.0,
    ) -> None:
        super().__init__(
            canvas,
            cad_dock_widget=cad_dock_widget,
            is_buffer=is_buffer,
            distance=distance,
        )
        dock = cad_dock_widget
        if dock is None:
            from qgis.gui import QgsAdvancedDigitizingDockWidget

            dock = QgsAdvancedDigitizingDockWidget(canvas)
            self._owned_dock = dock  # 生命周期随本 wrapper
        self._dock = dock
        self._tool = QGISCaptureTool(canvas, dock, self._capture_mode)
        self._tool._geometry_hook = self._emit_finished

    @property
    def tool(self):  # noqa: ANN201
        return self._tool
