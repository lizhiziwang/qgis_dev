"""绘图 UI 装配（与旧 :class:`component.draw_tool.Draw` 行为一致）。

按旧版设计：

- 工具条含 2 个 :class:`QToolButton`（编辑器 / 添加要素），各带一个
  :class:`QMenu`。
- 编辑器菜单：开始编辑 / 停止编辑 / 保存所选图层编辑
- 添加要素菜单：添加点 / 添加线 / 添加面
- 默认全部 disabled；外部 :meth:`set_edit_target` 启用"开始编辑"按钮。
- 几何类型按当前 layer 启用对应按钮（点 layer 只能点，线 layer 只能线）。

状态机：

::

    [无编辑图层] --set_edit_target--> [已指定编辑图层] --start_edit_fun--> [编辑中]
                                                                     |
                                                                     +--stop_edit_fun--> [已指定编辑图层]
                                                                     +--stop_edit_fun(切换其它图层)--> [无编辑图层]

:attr:`editing_state_changed` 信号在状态切换时发出，供 :class:`MainWindow`
转发给 :class:`QgisDigitizingToolbar` 等其它工具条同步。
"""

from __future__ import annotations

from typing import Optional, TYPE_CHECKING

if TYPE_CHECKING:
    from qgis.core import QgsVectorLayer
    from qgis.gui import QgsMapCanvas

from PyQt6.QtCore import Qt, QObject, pyqtSignal
from PyQt6.QtGui import QAction, QColor
from PyQt6.QtWidgets import (
    QMainWindow,
    QMenu,
    QMessageBox,
    QToolBar,
    QToolButton,
)

from qgis.core import (
    QgsGeometry,
    QgsWkbTypes,
)
from qgis.gui import QgsRubberBand

from app.core.domain.drawing import DrawManager
from app.core.domain.drawing.tools import get_tool_class


class DigitizingToolbar(QObject):
    """绘制 / 编辑工具条（与旧 ``component.draw_tool.Draw`` 行为一致）。

    继承 :class:`QObject` 是为了挂 :pyattr:`editing_state_changed` 信号——
    :class:`pyqtSignal` 必须挂在 :class:`QObject` 派生类上才能 ``emit`` /
    ``connect``，否则会触发 ``TypeError: cannot be converted to PyQt6.QtCore.QObject``。
    """

    #: 编辑状态切换时发出：True=进入编辑态，False=退出编辑态。
    editing_state_changed = pyqtSignal(bool)

    def __init__(
        self,
        main_window: "QMainWindow",
        canvas: "QgsMapCanvas",
        editing_layer_getter=None,
    ) -> None:
        # QObject.__init__ 必须显式调用（QObject 没有无参 __init__，会自动绑定 parent）
        QObject.__init__(self)
        self.main_window = main_window
        self.canvas = canvas
        # 编辑图层 getter：返回当前 ``edit_layer`` 或 None
        self._editing_layer_getter = editing_layer_getter or (lambda: None)
        # 业务层 DrawManager（懒创建）
        self.draw_manager: Optional[DrawManager] = None
        # 临时点 + 共享 rubber_band（与旧 Draw 一致）
        self.temp_points: list = []
        self.rubber_band = None
        self.active_tool = None

        # 当前是否处于编辑态（start_edit_fun 成功后 True，stop_edit_fun 后 False）
        self._editing = False

        self._build_toolbar()

    # ============================================================== 装配

    def _build_toolbar(self) -> None:
        self.digitizing_toolbar = QToolBar("编辑工具")
        self.digitizing_toolbar.setObjectName("DigitizingToolbar")

        # ---- 编辑器下拉按钮 ----
        self.digitize_button = QToolButton()
        self.digitize_button.setText("编辑器")
        self.digitize_button.setPopupMode(QToolButton.ToolButtonPopupMode.InstantPopup)
        self.digitize_menu = QMenu()

        self.start_edit = QAction("开始编辑", self.digitize_menu)
        self.start_edit.triggered.connect(self.start_edit_fun)
        self.stop_edit = QAction("停止编辑", self.digitize_menu)
        self.stop_edit.triggered.connect(self.stop_edit_fun)
        self.save_edit = QAction("保存所选图层编辑", self.digitize_menu)
        # 旧逻辑：stop_edit 也绑了 commitChanges，但语义重叠，这里拆开
        self.save_edit.triggered.connect(self._save_edits)

        # 默认禁用（与旧版一致：edit_layer is None 时全 disable）
        self._set_edit_menu_enabled(False)

        self.digitize_menu.addAction(self.start_edit)
        self.digitize_menu.addAction(self.stop_edit)
        self.digitize_menu.addAction(self.save_edit)
        self.digitize_button.setMenu(self.digitize_menu)
        self.digitizing_toolbar.addWidget(self.digitize_button)

        # ---- 添加要素下拉按钮 ----
        self.add_feature_button = QToolButton()
        self.add_feature_button.setText("添加要素")
        self.add_feature_button.setPopupMode(QToolButton.ToolButtonPopupMode.InstantPopup)
        self.add_feature_menu = QMenu()

        self.add_point_action = QAction("添加点", self.add_feature_menu)
        self.add_point_action.triggered.connect(lambda: self.enable_draw_tool("point"))
        self.add_line_action = QAction("添加线", self.add_feature_menu)
        self.add_line_action.triggered.connect(lambda: self.enable_draw_tool("line"))
        self.add_polygon_action = QAction("添加面", self.add_feature_menu)
        self.add_polygon_action.triggered.connect(lambda: self.enable_draw_tool("polygon"))

        # 默认禁用
        self.add_point_action.setEnabled(False)
        self.add_line_action.setEnabled(False)
        self.add_polygon_action.setEnabled(False)

        self.add_feature_menu.addAction(self.add_point_action)
        self.add_feature_menu.addAction(self.add_line_action)
        self.add_feature_menu.addAction(self.add_polygon_action)
        self.add_feature_button.setMenu(self.add_feature_menu)
        self.digitizing_toolbar.addWidget(self.add_feature_button)

        # 挂到主窗口
        self.main_window.addToolBar(self.digitizing_toolbar)

    # ============================================================== 公共 API

    def set_edit_target(self, layer: Optional["QgsVectorLayer"] = None) -> None:
        """外部调用：把 ``layer`` 设为"目标编辑图层"。

        仅打开"开始编辑"按钮（让用户能进入编辑态）；不直接修改 QGIS
        图层的 editing 状态——那是用户点"开始编辑"时才发生的。

        :param layer: 新目标图层；为 None 时按当前 getter 的值处理。
        """
        target = layer if layer is not None else self._current_layer()
        if target is None:
            self._set_edit_menu_enabled(False)
            self._set_add_buttons_for_geometry(-1)
            return
        if target.type() != target.VectorLayer:
            self._set_edit_menu_enabled(False)
            self._set_add_buttons_for_geometry(-1)
            return

        # 进入"已指定编辑图层"态：开始编辑可用，按图层几何类型启用"添加要素"
        self._set_edit_menu_enabled(True, editing_now=False)
        self._set_add_buttons_for_geometry(target.geometryType())

    def refresh_for_current_layer(self) -> None:
        """根据当前 ``edit_layer`` 重新刷一遍按钮状态（应对切图层等场景）。"""
        layer = self._current_layer()
        if layer is None or layer.type() != layer.VectorLayer:
            self._set_edit_menu_enabled(False)
            self._set_add_buttons_for_geometry(-1)
            return
        if layer.isEditable():
            # 当前已经在编辑态，保持按钮可用状态
            self._set_edit_menu_enabled(True, editing_now=True)
            self._set_add_buttons_for_geometry(layer.geometryType())
        else:
            self._set_edit_menu_enabled(True, editing_now=False)
            self._set_add_buttons_for_geometry(layer.geometryType())

    # ============================================================== 编辑器菜单回调

    def start_edit_fun(self) -> None:
        """开始编辑：真正调 ``layer.startEditing()``，切换按钮状态。"""
        layer = self._current_layer()
        if layer is None or layer.type() != layer.VectorLayer:
            return
        if layer.isEditable():
            # 已经在编辑态：保持现状即可（按钮状态以 _editing 为准）
            if not self._editing:
                self._editing = True
                self._set_edit_menu_enabled(True, editing_now=True)
                self._set_add_buttons_for_geometry(layer.geometryType())
            return

        ok = layer.startEditing()
        if not ok:
            QMessageBox.warning(self.main_window, "警告", "无法开始编辑！")
            return

        self._editing = True
        self._set_edit_menu_enabled(True, editing_now=True)
        self._set_add_buttons_for_geometry(layer.geometryType())
        self.editing_state_changed.emit(True)

    def stop_edit_fun(self) -> None:
        """停止编辑：弹"是否保存"对话框。"""
        layer = self._current_layer()
        if layer is None or not layer.isEditable():
            return
        buffer = layer.editBuffer()
        if buffer is not None:
            reply = QMessageBox.warning(
                self.main_window,
                "警告",
                "是否保存编辑的内容！",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.Yes,
            )
            if reply == QMessageBox.StandardButton.Yes:
                layer.commitChanges(stopEditing=True)
            else:
                layer.rollBack()
                layer.commitChanges(stopEditing=True)

        self._editing = False
        # 切回"已指定编辑图层"态：开始编辑可用，停止/保存禁用
        self._set_edit_menu_enabled(True, editing_now=False)
        # "添加要素"按钮保持启用（layer 仍是 target，但未进入 editing 态）
        # ——若要严格只在 editing 态可用，把下面这行注释掉
        if layer is not None and layer.type() == layer.VectorLayer:
            self._set_add_buttons_for_geometry(layer.geometryType())
        else:
            self._set_add_buttons_for_geometry(-1)
        self.clear_draw_tool()
        self.editing_state_changed.emit(False)

    def _save_edits(self) -> None:
        """仅保存编辑，不停止。"""
        layer = self._current_layer()
        if layer is not None and layer.isEditable():
            layer.commitChanges(stopEditing=False)

    # ============================================================== 添加要素菜单回调

    def enable_draw_tool(self, geom_type: str) -> None:
        """激活指定类型的绘制工具（点/线/面）。

        共享 ``temp_points`` / ``rubber_band`` 与子工具协同（与旧版一致）。
        """
        # 先清除旧工具
        self.clear_draw_tool()

        rubber_band_type = {
            "point": QgsWkbTypes.PointGeometry,
            "line": QgsWkbTypes.LineGeometry,
            "polygon": QgsWkbTypes.PolygonGeometry,
        }[geom_type]
        self.rubber_band = QgsRubberBand(self.canvas, rubber_band_type)
        self.rubber_band.setColor(Qt.GlobalColor.red)
        self.rubber_band.setWidth(2)
        self.rubber_band.setFillColor(QColor(255, 0, 0, 50))

        # 懒创建 DrawManager
        if self.draw_manager is None:
            self.draw_manager = DrawManager(
                self.canvas,
                editing_layer_getter=self._editing_layer_getter,
            )
            self.draw_manager.finished.connect(self.finish_draw)

        # 激活工具（与旧版 Draw.enable_draw_tool 行为一致：弹 QMessageBox 提示）
        cls = get_tool_class(geom_type)
        self.active_tool = cls(
            self.canvas,
            draw_manager=self.draw_manager,
        )
        self.canvas.setMapTool(self.active_tool)
        QMessageBox.information(
            self.main_window,
            "提示",
            f"已激活「添加{geom_type}」工具，点击画布绘制！",
        )

    def finish_draw(self, geometry: "QgsGeometry") -> None:
        """绘制完成：校验 + 创建要素 + 弹属性对话框（与旧版 finish_draw 行为一致）。"""
        if geometry is None:
            QMessageBox.warning(self.main_window, "警告", "绘制的几何图形无效！")
            self.clear_draw_tool()
            return

        layer = self._current_layer()
        if layer is None:
            self.clear_draw_tool()
            return

        # 几何类型与图层匹配校验
        layer_geom_type = layer.geometryType()
        draw_geom_type = geometry.type()
        geom_name = {0: "点", 1: "线", 2: "面"}.get(layer_geom_type, "未知")
        geom_name_d = {0: "点", 1: "线", 2: "面"}.get(draw_geom_type, "未知")
        if layer_geom_type != draw_geom_type:
            QMessageBox.warning(
                self.main_window,
                "警告",
                f"绘制类型与图层不匹配！\n图层类型：{geom_name}，绘制类型：{geom_name_d}",
            )
            self.clear_draw_tool()
            return

        # 创建要素 + 弹属性对话框
        from qgis.core import QgsVectorLayerUtils

        from app.ui.dialogs.custom_attr import CustomAttrDialog

        feature = QgsVectorLayerUtils.createFeature(layer, geometry)
        feature.setGeometry(geometry)
        dlg = CustomAttrDialog(layer, feature=feature, parent=self.main_window)
        dlg.setWindowTitle(f"编辑{geom_name}要素属性")
        dlg.show()

    def clear_draw_tool(self) -> None:
        """清掉激活工具 + 共享 rubber_band + 临时点（与旧版一致）。"""
        if self.active_tool is not None:
            try:
                self.canvas.unsetMapTool(self.active_tool)
            except Exception:  # noqa: BLE001
                pass
            self.active_tool = None
        if self.rubber_band is not None:
            try:
                self.rubber_band.reset()
            except Exception:  # noqa: BLE001
                pass
            self.rubber_band = None
        self.temp_points.clear()

    # ============================================================== 内部

    def _current_layer(self) -> "Optional[QgsVectorLayer]":
        return self._editing_layer_getter()

    def _set_edit_menu_enabled(self, enabled: bool, *, editing_now: bool = False) -> None:
        """编辑器菜单 3 个按钮的整体启用状态。

        :param enabled: False → 3 个全禁用（默认态）。
        :param editing_now: True → 处于编辑态：开始编辑禁用、停止/保存启用。
                            False → 仅指定了编辑图层：开始编辑启用、停止/保存禁用。
        """
        if not enabled:
            self.start_edit.setEnabled(False)
            self.stop_edit.setEnabled(False)
            self.save_edit.setEnabled(False)
            return
        self.start_edit.setEnabled(not editing_now)
        self.stop_edit.setEnabled(editing_now)
        self.save_edit.setEnabled(editing_now)

    def _set_add_buttons_for_geometry(self, g_type: int) -> None:
        """按 ``g_type`` 启用对应的"添加点/线/面"按钮；``g_type=-1`` 表示全禁用。"""
        self.add_point_action.setEnabled(g_type == 0)
        self.add_line_action.setEnabled(g_type == 1)
        self.add_polygon_action.setEnabled(g_type == 2)
