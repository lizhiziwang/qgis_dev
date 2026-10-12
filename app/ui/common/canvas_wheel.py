"""画布滚轮方向：按「编辑设置 → 滚轮向前滚动」把缩放方向反过来。

做法是**把 ``QWheelEvent`` 的增量取反后重投**，而不是自己调 ``zoomIn`` /
``zoomOut``：``QgsMapCanvas`` 的滚轮缩放是「以光标为中心 + 连续平滑」的，自己
重写一遍很难还原，让事件照走原生逻辑、只把方向反过来最稳。

装在画布的 ``viewport()`` 上——``QGraphicsView``（``QgsMapCanvas`` 的基类）的
滚轮事件落在 viewport，经 ``QAbstractScrollArea`` 的事件过滤器转给视图。
"""

from __future__ import annotations

from qgis.PyQt.QtCore import QEvent, QObject, QPoint
from qgis.PyQt.QtGui import QWheelEvent
from qgis.PyQt.QtWidgets import QApplication

from app.infra.logging import get_logger

logger = get_logger(__name__)


class CanvasWheelDirectionFilter(QObject):
    """按设置翻转画布滚轮的缩放方向（不需要翻转时原样放行）。"""

    def __init__(self, canvas) -> None:
        super().__init__(canvas)
        self._canvas = canvas
        #: 重投时置位，避免重投的那个事件又被自己拦一次（无限递归）
        self._repassing = False
        viewport = canvas.viewport()
        if viewport is not None:
            viewport.installEventFilter(self)

    def eventFilter(self, obj, event) -> bool:  # noqa: N802 —— Qt 命名
        if (
            event.type() == QEvent.Type.Wheel
            and not self._repassing
            and self._should_reverse()
            and self._repass(obj, event)
        ):
            return True  # 已重投翻转后的事件，原事件吞掉
        return super().eventFilter(obj, event)

    @staticmethod
    def _should_reverse() -> bool:
        from app.infra.edit_settings import EditSettingsManager

        return EditSettingsManager.instance().current().reverse_wheel

    def _repass(self, obj, event) -> bool:
        """把取反的滚轮事件重投给画布；成功返回 ``True``（调用方吞掉原事件）。"""
        pd, ad = event.pixelDelta(), event.angleDelta()
        try:
            # 注意：本构建的 PyQt6 ``QWheelEvent`` **没有 ``source()``**，无脑带上第 9 个
            # 参数会抛 AttributeError（早先就是这里被吞掉，导致翻转静默失效 —— 用户报
            # "改成缩小地图后没生效"）。所以按需探测：没有就退回 8 参数重载。
            builder = [
                event.position(),
                event.globalPosition(),
                QPoint(-pd.x(), -pd.y()),
                QPoint(-ad.x(), -ad.y()),
                event.buttons(),
                event.modifiers(),
                event.phase(),
                not event.inverted(),
            ]
            source = getattr(event, "source", None)
            if callable(source):
                builder.append(source())
            try:
                flipped = QWheelEvent(*builder)
            except TypeError:  # 没有 9 参数重载
                flipped = QWheelEvent(*builder[:8])
        except Exception:  # noqa: BLE001 —— 构造不出来就放行原事件
            logger.warning("构造翻转滚轮事件失败，按原方向处理", exc_info=True)
            return False
        self._repassing = True
        try:
            QApplication.sendEvent(obj, flipped)
        finally:
            self._repassing = False
        return True


__all__ = ["CanvasWheelDirectionFilter"]
