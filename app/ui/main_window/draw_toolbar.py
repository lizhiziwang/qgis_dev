"""绘图 UI 装配（与旧 :class:`component.draw_tool.Draw` 行为一致）。

按旧版设计：

- 工具条平铺 10 个文本按钮（不用下拉菜单）：
  开始编辑 / 停止编辑 / 保存编辑 / 融合几何 / 要素编辑 / 属性录入 / 线分割 ｜
  添加点 / 添加线 / 添加面
- 「开始编辑」**恒可用**：点击弹出图层选择框（:attr:`_edit_layer_chooser`，
  由 :class:`MainWindow` 注入），选中图层即成为编辑目标并进入编辑态——
  不再依赖图层树右键 / 点击预设编辑图层（那些入口已移除）。
- 几何类型按当前 layer 启用对应按钮（点 layer 只能点，线 layer 只能线）；
  「要素编辑」需处于编辑态（点击要素编辑其几何，见
  :class:`app.ui.tools.feature_edit.FeatureEditTool`）；「属性录入」**不需**
  先开编辑——激活时若目标图层未编辑会自动进入编辑态（点击要素 → 右侧
  Dock 编辑属性后确认，见
  :class:`app.ui.docks.attribute_entry_dock.AttributeEntryDock`）；
  「线分割」需处于编辑态
  （双击选要素后左键画切割线、右键完成分割，见
  :class:`app.ui.tools.split_feature.SplitFeatureTool`）。

状态机：

::

    [未编辑] --start_edit_fun(弹窗选层)--> [编辑中]
        ^                                        |
        +----------------stop_edit_fun-----------+
        编辑中再点「开始编辑」→ 重新弹窗换层，继续 [编辑中]

:attr:`editing_state_changed` 信号在状态切换时发出，供 :class:`MainWindow`
同步磁性捕捉等其它工具条。
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
from qgis.gui import QgsRubberBand
from qgis.PyQt.QtCore import QObject, Qt, pyqtSignal
from qgis.PyQt.QtGui import QAction, QColor
from qgis.PyQt.QtWidgets import (
    QMainWindow,
    QMessageBox,
    QToolBar,
)

from app.core.domain.drawing import DrawManager
from app.core.domain.drawing.tools import get_tool_class
from app.infra.logging import get_logger

logger = get_logger(__name__)


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
        edit_layer_chooser=None,
    ) -> None:
        # QObject.__init__ 必须显式调用（QObject 没有无参 __init__，会自动绑定 parent）
        QObject.__init__(self)
        self.main_window = main_window
        self.canvas = canvas
        # 编辑图层 getter：返回当前 ``edit_layer`` 或 None
        self._editing_layer_getter = editing_layer_getter or (lambda: None)
        #: 选编辑图层回调（MainWindow 注入）：点「开始编辑」先弹窗让用户选目标
        #: 图层，返回选中的图层或 None（取消）。仅负责选择——把选中层落地为
        #: 编辑目标（edit_layer / 停旧层 / 工具条联动）由 :meth:`start_edit_fun`
        #: 调用 main_window 的 ``_apply_edit_target`` 完成。为 None 时回退到当前
        #: getter 的值（老行为，供测试 / 直连使用）。
        self._edit_layer_chooser = edit_layer_chooser
        # 业务层 DrawManager（懒创建）
        self.draw_manager: DrawManager | None = None
        # 临时点 + 共享 rubber_band（与旧 Draw 一致）
        self.temp_points: list = []
        self.rubber_band = None
        self.active_tool = None
        #: 激活本工具条的地图工具前、画布正在用的工具；clear 后恢复（QGIS 的
        #: unsetMapTool 不会自动回退前一个工具，不记录的话停止编辑后画布会
        #: 变成“无任何工具”，点画布无反应）。
        self._previous_map_tool = None
        self.merge_tool = None
        self.feature_edit_tool = None
        self.split_tool = None
        #: 属性录入工具实例（防 GC）与右侧 Dock（懒建单实例）
        self.attribute_entry_tool = None
        self._attribute_entry_dock = None

        # 当前是否处于编辑态（start_edit_fun 成功后 True，stop_edit_fun 后 False）
        self._editing = False
        #: 当前激活的工具种类："point"/"line"/"polygon"/"merge"/"feature"/
        #: "split"/"attr_entry"，None=未激活。工具按钮的勾选/互斥、日志都
        #: 以它为唯一依据（_refresh_button_states 统一推导，不再散点 setChecked）
        self._active_kind: str | None = None
        #: 挂了 ``editingStopped`` 兜底钩子的图层：外部入口（属性表 Dock「停止
        #: 编辑」/ 切图层停旧层 / 关工程等）结束编辑时不走 :meth:`stop_edit_fun`，
        #: 靠本钩子统一清工具并复位状态（见 :meth:`_on_editing_stopped`）。
        self._edit_stop_hook_layer = None

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
        self.merge_geometry_action.setCheckable(True)
        # checkable 动作的 triggered 会带勾选态：True=激活，False=再点一次取消激活
        self.merge_geometry_action.triggered.connect(
            lambda checked=False: self.enable_merge_tool(checked)
        )

        # ---- 要素编辑（需处于编辑态；点击要素编辑其几何）----
        self.feature_edit_action = QAction("要素编辑", self.digitizing_toolbar)
        self.feature_edit_action.setCheckable(True)
        self.feature_edit_action.triggered.connect(
            lambda checked=False: self.enable_feature_edit(checked)
        )

        # ---- 属性录入（恒可用：无需编辑态，激活时自动开始编辑）----
        #     点击要素 → 右侧 Dock 改属性
        self.attribute_entry_action = QAction("属性录入", self.digitizing_toolbar)
        self.attribute_entry_action.setCheckable(True)
        self.attribute_entry_action.triggered.connect(
            lambda checked=False: self.enable_attribute_entry(checked)
        )

        # ---- 线分割（需处于编辑态；双击选要素 → 画切割线 → 右键分割）----
        self.split_feature_action = QAction("线分割", self.digitizing_toolbar)
        self.split_feature_action.setCheckable(True)
        self.split_feature_action.triggered.connect(
            lambda checked=False: self.enable_split_tool(checked)
        )

        # ---- 初始态：编辑动作全禁用（「开始编辑」例外——恒可用，点击弹窗选层）
        #     最后统一由 _refresh_button_states 推导，这里只组装不动状态

        self.digitizing_toolbar.addAction(self.start_edit)
        self.digitizing_toolbar.addAction(self.stop_edit)
        self.digitizing_toolbar.addAction(self.save_edit)
        self.digitizing_toolbar.addAction(self.merge_geometry_action)
        self.digitizing_toolbar.addAction(self.feature_edit_action)
        self.digitizing_toolbar.addAction(self.attribute_entry_action)
        self.digitizing_toolbar.addAction(self.split_feature_action)

        # 分隔两组
        self.digitizing_toolbar.addSeparator()

        # ---- 添加要素 ----
        self.add_point_action = QAction("添加点", self.digitizing_toolbar)
        self.add_point_action.setCheckable(True)
        self.add_point_action.triggered.connect(
            lambda checked=False: self.enable_draw_tool("point", checked)
        )
        self.add_line_action = QAction("添加线", self.digitizing_toolbar)
        self.add_line_action.setCheckable(True)
        self.add_line_action.triggered.connect(
            lambda checked=False: self.enable_draw_tool("line", checked)
        )
        self.add_polygon_action = QAction("添加面", self.digitizing_toolbar)
        self.add_polygon_action.setCheckable(True)
        self.add_polygon_action.triggered.connect(
            lambda checked=False: self.enable_draw_tool("polygon", checked)
        )

        # 工具按钮 ↔ 激活状态标识：勾选由 _refresh_button_states 统一推导
        # （同一时刻只有一个激活工具按钮处于按下态）
        self._tool_action_map = {
            "point": self.add_point_action,
            "line": self.add_line_action,
            "polygon": self.add_polygon_action,
            "merge": self.merge_geometry_action,
            "feature": self.feature_edit_action,
            "split": self.split_feature_action,
            "attr_entry": self.attribute_entry_action,
        }

        self.digitizing_toolbar.addAction(self.add_point_action)
        self.digitizing_toolbar.addAction(self.add_line_action)
        self.digitizing_toolbar.addAction(self.add_polygon_action)

        # 初始态由统一出口推导（无目标图层 → 仅「开始编辑」/「属性录入」可用）
        self._refresh_button_states()

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
            self._disconnect_edit_stop_hook()
            self._refresh_button_states()
            return
        if target.type() != target.VectorLayer:
            self._disconnect_edit_stop_hook()
            self._refresh_button_states()
            return

        # 进入"已指定编辑图层"态：按钮可用性统一由 _refresh_button_states 按图层
        # 是否已进入编辑态推导(编辑中 → 停止/保存/融合等 + 对应几何的"添加X"可用)
        # 任意入口（含属性表 Dock 等外部服务）结束本图层编辑时都能兜底清理
        self._connect_edit_stop_hook(target)
        self._refresh_button_states()

    def refresh_for_current_layer(self) -> None:
        """根据当前 ``edit_layer`` 重新刷一遍按钮状态（应对切图层等场景）。"""
        layer = self._current_layer()
        if layer is None or layer.type() != layer.VectorLayer:
            self._disconnect_edit_stop_hook()
        else:
            # 目标图层变化时把停编钩子挪到新图层（幂等）
            self._connect_edit_stop_hook(layer)
        self._refresh_button_states()

    # ============================================================== 编辑器菜单回调

    def start_edit_fun(self) -> None:
        """开始编辑：先弹窗选择目标图层（若有 chooser），再真正
        ``layer.startEditing()`` 并切换按钮状态。
        """
        layer = self._current_layer()
        chooser = self._edit_layer_chooser
        if chooser is not None:
            # 弹窗选层（纯选择，见 main_window._choose_edit_layer）；取消返回
            # None → 不做任何事
            layer = chooser()
            if layer is None:
                return
            # 落地为编辑目标：edit_layer / 停旧层 / 工具条 / 捕捉联动。
            # 若 chooser 本身已同步过（getter 已指向选中层）则跳过，幂等安全。
            if self._current_layer() is not layer:
                applier = getattr(self.main_window, "_apply_edit_target", None)
                if callable(applier):
                    applier(layer)
        if layer is None or layer.type() != layer.VectorLayer:
            return
        if not self._ensure_editing(layer):
            return

    def _ensure_editing(self, layer: QgsVectorLayer) -> bool:
        """确保 ``layer`` 进入编辑态并同步工具条状态机。

        - 已在编辑态：若状态机未同步（第三方/其它入口先开编辑）则补齐同步；
        - 未编辑：调用 ``layer.startEditing()``，成功后同步按钮组并发出
          :pyattr:`editing_state_changed`。

        供「开始编辑」「属性录入」（自动开编辑）共用。

        :returns: 是否处于（已进入）编辑态。
        """
        # 挂停编钩子：本工具条开始的编辑，不管之后由谁结束都能兜底清理工具
        self._connect_edit_stop_hook(layer)
        if layer.isEditable():
            if not self._editing:
                self._editing = True
                self._refresh_button_states()
            return True

        ok = layer.startEditing()
        if not ok:
            QMessageBox.warning(self.main_window, "警告", "无法开始编辑！")
            return False
        self._editing = True
        self._refresh_button_states()
        self.editing_state_changed.emit(True)
        return True

    def stop_edit_fun(self) -> None:
        """停止编辑：先取消当前激活的绘制/编辑工具，再弹「是否保存」对话框。

        无论用户之后选「是 / 否」，编辑一旦停止都不该让画布继续挂着一个激活的
        绘制工具，工具条也不该停留在「编辑中」可用态——所以：
         1. **先** ``clear_draw_tool()`` 取消激活工具（旧实现放在最后，中途异常 /
            状态残留会让工具继续挂活）；
         2. 会话是否结束以**图层真实编辑态**为准，复位放在 ``finally``——提交 /
            回滚真正结束编辑才切回「已指定编辑图层」态（编辑按钮禁用）。

        「是」路径必须检查 ``commitChanges`` 的返回值：保存失败（返回 False /
        抛异常，如数据源约束 / 锁等）时图层**仍处于编辑态**，不能假装已停止——
        否则会复现「点『是』后编辑按钮仍可用但 ``_editing`` 已置 False」的状态机
        脱节（按钮可用性由 ``layer.isEditable()`` 推导，会话没结束就一直是可用）。
        此时弹窗告知失败原因并保持「编辑中」状态一致，用户可修正后重试，或再次
        点「停止编辑」选「否」放弃。
        """
        layer = self._current_layer()
        logger.info(
            "停止编辑入口: layer=%s editable=%s active_tool=%s",
            getattr(layer, "name", lambda: None)(),
            layer.isEditable() if layer is not None else None,
            self.active_tool,
        )
        # 先取消激活工具：不管有没有画完，停止编辑前工具都要退出
        self.clear_draw_tool()
        if layer is None or not layer.isEditable():
            # 没有在编辑：兜底清掉可能残留的激活工具/橡皮筋，保持干净
            self._exit_editing_state(layer)
            return

        try:
            if layer.editBuffer() is not None:
                reply = QMessageBox.warning(
                    self.main_window,
                    "警告",
                    "是否保存编辑的内容！",
                    QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                    QMessageBox.StandardButton.Yes,
                )
                if reply == QMessageBox.StandardButton.Yes:
                    try:
                        saved = layer.commitChanges(stopEditing=True)
                    except Exception:  # noqa: BLE001
                        # 某些数据源提交时会抛异常（库锁等）；按「保存失败」处理，
                        # 会话是否仍在编辑由 finally 按 isEditable 兜底判断
                        logger.exception("停止编辑提交异常: layer=%s", layer.name())
                        saved = False
                    logger.info("停止编辑提交结果: layer=%s saved=%s", layer.name(), saved)
                    if not saved:
                        # 保存失败：图层仍在编辑态，不能假装已退出。明确提示原因，
                        # 保持「编辑中」状态一致（编辑按钮仍可用是正确行为）
                        QMessageBox.warning(
                            self.main_window,
                            "保存失败",
                            "保存编辑失败，更改尚未写入数据源，仍处于编辑状态！\n"
                            "请修正问题后重新保存，或再次点击「停止编辑」选择「否」放弃更改。\n\n"
                            f"失败原因：\n{self._commit_error_text(layer)}",
                        )
                else:
                    # 否 → 放弃改动并结束编辑。rollBack() 本身就会结束编辑会话
                    # （QGIS 4.2 实测 isEditable 立即变 False），不必再补一次
                    # commitChanges(stopEditing=True)。
                    layer.rollBack()
        finally:
            if layer is not None and layer.isEditable():
                # 提交失败 / 异常（或回滚失败）→ 会话仍在：保持「编辑中」状态机
                # 一致（按钮可用、不误发退出信号），只刷新一次按钮
                self._refresh_button_states()
            else:
                # 提交 / 回滚成功真正结束编辑：切回「已指定编辑图层」态并清工具残留
                self._exit_editing_state(layer)

    def _commit_error_text(self, layer: QgsVectorLayer) -> str:
        """把图层最近一次 commitChanges 的错误整理成可读文本。

        commitErrors() 拿不到（异常路径 / 空列表）时给通用兜底提示，避免弹窗空白。
        """
        try:
            errors = layer.commitErrors()
        except Exception:  # noqa: BLE001
            logger.exception("读取提交错误失败: layer=%s", layer.name())
            errors = []
        if errors:
            return "\n".join(f"- {e}" for e in errors)
        return "- 未知错误（详见日志）"

    def _exit_editing_state(self, layer: QgsVectorLayer | None) -> None:
        """离开编辑态后的统一状态复位（停编辑 / 外部停编兜底共用）。

        - 编辑标志置 False；仅在确实从「编辑中」切走时发
          :pyattr:`editing_state_changed`(False)（外部停编兜底
          :meth:`_on_editing_stopped` 在 commitChanges/rollBack 同步触发时
          可能已先复位，避免重复发信号）；
        - 工具全部解除，按钮状态统一由 :meth:`_refresh_button_states` 按
          「是否仍在编辑」推导：离开编辑态后「停止/保存/融合/要素编辑/线分割」
          与「添加要素」全禁用并取消勾选(需重新「开始编辑」才恢复可用)。
        """
        was_editing = self._editing
        logger.debug(
            "离开编辑态复位: layer=%s was_editing=%s active_tool=%s",
            getattr(layer, "name", lambda: None)(),
            was_editing,
            self.active_tool,
        )
        # 工具解除 → 编辑标志复位 → 统一刷新按钮（顺序：clear 内部已刷新一次，
        # 这里在 _editing 复位后再刷，保证退出编辑态时按钮一定是禁用+未勾选)
        self.clear_draw_tool()
        self._editing = False
        self._refresh_button_states()
        # 只在确实从「编辑中」切走时发信号：停编路径里 :meth:`_on_editing_stopped`
        # 兜底可能已先复位过（commitChanges/rollBack 同步触发 editingStopped），
        # 避免重复发 False 造成二次状态切换
        if was_editing:
            self.editing_state_changed.emit(False)

    def _save_edits(self) -> None:
        """仅保存编辑，不停止。"""
        layer = self._current_layer()
        if layer is not None and layer.isEditable():
            layer.commitChanges(stopEditing=False)

    # ============================================================== 添加要素菜单回调

    def enable_draw_tool(self, geom_type: str, checked: bool = True) -> None:
        """激活指定类型的绘制工具（点/线/面）。

        ``checked`` 来自 checkable 按钮的 triggered 勾选态：

        - ``False``(用户再点一次已按下的「添加X」按钮) → 取消激活：清工具、
          取消勾选，画布恢复(第二次点击就是"关闭工具"，不再重复激活)；
        - ``True`` → 激活。未处于编辑态时不允许激活(弹「请先开始编辑！」)，
          避免停编辑后点按钮又在非可编辑图层上画出红点。
        """
        if not checked:
            # Qt 已把按钮取消勾选；这里清掉画布上对应的工具
            logger.info("按钮取消激活绘制工具: %s", geom_type)
            self.clear_draw_tool()
            return
        # 只有处于编辑态才能画要素(与融合/要素编辑等工具一致)；停编辑后按钮
        # 已禁用，这里兜底挡住直接调用
        layer = self._current_layer()
        if layer is None or layer.type() != layer.VectorLayer or not layer.isEditable():
            # 拒绝激活：Qt 已把按钮勾上，按统一状态刷新弹掉勾选
            self._refresh_button_states()
            QMessageBox.warning(self.main_window, "警告", "请先开始编辑！")
            return
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
        self._activate_map_tool(self.active_tool)
        self._mark_active(geom_type)
        QMessageBox.information(
            self.main_window,
            "提示",
            f"已激活「添加{geom_type}」工具，点击画布绘制！",
        )

    def enable_merge_tool(self, checked: bool = True) -> None:
        """激活融合几何工具：右键选要素，左键双击融合。

        ``checked=False``(再点一次已按下的按钮) → 取消激活。
        """
        if not checked:
            self.clear_draw_tool()
            return
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
        self._activate_map_tool(self.merge_tool)
        self._mark_active("merge")
        QMessageBox.information(
            self.main_window,
            "提示",
            "已激活「融合几何」工具：右键选择要素，双击融合；Esc 退出",
        )

    def enable_feature_edit(self, checked: bool = True) -> None:
        """激活「要素编辑」工具：点击矢量要素编辑其几何（顶点级）。

        ``checked=False``(再点一次已按下的按钮) → 取消激活。
        """
        if not checked:
            self.clear_draw_tool()
            return
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
        self._activate_map_tool(self.feature_edit_tool)
        self._mark_active("feature")
        QMessageBox.information(
            self.main_window,
            "提示",
            "已激活「要素编辑」工具：点击要素进入几何编辑（拖动顶点 / 双击边加顶点 / 右键删顶点）；Esc 退出",
        )

    def enable_split_tool(self, checked: bool = True) -> None:
        """激活「线分割」工具：先左键双击选中要素，再画切割线右键分割。

        ``checked=False``(再点一次已按下的按钮) → 取消激活。
        """
        if not checked:
            self.clear_draw_tool()
            return
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
        self._activate_map_tool(self.split_tool)
        self._mark_active("split")
        QMessageBox.information(
            self.main_window,
            "提示",
            "已激活「线分割」工具：左键双击要素选中后，按住左键画切割线、右键完成分割；Esc 取消 / 退出",
        )

    def enable_attribute_entry(self, checked: bool = True) -> None:
        """激活「属性录入」工具：点击要素 → 右侧 Dock 编辑其属性。

        不需要预先「开始编辑」：目标图层若尚未进入编辑态，这里自动开启
        （与「开始编辑」同一条状态机：``startEditing`` + 按钮组同步 + 发
        :pyattr:`editing_state_changed`）。无编辑目标时先弹图层选择框。

        ``checked=False``(再点一次已按下的按钮) → 取消激活。
        """
        if not checked:
            self.clear_draw_tool()
            return
        layer = self._current_layer()
        chooser = self._edit_layer_chooser
        if layer is None and chooser is not None:
            # 弹窗选层（纯选择；取消返回 None → 不做任何事）
            layer = chooser()
            if layer is None:
                # 取消选层：Qt 已把按钮勾上，按统一状态刷新弹掉
                self._refresh_button_states()
                return
            # 落地为编辑目标：edit_layer / 停旧层 / 工具条 / 捕捉联动
            if self._current_layer() is not layer:
                applier = getattr(self.main_window, "_apply_edit_target", None)
                if callable(applier):
                    applier(layer)
        if layer is None or layer.type() != layer.VectorLayer:
            self._refresh_button_states()
            QMessageBox.warning(self.main_window, "警告", "请先选择矢量图层")
            return
        # 属性录入不需要用户先「开始编辑」：自动进入编辑态
        if not self._ensure_editing(layer):
            self._refresh_button_states()
            return
        # 清掉可能激活的绘制 / 融合 / 要素编辑 / 分割工具，避免并存
        self.clear_draw_tool()

        from app.ui.docks.attribute_entry_dock import AttributeEntryDock
        from app.ui.tools.attribute_entry import AttributeEntryTool

        # 懒建右侧 Dock（单实例；关闭后再次激活时重新显示）
        dock = self._attribute_entry_dock
        if dock is None:
            dock = AttributeEntryDock(self.main_window)
            self.main_window.addDockWidget(Qt.DockWidgetArea.RightDockWidgetArea, dock)
            self._attribute_entry_dock = dock

        self.attribute_entry_tool = AttributeEntryTool(
            self.canvas,
            editing_layer_getter=self._editing_layer_getter,
            on_feature=dock.load_feature,
            hint=lambda text: self.main_window.messageBar.pushMessage(
                text, level=Qgis.MessageLevel.Info, duration=2
            ),
        )
        self._activate_map_tool(self.attribute_entry_tool)
        self._mark_active("attr_entry")
        dock.show()
        dock.raise_()
        QMessageBox.information(
            self.main_window,
            "提示",
            "已激活「属性录入」工具：点击图层上的要素，在右侧面板修改其属性；确认后保存，Esc 退出",
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
        """取消激活本工具条挂到画布的工具，并清掉共享 rubber_band + 临时点。

        保证「停止编辑 / 切换工具 / 取消」后画布不再挂着任何本工具条的激活
        工具（``active_tool`` / 融合 / 要素编辑 / 属性录入 / 线分割）：

        - 对带 ``cleanup()`` 的工具先调 ``cleanup()``，隐藏工具自己的
          rubber_band / 磁吸标记等画布残留（旧实现只 unset，停编辑 / 切工具后
          绘制预览、吸附标记仍会留在画布上）；
        - unset 时处理带 ``.tool`` 属性的 wrapper（兼容历史实现）；
        - QGIS 的 ``unsetMapTool`` 不会自动回退前一个工具，画布若已无任何工具
          则恢复激活前记录的工具（如平移），避免停止编辑后画布"失能"。
        """
        # 收集本次卸下的真实 QgsMapTool（unset 静默失败时据此判断画布残留）
        dropped_real_tools: list = []

        def _drop(attr_name: str) -> None:
            tool = getattr(self, attr_name, None)
            if tool is None:
                return
            if hasattr(tool, "cleanup"):
                try:
                    tool.cleanup()
                except Exception:
                    logger.exception("清理工具 %s(%s) 的 cleanup 失败", attr_name, type(tool).__name__)
            try:
                # wrapper 兼容：工具可能是带 .tool 的对象（历史实现），也可能
                # 就是 QgsMapTool 本身；unset 用真实的地图工具实例才有效
                real_tool = getattr(tool, "tool", tool)
                dropped_real_tools.append(real_tool)
                self.canvas.unsetMapTool(real_tool)
            except Exception:
                logger.exception("从画布卸下工具 %s(%s) 失败", attr_name, type(tool).__name__)
            setattr(self, attr_name, None)

        _drop("merge_tool")
        _drop("feature_edit_tool")
        _drop("attribute_entry_tool")
        _drop("split_tool")
        _drop("active_tool")
        # 业务层 draw_manager 的共享 rubber_band / temp_points 也一并清掉
        if self.draw_manager is not None:
            try:
                self.draw_manager.clear_draw_tool()
            except Exception:
                logger.exception("清除业务层 DrawManager 工具失败")
        if self.rubber_band is not None:
            try:
                self.rubber_band.reset()
            except Exception:
                logger.exception("清除共享绘制预览橡皮筋失败")
            self.rubber_band = None
        self.temp_points.clear()
        # 画布已无任何工具（或 unset 静默失败、仍挂着我们刚卸下的工具）时恢复
        # 激活前的工具（QGIS 不自动回退）；外部工具不受影响（不覆盖）。
        current_tool = self.canvas.mapTool()
        still_ours = current_tool is not None and any(
            current_tool is t for t in dropped_real_tools
        )
        if self._previous_map_tool is not None and (current_tool is None or still_ours):
            try:
                self.canvas.setMapTool(self._previous_map_tool)
            except Exception:
                logger.exception("恢复激活前画布工具 %s 失败", type(self._previous_map_tool).__name__)
        self._previous_map_tool = None
        # 工具实例已全部卸下：激活种类复位 + 统一刷新按钮（勾选全弹起）
        self._active_kind = None
        self._refresh_button_states()
        logger.info(
            "工具清理完成: mapTool=%s active_tool=%s merge=%s feature=%s split=%s attr_entry=%s",
            type(self.canvas.mapTool()).__name__ if self.canvas.mapTool() is not None else None,
            self.active_tool,
            self.merge_tool,
            self.feature_edit_tool,
            self.split_tool,
            self.attribute_entry_tool,
        )

    # ============================================================== 内部

    def _activate_map_tool(self, tool) -> None:
        """把工具挂到画布并记录激活前的画布工具（供 clear 后恢复）。

        工具条里各 ``enable_*`` 激活前都会先 ``clear_draw_tool()``（此时
        ``_previous_map_tool`` 已复位并可能已把画布工具恢复回去），所以这里记到
        的就是"用户最后用的常规工具"（平移 / 选择等），停止编辑后能还原。
        """
        self._previous_map_tool = self.canvas.mapTool()
        self.canvas.setMapTool(tool)
        logger.debug(
            "激活画布工具: %s (激活前=%s editable=%s)",
            type(getattr(tool, "tool", tool)).__name__,
            type(self._previous_map_tool).__name__ if self._previous_map_tool is not None else None,
            self._editing,
        )

    def _connect_edit_stop_hook(self, layer: QgsVectorLayer | None) -> None:
        """把 ``layer`` 的 ``editingStopped`` 接到本工具条的兜底清理。

        属性表 Dock「停止编辑」/ 切图层停旧层 / 关工程等外部入口结束编辑时只
        调各自的 commit/rollBack，不经过 :meth:`stop_edit_fun`——QGIS 在
        提交/回滚结束编辑时发 ``editingStopped``，本工具条在信号里统一解除
        激活工具并复位按钮状态，避免「属性框取消后再停编，绘制工具仍挂在画布」。

        未编辑的图层也照连：之后由外部开启再结束编辑同样能兜底。先断开上一
        图层再连当前图层，同一时刻只挂一个钩子。
        """
        self._disconnect_edit_stop_hook()
        if layer is None or layer.type() != layer.VectorLayer:
            return
        try:
            layer.editingStopped.connect(self._on_editing_stopped)
            self._edit_stop_hook_layer = layer
            logger.debug("已挂接图层 editingStopped 钩子: %s", layer.name())
        except Exception:
            self._edit_stop_hook_layer = None
            logger.exception("连接图层 editingStopped 钩子失败: %s", layer.name())

    def _disconnect_edit_stop_hook(self) -> None:
        """断开当前图层的 ``editingStopped`` 钩子（目标切换 / 钩子已触发）。"""
        layer = self._edit_stop_hook_layer
        self._edit_stop_hook_layer = None
        if layer is None:
            return
        try:
            layer.editingStopped.disconnect(self._on_editing_stopped)
        except Exception:
            # 图层销毁 / 信号已被触发自动断开等场景会抛异常，属正常收尾
            logger.warning(
                "断开图层 editingStopped 钩子失败(收尾场景属正常): %s", layer.name(), exc_info=True
            )

    def _on_editing_stopped(self) -> None:
        """任意入口结束编辑的兜底：解除激活工具并复位工具条状态。

        commitChanges / rollBack 结束编辑时会同步发 ``editingStopped``（含
        :meth:`stop_edit_fun` 自己走的提交路径）；这里统一处理后，后续
        :meth:`_exit_editing_state` 因 ``_editing`` 已复位而不会重复发信号。
        """
        self._disconnect_edit_stop_hook()
        layer = self._current_layer()
        logger.info(
            "editingStopped 钩子触发(外部停编兜底): layer=%s active_tool=%s mapTool=%s",
            getattr(layer, "name", lambda: None)(),
            self.active_tool,
            type(self.canvas.mapTool()).__name__ if self.canvas.mapTool() is not None else None,
        )
        self._exit_editing_state(layer)

    def _current_layer(self) -> QgsVectorLayer | None:
        return self._editing_layer_getter()

    def _mark_active(self, kind: str) -> None:
        """激活成功后登记激活工具种类并同步按钮（勾选本按钮、互斥其余）。

        只改状态 + 触发统一刷新；真正挂画布 / 弹提示由各 ``enable_*`` 完成。
        """
        self._active_kind = kind
        self._refresh_button_states()

    def _refresh_button_states(self) -> None:
        """统一按钮状态出口：由「目标图层 / 是否编辑中 / 激活工具」推导全部按钮。

        - enabled(能否点击)：

          * 「开始编辑」「属性录入」恒可用(属性录入激活时自动进编辑态)；
          * 「停止编辑 / 保存编辑 / 融合几何 / 要素编辑 / 线分割」仅编辑中可用；
          * 「添加点/线/面」仅编辑中且图层几何匹配时可用；

        - checked(勾选=工具正激活)：仅激活工具对应的按钮按下，其余弹起。

        所有入口(开始/停止编辑、切图层、激活/清除/切换工具)只改状态字段后调
        本方法，不再散点手工 setEnabled/setChecked，避免按钮状态与真实状态脱节
        (历史 bug：工具已清除但「添加面」按钮仍呈激活/高亮态)。
        """
        layer = self._current_layer()
        is_vec = layer is not None and layer.type() == layer.VectorLayer
        editing = bool(is_vec and layer.isEditable())

        # 「开始编辑」恒可用(点击弹窗选层)；「属性录入」恒可用(激活时自动开编辑)
        self.start_edit.setEnabled(True)
        self.attribute_entry_action.setEnabled(True)
        # 编辑操作：编辑中才可用
        for action in (
            self.stop_edit,
            self.save_edit,
            self.merge_geometry_action,
            self.feature_edit_action,
            self.split_feature_action,
        ):
            action.setEnabled(editing)
        # 「添加X」：编辑中且与图层几何匹配才可用
        g_type = layer.geometryType() if is_vec else -1
        self.add_point_action.setEnabled(editing and g_type == QgsWkbTypes.PointGeometry)
        self.add_line_action.setEnabled(editing and g_type == QgsWkbTypes.LineGeometry)
        self.add_polygon_action.setEnabled(editing and g_type == QgsWkbTypes.PolygonGeometry)
        # 勾选互斥：仅激活工具对应的按钮按下
        for kind, action in self._tool_action_map.items():
            action.setChecked(kind == self._active_kind)
