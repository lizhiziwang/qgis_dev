"""QGIS 复用版数字化工具条。

与 :class:`app.ui.main_window.draw_toolbar.DigitizingToolbar`（自定义版）平行存在：

- **自定义版**：复用现有 ``DigitizingToolbar``，不改动
- **本类 (QGIS 版)**：独立工具条，5 个按钮（点/线/面/矩形/圆），
  通过 :meth:`DrawManager.enable_qgis_draw_tool` 激活
- 绘制完成的属性编辑流程与自定义版一致（调用方传入 ``on_finished`` 回调）
"""

from __future__ import annotations

from typing import Callable, Optional, TYPE_CHECKING

if TYPE_CHECKING:
    from qgis.core import QgsGeometry, QgsVectorLayer
    from qgis.gui import QgsMapCanvas

from PyQt5.QtWidgets import (
    QAction,
    QMainWindow,
    QMessageBox,
    QToolBar,
    QToolButton,
)

from app.core.domain.drawing import DrawManager


class QgisDigitizingToolbar:
    """QGIS 复用版数字化工具条（点 / 线 / 面 / 矩形 / 圆）。"""

    def __init__(
        self,
        main_window: "QMainWindow",
        canvas: "QgsMapCanvas",
        editing_layer_getter: Optional[Callable[[], "Optional[QgsVectorLayer]"]] = None,
        on_finished: Optional[Callable[["QgsGeometry"], None]] = None,
    ) -> None:
        """
        :param main_window: 主窗口
        :param canvas: 地图画布
        :param editing_layer_getter: 返回当前编辑图层（或 None）
        :param on_finished: 绘制完成的回调（通常是属性编辑 dialog）
        """
        self.main_window = main_window
        self.canvas = canvas
        self._editing_layer_getter = editing_layer_getter or (lambda: None)
        self._on_finished = on_finished

        self.draw_manager: Optional[DrawManager] = None

        # 每个按钮的 enable 状态（默认全 disabled，按编辑态 + 图层几何类型启用）
        self._buttons: dict[str, QToolButton] = {}
        # 当前记录的目标编辑图层（不直接决定按钮状态）
        self._layer: "Optional[QgsVectorLayer]" = None

        self._build_toolbar()

    # ============================================================== 装配

    def _build_toolbar(self) -> None:
        self.toolbar = QToolBar("QGIS 数字化")
        self.toolbar.setObjectName("QgisDigitizingToolbar")

        for label, geom_type in [
            ("Q-点", "point"),
            ("Q-线", "line"),
            ("Q-面", "polygon"),
            ("Q-矩形", "rectangle"),
            ("Q-圆", "circle"),
        ]:
            btn = QToolButton()
            btn.setText(label)
            btn.setEnabled(False)  # 默认禁用，由外部按图层启用
            btn.clicked.connect(lambda _checked=False, g=geom_type: self.enable_qgis_tool(g))
            self.toolbar.addWidget(btn)
            self._buttons[geom_type] = btn

        self.main_window.addToolBar(self.toolbar)

    # ============================================================== 公共 API

    def set_layer(self, layer: "Optional[QgsVectorLayer]") -> None:
        """仅"记录目标编辑图层"，按钮状态不变。

        按钮启用由 :meth:`enable_buttons_for_layer` 触发，通常绑定到
        ``DigitizingToolbar.editing_state_changed(True)`` 信号——即用户
        真正点击「开始编辑」后，5 个按钮才可点。
        """
        self._layer = layer

    def enable_buttons_for_layer(self, layer: "Optional[QgsVectorLayer]") -> None:
        """按图层几何类型启用对应按钮（其它禁用）。

        通常在 :class:`DigitizingToolbar` 进入编辑态时被调用。
        """
        # 先全禁用
        for btn in self._buttons.values():
            btn.setEnabled(False)

        if layer is None or layer.type() != layer.VectorLayer:
            return

        g_type = layer.geometryType()
        # 0=Point 1=Line 2=Polygon；矩形/圆不直接对应 layer geometry type，
        # 这里只在 layer 是 Polygon 时启用矩形 / 圆（最常见用法：编辑面/做缓冲区）
        if g_type == 0:
            self._buttons["point"].setEnabled(True)
        elif g_type == 1:
            self._buttons["line"].setEnabled(True)
        elif g_type == 2:
            self._buttons["polygon"].setEnabled(True)
            self._buttons["rectangle"].setEnabled(True)
            self._buttons["circle"].setEnabled(True)

    def disable_all_buttons(self) -> None:
        """停止编辑或切图层时调用：5 个按钮全禁用。"""
        for btn in self._buttons.values():
            btn.setEnabled(False)

    # ============================================================== 激活工具

    def enable_qgis_tool(self, geom_type: str) -> None:
        """激活指定类型的 QGIS 复用工具。"""
        # 懒创建 DrawManager（与自定义版 DigitizingToolbar 保持一致）
        if self.draw_manager is None:
            self.draw_manager = DrawManager(
                self.canvas,
                editing_layer_getter=self._editing_layer_getter,
            )
            if self._on_finished is not None:
                self.draw_manager.finished.connect(self._on_finished)

        self.draw_manager.enable_qgis_draw_tool(geom_type)
        QMessageBox.information(
            self.main_window,
            "提示",
            f"已激活「QGIS {geom_type}」工具，点击画布绘制！",
        )
