"""QGIS 复用版 - 点绘制工具。

基于 :class:`qgis.gui.QgsMapToolCapture`（``CaptureMode.CapturePoint``）：

- 左键点击即完成，完成几何（含吸附修正后的坐标）经虚函数
  ``geometryCaptured`` 交付（4.2 无 ``digitizingFinished`` 信号，见 :mod:`.capture`）。
- 相比旧的 ``QgsMapToolEmitPoint``（不吸附），CapturePoint 自动获得磁性捕捉。

实现：单继承 :class:`.capture.CaptureQgsDrawTool`（QObject 包装器），
``tool`` 属性暴露给 :class:`DrawManager` 激活。
"""

from __future__ import annotations

from qgis.gui import QgsMapToolCapture

from .capture import CaptureQgsDrawTool


class QGISPointDrawTool(CaptureQgsDrawTool):
    """点捕捉（QGIS 内置版本），自动获得磁性捕捉。"""

    _capture_mode = QgsMapToolCapture.CapturePoint
