"""QGIS 复用工具的公共基类。

职责：

- 统一持有 ``finished(QgsGeometry)`` 信号（与自定义版 :class:`BaseDrawTool` 对齐）。
- 提供 ``is_buffer`` / ``distance`` 后处理。
- 提供 ``cleanup`` 钩子，关闭 QGIS 自带的 rubber band。
- **必须**继承 :class:`PyQt6.QtCore.QObject`，否则 :pyattr:`finished`
  （:class:`pyqtSignal`）无法 emit，调用时会触发 Qt native 崩溃
  （Windows 上表现为 ``STATUS_STACK_BUFFER_OVERRUN`` 0xC0000409）。

子类的具体构造方式因 QGIS 内置基类而异，所以本类**不**再继承
``QgsMapTool``，而是一个 mixin：子类在 ``__init__`` 里 ``super().__init__(canvas)``
之后调用 :meth:`_wire_common_state` 把信号连上。
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from qgis.core import QgsGeometry
    from qgis.gui import QgsMapCanvas, QgsMapTool

from PyQt6.QtCore import QObject, QTimer, pyqtSignal


class BaseQgsDrawTool(QObject):
    """QGIS 复用工具的统一协议基类（继承 QObject 以承载 pyqtSignal）。"""

    #: 与自定义版 ``BaseDrawTool.finished`` 对齐。
    finished = pyqtSignal(object)

    def __init__(
        self,
        canvas: "QgsMapCanvas",
        *,
        is_buffer: bool = False,
        distance: float = 0.0,
    ) -> None:
        # 必须显式调 QObject.__init__，因为具体子类的 super().__init__
        # 链可能跳过了我们（如果它们同时继承其它 QObject 派生类）。
        QObject.__init__(self)
        self.canvas = canvas
        self.is_buffer = bool(is_buffer)
        self.distance = float(distance)
        self.geom = None  # type: ignore[assignment]
        # 防止 finished 被 emit 多次（QGIS 部分信号在外部也可能触发）
        self._finished_emitted = False

    # ------------------------------------------------------ 后处理

    def _emit_finished(self, geometry: "QgsGeometry") -> None:
        """子类的"绘制完成"信号统一调这里。

        负责 buffer 后处理 + 发 finished + 卸工具。
        ``unsetMapTool`` 必须延后到当前 Qt 事件循环出栈之后执行，
        否则会从 QGIS 工具自己的事件 handler 中重入，导致 native 崩溃。
        """
        if self._finished_emitted:
            return
        self._finished_emitted = True

        self.geom = geometry
        if self.is_buffer and geometry is not None and not geometry.isEmpty():
            try:
                geometry.buffer(self.distance, 16)
            except Exception:  # noqa: BLE001
                pass

        # 延后 unset，避免在 QGIS 工具自身的事件回调中重入
        try:
            QTimer.singleShot(0, self._safe_unset_tool)
        except Exception:  # noqa: BLE001
            pass

        self.finished.emit(geometry)

    def _safe_unset_tool(self) -> None:
        try:
            tool = self.tool  # 子类属性
        except Exception:  # noqa: BLE001
            tool = None
        if tool is None:
            return
        try:
            if self.canvas is not None:
                self.canvas.unsetMapTool(tool)
        except Exception:  # noqa: BLE001
            pass

    # ------------------------------------------------------ 生命周期

    def cleanup(self) -> None:
        """子类按需重写：清理 rubber band、临时点等。"""
        self._safe_unset_tool()

    # ------------------------------------------------------ 协议

    @property
    def tool(self) -> "QgsMapTool":
        """返回真正的 QGIS ``QgsMapTool`` 实例。子类必须实现。"""
        raise NotImplementedError
