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

    Qt6: ``QMouseEvent.globalPosition()`` 是**方法**，调用后返回 QPointF；
    Qt5: ``globalPos()``（QPoint）。
    """
    gp = getattr(event, "globalPosition", None)
    if gp is not None:
        return gp().toPoint()  # Qt6：先调用 globalPosition()
    return event.globalPos()  # Qt5：QMouseEvent.globalPos() 直接返回 QPoint


def event_pos(event) -> QPoint:
    """事件在接收控件本地坐标系下的位置（drop / 边缘命中检测用）。

    Qt6: ``position()`` 是**方法**，调用后返回 QPointF；Qt5: ``pos()``
    （QPoint）。Qt6 的 QDropEvent 已无 ``pos()``，必须走 ``position()``。
    """
    p = getattr(event, "position", None)
    if p is not None:
        return p().toPoint()  # Qt6：先调用 position()
    return event.pos()  # Qt5：QMouseEvent/QDropEvent/QWheelEvent.pos() 返回 QPoint
