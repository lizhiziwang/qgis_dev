"""QGIS 复用版 - 面绘制工具。

基于 :class:`qgis.gui.QgsMapToolCapture`（``CaptureMode.CapturePolygon``）：

- 左键加点、右键/双击闭合结束由 QGIS 内部管理，完成几何经虚函数
  ``geometryCaptured`` 交付（4.2 无 ``digitizingFinished`` 信号，见 :mod:`.capture`）。
- 磁性捕捉 / 闭合 / 撤销 / Advanced Digitizing Dock 等能力自动获得。

实现：单继承 :class:`.capture.CaptureQgsDrawTool`（QObject 包装器），
``tool`` 属性暴露给 :class:`DrawManager` 激活。
"""

from __future__ import annotations

from qgis.gui import QgsMapToolCapture

from .capture import CaptureQgsDrawTool


class QGISPolygonDrawTool(CaptureQgsDrawTool):
    """画面（QGIS 内置版本），自动获得捕捉 / 闭合 / 撤销。"""

    _capture_mode = QgsMapToolCapture.CapturePolygon
