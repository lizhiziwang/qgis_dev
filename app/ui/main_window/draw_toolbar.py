"""绘图 UI 装配（与旧 :class:`component.draw_tool.Draw` 行为一致）。

按旧版设计：

- 工具条平铺文本按钮（不用下拉菜单）：
  开始编辑 / 停止编辑 / 保存编辑 / 撤销 / 重做 / 合并 / 节点编辑 / 属性录入 /
  线分割 / 面分割 / 挖洞 / 补洞 / 分解 / 补缝 / 边界修改 / 移动要素 /
  删除 / 要素分割 ｜ 添加点 / 添加线 / 添加面
- 「开始编辑」**恒可用**：已经选过编辑目标时直接对该图层开编辑；没有目标时才
  弹图层选择框（:attr:`_edit_layer_chooser`，由 :class:`MainWindow` 注入）。
  编辑目标由工具条最左侧的「当前编辑图层」下拉框展示和切换——下拉框**只读**
  ``edit_layer``、不持有自己的状态，切换统一走 ``_apply_edit_target``。
  不再依赖图层树右键 / 点击预设编辑图层（那些入口已移除）。
- 几何类型按当前 layer 启用对应按钮（点 layer 只能点，线 layer 只能线）；
  **编辑类工具一律要求「先选中图形 + 图层处于编辑态」**——它们作用的正是选中的
  图形，没选中就无从下手，按钮置灰（唯一例外是「要素分割」，它要的选中集在
  **其它图层**上）。各工具的语义：
  「节点编辑」点击要素编辑其几何，见
  :class:`app.ui.tools.feature_edit.FeatureEditTool`；「属性录入」**不需**
  先开编辑——激活时若目标图层未编辑会自动进入编辑态（点击要素 → 弹窗
  编辑属性后确认，见
  :class:`app.ui.dialogs.attribute_entry.AttributeEntryDialog`）；
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

    [未编辑] --开始编辑(有目标) / 下拉框选层--> [编辑中]
        ^                                        |
        +----------------stop_edit_fun-----------+
    编辑中从下拉框换层 → 停旧层（脏则询问）→ 新层自动进编辑，会话连续

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
from qgis.PyQt import sip
from qgis.PyQt.QtCore import QObject, QSize, Qt, QTimer, pyqtSignal
from qgis.PyQt.QtGui import QAction, QColor
from qgis.PyQt.QtWidgets import (
    QComboBox,
    QDialog,
    QMainWindow,
    QMessageBox,
)

from app.core.domain.drawing import DrawManager
from app.core.domain.drawing.tools import get_tool_class
from app.infra.logging import get_logger
from app.infra.messagebar import send
from app.ui.common import tool_icons
from app.ui.common.modeless import show_modeless
from app.ui.common.ribbon import RibbonToolBar
from app.ui.common.theme import ThemeManager
from app.ui.dialogs.copy_to_layer import CopyToLayerDialog
from app.ui.dialogs.custom_attr import CustomAttrDialog
from app.ui.dialogs.node_list import NodeListDialog
from app.ui.dialogs.attribute_entry import AttributeEntryDialog
from app.ui.tools.attribute_entry import AttributeEntryTool
from app.ui.tools.boundary_edit import BoundaryEditTool
from app.ui.tools.copy_to_layer import copy_features_to as run_copy_to
from app.ui.tools.delete_features import delete_selected_features as run_delete
from app.ui.tools.dig_hole import DigHoleTool
from app.ui.tools.explode_parts import explode_features as run_explode
from app.ui.tools.feature_edit import FeatureEditTool
from app.ui.tools.fill_hole import FillHoleTool
from app.ui.tools.gap_repair import repair_gaps_interactive as run_gap_repair
from app.ui.tools.feature_attrs import new_feature_attributes
from app.ui.tools.merge_geometry import merge_features_interactive as run_merge
from app.ui.tools.move_features import MoveFeaturesTool
from app.ui.tools.node_list import NodeListTool
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
    "node_list_tool",
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
        #: 属性录入工具实例（防 GC）与弹窗（懒建单实例，关掉后复用）
        self.attribute_entry_tool = None
        self._attribute_entry_dialog = None
        #: 节点列表工具实例（防 GC）与弹窗（懒建单实例，关掉后复用）
        self.node_list_tool = None
        self._node_list_dialog = None

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
        #: 正在处理"编辑目标被隐藏"的退出流程（防止弹窗/重显可见性时重入）
        self._handling_hidden_target = False
        #: 已挂 ``selectionChanged`` 的图层 id 集合（工程级监听，见
        #: :meth:`_connect_selection_hook`）；工程量少，用集合去重即可。
        self._selection_hooked: dict[str, bool] = {}
        self._project_selection_connected = False

        self._build_toolbar()
        self._connect_map_tool_hook()
        # 选中集钩子是**工程级**的，跟着本工具条的生命周期走，与"有没有编辑目标"
        # 无关——早先它挂在 :meth:`_connect_edit_stop_hook` 里，于是没设过编辑目标
        # 时选要素根本不刷新按钮（「复制到」这种不要求编辑态的动作因此永远点不亮）。
        self._connect_selection_hook(None)

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

        # ---- 当前编辑图层（最左侧）：下拉框是 edit_layer 的显示器 + 切换入口。
        #      必须先于按钮加入，才能出现在工具条最左端 ----
        self._build_edit_layer_combo()

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

        # ---- 节点编辑（需处于编辑态；点击要素编辑其节点/顶点）----
        self.feature_edit_action = QAction("节点编辑", self.digitizing_toolbar)
        self.feature_edit_action.setCheckable(True)
        self.feature_edit_action.setToolTip(
            "先进入编辑并点此，再点击当前编辑图层上的图形：点击要素编辑其节点（顶点）"
        )
        self.feature_edit_action.triggered.connect(
            lambda checked=False: self.enable_feature_edit(checked)
        )

        # ---- 节点列表（需处于编辑态；点击要素弹出该要素的节点列表弹窗）----
        #     与节点编辑同属「几何」组，紧挨着排在它右边
        self.node_list_action = QAction("节点列表", self.digitizing_toolbar)
        self.node_list_action.setCheckable(True)
        self.node_list_action.setToolTip(
            "先进入编辑并点此，再点击当前编辑图层上的图形，弹出该图形的节点列表"
        )
        self.node_list_action.triggered.connect(
            lambda checked=False: self.enable_node_list(checked)
        )

        # ---- 属性录入（恒可用：无需编辑态，激活时自动开始编辑）----
        #     点击要素 → 弹窗改属性
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

        # ---- 复制到（**只要求有选中要素**，不要求编辑态；源 = 选中要素所在图层）----
        self.copy_to_action = QAction("复制到", self.digitizing_toolbar)
        self.copy_to_action.setToolTip(
            "用选择工具选中要素后点此，把要素复制到另一个图层（可逐字段搬运属性）"
        )
        self.copy_to_action.triggered.connect(self.copy_to_layer)

        # ---- 编辑设置（工具条最末尾；点击弹出面板，其「快捷键」页列编辑工具的快捷键）----
        self.shortcuts_action = QAction("编辑设置", self.digitizing_toolbar)
        self.shortcuts_action.setToolTip("编辑设置：查看 / 修改编辑工具栏下各工具的快捷键（改完保存即生效，可恢复默认）")
        self.shortcuts_action.triggered.connect(self.show_edit_settings_panel)

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
            "node_list": self.node_list_action,
            "split": self.split_feature_action,
            "split_polygon": self.split_polygon_action,
            "dig_hole": self.dig_hole_action,
            "fill_hole": self.fill_hole_action,
            "boundary_edit": self.boundary_edit_action,
            "move": self.move_action,
            "attr_entry": self.attribute_entry_action,
            "copy_to": self.copy_to_action,
        }

        # 按组挂到工具条：组间加竖分隔线，组名由 RibbonToolBar 画在每组按钮下方。
        # 第一组是下拉框（已经在 _build_edit_layer_combo 里 addWidget 加入），
        # 组名「当前编辑图层」与后面的「编辑 / 几何 / 分割」同一条基线。
        # 组名会随编辑状态动态变成「当前编辑图层（编辑中）」（见
        # _update_edit_layer_combo_state / RibbonToolBar.set_group_name），
        # 所以这里保留动作列表对象，供按身份定位该组。
        self._edit_layer_group_actions = [self._edit_layer_combo_action]
        tool_groups: list[tuple[str, list[QAction]]] = [
            ("当前编辑图层", self._edit_layer_group_actions),
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
                    self.feature_edit_action,
                    self.node_list_action,
                    self.attribute_entry_action,
                    self.merge_geometry_action,
                    self.boundary_edit_action,
                    self.move_action,
                    self.delete_action,
                    self.copy_to_action,
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
            (
                "其它",
                [self.shortcuts_action],
            ),
        ]
        for idx, (_group_name, acts) in enumerate(tool_groups):
            for act in acts:
                # 下拉框的 action 已由 addWidget 加入；再 addAction 会把它挪到
                # 工具条末尾（Qt 会重排），必须跳过
                if act is self._edit_layer_combo_action:
                    continue
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
            self.feature_edit_action: "node_edit",
            self.node_list_action: "node_list",
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
            self.copy_to_action: "copy_to_layer",
            self.shortcuts_action: "shortcut",
        }
        self._apply_icons()

        # 初始态由统一出口推导（无目标图层 → 仅「开始编辑」/「属性录入」可用）
        self._refresh_button_states()

        # 挂到主窗口
        self.main_window.addToolBar(self.digitizing_toolbar)

    # ============================================================== 编辑图层下拉框

    def _build_edit_layer_combo(self) -> None:
        """工具条最左侧的「当前编辑图层」下拉框。

        它**只读** ``edit_layer``，不保存第二份状态：显示由
        :meth:`sync_edit_layer_combo` 从真实目标重画；用户选择时交给
        :meth:`_on_edit_layer_activated` 走 ``_apply_edit_target`` 统一落地。

        布局：下拉框作为一个独立分组加入工具条，QToolBar 把它在按钮行里垂直
        居中；组名「当前编辑图层」和「编辑 / 几何 / 分割」一样由
        :class:`RibbonToolBar` 画在下方的组名带里。
        """
        combo = QComboBox(self.digitizing_toolbar)
        combo.setObjectName("EditLayerComboBox")
        combo.setFixedWidth(150)
        combo.setSizeAdjustPolicy(
            QComboBox.SizeAdjustPolicy.AdjustToMinimumContentsLengthWithIcon
        )
        combo.setMinimumContentsLength(8)
        # index=-1 时显示这个占位（Qt 5.15+ / Qt6 支持）
        combo.setPlaceholderText("未选择编辑图层")
        # 用 activated（只在用户选择时发）而不是 currentIndexChanged，
        # 避免同步列表时程序性 setCurrentIndex 触发一次假的"切换"
        combo.activated.connect(self._on_edit_layer_activated)
        self.edit_layer_combo = combo
        # addWidget 返回的 QAction 就是这一组的"按钮"；组名由 RibbonToolBar
        # 依据它的几何画在下方（见 tool_groups 的第一组）
        self._edit_layer_combo_action = self.digitizing_toolbar.addWidget(combo)

        #: 已挂 nameChanged 的图层 id（工程级，随候选增删同步）
        self._combo_name_hooked: set[str] = set()
        #: 已挂 visibilityChanged 的树节点（按 C++ 指针去重）
        self._combo_visibility_hooked: set[int] = set()
        #: 当前挂了 addedChildren/removedChildren 的图层树根（读工程后根对象可能换）
        self._combo_tree_root = None
        try:
            project = QgsProject.instance()
            project.layersAdded.connect(self._on_combo_project_layers_changed)
            project.layersRemoved.connect(self._on_combo_project_layers_changed)
            # 工程读入**完成**后图层树才是最终顺序：读的过程中 layersAdded
            # 会连续触发，但组/图层的最终排序还没成型；不在这里补一次，
            # 首次打开工程后下拉框顺序会停在"读了一半"的树序，直到下次
            # 增删图层才自愈（用户报过）。readProject 在读取完成后发。
            project.readProject.connect(self._on_combo_project_read)
            self._connect_combo_tree_hooks()
        except Exception:  # noqa: BLE001 —— 画布桩 / 旧构建降级
            logger.exception("连接工程图层增删信号失败（编辑图层下拉框不会自动刷新）")

        self.sync_edit_layer_combo()

    def _connect_combo_tree_hooks(self) -> None:
        """把根组的 ``addedChildren`` / ``removedChildren`` 接到下拉框刷新。

        图层在面板里上下移动不会发工程级 ``layersAdded/Removed``；根组信号能
        覆盖顶层重排。读工程后 ``layerTreeRoot()`` 可能换成新对象，所以这个
        方法在初始化与 ``readProject`` 后都会调用。
        """
        try:
            root = QgsProject.instance().layerTreeRoot()
        except RuntimeError:
            return
        if root is None or root is self._combo_tree_root:
            # 根没换也可能新增了图层 / 组：补挂可见性信号
            self._hook_combo_visibility()
            return
        old = self._combo_tree_root
        if old is not None and not sip.isdeleted(old):
            for signal_name in ("addedChildren", "removedChildren"):
                try:
                    getattr(old, signal_name).disconnect(self._on_combo_project_layers_changed)
                except Exception:  # noqa: BLE001 —— 旧根可能已销毁
                    pass
        # 根对象换了：旧树节点已销毁，可见性去重集合重置
        self._combo_visibility_hooked.clear()
        try:
            root.addedChildren.connect(self._on_combo_project_layers_changed)
            root.removedChildren.connect(self._on_combo_project_layers_changed)
            self._combo_tree_root = root
        except Exception:  # noqa: BLE001
            logger.exception("连接图层树根节点信号失败（下拉框不会跟随图层重排）")
        self._hook_combo_visibility()

    def _hook_combo_visibility(self) -> None:
        """给图层树根 / 组 / 图层挂 ``visibilityChanged`` → 刷新编辑列表。

        编辑列表只列可见图层，所以可见性一变化就要重画。注意**组勾选不会给
        子图层逐个发 visibilityChanged**（实测），必须把组节点自己也挂上，
        再用有效可见性（``node.isVisible()``）重算。
        """
        try:
            root = QgsProject.instance().layerTreeRoot()
            if root is None:
                return
            nodes = [root, *root.findGroups(), *root.findLayers()]
        except RuntimeError:
            return
        for node in nodes:
            try:
                key = int(sip.unwrapinstance(node))
            except (TypeError, RuntimeError):
                continue
            if key in self._combo_visibility_hooked:
                continue
            try:
                node.visibilityChanged.connect(self._on_combo_visibility_changed)
                self._combo_visibility_hooked.add(key)
            except (RuntimeError, AttributeError):
                continue

    @staticmethod
    def _tree_node_visible(layer) -> bool:
        """图层在图层树里的**有效**可见性（不存在于树里按不可见）。"""
        if layer is None or sip.isdeleted(layer):
            return False
        try:
            node = QgsProject.instance().layerTreeRoot().findLayer(layer.id())
        except (AttributeError, RuntimeError):
            return False
        if node is None:
            return False
        try:
            return bool(node.isVisible())
        except RuntimeError:
            return False

    def _on_combo_visibility_changed(self, *_args) -> None:
        """任何图层 / 组显隐变化 → 刷新编辑列表。

        隐藏的普通图层直接移出列表；**隐藏的是当前编辑目标**时不做"自动保持
        显示"，而是按用户要求退出编辑：有未保存修改先问保存 / 放弃 / 取消，
        然后清空编辑目标（图层保持隐藏）。
        """
        try:
            self._hook_combo_visibility()
            layer = self._current_layer()
            if layer is not None and not self._tree_node_visible(layer):
                self._detach_hidden_edit_target(layer)
            self.sync_edit_layer_combo()
            # 组勾选 / 取消退出后重新显示目标等场景，有效可见性可能晚一拍；
            # 再推迟一次刷新，避免列表少了刚显示出来的图层（实测组显隐）
            QTimer.singleShot(0, self.sync_edit_layer_combo)
        except Exception:  # noqa: BLE001 —— Qt 槽抛异常会直接杀进程
            logger.exception("图层显隐变化后刷新编辑列表失败")

    def _detach_hidden_edit_target(self, layer) -> None:
        """编辑目标被隐藏：退出编辑（有改动先问保存）并清空编辑目标。

        - 有未保存修改：弹「保存 / 放弃 / 取消」；取消 → 恢复图层显示并继续编辑；
        - 无修改 / 已退出编辑：直接清空目标（图层保持隐藏）；
        - 保存失败：重新显示图层并提示，保持编辑态，避免静默丢数据。
        """
        if self._handling_hidden_target:
            return
        self._handling_hidden_target = True
        try:
            keep_visible = False
            if layer.isEditable():
                if layer.isModified():
                    box = QMessageBox(self.main_window)
                    box.setWindowTitle("隐藏编辑图层")
                    box.setIcon(QMessageBox.Icon.Warning)
                    box.setText(f"图层「{layer.name()}」已隐藏，且还有未保存的修改。")
                    box.setInformativeText("退出编辑前请先处理这些修改：")
                    save_btn = box.addButton("保存", QMessageBox.ButtonRole.AcceptRole)
                    drop_btn = box.addButton("放弃", QMessageBox.ButtonRole.DestructiveRole)
                    cancel_btn = box.addButton("取消", QMessageBox.ButtonRole.RejectRole)
                    box.setDefaultButton(save_btn)
                    box.setEscapeButton(cancel_btn)
                    box.exec()
                    clicked = box.clickedButton()
                    if clicked is cancel_btn or clicked is None:
                        # 取消 / 关窗：不退出编辑，把图层恢复显示（否则编辑目标
                        # 会处于"隐藏但仍在编辑"的矛盾状态）
                        keep_visible = True
                    elif clicked is save_btn:
                        try:
                            layer.commitChanges(stopEditing=True)
                        except Exception:  # noqa: BLE001
                            logger.exception("隐藏编辑图层时保存失败: %s", layer.name())
                        if layer.isEditable():
                            QMessageBox.warning(
                                self.main_window,
                                "保存失败",
                                "保存编辑失败，图层仍在编辑状态，已重新显示。\n"
                                f"失败原因：\n{self._commit_error_text(layer)}",
                            )
                            keep_visible = True
                    else:
                        # 放弃：回滚并结束编辑（rollBack 会发 editingStopped）
                        try:
                            layer.rollBack()
                        except Exception:  # noqa: BLE001
                            logger.exception("隐藏编辑图层时回滚失败: %s", layer.name())
                        if layer.isEditable():
                            keep_visible = True
                else:
                    # 没有改动：直接结束编辑，不弹保存框
                    try:
                        layer.commitChanges(stopEditing=True)
                    except Exception:  # noqa: BLE001
                        logger.exception("隐藏编辑图层时结束编辑失败: %s", layer.name())
                    if layer.isEditable():
                        keep_visible = True

            if keep_visible:
                ensure = getattr(self.main_window, "_ensure_layer_visible", None)
                if callable(ensure):
                    try:
                        ensure(layer)
                    except Exception:  # noqa: BLE001
                        logger.exception("恢复编辑图层可见性失败")
                return

            # 编辑会话已结束（或本来就没编辑）：切到下一个可见可编辑图层。
            # 用户要求：隐藏当前目标后自动切到另一个；一个都没有才清空目标。
            next_layer = self._first_visible_candidate(exclude=layer)
            if next_layer is not None:
                applier = getattr(self.main_window, "_apply_edit_target", None)
                switched = False
                if callable(applier):
                    try:
                        switched = bool(applier(next_layer, announce=False))
                    except Exception:  # noqa: BLE001
                        logger.exception("切换到备用编辑图层失败")
                if not switched:
                    try:
                        self.main_window.edit_layer = next_layer
                        switched = True
                    except Exception:  # noqa: BLE001
                        logger.exception("切换到备用编辑图层失败（直接赋值）")
                if switched:
                    self._toast(
                        f"编辑图层「{layer.name()}」已隐藏并退出编辑，"
                        f"已切换到「{next_layer.name()}」",
                        level="info",
                    )
                else:
                    try:
                        self.main_window.edit_layer = None
                    except Exception:  # noqa: BLE001
                        logger.exception("清空已隐藏的编辑图层失败")
            else:
                try:
                    self.main_window.edit_layer = None
                except Exception:  # noqa: BLE001 —— 宿主不是主窗口时忽略
                    logger.exception("清空已隐藏的编辑图层失败")
                self._toast(
                    f"编辑图层「{layer.name()}」已隐藏，当前无可编辑图层",
                    level="info",
                )
            QTimer.singleShot(0, self.sync_edit_layer_combo)
        finally:
            self._handling_hidden_target = False

    def _first_visible_candidate(self, exclude=None):
        """当前可见可编辑图层里的第一个（排除 ``exclude`` 自己）。"""
        try:
            candidates = self._combo_candidate_layers()
        except Exception:  # noqa: BLE001
            logger.exception("读取备用编辑图层失败")
            return None
        excluded_id = None
        if exclude is not None and not sip.isdeleted(exclude):
            try:
                excluded_id = exclude.id()
            except RuntimeError:
                excluded_id = None
        for cand in candidates:
            try:
                cand_id = cand.id()
            except RuntimeError:
                continue
            if excluded_id is not None and cand_id == excluded_id:
                continue
            return cand
        return None

    def _on_combo_project_read(self, *_args) -> None:
        """工程读取完成：重连可能换掉的根节点，并按最终图层树顺序刷新下拉框。"""
        try:
            self._connect_combo_tree_hooks()
            self.sync_edit_layer_combo()
        except Exception:  # noqa: BLE001 —— Qt 槽抛异常会直接杀进程
            logger.exception("工程读取后刷新编辑图层下拉框失败")

    def sync_edit_layer_combo(self) -> None:
        """按真实状态重画下拉框：候选图层、当前目标、编辑中标记。

        所有刷新入口（edit_layer 赋值、图层增删 / 改名、编辑开始 / 结束、
        下拉框切换完成）都只调这一个方法；它不持有任何自己的状态。
        """
        combo = getattr(self, "edit_layer_combo", None)
        if combo is None or sip.isdeleted(combo):
            return
        layer = self._current_layer()
        if layer is not None and (sip.isdeleted(layer) or not layer.isValid()):
            layer = None
        candidates = self._combo_candidate_layers()
        # 编辑列表默认选中一个：没有目标（首次启动 / 读工程 / 上一个目标被隐藏且
        # 没有可切换的）时，自动选第一个可见可编辑图层。只选目标，不开始编辑。
        if layer is None and candidates:
            try:
                self.main_window.edit_layer = candidates[0]
            except Exception:  # noqa: BLE001 —— 宿主不是主窗口时忽略
                logger.exception("默认选择编辑图层失败")
            layer = self._current_layer()
            if layer is not None and (sip.isdeleted(layer) or not layer.isValid()):
                layer = None
        # 没有任何可见可编辑图层：明确显示"无可编辑图层"，而不是"未选择"
        combo.setPlaceholderText("无可编辑图层" if not candidates else "未选择编辑图层")

        was_blocked = combo.blockSignals(True)
        try:
            wanted = [c.id() for c in candidates]
            existing = [combo.itemData(i) for i in range(combo.count())]
            if wanted != existing:
                combo.clear()
                for cand in candidates:
                    combo.addItem(cand.name(), cand.id())
            # 顺序没变也要刷新文字：图层可能被改名
            for i, cand in enumerate(candidates):
                if i < combo.count():
                    combo.setItemText(i, cand.name())
            combo.setCurrentIndex(combo.findData(layer.id()) if layer is not None else -1)
        finally:
            combo.blockSignals(was_blocked)

        self._sync_combo_name_hooks(candidates)
        self._update_edit_layer_combo_state(layer)

    def _combo_candidate_layers(self) -> "list[QgsVectorLayer]":
        """下拉框候选：与「开始编辑」选层弹窗同一套过滤（可见 + 可添加要素）。"""
        layers = None
        getter = getattr(self.main_window, "_editable_layers", None)
        if callable(getter):
            try:
                layers = list(getter())
            except Exception:  # noqa: BLE001
                logger.exception("读取可编辑图层清单失败")
        if layers is None:
            # 测试桩 / 非主窗口宿主：退回工程里全部矢量图层
            try:
                layers = [
                    lyr
                    for lyr in QgsProject.instance().mapLayers().values()
                    if isinstance(lyr, QgsVectorLayer)
                ]
            except RuntimeError:
                layers = []
        return [
            lyr
            for lyr in layers
            if lyr is not None
            and not sip.isdeleted(lyr)
            and lyr.isValid()
            and self._tree_node_visible(lyr)
        ]

    def _sync_combo_name_hooks(self, candidates) -> None:
        """给新出现的候选图层挂 ``nameChanged``，改名时刷新下拉框文字。"""
        hooked = getattr(self, "_combo_name_hooked", None)
        if hooked is None:
            hooked = self._combo_name_hooked = set()
        live: set[str] = set()
        for layer in candidates:
            layer_id = layer.id()
            live.add(layer_id)
            if layer_id in hooked:
                continue
            try:
                layer.nameChanged.connect(self._on_combo_layer_name_changed)
                hooked.add(layer_id)
            except Exception:  # noqa: BLE001 —— 图层收尾 / 桩对象
                logger.warning("连接图层 nameChanged 失败: %s", layer.name(), exc_info=True)
        # 已移除的图层不再跟踪（C++ 对象销毁时连接会自动断开）
        hooked.intersection_update(live)

    def _update_edit_layer_combo_state(self, layer: "QgsVectorLayer | None") -> None:
        """刷新编辑状态的表达：组名 + tooltip。

        组名动态显示「当前编辑图层（编辑中）」/「当前编辑图层」，与
        「编辑 / 几何 / 分割」同一套画法；tooltip 再补充具体图层名。
        """
        combo = getattr(self, "edit_layer_combo", None)
        if combo is None or sip.isdeleted(combo):
            return
        editing = bool(
            layer is not None and not sip.isdeleted(layer) and layer.isEditable()
        )
        group_actions = getattr(self, "_edit_layer_group_actions", None)
        toolbar = getattr(self, "digitizing_toolbar", None)
        if group_actions is not None and isinstance(toolbar, RibbonToolBar):
            try:
                toolbar.set_group_name(
                    group_actions,
                    "当前编辑图层（编辑中）" if editing else "当前编辑图层",
                )
            except Exception:  # noqa: BLE001 —— 组名刷新失败不该影响编辑操作
                logger.exception("刷新编辑图层组名失败")
        if layer is None:
            combo.setToolTip("未选择编辑图层：从下拉框选择要编辑的图层")
        elif editing:
            combo.setToolTip(f"正在编辑：{layer.name()}")
        else:
            combo.setToolTip(f"编辑目标：{layer.name()}（尚未开始编辑）")

    def _on_combo_layer_name_changed(self, *_args) -> None:
        """图层改名 → 刷新下拉框文字（槽里兜异常，避免 Qt 直接杀进程）。"""
        try:
            self.sync_edit_layer_combo()
        except Exception:  # noqa: BLE001
            logger.exception("图层改名后刷新编辑图层下拉框失败")

    def _on_combo_project_layers_changed(self, *_args) -> None:
        """图层增删 / 顺序变化：候选和顺序都变了，重画；删掉的是目标就清目标。

        正常删除走 ``layer_actions._remove_layer`` 会先停编辑再清 ``edit_layer``；
        这里兜住脚本 / 其它入口直接 ``removeMapLayer`` 的情况，避免工具条和
        按钮状态继续引用已销毁的 C++ 对象。槽里统一兜异常（Qt 槽抛异常会
        直接结束进程）。
        """
        try:
            self._hook_combo_visibility()
            layer = self._current_layer()
            if layer is not None and sip.isdeleted(layer):
                try:
                    self.main_window.edit_layer = None
                except Exception:  # noqa: BLE001 —— 宿主不是主窗口时忽略
                    logger.exception("清理已删除的编辑图层目标失败")
            self.sync_edit_layer_combo()
        except Exception:  # noqa: BLE001
            logger.exception("工程图层变化后刷新编辑图层下拉框失败")

    def _on_edit_layer_activated(self, index: int) -> None:
        """用户从下拉框选了图层：按「是否正在编辑」决定要不要立刻开编辑。

        - 当前没有编辑会话 → 只切目标（不提示「已选 X 为编辑图层」，等用户点
          「开始编辑」时再提示）；
        - 当前正在编辑 → 停旧层（脏 buffer 由 ``_apply_edit_target`` 弹
          保存/放弃/取消）→ 新目标开始编辑，会话连续；因为会自动进入编辑，
          这时才提示「已选 X 为编辑图层」。
        """
        try:
            layer_id = self.edit_layer_combo.itemData(index)
            layer = QgsProject.instance().mapLayer(layer_id) if layer_id else None
            if layer is None or sip.isdeleted(layer) or not layer.isValid():
                self.sync_edit_layer_combo()
                return
            prev = self._current_layer()
            if layer is prev:
                return
            was_editing = bool(
                prev is not None and not sip.isdeleted(prev) and prev.isEditable()
            )
            applier = getattr(self.main_window, "_apply_edit_target", None)
            # 未编辑时只是"预选目标"：不提示「已选 X 为编辑图层」，等用户点
            # 「开始编辑」再提示；编辑中切层会自动进入编辑，才提示。
            if not callable(applier) or not applier(layer, announce=was_editing):
                # 用户取消 / 旧图层保存失败：回显真实目标，不能停在下拉框的假选择上
                self.sync_edit_layer_combo()
                return
            if was_editing or layer.isEditable():
                # 失败（数据源锁等）由 _ensure_editing 自己弹提示并保持真实状态
                self._ensure_editing(layer)
            self.sync_edit_layer_combo()
        except Exception:  # noqa: BLE001 —— Qt 槽里抛异常会直接杀进程
            logger.exception("切换编辑图层失败")
            self.sync_edit_layer_combo()

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
        """开始编辑：直接对「当前编辑图层」（下拉框显示的图层）开编辑。

        只有还没选过目标图层时才弹选层框（首次使用 / 工程里刚加数据）。
        目标已经由下拉框 / 上次编辑记忆下来时，不再重复弹窗 —— 否则用户在
        下拉框选完层，点开始编辑又被问一遍，两个入口互相打架。
        开始编辑成功后提示「已选 X 为编辑图层」（下拉框预选阶段刻意不提示）。
        """
        layer = self._current_layer()
        chooser = self._edit_layer_chooser
        #: 选层框路径会在 _apply_edit_target 里提示；已有目标的路径这里补提示
        announced = False
        if layer is None and chooser is not None:
            # 弹窗选层（纯选择，见 main_window._choose_edit_layer）；取消返回
            # None → 不做任何事
            layer = chooser()
            if layer is None:
                return
            # 落地为编辑目标：edit_layer / 停旧层 / 工具条 / 捕捉联动。
            # 若 chooser 本身已同步过（getter 已指向选中层）则跳过，幂等安全。
            if self._current_layer() is not layer:
                applier = getattr(self.main_window, "_apply_edit_target", None)
                # 旧编辑图层有未提交改动时 applier 会弹「保存/放弃/取消」；
                # 用户选了取消 → 整个换层中止，别再往下进编辑态
                if callable(applier):
                    if not applier(layer):
                        self._refresh_button_states()
                        return
                    announced = True
        if layer is None or layer.type() != layer.VectorLayer:
            return
        if not self._ensure_editing(layer):
            return
        if not announced:
            # 下拉框预先选好目标（未编辑时不提示），真正点「开始编辑」后才提示
            announcer = getattr(self.main_window, "_announce_edit_layer", None)
            if callable(announcer):
                try:
                    announcer(layer)
                except Exception:  # noqa: BLE001 —— 提示失败不该影响编辑
                    logger.exception("提示编辑图层失败")
            else:
                self._toast(f"已选 {layer.name()} 为编辑图层")

    def _ensure_editing(self, layer: QgsVectorLayer) -> bool:
        """确保 ``layer`` 进入编辑态并同步工具条状态机。

        - 已在编辑态：若状态机未同步（第三方/其它入口先开编辑）则补齐同步；
        - 未编辑：调用 ``layer.startEditing()``，成功后同步按钮组并发出
          :pyattr:`editing_state_changed`。

        供「开始编辑」「属性录入」（自动开编辑）共用。

        :returns: 是否处于（已进入）编辑态。
        """
        # 挂停编 / 起编钩子：本工具条开始的编辑，不管之后由谁结束都能兜底清理
        self._connect_edit_stop_hook(layer)
        # 目标图层可能是下拉框预选的隐藏图层：画布上看不到要素，点选工具也只
        # 查可见图层，会表现为"拖节点没反应 / 选中不了 / 分割按钮不亮"。
        # 真正进入编辑前先把它（和所在组）显示出来。
        ensure_visible = getattr(self.main_window, "_ensure_layer_visible", None)
        if callable(ensure_visible):
            try:
                if ensure_visible(layer):
                    bar = getattr(self.main_window, "messageBar", None)
                    if bar is not None:
                        bar.pushMessage(
                            f"编辑图层「{layer.name()}」原为隐藏，已自动显示",
                            level=Qgis.MessageLevel.Info,
                            duration=3,
                        )
            except Exception:  # noqa: BLE001 —— 显示失败不该挡住编辑
                logger.exception("确保编辑图层可见失败: %s", layer.name())
        if layer.isEditable():
            # 第三方 / 其它入口先开了编辑：只补齐状态机，不重复发信号
            if not self._editing:
                self._editing = True
                self.editing_state_changed.emit(True)
            self._refresh_button_states()
            self.sync_edit_layer_combo()
            return True

        ok = layer.startEditing()
        if not ok:
            QMessageBox.warning(self.main_window, "警告", "无法开始编辑！")
            self.sync_edit_layer_combo()
            return False
        # editingStarted 钩子通常已经把 _editing 置 True 并发过信号；这里只兜底
        # 钩子没接上的情况，避免同一状态发两次 editing_state_changed(True)
        if not self._editing:
            self._editing = True
            self.editing_state_changed.emit(True)
        self._refresh_button_states()
        self.sync_edit_layer_combo()
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

        **是否询问保存看 ``layer.isModified()``，不是 ``editBuffer()``**：后者从
        ``startEditing()`` 成功起就非空，什么都不改也会弹保存框（实测）。
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
            if layer.isModified():
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
                # 没有任何未提交改动：不必问保存，直接结束编辑；工具照常退出。
                # 判据必须是 isModified() 而不是 editBuffer() —— 后者从
                # startEditing() 成功起就非空，什么都不改也会弹保存框（实测）。
                self.clear_draw_tool()
                try:
                    layer.commitChanges(stopEditing=True)
                except Exception:  # noqa: BLE001
                    logger.exception("停止编辑（无改动）提交异常: layer=%s", layer.name())
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
          「是否仍在编辑」推导：离开编辑态后「停止/保存/合并/节点编辑/线分割/
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
        # 编辑态真实变化：下拉框的「编辑中」标记必须跟着走（外部停编也走这里）
        self.sync_edit_layer_combo()

    def _save_edits(self) -> None:
        """仅保存编辑，不停止。

        ``commitChanges`` 失败（唯一约束 / 数据源锁 / 字段类型等）时**必须告诉
        用户**：静默失败会让用户以为已经落盘，实际改动还留在编辑缓冲里，直到
        某次停止编辑 / 关工程才暴露。
        """
        layer = self._current_layer()
        if layer is None or not layer.isEditable():
            return
        if not layer.isModified():
            self._toast("没有需要保存的修改", level="info")
            return
        try:
            saved = layer.commitChanges(stopEditing=False)
        except Exception:  # noqa: BLE001 —— 数据源异常按保存失败处理
            logger.exception("保存编辑异常: layer=%s", layer.name())
            saved = False
        if saved:
            self._toast("编辑内容已保存")
        else:
            QMessageBox.warning(
                self.main_window,
                "保存失败",
                "保存编辑失败，更改尚未写入数据源，仍处于编辑状态！\n"
                "请修正问题后重新保存，或点击「停止编辑」选择「否」放弃更改。\n\n"
                f"失败原因：\n{self._commit_error_text(layer)}",
            )
        # 保存成功会清空撤销栈（canUndoChanged 也会触发刷新），这里统一再刷一次；
        # 失败时编辑态未变，按钮状态同样不能因「做过一次保存」而漂移
        self._refresh_button_states()

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
        """激活「节点编辑」工具：点击矢量要素编辑其几何（顶点级）。

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
        # 不再要求「先选中要素」：本工具自己在地图上点哪个图形就编哪个，
        # 与「节点列表」一致——连续改多个图形时不必来回选
        # 清掉可能激活的绘制工具，避免两个工具并存
        self.clear_draw_tool()


        self.feature_edit_tool = FeatureEditTool(
            self.canvas,
            editing_layer_getter=self._editing_layer_getter,
            hint=self._tool_hint,
            visible_filter_getter=getattr(self.main_window, "display_filter_for", None),
        )
        self._activate_map_tool(self.feature_edit_tool)
        self._mark_active("feature")
        self._toast(
            "已激活「节点编辑」工具：点击要素进入节点编辑（拖动节点 / 双击边加节点 / 右键删节点）；"
            "Esc 退出"
        )

    def enable_node_list(self, checked: bool = True) -> None:
        """激活「节点列表」工具：点击当前编辑图层上的图形 → 弹出其节点列表弹窗。

        **只有已处于编辑态才可用**（用户明确约束），所以守卫比「属性录入」
        （恒可用、激活时自动开始编辑）更严：不满足条件就提示并复位按钮。

        按需求，**按钮按下不开弹窗** —— 弹窗在点图形时由
        :meth:`_load_node_list_feature` 打开。
        """
        if not checked:
            self.clear_draw_tool()
            return
        layer = self._current_layer()
        if layer is None or layer.type() != layer.VectorLayer:
            self._refresh_button_states()
            QMessageBox.warning(self.main_window, "警告", "请先选择矢量图层")
            return
        if not layer.isEditable():
            self._refresh_button_states()
            QMessageBox.warning(self.main_window, "提示", "请先开始编辑！")
            return
        if layer.geometryType() != QgsWkbTypes.PolygonGeometry:
            self._refresh_button_states()
            QMessageBox.warning(self.main_window, "提示", "节点列表只支持面图层")
            return
        self.clear_draw_tool()
        self.node_list_tool = NodeListTool(
            self.canvas,
            editing_layer_getter=self._editing_layer_getter,
            on_feature=self._load_node_list_feature,
            hint=self._tool_hint,
        )
        self._activate_map_tool(self.node_list_tool)
        self._mark_active("node_list")
        self._toast(
            "已激活「节点列表」工具：点击当前编辑图层上的图形查看其节点列表；Esc 退出"
        )

    def _load_node_list_feature(self, layer, fid) -> None:
        """地图工具命中图形：打开 / 复用节点列表弹窗并载入该要素。"""
        dlg = self._node_list_dialog
        if dlg is None or sip.isdeleted(dlg):
            dlg = NodeListDialog(self.canvas, toast=self._toast, parent=self.main_window)
            self._node_list_dialog = dlg
        dlg.load_feature(layer, fid)
        if dlg.isMinimized():
            dlg.showNormal()
        else:
            dlg.show()
        dlg.raise_()
        # 不调 activateWindow()：用户接着还要在地图上点下一个图形 / 按 Delete，
        # 抢焦点会打断这个流程

    def _close_node_list_dialog(self) -> None:
        """关掉节点列表弹窗（幂等）。工具退出时统一走这里收口。"""
        dlg = self._node_list_dialog
        if dlg is None or sip.isdeleted(dlg):
            return
        try:
            dlg.close()
        except Exception:  # noqa: BLE001 —— 收尾失败不该影响工具清理
            logger.exception("关闭节点列表弹窗失败")

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
            # 走浮动提示条：删除这条链上不该再弹模态框打断操作
            self._toast("请先用点选 / 框选选中要删除的要素", level="info")
            return

        deleted = run_delete(
            layer,
            hint=self._tool_hint,
        )
        if deleted is None:
            return  # 失败原因已由下游落地
        if deleted == 0:
            self._toast("没有可删除的选中要素", level="info")
            return
        self._toast(f"已删除 {deleted} 个要素（可 Ctrl+Z 撤销）")
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

        def _gap_done(result) -> None:
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

        result = run_gap_repair(
            layer,
            canvas=self.canvas,
            parent=self.main_window,
            hint=self._tool_hint,
            on_done=_gap_done,
        )
        if result is not None:
            _gap_done(result)  # 没检出缝隙时的同步结果

    def copy_to_layer(self) -> None:
        """「复制到」一次性动作：把选中要素复制到另一个图层（可搬运字段属性）。

        **只要求有选中要素**，不要求编辑态 —— 这是编辑工具条里唯一的例外：源是
        「选中要素**所在的**图层」（对齐 B/S），它未必是当前编辑图层；而目标图层
        由本工具自己开编辑缓冲（见 :func:`app.ui.tools.copy_to_layer.copy_features_to`）。
        """
        # 源图层：画布渲染顺序里**最上面那个有选中要素**的图层（同「点选只取最顶层」
        # 的规则）；B/S 取的是"第一个选中要素所属图层"，桌面端选择跨图层、没有可靠的
        # "第一个"，按渲染顺序取最上面的更有确定性。
        source = next(
            (
                layer
                for layer in self.canvas.layers()
                if isinstance(layer, QgsVectorLayer)
                and layer.isValid()
                and layer.selectedFeatureIds()
            ),
            None,
        )
        if source is None:
            QMessageBox.warning(self.main_window, "警告", "请先用选择工具选中要复制的要素")
            return

        # 目标候选：工程里的矢量图层，排除源图层自己、且几何类型要相同（对齐 B/S）
        targets = [
            layer
            for layer in self.canvas.layers()
            if isinstance(layer, QgsVectorLayer)
            and layer.isValid()
            and layer.id() != source.id()
            and layer.geometryType() == source.geometryType()
        ]
        if not targets:
            QMessageBox.warning(
                self.main_window,
                "警告",
                f"没有可作为目标的图层：需要几何类型与「{source.name()}」相同的其它矢量图层",
            )
            return

        def _apply_copy(dlg) -> None:
            try:
                if not source.selectedFeatureIds():
                    QMessageBox.warning(
                        self.main_window,
                        "复制到失败",
                        "源图层当前没有选中的要素（选择可能在弹窗期间被清空），请重新选择后再复制。",
                    )
                    return
                target = dlg.target_layer()
                if target is None:
                    return
                result = run_copy_to(
                    source,
                    target,
                    dlg.field_mappings(),
                    dlg.copy_attributes_checked(),
                    hint=self._tool_hint,
                )
            except Exception as exc:  # noqa: BLE001 —— 源/目标在弹窗期间被删
                QMessageBox.warning(self.main_window, "复制到失败", str(exc))
                return
            if result.ok:
                self._toast(
                    f"已复制 {result.added} 个要素到「{target.name()}」（可 Ctrl+Z 撤销）"
                )
                QTimer.singleShot(0, self._safe_refresh)
                return
            # 校验失败 / 写入失败：面板已关，这里把原因讲清楚（附带涉及的字段名）
            detail = f"【{'】【'.join(result.fields)}】" if result.fields else ""
            QMessageBox.warning(
                self.main_window, "复制到失败", f"{result.reason}{detail}"
            )

        show_modeless(
            self.main_window,
            ("copy_to", source.id()),
            lambda: CopyToLayerDialog(
                source,
                targets,
                len(source.selectedFeatureIds()),
                parent=self.main_window,
            ),
            on_accepted=_apply_copy,
            layer=source,
        )

    def show_edit_settings_panel(self) -> None:
        """「编辑设置」一次性动作：弹面板编辑编辑工具的快捷键（保存即生效）。

        面板与落盘都在 :class:`MainWindow`（:meth:`apply_edit_settings_panel`）——快捷键
        真正挂在那些 QAction / QShortcut 上，只有主窗口拿得到它们。
        """
        fn = getattr(self.main_window, "apply_edit_settings_panel", None)
        if not callable(fn):
            QMessageBox.information(self.main_window, "提示", "当前窗口不支持编辑设置")
            return
        fn()

    def _safe_refresh(self) -> None:
        try:
            self.canvas.refresh()
        except RuntimeError:
            pass

    def _ensure_attribute_entry_dialog(self):
        """懒建属性录入弹窗（单实例；关掉了就复用同一个，保留窗口位置）。"""
        dialog = self._attribute_entry_dialog
        if dialog is None or sip.isdeleted(dialog):
            dialog = AttributeEntryDialog(self.main_window)
            # 弹窗被关掉后要让工具忘掉"已载入哪个要素"，否则再点同一个要素会被
            # 当成重复点击忽略（工具看起来失灵）
            dialog.finished.connect(self._on_attribute_entry_dialog_closed)
            self._attribute_entry_dialog = dialog
        return dialog

    def _on_attribute_entry_dialog_closed(self, _result: int = 0) -> None:
        tool = self.attribute_entry_tool
        if tool is not None:
            tool.reset_loaded()

    def _close_attribute_entry_dialog(self) -> None:
        """关掉属性录入弹窗（幂等）。工具退出时统一走这里收口。

        弹窗可能处于**最小化**（画布底部小条）状态——``reject()`` 会走它的
        ``done()``，一并把小条收掉、清掉选中高亮。不关的话，Esc 退出工具后
        屏幕上会留一条无主的小条 / 一个还能改属性的弹窗。
        """
        dialog = self._attribute_entry_dialog
        if dialog is None or sip.isdeleted(dialog):
            return
        try:
            # 用 reject 而不是 close：**最小化时弹窗是隐藏的，close() 对隐藏窗口
            # 是空操作**，收尾（收小条 / 清高亮）就不会跑。reject() 无条件走
            # QDialog.done()，弹窗的 done() 重写才是真正的收口点。
            dialog.reject()
        except Exception:  # noqa: BLE001 —— 收尾失败不该影响工具清理
            logger.exception("关闭属性录入弹窗失败")

    def _load_attribute_entry_feature(self, layer: QgsVectorLayer, fid: int) -> None:
        """「属性录入」点中要素：把属性载入弹窗。

        弹窗若已被用户手动关掉（标题栏 ×），这里**先重新显示**再载入——不然点了
        要素什么都没发生，看起来像工具失灵。
        """
        dialog = self._ensure_attribute_entry_dialog()
        if dialog.isHidden():
            dialog.show()
        dialog.raise_()
        dialog.load_feature(layer, fid)

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
        """激活「属性录入」工具：点击要素 → 弹窗编辑其属性。

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
                # 旧编辑图层有未提交改动时 applier 会弹「保存/放弃/取消」；
                # 用户选了取消 → 整个换层中止，别再往下进编辑态
                if callable(applier) and not applier(layer):
                    self._refresh_button_states()
                    return
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


        # 属性录入改成**弹窗**了（原先是右侧 Dock）：懒建单实例，点要素时载入。
        # 不再走 place_right_dock —— 它不是 Dock，不参与右侧叠放。
        self._ensure_attribute_entry_dialog()

        self.attribute_entry_tool = AttributeEntryTool(
            self.canvas,
            editing_layer_getter=self._editing_layer_getter,
            on_feature=self._load_attribute_entry_feature,
            hint=self._tool_hint,
        )
        self._activate_map_tool(self.attribute_entry_tool)
        self._mark_active("attr_entry")
        # 弹窗**不在这里显示**：与「节点列表」一致，点中要素时才 pop 出来
        # （见 :meth:`_load_attribute_entry_feature`）。激活时靠下面这条 toast 指路。
        self._toast(
            "已激活「属性录入」工具：点击图层上的要素，在弹窗中修改其属性；确认后保存，Esc 退出",
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
        工具（``active_tool`` / 节点编辑 / 属性录入 / 线分割 / 面分割）：

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
        _drop("node_list_tool")
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
        # 节点列表弹窗跟着工具走：Esc 退出 / 再点按钮取消 / 切别的工具 / 停止编辑，
        # 四条路都经过本方法，所以这里是唯一的收口点
        self._close_node_list_dialog()
        # 属性录入弹窗同理（含最小化小条）
        self._close_attribute_entry_dialog()
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
        """把 ``layer`` 的 ``editingStarted`` / ``editingStopped`` 接到本工具条。

        属性表 Dock「开始 / 停止编辑」/ 切图层停旧层 / 关工程等外部入口不经过
        :meth:`stop_edit_fun`——QGIS 在进入 / 结束编辑时发对应信号，本工具条在
        信号里统一补状态、解除激活工具、刷新按钮和下拉框，避免「外部停编后
        绘制工具仍挂在画布」「下拉框的编辑中标记不更新」。

        未编辑的图层也照连：之后由外部开启再结束编辑同样能兜底。先断开上一
        图层再连当前图层，同一时刻只挂一个钩子。
        """
        self._disconnect_edit_stop_hook()
        # 撤销栈信号与停编钩子同生命周期：目标图层切换时一并换绑。
        # **选中集钩子不在这里**——它是工程级、与本工具条有没有编辑目标无关，
        # 见 :meth:`_connect_map_tool_hook` 旁边的说明。
        self._connect_undo_stack(layer)
        if layer is None or layer.type() != layer.VectorLayer:
            return
        try:
            layer.editingStarted.connect(self._on_editing_started)
            layer.editingStopped.connect(self._on_editing_stopped)
            self._edit_stop_hook_layer = layer
            logger.debug("已挂接图层编辑状态钩子: %s", layer.name())
        except Exception:
            self._edit_stop_hook_layer = None
            try:
                layer_name = layer.name()
            except RuntimeError:
                layer_name = "<已销毁>"
            logger.exception("连接图层编辑状态钩子失败: %s", layer_name)

    def _disconnect_edit_stop_hook(self) -> None:
        """断开当前图层的 ``editingStarted`` / ``editingStopped`` 钩子。"""
        # 撤销栈信号与停编钩子同生命周期：一并断开（选中集钩子不归这里管）
        self._disconnect_undo_stack()
        layer = self._edit_stop_hook_layer
        self._edit_stop_hook_layer = None
        if layer is None:
            return
        # 数据源重设 / 删图层时 wrapper 可能已经指向被删除的 C++ 对象：
        # 此时再访问任何方法都会抛 RuntimeError（包括 name()），提前收手
        if sip.isdeleted(layer):
            return
        try:
            layer_name = layer.name()
        except RuntimeError:
            return
        for signal in ("editingStarted", "editingStopped"):
            try:
                getattr(layer, signal).disconnect(
                    self._on_editing_started
                    if signal == "editingStarted"
                    else self._on_editing_stopped
                )
            except Exception:
                # 图层销毁 / 信号已被触发自动断开等场景会抛异常，属正常收尾
                logger.warning(
                    "断开图层 %s 钩子失败(收尾场景属正常): %s",
                    signal,
                    layer_name,
                    exc_info=True,
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
        # 否则它的选中变化推不动按钮状态。图层刚被 removeMapLayer 删掉时
        # wrapper 可能已指向销毁的 C++ 对象，任何方法（含 id()）都会抛
        # RuntimeError —— 必须过滤，否则 slots 里抛异常会直接杀进程。
        current = self._current_layer()
        if isinstance(current, QgsVectorLayer) and not sip.isdeleted(current):
            try:
                layers[current.id()] = current
            except RuntimeError:
                pass
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
        「节点编辑」里按 Delete 删掉唯一的选中要素 → ``deleteFeatures`` 发
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

    def _on_editing_started(self) -> None:
        """任意入口开始编辑的兜底：补齐 ``_editing`` 标志并刷新按钮 / 下拉框。

        「属性录入」自动开编辑、属性表 Dock 的「开始编辑」、脚本直接
        ``startEditing()`` 都会走到这里；不补的话工具条的 ``_editing`` 会与
        真实 ``isEditable()`` 脱节，后续停编可能少发一次状态信号。
        信号只在「从无到有」时发，避免与 :meth:`_ensure_editing` 重复。
        """
        layer = self._current_layer()
        if (
            layer is None
            or layer.type() != layer.VectorLayer
            or not layer.isEditable()
        ):
            return
        was_editing = self._editing
        self._editing = True
        self._refresh_button_states()
        self.sync_edit_layer_combo()
        if not was_editing:
            self.editing_state_changed.emit(True)

    def _on_editing_stopped(self) -> None:
        """任意入口结束编辑的兜底：解除激活工具并复位工具条状态。

        commitChanges / rollBack 结束编辑时会同步发 ``editingStopped``（含
        :meth:`stop_edit_fun` 自己走的提交路径）；这里统一处理后，后续
        :meth:`_exit_editing_state` 因 ``_editing`` 已复位而不会重复发信号。

        **不在这里断开编辑状态钩子**：编辑目标在停编后仍然保留，用户可能从
        属性表 Dock 等入口再次开始编辑 —— 钩子一断，``editingStarted`` /
        下一次 ``editingStopped`` 就收不到了（下拉框的“编辑中”标记会失灵）。
        钩子只在切换目标（``set_edit_target``）或图层销毁时解除。
        """
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

    def _any_layer_has_selection(self) -> bool:
        """画布上是否有**任意**可见矢量图层存在选中要素（「复制到」的可用性依据）。"""
        try:
            return any(
                layer.selectedFeatureIds()
                for layer in self.canvas.layers()
                if isinstance(layer, QgsVectorLayer) and layer.isValid()
            )
        except RuntimeError:
            return False

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
            节点编辑 / 线分割 / 面分割 / 分解 要至少选中 1 个；挖洞 / 补洞 另需面图层；
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
            self.split_feature_action,
            self.split_polygon_action,
            self.explode_action,
        ):
            action.setEnabled(has_selection)
        # 「节点编辑」与「节点列表」一样，只要**进了编辑态**就能用：它自己在地图上点
        # 哪个图形就编辑哪个，不依赖事先选中的要素——用户要连续改多个图形时不必来回选
        self.feature_edit_action.setEnabled(editing)
        # 挖洞 / 补洞：编辑中、为面图层**且有选中**才可用（画区域 / 点击只作用于选中要素）
        is_polygon = g_type == QgsWkbTypes.PolygonGeometry
        self.dig_hole_action.setEnabled(has_selection and is_polygon)
        self.fill_hole_action.setEnabled(has_selection and is_polygon)
        # 节点列表：用户明确要求「先启动编辑」才可用（与「属性录入」恒可用、激活时
        # 自动开编辑的做法有意不同）；且只做面图层——点 / 线图层没有"环"可言
        self.node_list_action.setEnabled(editing and is_polygon)
        # 「合并」要两个及以上才谈得上合并，选中不足即置灰；「补缝」同理（缝隙在
        # 多个相邻面之间，一个面谈不上补缝）
        self.merge_geometry_action.setEnabled(has_selection and len(selected_fids) >= 2)
        self.gap_repair_action.setEnabled(is_polygon and len(selected_fids) >= 2)
        self.boundary_edit_action.setEnabled(is_polygon and has_selection)
        self.move_action.setEnabled(has_selection)
        # 「删除」删的是编辑图层的选中要素；「要素分割」要求**其它图层**上有选中的切割要素
        self.delete_action.setEnabled(has_selection)
        # 「复制到」是编辑工具条里**唯一不要求编辑态**的动作：源是"选中要素**所在**的
        # 图层"（未必是当前编辑图层），目标图层由工具自己开编辑缓冲。只要有选中就能用
        # ——所以这里看的是**所有可见矢量图层**的选中，而不只是编辑图层。
        self.copy_to_action.setEnabled(self._any_layer_has_selection())
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
