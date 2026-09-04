"""绘图 UI 装配（与旧 :class:`component.draw_tool.Draw` 行为一致）。

按旧版设计：

- 工具条平铺 9 个文本按钮（不用下拉菜单）：
  开始编辑 / 停止编辑 / 保存编辑 / 融合几何 / 要素编辑 / 线分割 ｜ 添加点 / 添加线 / 添加面
- 默认全部 disabled；外部 :meth:`set_edit_target` 启用"开始编辑"按钮。
- 几何类型按当前 layer 启用对应按钮（点 layer 只能点，线 layer 只能线）；
  「要素编辑」需处于编辑态（点击要素编辑其几何，见
  :class:`app.ui.tools.feature_edit.FeatureEditTool`）；「线分割」需处于编辑态
  （双击选要素后左键画切割线、右键完成分割，见
  :class:`app.ui.tools.split_feature.SplitFeatureTool`）。

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

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from qgis.core import QgsVectorLayer
    from qgis.gui import QgsMapCanvas

from qgis.core import (
    Qgis,
    QgsGeometry,
    QgsWkbTypes,
)
from qgis.gui import (
    QgsMapToolCapture,
    QgsMapToolEmitPoint,
    QgsRubberBand,
)
from qgis.PyQt.QtCore import QObject, Qt, pyqtSignal
from qgis.PyQt.QtGui import QAction, QColor
from qgis.PyQt.QtWidgets import (
    QMainWindow,
    QMessageBox,
    QToolBar,
)

from app.core.domain.drawing import DrawManager
from app.core.domain.drawing.tools import get_tool_class


class DigitizingToolbar(QObject):
    """绘制 / 编辑工具条（与旧 ``component.draw_tool.Draw`` 行为一致）。

    继承 :class:`QObject` 是为了挂 :pyattr:`editing_state_changed` 信号——
    :class:`pyqtSignal` 必须挂在 :class:`QObject` 派生类上才能 ``emit`` /
    ``connect``，否则会触发 ``TypeError: cannot be converted to QObject``。
    """

    #: 编辑状态切换时发出：True=进入编辑态，False=退出编辑态。
    editing_state_changed = pyqtSignal(bool)

    def __init__(
        self,
        main_window: QMainWindow,
        canvas: QgsMapCanvas,
        editing_layer_getter=None,
    ) -> None:
        # QObject.__init__ 必须显式调用（QObject 没有无参 __init__，会自动绑定 parent）
        QObject.__init__(self)
        self.main_window = main_window
        self.canvas = canvas
        # 编辑图层 getter：返回当前 ``edit_layer`` 或 None
        self._editing_layer_getter = editing_layer_getter or (lambda: None)
        # 业务层 DrawManager（懒创建）
        self.draw_manager: DrawManager | None = None
        # 临时点 + 共享 rubber_band（与旧 Draw 一致）
        self.temp_points: list = []
        self.rubber_band = None
        self.active_tool = None
        self.merge_tool = None
        self.feature_edit_tool = None
        self.split_tool = None

        # 当前是否处于编辑态（start_edit_fun 成功后 True，stop_edit_fun 后 False）
        self._editing = False

        self._build_toolbar()

    # ============================================================== 装配

    def _build_toolbar(self) -> None:
        self.digitizing_toolbar = QToolBar("编辑工具")
        self.digitizing_toolbar.setObjectName("DigitizingToolbar")
        # 工具直接平铺成文本按钮（不用下拉菜单），按编辑态/几何类型启用/禁用
        self.digitizing_toolbar.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextOnly)

        # ---- 编辑操作 ----
        self.start_edit = QAction("开始编辑", self.digitizing_toolbar)
        self.start_edit.triggered.connect(self.start_edit_fun)
        self.stop_edit = QAction("停止编辑", self.digitizing_toolbar)
        self.stop_edit.triggered.connect(self.stop_edit_fun)
        self.save_edit = QAction("保存编辑", self.digitizing_toolbar)
        # 旧逻辑：stop_edit 也绑了 commitChanges，但语义重叠，这里拆开
        self.save_edit.triggered.connect(self._save_edits)

        # ---- 融合几何（需处于编辑态）----
        self.merge_geometry_action = QAction("融合几何", self.digitizing_toolbar)
        self.merge_geometry_action.triggered.connect(self.enable_merge_tool)

        # ---- 要素编辑（需处于编辑态；点击要素编辑其几何）----
        self.feature_edit_action = QAction("要素编辑", self.digitizing_toolbar)
        self.feature_edit_action.triggered.connect(self.enable_feature_edit)

        # ---- 线分割（需处于编辑态；双击选要素 → 画切割线 → 右键分割）----
        self.split_feature_action = QAction("线分割", self.digitizing_toolbar)
        self.split_feature_action.triggered.connect(self.enable_split_tool)

        # 默认禁用（与旧版一致：edit_layer is None 时全 disable）
        self._set_edit_actions_enabled(False)

        self.digitizing_toolbar.addAction(self.start_edit)
        self.digitizing_toolbar.addAction(self.stop_edit)
        self.digitizing_toolbar.addAction(self.save_edit)
        self.digitizing_toolbar.addAction(self.merge_geometry_action)
        self.digitizing_toolbar.addAction(self.feature_edit_action)
        self.digitizing_toolbar.addAction(self.split_feature_action)

        # 分隔两组
        self.digitizing_toolbar.addSeparator()

        # ---- 添加要素 ----
        self.add_point_action = QAction("添加点", self.digitizing_toolbar)
        self.add_point_action.triggered.connect(lambda: self.enable_draw_tool("point"))
        self.add_line_action = QAction("添加线", self.digitizing_toolbar)
        self.add_line_action.triggered.connect(lambda: self.enable_draw_tool("line"))
        self.add_polygon_action = QAction("添加面", self.digitizing_toolbar)
        self.add_polygon_action.triggered.connect(lambda: self.enable_draw_tool("polygon"))

        # 默认禁用
        self.add_point_action.setEnabled(False)
        self.add_line_action.setEnabled(False)
        self.add_polygon_action.setEnabled(False)

        self.digitizing_toolbar.addAction(self.add_point_action)
        self.digitizing_toolbar.addAction(self.add_line_action)
        self.digitizing_toolbar.addAction(self.add_polygon_action)

        # 挂到主窗口
        self.main_window.addToolBar(self.digitizing_toolbar)

    # ============================================================== 公共 API

    def set_edit_target(self, layer: QgsVectorLayer | None = None) -> None:
        """外部调用：把 ``layer`` 设为"目标编辑图层"。

        仅打开"开始编辑"按钮（让用户能进入编辑态）；不直接修改 QGIS
        图层的 editing 状态——那是用户点"开始编辑"时才发生的。

        :param layer: 新目标图层；为 None 时按当前 getter 的值处理。
        """
        target = layer if layer is not None else self._current_layer()
        if target is None:
            self._set_edit_actions_enabled(False)
            self._set_add_buttons_for_geometry(-1)
            return
        if target.type() != target.VectorLayer:
            self._set_edit_actions_enabled(False)
            self._set_add_buttons_for_geometry(-1)
            return

        # 进入"已指定编辑图层"态：开始编辑可用，按图层几何类型启用"添加要素"
        self._set_edit_actions_enabled(True, editing_now=False)
        self._set_add_buttons_for_geometry(target.geometryType())

    def refresh_for_current_layer(self) -> None:
        """根据当前 ``edit_layer`` 重新刷一遍按钮状态（应对切图层等场景）。"""
        layer = self._current_layer()
        if layer is None or layer.type() != layer.VectorLayer:
            self._set_edit_actions_enabled(False)
            self._set_add_buttons_for_geometry(-1)
            return
        if layer.isEditable():
            # 当前已经在编辑态，保持按钮可用状态
            self._set_edit_actions_enabled(True, editing_now=True)
            self._set_add_buttons_for_geometry(layer.geometryType())
        else:
            self._set_edit_actions_enabled(True, editing_now=False)
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
                self._set_edit_actions_enabled(True, editing_now=True)
                self._set_add_buttons_for_geometry(layer.geometryType())
            return

        ok = layer.startEditing()
        if not ok:
            QMessageBox.warning(self.main_window, "警告", "无法开始编辑！")
            return

        self._editing = True
        self._set_edit_actions_enabled(True, editing_now=True)
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
        self._set_edit_actions_enabled(True, editing_now=False)
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

    def enable_merge_tool(self) -> None:
        """激活融合几何工具：右键选要素，左键双击融合。"""
        layer = self._current_layer()
        if layer is None or layer.type() != layer.VectorLayer:
            QMessageBox.warning(self.main_window, "警告", "请先选择矢量图层")
            return
        if not layer.isEditable():
            QMessageBox.warning(self.main_window, "警告", "请先开始编辑！")
            return
        # 清掉可能激活的绘制 / 融合工具，避免两个工具并存
        self.clear_draw_tool()

        from app.ui.tools.merge_geometry import MergeGeometryTool

        self.merge_tool = MergeGeometryTool(
            self.canvas,
            editing_layer_getter=self._editing_layer_getter,
            hint=lambda text: self.main_window.messageBar.pushMessage(
                text, level=Qgis.MessageLevel.Info, duration=2
            ),
        )
        self.canvas.setMapTool(self.merge_tool)
        QMessageBox.information(
            self.main_window,
            "提示",
            "已激活「融合几何」工具：右键选择要素，双击融合；Esc 退出",
        )

    def enable_feature_edit(self) -> None:
        """激活「要素编辑」工具：点击矢量要素编辑其几何（顶点级）。"""
        layer = self._current_layer()
        if layer is None or layer.type() != layer.VectorLayer:
            QMessageBox.warning(self.main_window, "警告", "请先选择矢量图层")
            return
        if not layer.isEditable():
            QMessageBox.warning(self.main_window, "警告", "请先开始编辑！")
            return
        # 清掉可能激活的绘制 / 融合工具，避免两个工具并存
        self.clear_draw_tool()

        from app.ui.tools.feature_edit import FeatureEditTool

        self.feature_edit_tool = FeatureEditTool(
            self.canvas,
            editing_layer_getter=self._editing_layer_getter,
            hint=lambda text: self.main_window.messageBar.pushMessage(
                text, level=Qgis.MessageLevel.Info, duration=2
            ),
        )
        self.canvas.setMapTool(self.feature_edit_tool)
        QMessageBox.information(
            self.main_window,
            "提示",
            "已激活「要素编辑」工具：点击要素进入几何编辑（拖动顶点 / 双击边加顶点 / 右键删顶点）；Esc 退出",
        )

    def enable_split_tool(self) -> None:
        """激活「线分割」工具：先左键双击选中要素，再画切割线右键分割。"""
        layer = self._current_layer()
        if layer is None or layer.type() != layer.VectorLayer:
            QMessageBox.warning(self.main_window, "警告", "请先选择矢量图层")
            return
        if not layer.isEditable():
            QMessageBox.warning(self.main_window, "警告", "请先开始编辑！")
            return
        if layer.geometryType() == QgsWkbTypes.PointGeometry:
            QMessageBox.warning(self.main_window, "警告", "点图层无法分割，请选择线 / 面图层")
            return
        # 清掉可能激活的绘制 / 融合 / 要素编辑工具，避免两个工具并存
        self.clear_draw_tool()

        from app.ui.tools.split_feature import SplitFeatureTool

        self.split_tool = SplitFeatureTool(
            self.canvas,
            editing_layer_getter=self._editing_layer_getter,
            hint=lambda text: self.main_window.messageBar.pushMessage(
                text, level=Qgis.MessageLevel.Info, duration=2
            ),
        )
        self.canvas.setMapTool(self.split_tool)
        QMessageBox.information(
            self.main_window,
            "提示",
            "已激活「线分割」工具：左键双击要素选中后，按住左键画切割线、右键完成分割；Esc 取消 / 退出",
        )

    def finish_draw(self, geometry: QgsGeometry) -> None:
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
        if getattr(self, "merge_tool", None) is not None:
            try:
                self.canvas.unsetMapTool(self.merge_tool)
            except Exception:
                pass
            self.merge_tool = None
        if getattr(self, "feature_edit_tool", None) is not None:
            try:
                self.canvas.unsetMapTool(self.feature_edit_tool)
            except Exception:
                pass
            self.feature_edit_tool = None
        if getattr(self, "split_tool", None) is not None:
            try:
                self.canvas.unsetMapTool(self.split_tool)
            except Exception:
                pass
            self.split_tool = None
        if self.active_tool is not None:
            try:
                self.canvas.unsetMapTool(self.active_tool)
            except Exception:
                pass
            self.active_tool = None
        if self.rubber_band is not None:
            try:
                self.rubber_band.reset()
            except Exception:
                pass
            self.rubber_band = None
        self.temp_points.clear()

    # ============================================================== 内部

    def _current_layer(self) -> QgsVectorLayer | None:
        return self._editing_layer_getter()

    def _set_edit_actions_enabled(self, enabled: bool, *, editing_now: bool = False) -> None:
        """编辑器 3 个按钮（开始/停止/保存）的整体启用状态。

        :param enabled: False → 3 个全禁用（默认态）。
        :param editing_now: True → 处于编辑态：开始编辑禁用、停止/保存启用。
                            False → 仅指定了编辑图层：开始编辑启用、停止/保存禁用。
        """
        if not enabled:
            self.start_edit.setEnabled(False)
            self.stop_edit.setEnabled(False)
            self.save_edit.setEnabled(False)
            self.merge_geometry_action.setEnabled(False)
            self.feature_edit_action.setEnabled(False)
            self.split_feature_action.setEnabled(False)
            return
        self.start_edit.setEnabled(not editing_now)
        self.stop_edit.setEnabled(editing_now)
        self.save_edit.setEnabled(editing_now)
        # 融合几何 / 要素编辑 / 线分割 要求编辑态：编辑中才可用
        self.merge_geometry_action.setEnabled(editing_now)
        self.feature_edit_action.setEnabled(editing_now)
        self.split_feature_action.setEnabled(editing_now)

    def _set_add_buttons_for_geometry(self, g_type: int) -> None:
        """按 ``g_type`` 启用对应的"添加点/线/面"按钮；``g_type=-1`` 表示全禁用。"""
        self.add_point_action.setEnabled(g_type == 0)
        self.add_line_action.setEnabled(g_type == 1)
        self.add_polygon_action.setEnabled(g_type == 2)


class QgisCaptureSelfTestToolbar(QObject):
    """QGIS GUI 自检工具条 — 直接调用 :class:`qgis.gui.QgsMapToolCapture`。

    与 :class:`DigitizingToolbar`（自定义绘制）/ :class:`QgisDigitizingToolbar`
    （封装后的 QGIS 版）并行的第三根工具条，定位为 **smoke test**：

    - 不需要编辑图层、不创建要素、不进 buffer
    - 激活对应 QGIS 内置 capture 工具后，点击画布触发原生信号
      （:attr:`QgsMapToolEmitPoint.canvasClicked` /
      :attr:`QgsMapToolCapture.digitizingFinished`）
    - 把捕获到的几何信息（类型 / 顶点数 / bbox）打到 :class:`QgisMessageBar`
      — 用来验证 QGIS GUI 的 capture / rubber band / 节点捕捉等链路是否通畅

    按钮：

    - 「自检-点」: :class:`QgsMapToolEmitPoint`（单击取点，无 capture 流程）
    - 「自检-线」: :class:`QgsMapToolCapture` (:attr:`CaptureLine`)
    - 「自检-面」: :class:`QgsMapToolCapture` (:attr:`CapturePolygon`)

    与现有两个工具条的区别：**不**参与 :attr:`DigitizingToolbar.editing_state_changed`
    联动，永远可点 — 仅为验证 QGIS GUI 集成而存在，调试 / 自检用。

    状态机：3 个按钮相互独立互斥（激活新的之前先卸掉旧的），避免两个
    capture 工具并存导致信号路由混乱。
    """

    def __init__(self, main_window: QMainWindow, canvas: QgsMapCanvas) -> None:
        # QObject.__init__ 必须显式调用（同 DigitizingToolbar，理由参见其注释）
        QObject.__init__(self)
        self.main_window = main_window
        self.canvas = canvas
        # 每种模式持有一个工具实例（避免重激活反复 new + 重接信号）
        self._capture_tools: dict[str, object] = {}
        self._active_tool = None

        self._build_toolbar()

    # ============================================================== 装配

    def _build_toolbar(self) -> None:
        self.capture_toolbar = QToolBar("QGIS 自检-捕捉")
        self.capture_toolbar.setObjectName("QgisCaptureSelfTestToolbar")
        # 与 DigitizingToolbar 风格保持一致：纯文本按钮
        self.capture_toolbar.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextOnly)

        for label, mode in (
            ("自检-点", "point"),
            ("自检-线", "line"),
            ("自检-面", "polygon"),
        ):
            act = QAction(label, self.capture_toolbar)
            act.triggered.connect(lambda _checked=False, m=mode: self._activate(m))
            self.capture_toolbar.addAction(act)

        # 挂到主窗口。**注意**：本类未在 MainWindow._tab_toolbars 里注册，
        # 工具条会常驻显示。是否进 tab 由 MainWindow 决定（自检 tab 或隐藏），
        # 类本身只负责「建好就挂上去」，与 DigitizingToolbar 装配约定一致。
        self.main_window.addToolBar(self.capture_toolbar)

    # ============================================================== 激活/卸载

    def _activate(self, mode: str) -> None:
        """激活指定 capture 工具；先卸掉旧的，避免两个工具并存。"""
        self._deactivate()

        tool = self._capture_tools.get(mode)
        if tool is None:
            if mode == "point":
                tool = QgsMapToolEmitPoint(self.canvas)
                tool.canvasClicked.connect(self._on_point_clicked)
            else:
                capture_mode = {
                    "line": QgsMapToolCapture.CaptureLine,
                    "polygon": QgsMapToolCapture.CapturePolygon,
                }[mode]
                tool = QgsMapToolCapture(self.canvas, capture_mode)
                tool.digitizingFinished.connect(self._on_digitizing_finished)
            self._capture_tools[mode] = tool

        self._active_tool = tool
        self.canvas.setMapTool(tool)
        self.main_window.messageBar.pushMessage(
            self._hint_for_mode(mode),
            level=Qgis.MessageLevel.Info,
            duration=2,
        )

    def _deactivate(self) -> None:
        """卸掉当前激活的 capture 工具（不抛异常，工具可能已被画布自动换掉）。"""
        if self._active_tool is None:
            return
        try:
            self.canvas.unsetMapTool(self._active_tool)
        except Exception:  # noqa: BLE001 — 同 DigitizingToolbar.clear_draw_tool 容错
            pass
        self._active_tool = None

    # ============================================================== 信号回调

    def _on_digitizing_finished(self, geometry) -> None:  # noqa: ANN001
        """QgsMapToolCapture 完成（右键 / 双击结束）→ 报告几何 → 卸工具。"""
        self._report_geometry(geometry)
        self._deactivate()

    def _on_point_clicked(self, point, button) -> None:  # noqa: ANN001
        """QgsMapToolEmitPoint 单击取点（只接左键，与现有 point 工具一致）。"""
        if button != Qt.MouseButton.LeftButton:
            return
        geometry = QgsGeometry.fromPointXY(point)
        self._report_geometry(geometry)
        # 与 line / polygon 一致：点一次结束（用户再点按钮才能再次激活）
        self._deactivate()

    # ============================================================== 内部

    def _hint_for_mode(self, mode: str) -> str:
        return {
            "point": "已激活「自检-点」：单击画布取点；完成后工具自动退出",
            "line": "已激活「自检-线」：左键加点，右键 / 双击结束，Esc 取消",
            "polygon": "已激活「自检-面」：左键加点，最后一点右键 / 双击自动闭合，Esc 取消",
        }[mode]

    def _report_geometry(self, geometry: QgsGeometry) -> None:
        """把捕获到的几何摘要推到 messageBar（自检报告，不落库）。"""
        if geometry is None or geometry.isEmpty():
            self.main_window.messageBar.pushMessage(
                "QGIS 自检-捕捉：未捕获到几何",
                level=Qgis.MessageLevel.Warning,
                duration=2,
            )
            return
        geom_type = geometry.type()  # 0=Point 1=Line 2=Polygon
        type_name = {0: "点", 1: "线", 2: "面"}.get(geom_type, f"未知({geom_type})")
        if geom_type == 0:
            count_part = "顶点数=1"
        elif geom_type == 1:
            count_part = f"顶点数={len(geometry.asPolyline())}"
        else:
            # 面可能含多个环（外环 + 内环）
            rings = geometry.asPolygon()
            total = sum(len(r) for r in rings)
            count_part = f"环数={len(rings)}（总顶点数={total}）"
        bb = geometry.boundingBox()
        bbox_part = (
            f"bbox=({bb.xMinimum():.4f},{bb.yMinimum():.4f},"
            f"{bb.xMaximum():.4f},{bb.yMaximum():.4f})"
        )
        self.main_window.messageBar.pushMessage(
            f"QGIS 自检-捕捉 ✓ 类型={type_name}；{count_part}；{bbox_part}",
            level=Qgis.MessageLevel.Success,
            duration=5,
        )
