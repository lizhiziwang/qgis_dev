"""通用算法 Dialog（按 ``QgsProcessingAlgorithm`` 参数描述动态生成表单）。

为 tools_dock 上挂的 48 个 ``gdal:*`` 算法（以及任意 processing 算法）复用
同一个对话框：构造时收 ``algo_id`` → ``QgsGui.processingGuiRegistry().createParameterWidgetWrapper``
按 ``alg.parameterDefinitions()`` 逐个造控件 → 收齐 ``value()`` 后调
:class:`app.core.domain.generic_alg.GenericAlgService` 跑算法。

- 所有 wrapper 建完后统一调用 ``postInitialize``，字段 / 波段等依赖父图层参数
  的下拉框才会填值并随父参数变化刷新；
- 图层类参数把 QGIS 自带的「选择输入」菜单换成统一的「添加数据」弹窗；
- 输出不再另画一行路径输入框：每个 destination 直接用 QGIS 原生控件
  （「临时文件 / 保存到文件…」按钮 + 下拉菜单），同一输出只有一个入口；
- wrapper 自带但本对话框没有执行语义的按钮（迭代、参数级高级选项、运行后
  打开输出文件）隐藏，避免出现“点了没反应”的按钮。

复用 :class:`BufferDialog` 的 layout（左参数 + 右算法描述 + 下按钮 / 进度条），
但不写死任何参数名。Advanced flag 的参数放在普通参数之后，由「高级」复选框
统一展开 / 收起，并与普通参数共用同一套表单列。

执行在**后台线程**（``QgsProcessingAlgRunnerTask``，与 QGIS processing 对话框
同一机制）跑，主线程事件循环不阻塞——GDAL 大栅格处理不再卡死 UI；带进度条与
取消按钮，结果经 ``executed`` 信号回主线程后再装配图层。
"""

from __future__ import annotations

import os
import re
from typing import Optional

from qgis.PyQt.QtCore import QSize, Qt, QTimer, QUrl
from qgis.PyQt.QtGui import QDesktopServices, QIcon
from qgis.PyQt.QtWidgets import (
    QAbstractItemView,
    QCheckBox,
    QComboBox,
    QDialogButtonBox,
    QFormLayout,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QMenu,
    QProgressBar,
    QPushButton,
    QScrollArea,
    QSizePolicy,
    QTableView,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

from qgis.core import (
    Qgis,
    QgsApplication,
    QgsCoordinateReferenceSystem,
    QgsCoordinateTransform,
    QgsMapLayer,
    QgsProcessing,
    QgsProcessingParameterCrs,
    QgsProcessingParameterDefinition,
    QgsProcessingParameterEnum,
    QgsProcessingParameterExtent,
    QgsProcessingParameterFeatureSource,
    QgsProcessingParameterField,
    QgsProcessingParameterBoolean,
    QgsProcessingParameterFileDestination,
    QgsProcessingParameterFolderDestination,
    QgsProcessingParameterMultipleLayers,
    QgsProcessingParameterNumber,
    QgsProcessingParameterRasterLayer,
    QgsProcessingParameterVectorLayer,
    QgsProcessingParameters,
    QgsProject,
    QgsRasterLayer,
    QgsVectorLayer,
)
from qgis.gui import (
    QgsAbstractProcessingParameterWidgetWrapper,
    QgsGui,
    QgsMapToolExtent,
    QgsProcessingContextGenerator,
    QgsProcessingGui,
    QgsProcessingParameterWidgetContext,
)

from app.core.domain.generic_alg import (
    RESULT_IS_PATH,
    GenericAlgInputError,
    GenericAlgJob,
    GenericAlgService,
)
from app.core.domain.generic_alg.ogr_columns import (
    fid_column_name,
    geometry_column_name,
)
from app.core.domain.geom_cleanup import copy_with_valid_polygons
from app.infra.logging import get_logger
from app.infra.messagebar import send
from app.ui.common.algo_desc import make_description_panel
from app.ui.common.base_dialog import ANALYSIS_DIALOG_MAX_HEIGHT, BaseDialog
from app.ui.common.layer_combo import LayerNameWatcher
from app.ui.common.layer_order import layers_in_tree_order
from app.ui.common.message_box import MessageBox
from app.ui.common.processing_zh import (
    algorithm_title,
    enum_label,
    parameter_label,
    parameter_tooltip,
)
from app.ui.common.validation import focus_and_highlight
from app.ui.common.widgets import reveal_in_file_manager

logger = get_logger(__name__)

#: 无 destination 算法（如 gdal:extractprojection）的文件输出名 → 中文标签。
#: QGIS 输出定义的描述是英文，这里只翻译本系统上架工具里出现的几项。
_FILE_OUTPUT_LABELS = {
    "WORLD_FILE": "世界文件",
    "PRJ_FILE": "投影文件",
}


class _ContextGenerator(QgsProcessingContextGenerator):
    """固定返回一个 ``QgsProcessingContext``（项目级）的生成器。

    QGIS processing GUI 注册表生成的 widget wrapper 需要这个接口才能解析
    当前项目 / 上下文。我们的 service 仍用单独 ``QgsProcessingContext`` 跑
    ``processing.run``，这里只是为了给 widget 提供上下文。
    """

    def __init__(self, project):
        from qgis.core import QgsProcessingContext

        super().__init__()
        self._ctx = QgsProcessingContext()
        self._ctx.setProject(project)

    def processingContext(self):
        return self._ctx


class _ProjectLayerPickerDialog(BaseDialog):
    """从当前工程图层里多选（多图层参数用）。"""

    def __init__(self, parent, layers, title: str = "选择图层") -> None:
        super().__init__(parent, title)
        self.setMinimumSize(0, 0)
        self.resize(460, 460)

        layout = QVBoxLayout()
        layout.setContentsMargins(14, 12, 14, 12)
        layout.setSpacing(8)
        hint = QLabel("选择要添加的图层（可按住 Ctrl / Shift 多选）：")
        hint.setWordWrap(True)
        layout.addWidget(hint)

        self.list = QListWidget()
        self.list.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        for layer in layers:
            item = QListWidgetItem(GenericAlgDialog._layer_label(layer))
            # 只存图层 id：工程里取活对象，避免 item data 持有图层包装器
            item.setData(Qt.ItemDataRole.UserRole, layer.id())
            self.list.addItem(item)
        layout.addWidget(self.list, 1)

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel,
            Qt.Orientation.Horizontal,
        )
        buttons.button(QDialogButtonBox.StandardButton.Ok).setText("添加")
        buttons.button(QDialogButtonBox.StandardButton.Cancel).setText("取消")
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)
        self.setLayout(layout)
        if self.list.count():
            self.list.setCurrentRow(0)

    def selected_layer_ids(self) -> list[str]:
        return [item.data(Qt.ItemDataRole.UserRole) for item in self.list.selectedItems()]


class GenericAlgDialog(BaseDialog):
    """任意 processing 算法的通用对话框。

    构造参数：
    :param parent: 父窗口
    :param algo_id: 算法 id（``gdal:warpreproject`` / ``native:buffer`` 等）
    :param title: 可选；为空时取算法中文标题（无映射时用 ``alg.displayName()``）
    """

    #: 分析工具弹窗统一高度上限：勾选「高级」后参数变多时不再撑高到整屏，
    #: 超出部分由参数区滚动条承载
    max_window_height = ANALYSIS_DIALOG_MAX_HEIGHT

    def __init__(self, parent=None, algo_id: str = "", title: str = "") -> None:
        self._algo_id = algo_id
        self._alg = self._resolve_algorithm(algo_id)
        self._algo_title = algorithm_title(
            algo_id, self._alg.displayName() if self._alg else algo_id
        )
        if not title:
            title = self._algo_title
        super().__init__(parent, title)
        # 最小高度不按 620 硬撑：没有点「高级」时只按可见内容给高度，
        # 勾选高级后再长高（见 _set_advanced_visible / _fit_to_visible_content）。
        self.setMinimumSize(760, 320)
        # 最大高度用屏幕可用高度（留出任务栏 / 边缘）：Grid IDW、PostgreSQL
        # 导出等参数特别多的算法比主窗口还高，按主窗口封顶会一直差一点。
        max_h = self._resolve_max_height(parent)
        if max_h:
            self.setMaximumHeight(max_h)
        self.project = QgsProject.instance()
        self.service = GenericAlgService()

        #: 当前后台任务及其反馈 / 上下文 / 任务参数。任务结束后 _task 立即置 None；
        #: context 与 feedback 继续保留（QgsProcessingAlgRunnerTask 内部保存的是裸
        #: 指针，任务收尾阶段仍会使用），下次执行时被新实例覆盖。
        self._task = None
        self._context = None
        self._feedback = None
        self._job: Optional[GenericAlgJob] = None
        self._primary_output = None
        #: 当前任务清洗后的参数（含临时 memory 输入图层）。任务结束后仍保留到下次
        #: 执行，避免任务收尾阶段临时图层被 GC 导致悬垂。
        self._prepared_params: dict = {}
        #: 最近一条 ERROR 级算法日志（任务失败时展示到 messagebar）
        self._last_error = ""
        #: 上一次由输入图层自动写入的几何列名（用于区分用户手工修改）
        self._auto_geometry_value = ""

        # 维持 wrapper 不被 Python GC（C++ 持有指针）
        self._wrappers: dict[str, "QgsAbstractProcessingParameterWidgetWrapper"] = {}
        #: 图层参数 → 自建下拉框（按图层面板顺序；QGIS 自带 combo 只保留作取值）
        self._layer_combos: dict[str, QComboBox] = {}
        #: 被替换下来的 QGIS 图层 combo。必须持有 Python 引用：它的 QWidget 父级
        #: 是 wrapper 内部容器，PyQt wrapper 被回收后 C++ 对象可能随之销毁，而
        #: QGIS wrapper 仍持有裸指针（取值时直接崩）。
        self._hidden_layer_combos: dict[str, QComboBox] = {}
        #: 坐标系参数 → 自建「可搜索下拉 + 选择…」行（见 _replace_crs_widget）；
        #: 原 QGIS 控件隐藏后仍是 wrapper 的取值来源，必须留 Python 引用。
        self._crs_combos: dict[str, QComboBox] = {}
        self._hidden_crs_widgets: dict[str, QWidget] = {}
        #: 多图层参数（gdal:merge 的 INPUT 等）→ 自建「列表 + 添加/移除」控件。
        #: 原 QGIS 多图层面板隐藏后仍是 wrapper 的取值来源，必须留引用。
        self._multilayer_lists: dict[str, QListWidget] = {}
        self._hidden_multilayer_widgets: dict[str, QWidget] = {}
        self._multilayer_pdefs: dict[str, object] = {}
        #: 范围参数的绘制工具。QgsMapToolExtent 由 Python 持有，绘制结束前不能让 GC 回收
        self._extent_tool = None
        #: 地图画布（取自父窗口），范围绘制 / 使用当前范围都用它
        self._canvas = None
        # 复用一个上下文生成器 + widget context，所有 wrapper 共用
        self._ctx_gen = _ContextGenerator(self.project)
        self._widget_ctx = self._build_widget_context()

        self._init_ui()

    # ----------------------------------------------------------- helpers

    def _resolve_max_height(self, parent) -> Optional[int]:
        """算出 dialog 最大高度：取屏幕可用高度（留出任务栏 / 边缘）。

        参数特别多的算法（Grid IDW / PostgreSQL 导出等）比主窗口还高；
        若按主窗口高度封顶，会永远差一点、必须滚动。这里改用父类统一的
        屏幕可用高度上限，让窗口能完整展开。
        """
        return self._max_window_height()

    def _resolve_algorithm(self, algo_id: str):
        if not algo_id:
            return None
        return QgsApplication.processingRegistry().algorithmById(algo_id)

    def _build_widget_context(self) -> "QgsProcessingParameterWidgetContext":
        """构造 ``QgsProcessingParameterWidgetContext``。

        设置项目 / 活动图层 / 地图画布，wrapper 内置的下拉框（layer / band /
        field 等）与画布取点、范围按钮才能正确工作。画布与当前图层从父窗口
        （主窗口）取——工程属性 ``qgis_iface`` 在本工程里从未被赋值，只作为兜底。
        """
        ctx = QgsProcessingParameterWidgetContext()
        ctx.setProject(self.project)

        canvas = None
        active = None
        iface = self.project.property("qgis_iface") if self.project else None
        try:
            parent = self.parent()
            canvas = getattr(parent, "canvas", None)
            if canvas is None and iface is not None:
                canvas = getattr(iface, "mapCanvas", lambda: None)()
            if canvas is not None:
                self._canvas = canvas
                ctx.setMapCanvas(canvas)
                active = getattr(canvas, "currentLayer", lambda: None)()
            if active is None and iface is not None and hasattr(iface, "activeLayer"):
                active = iface.activeLayer()
            if active is not None:
                ctx.setActiveLayer(active)
        except Exception:  # noqa: BLE001
            logger.debug("widgetContext 可选字段未装配，不影响主流程", exc_info=True)
        return ctx

    # ----------------------------------------------------------- UI

    def _init_ui(self) -> None:
        layout = QVBoxLayout()

        main = QHBoxLayout()

        # 参数面板装进 QScrollArea：当算法参数行数过多（如栅格计算器 13+ 行）
        # 内容总高度超过 MainWindow 可用高度时，由滚动条承载，而不是撑爆
        # dialog 或压扁 wrapper 控件。outer QScrollArea 的高度由 mainwindow
        # 高度上限决定（``_resolve_max_height``），超过部分滚动
        param_panel = QWidget()
        self._param_panel = param_panel
        self._init_parameters_panel(param_panel)
        param_scroll = QScrollArea()
        param_scroll.setWidgetResizable(True)
        param_scroll.setFrameShape(QScrollArea.Shape.NoFrame)
        param_scroll.setWidget(param_panel)
        main.addWidget(param_scroll, stretch=3)

        main.addWidget(make_description_panel(self._algo_id), stretch=1)
        layout.addLayout(main, stretch=1)

        b_l = QVBoxLayout()
        b_l.setContentsMargins(0, 0, 0, 0)

        # 输出控件（仅在算法有 destination 时显示）
        self._has_destination = bool(self._alg.destinationParameterDefinitions())
        if self._has_destination:
            out_widget = QWidget()
            self._init_output_panel(out_widget)
            b_l.addWidget(out_widget)

        # 所有 wrapper（含输出参数）建完后统一建立依赖联动：QGIS 的字段 / 波段 /
        # 距离单位等 wrapper 只有 postInitialize 之后才会把父图层信息灌进下拉框
        # （否则融合工具的字段下拉只有空项）。
        self._post_initialize_wrappers()

        self.loading = QProgressBar()
        self.loading.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        self.loading.setRange(0, 100)
        # 重要：与 BufferDialog 同原因，不接 feedback.progressChanged。
        b_l.addWidget(self.loading, stretch=2)

        # 结果文件面板：执行完成后列出输出路径（可选择复制），并可直接打开 /
        # 在文件管理器中定位。无文件输出的算法保持隐藏。
        self._result_box = QWidget()
        self._result_layout = QVBoxLayout(self._result_box)
        self._result_layout.setContentsMargins(0, 0, 0, 0)
        self._result_layout.setSpacing(4)
        self._result_box.setVisible(False)
        b_l.addWidget(self._result_box)

        bottom = QHBoxLayout()
        bottom.addStretch()
        self.execute_btn = QPushButton("执行")
        self.execute_btn.clicked.connect(self._on_execute)
        bottom.addWidget(self.execute_btn)
        self.cancel_btn = QPushButton("取消")
        self.cancel_btn.setEnabled(False)
        self.cancel_btn.clicked.connect(self._on_cancel)
        bottom.addWidget(self.cancel_btn)
        close = QPushButton("关闭")
        close.clicked.connect(self.close)
        bottom.addWidget(close)
        b_l.addLayout(bottom)
        layout.addLayout(b_l)

        self.setLayout(layout)
        self.fit_height_to_content(param_panel, extra=24)

    def _fit_to_visible_content(self) -> None:
        """按当前可见参数（高级折叠 / 展开）重新收紧窗口高度。

        没点「高级」时高级参数是隐藏的，窗口应只占可见内容的高度；勾选后
        再长高。高度上限仍由 :meth:`_max_window_height` 管，超出部分走滚动条。
        """
        try:
            panel = getattr(self, "_param_panel", None)
            if panel is None or self.isMaximized():
                return
            if panel.layout() is not None:
                panel.layout().activate()
            if self.layout() is not None:
                self.layout().activate()
            self.fit_height_to_content(panel, extra=24)
        except Exception:  # noqa: BLE001 —— 窗口可能已关闭 / 正在销毁
            logger.debug("按可见内容调整高度失败", exc_info=True)

    def showEvent(self, event) -> None:
        """窗口显示后统一表格列宽（rasteroptions 自己会在 showEvent 里改列宽）。"""
        super().showEvent(event)
        QTimer.singleShot(0, self._apply_creation_table_columns)

    def _init_parameters_panel(self, parent) -> None:
        """按 ``alg.parameterDefinitions()`` 走表生成表单。

        - 跳过 hidden flag（0x04，``FlagHidden``）
        - 跳过 destination 输出参数（统一走底部输出面板）
        - Advanced flag（0x02，``FlagAdvanced``）→ 放到普通参数之后，可勾选展开
        - 字段增长策略用 :attr:`QFormLayout.FieldGrowthPolicy.AllNonFixedFieldsGrow`
          —— QGIS wrapper 默认 size policy 是 ``Preferred``，若用
          ``ExpandingFieldsGrow`` 只会拉伸 ``Expanding`` 子控件，下拉框会卡
          在 ``minimumSizeHint()`` 宽度（实测 26px），形如被压扁
        """
        layout = QVBoxLayout(parent)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(8)

        form = QFormLayout()
        form.setContentsMargins(0, 0, 0, 0)
        form.setFieldGrowthPolicy(
            QFormLayout.FieldGrowthPolicy.AllNonFixedFieldsGrow
        )
        form.setRowWrapPolicy(QFormLayout.RowWrapPolicy.DontWrapRows)
        form.setSpacing(6)

        advanced_count = 0
        #: (是否高级, 标签控件或 None, 字段控件, 跨列控件, 跨列控件里的表格)
        rows: list[
            tuple[bool, Optional[QLabel], QWidget, Optional[QWidget], Optional[QWidget]]
        ] = []
        label_widgets: list[QLabel] = []
        self._creation_tables: list[tuple[QWidget, int]] = []
        for pdef in self._alg.parameterDefinitions():
            name = pdef.name()
            if pdef.flags() & QgsProcessingParameterDefinition.Flag.FlagHidden:
                continue
            # destination 统一放到窗口底部的输出面板（QGIS 的按钮 + 下拉控件），
            # 避免同一输出同时出现「参数行」与「输出路径」两个入口。
            if pdef.isDestination():
                continue
            wrapper = self._build_wrapper(pdef)
            if wrapper is None:
                continue
            self._wrappers[name] = wrapper

            is_advanced = bool(
                pdef.flags() & QgsProcessingParameterDefinition.Flag.FlagAdvanced
            )
            label = wrapper.createWrappedLabel()
            widget = wrapper.createWrappedWidget(self._ctx_gen.processingContext())
            if widget is None:
                continue
            label_text = self._parameter_label_text(pdef)
            tooltip = parameter_tooltip(pdef)
            # 坐标系参数不用 QGIS 自带的 QgsProjectionSelectionWidget（样式与
            # 图层属性不一致），换成自建的「可搜索下拉 + 选择…」行。
            if isinstance(pdef, QgsProcessingParameterCrs):
                widget = self._replace_crs_widget(pdef, wrapper, widget, tooltip)
            elif isinstance(pdef, QgsProcessingParameterMultipleLayers):
                # QGIS 的多图层控件只有「从文件选」入口、选中后也没法移除；
                # 换成「列表 + 添加工程图层/从文件添加/移除」。
                widget = self._replace_multilayer_widget(pdef, wrapper, widget)
            self._apply_widget_tooltip(widget, pdef, tooltip)
            self._translate_enum_options(widget, pdef)
            # 兜底：显式把横向 size policy 设为 Expanding，与上面增长策略配对
            # —— 防止某些 wrapper 子控件（多套一层 QWidget 容器）默认 Preferred
            # 被压成最小宽度。
            # 数字输入框的纵向固定：标签换行会把整行撑高，QFormLayout 会把
            # Preferred 的字段纵向拉满；QSS 的上下箭头按钮是固定 14px，拉高后
            # 两颗三角被推到最上 / 最下，中间空出一大截（重投影分辨率那一行）。
            vertical = (
                QSizePolicy.Policy.Fixed
                if isinstance(pdef, QgsProcessingParameterNumber)
                else QSizePolicy.Policy.Preferred
            )
            # 多图层参数的自建「列表 + 按钮」行高度固定（列表最高 132px）：
            # 若跟着表单被拉高，列表会垂直居中、按钮却顶在行首，看起来错位。
            if isinstance(pdef, QgsProcessingParameterMultipleLayers):
                vertical = QSizePolicy.Policy.Fixed
            widget.setSizePolicy(QSizePolicy.Policy.Expanding, vertical)
            # 替换 / 隐藏 wrapper 自带、在本对话框里没有执行语义的辅助按钮
            # （迭代、参数级高级选项），并给图层参数补一个「添加数据」入口。
            self._prepare_parameter_widget(pdef, wrapper, widget)
            # 范围参数：QGIS 自带的「…」按钮样式在本主题下只剩半截背景，
            # 换成「使用当前画布范围」+「▼」两颗按钮，菜单只留本系统支持的几项。
            if isinstance(pdef, QgsProcessingParameterExtent):
                self._customize_extent_widget(pdef, wrapper, widget)
            # 图层下拉框改用按图层面板顺序排列的自建 combo（QGIS 的按图层 id 排）
            if self._is_pickable_layer_parameter(pdef):
                self._replace_layer_combo(pdef, wrapper, widget)
            # rasteroptions 是「配置下拉 + 名称/值表格」。把表格所在的那块面板
            # 单独取出来跨列放在该参数下面，才能让表格的两列跟普通参数的
            # 标签列 / 字段列对齐。
            span_widget, span_table = self._take_raster_options_panel(pdef, widget)
            if label is None and isinstance(widget, QCheckBox) and widget.text().strip():
                # 布尔参数的 wrapper 把描述写在复选框文字里（createLabel 返回 None），
                # 再额外加一行 QLabel 会出现前后两条一样的说明。这里只保留复选框，
                # 并放进第二列（字段列），与其他参数控件按同一层级对齐。
                widget.setText(label_text)
                rows.append((is_advanced, None, widget, span_widget, span_table))
            elif label is None:
                label_widget = QLabel(label_text)
                label_widget.setSizePolicy(
                    QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Preferred
                )
                label_widgets.append(label_widget)
                rows.append((is_advanced, label_widget, widget, span_widget, span_table))
            else:
                if isinstance(label, QLabel):
                    label.setText(label_text)
                    # wrapper 建标签时会把英文 toolTip 一并挂上；标题已经是中文，
                    # 不再在悬停时补一遍对应英文。
                    label.setToolTip(tooltip)
                    label_widgets.append(label)
                rows.append((is_advanced, label, widget, span_widget, span_table))

            if is_advanced:
                advanced_count += 1

        label_width = 0
        if label_widgets:
            label_width = max(widget.sizeHint().width() for widget in label_widgets)
            for widget in label_widgets:
                widget.setMinimumWidth(label_width)

        def add_row(
            label: Optional[QLabel],
            widget: QWidget,
            span: Optional[QWidget],
            span_table: Optional[QWidget],
        ) -> None:
            if label is None:
                form.addRow("", widget)
            else:
                form.addRow(label, widget)
            if span is not None:
                form.addRow(span)
                if span_table is not None:
                    spacing = form.horizontalSpacing()
                    if spacing < 0:
                        spacing = 6
                    self._creation_tables.append(
                        (span_table, max(0, label_width + spacing))
                    )

        # 普通参数、高级开关、高级参数全部走同一个 QFormLayout，列宽天然一致
        for is_advanced, label, widget, span, span_table in rows:
            if is_advanced:
                continue
            add_row(label, widget, span, span_table)

        if advanced_count:
            advanced_toggle = QCheckBox("高级")
            # 记住开关：必填的高级参数为空时要先把这一组展开再定位
            self._advanced_toggle = advanced_toggle
            # 「高级」是分组开关，不是普通参数值，放回第一列（作为分组标题行）。
            form.addRow(advanced_toggle)
            advanced_widgets: list[QWidget] = []
            for is_advanced, label, widget, span, span_table in rows:
                if not is_advanced:
                    continue
                add_row(label, widget, span, span_table)
                if label is not None:
                    advanced_widgets.append(label)
                advanced_widgets.append(widget)
                if span is not None:
                    advanced_widgets.append(span)

            def _set_advanced_visible(visible: bool) -> None:
                for child in advanced_widgets:
                    child.setVisible(visible)
                # rasteroptions 面板显示时会自己重设表格列宽，等事件处理完再按
                # 表单列宽覆盖一次；同时按新的可见内容收紧 / 放开窗口高度。
                QTimer.singleShot(0, self._apply_creation_table_columns)
                QTimer.singleShot(0, self._fit_to_visible_content)

            advanced_toggle.toggled.connect(_set_advanced_visible)
            advanced_toggle.setChecked(False)
            _set_advanced_visible(False)

        layout.addLayout(form)
        layout.addStretch()

        # 工程图层增删后刷新 wrapper（layer/band/field 等下拉内容）
        self.project.layersAdded.connect(self._refresh_wrappers)
        self.project.layersWillBeRemoved.connect(self._refresh_wrappers)
        # 删除发生后还要再刷一次：willBeRemoved 时图层仍在，自建下拉框会留下已删项
        self.project.layersRemoved.connect(self._refresh_wrappers)
        # 图层面板拖动排序后，自建图层下拉框跟随
        root = self.project.layerTreeRoot()
        if root is not None:
            root.layerOrderChanged.connect(self._refresh_layer_combos)
        # 图层在面板里改名后，下拉框文字同步（工具对话框实例会被缓存复用）
        self._name_watcher = LayerNameWatcher(
            self.project, self._refresh_layer_combos, self
        )
        # dialog 关闭时清掉连接，避免 dangling
        self.destroyed.connect(self._disconnect_project_signals)

    def _build_wrapper(self, pdef) -> Optional["QgsAbstractProcessingParameterWidgetWrapper"]:
        """调 QGIS 注册表工厂创建单个参数的 widget wrapper。"""
        try:
            wrapper = QgsGui.processingGuiRegistry().createParameterWidgetWrapper(
                pdef, QgsProcessingGui.WidgetType.Standard
            )
            wrapper.setWidgetContext(self._widget_ctx)
            wrapper.registerProcessingContextGenerator(self._ctx_gen)
            return wrapper
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "无法为参数 %s (%s) 创建 widget wrapper：%s",
                pdef.name(), type(pdef).__name__, exc,
            )
            return None

    @staticmethod
    def _parameter_label_text(pdef) -> str:
        """参数中文标签（含可选 / 范围等提示）。"""
        text = parameter_label(pdef.description() or pdef.name())
        if isinstance(pdef, QgsProcessingParameterExtent):
            text += "（xmin, xmax, ymin, ymax）"
        if pdef.flags() & QgsProcessingParameterDefinition.Flag.FlagOptional:
            text += "（可选）"
        return text

    @staticmethod
    def _apply_widget_tooltip(widget: QWidget, pdef, tooltip: str) -> None:
        """用中文提示覆盖 wrapper 自带的英文 tooltip（描述不再重复）。"""
        widget.setToolTip(tooltip)
        desc = (pdef.description() or "").strip()
        for child in widget.findChildren(QWidget):
            current = child.toolTip()
            if not current:
                continue
            # wrapper 生成的提示统一是 <p><b>描述</b></p><p>Python identifier…；
            # 只清理这类提示，保留子控件自己的功能说明。
            if current.lstrip().startswith("<p><b>") or (desc and desc in current):
                child.setToolTip(tooltip)

    @staticmethod
    def _translate_enum_options(widget: QWidget, pdef) -> None:
        """把枚举参数下拉框里的英文选项换成中文。"""
        if not isinstance(pdef, QgsProcessingParameterEnum):
            return
        combos = (
            [widget]
            if isinstance(widget, QComboBox)
            else widget.findChildren(QComboBox)
        )
        for combo in combos:
            for index in range(combo.count()):
                option = combo.itemText(index)
                translated = enum_label(option)
                if translated != option:
                    combo.setItemText(index, translated)

    @staticmethod
    def _take_raster_options_panel(
        pdef, widget: QWidget
    ) -> tuple[Optional[QWidget], Optional[QWidget]]:
        """取出 rasteroptions 的表格面板与表格本身。

        QGIS 的 ``rasteroptions`` wrapper 把「Profile 下拉 + 选项表格」放在同一个
        字段控件里，表格整体落在字段列内，两列自然和上面的普通参数对不齐。
        这里把表格所在的 ``mOptionsWidget`` 摘出来，稍后作为跨列行放到该参数
        下面，再按表单列宽设置表格列宽。
        """
        metadata = pdef.metadata() or {}
        wrapper_meta = metadata.get("widget_wrapper") or {}
        if wrapper_meta.get("widget_type") != "rasteroptions":
            return None, None
        options_panel = widget.findChild(QWidget, "mOptionsWidget")
        table = widget.findChild(QTableView, "mOptionsTable")
        if options_panel is None or table is None:
            return None, None
        parent = options_panel.parentWidget()
        if parent is not None and parent.layout() is not None:
            parent.layout().removeWidget(options_panel)
        options_panel.setParent(None)
        return options_panel, table

    def _apply_creation_table_columns(self) -> None:
        """按表单列宽设置跨列表格的列宽（第二列对齐字段列）。"""
        for table, first_width in getattr(self, "_creation_tables", []):
            header = getattr(table, "horizontalHeader", lambda: None)()
            model = table.model()
            if header is None or model is None or model.columnCount() < 2:
                continue
            header.setSectionResizeMode(0, QHeaderView.ResizeMode.Fixed)
            header.setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
            table.setColumnWidth(0, first_width)

    def _refresh_wrappers(self, *args) -> None:
        """图层增删后通知所有 wrapper 刷新下拉内容。"""
        for wrapper in self._wrappers.values():
            try:
                wrapper.refresh()
            except AttributeError:
                pass
            except Exception:
                logger.debug("wrapper refresh 失败：%s", type(wrapper).__name__, exc_info=True)
        # wrapper 刷新会把隐藏 combo 恢复成 QGIS 的图层 id 顺序，这里再按图层树排一次
        self._refresh_layer_combos()
        # 多图层列表里的图层可能已被删除 / 改名，重建后再写回 wrapper
        self._refresh_multilayer_lists()

    def _disconnect_project_signals(self, *args) -> None:
        try:
            self.project.layersAdded.disconnect(self._refresh_wrappers)
            self.project.layersWillBeRemoved.disconnect(self._refresh_wrappers)
            self.project.layersRemoved.disconnect(self._refresh_wrappers)
            root = self.project.layerTreeRoot()
            if root is not None:
                root.layerOrderChanged.disconnect(self._refresh_layer_combos)
        except (TypeError, RuntimeError):
            pass

    def _init_output_panel(self, parent) -> None:
        """底部输出面板：输出图层名 + 每个 destination 的 QGIS 原生控件。

        输出路径直接复用 QGIS 的 destination wrapper（「临时文件 / 保存到文件…」
        按钮 + 下拉菜单），不再另画一行输入框——同一输出只保留一个入口。
        """
        layout = QFormLayout(parent)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(6)
        layout.setFieldGrowthPolicy(QFormLayout.FieldGrowthPolicy.AllNonFixedFieldsGrow)

        self.output_edit = QLineEdit(self._algo_title + " 结果")
        layout.addRow("输出图层名", self.output_edit)

        for odef in self._alg.destinationParameterDefinitions():
            if odef.flags() & QgsProcessingParameterDefinition.Flag.FlagHidden:
                continue
            wrapper = self._build_wrapper(odef)
            if wrapper is None:
                continue
            self._wrappers[odef.name()] = wrapper
            label = wrapper.createWrappedLabel()
            widget = wrapper.createWrappedWidget(self._ctx_gen.processingContext())
            if widget is None:
                continue
            if not isinstance(label, QLabel):
                label = QLabel()
            label.setText(self._destination_label_text(odef))
            widget.setSizePolicy(
                QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred
            )
            self._prepare_parameter_widget(odef, wrapper, widget)
            layout.addRow(label, widget)

    @staticmethod
    def _destination_label_text(pdef) -> str:
        """输出参数的标签：直接写「输出路径」，不用算法自带的“融合后”等描述。"""
        if isinstance(pdef, QgsProcessingParameterFolderDestination):
            return "输出目录"
        if isinstance(pdef, QgsProcessingParameterFileDestination):
            return "输出文件"
        return "输出路径"

    def _selected_output_path(self) -> str | None:
        """取主输出参数里用户选择的磁盘路径；临时输出 / 未设置时返回 ``None``。"""
        defs = self._alg.destinationParameterDefinitions()
        if not defs:
            return None
        wrapper = self._wrappers.get(defs[0].name())
        if wrapper is None:
            return None
        try:
            value = wrapper.parameterValue()
        except Exception:
            logger.debug("输出参数取值失败", exc_info=True)
            return None
        # 矢量 / 栅格 destination 返回 QgsProcessingOutputLayerDefinition；
        # 文件 / 目录 destination 的 wrapper 直接返回路径字符串。
        if isinstance(value, str):
            path = value
        else:
            sink = getattr(value, "sink", None)
            static = getattr(sink, "staticValue", None)
            path = static() if callable(static) else None
        if not isinstance(path, str):
            return None
        path = path.strip()
        if not path or path.upper() in ("TEMPORARY_OUTPUT", "MEMORY:"):
            return None
        return path

    # ----------------------------------------------------------- 输入数据

    def _post_initialize_wrappers(self) -> None:
        """让 wrapper 之间建立父子联动（字段 / 波段 / 距离单位等）。

        QGIS 的 ``QgsAbstractProcessingParameterWidgetWrapper.postInitialize``
        负责把父图层参数灌进字段下拉框，并在父参数变化时自动刷新。QGIS 自己的
        ``ParametersPanel`` 在表单建完后统一调用；这里自建表单也必须补上，否则
        融合等工具的字段下拉框永远只有空项。
        """
        wrappers = list(self._wrappers.values())
        for wrapper in wrappers:
            try:
                wrapper.postInitialize(wrappers)
            except Exception:
                logger.debug(
                    "wrapper postInitialize 失败：%s",
                    type(wrapper).__name__,
                    exc_info=True,
                )
        # 表单建完后 INPUT / GEOMETRY 才同时存在，此时再按输入图层修正几何列名
        self._sync_geometry_column()
        self._clear_fid_selection()
        # 字段下拉框由 QGIS wrapper 在父图层变化后重新填充，排到事件队列末尾再清一次
        self._queue_fid_clear()

    #: 图层 wrapper 行内“选择输入”按钮的文字。QGIS 用省略号作按钮文字，不随
    #: Qt 翻译变化；迭代 / 参数级高级选项是纯图标按钮（文字为空）。按图标 /
    #: 文字结构识别，比按英文 tooltip 匹配更稳（正式包会加载 QGIS 中文翻译）。
    _PICKER_BUTTON_TEXT = "…"

    def _prepare_parameter_widget(self, pdef, wrapper, widget) -> None:
        """隐藏无执行语义的辅助按钮，并给图层参数换上「添加数据」入口。"""
        if pdef.isDestination():
            # 「运行后打开输出文件」本对话框未实现；留着只会让人觉得按钮没用
            for check in widget.findChildren(QCheckBox):
                check.setVisible(False)
            return
        # 多图层参数已整体换成自建控件（见 _replace_multilayer_widget），
        # 原 wrapper 控件是隐藏的，不需要再处理按钮
        if not self._is_pickable_layer_parameter(pdef):
            return
        # 同一行里的纯图标按钮 = 迭代 / 参数级高级选项，本对话框不支持
        for button in widget.findChildren(QToolButton):
            if button.text().strip() != self._PICKER_BUTTON_TEXT:
                button.setVisible(False)
        self._replace_picker_button(wrapper, widget)

    def _replace_picker_button(self, wrapper, widget) -> None:
        """把 QGIS 自带的「选择输入」按钮换成打开统一「添加数据」的按钮。"""
        source = None
        for button in widget.findChildren(QToolButton):
            if button.text().strip() == self._PICKER_BUTTON_TEXT:
                source = button
                break
        if source is None:
            return
        source.setVisible(False)
        row = self._layout_containing(widget, source)
        if row is None:
            return
        picker = QToolButton()
        picker.setAutoRaise(True)
        picker.setIcon(QgsApplication.getThemeIcon("mActionAddLayer.svg"))
        picker.setToolTip("从本地文件或数据库选择图层（添加数据…）")
        picker.clicked.connect(
            lambda _checked=False, w=wrapper: self._pick_data_for(w)
        )
        row.addWidget(picker)

    @staticmethod
    def _layout_containing(widget, target):
        """找到 ``target`` 所在的（子）布局，用于在原位置插入自建按钮。"""
        outer = widget.layout()
        if outer is None:
            return None
        if outer.indexOf(target) >= 0:
            return outer
        for i in range(outer.count()):
            sub = outer.itemAt(i).layout()
            if sub is not None and sub.indexOf(target) >= 0:
                return sub
        return None

    @staticmethod
    def _is_pickable_layer_parameter(pdef) -> bool:
        return isinstance(
            pdef,
            (
                QgsProcessingParameterFeatureSource,
                QgsProcessingParameterVectorLayer,
                QgsProcessingParameterRasterLayer,
            ),
        )

    # ------------------------------------------------------ 坐标系 / 范围

    def _replace_crs_widget(self, pdef, wrapper, source, tooltip: str) -> QWidget:
        """坐标系参数换成「可搜索下拉 + 选择…」，与图层属性里的坐标系统一致。

        QGIS 自带的 ``QgsProjectionSelectionWidget`` 只能弹 QGIS 自己的投影选择
        对话框；本系统统一用 :class:`CrsComboBox`（可敲 EPSG / 名称搜索）和
        :class:`CrsPickDialog`（按基准分带挑）。原控件隐藏后继续作为 wrapper 的
        取值来源，本行只在用户选择时把新值写回去。
        """
        from app.ui.dialogs.create_data_dialog import CrsComboBox  # noqa: PLC0415

        row = QWidget()
        layout = QHBoxLayout(row)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(6)

        combo = CrsComboBox(row)
        combo.setMinimumContentsLength(12)
        combo.setToolTip(tooltip)
        layout.addWidget(combo, 1)

        pick_btn = QPushButton("选择…", row)
        pick_btn.setToolTip("按基准 / 分带挑选，或沿用某个图层、某份数据的空间参考")
        pick_btn.clicked.connect(
            lambda _checked=False, name=pdef.name(): self._pick_crs(name)
        )
        layout.addWidget(pick_btn)

        source.setVisible(False)
        self._hidden_crs_widgets[pdef.name()] = source
        self._crs_combos[pdef.name()] = combo

        try:
            current = self._crs_value_of(wrapper.parameterValue())
        except Exception:  # noqa: BLE001
            logger.debug("读取坐标系参数默认值失败", exc_info=True)
            current = ""
        if current:
            combo.set_crs(current)
        combo.currentTextChanged.connect(
            lambda _text, name=pdef.name(): self._sync_crs_value(name)
        )
        return row

    @staticmethod
    def _crs_value_of(value) -> str:
        """wrapper 取值 → CrsComboBox 能吃的 authid / WKT（取不到返回空串）。"""
        if value is None:
            return ""
        if isinstance(value, QgsCoordinateReferenceSystem):
            if not value.isValid():
                return ""
            return value.authid() or value.toWkt()
        if isinstance(value, str):
            return value.strip()
        return ""

    def _sync_crs_value(self, name: str) -> None:
        """把自建下拉框的选择写回 QGIS 隐藏控件（wrapper 的取值来源）。"""
        combo = self._crs_combos.get(name)
        wrapper = self._wrappers.get(name)
        if combo is None or wrapper is None:
            return
        try:
            crs = combo.crs()
            wrapper.setWidgetValue(
                crs if crs.isValid() else None, self._ctx_gen.processingContext()
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning("同步坐标系参数 %s 失败：%s", name, exc, exc_info=True)

    def _pick_crs(self, name: str) -> None:
        """打开坐标系选择弹窗，确定后写回下拉框并同步 wrapper。"""
        from app.ui.dialogs.crs_pick_dialog import CrsPickDialog  # noqa: PLC0415

        combo = self._crs_combos.get(name)
        if combo is None:
            return
        try:
            current = combo.authid() or combo.currentText().strip()
            picker = CrsPickDialog(
                self,
                current=current,
                layers=list(self.project.mapLayers().values()),
            )
            try:
                if not picker.exec():
                    return
                value = picker.crs_value()
            finally:
                picker.deleteLater()
            if value:
                combo.set_crs(value)
                self._sync_crs_value(name)
        except Exception as exc:  # noqa: BLE001
            # 槽里未捕获的异常会直接杀进程（PyQt6），必须兜住
            logger.error("选择坐标系失败：%s", exc, exc_info=True)
            MessageBox.warning(self, self._algo_title, f"选择坐标系失败：{exc}")

    # ------------------------------------------------------ 多图层参数

    def _replace_multilayer_widget(self, pdef, wrapper, source) -> QWidget:
        """多图层参数换成「列表 + 添加/移除」自建控件。

        QGIS 原生 ``QgsProcessingMultipleLayerPanelWidget`` 在标准对话框里只有
        一个「…」按钮（打开它自己的选层面板）和一行 ``2 input(s) selected``，
        本系统里还被换成了「添加数据」——只能从文件加、加完没法删。这里改成
        一个可见的图层列表：支持从当前工程图层多选、从文件添加、移除选中项。
        """
        row = QWidget()
        layout = QHBoxLayout(row)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(6)

        list_widget = QListWidget(row)
        list_widget.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        list_widget.setMinimumHeight(88)
        list_widget.setMaximumHeight(132)
        list_widget.setToolTip(
            "算法按列表顺序读取这些图层；选中一项或多项后点「移除」可删除"
        )
        layout.addWidget(list_widget, 1)

        buttons = QVBoxLayout()
        buttons.setContentsMargins(0, 0, 0, 0)
        buttons.setSpacing(4)
        add_btn = QPushButton("添加图层…", row)
        add_btn.setToolTip("从当前工程的图层列表中选择")
        add_btn.clicked.connect(
            lambda _checked=False, name=pdef.name(): self._add_project_layers_to(name)
        )
        file_btn = QPushButton("从文件添加…", row)
        file_btn.setToolTip("从本地文件 / 数据库添加图层")
        file_btn.clicked.connect(
            lambda _checked=False, name=pdef.name(): self._add_files_to_multilayer(name)
        )
        remove_btn = QPushButton("移除", row)
        remove_btn.setToolTip("移除列表中选中的图层")
        remove_btn.clicked.connect(
            lambda _checked=False, name=pdef.name(): self._remove_selected_multilayer_items(name)
        )
        clear_btn = QPushButton("清空", row)
        clear_btn.clicked.connect(
            lambda _checked=False, name=pdef.name(): self._clear_multilayer_items(name)
        )
        for btn in (add_btn, file_btn, remove_btn, clear_btn):
            buttons.addWidget(btn)
        buttons.addStretch()
        layout.addLayout(buttons)

        source.setVisible(False)
        self._hidden_multilayer_widgets[pdef.name()] = source
        self._multilayer_lists[pdef.name()] = list_widget
        self._multilayer_pdefs[pdef.name()] = pdef

        try:
            initial = wrapper.parameterValue()
        except Exception:  # noqa: BLE001
            initial = None
        for layer in self._multilayer_layers_from_value(initial):
            self._append_multilayer_item(pdef.name(), layer, sync=False)
        self._sync_multilayer_value(pdef.name())
        return row

    def _multilayer_layers_from_value(self, value) -> list:
        """wrapper 取值（图层对象 / 图层 id / 数据源字符串列表）→ 工程里的图层。"""
        if not value:
            return []
        items = value if isinstance(value, (list, tuple)) else [value]
        layers = []
        for item in items:
            if isinstance(item, QgsMapLayer):
                layers.append(item)
                continue
            text = str(item)
            layer = self.project.mapLayer(text)
            if layer is None:
                layer = next(
                    (
                        candidate
                        for candidate in self.project.mapLayers().values()
                        if candidate.source() == text
                    ),
                    None,
                )
            if layer is not None:
                layers.append(layer)
        return layers

    def _append_multilayer_item(self, name: str, layer, *, sync: bool = True) -> None:
        list_widget = self._multilayer_lists.get(name)
        if list_widget is None or layer is None:
            return
        for index in range(list_widget.count()):
            if list_widget.item(index).data(Qt.ItemDataRole.UserRole) == layer.id():
                return  # 已在列表里，不重复添加
        item = QListWidgetItem(self._layer_label(layer))
        item.setData(Qt.ItemDataRole.UserRole, layer.id())
        list_widget.addItem(item)
        list_widget.scrollToBottom()
        if sync:
            self._sync_multilayer_value(name)

    def _sync_multilayer_value(self, name: str) -> None:
        """把列表内容写回 QGIS 隐藏的多图层控件（wrapper 的取值来源）。"""
        list_widget = self._multilayer_lists.get(name)
        wrapper = self._wrappers.get(name)
        if list_widget is None or wrapper is None:
            return
        layers = []
        for index in range(list_widget.count()):
            layer = self.project.mapLayer(
                list_widget.item(index).data(Qt.ItemDataRole.UserRole)
            )
            if layer is not None:
                layers.append(layer)
        try:
            wrapper.setWidgetValue(
                layers if layers else None, self._ctx_gen.processingContext()
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning("同步多图层参数 %s 失败：%s", name, exc, exc_info=True)

    def _remove_selected_multilayer_items(self, name: str) -> None:
        list_widget = self._multilayer_lists.get(name)
        if list_widget is None:
            return
        for item in list_widget.selectedItems():
            list_widget.takeItem(list_widget.row(item))
        self._sync_multilayer_value(name)

    def _clear_multilayer_items(self, name: str) -> None:
        list_widget = self._multilayer_lists.get(name)
        if list_widget is None:
            return
        list_widget.clear()
        self._sync_multilayer_value(name)

    def _add_project_layers_to(self, name: str) -> None:
        """从当前工程图层里多选添加（按参数允许的图层类型过滤）。"""
        pdef = self._multilayer_pdefs.get(name)
        list_widget = self._multilayer_lists.get(name)
        if pdef is None or list_widget is None:
            return
        selected_ids = {
            list_widget.item(i).data(Qt.ItemDataRole.UserRole)
            for i in range(list_widget.count())
        }
        candidates = [
            layer
            for layer in layers_in_tree_order(self.project)
            if layer.id() not in selected_ids and self._layer_fits_parameter(pdef, layer)
        ]
        if not candidates:
            MessageBox.warning(
                self,
                self._algo_title,
                "当前工程里没有可添加的图层（可能已全部加入，或类型不匹配）",
            )
            return
        try:
            picker = _ProjectLayerPickerDialog(self, candidates)
            try:
                if not picker.exec():
                    return
                layer_ids = picker.selected_layer_ids()
            finally:
                picker.deleteLater()
            for layer_id in layer_ids:
                self._append_multilayer_item(
                    name, self.project.mapLayer(layer_id), sync=False
                )
            self._sync_multilayer_value(name)
        except Exception as exc:  # noqa: BLE001
            logger.error("选择工程图层失败：%s", exc, exc_info=True)
            MessageBox.warning(self, self._algo_title, f"选择图层失败：{exc}")

    def _add_files_to_multilayer(self, name: str) -> None:
        """从文件添加；加载后把类型匹配的新图层追加到列表。"""
        pdef = self._multilayer_pdefs.get(name)
        if pdef is None:
            return
        new_layers = self._load_new_layers_via_picker()
        if not new_layers:
            return
        fits = [layer for layer in new_layers if self._layer_fits_parameter(pdef, layer)]
        if not fits:
            MessageBox.warning(
                self, self._algo_title, "所选数据与本参数要求的图层类型不匹配"
            )
            return
        for layer in fits:
            self._append_multilayer_item(name, layer, sync=False)
        self._sync_multilayer_value(name)

    def _refresh_multilayer_lists(self) -> None:
        """工程图层增删后，移除列表里已删除的图层并重新同步 wrapper。"""
        for name, list_widget in self._multilayer_lists.items():
            for index in reversed(range(list_widget.count())):
                layer_id = list_widget.item(index).data(Qt.ItemDataRole.UserRole)
                if self.project.mapLayer(layer_id) is None:
                    list_widget.takeItem(index)
            self._sync_multilayer_value(name)

    def _customize_extent_widget(self, pdef, wrapper, widget) -> None:
        """把范围控件的「…」按钮换成「使用当前画布范围」图标 + ▼ 菜单。

        QGIS 的 ``QgsExtentWidget`` 用一个 ``MenuButtonPopup`` 的 QToolButton
        承载菜单；本主题隐藏了 ``menu-indicator``，按钮只剩图标 + 半截高亮底，
        看起来像坏了一样。这里隐藏原按钮，补两颗独立按钮（与 QGIS 的观感一致），
        菜单也只保留本系统真正支持的入口 —— 布局地图 / 书签不存在，点了没反应。

        左侧图标按钮**直接用当前画布范围**（用户要求不再默认进入地图绘制）；
        确实需要框选时，仍可从 ▼ 菜单选「在地图画布上绘制」。
        """
        line_edit = widget.findChild(QWidget, "mCondensedLineEdit")
        frame = widget.findChild(QWidget, "mCondensedFrame")
        old_button = widget.findChild(QToolButton, "mCondensedToolButton")
        if frame is None or old_button is None:
            return
        layout = frame.layout()
        if layout is None:
            return

        old_button.setVisible(False)
        layout.setSpacing(4)
        if line_edit is not None:
            index = layout.indexOf(line_edit)
            if index >= 0:
                layout.setStretch(index, 1)

        canvas_btn = QToolButton(frame)
        canvas_btn.setObjectName("ExtentActionButton")
        # 图标沿用 QGIS 原按钮的（地图 + 光标）：用户按位置认这颗按钮，
        # 换成别的图标反而找不到；行为已改为「取当前画布范围」。
        icon = old_button.icon()
        canvas_btn.setIcon(
            icon
            if not icon.isNull()
            else QgsApplication.getThemeIcon("mActionMapIdentification.svg")
        )
        canvas_btn.setIconSize(QSize(16, 16))
        canvas_btn.setFixedSize(26, 26)
        canvas_btn.setToolTip("使用当前画布范围")
        canvas_btn.clicked.connect(
            lambda _checked=False, w=wrapper: self._set_extent_from_canvas(w)
        )
        layout.addWidget(canvas_btn)

        menu_btn = QToolButton(frame)
        menu_btn.setObjectName("ExtentActionButton")
        menu_btn.setIcon(self._extent_menu_icon())
        menu_btn.setIconSize(QSize(10, 8))
        menu_btn.setToolTip("从图层 / 当前画布范围计算")
        menu_btn.setPopupMode(QToolButton.ToolButtonPopupMode.InstantPopup)
        menu_btn.setMenu(self._build_extent_menu(wrapper, menu_btn))
        layout.addWidget(menu_btn)

    @staticmethod
    def _extent_menu_icon() -> QIcon:
        """主题色下三角（主题 QSS 隐藏了 QToolButton 自带的菜单指示器）。"""
        try:
            from app.ui.common.theme import ThemeManager, _arrow_url  # noqa: PLC0415

            path = _arrow_url(ThemeManager.instance().current().menu_text)
            return QIcon(path)
        except Exception:  # noqa: BLE001
            return QIcon()

    def _build_extent_menu(self, wrapper, parent) -> QMenu:
        """范围菜单：从图层计算 / 使用当前画布范围 / 在地图画布上绘制。

        不再放 QGIS 的「从布局地图计算」「从书签计算」——本系统没有布局与书签，
        它们的子菜单永远是空的，点了没有任何反应。
        """
        menu = QMenu(parent)
        layer_menu = menu.addMenu("从图层计算")

        def _populate_layers() -> None:
            """每次展开都按当前工程重建图层列表（工具弹窗会被缓存复用）。"""
            layer_menu.clear()
            for layer in layers_in_tree_order(self.project):
                try:
                    rect = layer.extent()
                    crs = layer.crs()
                except (AttributeError, RuntimeError):
                    continue
                if rect is None or rect.isNull() or rect.isEmpty():
                    continue
                if not crs.isValid():
                    continue
                action = layer_menu.addAction(self._layer_label(layer))
                action.triggered.connect(
                    lambda _checked=False, r=rect, c=crs, w=wrapper: self._set_extent_value(
                        r, c, w
                    )
                )
            layer_menu.setEnabled(not layer_menu.isEmpty())

        def _populate_safe(*_args) -> None:
            try:
                _populate_layers()
            except Exception:  # noqa: BLE001
                logger.warning("刷新范围图层菜单失败", exc_info=True)

        _populate_layers()
        menu.aboutToShow.connect(_populate_safe)

        menu.addSeparator()
        menu.addAction(
            "使用当前画布范围", lambda _checked=False, w=wrapper: self._set_extent_from_canvas(w)
        )
        menu.addAction(
            "在地图画布上绘制", lambda _checked=False, w=wrapper: self._start_extent_draw(w)
        )
        return menu

    @staticmethod
    def _extent_text(rect, crs=None) -> str:
        """``QgsRectangle``（+ 可选 CRS）→ QGIS 范围参数字符串。"""
        text = (
            f"{rect.xMinimum()},{rect.xMaximum()},"
            f"{rect.yMinimum()},{rect.yMaximum()}"
        )
        if crs is not None and crs.isValid():
            text += f" [{crs.authid() or crs.toWkt()}]"
        return text

    def _set_extent_value(self, rect, crs, wrapper) -> None:
        """把范围（``QgsRectangle`` + CRS）写进 QGIS 范围控件。"""
        if rect is None or rect.isNull() or rect.isEmpty():
            return
        try:
            wrapper.setWidgetValue(
                self._extent_text(rect, crs), self._ctx_gen.processingContext()
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning("写入范围参数失败：%s", exc, exc_info=True)
            MessageBox.warning(self, self._algo_title, f"设置范围失败：{exc}")

    def _set_extent_from_canvas(self, wrapper) -> None:
        canvas = self._canvas
        if canvas is None:
            return
        try:
            self._set_extent_value(
                canvas.extent(),
                canvas.mapSettings().destinationCrs(),
                wrapper,
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning("读取画布范围失败：%s", exc, exc_info=True)

    def _start_extent_draw(self, wrapper) -> None:
        """在地图画布上画矩形取范围；画完立即清掉橡皮筋并还原对话框。"""
        canvas = self._canvas
        if canvas is None:
            MessageBox.warning(
                self, self._algo_title, "当前窗口没有地图画布，无法在地图上绘制范围"
            )
            return
        try:
            prev_tool = canvas.mapTool()
            tool = QgsMapToolExtent(canvas)
            self._extent_tool = tool
            #: 收尾只做一次：extentChanged 触发后 setMapTool 还会再发 deactivated
            done = {"value": False}

            def _cleanup(restore_tool: bool) -> None:
                if done["value"]:
                    return
                done["value"] = True
                for signal, slot in (
                    (tool.extentChanged, _finished),
                    (tool.deactivated, _on_deactivated),
                ):
                    try:
                        signal.disconnect(slot)
                    except (TypeError, RuntimeError):
                        pass
                # 用户明确要求：画完自动清除红色范围框，否则没有工具能清它
                try:
                    tool.clearRubberBand()
                    canvas.refresh()
                except Exception:  # noqa: BLE001
                    logger.debug("清除范围橡皮筋失败", exc_info=True)
                if restore_tool:
                    try:
                        if prev_tool is not None:
                            canvas.setMapTool(prev_tool)
                        else:
                            canvas.unsetMapTool(tool)
                    except Exception:  # noqa: BLE001
                        logger.debug("还原地图工具失败", exc_info=True)
                self._extent_tool = None
                self._restore_after_canvas_pick()

            def _finished(rect=None, *_args) -> None:
                try:
                    drawn = rect if rect is not None and not rect.isNull() else tool.extent()
                    self._set_extent_value(
                        drawn,
                        canvas.mapSettings().destinationCrs(),
                        wrapper,
                    )
                except Exception as exc:  # noqa: BLE001
                    logger.warning("绘制范围失败：%s", exc, exc_info=True)
                finally:
                    _cleanup(restore_tool=True)

            def _on_deactivated(*_args) -> None:
                # Esc / 切换到别的工具：同样清掉画了一半的框并还原对话框
                _cleanup(restore_tool=False)

            tool.extentChanged.connect(_finished)
            tool.deactivated.connect(_on_deactivated)
            canvas.setMapTool(tool)
            # 对话框会挡住画布，先最小化；画完（_finished）再还原
            self.showMinimized()
        except Exception as exc:  # noqa: BLE001
            logger.error("启动范围绘制失败：%s", exc, exc_info=True)
            MessageBox.warning(self, self._algo_title, f"无法在地图上绘制范围：{exc}")

    def _restore_after_canvas_pick(self) -> None:
        """画布取点 / 绘制范围结束后把对话框拉回前台。"""
        try:
            # 用户中途关掉了弹窗时不要再把它弹出来；最小化状态下 isVisible
            # 可能为假，所以用 isMinimized 兜底。
            if not self.isVisible() and not self.isMinimized():
                return
            self.showNormal()
            self.raise_()
            self.activateWindow()
        except RuntimeError:
            pass

    # ------------------------------------------------------ 图层顺序

    def _replace_layer_combo(self, pdef, wrapper, widget) -> None:
        """用按图层面板顺序排列的 QComboBox 替换 QGIS 自带的图层 combo。

        QGIS 的 ``QgsMapLayerComboBox`` 按图层 id 排序（``QgsProject.mapLayers()``
        的底层顺序），在用户看来就是乱序；它的模型是 C++ 私有类，无法直接排序。
        这里保留原 combo（隐藏）继续作为 wrapper 的取值来源，在同一个布局位置
        放自建下拉框，选项按 ``layerTreeRoot().findLayers()``（图层面板从上到下）
        排列；用户选择后再写回隐藏 combo。
        """
        source = self._find_layer_combo(widget)
        if source is None:
            return
        row = self._layout_containing(widget, source)
        if row is None:
            return
        index = row.indexOf(source)
        combo = QComboBox()
        combo.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred)
        combo.setToolTip("图层顺序与图层面板一致")
        if index >= 0:
            row.insertWidget(index, combo)
        else:
            row.addWidget(combo)
        # 不把原 combo 移出布局：隐藏即可（它仍留在原位置，占 0 宽）。
        # 同时保留 Python 引用，防止 C++ 对象被回收（见 _hidden_layer_combos 注释）。
        source.setVisible(False)
        self._hidden_layer_combos[pdef.name()] = source
        self._layer_combos[pdef.name()] = combo
        param_name = pdef.name()
        combo.currentIndexChanged.connect(
            lambda _index, name=param_name: self._sync_layer_value(name)
        )
        self._populate_layer_combo(pdef.name(), initial=True)

    @staticmethod
    def _find_layer_combo(widget) -> QComboBox | None:
        """找 wrapper 里真正的图层下拉框（C++ 类名 QgsMapLayerComboBox）。"""
        for combo in widget.findChildren(QComboBox):
            if combo.metaObject().className() == "QgsMapLayerComboBox":
                return combo
        return None

    def _populate_layer_combo(self, name: str, *, initial: bool = False) -> None:
        """按图层树顺序重建某个参数的自建下拉框，并尽量保持原选择。"""
        combo = self._layer_combos.get(name)
        wrapper = self._wrappers.get(name)
        if combo is None or wrapper is None:
            return
        pdef = wrapper.parameterDefinition()
        optional = bool(
            pdef.flags() & QgsProcessingParameterDefinition.Flag.FlagOptional
        )
        old_id = combo.currentData() if combo.currentIndex() >= 0 else None
        old_none = combo.currentIndex() < 0
        layers = [
            layer
            for layer in layers_in_tree_order(self.project)
            if self._layer_fits_parameter(pdef, layer)
        ]

        combo.blockSignals(True)
        combo.clear()
        if optional:
            combo.addItem("", None)
        for layer in layers:
            combo.addItem(self._layer_label(layer), layer.id())

        if initial or old_none:
            target = -1 if optional else (0 if combo.count() else -1)
        else:
            target = combo.findData(old_id) if old_id else -1
            if target < 0 and not optional:
                target = 0 if combo.count() else -1
        combo.setCurrentIndex(target)
        combo.blockSignals(False)
        self._sync_layer_value(name)

    def _refresh_layer_combos(self, *args) -> None:
        """图层增删 / 拖动排序后，重建所有自建图层下拉框。"""
        for name in list(self._layer_combos):
            self._populate_layer_combo(name)

    def _sync_layer_value(self, name: str) -> None:
        """把自建下拉框的选择写回 QGIS 隐藏 combo（wrapper 的取值来源）。"""
        combo = self._layer_combos.get(name)
        wrapper = self._wrappers.get(name)
        if combo is None or wrapper is None:
            return
        layer_id = combo.currentData() if combo.currentIndex() >= 0 else None
        layer = self.project.mapLayer(layer_id) if layer_id else None
        try:
            wrapper.setWidgetValue(layer, self._ctx_gen.processingContext())
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "同步图层参数 %s 失败：%s", name, exc, exc_info=True
            )
        if name == "INPUT":
            self._sync_geometry_column(layer)
        self._clear_fid_selection(name)
        # 父图层变化后 QGIS wrapper 会把字段下拉框重新填满，等它填完再清一次
        self._queue_fid_clear(name)

    def _queue_fid_clear(self, parent_name: str | None = None) -> None:
        """延迟一次 FID 选择清理：字段下拉框由 wrapper 异步重填后才稳定。

        回调里兜住 ``RuntimeError``：对话框可能在事件队列处理前就被销毁，
        PyQt 槽里未捕获的异常会直接杀进程。
        """
        QTimer.singleShot(0, lambda name=parent_name: self._clear_fid_selection_safe(name))

    def _clear_fid_selection_safe(self, parent_name: str | None = None) -> None:
        try:
            self._clear_fid_selection(parent_name)
        except RuntimeError:
            pass

    def _sync_geometry_column(self, layer=None) -> None:
        """按输入图层的真实几何列名更新 ``GEOMETRY`` 参数。

        GDAL 算法默认写 ``geometry``，但 GeoPackage 的几何列是 ``geom``、
        FileGDB 是 ``SHAPE``；不修正会在执行时被 GDAL 3.13 的 SQLite 方言
        拒绝（``no such column: geometry``）。只在字段仍是算法默认值或上一次
        自动填入的列名时覆盖，用户手工改过的值保持不动。
        """
        wrapper = self._wrappers.get("GEOMETRY")
        if wrapper is None:
            return
        if layer is None:
            input_wrapper = self._wrappers.get("INPUT")
            if input_wrapper is None:
                return
            layer = self._input_vector(input_wrapper.parameterValue())
        else:
            layer = self._input_vector(layer)
        if layer is None:
            return
        try:
            current = wrapper.parameterValue()
            default = wrapper.parameterDefinition().defaultValue() or ""
        except Exception:  # noqa: BLE001
            return
        managed = current in (None, "", default) or current == self._auto_geometry_value
        if not managed:
            return
        column = geometry_column_name(layer) or default
        if not column or column == current:
            return
        try:
            wrapper.setWidgetValue(column, self._ctx_gen.processingContext())
        except Exception:  # noqa: BLE001
            logger.debug("同步几何列名失败", exc_info=True)
            return
        self._auto_geometry_value = column

    def _clear_fid_selection(self, parent_name: str | None = None) -> None:
        """清掉字段下拉框里自动选中的 FID 伪字段（仅 ``gdal:*`` 算法）。

        QGIS 的 OGR provider 把 FID 列当普通字段暴露（FileGDB 的
        ``OBJECTID``、SQLite 的 ``rowid`` 等），但 GDAL 命令行工具按真实
        属性字段查找，``gdal_rasterize -a OBJECTID`` 只会报
        ``Failed to find field OBJECTID on layer ...``。

        QGIS 的 ``QgsFieldComboBox`` 由 ``QgsFieldModel`` 驱动，无法移除单项；
        这里在它自动选中 FID 列时清空选择，让必填校验提示用户显式挑选。
        用户仍能选到 FID 列，执行时 service 会给出明确提示（见
        :meth:`GenericAlgService._check_fid_field`）。
        """
        if not self._algo_id.startswith("gdal:"):
            return
        for wrapper in self._wrappers.values():
            pdef = wrapper.parameterDefinition()
            if not isinstance(pdef, QgsProcessingParameterField):
                continue
            parent = pdef.parentLayerParameterName()
            if parent_name is not None and parent != parent_name:
                continue
            parent_wrapper = self._wrappers.get(parent)
            if parent_wrapper is None:
                continue
            layer = self._input_vector(parent_wrapper.parameterValue())
            if layer is None:
                continue
            fid = fid_column_name(layer)
            if not fid:
                continue
            widget = wrapper.wrappedWidget()
            if isinstance(widget, QComboBox) and widget.currentText() == fid:
                widget.setCurrentIndex(-1)

    @staticmethod
    def _layer_label(layer) -> str:
        """下拉框显示文字，与 QGIS 一致：``名称 [EPSG:xxxx]``。"""
        try:
            crs = layer.crs()
            code = crs.authid() if crs.isValid() else ""
        except (AttributeError, RuntimeError):
            code = ""
        return f"{layer.name()} [{code}]" if code else layer.name()

    def _pick_data_for(self, wrapper) -> None:
        """给图层参数打开统一「添加数据」弹窗；加载后自动选中新图层。"""
        new_layers = self._load_new_layers_via_picker()
        if not new_layers:
            return
        pdef = wrapper.parameterDefinition()
        fits = [layer for layer in new_layers if self._layer_fits_parameter(pdef, layer)]
        if not fits:
            MessageBox.warning(
                self,
                self._algo_title,
                "所选数据与当前参数要求的图层类型不匹配",
            )
            return
        combo = self._layer_combos.get(pdef.name())
        if combo is not None:
            index = combo.findData(fits[0].id())
            if index >= 0:
                combo.setCurrentIndex(index)  # 触发 _sync_layer_value 写回 wrapper
                return
        try:
            wrapper.setWidgetValue(fits[0], self._ctx_gen.processingContext())
        except Exception as exc:  # noqa: BLE001
            logger.warning("选中输入图层失败：%s", exc, exc_info=True)
            MessageBox.warning(self, self._algo_title, f"选中输入图层失败：{exc}")

    def _load_new_layers_via_picker(self) -> list:
        """打开统一「添加数据」弹窗并加载，返回本次新增的图层（无新增返回空列表）。"""
        from app.ui.dialogs.add_data_dialog import AddDataDialog  # noqa: PLC0415

        picker = AddDataDialog(self)
        try:
            accepted = picker.exec()
            selections = picker.selected_sources() if accepted else []
        finally:
            picker.deleteLater()
        if not selections:
            return []

        before = set(self.project.mapLayers().keys())
        parent = self.parent()
        loader = getattr(parent, "_add_data_sources", None)
        try:
            if callable(loader):
                loader(selections)
            else:
                self._load_selections_fallback(selections)
        except Exception as exc:  # noqa: BLE001
            logger.error("添加数据失败：%s", exc, exc_info=True)
            MessageBox.critical(self, self._algo_title, f"添加数据失败：{exc}")
            return []

        self._refresh_wrappers()
        return [
            layer
            for layer_id, layer in self.project.mapLayers().items()
            if layer_id not in before
        ]

    def _load_selections_fallback(self, selections) -> None:
        """父窗口没有 ``_add_data_sources`` 时（测试 / 独立窗口）自行加载。"""
        from app.core.util.data_formats import RASTER_EXTS, suffix_of
        from app.core.util.sublayers import open_selections
        from qgis.core import QgsRasterLayer

        vector_selections = []
        for selection in selections:
            path = str(getattr(selection, "path", "") or "")
            if getattr(selection, "sublayer", None) is None and suffix_of(path) in RASTER_EXTS:
                layer = QgsRasterLayer(
                    path, os.path.splitext(os.path.basename(path))[0]
                )
                if layer.isValid():
                    self.project.addMapLayer(layer)
                continue
            vector_selections.append(selection)
        if vector_selections:
            layers, errors = open_selections(vector_selections)
            for layer in layers:
                self.project.addMapLayer(layer)
            for error in errors:
                logger.warning("添加数据：%s", error)

    @staticmethod
    def _layer_fits_parameter(pdef, layer) -> bool:
        """新加载的图层能否用于该参数（矢量 / 栅格 / 多图层 / 几何类型）。"""
        if isinstance(pdef, QgsProcessingParameterRasterLayer):
            return layer.type() == QgsMapLayer.RasterLayer
        if isinstance(pdef, QgsProcessingParameterVectorLayer):
            return layer.type() == QgsMapLayer.VectorLayer
        if isinstance(pdef, QgsProcessingParameterFeatureSource):
            return GenericAlgDialog._feature_source_fits(pdef, layer)
        if isinstance(pdef, QgsProcessingParameterMultipleLayers):
            try:
                layer_type = pdef.layerType()
            except Exception:  # noqa: BLE001
                return False
            if layer_type == QgsProcessing.TypeRaster:
                return layer.type() == QgsMapLayer.RasterLayer
            return layer.type() == QgsMapLayer.VectorLayer
        return False

    @staticmethod
    def _feature_source_fits(pdef, layer) -> bool:
        """FeatureSource 还带几何类型限制（如 Grid 工具只要点图层）。"""
        if layer.type() != QgsMapLayer.VectorLayer:
            return False
        try:
            wanted = list(pdef.dataTypes())
        except Exception:  # noqa: BLE001
            return True
        if not wanted:
            return True
        geometry = layer.geometryType()
        geometry_map = {
            QgsProcessing.TypeVectorPoint: Qgis.GeometryType.Point,
            QgsProcessing.TypeVectorLine: Qgis.GeometryType.Line,
            QgsProcessing.TypeVectorPolygon: Qgis.GeometryType.Polygon,
        }
        for data_type in wanted:
            if data_type in (
                QgsProcessing.TypeVector,
                QgsProcessing.TypeVectorAnyGeometry,
            ):
                return True
            if data_type in geometry_map and geometry == geometry_map[data_type]:
                return True
        return False

    # ----------------------------------------------------------- execute

    def _first_invalid_parameter(self):
        """第一个「必填但为空」的参数 ``(name, pdef, widget)``；都填了返回 ``None``。

        分析工具是独立弹窗，必填项校验不再写主窗口 messagebar（会被弹窗挡住），
        而是把焦点 / 高亮直接落到对应控件上。判定规则保守：只认非可选、非输出
        参数，值缺失 = ``None`` / 空字符串 / 空列表 / 无效 CRS；布尔参数 False
        是合法值，不在此列。
        """
        for name, wrapper in self._wrappers.items():
            pdef = wrapper.parameterDefinition()
            if pdef.isDestination():
                continue
            if pdef.flags() & QgsProcessingParameterDefinition.Flag.FlagOptional:
                continue
            try:
                value = wrapper.parameterValue()
            except Exception:  # noqa: BLE001
                continue
            if not self._parameter_value_missing(pdef, value):
                continue
            widget = self._widget_for_parameter(name)
            if widget is not None:
                return name, pdef, widget
        return None

    def _widget_for_parameter(self, name: str):
        """参数对应的**可见**控件（图层 / 坐标系参数用自建控件，不是隐藏的原控件）。"""
        combo = self._layer_combos.get(name)
        if combo is not None:
            return combo
        crs_combo = self._crs_combos.get(name)
        if crs_combo is not None:
            return crs_combo
        multilayer = self._multilayer_lists.get(name)
        if multilayer is not None:
            return multilayer
        wrapper = self._wrappers.get(name)
        return wrapper.wrappedWidget() if wrapper is not None else None

    def _reveal_parameter_widget(self, widget) -> None:
        """必填的高级参数为空时，先展开「高级」分组再定位。"""
        toggle = getattr(self, "_advanced_toggle", None)
        if toggle is None:
            return
        try:
            if not widget.isVisibleTo(self) and not toggle.isChecked():
                toggle.setChecked(True)
        except RuntimeError:
            pass

    @staticmethod
    def _parameter_value_missing(pdef, value) -> bool:
        if isinstance(pdef, QgsProcessingParameterBoolean):
            return False  # False 是合法取值
        if value is None:
            return True
        if isinstance(value, str):
            return not value.strip()
        if isinstance(value, (list, tuple)):
            return len(value) == 0
        is_valid = getattr(value, "isValid", None)
        if callable(is_valid):
            try:
                return not is_valid()
            except Exception:  # noqa: BLE001
                return False
        return False

    # ------------------------------------------------------ 算法组合校验

    def _algorithm_specific_problem(self):
        """参数之间的组合约束 → ``(控件, 提示)``；没问题返回 ``None``。

        通用的「必填 / 最小值」判断不了某些算法的组合要求，典型就是
        ``gdal:rasterize``：宽 / 高的最小值是 0，但按像素输出时二者都必须
        大于 0，否则 gdal_rasterize 直接以 usage 错误退出。
        """
        if self._algo_id == "gdal:rasterize":
            return self._rasterize_size_problem()
        if self._algo_id == "gdal:rgbtopct":
            return self._rgbtopct_colors_problem()
        if self._algo_id == "gdal:merge":
            return self._merge_band_problem()
        return None

    def _boolean_value(self, name: str):
        wrapper = self._wrappers.get(name)
        if wrapper is None:
            return None
        try:
            return bool(wrapper.parameterValue())
        except Exception:  # noqa: BLE001
            return None

    def _rgbtopct_colors_problem(self):
        """RGB 转 PCT 的「颜色数」必须 ≥ 2（rgb2pct 脚本硬性要求 2–256）。"""
        wrapper = self._wrappers.get("NCOLORS")
        if wrapper is None:
            return None
        try:
            value = wrapper.parameterValue()
        except Exception:  # noqa: BLE001
            return None
        if value is None:
            return None  # 交给通用必填校验
        try:
            count = int(value)
        except (TypeError, ValueError):
            return None
        if count < 2:
            return (
                self._widget_for_parameter("NCOLORS"),
                "颜色数至少为 2（rgb2pct 要求 2–256；只取 1 种颜色无法构成调色板）",
            )
        return None

    def _merge_band_problem(self):
        """拼接栅格：不勾「各输入独立波段」时，所有输入的波段数必须一致。

        gdal_merge 以第一个输入的波段数去读其余文件，波段数不同会抛
        ``GDALDataset::GetRasterBand(N) - Illegal band #`` 的 Python 堆栈
        （用户看到的是一屏箭头）。这里提前拦下并说明是哪个图层。
        """
        list_widget = self._multilayer_lists.get("INPUT")
        if list_widget is None or list_widget.count() < 2:
            return None
        if self._boolean_value("SEPARATE"):
            return None  # 独立波段模式允许不同波段数
        by_bands: dict[int, str] = {}
        for index in range(list_widget.count()):
            layer = self.project.mapLayer(
                list_widget.item(index).data(Qt.ItemDataRole.UserRole)
            )
            if layer is None:
                continue
            try:
                bands = int(layer.bandCount())
            except Exception:  # noqa: BLE001
                continue
            by_bands.setdefault(bands, layer.name())
        if len(by_bands) < 2:
            return None
        detail = "、".join(
            f"「{name}」{bands} 个波段" for bands, name in sorted(by_bands.items())
        )
        return (
            list_widget,
            f"拼接要求所有输入的波段数一致，当前为 {detail}；"
            "若想保留各自的波段，请勾选「将各输入文件置于不同的单独波段」",
        )

    # ------------------------------------------------------ 算法参数修正

    def _adjust_algorithm_params(self, params: dict) -> dict:
        """执行前按算法语义修正参数（目前只有「按范围裁剪栅格」）。

        修正失败抛 ``ValueError``（消息直接展示给用户），由 :meth:`_on_execute`
        定位到对应控件。
        """
        if self._algo_id == "gdal:cliprasterbyextent":
            return self._clip_extent_to_raster(params)
        if self._algo_id == "gdal:cliprasterbymasklayer":
            return self._prepare_mask_clip(params)
        return params

    def _clip_extent_to_raster(self, params: dict) -> dict:
        """把裁剪范围与输入栅格求交后写回参数。

        ``gdal_translate -projwin`` 会把栅格范围外的区域按源分辨率补齐
        （填 NoData / 0）。用户从别的图层取整层范围时，范围常比栅格大很多，
        输出窗口会膨胀到几十万像素、需要上百 GB 磁盘，GDAL 直接报错。
        裁剪的语义本来就不应超出输入栅格，这里按交集写回控件并提示用户。
        """
        wrapper = self._wrappers.get("PROJWIN")
        if wrapper is None or not params.get("PROJWIN"):
            return params
        raster = self._input_raster(params.get("INPUT"))
        if raster is None:
            return params
        ctx = self._ctx_gen.processingContext()
        try:
            rect = QgsProcessingParameters.parameterAsExtent(
                wrapper.parameterDefinition(), params, ctx, raster.crs()
            )
        except Exception:  # noqa: BLE001
            logger.debug("裁剪范围解析失败，交给算法自行校验", exc_info=True)
            return params

        raster_extent = raster.extent()
        if (
            rect.isNull()
            or rect.isEmpty()
            or raster_extent.isNull()
            or raster_extent.isEmpty()
        ):
            return params
        clipped = rect.intersect(raster_extent)
        if clipped.isNull() or clipped.isEmpty():
            crs_code = raster.crs().authid() or raster.crs().description()
            raise ValueError(
                "所选范围与输入栅格不相交。栅格范围约为 "
                f"{raster_extent.xMinimum():.3f}, {raster_extent.xMaximum():.3f}, "
                f"{raster_extent.yMinimum():.3f}, {raster_extent.yMaximum():.3f}"
                f"（{crs_code}），请重新选择裁剪范围"
            )
        if clipped.toString() == rect.toString():
            return params

        text = self._extent_text(clipped, raster.crs())
        params["PROJWIN"] = text
        try:
            wrapper.setWidgetValue(text, ctx)
        except Exception:  # noqa: BLE001
            logger.debug("回写裁剪范围失败", exc_info=True)
        send(
            "info",
            "所选范围超出输入栅格，已自动按「范围 ∩ 栅格范围」裁剪",
            duration=4,
        )
        return params

    def _input_raster(self, value):
        """输入参数值 → 有效的 ``QgsRasterLayer``（参数值可能是图层 id 字符串）。"""
        layer = value if isinstance(value, QgsRasterLayer) else None
        if layer is None and isinstance(value, str):
            layer = self.project.mapLayer(value)
        if isinstance(layer, QgsRasterLayer) and layer.isValid():
            return layer
        return None

    def _prepare_mask_clip(self, params: dict) -> dict:
        """按掩膜裁剪：清洗掩膜几何，并把输出范围限制在输入栅格内。

        - 掩膜里的空几何 / 自相交多边形会让 ``gdalwarp -cutline`` 直接失败，
          先走 :func:`copy_with_valid_polygons` 清洗；
        - ``-crop_to_cutline`` 会**忽略 ``-te``**，始终按掩膜整层范围作为输出
          范围；掩膜比栅格大时会按源分辨率生成几十万像素的靶图（可上百 GB）。
          所以这里自己算好「掩膜 ∩ 栅格」（用户填了目标范围则用「目标 ∩ 栅格」）
          写进 TARGET_EXTENT，并临时取消 CROP_TO_CUTLINE，让 ``-te`` 生效。
        """
        raster = self._input_raster(params.get("INPUT"))
        mask = self._input_vector(params.get("MASK"))
        if raster is None or mask is None:
            return params

        cleaned = copy_with_valid_polygons(mask)
        if cleaned is not mask:
            params["MASK"] = cleaned

        wrapper = self._wrappers.get("TARGET_EXTENT")
        if wrapper is None:
            return params
        crop = bool(params.get("CROP_TO_CUTLINE"))
        user_value = params.get("TARGET_EXTENT")

        raster_extent = raster.extent()
        mask_extent = self._transformed_extent(cleaned, raster)
        if (
            raster_extent.isNull()
            or raster_extent.isEmpty()
            or mask_extent is None
            or mask_extent.isEmpty()
        ):
            return params

        ctx = self._ctx_gen.processingContext()
        target = None
        if user_value:
            try:
                target = QgsProcessingParameters.parameterAsExtent(
                    wrapper.parameterDefinition(), params, ctx, raster.crs()
                )
            except Exception:  # noqa: BLE001
                logger.debug("目标范围解析失败，交给算法自行校验", exc_info=True)
                return params
            if target.isNull() or target.isEmpty():
                target = None

        # 不需要干预的两种情况：没填目标范围且掩膜本来就在栅格内（crop 行为正确），
        # 或者没填目标范围、也没勾选 crop（gdalwarp 默认按栅格范围输出）
        if target is None and (not crop or raster_extent.contains(mask_extent)):
            return params

        if target is not None:
            bounded = target.intersect(raster_extent)
            if bounded.isNull() or bounded.isEmpty():
                raise ValueError("所选目标范围与输入栅格不相交，请重新选择")
            if mask_extent.intersect(bounded).isEmpty():
                raise ValueError("所选目标范围与掩膜图层不相交，请重新选择")
            changed = bounded.toString() != target.toString()
        else:
            bounded = mask_extent.intersect(raster_extent)
            changed = True
        if bounded.isNull() or bounded.isEmpty():
            raise ValueError(
                "掩膜图层与输入栅格不相交：栅格范围约为 "
                f"{self._extent_summary(raster_extent)}（{raster.crs().authid()}），"
                "请确认掩膜图层与栅格是同一区域"
            )

        text = self._extent_text(bounded, raster.crs())
        params["TARGET_EXTENT"] = text
        try:
            wrapper.setWidgetValue(text, ctx)
        except Exception:  # noqa: BLE001
            logger.debug("回写目标范围失败", exc_info=True)

        if crop:
            # crop_to_cutline 优先级高于 -te，必须关掉才能让目标范围生效
            params["CROP_TO_CUTLINE"] = False
            crop_wrapper = self._wrappers.get("CROP_TO_CUTLINE")
            if crop_wrapper is not None:
                try:
                    crop_wrapper.setWidgetValue(False, ctx)
                except Exception:  # noqa: BLE001
                    logger.debug("回写裁剪到掩膜范围开关失败", exc_info=True)

        if changed or crop:
            send(
                "info",
                "已按「取范围 ∩ 栅格范围」限定输出范围"
                "（gdalwarp 的“范围与掩膜一致”会忽略目标范围，已自动取消）",
                duration=5,
            )
        return params

    @staticmethod
    def _extent_summary(rect) -> str:
        return (
            f"{rect.xMinimum():.3f}, {rect.xMaximum():.3f}, "
            f"{rect.yMinimum():.3f}, {rect.yMaximum():.3f}"
        )

    def _transformed_extent(self, layer, raster):
        """图层范围转换到栅格 CRS；转换失败返回 ``None``。"""
        extent = layer.extent()
        if extent.isNull() or extent.isEmpty():
            return None
        try:
            if layer.crs() == raster.crs() or not layer.crs().isValid():
                return extent
            transform = QgsCoordinateTransform(layer.crs(), raster.crs(), self.project)
            return transform.transformBoundingBox(extent)
        except Exception:  # noqa: BLE001
            logger.debug("掩膜范围坐标转换失败", exc_info=True)
            return None

    def _input_vector(self, value):
        """输入参数值 → 有效的 ``QgsVectorLayer``（参数值可能是图层 id 字符串）。"""
        layer = value if isinstance(value, QgsVectorLayer) else None
        if layer is None and isinstance(value, str):
            layer = self.project.mapLayer(value)
        if isinstance(layer, QgsVectorLayer) and layer.isValid():
            return layer
        return None

    def _rasterize_size_problem(self):
        """矢量转栅格的宽 / 高校验（按单位给不同提示）。"""
        if not {"UNITS", "WIDTH", "HEIGHT"} <= set(self._wrappers):
            return None
        try:
            units = self._wrappers["UNITS"].parameterValue()
        except Exception:  # noqa: BLE001
            return None
        if units is None:
            return None  # 单位没选，交给通用必填校验

        def _number(name: str) -> float:
            try:
                value = self._wrappers[name].parameterValue()
                return float(value or 0)
            except (TypeError, ValueError):
                return 0.0

        for name in ("WIDTH", "HEIGHT"):
            if _number(name) > 0:
                continue
            widget = self._widget_for_parameter(name)
            if units == 0:
                message = (
                    "按「像素」输出时，宽度和高度都必须大于 0（当前为 0，"
                    "gdal_rasterize 会直接报错）"
                )
            else:
                message = (
                    "按「地理参考单位」输出时，宽度和高度是分辨率，必须大于 0"
                    "（当前为 0，gdal_rasterize 会直接报错）"
                )
            return widget, message
        return None

    @staticmethod
    def _missing_output_path(results: dict, primary_output) -> str:
        """预期输出文件不存在时返回该路径，否则返回空串。

        只检查「路径型」输出：图层对象（native 算法 memory sink）与
        ``memory:`` 字符串不在此列。GDAL 命令行失败时 QGIS 包装不会让任务
        失败，这个检查能把「执行失败」和「结果加载失败」区分开。
        """
        if primary_output is None:
            return ""
        value = results.get(primary_output.name())
        if not isinstance(value, str) or value.startswith("memory:"):
            return ""
        if not value.strip():
            return ""
        return "" if os.path.exists(value) else value

    def _collect_params(self) -> dict:
        """遍历 wrapper 收集所有参数值（缺省走 ``parameterDefaultValue``）。"""
        params: dict = {}
        for name, wrapper in self._wrappers.items():
            try:
                value = wrapper.parameterValue()
            except Exception:  # noqa: BLE001
                logger.debug("wrapper %s 取值失败，用默认值", name, exc_info=True)
                try:
                    value = wrapper.parameterDefaultValue()
                except Exception:  # noqa: BLE001
                    value = None
            params[name] = value
        return params

    def _on_execute(self) -> None:
        if self._alg is None:
            MessageBox.critical(self, self._algo_title, "算法未注册或已被卸载")
            return
        if self._task is not None:
            MessageBox.warning(self, self._algo_title, "已有任务正在执行，请等待完成或取消")
            return

        invalid = self._first_invalid_parameter()
        if invalid is not None:
            _name, pdef, widget = invalid
            self._reveal_parameter_widget(widget)
            label = parameter_label(pdef.description() or pdef.name())
            focus_and_highlight(widget, f"请填写「{label}」")
            return

        # 算法自身的组合约束（如矢量转栅格的宽/高与单位），QGIS 参数级
        # 校验（必填 / 最小值）表达不了，执行前先拦下来。
        problem = self._algorithm_specific_problem()
        if problem is not None:
            widget, message = problem
            self._reveal_parameter_widget(widget)
            focus_and_highlight(widget, message)
            return

        params = self._collect_params()
        try:
            params = self._adjust_algorithm_params(params)
        except ValueError as exc:
            # 范围类修正失败：定位到对应的范围控件并提示原因
            extent_name = (
                "PROJWIN"
                if self._algo_id == "gdal:cliprasterbyextent"
                else "TARGET_EXTENT"
            )
            widget = self._widget_for_parameter(extent_name)
            self._reveal_parameter_widget(widget)
            focus_and_highlight(widget, str(exc))
            return
        output_name = (
            self.output_edit.text().strip()
            if self._has_destination and hasattr(self, "output_edit")
            else self._algo_title
        )
        # 输出路径直接取 QGIS destination 控件里用户的选择；临时输出时为 None，
        # 由 service 生成临时文件 / memory 图层。
        output_path = self._selected_output_path() if self._has_destination else None

        job = GenericAlgJob(
            algo_id=self._algo_id,
            params=params,
            output_name=output_name,
            output_path=output_path,
        )

        self.execute_btn.setEnabled(False)
        self.loading.setValue(0)
        self._last_error = ""
        # 上一个任务已结束（_task 为 None），可安全释放其临时输入图层
        self._prepared_params = {}

        # 算法声明 NoThreading → 只能在主线程跑（与 QGIS processing 对话框一致）。
        # 仍推迟到事件栈外，避免按钮点击处理过程中直接跑 processing.run。
        if self._alg.flags() & Qgis.ProcessingAlgorithmFlag.NoThreading:
            QTimer.singleShot(0, lambda: self._run(job))
            return

        # 后台线程执行（QgsProcessingAlgRunnerTask）：算法在 worker 线程跑，
        # 主线程事件循环不阻塞 → 窗口保持响应（GDAL 大栅格曾经卡死 UI）。
        try:
            task, context, feedback, primary_output = self.service.build_task(job)
        except GenericAlgInputError as exc:
            logger.warning("%s 输入检查未通过：%s", job.algo_id, exc)
            self.execute_btn.setEnabled(True)
            MessageBox.warning(self, self._algo_title, str(exc))
            return
        except Exception as exc:
            logger.error("%s 任务准备失败：%s", job.algo_id, exc, exc_info=True)
            self.execute_btn.setEnabled(True)
            MessageBox.critical(
                self, self._algo_title, f"任务准备失败：{exc}"
            )
            return

        # context / feedback 必须持有到任务结束（task 内部是裸指针，被 GC 会悬垂）
        self._task = task
        self._context = context
        self._feedback = feedback
        self._job = job
        self._primary_output = primary_output
        self._prepared_params = task.prepared_params
        self.cancel_btn.setEnabled(True)
        self.cancel_btn.setText("取消")

        task.progress_updated.connect(self._on_task_progress)
        # executed(successful, results) 在主线程回调里装配结果（见 _on_task_executed）
        task.executed.connect(self._on_task_executed)
        # 取消 / 异常终止时 executed 可能不发，用 taskTerminated 兜底复位按钮
        task.taskTerminated.connect(self._on_task_terminated)
        # 算法日志（worker 线程发出）跨线程投递到普通 Python 方法（安全）
        feedback.log_line.connect(self._on_alg_log)
        QgsApplication.taskManager().addTask(task)

    def _run(self, job: GenericAlgJob) -> None:
        """主线程同步执行（``NoThreading`` 算法 / 兜底路径）。

        注意：会阻塞事件循环，仅用于确实无法后台化的算法；常规 GDAL 算法走
        :meth:`_on_execute` 的 ``QgsProcessingAlgRunnerTask`` 后台路径。
        """
        self.loading.setValue(0)
        try:
            result = self.service.run(job)
        except GenericAlgInputError as exc:
            logger.warning("%s 输入检查未通过：%s", job.algo_id, exc)
            MessageBox.warning(self, self._algo_title, str(exc))
            return
        except Exception as exc:
            logger.error("%s 执行失败：%s", job.algo_id, exc, exc_info=True)
            self.loading.setValue(0)
            MessageBox.critical(self, self._algo_title, self._failure_message(str(exc)))
            return
        finally:
            self.execute_btn.setEnabled(True)
        self.loading.setValue(100)
        self._apply_result(job, result)

    # ------------------------------------------------------ 后台任务回调

    @staticmethod
    def _is_disk_space_error(error: str) -> bool:
        text = str(error or "")
        return (
            "Free disk space available" in text
            or "CHECK_DISK_FREE_SPACE" in text
            or "磁盘空间" in text
        )

    @classmethod
    def _failure_message(cls, error: str) -> str:
        """把 GDAL 的原始英文报错转成带原因提示的中文说明。"""
        text = str(error or "")
        if cls._is_disk_space_error(text):
            return (
                "执行失败：输出栅格需要的磁盘空间超过可用空间。\n"
                "常见原因是输入图层的坐标系或分辨率不一致（例如米制与经纬度"
                "混用），导致输出尺寸异常。\n"
                "请检查输入图层，统一坐标系 / 分辨率后再试。\n\n"
                f"原始错误：{text}"
            )
        return f"执行失败：{text}" if text else "执行失败"

    def _on_task_progress(self, value: int) -> None:
        """worker 线程进度跨线程投递到这里（普通 Python 方法，投递安全）。"""
        self.loading.setValue(int(value))

    def _on_alg_log(self, level: str, message: str) -> None:
        """worker 线程算法日志跨线程投递到这里。

        记录一条**有信息量**的 ERROR 供任务失败时展示（GDAL 最后一条错误往往
        只是「进程返回的错误代码 1」，前面那条才有原因）；完整日志写应用日志。
        不弹 UI 日志面板（GDAL 控制台输出量大，messagebar + 日志文件足够）。
        """
        if level == "ERROR":
            detail = self._meaningful_error(message)
            if detail:
                self._last_error = detail
        if level in ("ERROR", "WARNING"):
            logger.warning("[%s] %s", level, message)
        else:
            logger.debug("[%s] %s", level, message)

    @staticmethod
    def _meaningful_error(message: str) -> str:
        """从 GDAL 错误行里挑出可展示给用户的摘要。

        跳过退出码 / Usage / 告警；GDAL 的 Python 工具失败时会甩出整段
        traceback（含 ``^^^^`` 指示行），优先取 ``RuntimeError: ...`` 这种
        异常行，而不是一屏箭头。
        """
        text = str(message).strip()
        if text.startswith("[ERROR]"):
            text = text[len("[ERROR]"):].strip()
        lines = [line.strip() for line in text.splitlines() if line.strip()]
        if not lines:
            return ""
        first = lines[0]
        lowered_first = first.lower()
        if (
            first.startswith("Usage:")
            or lowered_first.startswith("warning")
            or "error code" in lowered_first
            or "错误代码" in first
        ):
            return ""

        def _noise(line: str) -> bool:
            lowered = line.lower()
            if "error code" in lowered or "错误代码" in line:
                return True
            if line.startswith("Usage:") or lowered.startswith("warning"):
                return True
            if line.startswith("Traceback") or line.startswith('File "'):
                return True
            return set(line) <= {"^", "~", "-"}

        candidates = [line for line in lines if not _noise(line)]
        for line in candidates:
            if line.startswith("ERROR") or re.match(
                r"^(?:[A-Za-z_][A-Za-z0-9_.]*Error|Exception)\b", line
            ):
                return line[:300]
        return candidates[0][:300] if candidates else ""

    def _on_task_executed(self, ok: bool, results: dict) -> None:
        """后台任务结束（``QgsProcessingAlgRunnerTask.executed``，主线程回调）。

        先快照再复位：任务随时可能被 ``QgsTaskManager`` 删除，不能再访问任务成员；
        ``context`` / ``feedback`` 继续保留到下次执行（任务收尾阶段仍会用到它们，
        且这里无法确定 ``executed`` 与任务析构的先后顺序）。
        """
        if self._task is None:
            return  # 已由 _on_task_terminated 处理（取消 / 异常终止）
        job = self._job
        primary_output = self._primary_output
        feedback = self._feedback
        error = self._last_error
        if job is None:
            return
        self._reset_task_ui()

        canceled = feedback is not None and feedback.isCanceled()
        command_failed = bool(getattr(feedback, "failed", False))

        if canceled:
            self.loading.setValue(0)
            send("warning", f"已取消 {job.algo_id}", duration=3)
            return
        if not ok or command_failed:
            self.loading.setValue(0)
            detail = f"：{error}" if error else ""
            logger.error("%s 执行失败%s", job.algo_id, detail)
            MessageBox.critical(self, self._algo_title, self._failure_message(error))
            return

        # GDAL 命令行工具非零退出时，QGIS 的算法包装只把错误写进日志、并不
        # 让任务失败（且错误文案会被翻译，光靠 feedback.failed 不可靠）。
        # 这里再用「输出文件是否真的落盘」兜一层，避免把执行失败误报成
        # 「结果装配失败：输出栅格无法加载」。
        missing_output = self._missing_output_path(results or {}, primary_output)
        if missing_output:
            self.loading.setValue(0)
            detail = f"：{error}" if error else ""
            logger.error("%s 执行失败：输出文件未生成 %s%s", job.algo_id, missing_output, detail)
            message = (
                self._failure_message(error)
                if self._is_disk_space_error(error)
                else f"执行失败：输出文件未生成{detail}"
            )
            MessageBox.critical(self, self._algo_title, message)
            return

        self.loading.setValue(100)
        try:
            result = self.service.wrap_results(job, results or {}, primary_output)
        except Exception as exc:
            logger.error("%s 结果装配失败：%s", job.algo_id, exc, exc_info=True)
            MessageBox.critical(self, self._algo_title, f"结果装配失败：{exc}")
            return
        self._apply_result(job, result)

    def _on_task_terminated(self) -> None:
        """任务异常终止 / 取消（``executed`` 未送达时的兜底）。

        ``QgsProcessingAlgRunnerTask`` 被取消时可能直接终止而不发 ``executed``，
        必须在此复位按钮状态，否则对话框会卡在「执行中」。
        """
        if self._task is None:
            return  # 已由 _on_task_executed 处理
        job = self._job
        feedback = self._feedback
        error = self._last_error
        self._reset_task_ui()
        self.loading.setValue(0)
        if job is None:
            return
        if feedback is not None and feedback.isCanceled():
            send("warning", f"已取消 {job.algo_id}", duration=3)
            return
        detail = f"：{error}" if error else ""
        logger.error("%s 执行失败%s", job.algo_id, detail)
        MessageBox.critical(self, self._algo_title, self._failure_message(error))

    def _reset_task_ui(self) -> None:
        """复位「执行中」状态（按钮 / 任务引用），保留 context / feedback。"""
        self._task = None
        self._job = None
        self._primary_output = None
        self._last_error = ""
        self.execute_btn.setEnabled(True)
        self.cancel_btn.setEnabled(False)
        self.cancel_btn.setText("取消")

    def _on_cancel(self) -> None:
        """请求取消后台任务（转发到 ``feedback.cancel()``，算法尽早停止）。"""
        task = self._task
        if task is None:
            return
        try:
            task.cancel()
        except RuntimeError:
            # 任务已完成，C++ 对象已被 QgsTaskManager 删除
            self._task = None
            return
        self.cancel_btn.setEnabled(False)
        self.cancel_btn.setText("取消中…")

    def closeEvent(self, event) -> None:
        """关闭对话框时取消在跑的后台任务（实例被工具树复用）。"""
        self._on_cancel()
        super().closeEvent(event)

    def _apply_result(self, job: GenericAlgJob, result) -> None:
        """按 service 返回类型装配地图 / 结果路径（主线程）。"""
        if isinstance(result, dict) and result.get(RESULT_IS_PATH):
            value = str(result.get("value", "") or "")
            outputs = result.get("outputs") or ([("", value)] if value else [])
            self._show_result_paths(outputs)
            if outputs:
                logger.info("%s 完成，输出文件：%s", job.algo_id, value)
                if len(outputs) == 1:
                    send("success", f"{job.algo_id} 完成：{outputs[0][1]}", duration=6)
                else:
                    send(
                        "success",
                        f"{job.algo_id} 完成，共 {len(outputs)} 个输出文件"
                        "（路径见对话框底部）",
                        duration=6,
                    )
            else:
                send(
                    "success",
                    f"{job.algo_id} 已完成（该算法无文件输出，详见日志）",
                    duration=4,
                )
            return

        if isinstance(result, QgsMapLayer):
            self._show_result_paths([])
            self.project.addMapLayer(result)
            fc = ""
            try:
                fc = f"，{result.featureCount()} 个要素"
            except Exception:  # noqa: BLE001
                fc = ""
            send(
                "success",
                f"{job.algo_id} 完成：{result.name()}{fc}",
                duration=3,
            )
            QTimer.singleShot(0, self._refresh_canvas)
            return

        # 兜底：未知返回类型
        self._show_result_paths([])
        send("success", f"{job.algo_id} 已完成", duration=3)

    def _show_result_paths(self, outputs) -> None:
        """在底部结果面板列出 ``(输出名, 路径)``；空列表时隐藏面板。

        路径用只读输入框展示（可选中 / 复制），每行带「打开」与「打开所在
        文件夹」，避免像以前那样只在主窗口 messagebar 一闪而过。
        """
        layout = self._result_layout
        while layout.count():
            item = layout.takeAt(0)
            widget = item.widget()
            if widget is not None:
                widget.setParent(None)
                widget.deleteLater()
        if not outputs:
            self._result_box.setVisible(False)
            return

        layout.addWidget(QLabel("输出文件"))
        for name, path in outputs:
            row = QWidget()
            row_layout = QHBoxLayout(row)
            row_layout.setContentsMargins(0, 0, 0, 0)
            row_layout.setSpacing(6)
            label = _FILE_OUTPUT_LABELS.get(name, "输出文件")
            row_layout.addWidget(QLabel(f"{label}："))
            edit = QLineEdit(path)
            edit.setReadOnly(True)
            edit.setCursorPosition(0)
            edit.setToolTip(path)
            row_layout.addWidget(edit, 1)
            open_btn = QPushButton("打开")
            open_btn.setToolTip("用默认程序打开该文件")
            open_btn.clicked.connect(
                lambda _checked=False, p=path: self._open_result_file(p)
            )
            row_layout.addWidget(open_btn)
            reveal_btn = QPushButton("打开所在文件夹")
            reveal_btn.setToolTip("在文件管理器中定位该文件")
            reveal_btn.clicked.connect(
                lambda _checked=False, p=path: reveal_in_file_manager(p)
            )
            row_layout.addWidget(reveal_btn)
            layout.addWidget(row)
        self._result_box.setVisible(True)
        # 结果行会占用底部高度：窗口高度不变时由参数区滚动条让位，
        # 还没到 900 上限则顺势长高一点。
        self._fit_to_visible_content()

    @staticmethod
    def _open_result_file(path: str) -> None:
        if not path or not os.path.exists(path):
            send("warning", f"文件不存在：{path}", duration=4)
            return
        if not QDesktopServices.openUrl(QUrl.fromLocalFile(path)):
            send("warning", f"打开文件失败：{path}", duration=4)

    def _refresh_canvas(self) -> None:
        canvas = (
            self.parent().canvas
            if self.parent() and hasattr(self.parent(), "canvas")
            else None
        )
        if canvas is not None:
            canvas.refresh()