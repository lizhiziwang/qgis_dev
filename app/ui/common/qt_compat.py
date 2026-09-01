"""Qt5 / Qt6 鼠标、拖放事件坐标兼容辅助。

项目统一通过 ``qgis.PyQt`` 使用 PyQt6 风格的 API（内部映射到 PyQt5 或 PyQt6，
Windows QGIS 4.2 是 PyQt6、Linux/QGIS 3.x 是 PyQt5）。

``qgis.PyQt`` 对枚举作用域、``exec()`` 等做了 Python 层 shim，但
``QMouseEvent.globalPosition()`` / ``position()`` 是 Qt6 新增的 C++ 方法，
PyQt5（Qt5）上没有，必须按版本取用：

- Qt6: ``event.globalPosition().toPoint()`` / ``event.position().toPoint()``
- Qt5: ``event.globalPos()`` / ``event.pos()``（均返回 ``QPoint``）
"""
from __future__ import annotations

from qgis.PyQt.QtCore import QPoint


def global_pos(event) -> QPoint:
    """事件在全局坐标系下的位置（标题栏拖动 / 手动 resize 用）。

    Qt6: ``QMouseEvent.globalPosition()``（QPointF）；Qt5: ``globalPos()``（QPoint）。
    """
    gp = getattr(event, "globalPosition", None)
    if gp is not None:
        return gp.toPoint()
    return event.globalPos()


def event_pos(event) -> QPoint:
    """事件在接收控件本地坐标系下的位置（drop / 边缘命中检测用）。

    Qt6: ``position()``（QMouseEvent / QDropEvent / QWheelEvent 均有）；
    Qt5: ``pos()``（QMouseEvent / QDropEvent / QWheelEvent 均返回 ``QPoint``）。
    """
    p = getattr(event, "position", None)
    if p is not None:
        return p.toPoint()
    return event.pos()
