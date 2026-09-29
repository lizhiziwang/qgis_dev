"""绘图 UI 装配（与旧 :class:`component.draw_tool.Draw` 行为一致）。

按旧版设计：

- 工具条平铺文本按钮（不用下拉菜单）：
  开始编辑 / 停止编辑 / 保存编辑 / 撤销 / 重做 / 合并 / 要素编辑 / 属性录入 /
  线分割 / 面分割 / 挖洞 / 补洞 / 分解 / 补缝 / 边界修改 / 移动要素 /
  删除 / 要素分割 ｜ 添加点 / 添加线 / 添加面
- 「开始编辑」**恒可用**：点击弹出图层选择框（:attr:`_edit_layer_chooser`，
  由 :class:`MainWindow` 注入），选中图层即成为编辑目标并进入编辑态——
  不再依赖图层树右键 / 点击预设编辑图层（那些入口已移除）。
- 几何类型按当前 layer 启用对应按钮（点 layer 只能点，线 layer 只能线）；
  **编辑类工具一律要求「先选中图形 + 图层处于编辑态」**——它们作用的正是选中的
  图形，没选中就无从下手，按钮置灰（唯一例外是「要素分割」，它要的选中集在
  **其它图层**上）。各工具的语义：
  「要素编辑」点击要素编辑其几何，见
  :class:`app.ui.tools.feature_edit.FeatureEditTool`；「属性录入」**不需**
  先开编辑——激活时若目标图层未编辑会自动进入编辑态（点击要素 → 右侧
  Dock 编辑属性后确认，见
  :class:`app.ui.docks.attribute_entry_dock.AttributeEntryDock`）；
  「合并」另要求选中 **≥ 2 个**（先在画布上多选，再点按钮，弹窗里选定
  「要并入哪个图斑」后确认，见
  :func:`app.ui.tools.merge_geometry.merge_features_interactive`——一次性动作，
  非画布工具，不占用 checkable 状态）。**只有相邻接的图形才能合并**：只取选中
  里最大的那组相邻要素，其余在弹窗里标注「已排除」；一组相邻的都没有则只弹提示；
  「线分割」
  （先点选 / 框选选中要切的图形，激活后直接按住左键画切割线、右键完成，切的就是
  选中的要素，见 :class:`app.ui.tools.split_feature.SplitFeatureTool`）；
  「面分割」（在**选中**要素上左键画多边形切割区域、双击分割，见
  :class:`app.ui.tools.split_polygon.SplitPolygonTool`）；「挖洞」（在**选中**面
  要素上画挖除区域、双击挖成洞）、「补洞」（点击**选中**面要素洞的内部填补）
  同样只对选中的要素生效；
  「分解」先用选择工具选中多部件要素，点击后拆成多个
  单部件要素，见 :func:`app.ui.tools.explode_parts.explode_features`——
  一次性动作，非画布工具，不占用 checkable 状态；
  「补缝」需为面图层且选中 **≥ 2 个**相邻面（点击后检出它们
  之间的封闭缝隙并弹窗逐条指定并入哪个参与面，见
  :func:`app.ui.tools.gap_repair.repair_gaps_interactive`——同为一次性动作）；
  「边界修改」需为面图层（先用点选 / 框选选中图形，
  再左键画整形线、双击完成整形，见
  :class:`app.ui.tools.boundary_edit.BoundaryEditTool`）。
  「移动要素」几何类型不限（左键拖动平移，
  见 :class:`app.ui.tools.move_features.MoveFeaturesTool`）。
  「删除」删掉编辑图层上的选中要素，见
  :func:`app.ui.tools.delete_features.delete_selected_features`；
  「要素分割」要求**其它图层**上有选中的切割要素（先用点选 / 框选
  在其它图层选好，再点按钮切开编辑图层中与之相交的要素，见
  :func:`app.ui.tools.split_by_selection.split_by_selected_features`）。
  后两者都是一次性动作，不占用勾选态。
  「添加点 / 线 / 面」是**新建**要素，不需要选中，只看编辑态与图层几何类型。

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
    QgsProject,
    QgsVectorLayer,
    QgsWkbTypes,
)
from qgis.gui import QgsRubberBand
from qgis.PyQt.QtCore import QObject, QSize, Qt, QTimer, pyqtSignal
from qgis.PyQt.QtGui import QAction, QColor
from qgis.PyQt.QtWidgets import (
    QMainWindow,
    QMessageBox,
)

from app.core.domain.drawing import DrawManager
from app.core.domain.drawing.tools import get_tool_class
from app.infra.logging import get_logger
from app.infra.messagebar import send
from app.ui.common import tool_icons
from app.ui.common.ribbon import RibbonToolBar
from app.ui.common.theme import ThemeManager
from app.ui.dialogs.custom_attr import CustomAttrDialog
from app.ui.docks.attribute_entry_dock import AttributeEntryDock
from app.ui.tools.attribute_entry import AttributeEntryTool
from app.ui.tools.boundary_edit import BoundaryEditTool
from app.ui.tools.delete_features import delete_selected_features as run_delete
from app.ui.tools.dig_hole import DigHoleTool
from app.ui.tools.explode_parts import explode_features as run_explode
from app.ui.tools.feature_edit import FeatureEditTool
from app.ui.tools.fill_hole import FillHoleTool
from app.ui.tools.gap_repair import repair_gaps_interactive as run_gap_repair
from app.ui.tools.feature_attrs import new_feature_attributes
from app.ui.tools.merge_geometry import merge_features_interactive as run_merge
from app.ui.tools.move_features import MoveFeaturesTool
from app.ui.tools.split_feature import SplitFeatureTool
from app.ui.tools.split_polygon import SplitPolygonTool
from app.ui.tools.split_by_selection import (
    has_cutter_selection,
    split_by_selected_features as run_split_by_selection,
)

logger = get_logger(__name__)

#: 必须先有选中要素才能用的工具：按钮可用性依赖选中集，失去选中即自动退出
#: （见 :meth:`DigitizingToolbar._on_selection_changed`）
_SELECTION_REQUIRED_KINDS = (
    "feature",
    "split",
    "split_polygon",
    "dig_hole",
    "fill_hole",
    "boundary_edit",
    "move",
)

#: 本工具条持有的画布工具实例所在的属性名（判「画布上的工具是不是我们的」用，
#: 见 :meth:`DigitizingToolbar._is_own_tool`）
_OWN_TOOL_ATTRS = (
    "active_tool",
    "feature_edit_tool",
    "attribute_entry_tool",
    "split_tool",
    "split_polygon_tool",
    "dig_hole_tool",
    "fill_hole_tool",
    "boundary_edit_tool",
    "move_features_tool",
)

#: 编辑工具图标逻辑边长（与 ``setIconSize`` 一致，SVG 按此尺寸原生渲染）
_ICON = 20


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
        #: 挂了 ``canUndoChanged`` / ``canRedoChanged`` 的撤销栈（与停编钩子同
        #: 生命周期）：撤销 / 重做按钮的可用态实时跟随栈变化
        self._undo_stack_hook = None
        self.feature_edit_tool = None
        self.split_tool = None
        self.split_polygon_tool = None
        self.dig_hole_tool = None
        self.fill_hole_tool = None
        self.boundary_edit_tool = None
        self.move_features_tool = None
        #: 属性录入工具实例（防 GC）与右侧 Dock（懒建单实例）
        self.attribute_entry_tool = None
        self._attribute_entry_dock = None

        # 当前是否处于编辑态（start_edit_fun 成功后 True，stop_edit_fun 后 False）
        self._editing = False
        #: 当前激活的工具种类："point"/"line"/"polygon"/"merge"/"feature"/
        #: "split"/"split_polygon"/"dig_hole"/"fill_hole"/"boundary_edit"/
        #: "move"/"attr_entry"，None=未激活（「分解」「补缝」是一次性动作，
        #: 不占用激活态）。工具按钮的勾选/互斥、日志都以它为唯一依据
        #: （_refresh_button_states 统一推导）
        self._active_kind: str | None = None
        #: 挂了 ``editingStopped`` 兜底钩子的图层：外部入口（属性表 Dock「停止
        #: 编辑」/ 切图层停旧层 / 关工程等）结束编辑时不走 :meth:`stop_edit_fun`，
        #: 靠本钩子统一清工具并复位状态（见 :meth:`_on_editing_stopped`）。
        self._edit_stop_hook_layer = None
        #: 已挂 ``selectionChanged`` 的图层 id 集合（工程级监听，见
        #: :meth:`_connect_selection_hook`）；工程量少，用集合去重即可。
        self._selection_hooked: dict[str, bool] = {}
        self._project_selection_connected = False

        self._build_toolbar()
        self._connect_map_tool_hook()

    def _connect_map_tool_hook(self) -> None:
        """监听画布工具变化，兜住各工具自己按 Esc 退出的路径。

        工具自己退出走的是 ``canvas.unsetMapTool(self)``，不经过本工具条；不接这个
        信号就会留下「工具已退出、按钮还按着、浮动提示还挂着」的假状态（见
        :meth:`_on_canvas_map_tool_set`）。
        """
        try:
            self.canvas.mapToolSet.connect(self._on_canvas_map_tool_set)
        except Exception:  # noqa: BLE001 —— 画布桩 / 旧构建没有该信号时降级
            logger.warning("连接画布 mapToolSet 信号失败，Esc 退出工具后按钮可能不弹起",
                           exc_info=True)

    # ============================================================== 装配

    def _build_toolbar(self) -> None:
        self.digitizing_toolbar = RibbonToolBar("编辑工具")
        self.digitizing_toolbar.setObjectName("DigitizingToolbar")
        # Ribbon 风格：图标在上 + 小字标签在下；按钮按编辑态/几何类型启用/禁用
        self.digitizing_toolbar.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextUnderIcon)
        self.digitizing_toolbar.setIconSize(QSize(_ICON, _ICON))

        # ---- 编辑操作 ----
        self.start_edit = QAction("开始编辑", self.digitizing_toolbar)
        self.start_edit.triggered.connect(self.start_edit_fun)
        self.stop_edit = QAction("停止编辑", self.digitizing_toolbar)
        self.stop_edit.triggered.connect(self.stop_edit_fun)
        self.save_edit = QAction("保存编辑", self.digitizing_toolbar)
        # 旧逻辑：stop_edit 也绑了 commitChanges，但语义重叠，这里拆开
        self.save_edit.triggered.connect(self._save_edits)

        # ---- 合并（需处于编辑态**且选中 2 个及以上要素**；一次性动作：先在画布上
        #      多选，再点此弹窗选定要并入哪个图斑。只有相邻接的才能合并）----
        self.merge_geometry_action = QAction("合并", self.digitizing_toolbar)
        self.merge_geometry_action.setToolTip(
            "先用点选/框选选中两个及以上相邻接的要素并进入编辑，再点此选定并入哪个图斑"
        )
        self.merge_geometry_action.triggered.connect(self.merge_features)

        # ---- 要素编辑（需处于编辑态；点击要素编辑其几何）----
        self.feature_edit_action = QAction("要素编辑", self.digitizing_toolbar)
        self.feature_edit_action.setCheckable(True)
        self.feature_edit_action.setToolTip(
            "先用点选/框选选中要素并进入编辑，再点此：点击要素编辑其几何"
        )
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
        self.split_feature_action.setToolTip(
            "先用点选/框选选中要素并进入编辑，再点此：按住左键画切割线、右键完成分割"
        )
        self.split_feature_action.triggered.connect(
            lambda checked=False: self.enable_split_tool(checked)
        )

        # ---- 撤销 / 重做（编辑中且栈上有对应步骤时可用；同 Ctrl+Z / Ctrl+Shift+Z）----
        self.undo_action = QAction("撤销", self.digitizing_toolbar)
        self.undo_action.setToolTip("撤销上一步编辑（Ctrl+Z）")
        self.undo_action.triggered.connect(self.undo_last_edit)
        self.redo_action = QAction("重做", self.digitizing_toolbar)
        self.redo_action.setToolTip("重做最近被撤销的编辑（Ctrl+Shift+Z / Ctrl+Y）")
        self.redo_action.triggered.connect(self.redo_last_edit)

        # ---- 面分割（需处于编辑态；画多边形切割区域 → 双击分割）----
        self.split_polygon_action = QAction("面分割", self.digitizing_toolbar)
        self.split_polygon_action.setCheckable(True)
        self.split_polygon_action.setToolTip(
            "先用点选/框选选中要素并进入编辑，再点此：在选中要素上画切割区、双击分割"
        )
        self.split_polygon_action.triggered.connect(
            lambda checked=False: self.enable_split_polygon_tool(checked)
        )

        # ---- 挖洞（需处于编辑态且为面图层；画挖除区域 → 双击挖成洞）----
        self.dig_hole_action = QAction("挖洞", self.digitizing_toolbar)
        self.dig_hole_action.setCheckable(True)
        self.dig_hole_action.setToolTip(
            "先用点选/框选选中面要素并进入编辑，再点此：在选中要素上画挖除区、双击挖洞"
        )
        self.dig_hole_action.triggered.connect(
            lambda checked=False: self.enable_dig_hole(checked)
        )

        # ---- 补洞（需处于编辑态且为面图层；点击洞的内部填补）----
        self.fill_hole_action = QAction("补洞", self.digitizing_toolbar)
        self.fill_hole_action.setCheckable(True)
        self.fill_hole_action.setToolTip(
            "先用点选/框选选中面要素并进入编辑，再点此：点击选中要素的洞内部填补"
        )
        self.fill_hole_action.triggered.connect(
            lambda checked=False: self.enable_fill_hole(checked)
        )

        # ---- 分解（需处于编辑态；一次性动作：选中多部件要素 → 拆成单部件）----
        self.explode_action = QAction("分解", self.digitizing_toolbar)
        self.explode_action.setToolTip(
            "先用点选/框选选中多部件要素并进入编辑，再点此拆成多个单部件要素"
        )
        self.explode_action.triggered.connect(self.explode_features)

        # ---- 补缝（需处于编辑态且为面图层；一次性动作：多选相邻面 → 检出它们
        #      之间的封闭缝隙 → 弹窗逐条指定并入哪个参与面）----
        self.gap_repair_action = QAction("补缝", self.digitizing_toolbar)
        self.gap_repair_action.setToolTip(
            "先用选择工具多选相邻面，再点此检出它们之间的封闭缝隙并指定并入哪个面"
        )
        self.gap_repair_action.triggered.connect(self.repair_gaps)

        # ---- 边界修改（需处于编辑态、面图层且**有选中要素**；左键画整形线、
        #      双击完成，线的两端须落在图形外侧）----
        self.boundary_edit_action = QAction("边界修改", self.digitizing_toolbar)
        self.boundary_edit_action.setCheckable(True)
        self.boundary_edit_action.setToolTip(
            "先用点选/框选选中图形并进入编辑，再点此：左键画整形线、双击完成"
        )
        self.boundary_edit_action.triggered.connect(
            lambda checked=False: self.enable_boundary_edit(checked)
        )

        # ---- 移动要素（需处于编辑态**且有选中要素**；左键拖动平移，任意几何类型）----
        self.move_action = QAction("移动要素", self.digitizing_toolbar)
        self.move_action.setCheckable(True)
        self.move_action.setToolTip(
            "先用点选/框选选中要素并进入编辑，再点此：在画布上左键拖动即可平移"
        )
        self.move_action.triggered.connect(
            lambda checked=False: self.enable_move_tool(checked)
        )

        # ---- 删除（需处于编辑态且有选中要素；一次性动作：删掉选中要素）----
        self.delete_action = QAction("删除", self.digitizing_toolbar)
        self.delete_action.setToolTip("删除当前编辑图层上选中的要素（可 Ctrl+Z 撤销）")
        self.delete_action.triggered.connect(self.delete_selected)

        # ---- 要素分割（需处于编辑态，且**其它图层**上有选中的切割要素；一次性动作）----
        self.split_by_selection_action = QAction("要素分割", self.digitizing_toolbar)
        self.split_by_selection_action.setToolTip(
            "先在其它图层选中与目标相交的要素，再点此分割当前编辑图层的相交要素"
        )
        self.split_by_selection_action.triggered.connect(self.split_by_selection)

        # ---- 初始态：编辑动作全禁用（「开始编辑」例外——恒可用，点击弹窗选层）
        #     最后统一由 _refresh_button_states 推导，这里只组装不动状态

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
            "feature": self.feature_edit_action,
            "split": self.split_feature_action,
            "split_polygon": self.split_polygon_action,
            "dig_hole": self.dig_hole_action,
            "fill_hole": self.fill_hole_action,
            "boundary_edit": self.boundary_edit_action,
            "move": self.move_action,
            "attr_entry": self.attribute_entry_action,
        }

        self.digitizing_toolbar.addAction(self.add_point_action)
        self.digitizing_toolbar.addAction(self.add_line_action)
        self.digitizing_toolbar.addAction(self.add_polygon_action)

        # 按组挂到工具条：组间加竖分隔线，组名由 RibbonToolBar 画在每组按钮下方
        tool_groups: list[tuple[str, list[QAction]]] = [
            (
                "编辑",
                [
                    self.start_edit,
                    self.stop_edit,
                    self.save_edit,
                    self.undo_action,
                    self.redo_action,
                ],
            ),
            (
                "几何",
                [
                    self.merge_geometry_action,
                    self.feature_edit_action,
                    self.attribute_entry_action,
                    self.boundary_edit_action,
                    self.move_action,
                    self.delete_action,
                ],
            ),
            (
                "分割",
                [
                    self.split_feature_action,
                    self.split_polygon_action,
                    self.dig_hole_action,
                    self.fill_hole_action,
                    self.gap_repair_action,
                    self.explode_action,
                    self.split_by_selection_action,
                ],
            ),
            (
                "绘制",
                [self.add_point_action, self.add_line_action, self.add_polygon_action],
            ),
        ]
        for idx, (_group_name, acts) in enumerate(tool_groups):
            for act in acts:
                self.digitizing_toolbar.addAction(act)
            if idx != len(tool_groups) - 1:
                self.digitizing_toolbar.addSeparator()
        self.digitizing_toolbar.set_groups(
            tool_groups, ThemeManager.instance().current().text_secondary
        )

        # 按钮图标（线性 SVG，随主题变色；见 app.ui.common.tool_icons）
        self._icon_keys: dict = {
            self.undo_action: "undo",
            self.redo_action: "redo",
            self.merge_geometry_action: "merge",
            self.feature_edit_action: "move",
            self.attribute_entry_action: "attribute",
            self.split_feature_action: "line_split",
            self.split_polygon_action: "polygon_split",
            self.dig_hole_action: "dig_hole",
            self.fill_hole_action: "fill_gap",
            self.explode_action: "explode",
            self.gap_repair_action: "fill_gap",
            self.boundary_edit_action: "boundary_edit",
            self.move_action: "move",
            self.delete_action: "delete",
            self.split_by_selection_action: "split_by_selection",
            self.add_point_action: "draw",
            self.add_line_action: "draw",
            self.add_polygon_action: "draw",
        }
        self._apply_icons()

        # 初始态由统一出口推导（无目标图层 → 仅「开始编辑」/「属性录入」可用）
        self._refresh_button_states()

        # 挂到主窗口
        self.main_window.addToolBar(self.digitizing_toolbar)

    def _apply_icons(self) -> None:
        """按当前主题强调色重刷编辑工具图标（主题切换时也调）。"""
        theme = ThemeManager.instance().current()
        color = theme.input_focus
        for act, key in self._icon_keys.items():
            act.setIcon(tool_icons.icon(key, color, _ICON))
        tb = getattr(self, "digitizing_toolbar", None)
        if isinstance(tb, RibbonToolBar):
            tb.set_group_color(theme.text_secondary)

    def apply_theme(self, theme=None) -> None:
        """主题切换入口（与 ToolBar/SideToolBar 一致）：重刷图标颜色。"""
        self._apply_icons()

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
        # 是否已进入编辑态推导(编辑中 → 停止/保存/合并等 + 对应几何的"添加X"可用)
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
         1. 弹窗选「取消」→ **只关窗**，不提交不回滚、不退出编辑，也不动激活工具
            （编辑会话与工具保持弹窗前的样子）；选「是 / 否」才真正结束编辑，
            此时先 ``clear_draw_tool()`` 取消激活工具（不管有没有画完都要退出）；
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
        if layer is None or not layer.isEditable():
            # 没有在编辑：兜底清掉可能残留的激活工具/橡皮筋，保持干净
            self.clear_draw_tool()
            self._exit_editing_state(layer)
            return

        try:
            if layer.editBuffer() is not None:
                reply = QMessageBox.warning(
                    self.main_window,
                    "警告",
                    "是否保存编辑的内容！",
                    QMessageBox.StandardButton.Yes
                    | QMessageBox.StandardButton.No
                    | QMessageBox.StandardButton.Cancel,
                    QMessageBox.StandardButton.Yes,
                )
                if reply == QMessageBox.StandardButton.Cancel:
                    # 「取消」只关窗：不提交、不回滚、不退出编辑，也不动激活工具——
                    # 编辑会话与工具状态保持打开弹窗前的样子
                    logger.info("停止编辑已取消（保持编辑态）: layer=%s", layer.name())
                    return
                # 用户已决定结束编辑：此时才取消激活工具（不管有没有画完都要退出）
                self.clear_draw_tool()
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
            else:
                # 无未提交改动：不必问保存，直接结束编辑；工具照常退出
                self.clear_draw_tool()
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
          「是否仍在编辑」推导：离开编辑态后「停止/保存/合并/要素编辑/线分割/
          面分割/分解」与「添加要素」全禁用并取消勾选(需重新「开始编辑」才恢复可用)。
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
        # 只有处于编辑态才能画要素(与要素编辑等工具一致)；停编辑后按钮
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
        # QMessageBox.information(
        #     self.main_window,
        #     "提示",
        #     f"已激活「添加{geom_type}」工具，点击画布绘制！",
        # )
        self._toast(f"已激活「添加{geom_type}」工具，点击画布绘制，右键完成。")

    def merge_features(self) -> None:
        """「合并」一次性动作：把相邻接的选中要素并成一个。

        不走画布工具、不占用勾选态；先用选择工具在画布上多选（按钮可用性要求
        **选中 ≥ 2 且图层处于编辑态**），点击后弹窗列出**可合并的那组相邻要素**
        及其属性、由用户指定「要并入哪个图斑」，写入**一个**撤销命令，可整体
        Ctrl+Z。选中图形之间完全没有邻接时只弹提示、不写任何东西。
        """
        layer = self._current_layer()
        if layer is None or layer.type() != layer.VectorLayer:
            QMessageBox.warning(self.main_window, "警告", "请先选择矢量图层")
            return
        if not layer.isEditable():
            QMessageBox.warning(self.main_window, "警告", "请先开始编辑！")
            return
        # 选中数在点击时再校验一次：按钮可用性已按选中数置灰，这里兜底防直接调用
        if len(layer.selectedFeatureIds()) < 2:
            QMessageBox.warning(
                self.main_window, "警告", "请先用选择工具选中两个及以上要合并的要素"
            )
            return

        result = run_merge(
            layer,
            canvas=self.canvas,
            parent=self.main_window,
            hint=self._tool_hint,
        )
        if result is None:
            return  # 用户取消 / 失败原因已由下游落地
        if result == 0:
            # 一组相邻的都没有：只有相邻接的图形才能合并（细缝/角接触/分离都不算）
            QMessageBox.warning(
                self.main_window,
                "警告",
                "选中的图形之间没有相邻接的，无法合并。\n"
                "只有共享边界（或相互重叠）的图形才能合并；"
                "仅角对角相接、中间有缝隙的请先用「补缝」处理。",
            )
            return
        self._toast(f"已合并 {result} 个要素为 1 个，面积已重算（可 Ctrl+Z 撤销）")
        # 事件栈内不直接 refresh（仓库约束），推迟出栈后再刷新
        QTimer.singleShot(0, self._safe_refresh)

    def enable_feature_edit(self, checked: bool = True) -> None:
        """激活「要素编辑」工具：点击矢量要素编辑其几何（顶点级）。

        **要求先有选中要素**（按钮可用性同样如此）：没选中时按钮已置灰，这里
        再兜一层。工具本身仍按点击载入要素——选中是「进入工具」的门槛。

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
        if not self._require_selection(layer, "编辑"):
            return
        # 清掉可能激活的绘制工具，避免两个工具并存
        self.clear_draw_tool()


        self.feature_edit_tool = FeatureEditTool(
            self.canvas,
            editing_layer_getter=self._editing_layer_getter,
            hint=self._tool_hint,
        )
        self._activate_map_tool(self.feature_edit_tool)
        self._mark_active("feature")
        # QMessageBox.information(
        #     self.main_window,
        #     "提示",
        #     "已激活「要素编辑」工具：点击要素进入几何编辑（拖动顶点 / 双击边加顶点 / 右键删顶点）；Esc 退出",
        # )
        self._toast(
            "已激活「要素编辑」工具：点击要素进入几何编辑（拖动顶点 / 双击边加顶点 / 右键删顶点）；"
            "Esc 退出"
        )

    def enable_split_tool(self, checked: bool = True) -> None:
        """激活「线分割」工具：对**选中**要素直接按住左键画切割线、右键完成。

        切的就是当前选中的要素（见 :meth:`SplitFeatureTool._finish_split`），
        所以要求先有选中（按钮可用性同样如此）；不再需要在工具内双击挑目标。

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
        if not self._require_selection(layer, "分割"):
            return
        # 清掉可能激活的绘制 / 要素编辑工具，避免两个工具并存
        self.clear_draw_tool()


        self.split_tool = SplitFeatureTool(
            self.canvas,
            editing_layer_getter=self._editing_layer_getter,
            hint=self._tool_hint,
        )
        self._activate_map_tool(self.split_tool)
        self._mark_active("split")
        # QMessageBox.information(
        #     self.main_window,
        #     "提示",
        #     "已激活「线分割」工具：左键双击要素选中后，按住左键画切割线、右键完成分割；Esc 取消 / 退出",
        # )
        self._toast(
            "已激活「线分割」工具：按住左键画切割线、右键完成分割选中要素；Esc 清除 / 退出",
            level="info",
        )

    def enable_split_polygon_tool(self, checked: bool = True) -> None:
        """激活「面分割」工具：在**选中**要素上画多边形切割区域，双击分割。

        只对选中的要素生效（见 :meth:`SplitPolygonTool._run_cutter_action`），
        所以要求先有选中（按钮可用性同样如此）。

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
        if not self._require_selection(layer, "分割"):
            return
        # 清掉可能激活的其它工具，避免并存
        self.clear_draw_tool()


        self.split_polygon_tool = SplitPolygonTool(
            self.canvas,
            editing_layer_getter=self._editing_layer_getter,
            hint=self._tool_hint,
        )
        self._activate_map_tool(self.split_polygon_tool)
        self._mark_active("split_polygon")
        self._toast(
            "已激活「面分割」工具：在选中要素上左键画多边形（支持捕捉），"
            "双击完成分割；右键清除多边形，Esc 退出"
        )

    def enable_dig_hole(self, checked: bool = True) -> None:
        """激活「挖洞」工具：在**选中**面要素上画挖除区域，双击把该区域挖成洞。

        只对选中的要素生效（见 :meth:`DigHoleTool._run_cutter_action`），所以
        要求先有选中（按钮可用性同样如此）。

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
        if layer.geometryType() != QgsWkbTypes.PolygonGeometry:
            QMessageBox.warning(self.main_window, "警告", "挖洞只支持面图层")
            return
        if not self._require_selection(layer, "挖洞"):
            return
        # 清掉可能激活的其它工具，避免并存
        self.clear_draw_tool()


        self.dig_hole_tool = DigHoleTool(
            self.canvas,
            editing_layer_getter=self._editing_layer_getter,
            hint=self._tool_hint,
        )
        self._activate_map_tool(self.dig_hole_tool)
        self._mark_active("dig_hole")
        self._toast(
            "已激活「挖洞」工具：在选中面要素上左键画挖除区域（支持捕捉），"
            "双击把该区域挖成洞；右键清除区域，Esc 退出"
        )

    def enable_fill_hole(self, checked: bool = True) -> None:
        """激活「补洞」工具：在**选中**面要素的洞内部单击，删除该内环。

        只对选中的要素生效（见 :meth:`FillHoleTool._fill_at`），所以要求先有
        选中（按钮可用性同样如此）。

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
        if layer.geometryType() != QgsWkbTypes.PolygonGeometry:
            QMessageBox.warning(self.main_window, "警告", "补洞只支持面图层")
            return
        if not self._require_selection(layer, "补洞"):
            return
        # 清掉可能激活的其它工具，避免并存
        self.clear_draw_tool()


        self.fill_hole_tool = FillHoleTool(
            self.canvas,
            editing_layer_getter=self._editing_layer_getter,
            hint=self._tool_hint,
        )
        self._activate_map_tool(self.fill_hole_tool)
        self._mark_active("fill_hole")
        self._toast("已激活「补洞」工具：点击选中面要素洞的内部填补（挖洞产生的洞、内环均可）；Esc 退出")

    def enable_boundary_edit(self, checked: bool = True) -> None:
        """激活「边界修改」工具：画整形线改写**选中**面要素的边界。

        与其它画布工具不同，本工具**必须先有选中要素**才可用（按钮可用性也由
        选中集决定，见 :meth:`_refresh_button_states`）：它改的就是选中图形，
        没有选中就无从下手。

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
        if layer.geometryType() != QgsWkbTypes.PolygonGeometry:
            QMessageBox.warning(self.main_window, "警告", "边界修改只支持面图层")
            return
        if not layer.selectedFeatureIds():
            QMessageBox.warning(
                self.main_window, "警告", "请先用点选 / 框选选中要修改边界的图形"
            )
            return
        # 清掉可能激活的其它工具，避免并存
        self.clear_draw_tool()

        self.boundary_edit_tool = BoundaryEditTool(
            self.canvas,
            editing_layer_getter=self._editing_layer_getter,
            hint=self._tool_hint,
        )
        self._activate_map_tool(self.boundary_edit_tool)
        self._mark_active("boundary_edit")
        self._toast(
            "已激活「边界修改」工具：左键画整形线、双击完成（线的两端要落在图形外侧）；"
            "右键清除、Esc 退出"
        )

    def enable_move_tool(self, checked: bool = True) -> None:
        """激活「移动要素」工具：在画布上拖动平移**选中**的要素。

        与「边界修改」一样属于**必须先有选中要素**的工具（可用性由选中集决定，
        见 :meth:`_refresh_button_states`）；几何类型不限——点 / 线 / 面都能移。

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
        if not layer.selectedFeatureIds():
            QMessageBox.warning(
                self.main_window, "警告", "请先用点选 / 框选选中要移动的要素"
            )
            return
        # 清掉可能激活的其它工具，避免并存
        self.clear_draw_tool()

        self.move_features_tool = MoveFeaturesTool(
            self.canvas,
            editing_layer_getter=self._editing_layer_getter,
            hint=self._tool_hint,
        )
        self._activate_map_tool(self.move_features_tool)
        self._mark_active("move")
        self._toast(
            "已激活「移动要素」工具：在画布上左键拖动即可平移选中要素"
            "（拖动起止点都会吸附）；Esc 取消本次拖动"
        )

    def delete_selected(self) -> None:
        """「删除」一次性动作：删掉当前编辑图层上选中的要素。

        不走画布工具、不占用勾选态；只作用于**当前编辑图层**（与工具条其它命令
        作用域一致），写入一个撤销命令，可整体 Ctrl+Z。
        """
        layer = self._current_layer()
        if layer is None or layer.type() != layer.VectorLayer:
            QMessageBox.warning(self.main_window, "警告", "请先选择矢量图层")
            return
        if not layer.isEditable():
            QMessageBox.warning(self.main_window, "警告", "请先开始编辑！")
            return
        if not layer.selectedFeatureIds():
            QMessageBox.warning(
                self.main_window, "警告", "请先用点选 / 框选选中要删除的要素"
            )
            return

        deleted = run_delete(
            layer,
            hint=self._tool_hint,
        )
        if deleted is None:
            return  # 失败原因已由下游落地
        if deleted == 0:
            QMessageBox.information(self.main_window, "提示", "没有可删除的选中要素")
            return
        QMessageBox.information(
            self.main_window, "提示", f"已删除 {deleted} 个要素（可 Ctrl+Z 撤销）"
        )
        # 事件栈内不直接 refresh（仓库约束），推迟出栈后再刷新
        QTimer.singleShot(0, self._safe_refresh)

    def split_by_selection(self) -> None:
        """「要素分割」一次性动作：用其它图层选中的要素切开当前编辑图层的相交要素。

        先用点选 / 框选在**其它图层**上选好切割要素，再点本按钮；与切割要素相交
        的编辑图层要素被切开。支持线切面 / 线切线 / 面切面 / 面切线（统一按
        「切割要素的边界线」切分）。写入一个撤销命令，可整体 Ctrl+Z。
        """
        layer = self._current_layer()
        if layer is None or layer.type() != layer.VectorLayer:
            QMessageBox.warning(self.main_window, "警告", "请先选择矢量图层")
            return
        if not layer.isEditable():
            QMessageBox.warning(self.main_window, "警告", "请先开始编辑！")
            return
        if not has_cutter_selection(layer):
            QMessageBox.warning(
                self.main_window,
                "警告",
                "请先在**其它图层**上选中要与目标相交的切割要素"
                "（当前编辑图层自身不算）",
            )
            return

        result = run_split_by_selection(
            layer,
            hint=self._tool_hint,
        )
        if result is None:
            return  # 失败原因已由下游落地
        changed, added = result
        if changed == 0:
            QMessageBox.information(
                self.main_window, "提示", "所选要素没能切开任何图形"
            )
            return
        QMessageBox.information(
            self.main_window,
            "提示",
            f"已切开 {changed} 个要素，新增 {added} 块（可 Ctrl+Z 撤销）",
        )
        # 事件栈内不直接 refresh（仓库约束），推迟出栈后再刷新
        QTimer.singleShot(0, self._safe_refresh)

    # ============================================================== 编辑撤销 / 重做

    def undo_last_edit(self) -> None:
        """「撤销」按钮：同画布 Ctrl+Z，对当前编辑图层撤销一步（MainWindow 实现）。"""
        fn = getattr(self.main_window, "_undo_edit", None)
        if not callable(fn):
            QMessageBox.information(self.main_window, "提示", "当前窗口不支持撤销")
            return
        fn()

    def redo_last_edit(self) -> None:
        """「重做」按钮：同画布 Ctrl+Shift+Z / Ctrl+Y，重做一步（MainWindow 实现）。"""
        fn = getattr(self.main_window, "_redo_edit", None)
        if not callable(fn):
            QMessageBox.information(self.main_window, "提示", "当前窗口不支持重做")
            return
        fn()

    def _on_undo_availability(self, *_args) -> None:
        """撤销栈 ``canUndoChanged`` / ``canRedoChanged`` → 刷新按钮可用态。"""
        self._refresh_button_states()

    def _connect_undo_stack(self, layer: QgsVectorLayer | None) -> None:
        """把 ``layer`` 撤销栈的可用态信号接到按钮刷新（与停编钩子同生命周期）。"""
        self._disconnect_undo_stack()
        if layer is None or layer.type() != layer.VectorLayer:
            return
        try:
            stack = layer.undoStack()
        except RuntimeError:
            return
        if stack is None:
            return
        try:
            stack.canUndoChanged.connect(self._on_undo_availability)
            stack.canRedoChanged.connect(self._on_undo_availability)
            self._undo_stack_hook = stack
        except Exception:  # noqa: BLE001
            self._undo_stack_hook = None
            logger.exception("连接撤销栈信号失败: %s", layer.name())

    def _disconnect_undo_stack(self) -> None:
        """断开当前挂着的撤销栈信号（目标切换 / 停止编辑）。"""
        stack = self._undo_stack_hook
        self._undo_stack_hook = None
        if stack is None:
            return
        for sig in ("canUndoChanged", "canRedoChanged"):
            try:
                getattr(stack, sig).disconnect(self._on_undo_availability)
            except Exception:  # noqa: BLE001 —— 栈销毁 / 未连接等收尾场景属正常
                pass

    def explode_features(self) -> None:
        """「分解」一次性动作：把选择工具选中的多部件要素拆成多个单部件要素。

        不走画布工具、不占用勾选态；通过图层选择读取目标要素，写入一个
        撤销命令（可整体 Ctrl+Z）。
        """
        layer = self._current_layer()
        if layer is None or layer.type() != layer.VectorLayer:
            QMessageBox.warning(self.main_window, "警告", "请先选择矢量图层")
            return
        if not layer.isEditable():
            QMessageBox.warning(self.main_window, "警告", "请先开始编辑！")
            return
        if not layer.selectedFeatureIds():
            QMessageBox.warning(
                self.main_window, "警告", "请先用选择工具选中要分解的多部件要素"
            )
            return


        result = run_explode(layer)
        if result is None:
            QMessageBox.warning(self.main_window, "警告", "分解失败（详见日志）")
            return
        changed, added = result
        if changed == 0:
            self._toast("所选要素中没有多部件要素，无需分解", level="info")
            return
        self._toast(f"已分解 {changed} 个多部件要素为 {changed + added} 个单部件要素（可 Ctrl+Z 撤销）")
        # 事件栈内不直接 refresh（仓库约束），推迟出栈后再刷新
        QTimer.singleShot(0, self._safe_refresh)

    def repair_gaps(self) -> None:
        """「补缝」一次性动作：把多选相邻面之间的封闭缝隙并入指定的目标面。

        不走画布工具、不占用勾选态；目标面由用户在弹窗里逐条指定（只能选
        参与围成该缝隙的面）。写入**一个**撤销命令，可整体 Ctrl+Z。
        """
        layer = self._current_layer()
        if layer is None or layer.type() != layer.VectorLayer:
            QMessageBox.warning(self.main_window, "警告", "请先选择矢量图层")
            return
        if not layer.isEditable():
            QMessageBox.warning(self.main_window, "警告", "请先开始编辑！")
            return
        if layer.geometryType() != QgsWkbTypes.PolygonGeometry:
            QMessageBox.warning(self.main_window, "警告", "补缝只支持面图层")
            return
        # 选中数在点击时校验：工具条没有 selectionChanged 监听，按钮可用性
        # 统一由 _refresh_button_states 从图层状态推导，不为此新增信号
        if len(layer.selectedFeatureIds()) < 2:
            QMessageBox.warning(
                self.main_window, "警告", "请先用选择工具选中至少两个相邻面要素"
            )
            return

        result = run_gap_repair(
            layer,
            canvas=self.canvas,
            parent=self.main_window,
            hint=self._tool_hint,
        )
        if result is None:
            return  # 用户取消 / 失败原因已由下游落地
        changed, filled = result
        if filled == 0:
            QMessageBox.information(
                self.main_window, "提示", "所选面要素之间未发现封闭缝隙"
            )
            return
        QMessageBox.information(
            self.main_window,
            "提示",
            f"已补 {filled} 处缝隙（并入 {changed} 个要素，可 Ctrl+Z 撤销）",
        )
        # 事件栈内不直接 refresh（仓库约束），推迟出栈后再刷新
        QTimer.singleShot(0, self._safe_refresh)

    def _safe_refresh(self) -> None:
        try:
            self.canvas.refresh()
        except RuntimeError:
            pass

    def _tool_hint(self, text: str) -> None:
        """工具运行期提示：推到浮动消息条，**不自动关闭**。

        消息条默认常驻，由用户点关闭按钮收起（见
        :class:`app.infra.messagebar.floating_bar.FloatingMessageBar`）。工具运行
        期的提示（「已选择 3 个要素」「已分割为 2 个」）常被用户盯着边操作边看，
        定时消失会看不全。
        """
        self.main_window.messageBar.pushMessage(text, level=Qgis.MessageLevel.Info)

    def _toast(self, message: str, level: str = "success") -> None:
        """编辑工具条的浮动提示：同样是**不自动关闭**的那种。

        ``duration=0`` 必须**显式传**：:func:`~app.infra.messagebar.send` 自己的
        默认是 3 秒——不传就等于又要自动消失，白改了。
        """
        send(level=level, message=message, duration=0)

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
        # 清掉可能激活的绘制 / 要素编辑 / 分割工具，避免并存
        self.clear_draw_tool()


        # 懒建右侧 Dock（单实例；关闭后再次激活时重新显示）
        dock = self._attribute_entry_dock
        if dock is None:
            dock = AttributeEntryDock(self.main_window)
            # 走 place_right_dock：右侧已有面板时叠放（覆盖），不堆在它下面
            placer = getattr(self.main_window, "place_right_dock", None)
            if callable(placer):
                placer(dock)
            else:  # 测试桩 / 旧窗口
                self.main_window.addDockWidget(Qt.DockWidgetArea.RightDockWidgetArea, dock)
            self._attribute_entry_dock = dock

        self.attribute_entry_tool = AttributeEntryTool(
            self.canvas,
            editing_layer_getter=self._editing_layer_getter,
            on_feature=dock.load_feature,
            hint=self._tool_hint,
        )
        self._activate_map_tool(self.attribute_entry_tool)
        self._mark_active("attr_entry")
        dock.show()
        dock.raise_()
        # QMessageBox.information(
        #     self.main_window,
        #     "提示",
        #     "已激活「属性录入」工具：点击图层上的要素，在右侧面板修改其属性；确认后保存，Esc 退出",
        # )
        self._toast(
            "已激活「属性录入」工具：点击图层上的要素，在右侧面板修改其属性；确认后保存，Esc 退出",
            level="info",
        )

    def _to_layer_geometry(self, layer: QgsVectorLayer, geometry: QgsGeometry) -> QgsGeometry:
        """绘制几何(画布坐标系) → 图层坐标系。

        绘制工具在画布上采集的是**画布 CRS** 坐标; 目标图层 CRS 不同时必须先
        转换, 否则经纬度度值会被当作投影米直接写入图层 —— 新要素画不到正确
        位置, 且右键缩放到该要素时反变换越界抛 ``QgsCsException``。
        CRS 相同 / 无效 / 转换失败时原样返回。
        """
        src = self.canvas.mapSettings().destinationCrs()
        dst = layer.crs()
        if not src.isValid() or not dst.isValid() or src == dst:
            return geometry
        from qgis.core import QgsCoordinateTransform, QgsProject

        geom = QgsGeometry(geometry)
        try:
            xform = QgsCoordinateTransform(
                src, dst, self.canvas.project() or QgsProject.instance()
            )
            if geom.transform(xform) == Qgis.GeometryOperationResult.Success:
                return geom
        except Exception:
            logger.exception("绘制几何坐标转换失败(%s→%s)", src.authid(), dst.authid())
        return geometry

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

        # 绘制几何在画布坐标系, 目标图层 CRS 不同时先转换(投影图层如 4527 必需)
        geometry = self._to_layer_geometry(layer, geometry)

        # 创建要素 + 弹属性对话框
        from qgis.core import QgsVectorLayerUtils


        feature = QgsVectorLayerUtils.createFeature(layer, geometry)
        feature.setGeometry(geometry)
        # 新增要素的属性预填：面积 / 周长类字段按刚画的几何算好、唯一标识字段
        # （FGUID / mzguid 等）生成新值、主键清空交给数据源——与分割 / 分解产出的
        # 新要素同一套规则（见 app.ui.tools.feature_attrs）。这样属性弹窗一打开
        # 看到的就是算好的面积和新 guid，不用用户手填。
        # 在 createFeature 给的属性（可能含字段默认值）上改，不整个覆盖。
        feature.setAttributes(
            new_feature_attributes(layer, list(feature.attributes()), geometry)
        )
        dlg = CustomAttrDialog(layer, feature=feature, parent=self.main_window)
        dlg.setWindowTitle(f"编辑{geom_name}要素属性")
        # 临时图形（刚画的那一笔）留到属性弹窗关闭再删：用户填属性时要能对着
        # 刚画的形状确认位置（见 DrawManager.finish_draw 的说明）
        dlg.finished.connect(lambda _result: self._release_draw_preview())
        dlg.show()

    def _release_draw_preview(self) -> None:
        """属性弹窗关闭后清掉绘制预览（临时图形）。"""
        if self.draw_manager is not None:
            try:
                self.draw_manager.clear_preview()
            except Exception:  # noqa: BLE001 —— 收尾失败不该影响弹窗关闭
                logger.exception("清掉绘制预览失败")
        if self.rubber_band is not None:
            try:
                self.rubber_band.reset()
            except Exception:  # noqa: BLE001
                logger.exception("清掉绘制预览橡皮筋失败")
            self.rubber_band = None

    def clear_draw_tool(self) -> None:
        """取消激活本工具条挂到画布的工具，并清掉共享 rubber_band + 临时点。

        保证「停止编辑 / 切换工具 / 取消」后画布不再挂着任何本工具条的激活
        工具（``active_tool`` / 要素编辑 / 属性录入 / 线分割 / 面分割）：

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

        _drop("feature_edit_tool")
        _drop("attribute_entry_tool")
        _drop("split_tool")
        _drop("split_polygon_tool")
        _drop("dig_hole_tool")
        _drop("fill_hole_tool")
        _drop("boundary_edit_tool")
        _drop("move_features_tool")
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
        # 激活种类先复位：下面恢复画布工具会**再次**触发 mapToolSet，届时
        # _on_canvas_map_tool_set 看到 _active_kind 已为 None 会直接返回，
        # 不会递归回到本方法
        self._active_kind = None
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
        # 工具退出 = 本次操作结束：把浮动提示也一并收掉（用户按 Esc 取消时，
        # 提示框还挂在屏幕上会像是"没取消干净"）
        self._dismiss_message_bar()
        # 按钮统一刷新（勾选全弹起）
        self._refresh_button_states()
        logger.info(
            "工具清理完成: mapTool=%s active_tool=%s feature=%s split=%s split_polygon=%s dig_hole=%s fill_hole=%s attr_entry=%s",
            type(self.canvas.mapTool()).__name__ if self.canvas.mapTool() is not None else None,
            self.active_tool,
            self.feature_edit_tool,
            self.split_tool,
            self.split_polygon_tool,
            self.dig_hole_tool,
            self.fill_hole_tool,
            self.attribute_entry_tool,
        )

    # ============================================================== 内部

    def _dismiss_message_bar(self) -> None:
        """收起浮动提示（工具退出 / 取消时用）。"""
        bar = getattr(self.main_window, "messageBar", None)
        if bar is None:
            return
        try:
            bar.dismiss()
        except Exception:  # noqa: BLE001 —— 收尾失败不该影响工具退出
            logger.exception("收起浮动提示失败")

    def _is_own_tool(self, tool) -> bool:
        """``tool``（或它的 ``.tool`` wrapper）是不是本工具条挂到画布上的。"""
        real = getattr(tool, "tool", tool)
        for attr in _OWN_TOOL_ATTRS:
            held = getattr(self, attr, None)
            if held is not None and real is getattr(held, "tool", held):
                return True
        return False

    def _on_canvas_map_tool_set(self, new_tool, _old_tool=None) -> None:
        """画布工具变化：新工具不是本工具条的 → 按「工具已自行退出」统一收尾。

        各编辑工具的 Esc 走的是 ``canvas.unsetMapTool(self)``（见 ``swipe.py``
        的仓库约束：不在事件栈内 unset 正在执行的地图工具），**不经过本工具条**，
        光靠 ``_active_kind`` 会留下「工具已经退出、按钮还按着、提示还挂着」的
        假状态。这里以画布真实工具为准兜底：按钮弹起、提示收掉、画布恢复到激活
        前的工具（``clear_draw_tool`` 内都已做）。

        注意用户在**别的**工具条上换了工具（如点侧栏「漫游」）时也会走到这儿——
        那正是我们要的：本工具条的激活态该跟着弹起。
        """
        if self._active_kind is None:
            return
        if new_tool is not None and self._is_own_tool(new_tool):
            return
        self.clear_draw_tool()

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
        # 撤销栈信号、选中集信号与停编钩子同生命周期：目标图层切换时一并换绑
        self._connect_undo_stack(layer)
        self._connect_selection_hook(layer)
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
        # 撤销栈信号、选中集信号与停编钩子同生命周期：一并断开
        self._disconnect_undo_stack()
        self._disconnect_selection_hook()
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

    def _connect_selection_hook(self, layer: QgsVectorLayer | None) -> None:
        """把工程内**所有矢量图层**的 ``selectionChanged`` 接到按钮状态重算。

        「边界修改」「移动要素」「删除」看的是**编辑图层**的选中集，而「要素分割」
        用的是**其它图层**上选中的切割要素——只监听编辑图层不够，必须工程级监听。
        :class:`QgsProject` 没有全局选择信号，所以逐层连，并跟随图层增删同步
        （见 :meth:`_sync_selection_listeners`）。

        与停编钩子同生命周期：切目标图层时重建。
        """
        self._disconnect_selection_hook()
        try:
            project = QgsProject.instance()
            project.layersAdded.connect(self._sync_selection_listeners)
            project.layersRemoved.connect(self._sync_selection_listeners)
            self._project_selection_connected = True
        except Exception:  # noqa: BLE001
            self._project_selection_connected = False
            logger.exception("连接工程图层增删信号失败")
        self._sync_selection_listeners()

    def _sync_selection_listeners(self, *_args) -> None:
        """让「已连接图层表」与工程当前矢量图层集合保持一致。"""
        try:
            project = QgsProject.instance()
            layers = {
                item.id(): item
                for item in project.mapLayers().values()
                if isinstance(item, QgsVectorLayer)
            }
        except Exception:  # noqa: BLE001
            return
        # 编辑图层未必在工程里（临时图层 / 测试桩），但它一定得被监听，
        # 否则它的选中变化推不动按钮状态
        current = self._current_layer()
        if isinstance(current, QgsVectorLayer):
            layers[current.id()] = current
        # 先摘掉已不在工程里的（图层可能已销毁，断开抛异常属正常收尾）
        for layer_id in list(self._selection_hooked):
            if layer_id in layers:
                continue
            stale = QgsProject.instance().mapLayer(layer_id)
            if stale is not None:
                try:
                    stale.selectionChanged.disconnect(self._on_selection_changed)
                except Exception:  # noqa: BLE001
                    pass
            self._selection_hooked.pop(layer_id, None)
        # 再接上新出现的
        for layer_id, item in layers.items():
            if layer_id in self._selection_hooked:
                continue
            try:
                item.selectionChanged.connect(self._on_selection_changed)
                self._selection_hooked[layer_id] = True
            except Exception:  # noqa: BLE001
                logger.warning("连接图层 selectionChanged 失败: %s", item.name(), exc_info=True)

    def _disconnect_selection_hook(self) -> None:
        """断开全部选中集钩子与工程图层增删信号（目标切换 / 收尾）。"""
        project = QgsProject.instance()
        if self._project_selection_connected:
            for signal in (project.layersAdded, project.layersRemoved):
                try:
                    signal.disconnect(self._sync_selection_listeners)
                except Exception:  # noqa: BLE001 —— 收尾场景属正常
                    pass
            self._project_selection_connected = False
        for layer_id in list(self._selection_hooked):
            layer = project.mapLayer(layer_id)
            if layer is not None:
                try:
                    layer.selectionChanged.disconnect(self._on_selection_changed)
                except Exception:  # noqa: BLE001 —— 图层销毁等收尾场景属正常
                    pass
        self._selection_hooked.clear()

    def _on_selection_changed(self) -> None:
        """选中集变化（任意图层）：重算按钮状态；依赖选中的工具失去选中即退出。

        工具作用的正是选中的图形，选中被清空后再挂着它没有意义——而且按钮会被
        :meth:`_refresh_button_states` 置灰，留下「按钮禁用却仍处激活态」的怪状态。

        **退出动作推迟出栈执行**：本方法常常是被图层 API 同步触发的（典型是
        「要素编辑」里按 Delete 删掉唯一的选中要素 → ``deleteFeatures`` 发
        ``selectionChanged``），此刻还在 Qt 事件栈 / 编辑命令窗口内，
        ``clear_draw_tool()`` 里的 ``unsetMapTool`` 会踩在这个栈上——仓库既定结论
        是这类调用一律 ``QTimer.singleShot(0, ...)`` 推迟出栈（见 ``swipe.py``
        / ``merge_geometry.py`` 的 docstring）。按钮状态仍立即刷新，不受影响。
        """
        if self._active_kind in _SELECTION_REQUIRED_KINDS:
            layer = self._current_layer()
            if layer is None or not layer.selectedFeatureIds():
                QTimer.singleShot(0, self._clear_tool_if_still_no_selection)
        self._refresh_button_states()

    def _clear_tool_if_still_no_selection(self) -> None:
        """推迟执行的「失去选中即退出工具」：执行时重新校验，不误清新选中的工具。"""
        if self._active_kind not in _SELECTION_REQUIRED_KINDS:
            return
        layer = self._current_layer()
        if layer is None or not layer.selectedFeatureIds():
            self.clear_draw_tool()

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

    def _require_selection(self, layer: QgsVectorLayer, action: str) -> bool:
        """「有选中」兜底校验：返回 False 时已弹过警告，调用方直接 return。

        编辑类工具的按钮可用性已按选中集置灰（见 :meth:`_refresh_button_states`），
        这里再挡一层，防止绕过按钮直接调用（如快捷键、脚本、测试桩）。
        """
        if layer.selectedFeatureIds():
            return True
        QMessageBox.warning(
            self.main_window, "警告", f"请先用点选 / 框选选中要{action}的要素"
        )
        return False

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
          * 「停止编辑 / 保存编辑」仅编辑中可用（会话控制，不作用于具体图形）；
          * **编辑类工具一律要求「有选中 + 编辑态」**——它们作用的正是选中的图形：
            要素编辑 / 线分割 / 面分割 / 分解 要至少选中 1 个；挖洞 / 补洞 另需面图层；
            合并 / 补缝 要选中 ≥ 2 个；边界修改 另需面图层；移动要素 / 删除 类型不限；
          * 「要素分割」仅编辑中且**其它图层有选中要素**时可用（它要的是切割要素，
            不是当前图层的选中集）；
          * 「撤销 / 重做」仅编辑中且当前图层撤销栈上有对应步骤
            （实时跟随 canUndoChanged / canRedoChanged）；
          * 「添加点/线/面」仅编辑中且图层几何匹配时可用（新建要素，不需要选中）；

        - checked(勾选=工具正激活)：仅激活工具对应的按钮按下，其余弹起。

        所有入口(开始/停止编辑、切图层、激活/清除/切换工具)只改状态字段后调
        本方法，不再散点手工 setEnabled/setChecked，避免按钮状态与真实状态脱节
        (历史 bug：工具已清除但「添加面」按钮仍呈激活/高亮态)。
        """
        layer = self._current_layer()
        is_vec = layer is not None and layer.type() == layer.VectorLayer
        editing = bool(is_vec and layer.isEditable())
        # 几何类型提前算：挖洞 / 补洞 / 「添加X」都要按类型区分可用性
        g_type = layer.geometryType() if is_vec else -1

        # 「开始编辑」恒可用(点击弹窗选层)；「属性录入」恒可用(激活时自动开编辑)
        self.start_edit.setEnabled(True)
        self.attribute_entry_action.setEnabled(True)
        # 会话控制：编辑中才可用（它们不作用于具体图形，不需要选中）
        for action in (self.stop_edit, self.save_edit):
            action.setEnabled(editing)
        # 编辑类工具一律要求「**有选中** + 编辑态」：它们作用的正是选中的图形，
        # 没选中就无从下手（唯一例外是「要素分割」，它要的是**其它图层**上的
        # 切割要素选中，见下）
        selected_fids = layer.selectedFeatureIds() if editing else []
        has_selection = bool(selected_fids)
        for action in (
            self.feature_edit_action,
            self.split_feature_action,
            self.split_polygon_action,
            self.explode_action,
        ):
            action.setEnabled(has_selection)
        # 挖洞 / 补洞：编辑中、为面图层**且有选中**才可用（画区域 / 点击只作用于选中要素）
        is_polygon = g_type == QgsWkbTypes.PolygonGeometry
        self.dig_hole_action.setEnabled(has_selection and is_polygon)
        self.fill_hole_action.setEnabled(has_selection and is_polygon)
        # 「合并」要两个及以上才谈得上合并，选中不足即置灰；「补缝」同理（缝隙在
        # 多个相邻面之间，一个面谈不上补缝）
        self.merge_geometry_action.setEnabled(has_selection and len(selected_fids) >= 2)
        self.gap_repair_action.setEnabled(is_polygon and len(selected_fids) >= 2)
        self.boundary_edit_action.setEnabled(is_polygon and has_selection)
        self.move_action.setEnabled(has_selection)
        # 「删除」删的是编辑图层的选中要素；「要素分割」要求**其它图层**上有选中的切割要素
        self.delete_action.setEnabled(has_selection)
        self.split_by_selection_action.setEnabled(
            bool(editing and has_cutter_selection(layer))
        )
        # 撤销 / 重做：编辑中且当前图层撤销栈上有对应步骤
        can_undo = can_redo = False
        if editing:
            try:
                stack = layer.undoStack()
                can_undo = bool(stack is not None and stack.canUndo())
                can_redo = bool(stack is not None and stack.canRedo())
            except RuntimeError:
                can_undo = can_redo = False  # 图层销毁中
        self.undo_action.setEnabled(can_undo)
        self.redo_action.setEnabled(can_redo)
        # 「添加X」：编辑中且与图层几何匹配才可用
        self.add_point_action.setEnabled(editing and g_type == QgsWkbTypes.PointGeometry)
        self.add_line_action.setEnabled(editing and g_type == QgsWkbTypes.LineGeometry)
        self.add_polygon_action.setEnabled(editing and g_type == QgsWkbTypes.PolygonGeometry)
        # 勾选互斥：仅激活工具对应的按钮按下
        for kind, action in self._tool_action_map.items():
            action.setChecked(kind == self._active_kind)
